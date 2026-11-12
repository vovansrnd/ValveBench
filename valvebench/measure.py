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

CLOCK_MAX_PPM = 100.0
CLOCK_CHANNEL_SPREAD_PPM = 5.0  # L/R must agree on the one physical ADC clock
CLOCK_MIN_SNR_DB = 20.0  # carrier-local engineering floor, not an uncertainty guarantee


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

        Первый сегмент должен быть самым громким — по нему находим начало.
        Начало сигнала ищется узкополосно на частоте тестового тона (если она
        известна): одиночный щелчок/звон при открытии ЦАП не принимается за
        начало программы (0.1.19, регрессия B6)."""
        parts, meta = [], []
        for tl, tr, st in segs:
            sig, s_n = self._seg(tl, tr, self.settle if st is None else st)
            parts.append(sig)
            meta.append((len(sig), s_n))
        rec = self.be.play_record(np.concatenate(parts))
        tone_hz = None
        for k, a in list(segs[0][0]) + list(segs[0][1]):
            if a > 0:
                tone_hz = self.f_of(k)
                break
        d = self._onset(rec, tone_hz=tone_hz)
        # notes копируются ПОСЛЕ onset: предупреждение о широкополосном fallback
        # (узкополосный поиск не нашёл тон) не теряется
        warns = list(self.be.notes)
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

    def _onset(self, rec, tone_hz=None):
        if tone_hz is not None:
            try:
                return self._onset_narrowband(rec, tone_hz)
            except MeasureError:
                if float(np.max(np.abs(rec))) <= 0.0:
                    raise
                # узкополосный поиск не нашёл тон: щелчок не должен сработать,
                # поэтому широкополосный fallback помечается предупреждением
                self.be.notes = list(getattr(self.be, "notes", [])) + [
                    "onset: узкополосный поиск не нашёл тон — начало найдено "
                    "широкополосно; проверь частоту генератора"]
        return self._onset_broadband(rec)

    def _onset_broadband(self, rec):
        blk = int(0.005 * self.fs)
        m = len(rec) // blk
        env = np.sqrt(np.mean(rec[: m * blk].reshape(m, blk, 2) ** 2, axis=1)).max(axis=1)
        noise = max(float(np.median(env[: int(0.2 / 0.005)])), 1e-7)
        thr = 8.0 * noise
        for i in range(m - 4):
            if np.all(env[i:i + 4] > thr):
                return i * blk
        raise MeasureError("сигнал на входе не найден: проверь кабели, уровни и выбранные каналы")

    def _onset_narrowband(self, rec, tone_hz):
        """Начало программы по огибающей ТОНА (демодуляция на tone_hz).

        Одиночный щелчок открытия ЦАП, даже на частоте тона, даёт короткий
        подъём огибающей; настоящий тон длится секунды. Требуем устойчивого
        превышения порога до конца записи (не менее 1 с) — иначе это не начало
        тона (регрессия B6)."""
        n = len(rec)
        blk = int(0.005 * self.fs)
        m = n // blk
        if m < 8:
            raise MeasureError("запись слишком коротка для поиска начала тона")
        t = np.arange(n) / self.fs
        demod = np.exp(-2j * np.pi * tone_hz * t)
        win = max(8, int(round(5 * self.fs / tone_hz)))   # 5 периодов тона
        env = np.zeros(n)
        for c in (0, 1):
            z = rec[:, c] * demod
            cs = np.concatenate(([0j], np.cumsum(z)))
            idx = np.arange(n)
            lo = np.clip(idx - win // 2, 0, n)
            hi = np.clip(idx - win // 2 + win, 0, n)
            amp = 2.0 * np.abs((cs[hi] - cs[lo]) / np.maximum(hi - lo, 1))
            env = np.maximum(env, amp)
        blocks = env[: m * blk].reshape(m, blk).max(axis=1)
        noise = max(float(np.median(blocks[: int(0.2 / 0.005)])), 1e-7)
        # Робастный пик: одиночный щелчок открытия (±0.5 FS на одном сэмпле,
        # 0.14 % блоков на DLX при холодном старте; найдено приёмкой
        # 0.1.21-dev4, отказ воспроизведён 60-с циклами) через 0.1*max
        # поднимал порог выше огибающей тона и отвергал настоящий тон
        # при отношении тон/шум 190x. 99-й перцентиль исключает редкие
        # выбросы; sustain-критерий ниже по-прежнему отбрасывает щелчки.
        peak = float(np.percentile(blocks, 99))
        thr = max(8.0 * noise, 0.1 * peak)
        # Устойчивость: за 1 с от кандидата огибающая выше порога ≥90 % времени.
        # Щелчок (50 мс подъёма) даёт ~5 % — отклоняется; настоящий тон с
        # короткими провалами (дефекты захвата) — ≥90 % — принимается на истинном
        # старте. Критерий «держится до конца записи» не годится: он смещал бы
        # начало за последний провал (регрессия B4).
        above = blocks > thr
        sustain = max(4, min(int(1.0 / 0.005), m))
        cum = np.concatenate(([0], np.cumsum(above.astype(np.int64))))
        for i in range(m - 4):
            end = min(m, i + sustain)
            span = end - i
            if above[i] and (cum[end] - cum[i]) >= 0.9 * span:
                return i * blk
        raise MeasureError(
            "начало тона не найдено узкополосно: проверь кабели, уровни и частоту "
            f"генератора ({tone_hz:.0f} Гц)")

    # ---- переиспользуемое
    def _levels_info(self, gen_dbfs, rec_dbfs, ch):
        return {"dut_in_v": self.cal.dut_in_v(gen_dbfs, ch), "out_v": self.cal.in_v(rec_dbfs, ch),
                "gain_db": self.cal.gain_db(gen_dbfs, rec_dbfs, ch)}

    def _tone_an(self, win, ch, k, nharm=9, coherent_line_floors=None):
        return dsp.analyze_tone(win[:, ch], self.fs, self.f_of(k), self.n, nharm=nharm,
                                band=(20.0, self.cal["band_hi"]), mains=self.cal["mains"],
                                coherent_line_floors=coherent_line_floors)


# ------------------------------------------------------------------ проверка такта


def clock_check(rig, level_dbfs=-72.0):
    """Measure relative DAC/ADC clock error and report three independent states.

    clock_ok: both channel estimates are within the ppm gate and agree with each other.
    signal_ok: both channels contain a usable tone, with adequate SNR and no
               transport/ADC-overload warning.
    coherent: the measured tone lies within COH_TOL_BINS of the analysis grid.

    A non-coherent result is not automatically a clock failure: drift-aware
    analyzers may still use it. Algorithms that require a coherent timebase
    must check the coherent flag separately.
    """
    level_dbfs = float(level_dbfs)
    if not math.isfinite(level_dbfs) or not -120.0 <= level_dbfs <= -24.0:
        raise MeasureError("уровень проверки такта должен быть от -120 до -24 dBFS")
    k, a = rig.amp_tone(1000.0, level_dbfs)
    wins, warns = rig.run([([(k, a)], [(k, a)], 0.4)])
    out = {"kind": "clock", "test_level_dbfs": level_dbfs, "warnings": warns, "ch": {}}
    per_channel_clock, per_channel_signal, per_channel_coherent, per_channel_ppm = [], [], [], []
    for c in (0, 1):
        r = rig._tone_an(wins[0], c, k, nharm=2)
        f0 = float(r.get("f0", float("nan")))
        ppm = (f0 / rig.f_of(k) - 1.0) * 1e6 if math.isfinite(f0) else float("nan")
        level = float(r.get("level_dbfs", float("nan")))
        noise = float(r.get("noise_dbfs", float("nan")))
        snr = float(r.get("local_snr_db", float("nan")))
        local_noise = float(r.get("local_noise_dbfs", float("nan")))
        coh_err = float(r.get("coh_err_bins", float("nan")))
        clock_ok = math.isfinite(ppm) and abs(ppm) <= CLOCK_MAX_PPM
        signal_ok = math.isfinite(snr) and snr >= CLOCK_MIN_SNR_DB
        coherent = math.isfinite(coh_err) and abs(coh_err) <= dsp.COH_TOL_BINS
        per_channel_clock.append(clock_ok)
        per_channel_signal.append(signal_ok)
        per_channel_coherent.append(coherent)
        per_channel_ppm.append(ppm)
        out["ch"][CH[c]] = {
            "f0": f0, "ppm": ppm, "level_dbfs": level, "noise_dbfs": noise,
            "snr_db": snr, "local_noise_dbfs": local_noise, "coh_err_bins": coh_err,
            "clock_ok": clock_ok, "signal_ok": signal_ok, "coherent": coherent,
        }
        for detail in r.get("warnings", []):
            warns.append(f"{CH[c]}: {detail}")
        if not clock_ok:
            warns.append(
                f"{CH[c]}: относительное расхождение тактов {ppm:+.1f} ppm "
                f"превышает допуск ±{CLOCK_MAX_PPM:.0f} ppm"
            )
        if not signal_ok:
            warns.append(
                f"{CH[c]}: недостаточный локальный SNR около несущей для проверки такта "
                f"({snr:.1f} dB; требуется не менее {CLOCK_MIN_SNR_DB:.0f} dB)"
            )
        if not coherent:
            warns.append(
                f"{CH[c]}: тон некогерентен окну "
                f"(сдвиг {coh_err:+.4f} бина; допуск ±{dsp.COH_TOL_BINS:.3f})"
            )

    transport_bad = any(
        token in str(w).upper()
        for w in warns
        for token in ("XRUN", "UNDERRUN", "OVERRUN", "CLIP", "OVERLOAD", "ПЕРЕГРУЗ")
    )
    if transport_bad:
        warns.append("проверка такта недействительна: XRUN или перегрузка АЦП")
    finite_ppm = [v for v in per_channel_ppm if math.isfinite(v)]
    channel_spread = (max(finite_ppm) - min(finite_ppm)) if len(finite_ppm) == 2 else float("inf")
    clock_consistent = channel_spread <= CLOCK_CHANNEL_SPREAD_PPM
    if not clock_consistent:
        warns.append(
            f"оценки такта L/R расходятся на {channel_spread:.1f} ppm "
            f"(допуск ±{CLOCK_CHANNEL_SPREAD_PPM:.1f} ppm); общая частота АЦП не подтверждена"
        )
    clock_ppm_ok = all(per_channel_clock)
    out["clock_channel_spread_ppm"] = channel_spread
    out["clock_ppm_ok"] = clock_ppm_ok
    out["clock_consistent"] = clock_consistent
    out["clock_ok"] = clock_ppm_ok and clock_consistent
    out["signal_ok"] = all(per_channel_signal) and not transport_bad
    out["coherent"] = all(per_channel_coherent)
    # The overall gate includes ppm tolerance, agreement between channels and signal validity.
    # Coherence is deliberately independent and must be checked by each analyzer.
    out["ok"] = out["clock_ok"] and out["signal_ok"]
    return out


def text_clock(r):
    lines = [f"Проверка такта (1 кГц, {r.get('test_level_dbfs', -72.0):.1f} dBFS):"]
    for c, v in r["ch"].items():
        state = "когерентен" if v.get("coherent") else "некогерентен"
        lines.append(
            f"  {c}: {v['f0']:.3f} Гц  ({v['ppm']:+.1f} ppm)  "
            f"уровень {v['level_dbfs']:.1f} dBFS  локальный SNR {v.get('snr_db', float('nan')):.1f} dB  "
            f"· {state} ({v.get('coh_err_bins', float('nan')):+.4f} бина)"
        )
    lines.append(
        f"  Допуск расхождения ±{CLOCK_MAX_PPM:.0f} ppm: "
        + ("PASS" if r.get("clock_ppm_ok", r.get("clock_ok", r.get("ok", False))) else "FAIL")
    )
    lines.append(
        f"  Согласие каналов L/R (разброс ≤ {CLOCK_CHANNEL_SPREAD_PPM:.1f} ppm): "
        + ("PASS" if r.get("clock_consistent", True) else "FAIL")
        + f" ({r.get('clock_channel_spread_ppm', 0.0):.1f} ppm)"
    )
    lines.append(
        f"  Достоверность сигнала (локальный SNR ≥ {CLOCK_MIN_SNR_DB:.0f} dB, без XRUN/CLIP): "
        + ("PASS" if r.get("signal_ok", r.get("ok", False)) else "FAIL")
    )
    lines.append(
        f"  Когерентность окна (±{dsp.COH_TOL_BINS:.3f} бина): "
        + ("PASS" if r.get("coherent", False) else "не выполнена")
    )
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
                   iec=False, seed=12345, response_calibration=None):
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
    if response_calibration is not None:
        apply_response_calibration(out, response_calibration)
    return out


def text_noise_response(res):
    title = {"white": "белый шум", "pink": "розовый шум", "riaa": "шум с inverse-RIAA"}.get(res["mode"], res["mode"])
    sig = res["signal"]
    view = int(res.get("display_view", 1))
    if view == 0:
        value_key, heading = "riaa_dev_db", "Отклонение от RIAA, дБ"
    elif view == 2:
        value_key, heading = "coherence", "Когерентность"
    else:
        value_key, heading = "fr_norm_db", "АЧХ, нормированная на 1 кГц, дБ"
    lines = [f"Шумовая АЧХ / H1: {title}; {res['seconds']:.1f} с; "
             f"запрошено {sig.get('requested_rms_dbfs', res['level_dbfs']):.1f} dBFS RMS, "
             f"фактически {sig['actual_rms_dbfs']:.1f} dBFS RMS; пик {sig['peak_dbfs']:.1f} dBFS",
             f"Разрешение Welch: {res['ch']['L']['freq'][1] - res['ch']['L']['freq'][0]:.3f} Гц; сегмент {res['ch']['L']['nperseg']} отсчётов",
             heading,
             f"{'Гц':>8s} | {'L':>9s} | {'R':>9s} | {'coh L/R':>9s}"]
    f = res["ch"]["L"]["freq"]
    rows = []
    for fq in (20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 315, 500, 1000, 2000,
               3150, 5000, 8000, 10000, 12500, 16000, 20000):
        if fq < f[0] or fq > f[-1]:
            continue
        i = int(np.argmin(np.abs(f - fq)))
        l, r = res["ch"]["L"], res["ch"]["R"]
        rows.append(f"{f[i]:8.1f} | {l[value_key][i]:+9.2f} | {r[value_key][i]:+9.2f} | "
                    f"{l['coherence'][i]:.2f}/{r['coherence'][i]:.2f}")
    lines.extend(rows)
    lines += ["", "Калибровка тракта: " + str(res.get("calibration_note", "не применялась")),
              f"Совмещение сигнала: {res['alignment_samples']} отсчётов"]
    if sig["peak_limited"]:
        lines.append("! RMS тестового шума снижен для удержания пика ниже −3 dBFS")
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
          nharm=5, response_calibration=None):
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
    out = {"kind": "sweep", "fs": int(rig.fs), "mode": mode, "iec": iec, "level_1k_dbfs": dsp.db20(a1),
           "f_lo": f_lo, "f_hi": f_hi, "dur": dur, "warnings": warns, "ch": {}}
    for c in (0, 1):
        r = dsp.analyze_sweep(rec[:, c], spec, f_lo=f_lo, f_hi=f_hi, nharm=nharm,
                              thd_hi=rig.cal["band_hi"])
        f = r["freq"]
        i1k = int(np.argmin(np.abs(f - 1000.0)))
        norm = r["fr_db"] - r["fr_db"][i1k]
        norm[np.abs(norm) < 1e-9] = 0.0
        r["fr_norm_db"] = norm
        r["riaa_curve_db"] = dsp.riaa_playback_db(f, iec)
        riaa_dev = norm - r["riaa_curve_db"]
        riaa_dev[np.abs(riaa_dev) < 1e-9] = 0.0
        r["riaa_dev_db"] = riaa_dev
        ci, co = rig.cal["in_fs_vrms"][c], rig.cal.out_full_scale_vrms(c)
        r["gain_1k_db"] = (r["fr_db"][i1k] + 20 * math.log10(ci / (co * 10 ** (rig.cal["att_db"] / 20)))
                           if ci and co else None)
        r["phase_deg"] = np.degrees(r["phase"])
        out["ch"][CH[c]] = r
    if response_calibration is not None:
        apply_response_calibration(out, response_calibration)
    return out


def build_response_calibration(result, device_key, mixer_fingerprint=None, fs=None):
    """Build a per-card/per-rate FR correction from a flat direct DAC→ADC sweep."""
    if result.get("kind") != "sweep" or result.get("mode") != "flat":
        raise MeasureError("калибровка АЧХ карты требует плоского свипа прямой петли")
    channels = {}
    for name in CH:
        r = result.get("ch", {}).get(name)
        if not r:
            raise MeasureError(f"нет данных канала {name} в калибровочной петле")
        freq = np.asarray(r.get("freq", []), dtype=float)
        fr = np.asarray(r.get("fr_norm_db", []), dtype=float)
        phase = np.asarray(r.get("phase_deg", []), dtype=float)
        absolute = np.asarray(r.get("fr_db", []), dtype=float)
        if freq.size < 50 or fr.size != freq.size or phase.size != freq.size or absolute.size != freq.size:
            raise MeasureError(f"неполная калибровочная кривая {name}")
        valid = np.isfinite(freq) & np.isfinite(fr) & np.isfinite(phase) & np.isfinite(absolute) & (freq > 0)
        if np.count_nonzero(valid) < 50:
            raise MeasureError(f"недостаточно достоверных точек калибровки {name}")
        # A disconnected loop can still produce a mathematically shaped curve
        # from noise. The calibration is explicitly a direct DAC→ADC cable loop,
        # so a transfer below -35 dB at 1 kHz is not accepted as a real path.
        # This leaves ample margin for card gain differences while rejecting
        # open/weak/noise-only captures that can otherwise look smooth.
        level_1k = float(np.interp(1000.0, freq[valid], absolute[valid]))
        if not (-35.0 <= level_1k <= 20.0):
            raise MeasureError(
                f"{name}: петля не подтверждена — передача на 1 кГц {level_1k:.1f} дБ; "
                "проверь L→L/R→R и повтори калибровку"
            )
        channels[name] = {
            "frequency_hz": freq[valid].tolist(),
            "fr_norm_db": fr[valid].tolist(),
            "phase_deg": phase[valid].tolist(),
        }
    if any("ПЕРЕГРУЗКА" in str(w).upper() for w in result.get("warnings", [])):
        raise MeasureError("петля перегрузила АЦП; профиль АЧХ не сохранён")
    return {
        "kind": "frequency_response_calibration", "schema": 1,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "device_key": str(device_key), "fs": int(fs or result.get("fs", 0) or 0),
        "f_lo": float(result["f_lo"]), "f_hi": float(result["f_hi"]),
        "mixer_fingerprint": mixer_fingerprint, "channels": channels,
        "source": "flat DAC→ADC loopback sweep; curves normalized at 1 kHz",
    }


def apply_response_calibration(result, profile):
    """Remove the measured per-channel DAC/ADC loop response from FR/phase/THD(f)."""
    if not isinstance(profile, dict) or profile.get("kind") != "frequency_response_calibration":
        raise MeasureError("неверный профиль частотной калибровки")
    warnings = result.setdefault("warnings", [])
    for name in CH:
        r = result.get("ch", {}).get(name)
        p = profile.get("channels", {}).get(name)
        if not r or not p:
            continue
        pf = np.asarray(p.get("frequency_hz", []), dtype=float)
        pfr = np.asarray(p.get("fr_norm_db", []), dtype=float)
        pph = np.asarray(p.get("phase_deg", []), dtype=float)
        f = np.asarray(r.get("freq", []), dtype=float)
        if pf.size < 2 or pfr.size != pf.size or f.size == 0:
            warnings.append(f"{name}: профиль АЧХ неполон; канал показан без коррекции")
            continue
        # Interpolate on log-frequency; never extrapolate beyond the calibrated band.
        pf_log = np.log10(pf)
        valid = (f >= pf[0]) & (f <= pf[-1]) & (f > 0)
        if np.count_nonzero(valid) < 2:
            warnings.append(f"{name}: частоты вне диапазона калибровки {pf[0]:.0f}–{pf[-1]:.0f} Гц")
            continue
        cal_db = np.interp(np.log10(f[valid]), pf_log, pfr)
        cal_1k = float(np.interp(3.0, pf_log, pfr))
        correction = cal_db - cal_1k
        fr = np.asarray(r.get("fr_norm_db", []), dtype=float).copy()
        fr[valid] -= correction
        # Set exact numerical zero at the normalization anchor, avoiding tiny residues such as 7e-16.
        i1k = int(np.argmin(np.abs(f - 1000.0)))
        if f.size and abs(f[i1k] - 1000.0) <= max(2.0, 0.01 * 1000.0):
            fr -= fr[i1k]
        fr[np.abs(fr) < 1e-9] = 0.0
        r["fr_norm_db"] = fr
        if "riaa_dev_db" in r:
            target_curve = np.asarray(
                r.get("riaa_curve_db", dsp.riaa_playback_db(f, bool(result.get("iec", False)))),
                dtype=float)
            dev = fr - target_curve
            dev[np.abs(dev) < 1e-9] = 0.0
            r["riaa_dev_db"] = dev
        # All sweep and H1 curves carry the target curve separately when available.
        if "phase_deg" in r and pph.size == pf.size:
            phase = np.asarray(r["phase_deg"], dtype=float).copy()
            pphase = np.interp(np.log10(f[valid]), pf_log, pph)
            phase[valid] -= pphase
            i1k = int(np.argmin(np.abs(f - 1000.0)))
            phase -= phase[i1k]
            r["phase_deg"] = phase
        # Correct harmonic response ratios by calibration response at f and k·f.
        h_db = r.get("h_db")
        if isinstance(h_db, dict) and h_db:
            powers = []
            for harmonic, values in h_db.items():
                try:
                    order = int(harmonic)
                except (TypeError, ValueError):
                    continue
                values = np.asarray(values, dtype=float).copy()
                thd_f = np.asarray(r.get("thd_freq", []), dtype=float)
                harm_f = thd_f * order
                if values.size != thd_f.size:
                    h_db[harmonic] = values
                    continue
                mask = (thd_f >= pf[0]) & (thd_f <= pf[-1]) & (harm_f >= pf[0]) & (harm_f <= pf[-1]) & (thd_f > 0)
                if np.any(mask):
                    base_c = np.interp(np.log10(thd_f[mask]), pf_log, pfr)
                    harm_c = np.interp(np.log10(harm_f[mask]), pf_log, pfr)
                    values[mask] += base_c - harm_c
                h_db[harmonic] = values
                powers.append(10.0 ** (values / 10.0))
            if powers:
                total_power = np.nansum(np.stack(powers), axis=0)
                r["thd_db"] = 10.0 * np.log10(np.maximum(total_power, 1e-30))
                r["thd_pct"] = 100.0 * np.sqrt(total_power)
        r["response_calibration_applied"] = True
    result["response_calibration"] = {
        "applied": True, "device_key": profile.get("device_key"),
        "fs": profile.get("fs"), "created": profile.get("created"),
        "range_hz": [profile.get("f_lo"), profile.get("f_hi")],
    }
    result["calibration_note"] = "АЧХ/фаза скорректированы по сохранённой петле этой карты; вне полосы профиля коррекция не экстраполируется."
    return result


def text_sweep(res):
    ref = "RIAA+IEC" if res["iec"] else "RIAA"
    is_riaa = res["mode"] == "riaa"
    L = [f"АЧХ и THD(f), свип {res['dur']:.0f} с, генератор: "
         + (f"inverse-{ref}" if is_riaa else "плоский")
         + f", {res['level_1k_dbfs']:.1f} dBFS @1 кГц"]
    if res.get("calibration_note"):
        L.append("Калибровка тракта: " + str(res["calibration_note"]))
    elif res.get("response_calibration", {}).get("applied"):
        L.append("Калибровка тракта: коррекция АЧХ/фазы применена")
    else:
        L.append("Калибровка тракта: не применена — показано измерение ЦАП + DUT + АЦП")
    for c, r in res["ch"].items():
        if r["gain_1k_db"] is not None:
            L.append(f"[{c}] усиление на 1 кГц: {r['gain_1k_db']:+.2f} дБ")
    if is_riaa:
        values_key, heading = "riaa_dev_db", f"Отклонение от {ref}, дБ"
    else:
        values_key, heading = "fr_norm_db", "АЧХ, нормированная на 1 кГц, дБ"
    L.append("\n" + heading)
    L.append(f"{'Гц':>8s}{'L':>9s}{'R':>9s}{'L−R':>9s}  {'THD L %':>10s}{'THD R %':>10s}")
    f = res["ch"]["L"]["freq"]
    tf = res["ch"]["L"]["thd_freq"]
    for fq in (20, 30, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 15000, 20000):
        if fq < res["f_lo"] * 0.98 or fq > res["f_hi"] * 1.02:
            continue
        i = int(np.argmin(np.abs(f - fq)))
        j = int(np.argmin(np.abs(tf - fq)))
        dl, dr = res["ch"]["L"][values_key][i], res["ch"]["R"][values_key][i]
        tl, tr = res['ch']['L']['thd_pct'][j], res['ch']['R']['thd_pct'][j]
        fm = lambda v: (f"{0.0:10.5f}" if abs(float(v)) < 1e-9 else f"{v:10.5f}") if np.isfinite(v) else f"{'—':>10s}"
        L.append(f"{fq:8d}{dl:9.2f}{dr:9.2f}{dl - dr:9.2f}  {fm(tl)}{fm(tr)}")
    if is_riaa:
        d = max(float(np.nanmax(np.abs(res['ch'][c]['riaa_dev_db']))) for c in CH)
        L.append(f"\nмакс. |отклонение от {ref}| в полосе: {d:.2f} дБ")
    else:
        delta = np.asarray(res['ch']['L']['fr_norm_db']) - np.asarray(res['ch']['R']['fr_norm_db'])
        L.append(f"\nмакс. |разница L−R| в полосе: {float(np.nanmax(np.abs(delta))):.2f} дБ")
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
