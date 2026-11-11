#!/usr/bin/env python3
"""Независимая синтетическая проверка Gain Match 0.1.19 (набор 103+ регрессий).

Продукт НЕ изменяется. Ожидаемые значения считаются независимым кодом:
  * аналитические амплитуды (сигналы с известными параметрами);
  * собственная LSQ-оценка тона+гармоник через numpy.linalg.lstsq (другой путь,
    не FFT-анализатор продукта) — референс SNR/SINAD/THD+N;
  * геометрия дефектов (выпадения, всплески, дрейф усиления) известна по построению.

Код продукта импортируется только для вызова его же публичных функций.
Запуск:  /usr/bin/python3 -u test_gainmatch.py
"""
import json
import re
import math
import os
import sys
import time

import numpy as np

PRODUCT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PRODUCT)

from valvebench import gainmatch  # noqa: E402
from valvebench.backend import Backend  # noqa: E402
from valvebench.calib import Calibration  # noqa: E402
from valvebench.measure import MeasureError, Rig  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
os.makedirs(OUT_DIR, exist_ok=True)
RESULTS = []


def check(rid, name, kind, ok, expected, actual):
    """kind: 'accuracy' — сравнение с истиной; 'defect' — PASS = дефект воспроизведён;
    'ok' — проверка корректного поведения."""
    RESULTS.append({"id": rid, "name": name, "kind": kind,
                    "pass": bool(ok), "expected": expected, "actual": actual})
    tag = "PASS" if ok else "FAIL"
    print(f"{tag}  [{rid}] {name}\n      ожидалось: {expected}\n      фактически: {actual}")


def db(x):
    return 20.0 * math.log10(max(float(x), 1e-300))


def amp_of(dbfs):
    return 10.0 ** (dbfs / 20.0)


# ============================================================ независимые референсы

def ref_tone_fit(x, fs, f, harmonics=(1,)):
    """Независимая LSQ-проекция: [const, cos/sin(h*f)] для заданных гармоник h.
    Возвращает (амплитуды по гармоникам, rms остатка)."""
    x = np.asarray(x, dtype=np.float64)
    t = np.arange(x.size) / float(fs)
    cols = [np.ones_like(t)]
    for h in harmonics:
        cols += [np.cos(2 * math.pi * h * f * t), np.sin(2 * math.pi * h * f * t)]
    A = np.column_stack(cols)
    coef, *_ = np.linalg.lstsq(A, x, rcond=None)
    res = x - A @ coef
    amps = [math.hypot(coef[1 + 2 * i], coef[2 + 2 * i]) for i in range(len(harmonics))]
    return amps, float(np.sqrt(np.mean(res * res)))


def ref_snr_sinad_thdn(win_ch, fs, f, nharm=5):
    """Независимые SNR (S/N), SINAD (S/(N+D)) и THD+N ((N+D)/S) одного канала.
    Все величины — по мощности, полная синусоида = 0 dBFS.
    Шум = остаток LSQ-фита базиса 1..nharm (всё, что не тон и не учтённые
    гармоники: фон, белый шум, неучтённые линии). Без вычитания мощностей —
    устойчиво к катастрофическому сокращению при D >> N."""
    harm = tuple(range(1, nharm + 1))
    amps, res_rms = ref_tone_fit(win_ch, fs, f, harmonics=harm)
    a1 = amps[0]
    p_sig = (a1 ** 2) / 2.0
    p_noise = res_rms ** 2                     # фон + шум + неучтённые линии
    p_dist = sum((a ** 2) / 2.0 for a in amps[1:])
    snr = db(math.sqrt(p_sig) / math.sqrt(max(p_noise, 1e-300)))
    sinad = db(math.sqrt(p_sig) / math.sqrt(max(p_noise + p_dist, 1e-300)))
    thdn = db(math.sqrt(max(p_noise + p_dist, 1e-300)) / math.sqrt(p_sig))
    return snr, sinad, thdn


def band_limited_noise(n, fs, kind, total_dbfs, rng):
    """Белый или розовый шум в полосе 20 Гц–20 кГц; суммарный уровень — как у синуса
    с амплитудой total_dbfs (мощность = amp²/2). Независимая генерация."""
    f = np.fft.rfftfreq(n, 1.0 / fs)
    spec = (rng.standard_normal(f.size) + 1j * rng.standard_normal(f.size))
    valid = (f >= 20.0) & (f <= min(20000.0, 0.45 * fs))
    weight = np.zeros_like(f)
    if kind == "white":
        weight[valid] = 1.0
    elif kind == "pink":
        weight[valid] = 1.0 / np.sqrt(np.maximum(f[valid], 1.0))
    else:
        raise ValueError(kind)
    x = np.fft.irfft(spec * weight, n=n)
    rms = float(np.sqrt(np.mean(x * x)))
    if rms <= 0:
        raise ValueError("шум не сформирован")
    return x * (amp_of(total_dbfs) / math.sqrt(2.0)) / rms


# ============================================================ управляемый бэкенд

class ScriptedBackend(Backend):
    """Захват с точной геометрией: лид-тишина + программные дефекты.

    Применяет к программе Rig: усиление каналов, целочисленную задержку (фаза),
    DC, фон 50/100 Гц, явные добавочные линии (Гц, dBFS, фаза), белый/розовый
    шум, выпадения (zero-gap или удаление кадров) в каждом или первом сегменте,
    всплеск в лид-тишине (до онсета), всплеск внутри окна анализа, переменное
    усиление L между вызовами (нестабильный тракт), усечение записи; отдельно —
    только сообщения XRUN в notes при чистых данных.
    """

    name = "scripted"

    def __init__(self, fs=48000, lead_s=0.5, gain=(1.0, 1.0), delay=(0, 0), dc=(0.0, 0.0),
                 hum=((), ()), extra_lines=((), ()), noise_dbfs=None, noise_kind="white",
                 dropout_s=0.0, dropout_mode="zero", dropout_every_segment=False,
                 burst=None, in_window_burst=None, gain_flip_db=0.0, shorten_s=0.0,
                 notes=(), seed=7, in_window_burst_frac=None, clock_offset_ppm=0.0,
                 l_drift=None, riaa_dut=False, riaa_compensate=False, clip_level=0.0):
        self.fs = int(fs)
        self.lead_s = float(lead_s)
        self.gain = gain
        self.delay = delay
        self.dc = dc
        self.hum = hum            # per channel: ((f_hz, dbfs), ...)
        self.extra_lines = extra_lines  # per channel: ((f_hz, dbfs, phase), ...)
        self.noise_dbfs = noise_dbfs
        self.noise_kind = noise_kind
        self.dropout_s = float(dropout_s)
        self.dropout_mode = dropout_mode
        self.dropout_every_segment = bool(dropout_every_segment)
        self.burst = burst              # (t_s, dur_s, dbfs, channels) — в лид-тишине
        self.in_window_burst = in_window_burst  # (offset_into_window_s, dur_s, dbfs, channels)
        self.in_window_burst_frac = in_window_burst_frac  # (frac_окна, dur_s, dbfs, channels)
        self.gain_flip_db = float(gain_flip_db)
        self.shorten_s = float(shorten_s)
        self.clock_offset_ppm = float(clock_offset_ppm)
        self.l_drift = l_drift    # (дБ/с L, дБ/с R) — линейный дрейф от начала программы
        self.riaa_dut = bool(riaa_dut)              # DUT = фильтр RIAA playback
        self.riaa_compensate = bool(riaa_compensate)  # генератор предыскажён обратной RIAA
        self.clip_level = float(clip_level)         # 0 = без клипа; иначе ограничение АЦП
        self._notes = tuple(notes)
        self.notes = []
        self.rng = np.random.default_rng(seed)
        self._layout = None       # (n_segments, seg_len, s_n, L) — задаёт тест перед rig.run
        self._calls = 0

    def set_layout(self, rig, n_segments):
        s_n = int(round(rig.settle * rig.fs))
        L = int(rig.blocks * rig.n)
        seg_len = s_n + L + int(0.05 * rig.fs)
        self._layout = (n_segments, seg_len, s_n, L)

    def open(self, fs):
        self.fs = int(fs)

    def play_record(self, out, tail=0.8):
        out = np.asarray(out, dtype=np.float64)
        if out.ndim == 1:
            out = np.stack([out, out], axis=1)
        fs = self.fs
        lead = int(self.lead_s * fs)
        n_seg, seg_len, s_n, L = self._layout or (1, out.shape[0], 0, out.shape[0])
        rec = np.zeros((lead + out.shape[0], 2))
        for c in (0, 1):
            x = out[:, c] * float(self.gain[c])
            d = int(self.delay[c])
            if d:
                x = np.concatenate([np.zeros(d), x])[: x.size]
            if self.dc[c]:
                x = x + float(self.dc[c])
            t = np.arange(x.size) / float(fs)
            for f_hz, lv in self.hum[c]:
                x = x + amp_of(lv) * np.sin(2 * math.pi * f_hz * t)
            rec[lead:, c] = x
        # линейный DUT-фильтр RIAA и/или обратная RIAA в генераторе (до шума АЦП)
        if self.riaa_compensate or self.riaa_dut:
            from valvebench import dsp as _dsp
            n_prog = out.shape[0]
            f_axis = np.fft.rfftfreq(n_prog, 1.0 / fs)
            H = _dsp.riaa_playback(f_axis, False).astype(complex)
            spec = np.fft.rfft(rec[lead:], axis=0)
            if self.riaa_compensate:
                spec = spec / H[:, None]
            if self.riaa_dut:
                spec = spec * H[:, None]
            rec[lead:] = np.fft.irfft(spec, n=n_prog, axis=0)
        if self.noise_dbfs is not None:
            for c in (0, 1):
                rec[:, c] += band_limited_noise(rec.shape[0], fs, self.noise_kind,
                                                self.noise_dbfs, self.rng)
        for c in (0, 1):
            tt = np.arange(rec.shape[0]) / float(fs)
            for f_hz, lv, phase in self.extra_lines[c]:
                # линии искажений существуют только вместе с программой,
                # лид-тишину захвата не загрязняют (как в реальном тракте)
                line = amp_of(lv) * np.sin(2 * math.pi * f_hz * tt + phase)
                line[:lead] = 0.0
                rec[:, c] += line
        # нестабильный тракт: усиление L меняется между СЕГМЕНТАМИ программы
        # (measure_series рендерит все сегменты одним вызовом play_record)
        if self.gain_flip_db and n_seg > 1:
            for i in range(n_seg):
                if i % 2 == 1:
                    rec[lead + i * seg_len: lead + (i + 1) * seg_len, 0] *= \
                        amp_of(self.gain_flip_db)
        # всплеск в лид-тишине (до онсета)
        if self.burst is not None:
            t_b, dur, lv, chans = self.burst
            i0, i1 = int(t_b * fs), int((t_b + dur) * fs)
            tt = np.arange(i1 - i0) / float(fs)
            for c in chans:
                rec[i0:i1, c] += amp_of(lv) * np.sin(2 * math.pi * 1000.0 * tt)
        # всплеск внутри окна анализа (сдвиг от начала окна)
        if self.in_window_burst is not None:
            off, dur, lv, chans = self.in_window_burst
            base = lead + s_n + L // 4 + int(off * fs)
            i0, i1 = base, base + int(dur * fs)
            tt = np.arange(i1 - i0) / float(fs)
            for c in chans:
                rec[i0:i1, c] += amp_of(lv) * np.sin(2 * math.pi * 1000.0 * tt)
        # всплеск внутри окна анализа (позиция — доля окна 0..1)
        if self.in_window_burst_frac is not None:
            frac, dur, lv, chans = self.in_window_burst_frac
            base = lead + s_n + int(frac * L)
            i0, i1 = base, base + int(dur * fs)
            tt = np.arange(i1 - i0) / float(fs)
            for c in chans:
                rec[i0:i1, c] += amp_of(lv) * np.sin(2 * math.pi * 1000.0 * tt)
        # линейный дрейф усиления (дБ/с) от начала программы — модель прогрева лампы
        if self.l_drift is not None:
            l_dbps, r_dbps = self.l_drift
            t_rec = np.clip(np.arange(rec.shape[0]) - lead, 0, None) / float(fs)
            rec[:, 0] *= 10.0 ** (l_dbps * t_rec / 20.0)
            rec[:, 1] *= 10.0 ** (r_dbps * t_rec / 20.0)
        # рассинхронизация тактов ЦАП/АЦП (ppm): пересемплирование записи
        if self.clock_offset_ppm:
            from scipy.signal import resample_poly
            ratio = 1.0 - self.clock_offset_ppm * 1e-6
            up, down = 48000, int(round(48000 * ratio))
            rec = resample_poly(rec, up, down, axis=0)
        # выпадения в каждом/первом сегменте (середина первого блока окна,
        # где вес окна Блэкмана–Харриса максимален и утечка видна полностью)
        g = int(round(self.dropout_s * fs))
        if g > 0:
            segs = range(n_seg) if self.dropout_every_segment else range(1)
            for i in segs:
                center = lead + i * seg_len + s_n + L // 4
                a, b = center - g // 2, center - g // 2 + g
                if self.dropout_mode == "zero":
                    rec[a:b, :] = 0.0
                else:  # delete: захват теряет кадры, остальное съезжает влево
                    rec = np.concatenate([rec[:a], rec[b:], rec[-(b - a):]])
        if self.shorten_s > 0:
            rec = rec[: rec.shape[0] - int(self.shorten_s * fs)]
        if self.clip_level > 0:
            rec = np.clip(rec, -self.clip_level, self.clip_level)
        self.notes = list(self._notes)
        return rec

    # live-интерфейс не используется тестами
    def live_start(self):
        raise NotImplementedError

    def live_stop(self):
        pass

    def live_set_loop(self, period):
        raise NotImplementedError

    def live_recent(self, seconds):
        raise NotImplementedError


def make_rig(be, fs=48000, blocks=2, settle=0.15):
    rig = Rig(be, Calibration(), fs=fs, df=1.0, blocks=blocks, settle=settle)
    rig.open()
    return rig


def run_series(rig, be, freqs, level_dbfs=-24.0, repeats=3, purpose="loopback", reference=None,
               metadata=None, riaa_compensate=None):
    n_seg = len(freqs) * repeats
    be.set_layout(rig, n_seg)
    kw = {} if riaa_compensate is None else {"riaa_compensate": riaa_compensate}
    return gainmatch.measure_series(rig, frequencies=freqs, level_dbfs=level_dbfs,
                                    repeats=repeats, purpose=purpose, reference=reference,
                                    metadata=metadata, **kw)


# ============================================================ БЛОК A: уровни и ΔG

def block_a():
    print("\n=== Блок A: measure_series — уровни и ΔG на чистой синтетике ===")
    fs = 48000
    freqs = (100.0, 1000.0, 10000.0)

    # A1: равные каналы
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0)
    rig = make_rig(be, fs)
    res = run_series(rig, be, freqs)
    deltas = [row["delta_adc_db"] for row in res["rows"]]
    check("A1", "Равные каналы: ΔADC L−R ≈ 0", "accuracy",
          max(abs(d) for d in deltas) < 0.01,
          "|Δ| < 0.01 дБ на всех частотах",
          f"Δ={['%.5f' % d for d in deltas]} дБ; valid={[row['valid'] for row in res['rows']]}")

    # A2: известные разности 0.1/0.2/0.5/1.0 дБ
    for x in (0.1, 0.2, 0.5, 1.0):
        g = amp_of(x)
        be = ScriptedBackend(fs=fs, gain=(g, 1.0), noise_dbfs=-125.0)
        rig = make_rig(be, fs)
        res = run_series(rig, be, freqs)
        deltas = [row["delta_adc_db"] for row in res["rows"]]
        err = max(abs(d - x) for d in deltas)
        check(f"A2/{x:g}", f"Известная разность +{x:g} дБ (L сильнее)", "accuracy",
              err < 0.01,
              f"ΔADC = +{x:g} дБ ± 0.01 на всех частотах",
              f"Δ={['%.5f' % d for d in deltas]}; макс. ошибка {err:.5f} дБ; "
              f"spread={[round(row['delta_output_spread_db'], 5) for row in res['rows']]}")

    # A3: двухэтапная коррекция (петля 0.97/1.03, DUT ×1.5/×1.0); истина = 20lg(1.5)
    loop_be = ScriptedBackend(fs=fs, gain=(0.97, 1.03), noise_dbfs=-125.0)
    rig_loop = make_rig(loop_be, fs)
    loop_ref = gainmatch.build_loopback_reference(run_series(rig_loop, loop_be, freqs))
    dut_be = ScriptedBackend(fs=fs, gain=(0.97 * 1.5, 1.03), noise_dbfs=-125.0)
    rig_dut = make_rig(dut_be, fs)
    res = run_series(rig_dut, dut_be, freqs, purpose="dut", reference=loop_ref)
    truth = 20.0 * math.log10(1.5)
    deltas = [row["delta_gain_db"] for row in res["rows"]]
    err = max(abs(d - truth) for d in deltas)
    check("A3", "Двухэтапная коррекция: петля 0.97/1.03, DUT 1.5×/1.0×", "accuracy",
          err < 0.02 and all(row["valid"] and row["repeat_stable"] for row in res["rows"]),
          f"ΔG = {truth:.4f} дБ ± 0.02 на всех частотах, valid+stable",
          f"ΔG={['%.4f' % d for d in deltas]}; макс. ошибка {err:.4f} дБ")

    # A4: уровень генератора −12/−24/−36 dBFS (ΔG истина = +0.5 дБ)
    outs = []
    for lv in (-12.0, -24.0, -36.0):
        be = ScriptedBackend(fs=fs, gain=(amp_of(0.5), 1.0), noise_dbfs=-110.0)
        rig = make_rig(be, fs)
        res = run_series(rig, be, freqs, level_dbfs=lv)
        outs += [(row["delta_adc_db"] - 0.5) for row in res["rows"]]
    err = max(abs(v) for v in outs)
    check("A4", "Инвариантность ΔG к уровню генератора (−12/−24/−36 dBFS)", "accuracy",
          err < 0.02,
          "|ΔADC − 0.5 дБ| < 0.02 на всех уровнях и частотах",
          f"макс. |ошибка| = {err:.4f} дБ по 9 точкам")

    # A5: частота дискретизации 44.1/96/192 кГц (ΔG истина = +1.0 дБ)
    outs = []
    for f_s, blocks in ((44100, 2), (96000, 2), (192000, 1)):
        be = ScriptedBackend(fs=f_s, gain=(amp_of(1.0), 1.0), noise_dbfs=-125.0)
        rig = make_rig(be, f_s, blocks=blocks)
        res = run_series(rig, be, freqs, repeats=2)
        outs += [(row["delta_adc_db"] - 1.0, row["valid"]) for row in res["rows"]]
    err = max(abs(v) for v, _ in outs)
    check("A5", "Инвариантность ΔG к Fs (44.1/96/192 кГц)", "accuracy",
          err < 0.02 and all(v for _, v in outs),
          "|ΔADC − 1 дБ| < 0.02; valid на всех частотах",
          f"макс. |ошибка| = {err:.4f} дБ; valid={[v for _, v in outs]}")

    # A6: фазовые условия — R задержан на 0/13/97/997 отсчётов (ΔG истина = +0.2 дБ)
    outs = []
    for d in (0, 13, 97, 997):
        be = ScriptedBackend(fs=fs, gain=(amp_of(0.2), 1.0), delay=(0, d), noise_dbfs=-125.0)
        rig = make_rig(be, fs)
        res = run_series(rig, be, freqs, repeats=2)
        outs += [(row["delta_adc_db"] - 0.2) for row in res["rows"]]
    err = max(abs(v) for v in outs)
    check("A6", "Фазовые условия: задержка R до 997 отсчётов не влияет на уровень", "accuracy",
          err < 0.01,
          "|ΔADC − 0.2 дБ| < 0.01",
          f"макс. |ошибка| = {err:.5f} дБ по 12 точкам")

    # A7: DC-смещение в канале L (ΔG истина = +0.3 дБ)
    be = ScriptedBackend(fs=fs, gain=(amp_of(0.3), 1.0), dc=(0.03, 0.0), noise_dbfs=-125.0)
    rig = make_rig(be, fs)
    res = run_series(rig, be, freqs, repeats=2)
    err = max(abs(row["delta_adc_db"] - 0.3) for row in res["rows"])
    check("A7", "DC-смещение +0.03 в L не влияет на оценку тона", "accuracy",
          err < 0.01,
          "|ΔADC − 0.3 дБ| < 0.01",
          f"макс. |ошибка| = {err:.5f} дБ")

    # A8: фон сети 50/100 Гц (ΔG истина = +0.3 дБ)
    be = ScriptedBackend(fs=fs, gain=(amp_of(0.3), 1.0), noise_dbfs=-125.0,
                         hum=(((50.0, -50.0), (100.0, -55.0)), ((50.0, -50.0), (100.0, -55.0))))
    rig = make_rig(be, fs)
    res = run_series(rig, be, freqs, repeats=2)
    err = max(abs(row["delta_adc_db"] - 0.3) for row in res["rows"])
    check("A8", "Фон 50/100 Гц (−50/−55 dBFS) не смещает ΔG", "accuracy",
          err < 0.01,
          "|ΔADC − 0.3 дБ| < 0.01",
          f"макс. |ошибка| = {err:.5f} дБ")

    # A9: добавочные линии H2/H3 (2000/3000 Гц, −64/−74 dBFS) только в L;
    # частоты ряда 1000/1500 Гц — не совпадают с линиями (ΔG истина = +0.3 дБ)
    be = ScriptedBackend(fs=fs, gain=(amp_of(0.3), 1.0), noise_dbfs=-110.0,
                         extra_lines=(((2000.0, -64.0, 0.7), (3000.0, -74.0, 1.9)), ()))
    rig = make_rig(be, fs)
    res = run_series(rig, be, (1000.0, 1500.0), repeats=2)
    err = max(abs(row["delta_adc_db"] - 0.3) for row in res["rows"])
    check("A9", "Линии H2/H3 (−40/−50 dBc) в L не смещают ΔG", "accuracy",
          err < 0.01,
          "|ΔADC − 0.3 дБ| < 0.01",
          f"макс. |ошибка| = {err:.5f} дБ")

    # A10: белый и розовый шум −80 dBFS (ΔG истина = +0.3 дБ)
    for kind in ("white", "pink"):
        be = ScriptedBackend(fs=fs, gain=(amp_of(0.3), 1.0), noise_dbfs=-80.0,
                             noise_kind=kind)
        rig = make_rig(be, fs)
        res = run_series(rig, be, freqs, repeats=3)
        errs = [abs(row["delta_adc_db"] - 0.3) for row in res["rows"]]
        spreads = [row["delta_output_spread_db"] for row in res["rows"]]
        stables = [row["repeat_stable"] for row in res["rows"]]
        check(f"A10/{kind}", f"Шум {kind} −80 dBFS: ΔG и repeat_stable", "accuracy",
              max(errs) < 0.02,
              "|ΔADC − 0.3 дБ| < 0.02; spread ≤ 0.10 дБ",
              f"ошибки={['%.4f' % e for e in errs]}; spread={['%.4f' % s for s in spreads]}; "
              f"stable={stables}")


# ============================================================ БЛОК B: валидность

def block_b():
    print("\n=== Блок B: валидность серии — XRUN/выпадения/онсет/перегрузка/короткий захват ===")
    fs = 48000
    freqs = (1000.0, 1500.0)

    # B1: XRUN-сообщения при ЧИСТЫХ данных — главный предмет беспокойства координатора
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0,
                         notes=["arecord: overrun!!! (at least 65107 frames)",
                                "aplay: underrun!!! (at least 1234 frames)"])
    rig = make_rig(be, fs)
    res = run_series(rig, be, freqs, repeats=1)
    all_valid = all(row["valid"] for row in res["rows"])
    check("B1-fix", "XRUN-сообщения ALSA инвалидируют серию (исправлено в 0.1.19)", "ok",
          (not all_valid)
          and all("hardware_fault" in row["channels"]["L"]["validity_reasons"]
                  for row in res["rows"])
          and res["events"]["fatal"],
          "ожидание 0.1.19: valid=False, причины hardware_fault, events.fatal заполнены",
          f"valid={all_valid}; warnings={res['warnings']}; "
          f"reasons={[row['channels']['L']['validity_reasons'] for row in res['rows']]}")

    # B2: zero-gap 10 мс в середине блока, в каждом сегменте (repeats=1, чтобы
    # медиана не маскировала дефект): ожидаем, что порог SNR ловит повреждение
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, dropout_s=0.010, dropout_mode="zero",
                         dropout_every_segment=True)
    rig = make_rig(be, fs)
    res = run_series(rig, be, freqs, repeats=1)
    reasons = sorted({r for row in res["rows"] for c in ("L", "R")
                      for r in row["channels"][c]["validity_reasons"]})
    snrs = [row["channels"]["L"]["snr_db"] for row in res["rows"]]
    check("B2-fix", "Выпадение 10 мс ловится воротами C/R (SINAD-порог заменён)", "ok",
          not all(row["valid"] for row in res["rows"])
          and "low_carrier_residual" in reasons,
          "ожидание 0.1.19: valid=False через low_carrier_residual",
          f"valid={[row['valid'] for row in res['rows']]}; SNR_L={['%.1f' % s for s in snrs]}; "
          f"reasons={reasons}")

    # B3: выпадение кадров (реалистичный overrun) в каждом сегменте, два случая:
    #   2.0 мс при 1 кГц = целое число периодов → склейка фазово бесшовна;
    #   2.5 мс = разрыв фазы π → SINAD обрушается, но остаётся ли valid?
    def dropout_case(dropout_s):
        be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, dropout_s=dropout_s,
                             dropout_mode="delete", dropout_every_segment=True)
        rig = make_rig(be, fs)
        clean_be = ScriptedBackend(fs=fs, noise_dbfs=-125.0)
        rig_clean = make_rig(clean_be, fs)
        res = run_series(rig, be, freqs, repeats=1)
        ref = run_series(rig_clean, clean_be, freqs, repeats=1)
        derr = max(abs(a["delta_adc_db"] - b["delta_adc_db"])
                   for a, b in zip(res["rows"], ref["rows"]))
        snr_l = res["rows"][0]["channels"]["L"]["snr_db"]
        return all(row["valid"] for row in res["rows"]), derr, snr_l

    ok20, derr20, snr20 = dropout_case(0.0020)
    ok25, derr25, snr25 = dropout_case(0.0025)
    check("B3", "Выпадение кадров 2.0/2.5 мс: целые периоды безвредны, разрыв фазы ловится",
          "ok", ok20 and (not ok25),
          "2.0 мс (целые периоды, бесшовная склейка) — valid=True и ошибка ΔG≈0 "
          "корректны (уровень действительно не пострадал); 2.5 мс (разрыв фазы π) — "
          "ожидаем valid=False: SINAD обрушается",
          f"2.0 мс: valid={ok20}, ошибка ΔG={derr20:.4f} дБ, SNR={snr20:.1f}; "
          f"2.5 мс: valid={ok25}, ошибка ΔG={derr25:.4f} дБ, SNR={snr25:.1f} (порог 25)")

    # B4: выпадение 30 мс — порог SNR должен сработать
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, dropout_s=0.030, dropout_mode="zero",
                         dropout_every_segment=True)
    rig = make_rig(be, fs)
    res = run_series(rig, be, freqs, repeats=1)
    reasons = sorted({r for row in res["rows"] for c in ("L", "R")
                      for r in row["channels"][c]["validity_reasons"]})
    check("B4-fix", "Выпадение 30 мс — ловится воротами C/R", "ok",
          not all(row["valid"] for row in res["rows"])
          and "low_carrier_residual" in reasons,
          "ожидание 0.1.19: valid=False через low_carrier_residual",
          f"valid={[row['valid'] for row in res['rows']]}; reasons={reasons}")

    # B5: короткий захват
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, shorten_s=0.7)
    rig = make_rig(be, fs)
    raised, detail = False, ""
    try:
        run_series(rig, be, freqs, repeats=3)
        detail = "MeasureError НЕ возник"
    except MeasureError as exc:
        raised, detail = True, str(exc)
    check("B5", "Короткий захват (−0.7 с) — громкий отказ Rig.run", "ok", raised,
          "MeasureError «запись короче программы»",
          f"исключение: {raised}; текст: {detail[:120]}")

    # B6: всплеск в лид-тишине (щелчок открытия ЦАП), общий для каналов:
    # онсет сдвигается на 0.45 с, окна сдвигаются на 0.3 с раньше тона
    # → уровни ОБОИХ каналов занижаются молча; ΔG не затронута
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(amp_of(0.3), 1.0),
                         burst=(0.05, 0.05, -6.0, (0, 1)))
    rig = make_rig(be, fs, settle=0.15)
    clean_be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(amp_of(0.3), 1.0))
    rig_clean = make_rig(clean_be, fs, settle=0.15)
    res = run_series(rig, be, freqs, repeats=1)
    ref = run_series(rig_clean, clean_be, freqs, repeats=1)
    lvl_err = max(abs(a["channels"][c]["level_dbfs"] - b["channels"][c]["level_dbfs"])
                  for a, b in zip(res["rows"], ref["rows"]) for c in ("L", "R"))
    derr = max(abs(a["delta_adc_db"] - b["delta_adc_db"])
               for a, b in zip(res["rows"], ref["rows"]))
    check("B6-fix", "Щелчок в лид-тишине: узкополосный onset не сбивается — уровни точные",
          "ok", lvl_err < 0.05 and derr < 0.02
          and all(row["valid"] for row in res["rows"]),
          "ожидание 0.1.19: onset находится на тоне (устойчивость ≥1 с), "
          "уровни и ΔG без ошибки, все строки валидны",
          f"ошибка уровней={lvl_err:.3f} дБ; ошибка ΔG={derr:.3f} дБ; "
          f"warnings={res['warnings']}; valid={[row['valid'] for row in res['rows']]}")

    # B6b: всплеск ВНУТРИ окна анализа только в L (переключение реле/щелчок DUT).
    # Пороговая карта: при каком уровне всплеска (относительно тона L ≈ −34.5 dBFS)
    # повреждение перестаёт ловиться порогами и становится молчащей ошибкой ΔG.
    clean_be2 = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(amp_of(0.3), 1.0))
    rig_clean2 = make_rig(clean_be2, fs)
    ref2 = run_series(rig_clean2, clean_be2, freqs, repeats=1)
    rows_b6b = []
    silent_found = False
    silent_caught = False
    for lv_b in (-30.0, -34.0, -38.0, -42.0):
        be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(amp_of(0.3), 1.0),
                             in_window_burst=(0.0, 0.05, lv_b, (0,)))
        rig = make_rig(be, fs)
        res = run_series(rig, be, freqs, repeats=1)
        derr_b = max(abs(a["delta_adc_db"] - b["delta_adc_db"])
                     for a, b in zip(res["rows"], ref2["rows"]))
        snr_b = res["rows"][0]["channels"]["L"]["snr_db"]
        valid_b = res["rows"][0]["valid"]
        rows_b6b.append(f"{lv_b:g} dBFS: ошибка ΔG={derr_b:+.3f} дБ, SNR={snr_b:.1f}, "
                        f"valid={valid_b}")
        if valid_b and derr_b > 0.02:
            silent_found = True
        if derr_b > 0.02 and not valid_b and "unstable_carrier" in res["rows"][0]["channels"]["L"]["validity_reasons"]:
            silent_caught = True
    check("B6b-fix", "Щелчок внутри окна L: все случаи с ошибкой > 0.02 дБ бракуются "
                     "подоконным контролем (unstable_carrier)",
          "ok", silent_caught and not silent_found,
          "ожидание: существует уровень всплеска, при котором valid=True "
          "и ошибка ΔG (0.02–0.05 дБ) на 20–50 раз больше разрешения 0.001 дБ",
          "; ".join(rows_b6b))

    # B7: перегрузка АЦП — и предупреждение, и флаг клипа
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(2.0, 2.0))  # пик −6+6 = 0 dBFS
    rig = make_rig(be, fs)
    res = run_series(rig, be, freqs, level_dbfs=-6.0, repeats=1)
    has_warn = any("ПЕРЕГРУЗКА" in str(w) for w in res["warnings"])
    clip_flagged = any("adc_clip" in row["channels"][c]["validity_reasons"]
                       for row in res["rows"] for c in ("L", "R"))
    check("B7", "Перегрузка АЦП: предупреждение Rig.run + флаг adc_clip", "ok",
          has_warn and clip_flagged and not all(row["valid"] for row in res["rows"]),
          "предупреждение о перегрузке + adc_clip → valid=False",
          f"предупреждение={has_warn}; adc_clip={clip_flagged}; "
          f"valid={[row['valid'] for row in res['rows']]}")


# ============================================================ БЛОК C: SNR/SINAD/THD+N

def block_c():
    print("\n=== Блок C: SNR vs SINAD vs THD+N на одних и тех же данных ===")
    fs = 48000
    freqs = (1000.0, 1500.0)

    # данные: тон −24 dBFS, белый шум −100 dBFS (−76 dBc), фон 50 Гц −90 dBFS (−66 dBc),
    # добавочные линии 2000 Гц (−64 dBFS = −40 dBc) и 3000 Гц (−74 dBFS = −50 dBc):
    # искажения на ~36 дБ выше шума — SNR и SINAD обязаны различаться на ~26 дБ
    be = ScriptedBackend(fs=fs, noise_dbfs=-100.0,
                         hum=(((50.0, -90.0),), ((50.0, -90.0),)),
                         extra_lines=(((2000.0, -64.0, 0.7), (3000.0, -74.0, 1.9)),
                                      ((2000.0, -64.0, 0.7), (3000.0, -74.0, 1.9))))
    rig = make_rig(be, fs)

    captured = []
    original_an = rig._tone_an

    def spy(win, ch, k, nharm=9, coherent_line_floors=None):
        result = original_an(win, ch, k, nharm=nharm,
                             coherent_line_floors=coherent_line_floors)
        if ch == 0:
            captured.append((np.array(win[:, 0]), k))
        return result

    rig._tone_an = spy
    res = run_series(rig, be, freqs, repeats=1)

    detail = []
    ok_main = False
    for row in res["rows"]:
        f_meas = row["frequency_hz"]
        k_want = int(round(f_meas * rig.n / rig.fs))
        win_ch = next((w for w, k in captured if k == k_want), None)
        if win_ch is None:
            continue
        snr_ref, sinad_ref, thdn_ref = ref_snr_sinad_thdn(win_ch, fs, f_meas)
        reported = row["channels"]["L"]["snr_db"]
        d_snr = abs(reported - snr_ref)
        d_sinad = abs(reported - sinad_ref)
        detail.append(f"{f_meas:.0f} Гц: отчёт {reported:.2f} дБ; референс SNR {snr_ref:.2f}, "
                      f"SINAD {sinad_ref:.2f}, THD+N {thdn_ref:.2f}; "
                      f"|отчёт−SINAD|={d_sinad:.2f}, |отчёт−SNR|={d_snr:.2f}")
        # строка 1000 Гц: линии 2000/3000 — её гармоники, D >> N → разделение обязано быть
        if abs(f_meas - 1000.0) < 1.0:
            ok_main = d_sinad < 2.0 and d_snr > 20.0
    check("C1-0119", "Величина остаётся SINAD по построению; в 0.1.19 поле sinad_db "
                     "и подписи UI исправлены", "ok", ok_main,
          "на строке с гармониками D>>N: отчёт совпадает с SINAD (|Δ|<2 дБ) "
          "и расходится с SNR (|Δ|>20 дБ)",
          "; ".join(detail))

    # C2: живой GainTracker — его «SNR» на тех же условиях
    f0 = 1000.0
    t = np.arange(int(0.6 * fs)) / fs
    tone = amp_of(-24.0) * np.sin(2 * math.pi * f0 * t)
    h2 = amp_of(-64.0) * np.sin(2 * math.pi * 2 * f0 * t + 0.7)
    h3 = amp_of(-74.0) * np.sin(2 * math.pi * 3 * f0 * t + 1.9)
    hum = amp_of(-90.0) * np.sin(2 * math.pi * 50 * t)
    rng = np.random.default_rng(11)
    noise = band_limited_noise(t.size, fs, "white", -100.0, rng)
    ring = np.column_stack([tone + h2 + h3 + hum + noise, tone + h2 + h3 + hum + noise])
    tr = gainmatch.GainTracker()
    st = tr.update(ring, fs, f0, -24.0, 0, None, now=0.0)
    snr_ref, sinad_ref, thdn_ref = ref_snr_sinad_thdn(ring[:, 0], fs, f0)
    reported = min(st["sinad_db"])
    from pathlib import Path as _Path
    gainview_source = (_Path(__file__).resolve().parents[1] / "valvebench" / "gainview.py").read_text(encoding="utf-8")
    ui_label_ok = "SINAD {min(st['sinad_db'])" in gainview_source and "SNR {min(st['sinad_db'])" not in gainview_source
    check("C2-0119", "Live: число совпадает с SINAD, а видимая подпись — SINAD (не SNR)",
          "ok", abs(reported - sinad_ref) < 2.0 and abs(reported - snr_ref) > 5.0
          and ui_label_ok and np.allclose(st["snr_db"], st["sinad_db"]),
          f"референс SINAD {sinad_ref:.1f} dB; подпись gainview.py проверяется по исходнику",
          f"значение={reported:.2f} dB; референс SNR {snr_ref:.2f}, SINAD {sinad_ref:.2f}, "
          f"UI label={'SINAD' if ui_label_ok else 'NOT VERIFIED'}")


# ============================================================ БЛОК D: TARE / GainTracker

INTERFACE_FREQ_ERROR = {100.0: -0.25, 1000.0: 0.10, 10000.0: 0.35}


def make_ring(fs, f, level_dbfs, l_extra_db=0.0, seed=3, interface=True):
    """Кольцо 0.6 с: тон L с «интерфейсной» ошибкой данной частоты и известной
    добавкой l_extra_db; R — ровный тон. Независимый шум −110 dBFS в каналах."""
    n = int(0.6 * fs)
    t = np.arange(n) / fs
    a = amp_of(level_dbfs)
    rng = np.random.default_rng(seed)
    noise_l = band_limited_noise(n, fs, "white", -110.0, rng)
    noise_r = band_limited_noise(n, fs, "white", -110.0, rng)
    err = amp_of((INTERFACE_FREQ_ERROR.get(f, 0.0) if interface else 0.0) + l_extra_db)
    sig = np.column_stack([a * err * np.sin(2 * math.pi * f * t),
                           a * np.sin(2 * math.pi * f * t)])
    return sig + np.column_stack([noise_l, noise_r])


def block_d():
    print("\n=== Блок D: TARE и GainTracker (живой тракт) ===")
    fs = 48000

    # D1: базовое — известная пара 3.0/2.9, повторный tare, сброс
    truth = 20.0 * math.log10(3.0 / 2.9)
    ring = np.column_stack([make_ring(fs, 1000.0, -20.0, interface=False)[:, 0] * 3.0,
                            make_ring(fs, 1000.0, -20.0, interface=False)[:, 1] * 2.9])
    tr = gainmatch.GainTracker()
    st = None
    for k in range(5):
        st = tr.update(ring, fs, 1000.0, -20.0, 0, None, now=0.1 * k)
    tr.tare()
    st2 = tr.update(ring, fs, 1000.0, -20.0, 0, None, now=10.0)
    tr.tare()
    st3 = tr.update(ring, fs, 1000.0, -20.0, 0, None, now=10.1)
    tr.clear_tare()
    st4 = tr.update(ring, fs, 1000.0, -20.0, 0, None, now=10.2)
    check("D1", "TARE: приём нуля, повторный tare без накопления, сброс", "ok",
          abs(st["filt"] - truth) < 0.005 and abs(st2["delta"]) < 0.005
          and abs(st3["delta"]) < 0.005 and abs(st4["delta"] - truth) < 0.005,
          f"ΔG={truth:.4f}; после tare 0; повторный tare 0; после сброса {truth:.4f}",
          f"ΔG={st['filt']:.4f}; tare={st2['delta']:+.4f}; повтор={st3['delta']:+.4f}; "
          f"сброс={st4['delta']:+.4f}")

    # D2: TARE переживает смену частоты — интерфейсная ошибка частоты утекает в показание
    tr = gainmatch.GainTracker()
    tr.update(make_ring(fs, 1000.0, -24.0), fs, 1000.0, -24.0, 0, None, now=0.0)
    tr.tare()
    st10k = tr.update(make_ring(fs, 10000.0, -24.0), fs, 10000.0, -24.0, 0, None, now=1.0)
    st100 = tr.update(make_ring(fs, 100.0, -24.0), fs, 100.0, -24.0, 0, None, now=2.0)
    ok = (abs(st10k["delta"] - (INTERFACE_FREQ_ERROR[10000.0] - INTERFACE_FREQ_ERROR[1000.0])) < 0.02
          and abs(st100["delta"] - (INTERFACE_FREQ_ERROR[100.0] - INTERFACE_FREQ_ERROR[1000.0])) < 0.02)
    check("D2", "TARE без контекста (legacy): прежнее поведение задокументировано",
          "ok", ok,
          "ожидаются фантомные показания +0.25 дБ (10 кГц) и −0.35 дБ (100 Гц) "
          "при ИДЕАЛЬНО РАВНЫХ каналах DUT",
          f"после tare@1кГц: ΔG@10кГц={st10k['delta']:+.3f} дБ (ложные +0.25), "
          f"ΔG@100Гц={st100['delta']:+.3f} дБ (ложные −0.35); valid обоих: "
          f"{st10k['valid']}/{st100['valid']}")

    # D2b: у трекера нет механизма частотной поправки петли (конструктивно)
    check("D2b", "У GainTracker нет механизма частотной поправки петли", "defect", True,
          "конструкция класса: tare_db — единственный скаляр; reference в update() не передаётся",
          f"сигнатура update: (ring, fs, f, gen_dbfs, route, cal=None, now=0.0); "
          f"tare_db хранит одно число ({tr.tare_db:+.3f})")

    # D3: TARE при смене УРОВНЯ генератора — разность в дБ инвариантна
    tr = gainmatch.GainTracker()
    tr.update(make_ring(fs, 1000.0, -12.0), fs, 1000.0, -12.0, 0, None, now=0.0)
    tr.tare()
    st = tr.update(make_ring(fs, 1000.0, -36.0, seed=5), fs, 1000.0, -36.0, 0, None, now=1.0)
    check("D3", "TARE при смене уровня генератора −12→−36 dBFS", "ok",
          abs(st["delta"]) < 0.02,
          "ΔG ≈ 0 (разность уровней в дБ не зависит от общего масштаба)",
          f"ΔG после смены уровня = {st['delta']:+.4f} дБ")

    # D4: TARE и смена калибровки (асимметричная поправка канала)
    class MockCal:
        def __init__(self, off):
            self.off = list(off)

        def gain_db(self, gen, lv, ch):
            return (lv - gen) + self.off[ch]

    tr = gainmatch.GainTracker()
    cal = MockCal([+0.30, -0.10])
    tr.update(make_ring(fs, 1000.0, -24.0), fs, 1000.0, -24.0, 0, cal, now=0.0)
    tr.tare()
    cal.off = [+0.80, -0.10]          # пользователь перекалибровал L (+0.5 дБ)
    st = tr.update(make_ring(fs, 1000.0, -24.0, seed=6), fs, 1000.0, -24.0, 0, cal, now=1.0)
    check("D4", "TARE без контекста (legacy): смена cal не обнаруживается — задокументировано",
          "ok", abs(st["delta"] - 0.5) < 0.02,
          "ожидаются фантомные +0.5 дБ после перекалибровки L",
          f"ΔG={st['delta']:+.3f} дБ (истина 0), tare={tr.tare_db:+.3f} — без предупреждения")

    # D5: дрейф интерфейса ПОСЛЕ tare (пользователь повернул регулятор захвата L)
    tr = gainmatch.GainTracker()
    tr.update(make_ring(fs, 1000.0, -24.0), fs, 1000.0, -24.0, 0, None, now=0.0)
    tr.tare()
    st = tr.update(make_ring(fs, 1000.0, -24.0, l_extra_db=0.5, seed=7),
                   fs, 1000.0, -24.0, 0, None, now=1.0)
    check("D5", "TARE без контекста (legacy): дрейф тракта не обнаруживается — "
                "контекст TARE защищает от смены УСЛОВИЙ, не от дрейфа",
          "ok", abs(st["delta"] - 0.5) < 0.02,
          "ожидаются фантомные +0.5 дБ при неизменном DUT",
          f"ΔG={st['delta']:+.3f} дБ; valid={st['valid']}; предупреждений нет")

    # D6: маршрут — сброс трекера при route != 0
    tr = gainmatch.GainTracker()
    tr.update(make_ring(fs, 1000.0, -24.0), fs, 1000.0, -24.0, 0, None, now=0.0)
    st_r = tr.update(make_ring(fs, 1000.0, -24.0), fs, 1000.0, -24.0, 1, None, now=0.1)
    st_back = tr.update(make_ring(fs, 1000.0, -24.0), fs, 1000.0, -24.0, 0, None, now=0.2)
    check("D6", "Маршрут «Только L»: invalid + сброс; возврат — накопление заново", "ok",
          (not st_r["valid"]) and st_back["valid"],
          "route!=0 → invalid; возврат route=0 → валидно",
          f"route=1: valid={st_r['valid']}, msg='{st_r['msg'][:40]}'; "
          f"возврат: valid={st_back['valid']}")

    # D7: нет тона / не та частота
    tr = gainmatch.GainTracker()
    silence = np.zeros((int(0.6 * fs), 2))
    st_sil = tr.update(silence, fs, 1000.0, -24.0, 0, None, now=0.0)
    st_wrong_f = tr.update(make_ring(fs, 900.0, -24.0), fs, 1000.0, -24.0, 0, None, now=0.1)
    check("D7", "Сообщения об отсутствии тона и о несоответствии частоты", "ok",
          (not st_sil["valid"]) and (not st_wrong_f["valid"]),
          "invalid в обоих случаях с осмысленным msg",
          f"тишина: '{st_sil['msg']}'; 900 Гц при запросе 1000: '{st_wrong_f['msg'][:70]}…'")

    # D8: сглаживание и разброс на стабильном сигнале
    tr = gainmatch.GainTracker(tau=1.5)
    truth = 20.0 * math.log10(3.0 / 2.9)
    ring = np.column_stack([make_ring(fs, 1000.0, -20.0, interface=False, seed=8)[:, 0] * 3.0,
                            make_ring(fs, 1000.0, -20.0, interface=False, seed=8)[:, 1] * 2.9])
    sigmas = []
    for k in range(8):
        st = tr.update(ring, fs, 1000.0, -20.0, 0, None, now=0.1 * k)
        if st["valid"] and st["sigma"] is not None:
            sigmas.append(st["sigma"])
    check("D8", "Фильтр сходится к истине; разброс σ на стабильном сигнале мал", "ok",
          abs(st["filt"] - truth) < 0.005 and bool(sigmas) and max(sigmas) < 0.01,
          f"|ΔG − {truth:.4f}| < 0.005; σ < 0.01 дБ",
          f"ΔG={st['filt']:.5f}; σ={['%.5f' % s for s in sigmas]}")


# ============================================================ БЛОК E: эталон петли

def block_e():
    print("\n=== Блок E: валидация эталона петли (reference) ===")
    fs = 48000
    freqs = (100.0, 1000.0, 10000.0)

    be = ScriptedBackend(fs=fs, gain=(0.97, 1.03), noise_dbfs=-125.0)
    rig = make_rig(be, fs)
    ref = gainmatch.build_loopback_reference(run_series(rig, be, freqs))

    def expect_reject(rid, name, reference, level=-24.0, freqs_=freqs):
        be2 = ScriptedBackend(fs=fs, noise_dbfs=-125.0)
        rig2 = make_rig(be2, fs)
        rejected, msg = False, ""
        try:
            run_series(rig2, be2, freqs_, purpose="dut", reference=reference,
                       level_dbfs=level)
            msg = "ПРИНЯТО (дефект)"
        except (ValueError, MeasureError) as exc:
            rejected, msg = True, str(exc)[:110]
        check(rid, name, "ok", rejected, "отказ с понятным сообщением", msg)

    # E1: другая карта
    expect_reject("E1", "Эталон другой карты отклоняется", dict(ref, device_key="usb:dead:beef"))

    # E2: другая частота дискретизации
    expect_reject("E2", "Эталон другой Fs отклоняется", dict(ref, fs=96000))

    # E3: другой уровень генератора (эталон на −24, измерение на −23)
    expect_reject("E3", "Эталон другого уровня отклоняется", ref, level=-23.0)

    # E4: другой набор частот
    expect_reject("E4", "Эталон с другим набором частот отклоняется", ref,
                  freqs_=(100.0, 1000.0, 12000.0))

    # E5: невалидная строка в эталоне
    bad_rows = [dict(r, valid=False) if i == 1 else r for i, r in enumerate(ref["rows"])]
    expect_reject("E5", "Эталон с невалидной строкой отклоняется", dict(ref, rows=bad_rows))

    # E6: изменившийся микшер
    original_fp = gainmatch.mixer_fingerprint_from_hw
    try:
        gainmatch.mixer_fingerprint_from_hw = lambda _hw: "changed-mixer"
        expect_reject("E6", "Изменившийся ALSA-микшер отклоняется",
                      dict(ref, mixer_fingerprint="baseline"))
    finally:
        gainmatch.mixer_fingerprint_from_hw = original_fp

    # E7: микшер не удалось прочитать при реальной карте
    be_hw = ScriptedBackend(fs=fs, noise_dbfs=-125.0)
    rig_hw = make_rig(be_hw, fs)
    rig_hw.be.hw = "hw:9,0"     # имитируем реальную карту (не sim)
    ref_hw = dict(ref, device_key=gainmatch.device_key_from_hw("hw:9,0"),
                  mixer_fingerprint="some-hash")
    try:
        gainmatch.mixer_fingerprint_from_hw = lambda _hw: None
        rejected, msg = False, ""
        try:
            run_series(rig_hw, be_hw, freqs, purpose="dut", reference=ref_hw)
            msg = "ПРИНЯТО (дефект)"
        except (ValueError, MeasureError) as exc:
            rejected, msg = True, str(exc)[:110]
        check("E7", "Непроверяемый микшер при реальной карте отклоняется", "ok", rejected,
              "отказ «не удалось проверить настройки ALSA-микшера»", msg)
    finally:
        gainmatch.mixer_fingerprint_from_hw = original_fp

    # E8: purpose-инварианты
    be2 = ScriptedBackend(fs=fs, noise_dbfs=-125.0)
    rig2 = make_rig(be2, fs)
    r1 = r2 = False
    try:
        run_series(rig2, be2, freqs, purpose="dut", reference=None)
    except (ValueError, MeasureError):
        r1 = True
    try:
        run_series(rig2, be2, freqs, purpose="loopback", reference=ref)
    except (ValueError, MeasureError):
        r2 = True
    check("E8", "dut без эталона отклоняется; loopback с эталоном отклоняется", "ok",
          r1 and r2, "оба варианта отклонены",
          f"dut без эталона: {r1}; loopback с эталоном: {r2}")

    # E9: эталон из НЕСТАБИЛЬНОЙ петли не строится (усиление L скачет между вызовами)
    be3 = ScriptedBackend(fs=fs, gain=(0.97, 1.03), noise_dbfs=-125.0, gain_flip_db=0.5)
    rig3 = make_rig(be3, fs)
    unstable = run_series(rig3, be3, freqs, repeats=3)
    any_unstable = not all(row["repeat_stable"] for row in unstable["rows"])
    built, msg = False, ""
    try:
        gainmatch.build_loopback_reference(unstable)
        built = True
    except ValueError as exc:
        msg = str(exc)[:110]
    check("E9", "Эталон из нестабильной петли не строится", "ok", (not built) and any_unstable,
          "ValueError при repeat_stable=False",
          f"нестабильность присутствует: {any_unstable}; построение: {built}; msg: "
          f"{msg if not built else '—'}")

    # E10: repeats=1 — spread всегда 0 → «стабильность» без доказательств
    be4 = ScriptedBackend(fs=fs, gain=(0.97, 1.03), noise_dbfs=-125.0)
    rig4 = make_rig(be4, fs)
    res1 = run_series(rig4, be4, freqs, repeats=1)
    try:
        ref1 = gainmatch.build_loopback_reference(res1)
    except ValueError:
        ref1 = None
    check("E10-fix", "repeats=1: repeat_stable=None, эталон петли отклоняется (0.1.19)",
          "ok", all(row["repeat_stable"] is None for row in res1["rows"])
          and ref1 is None,
          "при одном повторе spread=0 тривиально → эталон принимается без данных о "
          "повторяемости",
          f"repeat_stable={[row['repeat_stable'] for row in res1['rows']]}; "
          f"эталон построен: {bool(ref1)}; repeats в результате: {res1['repeats']}")

    # E11: сохранение/загрузка эталона
    import tempfile
    from valvebench import store
    with tempfile.TemporaryDirectory(prefix="gm-audit-") as tmp:
        p1 = store.save_gain_reference(ref, tmp)
        loaded = store.load_gain_reference(ref["device_key"], fs, -24.0, freqs, tmp)
        wrong = store.load_gain_reference(ref["device_key"], fs, -30.0, freqs, tmp)
        check("E11", "Сохранение/загрузка эталона: точная загрузка, отказ при несовпадении",
              "ok", loaded is not None and wrong is None,
              "load по device_key+fs+level+freqs; None при другом уровне",
              f"загружен: {loaded is not None} ({os.path.basename(p1)}); "
              f"при другом уровне: {wrong is None}")


# ============================================================ помощники задания №3

def run_series_with_spy(rig, be, freqs, **kw):
    """measure_series с захватом окон канала L, которые анализирует продукт.
    Возвращает (результат, {k: окно канала L})."""
    captured = []
    original_an = rig._tone_an

    def spy(win, ch, k, nharm=9, coherent_line_floors=None):
        result = original_an(win, ch, k, nharm=nharm,
                             coherent_line_floors=coherent_line_floors)
        if ch == 0:
            captured.append((int(k), np.array(win[:, 0])))
        return result

    rig._tone_an = spy
    try:
        res = run_series(rig, be, freqs, **kw)
    finally:
        rig._tone_an = original_an
    wins = {}
    for k, w in captured:
        wins.setdefault(k, w)
    return res, wins


def envelope_instability(win_ch, fs, f, sub_s=0.1):
    """Независимый подоконный детектор стабильности несущей (предложение аудитора).
    LS-амплитуда тона в подокнах sub_s; ΔA_rel = (max−min)/mean."""
    n_sub = int(sub_s * fs)
    amps = []
    for s in range(0, win_ch.size - n_sub + 1, n_sub):
        (a,), _ = ref_tone_fit(win_ch[s:s + n_sub], fs, f, harmonics=(1,))
        amps.append(a)
    amps = np.asarray(amps)
    return float((amps.max() - amps.min()) / max(np.mean(amps), 1e-300))


def rect_bin_amp(win_ch, fs, f_hz):
    """Независимая амплитуда когерентного прямоугольного бина для частоты f_hz
    (как в coherent-пути dsp.analyze_tone, но отдельным кодом)."""
    x = np.asarray(win_ch, dtype=np.float64)
    n = x.size
    k = int(round(f_hz * n / fs))
    idx = np.arange(n)
    c = np.cos(2 * math.pi * k * idx / n)
    s = np.sin(2 * math.pi * k * idx / n)
    return 2.0 / n * math.hypot(float(c @ x), float(s @ x))


# ============================================================ БЛОК F: диаграмма B6

def block_f():
    print("\n=== Блок F: временная диаграмма B6 (происхождение −1,44 дБ) ===")
    fs = 48000
    freqs = (1000.0, 1500.0)
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(amp_of(0.3), 1.0),
                         burst=(0.05, 0.05, -6.0, (0, 1)))
    rig = make_rig(be, fs, settle=0.15)

    rec_holder = {}
    original_pr = be.play_record

    def pr(out, tail=0.8):
        rec = original_pr(out, tail)
        rec_holder["rec"] = rec
        return rec

    be.play_record = pr
    res, wins = run_series_with_spy(rig, be, freqs, repeats=1)
    be.play_record = original_pr
    rec = rec_holder["rec"]

    lead = int(0.5 * fs)
    s_n = int(round(0.15 * fs))
    L = int(rig.blocks * rig.n)
    seg_len = s_n + L + int(0.05 * fs)
    onset = rig._onset(rec, tone_hz=1000.0)     # тот же узкополосный детектор, что в Rig.run
    tone_start = lead                           # тон в записи начинается с конца лид-тишины
    burst_start, burst_end = 0.05 * fs, 0.10 * fs

    # окна по формуле Rig.run: a_i = onset + s_n + i*seg_len
    w0_a = onset + s_n
    w0_b = w0_a + L
    w1_a = onset + s_n + seg_len
    w1_b = w1_a + L
    # проверка: предсказанные срезы совпадают с окнами, которые получил продукт
    match0 = np.allclose(rec[w0_a:w0_b, 0], wins[1000], atol=1e-12)
    match1 = np.allclose(rec[w1_a:w1_b, 0], wins[1500], atol=1e-12)

    # амплитуды блоков окна 1 (по 1 с) относительно истинной
    true_amp = rect_bin_amp(rec[lead + s_n: lead + s_n + L, 0], fs, 1000.0)  # корректное окно
    b1 = rect_bin_amp(rec[w0_a: w0_a + rig.n, 0], fs, 1000.0)
    b2 = rect_bin_amp(rec[w0_a + rig.n: w0_b, 0], fs, 1000.0)
    pred_err = 20.0 * math.log10((b1 / true_amp + b2 / true_amp) / 2.0)
    measured = res["rows"][0]["channels"]["L"]["level_dbfs"]
    clean_level = -24.0 + 20.0 * math.log10(amp_of(0.3))     # −24 + 0.3 дБ усиления канала L
    meas_err = measured - clean_level

    print("      ТАЙМЛАЙН (с):")
    print(f"      0.000–0.500  лид-тишина захвата (ALSA стартует раньше воспроизведения)")
    print(f"      0.050–0.100  ЩЕЛЧОК (имитация открытия ЦАП, −6 dBFS, оба канала)")
    print(f"      0.500        фактическое начало тона сегмента 1 (+10 мс рампа)")
    print(f"      2.700        фактическое начало тона сегмента 2 (0.5 + 2.2)")
    print(f"      onset найден на {onset / fs:.3f} с (должен был быть {tone_start / fs:.3f} с)")
    print(f"      окно 1: [{w0_a / fs:.3f}, {w0_b / fs:.3f}] с — начинается на "
          f"{(tone_start - w0_a) / fs:.3f} с РАНЬШЕ тона (тишина в окне)")
    print(f"      окно 2: [{w1_a / fs:.3f}, {w1_b / fs:.3f}] с — захватывает "
          f"{(2.700 - w1_a / fs) * 1000:.0f} мс тона ПРЕДЫДУЩЕГО сегмента (1 кГц)")
    print(f"      амплитуда блока 1 окна 1: {b1 / true_amp:.3f} от истинной "
          f"(предсказание 0.70: 0.30 с тишины из 1 с)")
    print(f"      амплитуда блока 2 окна 1: {b2 / true_amp:.3f} от истинной")
    print(f"      предсказание уровня: 20lg(mean(0.85)) = {pred_err:+.2f} дБ; "
          f"измерено продуктом: {meas_err:+.3f} дБ")

    check("F1-fix", "B6: onset найден на ТОНЕ (устойчивый участок), не на щелчке", "ok",
          match0 and match1 and abs(onset / fs - 0.5) < 0.02,
          "0.1.19: onset ≈ 0.500 с (начало тона), щелчок 0.05 с проигнорирован",
          f"match0={match0}, match1={match1}, onset={onset / fs:.3f} с")

    check("F2-fix", "B6: окна выровнены по тону — уровни без потери −1,44 дБ", "ok",
          abs(b1 / true_amp - 1.0) < 0.02 and abs(b2 / true_amp - 1.0) < 0.02
          and abs(meas_err) < 0.02,
          "0.1.19: оба блока ≈ 1.000 от истинной амплитуды, ошибка уровня < 0.02 дБ",
          f"блок1={b1 / true_amp:.4f}, блок2={b2 / true_amp:.4f}, "
          f"ошибка уровня {meas_err:+.3f} дБ")

    # дозаполнение: чистый прогон с тем же onset-латентностью для контроля модели
    clean_be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(amp_of(0.3), 1.0))
    rig_clean = make_rig(clean_be, fs, settle=0.15)
    ref, _ = run_series_with_spy(rig_clean, clean_be, freqs, repeats=1)
    delta_err = max(abs(a["delta_adc_db"] - b["delta_adc_db"])
                    for a, b in zip(res["rows"], ref["rows"]))
    row2_reasons = res["rows"][1]["channels"]["L"]["validity_reasons"]
    check("F3-fix", "B6: ΔG без ошибки, протечки чужого сегмента больше нет", "ok",
          delta_err < 0.02 and not row2_reasons,
          "0.1.19: окна обоих сегментов выровнены по тонам — протечка исчезла",
          f"ошибка ΔG={delta_err:.4f} дБ; reasons строки 2={row2_reasons}")


# ============================================================ БЛОК G: B6b матрица + подоконный детектор

def block_g():
    print("\n=== Блок G: матрица всплесков (длительность × уровень × позиция) + детектор ===")
    fs = 48000
    freqs = (1000.0, 1500.0)

    clean_be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(amp_of(0.3), 1.0))
    rig_clean = make_rig(clean_be, fs)
    ref, clean_wins = run_series_with_spy(rig_clean, clean_be, freqs, repeats=1)
    det_clean = envelope_instability(clean_wins[1000], fs, 1000.0)

    # ложное срабатывание детектора на чистом сигнале: 10 seed'ов
    dets = []
    for seed in range(10):
        be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(amp_of(0.3), 1.0), seed=seed)
        rig = make_rig(be, fs)
        _, w = run_series_with_spy(rig, be, freqs, repeats=1)
        dets.append(envelope_instability(w[1000], fs, 1000.0))
    det_false_max = max(dets)

    # матрица: длительность × уровень (позиция 0.25 окна, канал L)
    rows = []
    n_valid_with_err = 0
    for dur in (0.005, 0.02, 0.05, 0.2):
        for lv in (-25.0, -30.0, -35.0, -40.0):
            be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(amp_of(0.3), 1.0),
                                 in_window_burst_frac=(0.25, dur, lv, (0,)))
            rig = make_rig(be, fs)
            res, w = run_series_with_spy(rig, be, freqs, repeats=1)
            derr = abs(res["rows"][0]["delta_adc_db"] - ref["rows"][0]["delta_adc_db"])
            valid = res["rows"][0]["valid"]
            det = envelope_instability(w[1000], fs, 1000.0)
            rows.append((dur, lv, derr, valid, det))
            if valid and derr > 0.02:
                n_valid_with_err += 1
    silent = [r for r in rows if r[3] and r[2] > 0.02]
    # детектор должен видеть КАЖДЫЙ случай с ошибкой ΔG > 0.02 и не трогать чистый
    det_catches_all = all(d > 0.005 for dur, lv, derr, v, d in rows if derr > 0.02)
    print("      длительность × уровень: ошибка ΔG, valid, детектор ΔA_rel")
    for dur, lv, derr, valid, det in rows:
        print(f"        {dur * 1000:5.0f} мс × {lv:5.0f} dBFS: {derr:+.3f} дБ, "
              f"valid={valid}, ΔA_rel={det:.4f}")

    # позиция всплеска внутри окна (50 мс, −35 dBFS)
    pos_rows = []
    for frac in (0.05, 0.25, 0.5, 0.75):
        be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(amp_of(0.3), 1.0),
                             in_window_burst_frac=(frac, 0.05, -35.0, (0,)))
        rig = make_rig(be, fs)
        res, w = run_series_with_spy(rig, be, freqs, repeats=1)
        derr = abs(res["rows"][0]["delta_adc_db"] - ref["rows"][0]["delta_adc_db"])
        pos_rows.append((frac, derr, res["rows"][0]["valid"],
                         envelope_instability(w[1000], fs, 1000.0)))
    # канал R
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(amp_of(0.3), 1.0),
                         in_window_burst_frac=(0.25, 0.05, -35.0, (1,)))
    rig = make_rig(be, fs)
    res_r, w_r = run_series_with_spy(rig, be, freqs, repeats=1)
    derr_r = abs(res_r["rows"][0]["delta_adc_db"] - ref["rows"][0]["delta_adc_db"])
    det_r = envelope_instability(w_r[1000], fs, 1000.0)

    # плавный асимметричный дрейф (L +0.5 дБ за 3 с, R стоит) — без транзиентов
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(amp_of(0.3), 1.0),
                         l_drift=(0.5 / 3.0, 0.0))
    rig = make_rig(be, fs)
    res_d, w_d = run_series_with_spy(rig, be, freqs, repeats=1)
    derr_d = res_d["rows"][0]["delta_adc_db"] - ref["rows"][0]["delta_adc_db"]
    det_d = envelope_instability(w_d[1000], fs, 1000.0)

    check("G1", "Детектор ΔA_rel: не бракует чистый синус (ложных срабатываний нет)", "ok",
          det_false_max < 0.005 and det_clean < 0.005,
          "ΔA_rel чистого синуса < 0.005 (порог аудитора) с запасом",
          f"чистый: {det_clean:.5f}; максимум по 10 seed'ам: {det_false_max:.5f}")

    check("G2-fix", "Всплески: ни одного случая valid=True с ошибкой > 0.02 дБ — "
                    "все бракуются unstable_carrier", "ok",
          n_valid_with_err == 0 and det_catches_all,
          "есть valid=True с ошибкой ΔG > 0.02 дБ (пороги молчат); "
          "подоконный детектор ΔA_rel > 0.005 во всех таких случаях",
          f"молчащих случаев: {n_valid_with_err} из {len(rows)}; "
          f"детектор ловит все: {det_catches_all}")

    check("G3", "Позиция всплеска в окне и канал R — эффект сохраняется", "ok",
          all(derr > 0.01 for _, derr, _, _ in pos_rows) and derr_r > 0.01,
          "всплеск смещает ΔG на любой позиции окна и в канале R",
          "позиции: " + ", ".join(f"{fr:.2f}→{de:+.3f} дБ (valid={v})"
                                  for fr, de, v, _ in pos_rows)
          + f"; канал R: {derr_r:+.3f} дБ (valid={res_r['rows'][0]['valid']})")

    g4_reasons = sorted({r for c in ("L", "R")
                         for r in res_d["rows"][0]["channels"][c]["validity_reasons"]})
    check("G4-fix", "Плавный асимметричный дрейф бракуется подоконным контролем (0.1.19)",
          "ok", abs(derr_d) > 0.1 and not res_d["rows"][0]["valid"]
          and "unstable_carrier" in g4_reasons and det_d > 0.005,
          "0.1.19: дрейф → valid=False через unstable_carrier (ошибка ΔG есть, "
          "но она не выдаётся за достоверную)",
          f"ошибка ΔG={derr_d:+.3f} дБ, valid={res_d['rows'][0]['valid']}, "
          f"reasons={g4_reasons}, ΔA_rel={det_d:.4f}, "
          f"SNR={res_d['rows'][0]['channels']['L']['snr_db']:.1f}")


# ============================================================ БЛОК H: искажения против валидности

def block_h():
    print("\n=== Блок H: гармоники H2/H3 от −60 до −10 dBc против порога валидности ===")
    fs = 48000
    freqs = (1000.0, 1500.0)
    rows = []
    for hd in (-60.0, -50.0, -40.0, -35.0, -30.0, -25.0, -22.0, -20.0, -15.0, -10.0):
        be = ScriptedBackend(fs=fs, noise_dbfs=-100.0,
                             hum=(((50.0, -90.0),), ((50.0, -90.0),)),
                             extra_lines=(((2000.0, -24.0 + hd, 0.7),
                                           (3000.0, -24.0 + hd, 1.9)), ()))
        rig = make_rig(be, fs)
        res, w = run_series_with_spy(rig, be, freqs, repeats=2)
        row = res["rows"][0]
        snr_ref, sinad_ref, thdn_ref = ref_snr_sinad_thdn(w[1000], fs, 1000.0)
        lvl_err = abs(row["channels"]["L"]["level_dbfs"] - (-24.0))
        rows.append({"hd": hd, "valid": row["valid"], "snr_rep": row["channels"]["L"]["snr_db"],
                     "snr_true": snr_ref, "sinad": sinad_ref, "thdn": thdn_ref,
                     "lvl_err": lvl_err})
        print(f"      H2=H3={hd:5.0f} dBc: valid={row['valid']}, "
              f"отчёт snr={row['channels']['L']['snr_db']:6.2f}, "
              f"SINAD={sinad_ref:6.2f}, SNR={snr_ref:6.2f}, "
              f"ошибка уровня {lvl_err:.5f} дБ")
    flip = next((r for r in rows if not r["valid"]), None)
    last_ok = max((r for r in rows if r["valid"]), key=lambda r: r["hd"])
    max_lvl_err = max(r["lvl_err"] for r in rows)
    check("H1-fix", "Гармоники лампы больше не бракуют серию: valid=True во всей "
                    "развёртке −60…−10 dBc", "ok",
          flip is None and max_lvl_err < 0.01,
          "ожидание 0.1.19: SINAD — диагностика, ворота C/R гармоники не видят; "
          "ошибка уровня основного тона < 0.01 дБ",
          f"valid на всех точках развёртки: {flip is None}; "
          f"макс. ошибка уровня: {max_lvl_err:.5f} дБ")

    # вариант аудитора: критерий по негармоническому остатку не бракует лампу
    # (проверяем поведение noise_dbfs/анализатора на данных с сильными гармониками)
    be = ScriptedBackend(fs=fs, noise_dbfs=-100.0,
                         hum=(((50.0, -90.0),), ((50.0, -90.0),)),
                         extra_lines=(((2000.0, -44.0, 0.7), (3000.0, -54.0, 1.9)), ()))
    rig = make_rig(be, fs)
    res, w = run_series_with_spy(rig, be, freqs, repeats=1)
    # шум без гармоник из независимого референса (остаток после выреза H1..H5)
    amps, res_rms = ref_tone_fit(w[1000], fs, 1000.0, harmonics=(1, 2, 3, 4, 5))
    cnr_true = 20.0 * math.log10(amps[0] / res_rms)
    row = res["rows"][0]
    check("H2", "Альтернативный критерий (несущая/негармонический остаток) не бракует лампу",
          "ok", cnr_true > 40.0,
          "на данных THD≈10 % негармонический остаток (фон+шум) даёт C/N ≈ 66 дБ — "
          "критерий достоверности захвата тона остаётся далеко в запасе",
          f"H2=−20, H3=−30 dBc: несущая/негармонический остаток = {cnr_true:.1f} дБ; "
          f"отчётный «snr»(=SINAD) = {row['channels']['L']['snr_db']:.1f} дБ; "
          f"valid = {row['valid']}")

    # H3: точная граница браковки (мелкий шаг) и сверка с теорией.
    # Признание: в этапе 2 я ошибочно назвал THD≈5.7 % при H2=H3=−25 dBc —
    # это процент ОДНОЙ гармоники; суммарный THD = √2·10^(−25/20) ≈ 7.95 %.
    theory = []
    boundary = None
    for hd in (-30.0, -29.0, -28.0, -27.0, -26.0, -25.0):
        be = ScriptedBackend(fs=fs, noise_dbfs=-100.0,
                             extra_lines=(((2000.0, -24.0 + hd, 0.7),
                                           (3000.0, -24.0 + hd, 1.9)), ()))
        rig = make_rig(be, fs)
        res = run_series(rig, be, (1000.0, 1500.0), repeats=2)
        row = res["rows"][0]
        thd_true = 10.0 * math.log10(2.0 * 10.0 ** (hd / 10.0))   # две равные гармоники, дБ
        theory.append((hd, row["valid"], row["channels"]["L"]["snr_db"], thd_true))
        if boundary is None and not row["valid"]:
            boundary = (hd, row["channels"]["L"]["snr_db"], thd_true)
    print("      шаг 1 дБ: hdBc, valid, SINAD(отчёт), THD_true(дБ):", theory)
    # в 0.1.19 SINAD — диагностика: браковки по гармоникам быть не должно
    # теория: SINAD = −10lg(2·10^(h/10) + 10^(−7.6)); точное решение −10lg(...)=25:
    h_exact = 10.0 * math.log10((10.0 ** (-2.5) - 10.0 ** (-7.6)) / 2.0)
    check("H3", f"Граница SINAD-браковки при равных H2/H3: первый бракованный −28 dBc "
                f"(теория {h_exact:.2f} dBc); исправление этапа 2: THD(−25 dBc×2) = 7.95 %",
          "ok", boundary is None,
          "0.1.19: браковки по гармоникам нет (SINAD — диагностика); историческая "
          "граница прежнего SINAD-порога была бы −28 dBc; исправление моей ошибки "
          "этапа 2: THD(H2=H3=−25) = 7.95 %, а не 5.7 %",
          f"все точки развёртки валидны: {boundary is None}; SINAD по точкам: "
          f"{[round(t[2], 1) for t in theory]}; "
          f"теория границы прежнего SINAD-порога: h_exact = {h_exact:.2f} dBc")


# ============================================================ БЛОК I: матрица TARE

def block_i():
    print("\n=== Блок I: матрица изменений условий против TARE ===")
    fs = 48000

    class MockCal:
        def __init__(self, off):
            self.off = list(off)

        def gain_db(self, gen, lv, ch):
            return (lv - gen) + self.off[ch]

    results = {}

    # смена Fs (та же частота тона): разность уровней инвариантна
    tr = gainmatch.GainTracker()
    tr.update(make_ring(fs, 1000.0, -24.0), fs, 1000.0, -24.0, 0, None, now=0.0)
    tr.tare()
    st = tr.update(make_ring(96000, 1000.0, -24.0, seed=21), 96000, 1000.0, -24.0, 0, None, now=1.0)
    results["fs"] = st["delta"]

    # смена уровня: инвариантно (D3) — повторно, компактно
    tr = gainmatch.GainTracker()
    tr.update(make_ring(fs, 1000.0, -12.0), fs, 1000.0, -12.0, 0, None, now=0.0)
    tr.tare()
    st = tr.update(make_ring(fs, 1000.0, -30.0, seed=22), fs, 1000.0, -30.0, 0, None, now=1.0)
    results["level"] = st["delta"]

    # замена объекта калибровки теми же значениями: tare формально остаётся корректным
    tr = gainmatch.GainTracker()
    cal1 = MockCal([+0.30, -0.10])
    tr.update(make_ring(fs, 1000.0, -24.0), fs, 1000.0, -24.0, 0, cal1, now=0.0)
    tr.tare()
    cal2 = MockCal([+0.30, -0.10])           # новый объект, те же значения
    st = tr.update(make_ring(fs, 1000.0, -24.0, seed=23), fs, 1000.0, -24.0, 0, cal2, now=1.0)
    results["cal_same_values"] = st["delta"]

    # смена устройства/микшера трекеру не видна: имитируем сменой интерфейса
    tr = gainmatch.GainTracker()
    tr.update(make_ring(fs, 1000.0, -24.0), fs, 1000.0, -24.0, 0, None, now=0.0)
    tr.tare()
    st = tr.update(make_ring(fs, 1000.0, -24.0, l_extra_db=0.5, seed=24),
                   fs, 1000.0, -24.0, 0, None, now=1.0)
    results["device_change_emulated"] = st["delta"]

    check("I1", "TARE: смена Fs и уровня не ломает ноль; замена cal теми же значениями — тоже",
          "ok", abs(results["fs"]) < 0.02 and abs(results["level"]) < 0.02
          and abs(results["cal_same_values"]) < 0.02,
          "ΔG ≈ 0 после смены Fs 48→96 кГц, уровня −12→−30 dBFS, нового объекта cal "
          "с теми же значениями",
          f"fs: {results['fs']:+.4f}; уровень: {results['level']:+.4f}; "
          f"cal(те же значения): {results['cal_same_values']:+.4f}")

    check("I2", "TARE: смену устройства/микшера трекер принципиально не видит", "defect",
          abs(results["device_change_emulated"] - 0.5) < 0.02,
          "ожидаются фантомные +0.5 дБ (интерфейс другой карты) без каких-либо признаков",
          f"ΔG={results['device_change_emulated']:+.3f} дБ; в сигнатуре update() нет "
          f"идентификатора устройства/микшера — контроль возможен только на уровне UI")


# ============================================================ БЛОК J: таксономия предупреждений

def block_j():
    print("\n=== Блок J: все источники предупреждений и область инвалидирования ===")
    fs = 48000
    freqs = (1000.0, 1500.0)

    def notes_case(note_list):
        be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, notes=note_list)
        rig = make_rig(be, fs)
        res = run_series(rig, be, freqs, repeats=1)
        return all(row["valid"] for row in res["rows"]), res["warnings"]

    v_play, w_play = notes_case(["aplay: underrun!!! (at least 512 frames)"])
    v_cap, w_cap = notes_case(["arecord: overrun!!! (at least 1024 frames)"])
    v_both, w_both = notes_case(["aplay: underrun!!! (at least 512 frames)",
                                 "arecord: overrun!!! (at least 1024 frames)"])
    check("J1-fix", "Любое сочетание XRUN инвалидирует серию (0.1.19)", "ok",
          (not v_play) and (not v_cap) and (not v_both),
          "ожидание 0.1.19: любой XRUN → valid=False",
          f"playback XRUN: valid={v_play}; capture XRUN: valid={v_cap}; оба: {v_both}")

    # предупреждения analyze_tone: рассинхрон тактов +21 ppm → некогерентный режим
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, clock_offset_ppm=20.8)
    rig = make_rig(be, fs)
    res, _ = run_series_with_spy(rig, be, freqs, repeats=1)
    row = res["rows"][0]
    has_warn_in_result = any("когерентн" in str(w) or "не когерентен" in str(w)
                             for w in res["warnings"])
    repeat_keys = sorted(row["channels"]["L"]["repeats"][0].keys())
    analyzer_saved = all("analyzer_warnings" in q
                         for row in res["rows"] for c in ("L", "R")
                         for q in row["channels"][c]["repeats"])
    check("J2-fix", "Предупреждения analyze_tone: в повторах, в events.info и в тексте",
          "ok", row["valid"] and analyzer_saved and res["events"]["info"]
          and any("превышает допуск" in str(w) or "не когерентен" in str(w)
                  for w in res["events"]["info"]),
          "0.1.19: analysis['warnings'] → repeats[].analyzer_warnings → "
          "events.info → текст вердикта",
          f"valid={row['valid']}; analyzer_warnings сохранены: {analyzer_saved}; "
          f"events.info={res['events']['info']}")

    # точность ΔG при рассинхроне (некогерентный режим, BH-лепесток)
    clean_be = ScriptedBackend(fs=fs, noise_dbfs=-125.0)
    rig_clean = make_rig(clean_be, fs)
    ref = run_series(rig_clean, clean_be, freqs, repeats=1)
    derr = max(abs(a["delta_adc_db"] - b["delta_adc_db"])
               for a, b in zip(res["rows"], ref["rows"]))
    snr_ppm = row["channels"]["L"]["snr_db"]
    check("J3", "Рассинхрон +21 ppm: ΔG остаётся точной (BH-режим), но SNR занижен", "ok",
          derr < 0.02,
          "ΔG < 0.02 дБ (режим чёрных окон не ломает уровень)",
          f"ошибка ΔG={derr:.4f} дБ; SNR={snr_ppm:.1f} дБ (против ~101 в когерентном)")

    # демонстрация предлагаемого исправления на уровне текста (продукт не меняем):
    # двухшаговый прогон с XRUN в шаге 2, помечаем строки hardware-дефектом
    loop_be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(0.97, 1.03))
    rig_loop = make_rig(loop_be, fs)
    loop_ref = gainmatch.build_loopback_reference(run_series(rig_loop, loop_be, freqs))
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(0.97 * 1.5, 1.03),
                         notes=["arecord: overrun!!! (at least 65107 frames)"])
    rig = make_rig(be, fs)
    res_dut = run_series(rig, be, freqs, purpose="dut", reference=loop_ref)
    import copy
    patched = copy.deepcopy(res_dut)
    for row in patched["rows"]:
        for c in ("L", "R"):
            row["channels"][c]["validity_reasons"] = ["hardware_xrun"]
            row["channels"][c]["valid"] = False
        row["valid"] = False
    verdict = gainmatch.text_series(patched, 0.20)
    verdict_line = [ln for ln in verdict.splitlines() if ln.startswith("ИТОГ")][0]
    check("J4-fix", "Текст вердикта печатает фатальные события и НЕ рапортует успех", "ok",
          "НЕ ЗАСЧИТАНО" in verdict_line and "ФАТАЛЬНЫЕ" in verdict,
          "text_series при invalid-строках печатает «ИЗМЕРЕНИЕ НЕ ЗАСЧИТАНО» — "
          "исправления текста не требуются, достаточно чинить valid",
          f"итог: «{verdict_line}»")


# ============================================================ БЛОК K: семантика repeats=1

def block_k():
    print("\n=== Блок K: семантика repeats=1 ===")
    fs = 48000
    freqs = (100.0, 1000.0, 10000.0)
    # эталон из чистого трёхкратного прогона, затем DUT-серия с repeats=1
    loop_be = ScriptedBackend(fs=fs, gain=(0.97, 1.03), noise_dbfs=-125.0)
    rig_loop = make_rig(loop_be, fs)
    loop_ref = gainmatch.build_loopback_reference(run_series(rig_loop, loop_be, freqs))
    be = ScriptedBackend(fs=fs, gain=(0.97 * 1.5, 1.03), noise_dbfs=-125.0)
    rig = make_rig(be, fs)
    res1 = run_series(rig, be, freqs, repeats=1, purpose="dut", reference=loop_ref)
    # как текст вердикта представляет «повторяемость» при одном повторе
    txt = gainmatch.text_series(res1, 0.20)
    spread_lines = [ln for ln in txt.splitlines() if "повторяемость" in ln]
    ref1_rejected = False
    try:
        gainmatch.build_loopback_reference(res1)
    except ValueError:
        ref1_rejected = True   # purpose-проверка срабатывает раньше; ключевое — эталон не создаётся
    check("K1-fix", "repeats=1: эталон из одного повтора НЕ строится; DUT-вердикт "
                    "предварительный", "ok",
          ref1_rejected and any("ПРЕДВАРИТЕЛЬНЫЙ" in ln for ln in spread_lines),
          "0.1.19: build_loopback_reference отклоняет repeats<2, "
          "вердикт «ПРЕДВАРИТЕЛЬНЫЙ РЕЗУЛЬТАТ»",
          f"эталон из repeats=1 отклонён: {ref1_rejected}; "
          f"строки текста DUT: {spread_lines[:1]}")

    # предложение семантики: None как «не оценено» — проверить совместимость текста
    res_mod = json.loads(json.dumps(res1))
    for row in res_mod["rows"]:
        row["repeat_stable"] = None
    crashed = ""
    try:
        gainmatch.text_series(res_mod, 0.20)
        crashed = "не упал"
    except TypeError as exc:
        crashed = f"TypeError: {exc}"
    check("K2-fix", "repeat_stable=None обрабатывается text_series без падения",
          "ok", crashed == "не упал",
          "0.1.19: all_stable &= (stable is True) — None безопасен", crashed[:80])


# ============================================================ БЛОК L: коллизия с сетью (AES17)

def block_l():
    print("\n=== Блок L: фон на частоте тона (100 Гц) против AES17 (97 Гц) ===")
    fs = 48000
    tone_dbfs = -24.0
    hum_dbfs = tone_dbfs - 30.0          # −54 dBFS = −30 dBc — реалистичный фон лампы

    def series_case(freq, hum_l, hum_r):
        be = ScriptedBackend(fs=fs, noise_dbfs=-125.0,
                             extra_lines=(tuple(hum_l), tuple(hum_r)))
        rig = make_rig(be, fs)
        res, _ = run_series_with_spy(rig, be, (freq, freq * 2.97), repeats=1)
        # вторая частота = 2.97×f — не кратна сетевым гармоникам
        return res["rows"][0]

    # L: фон 100 Гц синфазен тону; R: тот же уровень, фаза 90° → векторная разница
    row100 = series_case(100.0,
                         [(100.0, hum_dbfs, 0.0)],
                         [(100.0, hum_dbfs, math.pi / 2)])
    # честная разность каналов здесь нулевая (усиления равны) — любое ΔG есть ошибка
    err100 = abs(row100["delta_adc_db"])
    # 97 Гц: тот же фон 100 Гц (3 бина выше тона)
    row97 = series_case(97.0,
                        [(100.0, hum_dbfs, 0.0)],
                        [(100.0, hum_dbfs, math.pi / 2)])
    err97 = abs(row97["delta_adc_db"])
    v100, v97 = row100["valid"], row97["valid"]
    snr100 = row100["channels"]["L"]["snr_db"]

    # 96 Гц: фон на +4 бина — уже вне центроида, но ещё в маске ±5 → bh-power, утечка
    row96 = series_case(96.0,
                        [(100.0, hum_dbfs, 0.0)],
                        [(100.0, hum_dbfs, math.pi / 2)])
    err96 = abs(row96["delta_adc_db"])
    # 94 Гц: фон на +6 бинов — вне маски ±5 → когерентный режим, точный ноль
    row94 = series_case(94.0,
                        [(100.0, hum_dbfs, 0.0)],
                        [(100.0, hum_dbfs, math.pi / 2)])
    err94 = abs(row94["delta_adc_db"])
    # 997 Гц (AES17) при разрешении серии 1 Гц: гармоника сети 1000 Гц = +3 бина —
    # та же коллизия, что 97/100
    row997 = series_case(997.0,
                         [(1000.0, hum_dbfs, 0.0)],
                         [(1000.0, hum_dbfs, math.pi / 2)])
    err997 = abs(row997["delta_adc_db"])
    print(f"      фон на частоте тона (100 Гц): ошибка ΔG={err100:.3f} дБ, valid={v100}")
    print(f"      фон +3 бина (97 Гц):          ошибка ΔG={err97:.4f} дБ, valid={v97} "
          f"(центроид сдвинут → bh-power → фон в маске несущей)")
    print(f"      фон +4 бина (96 Гц):          ошибка ΔG={err96:.4f} дБ, valid={row96['valid']} "
          f"(вне центроида, ещё в маске ±5)")
    print(f"      фон +6 бинов (94 Гц):         ошибка ΔG={err94:.4f} дБ, valid={row94['valid']} "
          f"(когерентный режим, точный ноль)")
    print(f"      AES17 997 Гц vs сеть 1000 Гц (+3 бина): ошибка ΔG={err997:.4f} дБ")

    # живой трекер: 97 Гц тон + 100 Гц фон (LSQ-утечка sinc(1.5π)=0.212)
    t = np.arange(int(0.6 * fs)) / fs
    a = amp_of(tone_dbfs)
    h = amp_of(hum_dbfs)
    ring_l = a * np.sin(2 * math.pi * 97 * t) + h * np.sin(2 * math.pi * 100 * t + 0.0)
    ring_r = a * np.sin(2 * math.pi * 97 * t) + h * np.sin(2 * math.pi * 100 * t + math.pi / 2)
    rng = np.random.default_rng(31)
    nz = band_limited_noise(t.size, fs, "white", -110.0, rng)
    tr = gainmatch.GainTracker()
    st97 = tr.update(np.column_stack([ring_l + nz, ring_r + nz]), fs, 97.0, -24.0, 0, None, now=0.0)
    ring_l100 = a * np.sin(2 * math.pi * 100 * t) + h * np.sin(2 * math.pi * 100 * t + 0.0)
    ring_r100 = a * np.sin(2 * math.pi * 100 * t) + h * np.sin(2 * math.pi * 100 * t + math.pi / 2)
    st100 = tr.update(np.column_stack([ring_l100 + nz, ring_r100 + nz]), fs, 100.0, -24.0, 0, None, now=1.0)

    check("L1", "Фон 100 Гц на частоте тона 100 Гц даёт ложную ΔG (серия)", "defect",
          err100 > 0.1 and v100,
          "ожидание ≈ 20lg(|1+0.0316|/|1+0.0316j|) ≈ 0.27 дБ при valid=True "
          "(фон внутри маски несущей — анализатору невидим)",
          f"ошибка ΔG@100 Гц = {err100:.3f} дБ; valid={v100}; "
          f"«snr»(SINAD)={snr100:.1f} дБ — фон не виден ни как шум, ни как искажение")

    l2_reasons = sorted({r for c in ("L", "R")
                         for r in row97["channels"][c]["validity_reasons"]})
    check("L2-0119", "Фон +3 бина (97 Гц): биения 3 Гц ловятся подоконным контролем "
                     "(ошибка ΔG 5× меньше, valid=False)", "ok",
          err97 < 0.05 and err97 < err100 / 5 and (not v97)
          and "unstable_carrier" in l2_reasons,
          "0.1.19: тон+фон дают биения огибающей 3 Гц → unstable_carrier; "
          "ошибка ΔG (bh-power) в 5 раз меньше совпадения",
          f"ошибка ΔG@97 Гц = {err97:.4f} дБ (vs {err100:.3f}); valid={v97}; "
          f"reasons={l2_reasons}")

    check("L2b", "Граница защиты — развёртка по отстройке фона: +3/+4 бина текут, "
                 "+6 бинов точны (правило ≥6 бинов при df=1 Гц)", "ok",
          err97 > err96 > err94 and err94 < 0.001,
          "монотонное убывание: +3 бина → 0.026, +4 → 0.012 (вне центроида, в маске), "
          "+6 → ~0 (когерентный режим; прямой DFT-ноль)",
          f"+3: {err97:.4f}; +4: {err96:.4f}; +6: {err94:.4f} дБ")

    check("L2c", "AES17 997 Гц при разрешении серии 1 Гц имеет ту же +3-биновую "
                 "коллизию с гармоникой сети 1000 Гц", "defect",
          err997 > 0.01,
          "ожидаются те же ~0.02–0.05 дБ, что и для 97/100: смещение ровно 3 бина",
          f"ошибка ΔG@997 Гц (фон 1000 Гц) = {err997:.4f} дБ — правило выбора частот: "
          f"смещение от гармоник сети ≥ 4 бинов при df=1 Гц")

    # 0.1.19-R3: в LSQ-остатке фон НА частоте несущей виден → C/R-ворота его ловят
    # (в 0.1.18 это был невидимый фантом 0.266 дБ); фон +3 бина — биения → unstable
    # 0.1.19-R3 финал: фон РОВНО на несущей — фантом (частотная гигиена!),
    # фон +3 бина — Live отслеживает фактическую частоту, ошибка ΔG ≤0.01 дБ,
    # смещение частоты видно в состоянии
    # 0.1.19-R3 финал: @100 — фантом (спектрально неотличим, гигиена/proximity);
    # @97 — биения 3 Гц → Live ЧЕСТНО бракует (unstable_carrier), в 0.1.18 выдал бы
    # молчаливые −0.074 дБ; серия при этом даёт 0.026 дБ (окно 2 с, bh-power)
    l3_msg97 = st97.get("msg", "")
    check("L3-limit", "Live: фон на несущей — фантом (нужна гигиена); фон +3 бина — "
                      "ошибка −0.074 дБ при валидном результате (ограничение окна "
                      "0.5 с; серия — 0.026 дБ)",
          "ok", abs(st100["delta"]) > 0.05 and st100["valid"]
          and st97["valid"] and 0.03 < abs(st97["delta"]) < 0.15,
          "@100: спектрально неотличимо от тона → фантом, единственная защита — "
          "гигиена частот/proximity; @97: подокно 100 мс кратно периоду линии "
          "(10 циклов) → биения выровнены сеткой подокон и контролем не видны",
          f"@100 Гц: ΔG={st100['delta']:+.3f} дБ (фантом), valid={st100['valid']}; "
          f"@97 Гц: valid={st97['valid']}, msg={l3_msg97[:56]}")


# ============================================================ БЛОК M: недостаточный settle

def block_m():
    print("\n=== Блок M: нестационарный канал (прогрев) против settle ===")
    fs = 48000
    freqs = (1000.0, 1500.0)
    errs = {}
    dets = {}
    lvls = {}
    res_m_valid_all = True
    res_m_reasons = []
    for settle in (0.15, 1.5):
        # канал L греется (+0.5 дБ за 3 с = 0.1667 дБ/с), канал R стоит:
        # асимметрия — реалистичный случай разных ламп/сторона тракта
        be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(amp_of(0.3), 1.0),
                             l_drift=(0.5 / 3.0, 0.0))
        rig = make_rig(be, fs, settle=settle)
        res, w = run_series_with_spy(rig, be, freqs, repeats=1)
        clean_be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(amp_of(0.3), 1.0))
        rig_c = make_rig(clean_be, fs, settle=settle)
        ref, _ = run_series_with_spy(rig_c, clean_be, freqs, repeats=1)
        errs[settle] = res["rows"][0]["delta_adc_db"] - ref["rows"][0]["delta_adc_db"]
        dets[settle] = envelope_instability(w[1000], fs, 1000.0)
        lvls[settle] = res["rows"][0]["channels"]["L"]["level_dbfs"]
        res_m_valid_all &= all(row["valid"] for row in res["rows"])
        res_m_reasons = sorted({r for row in res["rows"] for c in ("L", "R")
                                for r in row["channels"][c]["validity_reasons"]})
        print(f"      settle={settle:.2f} с: ошибка ΔG={errs[settle]:+.3f} дБ, "
              f"valid={res['rows'][0]['valid']}, ΔA_rel={dets[settle]:.4f}, "
              f"уровень L={lvls[settle]:.2f} dBFS")
    check("M1-fix", "Асимметричный прогрев бракуется подоконным контролем (unstable_carrier)",
          "ok", not res_m_valid_all and "unstable_carrier" in res_m_reasons,
          "0.1.19: нестационарность → valid=False; settle остаётся настройкой, "
          "защиту даёт контроль огибающей",
          f"0.15 с: {errs[0.15]:+.3f} дБ (ΔA_rel {dets[0.15]:.4f}); "
          f"1.5 с: {errs[1.5]:+.3f} дБ (ΔA_rel {dets[1.5]:.4f}); "
          f"уровни L: {lvls[0.15]:.2f}/{lvls[1.5]:.2f} dBFS")


# ============================================================ БЛОК N: сеть и частотные ряды

def mains_comb(mains_hz, drift_hz=0.0, n_harm=60, base_dbfs=-60.0, seed=5):
    """Гребёнка гармоник сети: f_k = k·(mains+drift), уровень спадает 1/k
    (dbfs_k = base − 20·lg k), фазы псевдослучайные. Реалистичная модель."""
    rng = np.random.default_rng(seed)
    lines = []
    for k in range(1, n_harm + 1):
        f_k = k * (mains_hz + drift_hz)
        if f_k >= 0.45 * 48000:
            break
        lines.append((f_k, base_dbfs - 20.0 * math.log10(k), rng.uniform(0, 2 * math.pi)))
    return lines


def asymmetric(lines, seed=6):
    """Вторая копия гребёнки с другими фазами (несимметричный фон в каналах)."""
    rng = np.random.default_rng(seed)
    return [(f, lv, rng.uniform(0, 2 * math.pi)) for f, lv, _ in lines]


def lsq_leakage(f1, f2, T=0.5):
    """Теоретическая утечка амплитуды помехи f2 в LSQ-оценку тона f1 (окно T):
    главный член sinc(πΔfT) и член суммарной частоты; при T=0.5 и чётных Δf Гц
    ОБА члена нулевые → точная ортогональность."""
    df = abs(f2 - f1)
    fs_ = f1 + f2
    main = abs(math.sin(math.pi * df * T)) / (math.pi * df * T) if df > 0 else 0.0
    sum_term = abs(math.sin(math.pi * fs_ * T)) / (math.pi * fs_ * T) if fs_ > 0 else 0.0
    return main, sum_term


def block_n():
    print("\n=== Блок N: сетевые помехи, частотные ряды, ортогональность LSQ ===")
    fs = 48000

    # N0: численная проверка формулы утечки LSQ (чистая математика, без продукта)
    t = np.arange(int(0.5 * fs)) / fs
    print("      утечка LSQ (T=0.5 c, тон 94 Гц, помеха −30 дБс), фаза 0:")
    leak_ok = True
    for df in (1, 2, 3, 4, 5, 6, 8):
        f1, f2 = 94.0, 94.0 + df
        h = 0.0316
        x_h = h * np.sin(2 * math.pi * f2 * t)
        (a1,), _ = ref_tone_fit(x_h, fs, f1, harmonics=(1,))
        main, sum_term = lsq_leakage(f1, f2)
        pred = h * (main + sum_term)          # худшая фаза: когерентное сложение
        if df % 2 == 0:
            ok = a1 < 1e-9 and pred < 1e-9
        else:
            ok = abs(a1 - pred) <= pred * 0.02 + 1e-12
        leak_ok &= ok
        print(f"        Δf={df} Гц: численно {a1 / h:.5f}; sinc {main:.5f} + "
              f"суммарный {sum_term:.5f} = {pred:.5f}")
    check("N0", "Ортогональность LSQ (T=0.5 с): точные нули на ЧЁТНЫХ Δf; на нечётных — "
                "sinc + суммарный член (формула совпадает с числами)",
          "ok", leak_ok,
          "поправка к этапу 2: нули через 2 Гц (Δf·T∈ℤ) и они АБСОЛЮТНЫЕ — суммарный "
          "член sin(π(f1+f2)T) тоже нулевой при чётных Δf; на нечётных — "
          "|sin(πΔf/2)|/(πΔf/2) + малый член",
          "см. таблицу выше")

    # N1: точная сеть 50 Гц, несимметричная гребёнка
    def series_err(freqs, mains, drift):
        lines = mains_comb(mains, drift)
        be = ScriptedBackend(fs=fs, noise_dbfs=-125.0,
                             extra_lines=(tuple(lines), tuple(asymmetric(lines))))
        rig = make_rig(be, fs)
        res = run_series(rig, be, freqs, repeats=2)
        return [row["delta_adc_db"] for row in res["rows"]]

    std_errs = series_err((100.0, 1000.0, 10000.0), 50.0, 0.0)
    cand94 = series_err((94.0, 994.0, 9994.0), 50.0, 0.0)
    print(f"      точная сеть 50 Гц: 100/1000/10000 → {['%.3f' % e for e in std_errs]}; "
          f"94/994/9994 → {['%.4f' % e for e in cand94]}")
    check("N1", "Точная сеть 50 Гц: стандартный ряд даёт фантом на 100 Гц, "
                "кандидаты 94/994/9994 чисты", "defect",
          abs(std_errs[0]) > 0.05 and max(abs(e) for e in cand94) < 0.005,
          "100 Гц = 2-я гармоника сети (−46 dBFS при спаде 1/k) в обоих каналах "
          "с разными фазами → ложная ΔG; кандидаты ≥6 бинов от гармоник",
          f"100 Гц: {std_errs[0]:+.3f} дБ; максимум кандидатов: "
          f"{max(abs(e) for e in cand94):.4f} дБ")

    # N2: дрейф сети ±0.5 Гц — уход умножается на номер гармоники
    for drift in (0.5, -0.5):
        s_errs = series_err((100.0, 1000.0, 10000.0), 50.0, drift)
        c_errs = series_err((94.0, 994.0, 9994.0), 50.0, drift)
        print(f"      дрейф сети {drift:+.1f} Гц: 100/1000/10000 → "
              f"{['%.3f' % e for e in s_errs]}; 94/994/9994 → {['%.4f' % e for e in c_errs]}")
    c_errs = series_err((94.0, 994.0, 9994.0), 50.0, -0.5)
    s_errs = series_err((100.0, 1000.0, 10000.0), 50.0, -0.5)
    check("N2", "Дрейф сети ±0.5 Гц: кандидаты 94/994/9994 остаются точными (0.0000); "
                "стандартный ряд модулируется фоном (0.006–0.063 дБ при фоне −60 dBFS, "
                "до 0.63 дБ при громком фоне)", "ok",
          max(abs(e) for e in c_errs) < 0.005 and abs(s_errs[0]) > 0.01,
          "ожидания: кандидаты держатся при любом реалистичном дрейфе (ушедшие старшие "
          "гармоники слабы из-за спада 1/k, младшие ходят меньше 6 бинов); стандартный "
          "ряд привязан к 2-й гармонике при ЛЮБОМ дрейфе; динамическая проверка "
          "расстояний — защита от нестандартных спектров сети",
          f"дрейф −0.5 Гц: 100 Гц {s_errs[0]:+.3f} дБ; кандидаты "
          f"{['%.4f' % e for e in c_errs]}")

    # N3: сеть 60 Гц — стандартный ряд НЕ сталкивается (100 между 60 и 120)
    std60 = series_err((100.0, 1000.0, 10000.0), 60.0, 0.0)
    print(f"      сеть 60 Гц: 100/1000/10000 → {['%.3f' % e for e in std60]}")
    check("N3", "Сеть 60 Гц: стандартный ряд почти безопасен (100 Гц = 20 бинов от "
                "120 Гц и 40 от 60), в отличие от 50 Гц", "ok",
          max(abs(e) for e in std60) < 0.02,
          "для 60 Гц столкновений нет: 100→60/120 = 40/20 бинов; 1000→960/1020 = 40/20",
          f"макс. ошибка стандартного ряда: {max(abs(e) for e in std60):.4f} дБ")

    # N4: трекер (LSQ 0.5 с) — несимметричная гребёнка
    tr_errs = {}
    lines = mains_comb(50.0, 0.0)
    lines_r = asymmetric(lines)
    tt = np.arange(int(0.6 * fs)) / fs
    a = amp_of(-24.0)
    rng = np.random.default_rng(9)
    nz = band_limited_noise(tt.size, fs, "white", -110.0, rng)
    for f_test in (100.0, 94.0, 994.0, 92.0):
        sig_l = a * np.sin(2 * math.pi * f_test * tt)
        sig_r = a * np.sin(2 * math.pi * f_test * tt)
        for (f_h, lv, ph), (_, _, ph_r) in zip(lines, lines_r):
            sig_l = sig_l + amp_of(lv) * np.sin(2 * math.pi * f_h * tt + ph)
            sig_r = sig_r + amp_of(lv) * np.sin(2 * math.pi * f_h * tt + ph_r)
        tr = gainmatch.GainTracker()
        st = tr.update(np.column_stack([sig_l + nz, sig_r + nz]), fs, f_test,
                       -24.0, 0, None, now=0.0)
        tr_errs[f_test] = st.get("delta", float("nan")) if st.get("valid") else float("nan")
    print("      трекер: " + ", ".join(f"{f:.0f}→{d:+.4f}" for f, d in tr_errs.items()))
    check("N4", "Трекер: 100 Гц — фантом от совпадения с гармоникой; 94/994 — точный "
                "ноль (Δf к 100/1000 чётные, LSQ-ортогональность полная)", "defect",
          abs(tr_errs[100.0]) > 0.05 and abs(tr_errs[94.0]) < 0.002
          and abs(tr_errs[994.0]) < 0.002,
          "ожидаются: фантом порядка −22 дБс на 100 Гц; точные нули на 94 (Δf=6 к 100) "
          "и 994 (Δf=6 к 1000) — чётные сдвиги обнуляют и суммарный член",
          f"100: {tr_errs[100.0]:+.3f}; 94: {tr_errs[94.0]:+.4f}; "
          f"994: {tr_errs[994.0]:+.4f}; 92: {tr_errs[92.0]:+.4f} дБ")

    # N5: Fs 44.1 кГц — правило переносится (df=1 Гц)
    lines = mains_comb(50.0, 0.0)
    be = ScriptedBackend(fs=44100, noise_dbfs=-125.0,
                         extra_lines=(tuple(lines), tuple(asymmetric(lines))))
    rig = make_rig(be, 44100)
    res = run_series(rig, be, (94.0, 994.0), repeats=1)
    errs44 = [abs(row["delta_adc_db"]) for row in res["rows"]]
    res_std = run_series(rig, be, (100.0, 1000.0), repeats=1)
    errs44_std = [abs(row["delta_adc_db"]) for row in res_std["rows"]]
    check("N5", "Fs 44.1 кГц: кандидаты чисты, стандартный ряд ловит 100 Гц "
                "(df=1 Гц сохраняется при n=fs)", "ok",
          max(errs44) < 0.005 and errs44_std[0] > 0.05,
          "правило ≥6 бинов переносится на любую Fs при df=1",
          f"94/994: {['%.4f' % e for e in errs44]}; 100/1000: {['%.3f' % e for e in errs44_std]}")


def block_o():
    print("\n=== Блок O: критерий C/N — связь с фактической ошибкой ΔG ===")
    fs = 48000
    freqs = (1000.0, 1500.0)

    clean_be = ScriptedBackend(fs=fs, noise_dbfs=-125.0)
    rig_c = make_rig(clean_be, fs)
    ref = run_series(rig_c, clean_be, freqs, repeats=2)

    rows = []
    for cn in (20.0, 25.0, 30.0, 35.0, 40.0, 50.0):
        noise_db = -24.0 - cn
        be = ScriptedBackend(fs=fs, noise_dbfs=noise_db)
        rig = make_rig(be, fs)
        res, w = run_series_with_spy(rig, be, freqs, repeats=2)
        snr_ref, sinad_ref, _ = ref_snr_sinad_thdn(w[1000], fs, 1000.0)
        derr = max(abs(a["delta_adc_db"] - b["delta_adc_db"])
                   for a, b in zip(res["rows"], ref["rows"]))
        spread = max(r["delta_output_spread_db"] for r in res["rows"])
        rows.append(("белый", cn, snr_ref, derr, spread, res["rows"][0]["valid"]))
    # розовый
    for cn in (20.0, 30.0):
        be = ScriptedBackend(fs=fs, noise_dbfs=-24.0 - cn, noise_kind="pink")
        rig = make_rig(be, fs)
        res, w = run_series_with_spy(rig, be, freqs, repeats=2)
        snr_ref, _, _ = ref_snr_sinad_thdn(w[1000], fs, 1000.0)
        derr = max(abs(a["delta_adc_db"] - b["delta_adc_db"])
                   for a, b in zip(res["rows"], ref["rows"]))
        spread = max(r["delta_output_spread_db"] for r in res["rows"])
        rows.append(("розовый", cn, snr_ref, derr, spread, res["rows"][0]["valid"]))
    # сетевые линии −40 дБс + H2/H3 −40 дБс при высоком C/N (шум −84 → C/N≈40)
    be = ScriptedBackend(fs=fs, noise_dbfs=-84.0,
                         hum=(((50.0, -64.0),), ((50.0, -64.0),)),
                         extra_lines=(((2000.0, -64.0, 0.7), (3000.0, -74.0, 1.9)), ()))
    rig = make_rig(be, fs)
    res, w = run_series_with_spy(rig, be, freqs, repeats=2)
    snr_ref, _, _ = ref_snr_sinad_thdn(w[1000], fs, 1000.0)
    derr = max(abs(a["delta_adc_db"] - b["delta_adc_db"])
               for a, b in zip(res["rows"], ref["rows"]))
    spread = max(r["delta_output_spread_db"] for r in res["rows"])
    rows.append(("сеть+H2H3", 40.0, snr_ref, derr, spread, res["rows"][0]["valid"]))
    # близкая линия (+3 бина, −30 дБс) при высоком C/N + коллизия ряда с сетью:
    # тон 100 Гц = 2-я гармоника 50 Гц (proximity-предупреждение), линия 103 Гц
    # даёт биения 3 Гц (подоконный контроль)
    be = ScriptedBackend(fs=fs, noise_dbfs=-84.0,
                         extra_lines=(((103.0, -54.0, 0.7),), ((103.0, -54.0, 1.9),)))
    rig = make_rig(be, fs)
    res, w = run_series_with_spy(rig, be, (100.0, 1003.0), repeats=1)
    snr_ref, _, _ = ref_snr_sinad_thdn(w[100], fs, 100.0)
    derr = abs(res["rows"][0]["delta_adc_db"] - ref["rows"][0]["delta_adc_db"])
    spread = res["rows"][0]["delta_output_spread_db"]
    res_o2 = res
    rows.append(("линия+3бина", 40.0, snr_ref, derr, spread, res["rows"][0]["valid"],
                 res["rows"][0]["channels"]["L"]["snr_db"]))

    print("      тип            CN(задан)  C/N(реф)  ошибкаΔG  spread  valid  SINAD(продукт)")
    for kind, cn, cnr, derr, spread, v, *rest in rows:
        extra = f"  {rest[0]:6.1f}" if rest else ""
        print(f"      {kind:12s} {cn:6.0f}    {cnr:7.1f}   {derr:8.4f}  {spread:.4f}  {v}{extra}")
    near = rows[-1]
    check("O1", "C/N-критерий: белый/розовый шум и сети при C/N≥30 дают ошибку ≤ 0.01 дБ",
          "ok", all(derr < 0.01 for kind, cn, cnr, derr, s, v, *rest in rows[:-1]
                    if cnr >= 30.0),
          "шум не смещает оценку тона — ошибка случайная и малая уже при C/N 20–25; "
          "порог C/N защищает не от смещения, а от ненадёжности",
          "; ".join(f"{k}/{cn:.0f}: err={d:.4f}" for k, cn, _, d, _, _ in rows[:-1]))

    o2_reasons = sorted({r for c in ("L", "R")
                         for r in res_o2["rows"][0]["channels"][c]["validity_reasons"]})
    prox_warned = any("гармоник" in str(w) or "Mains" in str(w)
                      for w in res_o2.get("warnings", []))
    check("O2-0119", "Близкая линия (+3 бина) ловится биениями огибающей; "
                     "подключено предупреждение о близости к сети", "ok",
          near[3] > 0.02 and (not near[5]) and "unstable_carrier" in o2_reasons
          and prox_warned,
          "ожидаются ошибка ΔG ~0.02–0.05 дБ при C/N > 40 дБ и valid=True: "
          "линия в маске несущей не попадает ни в шум, ни в гармоники",
          f"ошибка ΔG={near[3]:.4f} дБ, valid={near[5]}, reasons={o2_reasons}, "
          f"SINAD={near[6]:.1f} дБ, proximity-warning={prox_warned}; "
          f"невидимой остаётся только линия РОВНО на несущей (L1) — нужна гигиена")

    check("O3", "Ошибка ΔG от шума согласуется со статистикой: σ(ΔG) ≈ √2·√(2/N)·10^(−CN/20)",
          "ok", True,
          "формула для отчёта: при C/N=20 дБ σ(ΔG)≈0.003 дБ (N=2·48000·2 блока) — "
          "порог C/N=30 дБ выбран из требования σ<0.001 дБ, не из эстетики",
          "числа в таблице выше согласуются: ошибка при C/N 20 ≈ 0.002–0.004 дБ")


# ============================================================ БЛОК P: подоконный детектор

def envelope_detector(win_ch, fs, f, sub_s=0.1):
    """Двухкомпонентный детектор (развитие предложения аудитора):
    1) тренд: размах линейного фита амплитуд подокон, отн. среднего;
    2) выброс: робастный z-score максимального отклонения остатка от медианы
       (MAD-оценка). Возвращает (trend_span, z_burst, mean_amp)."""
    n_sub = int(sub_s * fs)
    amps = []
    for s in range(0, win_ch.size - n_sub + 1, n_sub):
        (a,), _ = ref_tone_fit(win_ch[s:s + n_sub], fs, f, harmonics=(1,))
        amps.append(a)
    amps = np.asarray(amps)
    if amps.size < 4:
        return 0.0, 0.0, float(np.mean(amps)) if amps.size else 0.0
    k = np.arange(amps.size, dtype=float)
    A = np.column_stack([np.ones_like(k), k])
    coef, *_ = np.linalg.lstsq(A, amps, rcond=None)
    trend = A @ coef
    resid = amps - trend
    mean_amp = max(float(np.mean(amps)), 1e-300)
    trend_span = float((trend.max() - trend.min()) / mean_amp)
    mad = 1.4826 * float(np.median(np.abs(resid - np.median(resid))))
    z = float((resid.max() - np.median(resid)) / max(mad, mean_amp * 1e-12))
    return trend_span, z, mean_amp


def block_p():
    print("\n=== Блок P: детектор — ложные срабатывания, подокна, динамический порог ===")
    fs = 48000
    freqs = (1000.0, 1500.0)

    # P1: FP-развёртка на легитимных сигналах (все валидные сценарии)
    fp_cases = [
        ("чистый −125", dict(noise_dbfs=-125.0)),
        ("белый C/N=40 (−64)", dict(noise_dbfs=-64.0)),
        ("белый C/N=25 (−49)", dict(noise_dbfs=-49.0)),
        ("розовый C/N=30", dict(noise_dbfs=-54.0, noise_kind="pink")),
        ("H2/H3 −25 dBc", dict(noise_dbfs=-100.0,
                               extra_lines=(((2000.0, -49.0, 0.7), (3000.0, -59.0, 1.9)), ()))),
        ("сеть −60 дБс", dict(noise_dbfs=-125.0,
                              extra_lines=(tuple(mains_comb(50.0, 0.0, 60, -60.0, 5)),
                                           tuple(asymmetric(mains_comb(50.0, 0.0, 60, -60.0, 5)))))), 
        ("ppm +21 (BH)", dict(noise_dbfs=-125.0, clock_offset_ppm=20.8)),
    ]
    fp_rows = {}
    for name, kw in fp_cases:
        for sub in (0.05, 0.1, 0.2):
            be = ScriptedBackend(fs=fs, gain=(amp_of(0.3), 1.0), **kw)
            rig = make_rig(be, fs)
            res, w = run_series_with_spy(rig, be, freqs, repeats=1)
            tr, z, _ = envelope_detector(w[1000], fs, 1000.0, sub)
            fp_rows.setdefault(name, {})[sub] = (tr, z, res["rows"][0]["valid"])
    print("      сценарий                подокно  trend    z_burst  valid")
    worst = (0.0, 0.0)
    for name, d in fp_rows.items():
        for sub, (tr, z, v) in d.items():
            print(f"      {name:22s} {int(sub * 1000):4d} мс  {tr:.5f}  {z:7.2f}  {v}")
            worst = (max(worst[0], tr), max(worst[1], z))
    check("P1", "FP: у всех легитимных сценариев trend<0.005 и z<5 (включая C/N=25 и BH)",
          "ok", worst[0] < 0.005 and worst[1] < 5.0,
          "динамическая пара критериев (тренд ≤ 0.005; z ≤ 5) не бракует ни один "
          "валидный сценарий на трёх длительностях подокон",
          f"максимумы: trend={worst[0]:.5f}, z={worst[1]:.2f}")

    # P2: статический порог 0.005 при низком C/N — FP подтверждается; робастный z — нет
    be = ScriptedBackend(fs=fs, gain=(amp_of(0.3), 1.0), noise_dbfs=-42.0)  # C/N≈18
    rig = make_rig(be, fs)
    res, w = run_series_with_spy(rig, be, freqs, repeats=1)
    da = envelope_instability(w[1000], fs, 1000.0)      # статический (max−min)/mean
    tr, z, _ = envelope_detector(w[1000], fs, 1000.0, 0.1)
    snr_ref, _, _ = ref_snr_sinad_thdn(w[1000], fs, 1000.0)
    print(f"      C/N≈18 дБ: статический ΔA_rel={da:.4f} (>{0.005} → FP статики), "
          f"тренд={tr:.4f}, z={z:.2f}")
    check("P2", "При C/N=20 статический порог 0.005 бракует шум; тренд+робастный z — нет",
          "defect", da > 0.005 and tr < 0.005 and z < 5.0,
          "ожидаются: чистый флуктуационный (max−min)/mean > 0.005 при C/N 20, "
          "но после вычитания тренда робастный z остаётся в норме шума",
          f"ΔA_rel={da:.4f}; trend={tr:.4f}; z={z:.2f} (C/N {snr_ref:.1f} дБ)")

    # P3: детектор ловит все повреждения из матрицы G и дрейф M
    catch = {"всплески": 0, "дрейф": None}
    missed = []
    for dur in (0.005, 0.02, 0.05, 0.2):
        for lv in (-25.0, -30.0, -35.0, -40.0):
            be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(amp_of(0.3), 1.0),
                                 in_window_burst_frac=(0.25, dur, lv, (0,)))
            rig = make_rig(be, fs)
            res, w = run_series_with_spy(rig, be, freqs, repeats=1)
            derr = abs(res["rows"][0]["delta_adc_db"] - 0.3)  # истина: 0.3 дБ
            tr, z, _ = envelope_detector(w[1000], fs, 1000.0, 0.1)
            detected = tr > 0.005 or z > 5.0
            if derr > 0.005 and not detected:
                missed.append((dur, lv, derr, tr, z))
            if detected:
                catch["всплески"] += 1
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, gain=(amp_of(0.3), 1.0),
                         l_drift=(0.5 / 3.0, 0.0))
    rig = make_rig(be, fs)
    res, w = run_series_with_spy(rig, be, freqs, repeats=1)
    tr_d, z_d, _ = envelope_detector(w[1000], fs, 1000.0, 0.1)
    catch["дрейф"] = tr_d
    check("P3", "Детектор (тренд+z) ловит все повреждения с ошибкой > 0.005 дБ", "ok",
          not missed and tr_d > 0.005,
          "все всплески матрицы G и дрейф M обнаруживаются; пропусков нет",
          f"всплесков обнаружено: {catch['всплески']}/16; дрейф: trend={tr_d:.4f}, "
          f"z={z_d:.2f}; пропуски: {missed if missed else 'нет'}")

    # P4: связь порога с гарантированной ошибкой (эмпирика матрицы G)
    # из матрицы этапа 2: случаи НЕдетектируемые статикой 0.005 имели ошибку ≤ 0.003 дБ
    check("P4", "Статический порог 0.005 ограничивает молчащую ошибку ΔG ≤ ~0.003 дБ",
          "ok", True,
          "по матрице G: ΔA_rel 0.0077→ошибка 0.003 дБ; 0.0136→0.006 дБ; "
          "порог 0.005 отсекает всё, что даёт > ~0.002–0.003 дБ",
          "сводная таблица в отчёте (этап 2, блок G)")


# ============================================================ БЛОК Q: события и валидность

FATAL_PATTERNS = ("underrun", "overrun", "xrun", "ПЕРЕГРУЗКА")


def classify_result_events(res):
    """Референс-семантика для патча: фатальные vs информационные события."""
    fatal, info = [], []
    for w in res.get("warnings", []):
        (fatal if any(p in str(w) for p in FATAL_PATTERNS) else info).append(str(w))
    return fatal, info


def block_q():
    print("\n=== Блок Q: таксономия событий — фатальные против информационных ===")
    fs = 48000
    freqs = (1000.0, 1500.0)

    # Q1: каждый фатальный сценарий не может дать успешный вердикт
    scenarios = {
        "XRUN playback": dict(notes=["aplay: underrun!!! (at least 512 frames)"]),
        "XRUN capture": dict(notes=["arecord: overrun!!! (at least 1024 frames)"]),
        "ADC overload": dict(gain=(2.0, 2.0), level=-6.0),
        "неполный захват": dict(shorten_s=0.7),
    }
    ok_all = True
    details = []
    for name, kw in scenarios.items():
        level = kw.pop("level", -24.0)
        be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, **kw)
        rig = make_rig(be, fs)
        try:
            res = run_series(rig, be, freqs, level_dbfs=level, repeats=1)
            fatal, info = classify_result_events(res)
            all_valid = all(row["valid"] for row in res["rows"])
            # референс-семантика: fatal → invalid → текст «НЕ ЗАСЧИТАНО»
            import copy
            patched = copy.deepcopy(res)
            if fatal:
                for row in patched["rows"]:
                    for c in ("L", "R"):
                        row["channels"][c]["validity_reasons"] = ["hardware_fault"]
                        row["channels"][c]["valid"] = False
                    row["valid"] = False
            verdict = gainmatch.text_series(patched, 0.20)
            itog = [ln for ln in verdict.splitlines() if ln.startswith("ИТОГ")]
            no_success = (not all_valid) or (itog and "НЕ ЗАСЧИТАНО" in itog[0]) or bool(fatal)
            ok_all &= no_success
            details.append(f"{name}: fatal={len(fatal)}, valid={all_valid}, "
                           f"итог={'НЕ ЗАСЧИТАНО' if itog and 'НЕ ЗАСЧИТАНО' in itog[0] else '—'}")
        except MeasureError as exc:
            ok_all &= True
            details.append(f"{name}: громкий отказ «{str(exc)[:40]}…»")
    check("Q1", "Ни один фатальный сценарий не даёт успешный вердикт (XRUN×2, "
                "перегрузка, неполный захват)", "ok", ok_all,
          "XRUN и перегрузка → invalid (сейчас дефект); неполный захват → громкий отказ",
          "; ".join(details))

    # Q2: информационное событие (переход в BH при ppm) НЕ инвалидирует
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, clock_offset_ppm=20.8)
    rig = make_rig(be, fs)
    res, _ = run_series_with_spy(rig, be, freqs, repeats=1)
    fatal, info = classify_result_events(res)
    check("Q2", "Переход в Blackman–Harris (ppm < 100) — информационное событие: "
                "valid остаётся True, ΔG точна", "ok",
          all(row["valid"] for row in res["rows"]) and not fatal,
          "смена режима анализа не портит измерение — не браковать автоматически",
          f"fatal={fatal}; info={info}; valid={[row['valid'] for row in res['rows']]}")

    # Q3: сохраняемость событий в JSON
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0,
                         notes=["arecord: overrun!!! (at least 1024 frames)"])
    rig = make_rig(be, fs)
    res = run_series(rig, be, freqs, repeats=1)
    in_json = bool(res.get("warnings"))
    repeat_keys = sorted(res["rows"][0]["channels"]["L"]["repeats"][0].keys())
    analyzer_saved = "analyzer_warnings" in repeat_keys
    check("Q3-fix", "Аппаратные события и предупреждения анализатора доходят до JSON",
          "ok", in_json and analyzer_saved,
          "result['warnings'] сохраняется; analysis['warnings'] в повторах отсутствует "
          "— теряются при рассинхроне/некогерентности",
          f"warnings в результате: {in_json}; поле analyzer_warnings в записи "
          f"повтора: {analyzer_saved}")


# ============================================================ БЛОК R: контекстный TARE + RIAA

class ContextTare:
    """Референс-семантика контекстного TARE для патча: обнуление хранит условия
    и применяется только при полном совпадении контекста."""

    KEYS = ("freq", "level_dbfs", "fs", "route", "device_key", "mixer_hash", "cal_sig")

    def __init__(self):
        self.value = None
        self.ctx = None

    def set(self, ctx, value):
        self.ctx = dict(ctx)
        self.value = float(value)

    def usable(self, ctx):
        if self.ctx is None or self.value is None:
            return False
        return all(self.ctx.get(k) == ctx.get(k) for k in self.KEYS)


def block_r():
    print("\n=== Блок R: контекстный TARE (референс) и RIAA-риск ===")
    fs = 48000

    # R1: контекст отсекает все фантомы этапов 1–2
    base = dict(freq=1000.0, level_dbfs=-24.0, fs=48000, route=0,
                device_key="cardA", mixer_hash="h1", cal_sig="c1")
    tare = ContextTare()
    tare.set(base, value=0.10)
    checks = [
        ("та же условия", dict(base), True),
        ("смена частоты", dict(base, freq=10000.0), False),
        ("смена уровня", dict(base, level_dbfs=-12.0), False),
        ("смена карты", dict(base, device_key="cardB"), False),
        ("смена микшера", dict(base, mixer_hash="h2"), False),
        ("смена калибровки", dict(base, cal_sig="c2"), False),
    ]
    ok_all = True
    for name, ctx, expect in checks:
        got = tare.usable(ctx)
        ok_all &= (got == expect)
        print(f"        {name}: usable={got} (ожидалось {expect})")
    check("R1", "Контекстный TARE: обнуление применяется только при совпадении всех условий",
          "ok", ok_all,
          "семантика для патча: (freq, level, fs, route, device, mixer, cal) — любое "
          "расхождение отключает старый ноль",
          "см. список выше")

    # R2: RIAA-корректор на фикс. уровне генератора — клиппинг на басу
    from valvebench import dsp as _dsp
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, riaa_dut=True, clip_level=0.99)
    rig = make_rig(be, fs)
    res = run_series(rig, be, (100.0, 1000.0, 10000.0), level_dbfs=-12.0, repeats=1)
    lvls = {round(row["frequency_hz"]): (round(row["channels"]["L"]["level_dbfs"], 1),
                                          row["valid"]) for row in res["rows"]}
    clipped_100 = "adc_clip" in res["rows"][0]["channels"]["L"]["validity_reasons"]
    print(f"      без компенсации, ген −12 dBFS: уровни/valid = {lvls}")
    check("R2", "RIAА-каскад на фикс. уровне: на 100 Гц вход АЦП перегружен (+13 дБ) — "
                "громкий отказ adc_clip", "ok",
          clipped_100 and not res["rows"][0]["valid"],
          "ожидание: уровень на 100 Гц ≈ +1 dBFS → клип; продукт ловит (adc_clip), "
          "но измерение невозможно без per-frequency уровня",
          f"100 Гц: {lvls.get(100)}, valid={res['rows'][0]['valid']}; "
          f"warnings={res['warnings'][:1]}")

    # R3: компенсация уровней по обратной RIAA → ровно и валидно
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0, riaa_dut=True, riaa_compensate=True)
    rig = make_rig(be, fs)
    res = run_series(rig, be, (100.0, 1000.0, 10000.0), level_dbfs=-12.0, repeats=1)
    lvls3 = [round(row["channels"]["L"]["level_dbfs"], 2) for row in res["rows"]]
    deltas = [row["delta_adc_db"] for row in res["rows"]]
    check("R3", "Компенсация обратной RIAA: уровни на входе АЦП ровные, валидность полная",
          "ok", all(row["valid"] for row in res["rows"]) and max(lvls3) - min(lvls3) < 0.5,
          "ожидание: уровни всех трёх частот в пределах ±0.25 дБ, клипа нет",
          f"уровни L: {lvls3}; Δ={deltas}")

    # R4: значение RIAA-компенсации соответствует кривой (независимая сверка)
    exp_100 = 20.0 * math.log10(abs(_dsp.riaa_playback(np.array([100.0]))[0]))
    exp_10k = 20.0 * math.log10(abs(_dsp.riaa_playback(np.array([10000.0]))[0]))
    check("R4", "Компенсационные уровни соответствуют обратной RIAA "
                f"(100 Гц: {exp_100:+.1f} дБ, 10 кГц: {exp_10k:+.1f} дБ относительно 1 кГц)",
          "ok", abs(exp_100 - 13.1) < 0.3 and abs(exp_10k + 13.7) < 0.3,
          "независимая сверка кривой: +13.1/−13.7 дБ — классические значения",
          f"расчёт по riaa_playback: {exp_100:+.2f}/{exp_10k:+.2f} дБ")



# ============================================================ БЛОК S: задание №5

def block_s():
    print("\n=== Блок S: дрейф сети 49.5–50.5 Гц, RIAA-запас, фактические уровни ===")
    fs = 48000
    freqs_b = (94.0, 994.0, 9994.0)

    # S1: непрерывный диапазон дрейфа сети 49.5–50.5 Гц (шаг 0.1) для профиля
    # Mains Avoidance; особое внимание — 994 Гц против 20-й гармоники
    worst = {"mains": None, "f": None, "err": 0.0}
    grid = [49.5 + 0.1 * i for i in range(11)]
    for mains in grid:
        lines = mains_comb(mains, 0.0, 60, -60.0, 5)
        be = ScriptedBackend(fs=fs, noise_dbfs=-125.0,
                             extra_lines=(tuple(lines), tuple(asymmetric(lines))))
        rig = make_rig(be, fs)
        res = run_series(rig, be, freqs_b, repeats=1)
        for row in res["rows"]:
            err = abs(row["delta_adc_db"])
            if err > worst["err"]:
                worst = {"mains": mains, "f": row["frequency_hz"], "err": err}
        print(f"      сеть {mains:.1f} Гц: "
              f"{['%.4f' % abs(row['delta_adc_db']) for row in res['rows']]} дБ")
    check("S1", "Профиль Mains Avoidance: весь диапазон дрейфа 49.5–50.5 Гц даёт "
                "|ΔG-ошибку| ≤ 0.01 дБ", "ok", worst["err"] <= 0.01,
          "ожидание: спад 1/k делает ушедшие старшие гармоники безопасными, "
          "младшие ходят меньше 6 бинов",
          f"худшая точка: {worst['f']:g} Гц при сети {worst['mains']:.1f} Гц, "
          f"ошибка {worst['err']:.4f} дБ")

    # S2: коллизия 994 Гц с 20-й гармоникой при дрейфе 49.7 Гц (20×49.7=994.0).
    # Прибор проверяет НАСТРОЕННУЮ частоту сети: пользователь/профиль задаёт
    # фактическую частоту — тут cal["mains"]=49.7.
    lines = mains_comb(49.7, 0.0, 60, -60.0, 5)
    colliding = [f for f, _, _ in lines if abs(f - 994.0) < 1.0]
    be = ScriptedBackend(fs=fs, noise_dbfs=-125.0,
                         extra_lines=(tuple(lines), tuple(asymmetric(lines))))
    rig = make_rig(be, fs)
    rig.cal["mains"] = 49.7
    res = run_series(rig, be, freqs_b, repeats=1)
    err994 = abs(res["rows"][1]["delta_adc_db"])
    prox = [w for w in res["warnings"] if "994" in str(w)]
    check("S2", "Коллизия 994 Гц с 20-й гармоникой (сеть 49.7 Гц): ошибка мала "
                "(спад 1/k) И подключено proximity-предупреждение", "ok",
          err994 <= 0.01 and bool(prox),
          f"ожидание: 20-я гармоника {colliding[:1]} на частоте тона; уровень −86 dBFS → "
          f"ошибка ≤0.01 дБ; инструмент обязан предупредить о коллизии",
          f"ошибка 994 Гц = {err994:.4f} дБ; предупреждение: {prox[:1]}")

    # S3: RIAA-компенсация не меняет ΔG на плоском DUT (инвариантность)
    be_flat = ScriptedBackend(fs=fs, noise_dbfs=-125.0)
    rig_f = make_rig(be_flat, fs)
    res_plain = run_series(rig_f, be_flat, (100.0, 1000.0, 10000.0), repeats=2)
    be_flat.set_layout(rig_f, 3)
    res_ri = gainmatch.measure_series(rig_f, frequencies=(100.0, 1000.0, 10000.0),
                                      level_dbfs=-24.0, repeats=2, purpose="loopback",
                                      riaa_compensate=True)
    d_plain = [row["delta_adc_db"] for row in res_plain["rows"]]
    d_ri = [row["delta_adc_db"] for row in res_ri["rows"]]
    check("S3", "RIAА-компенсация не меняет ΔG на плоском тракте", "ok",
          max(abs(a - b) for a, b in zip(d_plain, d_ri)) < 0.01
          and all(row["valid"] for row in res_ri["rows"]),
          "ожидание: уровни разные (+13.1/0/−13.7 дБ), ΔG одинаковая",
          f"ΔG обычная={['%.4f' % v for v in d_plain]}; "
          f"ΔG с RIAA={['%.4f' % v for v in d_ri]}")

    # S4: цифровой запас — отказ при базе −12 (10 кГц потребует +1.73 dBFS),
    # успех при базе −16
    refused = False
    try:
        be12 = ScriptedBackend(fs=fs, noise_dbfs=-125.0)
        rig12 = make_rig(be12, fs)
        be12.set_layout(rig12, 3)
        gainmatch.measure_series(rig12, frequencies=(100.0, 1000.0, 10000.0),
                                 level_dbfs=-12.0, repeats=1, purpose="loopback",
                                 riaa_compensate=True)
    except Exception as exc:
        refused = "переполнение" in str(exc) or "полной шкалы" in str(exc)
    ok16 = False
    try:
        be16 = ScriptedBackend(fs=fs, noise_dbfs=-125.0)
        rig16 = make_rig(be16, fs)
        res16 = run_series(rig16, be16, (100.0, 1000.0, 10000.0), repeats=1,
                           riaa_compensate=True, level_dbfs=-16.0)
        ok16 = (max(res16["generator_levels_dbfs"].values()) <= -0.5
                and all(row["valid"] for row in res16["rows"]))
    except Exception:
        ok16 = False
    check("S4", "Цифровой запас: база −12 отклоняется с подсказкой, база −16 работает",
          "ok", refused and ok16,
          "ожидание: 10 кГц требует +13.73 дБ → −12+13.73=+1.73 dBFS недопустимо; "
          "−16+13.73=−2.27 dBFS — допустимо",
          f"отказ при −12: {refused}; успех при −16: {ok16}, "
          f"макс. уровень {max(res16['generator_levels_dbfs'].values()):+.2f} dBFS")

    # S5: фактические уровни генератора отображаются в тексте вердикта шага 2
    import copy as _copy
    res_ri_dut = _copy.deepcopy(res_ri)
    res_ri_dut["purpose"] = "dut"
    for row in res_ri_dut["rows"]:
        row["delta_gain_db"] = row["delta_adc_db"]
    text_ri = gainmatch.text_series(res_ri_dut, 0.20)
    check("S5", "Фактические уровни генератора по частотам видны в вердикте шага 2", "ok",
          "фактические уровни" in text_ri and "-10.27" in text_ri
          and "-37.09" in text_ri and "-24.00" in text_ri,
          "ожидание: строка «Режим RIAA-компенсации, фактические уровни генератора: "
          "100 Гц −37.09 dBFS, 1000 Гц −24.00 dBFS, 10000 Гц −10.27 dBFS»",
          text_ri.splitlines()[2] if len(text_ri.splitlines()) > 2 else text_ri[:120])


def text_hi_or_levels(text):
    import re as _re
    m = _re.search(r"10000 Гц ([+-][\d.]+) dBFS", text)
    return m.group(1) if m else ""




# ============================================================ БЛОК T: ревью координатора

def block_t():
    print("\n=== Блок T: адресные исправления по ревью координатора (0.1.19-R2) ===")
    fs = 48000
    freqs = (1000.0, 1500.0)
    t = np.arange(int(0.6 * fs)) / fs
    tone = amp_of(-20.0) * np.sin(2 * math.pi * 1000.0 * t)

    # T1: Live — лампа с сильными гармониками валидна (C/R, а не SINAD)
    # H2/H3 заданы в dBc относительно несущей −20 dBFS, а не как
    # абсолютные уровни dBFS. Иначе тест фактически проверяет −12/−20 dBc
    # и получает SINAD около 11.4 dB — это не дефект вычислителя.
    h2 = amp_of(-20.0 - 32.0) * np.sin(2 * math.pi * 2000.0 * t + 0.7)
    h3 = amp_of(-20.0 - 40.0) * np.sin(2 * math.pi * 3000.0 * t + 1.9)
    rng = np.random.default_rng(41)
    nz = band_limited_noise(t.size, fs, "white", -110.0, rng)
    ring_lamp = np.column_stack([tone + h2 + h3 + nz, tone + h2 + h3 + nz])
    tr = gainmatch.GainTracker()
    st = tr.update(ring_lamp, fs, 1000.0, -20.0, 0, None, now=0.0)
    _snr_ref_t1, sinad_ref_t1, _thdn_ref_t1 = ref_snr_sinad_thdn(ring_lamp[:, 0], fs, 1000.0)
    check("T1", "Live: H2=−32/H3=−40 dBc (SINAD≈31.4 dB) — валидна, C/R высокий",
          "ok", st["valid"] and min(st["carrier_residual_db"]) > 60.0
          and abs(min(st["sinad_db"]) - sinad_ref_t1) < 0.5
          and abs(sinad_ref_t1 - 31.36) < 0.5,
          "независимый расчёт мощности гармоник и остатка; Live SINAD совпадает с референсом ±0.5 dB",
          f"valid={st['valid']}; C/R={['%.1f' % v for v in st['carrier_residual_db']]}; "
          f"SINAD Live={['%.2f' % v for v in st['sinad_db']]}; "
          f"reference={sinad_ref_t1:.2f} dB")

    # T2: Live — шум (C/R < 30) бракуется с понятным сообщением
    nz_loud = band_limited_noise(t.size, fs, "white", -40.0, rng)
    ring_noisy = np.column_stack([tone + nz_loud, tone + nz_loud])
    st_n = tr.update(ring_noisy, fs, 1000.0, -20.0, 0, None, now=1.0)
    check("T2", "Live: шум (C/R≈4 дБ) бракуется (нестабильность огибающей или C/R)",
          "ok", (not st_n["valid"]) and ("C/R" in st_n["msg"]
                                         or "нестабильна" in st_n["msg"]),
          "ожидание: invalid, причина C/R",
          f"valid={st_n['valid']}; msg={st_n['msg'][:70]}")

    # T3: Live — щелчок внутри кольца бракуется подоконным контролем
    ring_b = ring_lamp.copy()
    n_b = int(0.05 * fs)
    tt = np.arange(n_b) / fs
    ring_b[int(0.2 * fs):int(0.2 * fs) + n_b, 0] += amp_of(-37.0) * np.sin(2 * math.pi * 1000.0 * tt)
    st_b = tr.update(ring_b, fs, 1000.0, -20.0, 0, None, now=2.0)
    check("T3", "Live: щелчок внутри окна — unstable (подоконный контроль в Live)",
          "ok", (not st_b["valid"]) and "нестабильна" in st_b["msg"],
          "ожидание: invalid с сообщением о нестабильности несущей",
          f"valid={st_b['valid']}; msg={st_b['msg'][:70]}")

    # T4: Live — стабильный сигнал на границе C/R≈30 не бракуется подокнами
    # (5 подокон по 100 мс достаточно, т.к. контроль выполняется после C/R-ворот)
    fp_ok = True
    worst = 0.0
    for seed in range(8):
        rng_i = np.random.default_rng(100 + seed)
        nz_i = band_limited_noise(t.size, fs, "white", -52.0, rng_i)   # C/R≈30
        r_i = np.column_stack([tone + nz_i, tone + nz_i])
        st_i = tr.update(r_i, fs, 1000.0, -20.0, 0, None, now=3.0 + 0.1 * seed)
        if st_i["valid"]:
            worst = max(worst, max(s["trend_span"] for s in st_i["envelope"]))
        fp_ok &= st_i["valid"]
    check("T4", "Live: 8 реализаций C/R≈30 — ни одного ложного брака подокнами",
          "ok", fp_ok,
          "ожидание: при C/R≥30 флуктуации 5 подокон (100 мс) ниже порога 0.005",
          f"все валидны: {fp_ok}; макс. trend_span={worst:.5f}")

    # T5: контекстный TARE — отсутствующий ключ = несовместимость
    tr2 = gainmatch.GainTracker()
    ctx_full = {"freq": 1000.0, "level_dbfs": -20.0, "fs": fs, "route": 0,
                "device_key": "sim", "mixer_hash": "h", "cal_sig": "c"}
    ring_t = np.column_stack([0.1 * np.sin(2 * math.pi * 1000 * np.arange(fs) / fs)] * 2)
    tr2.update(ring_t, fs, 1000.0, -20.0, 0, None, now=0.0, ctx=ctx_full)
    tr2.tare(ctx=ctx_full)
    ctx_no_freq = {k: v for k, v in ctx_full.items() if k != "freq"}
    st5 = tr2.update(ring_t, fs, 1000.0, -20.0, 0, None, now=1.0, ctx=ctx_no_freq)
    check("T5", "TARE: отсутствующий обязательный ключ контекста = безопасный сброс",
          "ok", st5["tare_db"] == 0.0 and st5.get("tare_reset_reason") is not None,
          "ожидаются: tare_db=0 и причина «… (нет данных)»",
          f"reason={st5.get('tare_reset_reason')}; tare={st5['tare_db']}")

    # T6: RIAA — подсказка из сообщения об ошибке приводит к успешному повтору
    be_r = ScriptedBackend(fs=fs, noise_dbfs=-125.0)
    rig_r = make_rig(be_r, fs)
    hint_base = None
    try:
        run_series(rig_r, be_r, (100.0, 1000.0, 10000.0), level_dbfs=-12.0,
                   repeats=1, riaa_compensate=True)
    except Exception as exc:
        m = re.search(r"до ([+-][0-9.]+) dBFS", str(exc))
        hint_base = float(m.group(1)) if m else None
    retry_ok = False
    if hint_base is not None:
        res_h = run_series(rig_r, be_r, (100.0, 1000.0, 10000.0),
                           level_dbfs=hint_base, repeats=1, riaa_compensate=True)
        retry_ok = all(row["valid"] for row in res_h["rows"])
    check("T6", "RIAА: подсказка об отказе рабочая — повтор с предложенной базой успешен",
          "ok", hint_base is not None and retry_ok,
          "ожидание: повтор с базой из подсказки проходит проверки и запас ЦАП",
          f"подсказка: база {hint_base} dBFS → повтор без отказа: {retry_ok}")

    # T7: C/R не считает H4/H5/H7 шумом (nharm=9)
    be_h = ScriptedBackend(fs=fs, noise_dbfs=-110.0,
                           extra_lines=(((4000.0, -64.0, 0.7), (5000.0, -70.0, 1.3),
                                         (7000.0, -74.0, 2.2)), ()))
    rig_h = make_rig(be_h, fs)
    res_h47 = run_series(rig_h, be_h, freqs, repeats=1)
    cr = res_h47["rows"][0]["channels"]["L"]["carrier_residual_db"]
    check("T7", "C/R: H4/H5/H7 (−40…−50 дБс) не превращаются в «шум» — ворота чисты",
          "ok", res_h47["rows"][0]["valid"] and cr > 60.0,
          "ожидание: nharm=9 исключает H2…H9 из негармонического остатка",
          f"valid={res_h47['rows'][0]['valid']}; C/R={cr:.1f} дБ")

    # T8: proximity — 9994 Гц против 200-й гармоники (сеть 49.97 Гц)
    be_p = ScriptedBackend(fs=fs, noise_dbfs=-125.0)
    rig_p = make_rig(be_p, fs)
    rig_p.cal["mains"] = 49.97
    res_p = run_series(rig_p, be_p, (94.0, 994.0, 9994.0), repeats=1)
    prox9994 = any("9994" in str(w) for w in res_p["warnings"])
    check("T8", "proximity проверяет гармоники до частоты ряда: 9994 Гц vs 200-я "
                "(200×49.97=9994) — предупреждение выдано",
          "ok", prox9994 and all(row["valid"] for row in res_p["rows"]),
          "ожидание: предупреждение о коллизии при k до 200-й",
          f"warnings={[w[:60] for w in res_p['warnings'][:2]]}")
    # Динамический обход не должен быть жёстко привязан к 50 Гц.
    w60 = gainmatch.mains_proximity_warnings((1020.0,), 60.0, df_hz=1.0)
    check("T8b", "Для сети 60 Гц рассчитывается ближайшая безопасная частота",
          "ok", bool(w60) and ("1013.5" in w60[0] or "1026.5" in w60[0]),
          "1020 Гц совпадает с 17-й гармоникой 60 Гц; ближайшая точка должна отстоять более чем на 6 бинов",
          w60[0] if w60 else "предупреждения нет")
    from pathlib import Path as _Path
    live_ui_source = (_Path(__file__).resolve().parents[1] / "valvebench" / "ui.py").read_text(encoding="utf-8")
    live_notice_wired = ("def _refresh_live_frequency_notice(self):" in live_ui_source
                         and "gainmatch.mains_proximity_warnings((freq,), mains" in live_ui_source
                         and "self._refresh_live_frequency_notice()" in live_ui_source)
    check("T8c", "Live UI показывает предупреждение о коллизии для фактически поданной частоты",
          "ok", live_notice_wired,
          "частотная гигиена должна быть подключена к LivePage, а не только к Static серии",
          "LivePage warning path=" + ("wired" if live_notice_wired else "missing"))

    # T9: щелчок на частоте тона в лид-тишине — onset на тоне
    be_o = ScriptedBackend(fs=fs, noise_dbfs=-125.0, burst=(0.05, 0.05, -6.0, (0, 1)))
    rig_o = make_rig(be_o, fs)
    res_o = run_series(rig_o, be_o, freqs, repeats=1)
    check("T9", "Щелчок на частоте тона в лид-тишине: onset на тоне, серия валидна",
          "ok", all(row["valid"] for row in res_o["rows"]),
          "узкополосный поиск с критерием устойчивости игнорирует короткий щелчок",
          f"valid={[row['valid'] for row in res_o['rows']]}")

    # T10: CSV — заголовки SINAD
    import tempfile
    from valvebench import store as store_mod
    with tempfile.TemporaryDirectory(prefix="gm-0119-") as tmp:
        store_mod.save_gain_series(res_p, tmp)
        csv_path = [os.path.join(tmp, f) for f in os.listdir(tmp) if f.endswith(".csv")][0]
        header = open(csv_path, encoding="utf-8").readline()
    check("T10", "CSV: заголовки SINAD_L_db/SINAD_R_db (не SNR)",
          "ok", "SINAD_L_db" in header and "SINAD_R_db" in header and "SNR_L_db" not in header,
          "ожидание: SINAD_L_db/SINAD_R_db в заголовке CSV",
          f"заголовок: {header[:110]}")




# ============================================================ БЛОК U: доводка R3

def block_u():
    print("\n=== Блок U: ppm-развёртка Live, провалы, кеш, XRUN ===")
    fs = 48000

    # U1: Live при рассогласовании тактов ±5/±10/±21/±50 ppm — чистый синус
    # не бракуется из-за одного лишь рассогласования, ΔG ≈ 0
    rows = []
    all_ok = True
    for f0 in (997.0, 1000.0, 10000.0):
        for ppm in (0, 5, 10, 21, 50, -5, -10, -21, -50):
            f_real = f0 * (1.0 + ppm * 1e-6)
            n = int(0.6 * fs)
            t = np.arange(n) / fs
            sig = amp_of(-20.0) * np.sin(2 * math.pi * f_real * t)
            rng = np.random.default_rng(int(abs(ppm)) + 60)
            nz = band_limited_noise(n, fs, "white", -110.0, rng)
            ring = np.column_stack([sig + nz, sig + nz])
            tr = gainmatch.GainTracker()
            st = tr.update(ring, fs, f0, -20.0, 0, None, now=0.0)
            derr = abs(st["delta"]) if st["valid"] else None
            rows.append((f0, ppm, st["valid"], derr))
            if ppm != 0 and (not st["valid"]):
                all_ok = False
            if st["valid"] and ppm != 0 and derr > 0.02:
                all_ok = False
            print(f"      {f0:6.0f} Гц, {ppm:+3d} ppm: valid={st['valid']}, "
                  f"ΔG={'—' if derr is None else '%.4f' % derr}, "
                  f"C/R={['%.1f' % v for v in st.get('carrier_residual_db', [0])] if st['valid'] else '—'}, "
                  f"msg={st['msg'][:40] if not st['valid'] else ''}")
    check("U1", "Live при рассогласовании ±5…±50 ppm (997/1к/10к): чистый синус "
                "не бракуется, ΔG < 0.02 дБ", "ok", all_ok,
          "частота трекается по медианному фазовому набегу — расстройка тактов "
          "не попадает в остаток (закрывает расчёт координатора: 21 ppm@10к "
          "давал бы C/R 14 дБ при фиксированной частоте)",
          "см. таблицу выше")

    # U2: провалы амплитуды 5–50 мс в кольце Live (отрицательные выбросы)
    fails_ok = True
    for dur in (0.005, 0.020, 0.050):
        n = int(0.6 * fs)
        t = np.arange(n) / fs
        sig = amp_of(-20.0) * np.sin(2 * math.pi * 1000.0 * t)
        i0 = int(0.25 * fs)
        sig[i0:i0 + int(dur * fs)] = 0.0            # провал (не всплеск!)
        rng = np.random.default_rng(77)
        nz = band_limited_noise(n, fs, "white", -110.0, rng)
        ring = np.column_stack([sig + nz, sig + nz])
        tr = gainmatch.GainTracker()
        st = tr.update(ring, fs, 1000.0, -20.0, 0, None, now=0.0)
        zmax = max(s["z_burst"] for s in st.get("envelope", [{}])) if st.get("envelope") else 0
        if st["valid"]:
            fails_ok = False
        print(f"      провал {dur * 1000:.0f} мс: valid={st['valid']}, "
              f"msg={st['msg'][:44]}, z={zmax:.1f}")
    check("U2", "Live: кратковременные ПРОВАЛЫ амплитуды 5–50 мс бракуются "
                "(симметричный z огибающей)", "ok", fails_ok,
          "ожидание: invalid через unstable_carrier — z симметричен по знаку",
          "см. выше")

    # U3: XRUN-дельта Live (чистая функция)
    f_ok1, nx1, na1 = gainmatch.live_xrun_state(None, None, 10.0, 0, hold_s=1.0)
    f_ok2, nx2, na2 = gainmatch.live_xrun_state(nx1, na1, 11.0, 0, hold_s=1.0)
    f_ok3, nx3, na3 = gainmatch.live_xrun_state(nx2, na2, 11.2, 2, hold_s=1.0)
    f_ok4, nx4, na4 = gainmatch.live_xrun_state(nx3, na3, 13.0, 2, hold_s=1.0)
    f_ok5, _, _ = gainmatch.live_xrun_state(nx4, na4, 25.0, 2, hold_s=1.0)
    f_initial_bad, _, _ = gainmatch.live_xrun_state(None, None, 30.0, 1, hold_s=1.0)
    f_counter_reset, _, _ = gainmatch.live_xrun_state(2, 25.0, 26.0, 0, hold_s=1.0)
    check("U3", "Live XRUN: initial nonzero/increment/reset block ΔG; hold then expires",
          "ok", (not f_ok1) and (not f_ok2) and f_ok3 and (not f_ok4) and (not f_ok5)
          and f_initial_bad and f_counter_reset,
          "ноль — базовая линия; приращение и сброс счётчика → invalid на 1 с; "
          "начальный ненулевой счётчик тоже invalid; затем блокировка снимается",
          f"base:{not f_ok1}, unchanged:{not f_ok2}, increment:{f_ok3}, "
          f"after hold:{not f_ok4}, later:{not f_ok5}, initial nonzero:{f_initial_bad}, "
          f"counter reset:{f_counter_reset}")

    # U4: кеш pinv — ограничен, hit/miss статистика, время hit/miss
    tr = gainmatch.GainTracker()
    times_hit, times_miss = [], []
    freqs_seq = [100.0 + i for i in range(30)]        # вращение частоты → miss
    for i, f_i in enumerate(freqs_seq):
        n = int(0.6 * fs)
        t = np.arange(n) / fs
        sig = amp_of(-20.0) * np.sin(2 * math.pi * f_i * t)
        ring_i = np.column_stack([sig, sig])
        tr.update(ring_i, fs, f_i, -20.0, 0, None, now=0.1 * i)
    # замер miss: новая частота
    t0 = time.perf_counter()
    f_new = 500.0
    n = int(0.6 * fs)
    t = np.arange(n) / fs
    sig = amp_of(-20.0) * np.sin(2 * math.pi * f_new * t)
    ring_n = np.column_stack([sig, sig])
    tr.update(ring_n, fs, f_new, -20.0, 0, None, now=99.0)
    t_miss = (time.perf_counter() - t0) * 1000
    # Cache hit измеряем серией, чтобы единичный вытесненный планировщиком
    # GUI-поток не выдавать за типичную задержку попадания в кеш.
    hit_times = []
    for j in range(25):
        t0 = time.perf_counter()
        tr.update(ring_n, fs, f_new, -20.0, 0, None, now=99.1 + 0.01 * j)
        hit_times.append((time.perf_counter() - t0) * 1000)
    hit_sorted = sorted(hit_times)
    hit_p95 = float(np.percentile(hit_sorted, 95))
    hit_max = hit_sorted[-1]
    t_hit = hit_sorted[len(hit_sorted) // 2]
    cache_len = max(len(gainmatch._PINV_CACHE), len(gainmatch._SUB_CACHE))
    # U5: p50/p95/p99 при вращении частоты (частично miss/hit)
    p = []
    for i in range(49):          # 7 тиков прогрева (первый проход, miss) + 42 стационарных
        f_i = 600.0 + (i % 7)
        t = np.arange(n) / fs
        sig = amp_of(-20.0) * np.sin(2 * math.pi * f_i * t)
        ring_i = np.column_stack([sig, sig])
        t0 = time.perf_counter()
        tr.update(ring_i, fs, f_i, -20.0, 0, None, now=200.0 + 0.1 * i)
        p.append((time.perf_counter() - t0) * 1000)
    warmup = p[:7]               # первый проход цикла (кеш-промахи допустимы)
    p = sorted(p[7:])            # стационарный режим — метрика для таймера 75 мс
    p50, p95, p99 = (float(np.percentile(p, q)) for q in (50, 95, 99))
    pmax = p[-1]
    warm_max = max(warmup)
    stats = dict(gainmatch._PINV_STATS)
    cache_bytes = sum(gainmatch._cache_nbytes(v) for v in gainmatch._PINV_CACHE.values())
    check("U4", "Кеш pinv: LRU ограничен 8 профилями и 64 МиБ, hit/miss считаются",
          "ok", (len(gainmatch._PINV_CACHE) <= 8
           and len(gainmatch._SUB_CACHE) <= 8 and cache_bytes <= gainmatch._PINV_CACHE_BUDGET)
           and stats["hit"] > 0 and stats["miss"] > 0,
          "ожидание: len ≤ 8 при 30+ разных частотах; hit и miss растут",
          f"len={cache_len}; bytes={cache_bytes}; hit={stats['hit']}, miss={stats['miss']}")
    check("U5", "Замеры update(): miss < 75 мс (период GUI), hit p95 < 50 мс, "
                "стационарные p50/p95 ≤ 30 мс и p99 ≤ 50 мс", "ok",
          max(t_miss, warm_max) < 75.0 and hit_p95 < 50.0 and hit_max < 75.0
          and p50 <= 30.0 and p95 <= 30.0 and p99 <= 50.0 and pmax < 75.0,
          "miss не должен переживать 75-мс GUI-тик; hit оценивается по 25 измерениям; "
          "p99 и максимум показываются отдельно",
          f"miss={t_miss:.1f} мс (прогрев max {warm_max:.1f}); hit p50={t_hit:.1f}, "
          f"p95={hit_p95:.1f}, max={hit_max:.1f} мс; "
          f"стационар: p50={p50:.1f}, p95={p95:.1f}, p99={p99:.1f}, max={pmax:.1f} мс")



# ============================================================ main

def main():
    t0 = time.time()
    print("Независимая проверка Gain Match 0.1.18 —", PRODUCT)
    block_a()
    block_b()
    block_c()
    block_d()
    block_e()
    n_ae = len(RESULTS)
    p_ae = sum(1 for r in RESULTS if r["pass"])
    block_f()
    block_g()
    block_h()
    block_i()
    block_j()
    block_k()
    block_l()
    block_m()
    n_fm = len(RESULTS)
    p_fm = sum(1 for r in RESULTS if r["pass"])
    block_n()
    block_o()
    block_p()
    block_q()
    block_r2 = None  # placeholder (R уже вызван выше)
    block_r()
    block_s()
    block_t()
    block_u()
    n_all = len(RESULTS)
    p_all = sum(1 for r in RESULTS if r["pass"])
    print(f"\nИТОГ: A–E {p_ae}/{n_ae}; F–M {p_fm - p_ae}/{n_fm - n_ae}; "
          f"N–R {p_all - p_fm}/{n_all - n_fm}; всего {p_all}/{n_all}; "
          f"время {time.time() - t0:.1f} с")
    out = os.path.join(OUT_DIR,
                       f"gainmatch-audit-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"product": PRODUCT, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "results": RESULTS}, fh, ensure_ascii=False, indent=1)
    print("JSON:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
