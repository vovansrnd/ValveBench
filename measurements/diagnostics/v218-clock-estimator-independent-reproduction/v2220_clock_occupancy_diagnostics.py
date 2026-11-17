#!/usr/bin/env python3
"""v2.22.0 full-path diagnostics for clock-check occupancy and block presence.

The real production Rig.run() and clock_check() are exercised through an
offline synthetic capture backend. The script also records per-block tone
metrics and two candidate frame-occupancy summaries. No devices, playback,
hardware capture, DUT, WAV, or production-source changes are used.
"""
from __future__ import annotations

import json
import math
import platform
import sys
from pathlib import Path

import numpy as np
import scipy

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from valvebench.measure import Rig, clock_check

FS = 96000
WINDOW_S = 2.0
SETTLE_S = 0.4
DELAY_S = 0.137
PILOT_DBFS = -72.0
NOISE_DBFS = -110.0
SEED_BASE = 222001
FRAME_S = 0.002
DROP_DB = 12.0


def amp(dbfs: float) -> float:
    return 10.0 ** (float(dbfs) / 20.0)


def finite(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def case(name, **kwargs):
    out = {"name": name, "active_intervals_s": None, "frequency_offset_ppm": 0.0,
           "am_depth": 0.0, "am_hz": 0.0, "noise_dbfs": NOISE_DBFS}
    out.update(kwargs)
    if out["active_intervals_s"] is None:
        out["active_intervals_s"] = [[0.0, WINDOW_S]]
    out["active_fraction"] = sum(b - a for a, b in out["active_intervals_s"]) / WINDOW_S
    return out


def periodic_case(period_ms: float, duty: float):
    period = period_ms / 1000.0
    on = period * duty
    intervals = []
    t = 0.0
    while t < WINDOW_S - 1e-12:
        end = min(WINDOW_S, t + on)
        if end > t:
            intervals.append([t, end])
        t += period
    return case(
        f"pulse_period_{period_ms:g}ms_duty_{int(round(duty * 100)):02d}pct",
        active_intervals_s=intervals,
        pulse_period_ms=period_ms,
        pulse_duty=duty,
        max_nominal_gap_ms=(period - on) * 1000.0,
    )


CASES = [
    case("clean_steady"),
    case("front_contiguous_20pct", active_intervals_s=[[0.0, 0.4]]),
    case("front_contiguous_40pct", active_intervals_s=[[0.0, 0.8]]),
    case("front_contiguous_49pct", active_intervals_s=[[0.0, 0.98]]),
    case("front_contiguous_50pct", active_intervals_s=[[0.0, 1.0]]),
    case("front_contiguous_60pct", active_intervals_s=[[0.0, 1.2]]),
    case("40pct_center", active_intervals_s=[[0.6, 1.4]]),
    case("40pct_back", active_intervals_s=[[1.2, 2.0]]),
    case("40pct_split", active_intervals_s=[[0.0, 0.4], [1.0, 1.4]]),
    case("40pct_four_bursts", active_intervals_s=[
        [0.0, 0.2], [0.45, 0.65], [0.9, 1.1], [1.35, 1.55]
    ]),
]
for period_ms, duty in (
    (40, 0.40), (40, 0.60),
    (50, 0.50),
    (80, 0.40), (80, 0.60),
    (100, 0.40), (100, 0.60),
    (120, 0.40), (120, 0.60),
    (200, 0.40), (200, 0.80),
):
    CASES.append(periodic_case(period_ms, duty))

for ppm in (-110.0, -100.0, -80.0, -50.0, 0.0, 50.0, 80.0, 100.0, 110.0):
    CASES.append(case(f"steady_frequency_offset_{ppm:+g}ppm",
                      frequency_offset_ppm=ppm))

for depth, mod_hz in ((0.20, 5.0), (0.50, 5.0), (0.50, 20.0), (0.80, 2.0)):
    CASES.append(case(f"steady_am_depth_{depth:g}_at_{mod_hz:g}hz",
                      am_depth=depth, am_hz=mod_hz))


def frame_metrics(x: np.ndarray, fs: int):
    frame_n = int(round(FRAME_S * fs))
    usable = len(x) // frame_n * frame_n
    if usable < frame_n * 4:
        return {"frames": 0}
    frames = x[:usable].reshape(-1, frame_n)
    rms = np.sqrt(np.mean(frames * frames, axis=1)) * math.sqrt(2.0)
    db = 20.0 * np.log10(np.clip(rms, 1e-12, None))
    median = float(np.median(db))
    q90 = float(np.percentile(db, 90))
    q95 = float(np.percentile(db, 95))

    def occupancy(ref_db):
        return float(np.mean(db >= ref_db - DROP_DB))

    low = db < median - DROP_DB
    max_run = 0
    run = 0
    for value in low:
        if value:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    return {
        "frames": int(len(db)),
        "frame_ms": FRAME_S * 1000.0,
        "median_dbfs": median,
        "q90_dbfs": q90,
        "q95_dbfs": q95,
        "median_relative_above_12db_fraction": occupancy(median),
        "q90_relative_above_12db_fraction": occupancy(q90),
        "q95_relative_above_12db_fraction": occupancy(q95),
        "median_relative_low_max_run_ms": float(max_run * FRAME_S * 1000.0),
    }


class SyntheticCaptureBackend:
    name = "v2220-synthetic-occupancy-clock-offset"

    def __init__(self, cfg: dict, seed: int):
        self.cfg = cfg
        self.seed = int(seed)
        self.fs = FS
        self.notes = []
        self.play_calls = 0
        self.capture_meta = {}

    def open(self, fs):
        self.fs = int(fs)

    def play_record(self, out, tail=0.8):
        self.play_calls += 1
        program = np.asarray(out, dtype=np.float64)
        if program.ndim == 1:
            program = np.column_stack((program, program))
        if program.ndim != 2 or program.shape[1] != 2:
            raise ValueError(f"unexpected program shape: {program.shape}")

        ppm = float(self.cfg.get("frequency_offset_ppm", 0.0))
        if ppm:
            source_pos = np.arange(len(program), dtype=np.float64) * (1.0 + ppm * 1e-6)
            source_idx = np.arange(len(program), dtype=np.float64)
            program = np.column_stack([
                np.interp(source_pos, source_idx, program[:, c],
                          left=0.0, right=0.0)
                for c in (0, 1)
            ])

        depth = float(self.cfg.get("am_depth", 0.0))
        am_hz = float(self.cfg.get("am_hz", 0.0))
        if depth and am_hz:
            t = np.arange(len(program), dtype=np.float64) / self.fs
            envelope = 1.0 + depth * np.sin(2.0 * math.pi * am_hz * t)
            program = program * envelope[:, None]

        delay_n = int(round(DELAY_S * self.fs))
        settle_n = int(round(SETTLE_S * self.fs))
        window_n = int(round(WINDOW_S * self.fs))
        tail_n = int(round(max(0.0, float(tail)) * self.fs))
        n = delay_n + len(program) + tail_n + int(round(0.3 * self.fs))

        rng = np.random.default_rng(self.seed)
        noise_dbfs = float(self.cfg.get("noise_dbfs", NOISE_DBFS))
        noise = amp(noise_dbfs) / math.sqrt(2.0) * rng.standard_normal((n, 2))
        rec = noise.copy()
        rec[delay_n:delay_n + len(program)] += program

        nominal_start = delay_n + settle_n
        nominal_end = nominal_start + window_n
        source_start = settle_n
        source_end = source_start + window_n
        if source_end > len(program) or nominal_end > n:
            raise RuntimeError("synthetic capture too short for analysis window")

        source_window = program[source_start:source_end].copy()
        rec[nominal_start:nominal_end] = noise[nominal_start:nominal_end]
        active_samples = 0
        for start_s, end_s in self.cfg["active_intervals_s"]:
            a = nominal_start + int(round(start_s * self.fs))
            b = nominal_start + int(round(end_s * self.fs))
            sa = source_start + int(round(start_s * self.fs))
            sb = source_start + int(round(end_s * self.fs))
            rec[a:b] = noise[a:b] + source_window[sa - source_start:sb - source_start]
            active_samples += b - a

        self.capture_meta = {
            "capture_samples": int(n),
            "program_samples": int(len(program)),
            "delay_samples": int(delay_n),
            "nominal_window_start_sample": int(nominal_start),
            "nominal_window_samples": int(window_n),
            "active_samples": int(active_samples),
            "active_fraction": float(active_samples / window_n),
            "frequency_offset_ppm": ppm,
            "am_depth": depth,
            "am_hz": am_hz,
        }
        return rec


def per_block_metrics(rig: Rig, win: np.ndarray, k: int):
    result = {}
    block_n = rig.n
    for block_index in range(rig.blocks):
        block = win[block_index * block_n:(block_index + 1) * block_n]
        channels = {}
        for ch, label in ((0, "L"), (1, "R")):
            try:
                r = rig._tone_an(block, ch, k, nharm=2)
                channels[label] = {
                    "f0_hz": finite(r.get("f0")),
                    "level_dbfs": finite(r.get("level_dbfs")),
                    "local_snr_db": finite(r.get("local_snr_db")),
                    "coh_err_bins": finite(r.get("coh_err_bins")),
                    "warnings": list(r.get("warnings", [])),
                }
            except Exception as exc:
                channels[label] = {"error": f"{type(exc).__name__}: {exc}"}
        result[f"block_{block_index + 1}"] = channels
    return result


def run_case(cfg: dict, index: int):
    backend = SyntheticCaptureBackend(cfg, SEED_BASE + index)
    rig = Rig(backend=backend, fs=FS, df=1.0, blocks=2)
    k, _ = rig.amp_tone(1000.0, PILOT_DBFS)
    row = {
        "case": cfg["name"],
        "active_fraction": cfg["active_fraction"],
        "active_intervals_s": cfg["active_intervals_s"],
        "pulse_period_ms": cfg.get("pulse_period_ms"),
        "pulse_duty": cfg.get("pulse_duty"),
        "max_nominal_gap_ms": cfg.get("max_nominal_gap_ms"),
        "frequency_offset_ppm_injected": cfg.get("frequency_offset_ppm", 0.0),
        "am_depth": cfg.get("am_depth", 0.0),
        "am_hz": cfg.get("am_hz", 0.0),
        "rig_run": "not_started",
        "clock_check": "not_started",
        "error": None,
        "onset_s": None,
    }
    rig.open()
    captured = {}
    original_onset = rig._onset

    def tracked_onset(rec, tone_hz=None):
        onset = original_onset(rec, tone_hz=tone_hz)
        row["onset_s"] = float(onset / rig.fs)
        return onset

    rig._onset = tracked_onset
    original_run = rig.run

    def tracked_run(segs):
        try:
            wins, warnings = original_run(segs)
            row["rig_run"] = "pass"
            row["run_warnings"] = list(warnings)
            captured["window"] = wins[0].copy()
            return wins, warnings
        except Exception as exc:
            row["rig_run"] = "reject"
            row["run_error"] = f"{type(exc).__name__}: {exc}"
            raise

    rig.run = tracked_run

    try:
        result = clock_check(rig, level_dbfs=PILOT_DBFS)
        row["clock_check"] = "returned"
        row["clock_result"] = {
            "ok": bool(result.get("ok")),
            "clock_ok": bool(result.get("clock_ok")),
            "signal_ok": bool(result.get("signal_ok")),
            "coherent": bool(result.get("coherent")),
            "clock_channel_spread_ppm": finite(result.get("clock_channel_spread_ppm")),
            "channels": {
                ch: {
                    "f0_hz": finite(values.get("f0")),
                    "ppm": finite(values.get("ppm")),
                    "level_dbfs": finite(values.get("level_dbfs")),
                    "snr_db": finite(values.get("snr_db")),
                    "coh_err_bins": finite(values.get("coh_err_bins")),
                    "clock_ok": bool(values.get("clock_ok")),
                    "signal_ok": bool(values.get("signal_ok")),
                    "coherent": bool(values.get("coherent")),
                } for ch, values in result.get("ch", {}).items()
            },
            "warnings": list(result.get("warnings", [])),
        }
    except Exception as exc:
        row["clock_check"] = "raised"
        row["error"] = f"{type(exc).__name__}: {exc}"

    if "window" in captured:
        win = captured["window"]
        row["frame_occupancy"] = {
            "L": frame_metrics(win[:, 0], FS),
            "R": frame_metrics(win[:, 1], FS),
        }
        row["per_block_tone_analysis"] = per_block_metrics(rig, win, k)
        block_snr = [
            row["per_block_tone_analysis"].get(f"block_{i}", {}).get(ch, {}).get("local_snr_db")
            for i in (1, 2) for ch in ("L", "R")
        ]
        finite_snr = [v for v in block_snr if v is not None]
        row["candidate_block_snr_min_db"] = min(finite_snr) if finite_snr else None
        for threshold in (20.0, 30.0, 40.0):
            row[f"candidate_both_blocks_snr_ge_{int(threshold)}db"] = (
                len(finite_snr) == 4 and all(v >= threshold for v in finite_snr)
            )
        occ90 = [
            row["frame_occupancy"][ch]["q90_relative_above_12db_fraction"]
            for ch in ("L", "R")
        ]
        row["candidate_both_channels_q90_occupancy_ge_90pct"] = all(v >= 0.90 for v in occ90)
        row["candidate_both_channels_q90_occupancy_ge_95pct"] = all(v >= 0.95 for v in occ90)

    row["capture"] = backend.capture_meta
    return row


def main():
    rows = [run_case(cfg, i) for i, cfg in enumerate(CASES)]
    summary = {
        "purpose": (
            "Compare real production clock_check acceptance with per-block SNR "
            "and upper-quantile-relative frame occupancy on continuous, gated, "
            "pulsed and amplitude-modulated synthetic pilots."
        ),
        "fs_hz": FS,
        "window_s": WINDOW_S,
        "blocks": 2,
        "settle_s": SETTLE_S,
        "pilot_hz": 1000.0,
        "pilot_level_dbfs": PILOT_DBFS,
        "case_count": len(rows),
        "rig_run_counts": {
            s: sum(r["rig_run"] == s for r in rows)
            for s in sorted({r["rig_run"] for r in rows})
        },
        "clock_check_counts": {
            s: sum(r["clock_check"] == s for r in rows)
            for s in sorted({r["clock_check"] for r in rows})
        },
        "returned_ok_cases": [r["case"] for r in rows if r.get("clock_result", {}).get("ok")],
        "candidate_gate_counts": {
            key: sum(bool(r.get(key)) for r in rows if key in r)
            for key in (
                "candidate_both_blocks_snr_ge_20db",
                "candidate_both_blocks_snr_ge_30db",
                "candidate_both_blocks_snr_ge_40db",
                "candidate_both_channels_q90_occupancy_ge_90pct",
                "candidate_both_channels_q90_occupancy_ge_95pct",
            )
        },
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
    }
    output = {"summary": summary, "per_case": rows}
    out_path = Path(__file__).resolve().parent / "v2220-clock-occupancy-diagnostics.json"
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {out_path}", flush=True)


if __name__ == "__main__":
    main()
