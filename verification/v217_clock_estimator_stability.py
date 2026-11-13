#!/usr/bin/env python3
"""v2.17 — offline sensitivity of the clock ppm estimator to window placement.

No playback, no device open, no production changes.
Reuses the exact production Rig._tone_an / dsp.analyze_tone on overlapping
2-second windows cut from the saved v2.14 captures. The R-only onset from
v2.16 anchors the sweep. A separate replay of the recorded v2.14 window
validates that the offline estimator reproduces the stored clock result.

The sweep deliberately does NOT select a "best" window or change thresholds.
It reports how L/R ppm and carrier-local SNR move as the same capture window
slides over the same nominal 1-kHz tone.
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
CLOCK_SETTLE_FRAMES = int(round(0.4 * FS))
WINDOW_SECONDS = 2.0
WINDOW_FRAMES = int(round(WINDOW_SECONDS * FS))
BLOCK_FRAMES = int(round(0.005 * FS))
FAILED = (1, 2, 15, 16)
REFERENCES = (3, 4, 8)
SWEEP_OFFSETS_S = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40)
REFERENCE_ONSET_FRAME = 356160


def tone_blocks(channel: np.ndarray) -> np.ndarray:
    """Exact 5-ms narrowband envelope from Rig._onset_narrowband."""
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
    count = n // BLOCK_FRAMES
    return amp[:count * BLOCK_FRAMES].reshape(count, BLOCK_FRAMES).max(axis=1)


def detect_onset(blocks: np.ndarray) -> dict:
    """Exact production threshold and sustain constants, no tuning."""
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
        "noise_dbfs": round(20.0 * math.log10(max(noise, 1e-15)), 3),
        "threshold_dbfs": round(20.0 * math.log10(max(threshold, 1e-15)), 3),
        "sustain_found": onset is not None,
        "onset_frame": onset,
        "onset_s": None if onset is None else round(onset / FS, 6),
    }


def analyze_window(rig: m.Rig, rec: np.ndarray, start_frame: int,
                   level_dbfs: float, label: str) -> dict:
    end_frame = start_frame + WINDOW_FRAMES
    row = {
        "label": label,
        "start_frame": int(start_frame),
        "end_frame": int(end_frame),
        "start_s": round(start_frame / FS, 6),
        "end_s": round(end_frame / FS, 6),
    }
    if start_frame < 0 or end_frame > len(rec):
        row["error"] = "window outside capture"
        return row

    win = np.asarray(rec[start_frame:end_frame], dtype=np.float64)
    k, amp = rig.amp_tone(TONE_HZ, level_dbfs)
    tones = [(k, amp)]
    row["window_stability_reason"] = rig._window_stability_reason(win, tones, tones)
    row["adc_clip_gate_hit"] = bool(float(np.max(np.abs(win))) >= 0.98)

    channels = {}
    ppms, signal_ok, clock_ok = [], [], []
    for ch, name in ((0, "L"), (1, "R")):
        a = rig._tone_an(win, ch, k, nharm=2)
        f0 = float(a.get("f0", float("nan")))
        ppm = (f0 / rig.f_of(k) - 1.0) * 1e6 if math.isfinite(f0) else float("nan")
        snr = float(a.get("local_snr_db", float("nan")))
        level = float(a.get("level_dbfs", float("nan")))
        noise = float(a.get("noise_dbfs", float("nan")))
        local_noise = float(a.get("local_noise_dbfs", float("nan")))
        c_ok = math.isfinite(ppm) and abs(ppm) <= m.CLOCK_MAX_PPM
        s_ok = math.isfinite(snr) and snr >= m.CLOCK_MIN_SNR_DB
        channels[name] = {
            "f0_hz": f0 if math.isfinite(f0) else None,
            "ppm": ppm if math.isfinite(ppm) else None,
            "level_dbfs": level if math.isfinite(level) else None,
            "noise_dbfs": noise if math.isfinite(noise) else None,
            "local_noise_dbfs": local_noise if math.isfinite(local_noise) else None,
            "carrier_local_snr_db": snr if math.isfinite(snr) else None,
            "clock_ok": bool(c_ok),
            "signal_ok": bool(s_ok),
            "analyzer_warnings": list(a.get("warnings", [])),
        }
        ppms.append(ppm)
        signal_ok.append(s_ok)
        clock_ok.append(c_ok)

    spread = max(ppms) - min(ppms) if all(math.isfinite(v) for v in ppms) else float("inf")
    consistent = spread <= m.CLOCK_CHANNEL_SPREAD_PPM
    numeric_clock_ok = all(clock_ok) and consistent
    numeric_signal_ok = all(signal_ok) and not row["adc_clip_gate_hit"]
    row["channels"] = channels
    row["ppm_spread"] = spread if math.isfinite(spread) else None
    row["spread_gate"] = bool(consistent)
    row["numeric_clock_gate_without_transport"] = bool(numeric_clock_ok and numeric_signal_ok)
    row["transport_metadata_available"] = False
    return row


def locate_capture(row: dict, wav_dir: Path | None, default_dir: Path) -> Path:
    original = Path(row["files"]["capture"]["path"])
    candidates = [original]
    if wav_dir is not None:
        candidates.append(wav_dir / original.name)
    candidates.append(default_dir / original.name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"missing {original.name}; tried {candidates}")


def summarise_sweep(rows: list[dict]) -> dict:
    usable = [r for r in rows if "ppm_spread" in r and r["ppm_spread"] is not None]
    out = {}
    for name in ("L", "R"):
        values = [r["channels"][name]["ppm"] for r in usable
                  if r.get("channels", {}).get(name, {}).get("ppm") is not None]
        snrs = [r["channels"][name]["carrier_local_snr_db"] for r in usable
                if r.get("channels", {}).get(name, {}).get("carrier_local_snr_db") is not None]
        out[name] = {
            "ppm_min": min(values) if values else None,
            "ppm_max": max(values) if values else None,
            "ppm_range": (max(values) - min(values)) if values else None,
            "ppm_std_population": float(np.std(values)) if values else None,
            "snr_min_db": min(snrs) if snrs else None,
            "snr_max_db": max(snrs) if snrs else None,
        }
    spreads = [r["ppm_spread"] for r in usable]
    out["spread_gate_pass_windows"] = sum(r["spread_gate"] for r in usable)
    out["spread_gate_total_windows"] = len(usable)
    out["spread_min"] = min(spreads) if spreads else None
    out["spread_max"] = max(spreads) if spreads else None
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-json",
        default=str(ROOT / "measurements" / "diagnostics" /
                    "v214-clock-diagnostic" / "20261114-004425-results.json"),
    )
    parser.add_argument("--source-wav-dir", default=None)
    parser.add_argument(
        "--out-dir", default=str(ROOT / "measurements" / "diagnostics" /
                                 "v217-clock-estimator-stability"),
    )
    args = parser.parse_args()

    source_json = Path(args.source_json)
    wav_dir = Path(args.source_wav_dir) if args.source_wav_dir else None
    default_wav_dir = ROOT / "measurements" / "diagnostics" / "v214-clock-diagnostic"
    source = json.loads(source_json.read_text(encoding="utf-8"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rig = m.Rig(None, calib.Calibration(), fs=FS, blocks=2, settle=0.5)
    results = []
    for index in (*FAILED, *REFERENCES):
        source_row = next(x for x in source["clock_results"] if x["index"] == index)
        path = locate_capture(source_row, wav_dir, default_wav_dir)
        sample_rate, rec = wavfile.read(path)
        if sample_rate != FS:
            raise ValueError(f"{path}: expected {FS} Hz, got {sample_rate}")
        rec = np.asarray(rec, dtype=np.float64)
        if rec.ndim != 2 or rec.shape[1] != 2:
            raise ValueError(f"{path}: expected stereo, got {rec.shape}")

        env_l, env_r = tone_blocks(rec[:, 0]), tone_blocks(rec[:, 1])
        onset_l, onset_r = detect_onset(env_l), detect_onset(env_r)
        onset_combined = detect_onset(np.maximum(env_l, env_r))
        result = {
            "index": index,
            "level_dbfs": float(source_row["level_dbfs"]),
            "production_failed": bool(source_row.get("clock_error")),
            "capture": path.name,
            "onsets": {
                "L": onset_l,
                "R": onset_r,
                "combined": onset_combined,
            },
            "windows": [],
        }

        start_frames: dict[int, list[str]] = {}
        if onset_r["onset_frame"] is not None:
            r_onset = int(onset_r["onset_frame"])
            for offset in SWEEP_OFFSETS_S:
                frame = r_onset + int(round(offset * FS))
                start_frames.setdefault(frame, []).append(f"R_onset+{offset:.2f}s")
            prod_frame = r_onset + CLOCK_SETTLE_FRAMES
            start_frames.setdefault(prod_frame, []).append("R_onset+production_settle_0.40s")

        # A fixed reference placement is the actual successful v2.14 onset
        # reference, included to expose sensitivity to the delayed idx=1 R onset.
        reference_frame = REFERENCE_ONSET_FRAME + CLOCK_SETTLE_FRAMES
        start_frames.setdefault(reference_frame, []).append("fixed_reference_onset+0.40s")

        # The original recorded window is independently replayed for every
        # successful source row. It validates numerical parity with saved clock_result.
        if source_row.get("window", {}).get("start_s") is not None:
            recorded_frame = int(round(float(source_row["window"]["start_s"]) * FS))
            start_frames.setdefault(recorded_frame, []).append("recorded_v214_window")

        window_rows = []
        for frame, labels in sorted(start_frames.items()):
            window_rows.append(analyze_window(
                rig, rec, frame, float(source_row["level_dbfs"]), "+".join(labels)
            ))

        result["windows"] = window_rows
        result["sweep_summary"] = summarise_sweep(window_rows)

        # Compare replayed recorded window with the serialized production result.
        recorded = source_row.get("clock_result")
        replay = next((w for w in window_rows if "recorded_v214_window" in w["label"]), None)
        if recorded and replay and replay.get("channels"):
            deltas = {}
            for name in ("L", "R"):
                old = recorded["ch"][name]
                new = replay["channels"][name]
                deltas[name] = {
                    "ppm_delta": float(new["ppm"] - old["ppm"]),
                    "snr_delta_db": float(new["carrier_local_snr_db"] - old["snr_db"]),
                    "f0_delta_hz": float(new["f0_hz"] - old["f0"]),
                }
            result["recorded_result_replay_delta"] = deltas

        results.append(result)
        print(
            f"[{index:02d}] {source_row['level_dbfs']:+.0f} dBFS "
            f"R onset={onset_r['onset_s']} s, windows={len(window_rows)}, "
            f"L ppm range={result['sweep_summary']['L']['ppm_range']}, "
            f"R ppm range={result['sweep_summary']['R']['ppm_range']}, "
            f"spread passes={result['sweep_summary']['spread_gate_pass_windows']}/"
            f"{result['sweep_summary']['spread_gate_total_windows']}",
            flush=True,
        )

    output = {
        "version": "v2.17-clock-estimator-window-sensitivity",
        "playback_performed": False,
        "production_modified": False,
        "source_json": str(source_json),
        "window": {
            "frames": WINDOW_FRAMES,
            "seconds": WINDOW_SECONDS,
            "R_onset_offsets_s": list(SWEEP_OFFSETS_S),
            "purpose": "sensitivity characterization only; no window selection or thresholds adjusted",
        },
        "gates": {
            "min_carrier_local_snr_db": m.CLOCK_MIN_SNR_DB,
            "max_abs_ppm": m.CLOCK_MAX_PPM,
            "max_lr_spread_ppm": m.CLOCK_CHANNEL_SPREAD_PPM,
        },
        "transport_limit": (
            "backend XRUN/transport notes are unavailable in saved captures; "
            "all pass/fail values are numeric-only"
        ),
        "results": results,
    }
    output_path = out_dir / "v217-clock-estimator-stability-results.json"
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
