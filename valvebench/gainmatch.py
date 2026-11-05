"""Gain Match: узкополосное измерение уровня тона и разности усиления каналов.

Почему не RMS по 120 мс. Широкополосный RMS включает шум, сетевой фон и
гармоники лампового каскада, а окно в 120 мс не содержит целого числа периодов.
Здесь уровень тона на частоте генератора определяется методом наименьших
квадратов (cos, sin, const) по окну, кратному 20 мс (целое число периодов
сети): шум и фон не попадают в результат, а ΔG устойчива до тысячных долей дБ.
Модуль не зависит от GTK и проверяется автоматически.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
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
MIN_SERIES_SNR_DB = 25.0
DEFAULT_MATCH_TOL_DB = 0.20


def device_key_from_hw(hw):
    """Stable-ish ALSA identity for a local loopback reference (not card index alone)."""
    value = str(hw or "sim")
    if value == "sim":
        return "sim"
    match = re.fullmatch(r"hw:(\d+)(?:,(\d+))?", value)
    if not match:
        return value
    card, pcm = int(match.group(1)), int(match.group(2) or 0)
    try:
        card_id = open(f"/proc/asound/card{card}/id", encoding="utf-8").read().strip()
    except OSError:
        card_id = f"card{card}"
    return f"alsa:{card_id}:pcm{pcm}"


def _device_description(backend):
    hw = getattr(backend, "hw", None)
    if hw:
        return str(hw)
    return getattr(backend, "name", type(backend).__name__)


def mixer_fingerprint_from_hw(hw):
    """Hash ALSA controls so a loopback reference cannot survive mixer changes."""
    match = re.fullmatch(r"hw:(\d+)(?:,\d+)?", str(hw or ""))
    if not match:
        return None
    try:
        proc = subprocess.run(["amixer", "-c", match.group(1), "contents"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=3.0, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return hashlib.sha256(proc.stdout).hexdigest()


def _reference_map(reference):
    return {round(float(row["frequency_hz"]), 6): row for row in reference.get("rows", [])}


def validate_loopback_reference(rig, reference, level_dbfs, frequencies=SERIES_FREQUENCIES):
    """Reject stale or mismatched reference rather than silently applying it."""
    if not isinstance(reference, dict) or reference.get("kind") != "gain_match_loopback_reference":
        raise ValueError("нет эталона петли карты: сначала выполни шаг 1")
    backend = rig.be
    key = device_key_from_hw(getattr(backend, "hw", "sim"))
    if reference.get("device_key") != key:
        raise ValueError("эталон петли создан для другой звуковой карты; повтори шаг 1")
    expected_mixer = reference.get("mixer_fingerprint")
    if expected_mixer:
        current_mixer = mixer_fingerprint_from_hw(getattr(backend, "hw", None))
        if current_mixer is None:
            raise ValueError("не удалось проверить настройки ALSA-микшера; повтори шаг 1")
        if current_mixer != expected_mixer:
            raise ValueError("настройки микшера звуковой карты изменились после петли; повтори шаг 1")
    if int(reference.get("fs", -1)) != int(rig.fs):
        raise ValueError("частота дискретизации изменилась; повтори шаг 1")
    if abs(float(reference.get("generator_level_dbfs", 999.0)) - float(level_dbfs)) > 1e-6:
        raise ValueError("уровень генератора изменился; для точной компенсации повтори шаг 1")
    rows = _reference_map(reference)
    wanted = {round(float(f), 6) for f in frequencies}
    if set(rows) != wanted:
        raise ValueError("набор частот эталона не совпадает с измерением; повтори шаг 1")
    bad = [f for f, row in rows.items() if not row.get("valid", False)]
    if bad:
        raise ValueError("эталон петли содержит невалидные измерения; повтори шаг 1")
    return rows


def build_loopback_reference(result):
    """Create a frequency-dependent L/R correction from a direct DAC→ADC loop."""
    if result.get("purpose") != "loopback":
        raise ValueError("эталон можно создать только из измерения прямой петли ЦАП→АЦП")
    rows = []
    for row in result.get("rows", []):
        if not row.get("valid"):
            raise ValueError(f"петля на {row['frequency_hz']:.0f} Гц измерена ненадёжно")
        if not row.get("repeat_stable", False):
            raise ValueError(f"петля на {row['frequency_hz']:.0f} Гц нестабильна; проверь соединения и повтори")
        rows.append({
            "frequency_hz": float(row["frequency_hz"]),
            "delta_adc_db": float(row["delta_adc_db"]),
            "repeat_spread_db": float(row["delta_output_spread_db"]),
            "snr_l_db": float(row["channels"]["L"]["snr_db"]),
            "snr_r_db": float(row["channels"]["R"]["snr_db"]),
            "valid": True,
        })
    if len(rows) != len(result.get("frequencies_hz", [])) or not rows:
        raise ValueError("эталон петли неполон")
    return {
        "kind": "gain_match_loopback_reference",
        "schema": 1,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "device_key": result["device_key"],
        "device": result["device"],
        "backend": result["backend"],
        "mixer_fingerprint": result.get("mixer_fingerprint"),
        "fs": int(result["fs"]),
        "generator_level_dbfs": float(result["generator_level_dbfs"]),
        "frequencies_hz": list(result["frequencies_hz"]),
        "rows": rows,
        "instruction": "Direct DAC L→ADC L and DAC R→ADC R loop; repeat after mixer, cable, device or rate changes.",
    }


def measure_series(rig, frequencies=SERIES_FREQUENCIES, level_dbfs=-30.0, repeats=3,
                   metadata=None, purpose="loopback", reference=None):
    """Measure three tones for either loopback correction or DUT channel matching.

    Step 1 (purpose='loopback'): connect DAC L→ADC L and R→R directly. The measured
    per-frequency ADC L−R difference becomes the interface correction.
    Step 2 (purpose='dut'): insert the DUT and subtract that correction. The result
    is relative DUT mismatch ΔG(L−R), and does not require absolute volt calibration.
    """
    freqs = tuple(float(f) for f in frequencies)
    if not freqs or len(freqs) < 2:
        raise ValueError("для серии Gain Match нужны минимум две частоты")
    if any(not math.isfinite(f) or f <= 0 or f >= rig.fs / 2 for f in freqs):
        raise ValueError("частоты должны быть положительными и ниже Найквиста")
    if len({round(f, 9) for f in freqs}) != len(freqs):
        raise ValueError("частоты в серии не должны повторяться")
    if purpose not in ("loopback", "dut"):
        raise ValueError("purpose должен быть loopback или dut")
    level_dbfs = float(level_dbfs)
    if not math.isfinite(level_dbfs) or not -80.0 <= level_dbfs <= -0.5:
        raise ValueError("уровень генератора должен быть в диапазоне −80…−0.5 dBFS")
    repeats = int(repeats)
    if not 1 <= repeats <= 10:
        raise ValueError("число повторов должно быть от 1 до 10")

    backend = rig.be
    hw = getattr(backend, "hw", "sim")
    device_key = device_key_from_hw(hw)
    mixer_fingerprint = mixer_fingerprint_from_hw(hw)

    refs = None
    if purpose == "dut":
        if reference is None:
            raise ValueError("нет эталона петли карты: сначала выполни шаг 1")
        refs = validate_loopback_reference(rig, reference, level_dbfs, freqs)
    elif reference is not None:
        raise ValueError("при измерении петли эталон не нужен")

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
        requested_f, rep, actual_f, k = order_item
        for ch_index, channel in enumerate(("L", "R")):
            signal = np.asarray(window[:, ch_index], dtype=np.float64)
            analysis = rig._tone_an(window, ch_index, k, nharm=3)
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
            absolute_gain = rig.cal.gain_db(level_dbfs, level, ch_index)
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
                "absolute_gain_db": None if absolute_gain is None else float(absolute_gain),
                "input_vrms": input_v,
                "output_vrms": output_v,
            })

    rows = []
    for requested_f in freqs:
        channels = {}
        for channel in ("L", "R"):
            samples = per_frequency[requested_f][channel]
            levels = [q["level_dbfs"] for q in samples]
            snrs = [q["snr_db"] for q in samples]
            channels[channel] = {
                "level_dbfs": float(np.median(levels)),
                "level_spread_db": float(max(levels) - min(levels)),
                "residual_dbfs": float(np.median([q["residual_dbfs"] for q in samples])),
                "snr_db": float(np.median(snrs)),
                "absolute_gain_db": (float(np.median([q["absolute_gain_db"] for q in samples]))
                                     if all(q["absolute_gain_db"] is not None for q in samples) else None),
                "valid": all(q["valid"] for q in samples),
                "validity_reasons": sorted({reason for q in samples for reason in q["validity_reasons"]}),
                "repeats": samples,
            }
        delta_adc = channels["L"]["level_dbfs"] - channels["R"]["level_dbfs"]
        per_repeat_delta = [l["level_dbfs"] - r["level_dbfs"]
                            for l, r in zip(per_frequency[requested_f]["L"], per_frequency[requested_f]["R"])]
        spread = float(max(per_repeat_delta) - min(per_repeat_delta))
        ref_row = refs.get(round(requested_f, 6)) if refs else None
        correction = float(ref_row["delta_adc_db"]) if ref_row else None
        corrected = (delta_adc - correction) if purpose == "dut" else None
        rows.append({
            "requested_frequency_hz": requested_f,
            "frequency_hz": tones[requested_f][1],
            "channels": channels,
            "delta_adc_db": float(delta_adc),
            "loopback_correction_db": correction,
            "delta_gain_db": None if corrected is None else float(corrected),
            "delta_output_spread_db": spread,
            "repeat_stable": spread <= 0.10,
            "valid": channels["L"]["valid"] and channels["R"]["valid"],
        })

    result = {
        "kind": "gain_match_series",
        "schema": 2,
        "purpose": purpose,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "device": _device_description(backend),
        "device_key": device_key,
        "mixer_fingerprint": mixer_fingerprint,
        "backend": getattr(backend, "name", type(backend).__name__),
        "fs": int(rig.fs),
        "generator_level_dbfs": level_dbfs,
        "frequencies_hz": [row["frequency_hz"] for row in rows],
        "repeats": repeats,
        "blocks": int(rig.blocks),
        "settle_seconds": float(rig.settle),
        "absolute_voltage_calibrated": all(
            row["channels"]["L"]["absolute_gain_db"] is not None
            and row["channels"]["R"]["absolute_gain_db"] is not None for row in rows),
        "loopback_corrected": purpose == "dut",
        "reference_created": reference.get("created") if reference else None,
        "rows": rows,
        "metadata": dict(metadata or {}),
        "warnings": list(warnings),
    }
    return result


def text_series(result, tolerance_db=DEFAULT_MATCH_TOL_DB):
    """Short user-facing result; hides raw dBFS internals unless measurement failed."""
    rows = result.get("rows", [])
    if result.get("purpose") == "loopback":
        lines = ["ШАГ 1 ИЗ 2 · ЭТАЛОН ЗВУКОВОЙ КАРТЫ",
                 "Петля L→L и R→R проверена. Измерена собственная разница каналов карты:"]
        for row in rows:
            lines.append(f"{row['frequency_hz']:g} Гц: поправка {row['delta_adc_db']:+.3f} дБ")
        lines += ["Эталон сохранён. Теперь подключи проверяемый усилитель, не меняя карту, "
                  "уровень генератора и аттенюаторы."]
        if result.get("backend") != "sim" and not result.get("mixer_fingerprint"):
            lines.append("ПРЕДУПРЕЖДЕНИЕ: снимок ALSA-микшера не получен; его изменения между шагами автоматически не обнаружатся.")
        if not all(row["valid"] and row["repeat_stable"] for row in rows):
            lines.append("ВНИМАНИЕ: петля нестабильна или измерена ненадёжно — эталон нельзя применять.")
        return "\n".join(lines)

    lines = ["ШАГ 2 ИЗ 2 · РЕЗУЛЬТАТ УСИЛИТЕЛЯ",
             f"Сравнение каналов после компенсации звуковой карты · допуск ±{tolerance_db:.2f} дБ"]
    values = []
    all_valid = True
    all_stable = True
    for row in rows:
        delta = row["delta_gain_db"]
        valid = row["valid"] and delta is not None
        stable = row["repeat_stable"]
        all_valid &= valid
        all_stable &= stable
        if not valid:
            verdict = "НЕТ ДОСТОВЕРНОГО ИЗМЕРЕНИЯ"
        elif not stable:
            verdict = "НЕСТАБИЛЬНО — повторить"
        elif abs(delta) <= tolerance_db:
            verdict = "СОВПАДАЮТ"
        else:
            stronger = "левый" if delta > 0 else "правый"
            verdict = f"{stronger.capitalize()} сильнее на {abs(delta):.3f} дБ"
        lines.append(f"{row['frequency_hz']:g} Гц:  ΔG L−R = "
                     f"{'—' if delta is None else f'{delta:+.3f} дБ'}  ·  {verdict}")
        lines.append(f"       повторяемость: {row['delta_output_spread_db']:.3f} дБ")
        if valid:
            values.append(abs(delta))
    if not all_valid:
        lines.append("ИТОГ: ИЗМЕРЕНИЕ НЕ ЗАСЧИТАНО — проверь уровни, кабели и перегрузку АЦП.")
    elif not all_stable:
        lines.append("ИТОГ: РЕЗУЛЬТАТ НЕСТАБИЛЕН — сначала устрани разброс повторов.")
    else:
        worst = max(values, default=0.0)
        if worst <= tolerance_db:
            lines.append(f"ИТОГ: КАНАЛЫ СОГЛАСОВАНЫ · максимальная разница {worst:.3f} дБ.")
        else:
            lines.append(f"ИТОГ: КАНАЛЫ НЕ СОГЛАСОВАНЫ · максимальная разница {worst:.3f} дБ.")
    lines.append("ΔG — относительная разница усиления L/R; абсолютная калибровка вольт для неё не требуется.")
    if result.get("metadata"):
        meta = result["metadata"]
        if meta.get("dut"):
            lines.append(f"Устройство: {meta['dut']}")
        if meta.get("tubes"):
            lines.append(f"Лампы/позиции: {meta['tubes']}")
        if meta.get("notes"):
            lines.append(f"Примечание: {meta['notes']}")
    return "\n".join(lines)
