"""Измерения: «программы» воспроизведения/записи и высокоуровневые приборы.

Каждый прибор — функция Rig → dict-результат (JSON-совместимый после to_jsonable),
плюс функция text_*() с готовым отчётом для консоли и GUI.
"""
from __future__ import annotations

import math
import time

import numpy as np
from scipy import signal as sg

from . import dsp
from .calib import Calibration, fmt_v
from .signals import make_test_noise

CH = ("L", "R")


class MeasureError(RuntimeError):
    pass


class Rig:
    """Стенд: бэкенд + калибровка + параметры когерентного окна."""

    def __init__(self, backend, cal=None, fs=96000, df=1.0, blocks=2, settle=0.5):
        self.be = backend
        self.cal = cal or Calibration()
        self.fs = int(fs)
        self.df = df
        self.blocks = blocks
        self.settle = settle
        self.n = int(round(self.fs / df))
        self.progress = lambda msg: None      # GUI подменяет
        self.cancel = False

    def open(self):
        self.be.open(self.fs)

    def amp_tone(self, f, dbfs):
        """(бин k, амплитуда): частота округляется к когерентной."""
        return max(1, int(round(f * self.n / self.fs))), float(dsp.dbfs_to_amp(dbfs))

    def f_of(self, k):
        return k * self.fs / self.n

    # ---- рендер сегмента программы
    def _seg(self, tones_l, tones_r, settle):
        s_n = int(round(settle * self.fs))
        N = s_n + self.blocks * self.n + int(0.05 * self.fs)      # хвост-запас на неточность отсчёта
        i = np.arange(N, dtype=np.int64)
        sig = np.zeros((N, 2))
        for ch, tones in ((0, tones_l), (1, tones_r)):
            for k, a in tones:
                sig[:, ch] += a * np.sin(2 * np.pi * ((k * i) % self.n) / self.n)
        if np.max(np.abs(sig)) > 0.999:
            raise MeasureError("сумма тонов выше 0 dBFS — уменьши уровень")
        r = int(0.01 * self.fs)
        ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(r) / r)
        sig[:r] *= ramp[:, None]
        sig[-r:] *= ramp[::-1][:, None]
        return sig, s_n

    def run(self, segs):
        """segs: [(tones_l, tones_r, settle|None)] → ([окна (blocks·n, 2)], warnings).

        Первый сегмент должен быть самым громким — по нему находим начало."""
        parts, meta = [], []
        for tl, tr, st in segs:
            sig, s_n = self._seg(tl, tr, self.settle if st is None else st)
            parts.append(sig)
            meta.append((len(sig), s_n))
        rec = self.be.play_record(np.concatenate(parts))
        warns = list(self.be.notes)
        d = self._onset(rec)
        wins, pos = [], d
        for (ln, s_n) in meta:
            a = pos + s_n
            b = a + self.blocks * self.n
            if b > len(rec):
                raise MeasureError("запись короче программы — захват оборвался")
            wins.append(rec[a:b])
            pos += ln
        pk = max(float(np.max(np.abs(w))) for w in wins)
        if pk >= 0.98:
            warns.append(f"ПЕРЕГРУЗКА ВХОДА АЦП (пик {dsp.db20(pk):.1f} dBFS): уменьши уровень или добавь делитель")
        return wins, warns

    def _onset(self, rec):
        blk = int(0.005 * self.fs)
        m = len(rec) // blk
        env = np.sqrt(np.mean(rec[: m * blk].reshape(m, blk, 2) ** 2, axis=1)).max(axis=1)
        noise = max(float(np.median(env[: int(0.2 / 0.005)])), 1e-7)
        thr = 8.0 * noise
        for i in range(m - 4):
            if np.all(env[i:i + 4] > thr):
                return i * blk
        raise MeasureError("сигнал на входе не найден: проверь кабели, уровни и выбранные каналы")

    # ---- переиспользуемое
    def _levels_info(self, gen_dbfs, rec_dbfs, ch):
        return {"dut_in_v": self.cal.dut_in_v(gen_dbfs, ch), "out_v": self.cal.in_v(rec_dbfs, ch),
                "gain_db": self.cal.gain_db(gen_dbfs, rec_dbfs, ch)}

    def _tone_an(self, win, ch, k, nharm=9, coherent_line_floors=None):
        return dsp.analyze_tone(win[:, ch], self.fs, self.f_of(k), self.n, nharm=nharm,
                                band=(20.0, self.cal["band_hi"]), mains=self.cal["mains"],
                                coherent_line_floors=coherent_line_floors)


# ------------------------------------------------------------------ проверка такта


def clock_check(rig):
    k, a = rig.amp_tone(1000.0, -12.0)
    wins, warns = rig.run([([(k, a)], [(k, a)], 0.4)])
    out = {"kind": "clock", "warnings": warns, "ch": {}}
    for c in (0, 1):
        r = rig._tone_an(wins[0], c, k, nharm=2)
        err = (r["f0"] / rig.f_of(k) - 1.0) * 1e6
        out["ch"][CH[c]] = {"f0": r["f0"], "ppm": err, "level_dbfs": r["level_dbfs"]}
        for detail in r.get("warnings", []):
            warns.append(f"{CH[c]}: {detail}")
        if abs(err) > 100:
            warns.append(f"{CH[c]}: частота захвата отличается от воспроизведения на {err:+.0f} ppm "
                         f"(×{r['f0'] / rig.f_of(k):.4f}) — такт ЦАП и АЦП разный, измерения ложны")
    out["ok"] = not any(("такт" in w or "не когерентен" in w) for w in warns)
    return out


def text_clock(r):
    lines = ["Проверка такта (1 кГц, −12 dBFS):"]
    for c, v in r["ch"].items():
        lines.append(f"  {c}: {v['f0']:.3f} Гц  ({v['ppm']:+.1f} ppm)  уровень {v['level_dbfs']:.1f} dBFS")
    lines.append("  такт ЦАП = такту АЦП; тон когерентен окну" if r["ok"] else "  ТАКТ/КОГЕРЕНТНОСТЬ НЕ ПРОШЛИ ПРОВЕРКУ")
    return "\n".join(lines + [f"! {w}" for w in r["warnings"]])


# --------------------------------------------------------------------------- THD


def thd_point(rig, f=1000.0, level_dbfs=-6.0, route="both", nharm=9):
    k, a = rig.amp_tone(f, level_dbfs)
    tl = [(k, a)] if route in ("both", "L") else []
    tr = [(k, a)] if route in ("both", "R") else []
    wins, warns = rig.run([(tl, tr, None)])
    res = {"kind": "thd", "f": rig.f_of(k), "level_dbfs": level_dbfs, "warnings": warns, "ch": {}}
    for c in (0, 1):
        if (c == 0 and not tl) or (c == 1 and not tr):
            continue
        r = rig._tone_an(wins[0], c, k, nharm)
        r["info"] = rig._levels_info(level_dbfs, r["level_dbfs"], c)
        res["ch"][CH[c]] = r
        warns += [f"{CH[c]}: {w}" for w in r["warnings"]]
    return res


def text_thd(res):
    L = [f"THD  f = {res['f']:.1f} Гц, уровень генератора {res['level_dbfs']:.1f} dBFS"]
    for c, r in res["ch"].items():
        inf = r["info"]
        L.append(f"\n[{c}]  вход АЦП {r['level_dbfs']:.2f} dBFS"
                 + (f" = {fmt_v(inf['out_v'])}" if inf["out_v"] else "")
                 + (f" | вход DUT {fmt_v(inf['dut_in_v'])}" if inf["dut_in_v"] else "")
                 + (f" | усиление {inf['gain_db']:+.2f} дБ" if inf["gain_db"] is not None else ""))
        residual_dbfs = r["level_dbfs"] + r["thdn_db"]
        if r.get("thd_bound"):
            L.append(f"  THD    не обнаружен выше порога достоверности; сырая сумма гармонических бинов "
                     f"(включая шум): {r['thd_upper_pct']:.5f} % ({r['thd_upper_db']:.1f} дБ)")
        elif r.get("thd_partial"):
            L.append(f"  THD обнаруженных гармоник: {r['thd_pct']:.5f} % ({r['thd_db']:.1f} дБ); "
                     f"неразрешённые линии: сырой суммарный уровень {r['thd_upper_pct']:.5f} % ({r['thd_upper_db']:.1f} дБ)")
        else:
            L.append(f"  THD    {r['thd_pct']:.5f} %   ({r['thd_db']:.1f} дБ)")
        if r.get("phase_coherence") is not None:
            L.append(f"  Фазовая согласованность блоков: {r['phase_coherence']:.4f}")
        if r.get("line_floor_dbc") is not None:
            L.append(f"  Пол анализа гармоник: {r['line_floor_dbc']:.1f} dBc ({r['level_dbfs'] + r['line_floor_dbc']:.1f} dBFS) "
                     f"· когерентные линии, усреднение {r['blocks']} с")
        L.append(f"  THD+N  {r['thdn_pct']:.5f} %   ({r['thdn_db']:.1f} dBc; остаток {residual_dbfs:.1f} dBFS)")
        L.append(f"  Шум без гармоник в полосе 20 Гц–{min(20000, r.get('band_hi', 20000))} Гц: "
                 f"{r['noise_dbfs']:.1f} dBFS ({r['noise_dbc']:.1f} dBc)")
        L.append("  гармоники: " + "  ".join(
            f"H{h['h']} {'≤' if h.get('bound') else ''}{h['dbc']:.1f} dBc ({r['level_dbfs'] + h['dbc']:.1f} dBFS)"
            for h in r["harmonics"]))
        if r["hum"]:
            L.append("  фон, дБc: " + "  ".join(f"{h['f']:.0f} Гц {h['dbc']:.1f}" for h in r["hum"]))
    return "\n".join(L + [f"! {w}" for w in res["warnings"]])


def thd_vs_level(rig, f=1000.0, levels=None, nharm=9):
    """THD(уровень): один проход, сегменты от громкого к тихому."""
    levels = sorted(levels if levels is not None else range(-6, -61, -6), reverse=True)
    segs = []
    for lv in levels:
        k, a = rig.amp_tone(f, lv)
        segs.append(([(k, a)], [(k, a)], None))
    wins, warns = rig.run(segs)
    out = {"kind": "thd_level", "f": rig.f_of(rig.amp_tone(f, 0)[0]), "levels": list(levels),
           "warnings": warns, "ch": {}}
    k = rig.amp_tone(f, 0)[0]
    for c in (0, 1):
        rows = []
        for lv, w in zip(levels, wins):
            try:
                r = rig._tone_an(w, c, k, nharm)
                rows.append({"gen": lv, "in": r["level_dbfs"], "thd_pct": r["thd_pct"],
                             "thdn_pct": r["thdn_pct"], "thd_db": r["thd_db"], "thdn_db": r["thdn_db"],
                             "bound": bool(r.get("thd_bound")), "partial": bool(r.get("thd_partial")),
                             "thd_upper_pct": r.get("thd_upper_pct"), "thd_upper_db": r.get("thd_upper_db")})
            except ValueError:
                rows.append({"gen": lv, "in": None, "thd_pct": float("nan"), "thdn_pct": float("nan"),
                             "thd_db": float("nan"), "thdn_db": float("nan")})
        out["ch"][CH[c]] = rows
    return out


def text_thd_level(res):
    L = [f"THD от уровня, f = {res['f']:.1f} Гц"]
    for c, rows in res["ch"].items():
        L.append(f"\n[{c}]   ген dBFS   вх АЦП dBFS   THD %      THD+N %")
        for r in rows:
            L.append(f"      {r['gen']:7.1f}   {r['in'] if r['in'] is not None else float('nan'):10.2f}   "
                     f"{'≤' if r.get('bound') else ' '}{r['thd_pct']:.5f}   {r['thdn_pct']:.5f}")
    return "\n".join(L + [f"! {w}" for w in res["warnings"]])


# --------------------------------------------------------------------------- IMD

IMD_PRESETS = {
    "CCIF 19+20 кГц": (19000.0, 20000.0, "ccif"),
    "SMPTE 60 Гц + 7 кГц (4:1)": (60.0, 7000.0, "smpte"),
    "DIN 250 Гц + 8 кГц (4:1)": (250.0, 8000.0, "smpte"),
    "CCIF 14+15 кГц": (14000.0, 15000.0, "ccif"),
}


def imd(rig, f1, f2, mode="ccif", level_dbfs=-6.0):
    """level_dbfs — пик суммы двух тонов. CCIF: поровну; SMPTE: 4:1 (низкий в 4 раза выше)."""
    lo, hi = sorted((f1, f2))
    pk = float(dsp.dbfs_to_amp(level_dbfs))
    a_lo, a_hi = (pk * 0.8, pk * 0.2) if mode == "smpte" else (pk * 0.5, pk * 0.5)
    klo, khi = rig.amp_tone(lo, 0)[0], rig.amp_tone(hi, 0)[0]
    wins, warns = rig.run([([(klo, a_lo), (khi, a_hi)], [(klo, a_lo), (khi, a_hi)], None)])
    out = {"kind": "imd", "mode": mode, "f1": rig.f_of(klo), "f2": rig.f_of(khi),
           "level_dbfs": level_dbfs, "warnings": warns, "ch": {}}
    for c in (0, 1):
        out["ch"][CH[c]] = dsp.analyze_imd(wins[0][:, c], rig.fs, out["f1"], out["f2"], rig.n,
                                            mode=mode, band_hi=min(rig.cal["band_hi"] * 2, 0.45 * rig.fs))
    return out


def text_imd(res):
    name = "SMPTE/DIN" if res["mode"] == "smpte" else "CCIF"
    L = [f"IMD {name}: {res['f1']:.0f} + {res['f2']:.0f} Гц, пик суммы {res['level_dbfs']:.1f} dBFS"]
    for c, r in res["ch"].items():
        L.append(f"\n[{c}]  IMD {r['imd_pct']:.5f} %  ({r['imd_db']:.1f} дБ)   чётные {r['d2_pct']:.5f} %   нечётные {r['d3_pct']:.5f} %"
                 f"   (к {r['ref']})")
        for p in r["products"]:
            L.append(f"     {p['label']:>9s}  {p['f']:8.0f} Гц  {p['dbc']:8.1f} дБc")
    return "\n".join(L + [f"! {w}" for w in res["warnings"]])



# ------------------------------------------------------ шумовая АЧХ / transfer


def noise_transfer_capture(source, rec, fs, f_lo=20.0, f_hi=20000.0, iec=False,
                          analysis_signal=None, analysis_offset=0):
    """Сопоставить известный сигнал с записью и вычислить H1 для L/R.

    source — опорная последовательность для поиска задержки в записи.
    analysis_signal — участок source, по которому считать H1. Это важно для
    пакетного теста: начальная/конечная тишина нужна для синхронизации, но не
    должна попадать в спектральную оценку передаточной функции.
    analysis_offset — начало analysis_signal относительно source.
    """
    source = np.asarray(source, dtype=float).ravel()
    analysis = source if analysis_signal is None else np.asarray(analysis_signal, dtype=float).ravel()
    rec = np.asarray(rec, dtype=float)
    if source.size < 8192 or analysis.size < 8192:
        raise MeasureError("для шумовой АЧХ нужно не менее 8192 отсчётов")
    if rec.ndim != 2 or rec.shape[1] < 2:
        raise MeasureError("захват должен содержать два канала")
    if len(rec) < len(source):
        raise MeasureError("для шумовой АЧХ нужно накопить не менее одной полной опорной последовательности")
    offset = int(analysis_offset)
    if offset < 0 or offset + len(analysis) > len(source):
        raise MeasureError("участок анализа выходит за границы опорного сигнала")

    # Корреляция на пониженной частоте существенно дешевле, а точность в
    # пределах нескольких исходных отсчётов для Welch-оценки достаточна.
    dec = max(1, int(fs) // 12000)
    ref = sg.resample_poly(source, 1, dec)
    observed = sg.resample_poly(rec[:, 0], 1, dec)
    ref = ref - np.mean(ref)
    observed = observed - np.mean(observed)
    corr = sg.correlate(observed, ref, mode="valid", method="fft")
    coarse_lag = int(np.argmax(np.abs(corr))) * dec

    # Уточняем задержку до одного исходного отсчёта. Иначе при Fs=96 кГц
    # ошибка до 7 отсчётов сама портит фазу на верхах звукового диапазона.
    probe_n = min(len(source), max(8192, int(0.35 * fs)))
    probe = source[:probe_n] - float(np.mean(source[:probe_n]))
    low = max(0, coarse_lag - dec)
    high = min(len(rec) - probe_n, coarse_lag + dec)
    best_lag, best_score = coarse_lag, -1.0
    probe_norm = float(np.linalg.norm(probe)) + 1e-30
    for candidate in range(low, high + 1):
        segment = rec[candidate:candidate + probe_n, 0]
        segment = segment - float(np.mean(segment))
        score = abs(float(np.dot(probe, segment))) / (probe_norm * (float(np.linalg.norm(segment)) + 1e-30))
        if score > best_score:
            best_lag, best_score = candidate, score
    lag = int(best_lag)
    begin = lag + offset
    aligned = rec[begin:begin + len(analysis)]
    if len(aligned) < len(analysis):
        raise MeasureError("не удалось совместить тестовый шум и захват")
    out = {"alignment_samples": lag, "ch": {}}
    for c, name in ((0, "L"), (1, "R")):
        r = dsp.analyze_transfer_noise(analysis, aligned[:, c], fs, band=(f_lo, f_hi))
        freq = r["freq"]
        i1k = int(np.argmin(np.abs(freq - 1000.0)))
        norm = r["transfer_db"] - r["transfer_db"][i1k]
        r["fr_norm_db"] = norm
        r["riaa_dev_db"] = norm - dsp.riaa_playback_db(freq, iec)
        r["channel"] = name
        out["ch"][name] = r
    return out


def noise_response(rig, seconds=10.0, kind="pink", level_dbfs=-24.0, f_lo=20.0, f_hi=20000.0,
                   iec=False, seed=12345):
    """Измерение передаточной функции шумовым сигналом (H1 + coherence).

    Это не просто RTA выхода: известный цифровой сигнал сопоставляется с записью.
    DAC/ADC и внешний измерительный адаптер всё равно входят в результат и должны
    быть учтены петлевой калибровкой перед абсолютными выводами.
    """
    if seconds < 4.0 or seconds > 60.0:
        raise MeasureError("для шумовой АЧХ выбери длительность от 4 до 60 секунд")
    if f_hi >= 0.45 * rig.fs:
        raise MeasureError(f"верхняя частота должна быть ниже 0.45·Fs ({0.45 * rig.fs:.0f} Гц)")
    if not (f_lo < 1000.0 <= f_hi):
        raise MeasureError("полоса измерения должна включать 1 кГц: по нему нормируется АЧХ")
    preemph = None
    signal_kind = kind
    if kind == "riaa":
        signal_kind = "riaa"
        preemph = lambda f: 10.0 ** (-dsp.riaa_playback_db(np.maximum(f, 1.0), iec) / 20.0)
    x, info = make_test_noise(rig.fs, seconds, kind=signal_kind, rms_dbfs=level_dbfs,
                              f_lo=f_lo, f_hi=f_hi, seed=seed, riaa_shape=preemph)
    lead = int(0.35 * rig.fs)
    tail = int(0.25 * rig.fs)
    source = np.concatenate([np.zeros(lead), x, np.zeros(tail)])
    stereo = np.column_stack((source, source))
    rec = rig.be.play_record(stereo, tail=0.3)
    warns = list(rig.be.notes)
    if float(np.max(np.abs(rec))) >= 0.98:
        warns.append("ПЕРЕГРУЗКА АЦП во время шумового измерения — результат недостоверен")
    aligned = noise_transfer_capture(source, rec, rig.fs, f_lo, f_hi, iec,
                                      analysis_signal=x, analysis_offset=lead)
    out = {"kind": "noise_response", "mode": kind, "seconds": seconds, "level_dbfs": level_dbfs,
           "signal": info, "iec": bool(iec), "warnings": warns, "ch": aligned["ch"],
           "alignment_samples": aligned["alignment_samples"],
           "calibration_note": "H1 включает АЦП, ЦАП, аттенюатор и DUT; без loop-профиля это относительное измерение."}
    for name, r in out["ch"].items():
        if r["valid_fraction_coh_0_8"] < 0.8:
            warns.append(f"{name}: coherence ниже 0.8 на значительной части полосы; проверьте уровень, шум и перегрузку")
    return out


def text_noise_response(res):
    title = {"white": "белый шум", "pink": "розовый шум", "riaa": "шум с inverse-RIAA"}.get(res["mode"], res["mode"])
    lines = [f"Шумовая АЧХ / H1: {title}; {res['seconds']:.1f} с; генератор {res['signal']['actual_rms_dbfs']:.1f} dBFS RMS",
             f"Разрешение Welch: {res['ch']['L']['freq'][1] - res['ch']['L']['freq'][0]:.3f} Гц; сегмент {res['ch']['L']['nperseg']} отсчётов",
             "Частота, Гц | L откл. RIAA, дБ | R откл. RIAA, дБ | coherence L/R"]
    f = res["ch"]["L"]["freq"]
    rows = []
    for fq in (20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 315, 500, 1000, 2000,
               3150, 5000, 8000, 10000, 12500, 16000, 20000):
        if fq < f[0] or fq > f[-1]:
            continue
        i = int(np.argmin(np.abs(f - fq)))
        l, r = res["ch"]["L"], res["ch"]["R"]
        rows.append(f"{f[i]:9.1f} | {l['riaa_dev_db'][i]:+9.2f} | {r['riaa_dev_db'][i]:+9.2f} | {l['coherence'][i]:.2f}/{r['coherence'][i]:.2f}")
    lines.extend(rows)
    lines += ["", res["calibration_note"], f"Совмещение сигнала: {res['alignment_samples']} отсчётов"]
    if res["signal"]["peak_limited"]:
        lines.append("! RMS тестового шума снижен для удержания пика ниже -3 dBFS")
    return "\n".join(lines + [f"! {w}" for w in res["warnings"]])

# -------------------------------------------------------------------------- шум


def noise(rig, seconds=6.0):
    """Вход корректора должен быть закорочен/нагружен источником (резистор или картридж)."""
    n_s = int((seconds + 0.5) * rig.fs)
    rec = rig.be.play_record(np.zeros((n_s, 2)), tail=0.0)
    x = rec[int(0.5 * rig.fs):int(0.5 * rig.fs) + int(seconds * rig.fs)]
    out = {"kind": "noise", "seconds": seconds, "warnings": list(rig.be.notes), "ch": {}}
    for c in (0, 1):
        r = dsp.analyze_noise(x[:, c], rig.fs, band=(20.0, rig.cal["band_hi"]), mains=rig.cal["mains"])
        r["in_v_unw"] = rig.cal.in_v(r["unw_dbfs"], c)
        r["in_v_a"] = rig.cal.in_v(r["a_dbfs"], c)
        out["ch"][CH[c]] = r
    return out


def text_noise(res):
    L = [f"Шум, запись {res['seconds']:.0f} с, полоса 20 Гц–20 кГц"]
    for c, r in res["ch"].items():
        L.append(f"\n[{c}]  без веса {r['unw_dbfs']:.1f} dBFS"
                 + (f" = {fmt_v(r['in_v_unw'])}" if r["in_v_unw"] else "")
                 + f" | A {r['a_dbfs']:.1f} | C {r['c_dbfs']:.1f}"
                 + (f" ({fmt_v(r['in_v_a'])})" if r["in_v_a"] else ""))
        if r["hum"]:
            L.append("  фон: " + "  ".join(f"{h['f']:.0f} Гц {h['dbfs']:.1f} dBFS" for h in r["hum"]))
    return "\n".join(L + [f"! {w}" for w in res["warnings"]])


# ------------------------------------------------------------------- проникновение


def crosstalk(rig, freqs=(100.0, 1000.0, 10000.0), level_dbfs=-6.0):
    ks = [rig.amp_tone(f, level_dbfs) for f in freqs]
    segs = []
    for k, a in ks:
        segs += [([(k, a)], [], None), ([], [(k, a)], None)]
    wins, warns = rig.run(segs)
    out = {"kind": "crosstalk", "warnings": warns, "rows": []}
    for i, (k, a) in enumerate(ks):
        wl, wr = wins[2 * i], wins[2 * i + 1]
        row = {"f": rig.f_of(k)}
        for name, w, drv, oth in (("L→R", wl, 0, 1), ("R→L", wr, 1, 0)):
            ld = rig._tone_an(w, drv, k, 2)["level_dbfs"]
            lo = rig._tone_an(w, oth, k, 2)["level_dbfs"]
            row[name] = lo - ld
        out["rows"].append(row)
    return out


def text_crosstalk(res):
    L = ["Проникновение каналов (нижний предел — собственный шум):", f"{'Гц':>8s}{'L→R, дБ':>12s}{'R→L, дБ':>12s}"]
    for r in res["rows"]:
        L.append(f"{r['f']:8.0f}{r['L→R']:12.1f}{r['R→L']:12.1f}")
    return "\n".join(L + [f"! {w}" for w in res["warnings"]])


# --------------------------------------------------------------------- АЧХ / THD(f)


def sweep(rig, f_lo=20.0, f_hi=20000.0, dur=8.0, level_1k_dbfs=-20.0, mode="riaa", iec=False,
          nharm=5):
    """АЧХ, фаза и THD(f) по гармоникам за один проход (экспоненциальный свип, Фарина).

    mode='flat'  — постоянный уровень на входе DUT;
    mode='riaa'  — предыскажение inverse-RIAA: на выходе корректора ровно, а АЧХ
                   отдаётся как отклонение от RIAA (опционально +IEC)."""
    f1 = f_lo / 2.0
    f2 = min(f_hi * 1.15, 0.45 * rig.fs)
    a1 = float(dsp.dbfs_to_amp(level_1k_dbfs))
    warns = []
    probe = np.geomspace(f1, f2, 400)
    if mode == "riaa":
        shape = lambda f: 10 ** (-dsp.riaa_playback_db(f, iec) / 20)
    else:
        shape = lambda f: np.ones_like(np.asarray(f, dtype=float))
    peak = a1 * float(np.max(shape(probe)))
    if peak > 0.9:
        a1 *= 0.9 / peak
        warns.append(f"уровень на 1 кГц снижен до {dsp.db20(a1):.1f} dBFS: inverse-RIAA на ВЧ "
                     "требует запаса до полной шкалы")
    spec = dsp.make_sweep(f1, f2, dur, rig.fs, preemph=lambda f: a1 * shape(f))
    rec = rig.be.play_record(np.stack([spec.x, spec.x], axis=1), tail=1.0)
    warns += list(rig.be.notes)
    if float(np.max(np.abs(rec))) >= 0.98:
        warns.append("ПЕРЕГРУЗКА ВХОДА АЦП во время свипа — результат недостоверен")
    out = {"kind": "sweep", "mode": mode, "iec": iec, "level_1k_dbfs": dsp.db20(a1),
           "f_lo": f_lo, "f_hi": f_hi, "dur": dur, "warnings": warns, "ch": {}}
    for c in (0, 1):
        r = dsp.analyze_sweep(rec[:, c], spec, f_lo=f_lo, f_hi=f_hi, nharm=nharm,
                              thd_hi=rig.cal["band_hi"])
        f = r["freq"]
        i1k = int(np.argmin(np.abs(f - 1000.0)))
        norm = r["fr_db"] - r["fr_db"][i1k]
        r["fr_norm_db"] = norm
        r["riaa_dev_db"] = norm - dsp.riaa_playback_db(f, iec)
        ci, co = rig.cal["in_fs_vrms"][c], rig.cal.out_full_scale_vrms(c)
        r["gain_1k_db"] = (r["fr_db"][i1k] + 20 * math.log10(ci / (co * 10 ** (rig.cal["att_db"] / 20)))
                           if ci and co else None)
        r["phase_deg"] = np.degrees(r["phase"])
        out["ch"][CH[c]] = r
    return out


def text_sweep(res):
    ref = "RIAA+IEC" if res["iec"] else "RIAA"
    L = [f"АЧХ и THD(f), свип {res['dur']:.0f} с, генератор: "
         + (f"inverse-{ref}" if res["mode"] == "riaa" else "плоский")
         + f", {res['level_1k_dbfs']:.1f} dBFS @1 кГц"]
    for c, r in res["ch"].items():
        if r["gain_1k_db"] is not None:
            L.append(f"[{c}] усиление на 1 кГц: {r['gain_1k_db']:+.2f} дБ")
    L.append(f"\nОтклонение от {ref}, дБ")
    L.append(f"{'Гц':>8s}{'L':>9s}{'R':>9s}{'L−R':>9s}  {'THD L %':>10s}{'THD R %':>10s}")
    f = res["ch"]["L"]["freq"]
    tf = res["ch"]["L"]["thd_freq"]
    for fq in (20, 30, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 15000, 20000):
        if fq < res["f_lo"] * 0.98 or fq > res["f_hi"] * 1.02:
            continue
        i = int(np.argmin(np.abs(f - fq)))
        j = int(np.argmin(np.abs(tf - fq)))
        dl, dr = res["ch"]["L"]["riaa_dev_db"][i], res["ch"]["R"]["riaa_dev_db"][i]
        tl, tr = res['ch']['L']['thd_pct'][j], res['ch']['R']['thd_pct'][j]
        fm = lambda v: f"{v:10.5f}" if np.isfinite(v) else f"{'—':>10s}"
        L.append(f"{fq:8d}{dl:9.2f}{dr:9.2f}{dl - dr:9.2f}  {fm(tl)}{fm(tr)}")
    d = np.array([[np.nanmax(np.abs(res['ch'][c]['riaa_dev_db'])) for c in CH]]).max()
    L.append(f"\nмакс. |отклонение| в полосе: {d:.2f} дБ")
    return "\n".join(L + [f"! {w}" for w in res["warnings"]])


# ----------------------------------------------------------------- сохранение


def to_jsonable(o):
    if isinstance(o, dict):
        return {str(k): to_jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [to_jsonable(v) for v in o]
    if isinstance(o, np.ndarray):
        return [None if not np.isfinite(v) else float(v) for v in o.ravel()] if o.ndim == 1 else o.tolist()
    if isinstance(o, (np.floating, float)):
        return None if not math.isfinite(float(o)) else float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    return o
