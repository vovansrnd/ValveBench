#!/usr/bin/env python3
"""v2.16 — offline clock-gate audit using the R-only onset on v2.14 WAVs.

No playback, no device open, no source/production modifications.
For every failed capture, reproduce the narrowband detector per channel, then
analyze the exact window that Rig.run would select if R supplied the onset:
R_onset + clock_check settle (0.4 s), length blocks*n.

Also analyze the stable-reference window (1.855 s + 0.4 s) to expose cases
where the R-only onset is delayed. Recompute the actual production clock gates
using Rig._tone_an / dsp.analyze_tone: per-channel carrier-local SNR, ppm,
L/R spread, and Rig._window_stability_reason.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from valvebench import calib, measure as m

FS = 192000
TONE_HZ = 1000.0
CLOCK_SETTLE_S = 0.4
REFERENCE_ONSET_FRAME = 356160  # 1.855 s, successful v2.14 captures
FAILED = (1, 2, 15, 16)
REFERENCES = (3, 4, 8)
BLOCK_FRAMES = int(0.005 * FS)
WINDOW_SECONDS = 2.0


def tone_blocks(channel: np.ndarray) -> np.ndarray:
    """Exact 5-ms narrowband envelope used by Rig._onset_narrowband."""
    x = np.asarray(channel, dtype=np.float64)
    n = len(x)
    t = np.arange(n, dtype=np.float64) / FS
    z = x * np.exp(-2j * np.pi * TONE_HZ * t)
    cs = np.concatenate(([0j], np.cumsum(z)))
    win = max(8, int(round(5 * FS / TONE_HZ)))
    idx = np.arange(n)
    lo = np.clip(idx - win // 2, 0, n)
    hi = np.clip(idx - win // 2 + win, 0, n)
    amp = 2.0 * np.abs((cs[hi] - cs[lo]) / np.maximum(hi - lo, 1))
    nblocks = n // BLOCK_FRAMES
    return amp[:nblocks * BLOCK_FRAMES].reshape(nblocks, BLOCK_FRAMES).max(axis=1)


def onset_stats(blocks: np.ndarray) -> dict:
    """Same constants and 90%-over-1-s sustain condition as production."""
    noise = max(float(np.median(blocks[:40])), 1e-7)
    peak99 = float(np.percentile(blocks, 99))
    threshold = max(8.0 * noise, 0.1 * peak99)
    above = blocks > threshold
    sustain = max(4, min(int(1.0 / 0.005), len(blocks)))
    cumulative = np.concatenate(([0], np.cumsum(above.astype(np.int64))))
    onset = None
    for i in range(len(blocks) - 4):
        end = min(len(blocks), i + sustain)
        span = end - i
        if above[i] and (cumulative[end] - cumulative[i]) >= 0.9 * span:
            onset = i * BLOCK_FRAMES
            break
    return {
        "noise_dbfs": round(20 * math.log10(max(noise, 1e-15)), 2),
        "peak99_dbfs": round(20 * math.log10(max(peak99, 1e-15)), 2),
        "threshold_dbfs": round(20 * math.log10(max(threshold, 1e-15)), 2),
        "threshold_rule": "8x_noise" if 8.0 * noise >= 0.1 * peak99 else "0.1x_peak99",
        "fraction_blocks_above": round(float(np.mean(above)), 5),
        "sustain_found": onset is not None,
        "onset_frame": onset,
        "onset_s": None if onset is None else round(onset / FS, 6),
    }


def analyze_clock_window(rig: m.Rig, rec: np.ndarray, onset_frame: int,
                         level_dbfs: float, label: str) -> dict:
    """Run the production analyzer and exact clock validity formulas offline."""
    window_start = int(onset_frame + round(CLOCK_SETTLE_S * FS))
    window_frames = int(rig.blocks * rig.n)
    window_end = window_start + window_frames
    result = {
        "label": label,
        "onset_frame": int(onset_frame),
        "onset_s": round(onset_frame / FS, 6),
        "window_start_frame": window_start,
        "window_end_frame": window_end,
        "window_start_s": round(window_start / FS, 6),
        "window_end_s": round(window_end / FS, 6),
        "window_frames": window_frames,
    }
    if window_start < 0 or window_end > len(rec):
        result["error"] = "analysis window lies outside capture"
        return result

    window = np.asarray(rec[window_start:window_end], dtype=np.float64)
    k, _ = rig.amp_tone(TONE_HZ, level_dbfs)
    tones = [(k, 10.0 ** (level_dbfs / 20.0))]
    stability = rig._window_stability_reason(window, tones, tones)
    result["window_stability_reason"] = stability
    result["peak_abs"] = round(float(np.max(np.abs(window))), 8)
    result["adc_clip_gate_hit"] = bool(float(np.max(np.abs(window))) >= 0.98)

    channel_rows = {}
    ppm_values = []
    signal_flags = []
    clock_flags = []
    coherent_flags = []
    for channel, name in ((0, "L"), (1, "R")):
        analysis = rig._tone_an(window, channel, k, nharm=2)
        f0 = float(analysis.get("f0", float("nan")))
        ppm = (f0 / rig.f_of(k) - 1.0) * 1e6 if math.isfinite(f0) else float("nan")
        snr = float(analysis.get("local_snr_db", float("nan")))
        coh_err = float(analysis.get("coh_err_bins", float("nan")))
        level = float(analysis.get("level_dbfs", float("nan")))
        noise = float(analysis.get("noise_dbfs", float("nan")))
        local_noise = float(analysis.get("local_noise_dbfs", float("nan")))
        clock_ok = math.isfinite(ppm) and abs(ppm) <= m.CLOCK_MAX_PPM
        signal_ok = math.isfinite(snr) and snr >= m.CLOCK_MIN_SNR_DB
        coherent = math.isfinite(coh_err) and abs(coh_err) <= 0.002
        channel_rows[name] = {
            "f0_hz": f0 if math.isfinite(f0) else None,
            "ppm": ppm if math.isfinite(ppm) else None,
            "level_dbfs": level if math.isfinite(level) else None,
            "noise_dbfs": noise if math.isfinite(noise) else None,
            "local_noise_dbfs": local_noise if math.isfinite(local_noise) else None,
            "carrier_local_snr_db": snr if math.isfinite(snr) else None,
            "coh_err_bins": coh_err if math.isfinite(coh_err) else None,
            "clock_ok": bool(clock_ok),
            "signal_ok": bool(signal_ok),
            "coherent": bool(coherent),
            "analyzer_warnings": list(analysis.get("warnings", [])),
        }
        ppm_values.append(ppm)
        signal_flags.append(signal_ok)
        clock_flags.append(clock_ok)
        coherent_flags.append(coherent)

    spread = (max(ppm_values) - min(ppm_values)
              if len(ppm_values) == 2 and all(math.isfinite(v) for v in ppm_values)
              else float("inf"))
    clock_consistent = spread <= m.CLOCK_CHANNEL_SPREAD_PPM
    clock_ppm_ok = all(clock_flags) and clock_consistent
    signal_ok = all(signal_flags) and not result["adc_clip_gate_hit"]
    result["channels"] = channel_rows
    result["clock_channel_spread_ppm"] = spread if math.isfinite(spread) else None
    result["clock_ppm_ok"] = bool(clock_ppm_ok)
    result["clock_consistent"] = bool(clock_consistent)
    result["signal_ok_both_channels"] = bool(signal_ok)
    result["coherent_both_channels"] = bool(all(coherent_flags))
    result["clock_ok_without_transport_metadata"] = bool(clock_ppm_ok and signal_ok)
    result["transport_metadata_available"] = False
    result["note"] = (
        "XRUN/transport warnings are not recoverable from the WAV/JSON; "
        "the overall flag above reproduces numeric gates only."
    )
    return result


def locate_capture(row: dict, source_wav_dir: Path | None) -> Path:
    original = Path(row["files"]["capture"]["path"])
    candidates = [original]
    if source_wav_dir is not None:
        candidates.append(source_wav_dir / original.name)
    candidates.append(ROOT / "measurements" / "diagnostics" /
                      "v214-clock-diagnostic" / original.name)
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError(
        f"WAV not found: {original}; tried " + ", ".join(map(str, candidates))
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-json",
        default=str(ROOT / "measurements" / "diagnostics" /
                    "v214-clock-diagnostic" / "20261114-004425-results.json"),
    )
    parser.add_argument("--source-wav-dir", default=None)
    parser.add_argument(
        "--out-dir",
        default=str(ROOT / "measurements" / "diagnostics" /
                    "v216-clock-offline-gate"),
    )
    args = parser.parse_args()

    source_json = Path(args.source_json)
    source_wav_dir = Path(args.source_wav_dir) if args.source_wav_dir else None
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    source = json.loads(source_json.read_text(encoding="utf-8"))

    cfg = calib.Calibration()
    rig = m.Rig(None, cfg, fs=FS, blocks=2, settle=0.5)
    rows = []
    for index in (*FAILED, *REFERENCES):
        row = next(item for item in source["clock_results"] if item["index"] == index)
        path = locate_capture(row, source_wav_dir)
        sample_rate, rec = wavfile.read(path)
        if sample_rate != FS:
            raise ValueError(f"{path}: expected {FS} Hz, got {sample_rate}")
        rec = np.asarray(rec, dtype=np.float64)
        if rec.ndim != 2 or rec.shape[1] != 2:
            raise ValueError(f"{path}: expected stereo WAV, got shape {rec.shape}")

        blocks_l = tone_blocks(rec[:, 0])
        blocks_r = tone_blocks(rec[:, 1])
        stats_l = onset_stats(blocks_l)
        stats_r = onset_stats(blocks_r)
        stats_combined = onset_stats(np.maximum(blocks_l, blocks_r))
        result = {
            "index": index,
            "level_dbfs": float(row["level_dbfs"]),
            "production_failed": bool(row.get("clock_error")),
            "capture": path.name,
            "onset": {
                "L": stats_l,
                "R": stats_r,
                "combined_production": stats_combined,
            },
            "windows": {},
        }
        # This is the actual window that a hypothetical R-only onset would select.
        if stats_r["onset_frame"] is not None:
            result["windows"]["from_R_only_onset"] = analyze_clock_window(
                rig, rec, stats_r["onset_frame"], float(row["level_dbfs"]),
                "R-only onset + 0.4-s clock settle",
            )
        # Compare with the normal timing seen in successful v2.14 captures.
        result["windows"]["from_success_reference_onset"] = analyze_clock_window(
            rig, rec, REFERENCE_ONSET_FRAME, float(row["level_dbfs"]),
            "fixed successful-capture onset reference + 0.4-s clock settle",
        )
        if row.get("clock_result"):
            result["recorded_v214_clock_result"] = row["clock_result"]
        rows.append(result)
        print(
            f"[{index:02d}] {row['level_dbfs']:+.0f} dBFS "
            f"R onset={stats_r['onset_s']} s; "
            f"combined onset={stats_combined['onset_s']} s",
            flush=True,
        )
        selected = result["windows"].get("from_R_only_onset", {})
        if selected.get("channels"):
            for name in ("L", "R"):
                ch = selected["channels"][name]
                print(
                    f"    {name}: level={ch['level_dbfs']:.2f} dBFS "
                    f"carrier-local SNR={ch['carrier_local_snr_db']:.2f} dB "
                    f"ppm={ch['ppm']:.2f}",
                    flush=True,
                )
            print(
                f"    numeric clock gate={selected['clock_ok_without_transport_metadata']} "
                f"stability={selected['window_stability_reason']}",
                flush=True,
            )

    output = {
        "version": "v2.16-clock-offline-gate",
        "source_json": str(source_json),
        "method": {
            "playback_performed": False,
            "onset": "same 5-ms narrowband envelope and sustain test as v2.15",
            "window": "per-capture R onset + 0.4-s settle, 2.0-s exact production window",
            "analyzer": "Rig._tone_an / dsp.analyze_tone, nharm=2",
            "clock_gates": {
                "min_carrier_local_snr_db": m.CLOCK_MIN_SNR_DB,
                "max_abs_ppm": m.CLOCK_MAX_PPM,
                "max_lr_spread_ppm": m.CLOCK_CHANNEL_SPREAD_PPM,
            },
            "transport_limit": (
                "XRUN/backend notes cannot be reconstructed from WAV alone; "
                "numeric gates are reported separately."
            ),
        },
        "results": rows,
    }
    out_json = out_dir / "v216-clock-offline-gate-results.json"
    out_json.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON: {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
