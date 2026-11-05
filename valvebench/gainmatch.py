"""Gain Match: узкополосное измерение уровня тона и разности усиления каналов.

Почему не RMS по 120 мс. Широкополосный RMS включает шум, сетевой фон и
гармоники лампового каскада, а окно в 120 мс не содержит целого числа периодов.
Здесь уровень тона на частоте генератора определяется методом наименьших
квадратов (cos, sin, const) по окну, кратному 20 мс (целое число периодов
сети): шум и фон не попадают в результат, а ΔG устойчива до тысячных долей дБ.
Модуль не зависит от GTK и проверяется автоматически.
"""
from __future__ import annotations

import math
import time
from collections import deque

import numpy as np

WINDOW_S = 0.5            # кратно 20 мс: целое число периодов 50 Гц (и 60 Гц)
RANGES = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0)


def tone_fit(x, fs, f):
    """(пиковая амплитуда, фаза рад, rms остатка) тона частоты f в x (LSQ cos/sin/const)."""
    x = np.asarray(x, dtype=np.float64)
    t = np.arange(x.size) / float(fs)
    w = 2.0 * math.pi * f
    a = np.column_stack((np.cos(w * t), np.sin(w * t), np.ones_like(t)))
    coef, *_ = np.linalg.lstsq(a, x, rcond=None)
    res = x - a @ coef
    amp = math.hypot(coef[0], coef[1])
    return amp, math.atan2(-coef[1], coef[0]), float(np.sqrt(np.mean(res * res)))


def to_db(a):
    return 20.0 * math.log10(max(a, 1e-12))


def pick_range(delta_db, tol_db):
    """Полуширина шкалы ΔG: минимальная из ряда, где стрелка остаётся в пределах 80 %."""
    need = max(abs(delta_db) / 0.8, 3.0 * tol_db)
    for r in RANGES:
        if r >= need:
            return r
    return RANGES[-1]


def classify(delta_db, tol_db):
    """'ok' внутри допуска, 'near' в пределах трёх допусков, иначе 'far'."""
    d = abs(delta_db)
    return "ok" if d <= tol_db else ("near" if d <= 3.0 * tol_db else "far")


class GainTracker:
    """Скользящая оценка ΔG = G(L) − G(R) по живому захвату."""

    def __init__(self, tau=1.5, hist_s=3.0):
        self.tau = tau
        self.hist_s = hist_s
        self.tare_db = 0.0
        self.reset()

    def reset(self):
        self.filt = None
        self.hist = deque()
        self.last_t = None
        self.last_raw = None

    def tare(self, value=None):
        """Принять текущую разность за ноль (петля или заведомо одинаковые каналы)."""
        if value is None:
            value = self.last_raw
        if value is not None:
            # "Принять текущее значение за ноль" задаёт новую опорную точку.
            # Нельзя накапливать поправку при повторном нажатии: это сдвинет
            # индикатор в противоположную сторону на уже учтённую ΔG.
            self.tare_db = float(value)
            self.reset()
        return self.tare_db

    def clear_tare(self):
        self.tare_db = 0.0
        self.reset()

    def update(self, ring, fs, f, gen_dbfs, route, cal=None, now=0.0):
        """ring — последний захват (N, 2). Возвращает словарь состояния для индикатора."""
        n = int(round(WINDOW_S * fs))
        if route != 0:
            self.reset()
            return {"valid": False, "msg": "ΔG: подай тест в оба канала (L + R)"}
        if ring is None or len(ring) < n:
            self.reset()
            return {"valid": False, "msg": "накопление данных…"}
        seg = ring[-n:]
        amps, phs, res = [], [], []
        for c in (0, 1):
            a, ph, r = tone_fit(seg[:, c], fs, f)
            amps.append(a); phs.append(ph); res.append(r)
        lv = [to_db(a / 1.0) for a in amps]               # dBFS пика синуса (0 dBFS = полный синус)
        if min(amps) < 1e-6:
            self.reset()
            return {"valid": False, "msg": "на входе нет тона: проверь кабели и частоту"}
        snr = [to_db(a / math.sqrt(2.0)) - to_db(max(r, 1e-12)) for a, r in zip(amps, res)]
        if min(snr) < 15.0:
            self.reset()
            return {"valid": False, "msg": f"недостаточный SNR для сравнения каналов ({min(snr):.1f} dB; нужно ≥15 dB)"}
        gains = None
        calibrated = False
        if cal is not None:
            try:
                g = [cal.gain_db(float(gen_dbfs), lv[c], c) for c in (0, 1)]
                if all(v is not None for v in g):
                    gains, calibrated = g, True
            except Exception:                              # старый профиль без нужных полей
                gains = None
        raw = (gains[0] - gains[1]) if calibrated else (lv[0] - lv[1])
        self.last_raw = raw
        delta = raw - self.tare_db
        # фильтр и разброс
        if self.filt is None or self.last_t is None:
            self.filt = delta
        else:
            dt = max(0.0, now - self.last_t)
            al = 1.0 - math.exp(-dt / self.tau)
            self.filt += al * (delta - self.filt)
        self.last_t = now
        self.hist.append((now, delta))
        while self.hist and now - self.hist[0][0] > self.hist_s:
            self.hist.popleft()
        vals = np.array([v for _, v in self.hist])
        sigma = float(np.std(vals)) if len(vals) > 5 else None
        dphi = math.degrees(((phs[0] - phs[1] + math.pi) % (2 * math.pi)) - math.pi)
        return {"valid": True, "delta": delta, "filt": self.filt, "sigma": sigma,
                "level_dbfs": lv, "snr_db": snr, "phase_deg": dphi,
                "gain_db": gains, "calibrated": calibrated, "tare_db": self.tare_db,
                "msg": ""}


SERIES_FREQUENCIES = (100.0, 1000.0, 10000.0)
MIN_SERIES_SNR_DB = 15.0


def measure_series(rig, frequencies=SERIES_FREQUENCIES, level_dbfs=-30.0, repeats=3, metadata=None):
    """Measure L/R gain at several frequencies in one coherent DAC→ADC run.

    Each frequency is repeated in every pass (100, 1k, 10k, 100, ...), which
    makes slow drift visible instead of measuring all repetitions of one
    frequency back-to-back.  Absolute G is reported only when both DAC and ADC
    channels have voltage calibration.  Without it, ``delta_output_db`` is
    explicitly an ADC-level difference, not a calibrated gain difference.
    """
    freqs = tuple(float(f) for f in frequencies)
    if not freqs or len(freqs) < 2:
        raise ValueError("для серии Gain Match нужны минимум две частоты")
    if any(not math.isfinite(f) or f <= 0 or f >= rig.fs / 2 for f in freqs):
        raise ValueError("частоты должны быть положительными и ниже Найквиста")
    if len({round(f, 9) for f in freqs}) != len(freqs):
        raise ValueError("частоты в серии не должны повторяться")
    level_dbfs = float(level_dbfs)
    if not math.isfinite(level_dbfs) or not -80.0 <= level_dbfs <= -0.5:
        raise ValueError("уровень генератора должен быть в диапазоне −80…−0.5 dBFS")
    repeats = int(repeats)
    if not 1 <= repeats <= 10:
        raise ValueError("число повторов должно быть от 1 до 10")

    tones = {}
    segments = []
    order = []
    for rep in range(repeats):
        for requested_f in freqs:
            k, amplitude = rig.amp_tone(requested_f, level_dbfs)
            actual_f = float(rig.f_of(k))
            tones[requested_f] = (k, actual_f)
            segments.append(([(k, amplitude)], [(k, amplitude)], None))
            order.append((requested_f, rep + 1, actual_f, k))

    windows, warnings = rig.run(segments)
    per_frequency = {f: {"L": [], "R": []} for f in freqs}
    for order_item, window in zip(order, windows):
        requested_f, rep, actual_f, _k = order_item
        for ch_index, channel in enumerate(("L", "R")):
            signal = np.asarray(window[:, ch_index], dtype=np.float64)
            # Reuse the production tone analyser: unlike a fixed-frequency LS
            # fit, it estimates the captured f0 and remains valid with small
            # independent DAC/ADC clock offsets.
            analysis = rig._tone_an(window, ch_index, tones[requested_f][0], nharm=3)
            measured_f = float(analysis["f0"])
            level = float(analysis["level_dbfs"])
            snr = max(0.0, -float(analysis["thdn_db"]))
            residual_dbfs = level - snr
            peak = float(np.max(np.abs(signal))) if signal.size else 0.0
            clipped = peak >= 0.98
            reasons = []
            if not math.isfinite(level) or level <= -120.0:
                reasons.append("tone_missing")
            if snr < MIN_SERIES_SNR_DB:
                reasons.append("low_snr")
            if clipped:
                reasons.append("adc_clip")

            gain = rig.cal.gain_db(level_dbfs, level, ch_index)
            input_v = rig.cal.dut_in_v(level_dbfs, ch_index)
            output_v = rig.cal.in_v(level, ch_index)
            per_frequency[requested_f][channel].append({
                "repeat": rep,
                "frequency_hz": measured_f,
                "level_dbfs": level,
                "residual_dbfs": residual_dbfs,
                "snr_db": snr,
                "phase_coherence": analysis.get("phase_coherence"),
                "peak_dbfs": to_db(peak),
                "clipped": clipped,
                "valid": not reasons,
                "validity_reasons": reasons,
                "gain_db": None if gain is None else float(gain),
                "input_vrms": input_v,
                "output_vrms": output_v,
            })

    rows = []
    for requested_f in freqs:
        channels = {}
        for channel in ("L", "R"):
            samples = per_frequency[requested_f][channel]
            levels = [q["level_dbfs"] for q in samples]
            gains = [q["gain_db"] for q in samples if q["gain_db"] is not None]
            snrs = [q["snr_db"] for q in samples]
            channels[channel] = {
                "level_dbfs": float(np.median(levels)),
                "level_min_dbfs": float(min(levels)),
                "level_max_dbfs": float(max(levels)),
                "level_spread_db": float(max(levels) - min(levels)),
                "residual_dbfs": float(np.median([q["residual_dbfs"] for q in samples])),
                "snr_db": float(np.median(snrs)),
                "gain_db": float(np.median(gains)) if len(gains) == repeats else None,
                "input_vrms": samples[0]["input_vrms"],
                "output_vrms": float(np.median([q["output_vrms"] for q in samples if q["output_vrms"] is not None]))
                               if any(q["output_vrms"] is not None for q in samples) else None,
                "valid": all(q["valid"] for q in samples),
                "validity_reasons": sorted({reason for q in samples for reason in q["validity_reasons"]}),
                "repeats": samples,
            }
        calibrated = channels["L"]["gain_db"] is not None and channels["R"]["gain_db"] is not None
        delta_output = channels["L"]["level_dbfs"] - channels["R"]["level_dbfs"]
        delta_gain = (channels["L"]["gain_db"] - channels["R"]["gain_db"]) if calibrated else None
        per_repeat_delta = [l["level_dbfs"] - r["level_dbfs"]
                            for l, r in zip(per_frequency[requested_f]["L"], per_frequency[requested_f]["R"])]
        rows.append({
            "requested_frequency_hz": requested_f,
            "frequency_hz": tones[requested_f][1],
            "channels": channels,
            "calibrated": calibrated,
            "delta_gain_db": delta_gain,
            "delta_output_db": float(delta_output),
            "delta_output_spread_db": float(max(per_repeat_delta) - min(per_repeat_delta)),
            "valid": channels["L"]["valid"] and channels["R"]["valid"],
        })

    backend = rig.be
    result = {
        "kind": "gain_match_series",
        "schema": 1,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "device": getattr(backend, "hw", getattr(backend, "name", type(backend).__name__)),
        "backend": getattr(backend, "name", type(backend).__name__),
        "fs": int(rig.fs),
        "generator_level_dbfs": level_dbfs,
        "frequencies_hz": [row["frequency_hz"] for row in rows],
        "repeats": repeats,
        "blocks": int(rig.blocks),
        "settle_seconds": float(rig.settle),
        "calibrated": all(row["calibrated"] for row in rows),
        "calibration": {
            "out_fs_vrms": list(rig.cal["out_fs_vrms"]),
            "in_fs_vrms": list(rig.cal["in_fs_vrms"]),
            "att_db": float(rig.cal["att_db"]),
            "method": "single-point voltage calibration",
            "frequency_dependent_loopback_correction": False,
        },
        "rows": rows,
        "metadata": dict(metadata or {}),
        "warnings": list(warnings),
    }
    return result


def text_series(result):
    """Compact human-readable summary for CLI/GUI."""
    calibrated = bool(result.get("calibrated"))
    lines = [f"GAIN MATCH · Fs {result['fs']} Гц · генератор {result['generator_level_dbfs']:.1f} dBFS · "
             f"{result['repeats']} повтора(ов)",
             f"{'Гц':>8s}  {'L dBFS':>9s}  {'R dBFS':>9s}  {'Δ ADC':>9s}  "
             f"{'G L':>9s}  {'G R':>9s}  {'ΔG':>9s}  статус"]
    for row in result["rows"]:
        left, right = row["channels"]["L"], row["channels"]["R"]
        gl = f"{left['gain_db']:+.3f}" if left["gain_db"] is not None else "—"
        gr = f"{right['gain_db']:+.3f}" if right["gain_db"] is not None else "—"
        dg = f"{row['delta_gain_db']:+.3f}" if row["delta_gain_db"] is not None else "—"
        status = "OK" if row["valid"] else "ПРОВЕРИТЬ"
        if not row["calibrated"]:
            status += " · не калибровано"
        lines.append(f"{row['frequency_hz']:8.1f}  {left['level_dbfs']:9.2f}  {right['level_dbfs']:9.2f}  "
                     f"{row['delta_output_db']:+9.3f}  {gl:>9s}  {gr:>9s}  {dg:>9s}  {status}")
        lines.append(f"          SNR L/R {left['snr_db']:.1f}/{right['snr_db']:.1f} dB · "
                     f"разброс ΔADC {row['delta_output_spread_db']:.3f} dB")
    if not calibrated:
        lines.append("G и ΔG недоступны без калибровки напряжения ЦАП L/R и АЦП L/R; ΔADC — только разность уровней на входах АЦП.")
    else:
        lines.append("Примечание: напряжения откалиброваны в одной точке; частотная коррекция петли ЦАП/АЦП не применена. На 100/10k результат включает АЧХ измерительной карты.")
    if result.get("metadata"):
        meta = result["metadata"]
        for key, title in (("dut", "Устройство"), ("tubes", "Лампы"), ("notes", "Примечание")):
            if meta.get(key):
                lines.append(f"{title}: {meta[key]}")
    if result.get("warnings"):
        lines.extend(f"! {warning}" for warning in result["warnings"])
    return "\n".join(lines)
