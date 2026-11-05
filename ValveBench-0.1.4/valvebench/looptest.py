"""Повторяемая аппаратная проверка петли ЦАП -> АЦП с текстовым отчётом."""
from __future__ import annotations

import csv
import math
import statistics
import time
from pathlib import Path

import numpy as np

from . import dsp
from .measure import Rig, clock_check


def _fmt(values, digits=2):
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not clean:
        return "n/a"
    if len(clean) == 1:
        return f"{clean[0]:.{digits}f}"
    return f"{statistics.median(clean):.{digits}f} [{min(clean):.{digits}f}…{max(clean):.{digits}f}]"


def _capture_silence(rig: Rig, seconds: float, label: str) -> tuple[list[str], list[dict], np.ndarray]:
    lines = [f"{label}: {seconds:.1f} с цифровой тишины при одновременно открытых DAC+ADC"]
    # Keep both PCM endpoints open, send digital zero to DAC, and measure the actual
    # silent-input floor in the same full-duplex state used by the live meter.
    rig.be.live_start()
    try:
        rig.be.live_set_loop(None)
        time.sleep(seconds + 0.30)  # startup allowance plus the requested observation window
        x = rig.be.live_recent(seconds)
        status = rig.be.live_status() if hasattr(rig.be, "live_status") else {}
    finally:
        rig.be.live_stop()
    if len(x) < int(seconds * rig.fs * 0.95):
        raise RuntimeError(f"получено слишком мало кадров тишины: {len(x)} при ожидании {int(seconds * rig.fs)}")
    lines.append(f"  Получено: {len(x)} кадров / {rig.fs} = {len(x) / rig.fs:.3f} с; shape={x.shape}")
    if status:
        xruns = status.get("xruns")
        lines.append(f"  Full-duplex status: XRUN={xruns if xruns is not None else 'n/a'} · "
                     f"ADC {status.get('capture_frames', 0)} frames · DAC {status.get('playback_frames', 0)} frames")
    rows = []
    for c, ch in ((0, "L"), (1, "R")):
        seg = np.asarray(x[:, c], dtype=np.float64)
        peak = float(np.max(np.abs(seg))) if len(seg) else 0.0
        rms = float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0
        noise = dsp.analyze_noise(seg, rig.fs, band=(20.0, min(20000.0, 0.45 * rig.fs)),
                                  mains=float(rig.cal["mains"]))
        mean = float(np.mean(seg)) if len(seg) else 0.0
        row = {
            "section": label, "channel": ch,
            "rms_dbfs": float(dsp.rms_dbfs(rms)),
            "peak_dbfs": float(dsp.db20(peak)),
            "noise_unweighted_dbfs": float(noise["unw_dbfs"]),
            "noise_a_dbfs": float(noise["a_dbfs"]),
            "noise_c_dbfs": float(noise["c_dbfs"]),
            "dc_dbfs": float(dsp.db20(abs(mean))),
        }
        rows.append(row)
        hum = "  ".join(f"{h['f']:.0f}Hz {h['dbfs']:.1f}dBFS" for h in noise["hum"][:4])
        lines.append(
            f"  [{ch}] RMS={row['rms_dbfs']:.2f} dBFS(sine-ref) · peak={row['peak_dbfs']:.2f} dBFS · "
            f"noise 20–20k={row['noise_unweighted_dbfs']:.2f} dBFS · "
            f"A={row['noise_a_dbfs']:.2f} · C={row['noise_c_dbfs']:.2f} · DC={row['dc_dbfs']:.1f} dBFS")
        if hum:
            lines.append(f"      фон: {hum}")
    if rig.be.notes:
        lines += ["  Backend notes: " + "; ".join(rig.be.notes)]
    return lines, rows, x


def _noise_profiles(captures, fs):
    """Average silent-input PSDs per channel for local harmonic confidence checks."""
    profiles = {}
    if not captures:
        return profiles
    per_channel = {"L": [], "R": []}
    for capture in captures:
        for c, ch in ((0, "L"), (1, "R")):
            result = dsp.analyze_noise(capture[:, c], fs, band=(20.0, min(20000.0, 0.45 * fs)))
            per_channel[ch].append((result["freq"], 10.0 ** (np.asarray(result["dens_dbfs"]) / 10.0)))
    for ch, entries in per_channel.items():
        freq = entries[0][0]
        density = np.mean([entry[1] for entry in entries], axis=0)
        profiles[ch] = (freq, density, float(freq[1] - freq[0]))
    return profiles


def _local_floor_dbfs(profile, f0, half_width_hz=5.0):
    """Integrated silent-input noise near one harmonic, on the same sine dBFS reference."""
    if profile is None:
        return None
    freq, density_ratio, df = profile
    sel = (freq >= f0 - half_width_hz) & (freq <= f0 + half_width_hz)
    if not np.any(sel):
        return None
    power_ratio = float(np.sum(density_ratio[sel]) * df)
    return 10.0 * math.log10(max(power_ratio, 1e-300))


def _tone_once(rig: Rig, frequency: float, level_dbfs: float, repeat: int, noise_profiles=None) -> tuple[list[str], list[dict]]:
    k, amp = rig.amp_tone(frequency, level_dbfs)
    tones = [(k, amp)]
    wins, warnings = rig.run([(tones, tones, None)])
    win = wins[0]
    lines = [f"  Повтор {repeat}: DAC={level_dbfs:.1f} dBFS, f={rig.f_of(k):.3f} Гц"]
    rows = []
    for c, ch in ((0, "L"), (1, "R")):
        r = rig._tone_an(win, c, k, nharm=9)
        peak = float(np.max(np.abs(win[:, c])))
        peak_dbfs = float(dsp.db20(peak))
        loss = float(r["level_dbfs"] - level_dbfs)
        residual_dbfs = float(r["level_dbfs"] + r["thdn_db"])
        harmonics = {int(h["h"]): float(h["dbc"]) for h in r["harmonics"]}
        row = {
            "frequency_hz": float(rig.f_of(k)), "generator_dbfs": float(level_dbfs),
            "repeat": int(repeat), "channel": ch,
            "adc_fundamental_dbfs": float(r["level_dbfs"]),
            "loop_gain_db": loss, "peak_dbfs": peak_dbfs,
            "headroom_db": -peak_dbfs,
            "thd_pct": float(r["thd_pct"]), "thd_db": float(r["thd_db"]),
            "thdn_pct": float(r["thdn_pct"]), "thdn_db": float(r["thdn_db"]),
            "thdn_residual_dbfs": residual_dbfs,
            "noise_only_dbfs": float(r["noise_dbfs"]),
            "noise_only_dbc": float(r["noise_dbc"]),
            "adc_clip": bool(peak >= 0.98),
        }
        for h in range(2, 10):
            row[f"H{h}_dbc"] = harmonics.get(h)
        rows.append(row)
        htxt = " ".join(f"H{h} {harmonics[h]:.1f}" for h in range(2, 10) if h in harmonics)
        confidence = {}
        profile = (noise_profiles or {}).get(ch)
        if profile is not None:
            for h in range(2, 10):
                if h in harmonics:
                    hf = frequency * h
                    local_floor = _local_floor_dbfs(profile, hf)
                    abs_level = float(r["level_dbfs"] + harmonics[h])
                    if local_floor is not None:
                        margin = abs_level - local_floor
                        confidence[h] = {"harmonic_abs_dbfs": abs_level,
                                         "local_noise_dbfs": local_floor,
                                         "margin_db": margin}
                        row[f"H{h}_absolute_dbfs"] = abs_level
                        row[f"H{h}_local_noise_dbfs"] = local_floor
                        row[f"H{h}_margin_db"] = margin
        lines.append(
            f"    [{ch}] ADC fundamental={r['level_dbfs']:.2f} dBFS · loop Δ={loss:+.2f} dB · "
            f"peak={peak_dbfs:.2f} dBFS · headroom={-peak_dbfs:.2f} dB · "
            f"THD={r['thd_pct']:.6f}% ({r['thd_db']:.1f} dB) · "
            f"THD+N={r['thdn_pct']:.6f}% ({r['thdn_db']:.1f} dBc; остаток {residual_dbfs:.1f} dBFS) · "
            f"noise-only={r['noise_dbfs']:.1f} dBFS ({r['noise_dbc']:.1f} dBc)")
        lines.append(f"      Гармоники относительно основного тона, dBc: {htxt}")
        if confidence:
            ctext = " · ".join(f"H{h}: {v['margin_db']:+.1f} dB над локальным шумом "
                                f"(линия {v['harmonic_abs_dbfs']:.1f}, шум {v['local_noise_dbfs']:.1f} dBFS)"
                                for h, v in confidence.items())
            lines.append(f"      Достоверность линий (шумовая запись, полоса ±5 Гц): {ctext}")
            weak = [h for h, v in confidence.items() if v["margin_db"] < 6.0]
            if weak:
                lines.append(f"      ! Гармоники H{', H'.join(map(str, weak))} ближе 6 dB к локальному шумовому полу; не считать их точной характеристикой DAC/ADC.")
        if peak >= 0.98:
            lines.append("      !!! DIGITAL ADC CLIP: отсчёт достиг ≥0.98 FS")
        if r["warnings"]:
            lines.extend("      ! " + w for w in r["warnings"])
    if warnings:
        lines.extend("  ! " + w for w in warnings)
    return lines, rows


def run_loopback_report(rig: Rig, frequency=1000.0, levels=(-12.0, -20.0, -30.0, -40.0),
                        repeats=3, silence_seconds=3.0, csv_path=None) -> str:
    if not (20.0 <= frequency <= min(20000.0, 0.4 * rig.fs)):
        raise ValueError("для loopback-теста выбери частоту от 20 Гц до 0.4·Fs")
    if not levels or any(not (-80.0 <= float(v) <= -3.0) for v in levels):
        raise ValueError("уровни генератора должны находиться в диапазоне −80…−3 dBFS")
    if not (1 <= repeats <= 10):
        raise ValueError("число повторов должно быть от 1 до 10")
    if not (0.0 <= silence_seconds <= 10.0):
        raise ValueError("длительность записи тишины должна быть от 0 до 10 секунд (буфер измерения — 12 с)")

    started = time.monotonic()
    lines = [
        "VALVEBENCH HARDWARE LOOPBACK REPORT",
        f"Time: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
        "=" * 76,
        f"Backend: {rig.be.name} · device={getattr(rig.be, 'hw', 'sim')}",
        f"Fs={rig.fs} Hz · format={getattr(rig.be, 'fmt_in', 'float64')}/{getattr(rig.be, 'fmt_out', 'float64')} · "
        f"ADC channels={getattr(rig.be, 'ch_in', 2)} · DAC channels={getattr(rig.be, 'ch_out', 2)}",
        f"in_map={getattr(rig.be, 'in_map', (0, 1))} · out_map={getattr(rig.be, 'out_map', (0, 1))}",
        f"Tone={frequency:g} Hz · levels={','.join(f'{v:g}' for v in levels)} dBFS · "
        f"repeats={repeats} · coherent blocks={rig.blocks} · settle={rig.settle:.2f}s",
        "WARNING: connect selected DAC line outputs directly to matching ADC LINE inputs; do not connect a phono input.",
        "",
    ]
    rows = []
    silence_captures = []
    if silence_seconds > 0:
        section, rr, silent = _capture_silence(rig, silence_seconds, "SILENCE BEFORE")
        lines += section + [""]
        rows += rr
        silence_captures.append(silent)

    lines.append("CLOCK CHECK · 1 kHz loopback")
    clock = clock_check(rig)
    for ch, r in clock["ch"].items():
        lines.append(f"  [{ch}] measured={r['f0']:.4f} Hz · error={r['ppm']:+.1f} ppm · level={r['level_dbfs']:.2f} dBFS")
    lines.append("  PASS: playback/capture clock ratio within 100 ppm" if clock["ok"] else "  FAIL/WARNING: clock mismatch or non-coherent tone")
    lines.extend("  ! " + w for w in clock["warnings"])
    lines.append("")

    all_tone_rows = []
    if clock["ok"]:
        profiles = _noise_profiles(silence_captures, rig.fs) if silence_captures else None
        for level in levels:
            lines.append(f"TONE LEVEL {float(level):.1f} dBFS")
            for repeat in range(1, repeats + 1):
                section, rr = _tone_once(rig, frequency, float(level), repeat, profiles)
                lines.extend(section)
                all_tone_rows.extend(rr)
            lines.append("")
    else:
        lines.append("TONE SERIES SKIPPED: clock/coherence check failed; further THD data would be misleading.")
        lines.append("")

    if silence_seconds > 0:
        section, rr, silent = _capture_silence(rig, silence_seconds, "SILENCE AFTER")
        lines += section + [""]
        rows += rr
        silence_captures.append(silent)

    if silence_seconds > 0 and len(silence_captures) >= 2:
        before = {row["channel"]: row for row in rows if row["section"] == "SILENCE BEFORE"}
        after = {row["channel"]: row for row in rows if row["section"] == "SILENCE AFTER"}
        lines.append("SILENCE STABILITY · AFTER minus BEFORE")
        for ch in ("L", "R"):
            if ch in before and ch in after:
                delta = after[ch]["noise_unweighted_dbfs"] - before[ch]["noise_unweighted_dbfs"]
                lines.append(f"  [{ch}] noise 20–20k: {delta:+.2f} dB")
        lines.append("")

    lines.append("REPEATABILITY SUMMARY · median [min…max]")
    lines.append(" level   ch   ADC level dBFS   loop Δ dB   THD %       THD+N dBc   noise-only dBFS   H2 dBc   H3 dBc")
    for level in levels:
        for ch in ("L", "R"):
            subset = [q for q in all_tone_rows if q["generator_dbfs"] == float(level) and q["channel"] == ch]
            if not subset:
                continue
            lines.append(
                f" {float(level):6.1f}  {ch}   {_fmt([q['adc_fundamental_dbfs'] for q in subset], 2):>17s} "
                f"{_fmt([q['loop_gain_db'] for q in subset], 2):>12s} "
                f"{_fmt([q['thd_pct'] for q in subset], 6):>12s} "
                f"{_fmt([q['thdn_db'] for q in subset], 2):>12s} "
                f"{_fmt([q['noise_only_dbfs'] for q in subset], 2):>17s} "
                f"{_fmt([q.get('H2_dbc') for q in subset], 1):>9s} "
                f"{_fmt([q.get('H3_dbc') for q in subset], 1):>9s}")

    lines += [
        "",
        "INTERPRETATION NOTES",
        "- ADC fundamental level is normalized to a full-scale sine (dBFS).",
        "- THD+N dBc is relative to the measured fundamental; residual absolute dBFS is also printed.",
        "- noise-only excludes the fundamental and detected harmonics; it is not the same as a silent-input noise measurement.",
        "- SILENCE BEFORE/AFTER is the integrated 20 Hz–20 kHz ADC noise while both DAC+ADC streams are open and DAC sends digital zero.",
        "- Hn values are reported dBc relative to the fundamental. Local confidence compares each harmonic line with silent-input noise integrated over ±5 Hz; a margin below 6 dB is flagged as near/below the floor (diagnostic, not a formal uncertainty budget).",
        "- ADC CLIP means samples reached 0.98 FS; it cannot detect every analogue front-end overload.",
        "- XRUN means ALSA playback underrun or capture overrun. It is a buffer-service failure, not clipping.",
        f"Total elapsed: {time.monotonic() - started:.1f} s",
    ]
    report = "\n".join(lines)

    if csv_path:
        csv_file = Path(csv_path).expanduser()
        csv_file.parent.mkdir(parents=True, exist_ok=True)
        fields = sorted({key for row in all_tone_rows for key in row})
        with open(csv_file, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(all_tone_rows)
    return report
