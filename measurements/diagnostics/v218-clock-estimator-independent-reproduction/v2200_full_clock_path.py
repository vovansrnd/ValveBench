#!/usr/bin/env python3
"""v2.20.0 full production-path synthetic test for the clock preflight.

Calls the real Rig.run() and measure.clock_check() with a deterministic
synthetic capture backend. The backend injects only time-domain dropouts,
gaps, or a level sag into the returned capture. No audio device, playback,
recording, DUT, WAV, or production-source modification is used.
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
DELAY_S = 0.137
SETTLE_S = 0.4
TAIL_S = 0.8
NOISE_DBFS = -110.0
SEED_BASE = 220001

CASES = (
    {"name": "clean", "kind": "none"},
    {"name": "occupancy_40_percent_front_loaded", "kind": "quiet", "start_s": 0.8, "end_s": 2.0},
    {"name": "occupancy_50_percent_front_loaded", "kind": "quiet", "start_s": 1.0, "end_s": 2.0},
    {"name": "tail_dropout_25_percent", "kind": "quiet", "start_s": 1.5, "end_s": 2.0},
    {"name": "head_dropout_500ms", "kind": "quiet", "start_s": 0.0, "end_s": 0.5},
    {"name": "gap_40ms", "kind": "quiet", "start_s": 0.4, "end_s": 0.44},
    {"name": "gap_60ms", "kind": "quiet", "start_s": 0.4, "end_s": 0.46},
    {"name": "sag_200ms_minus6db", "kind": "scale", "start_s": 0.4, "end_s": 0.6, "factor": 0.5},
)


def amp(dbfs: float) -> float:
    return 10.0 ** (float(dbfs) / 20.0)


def finite(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


class SyntheticCaptureBackend:
    """Backend contract shim; output is the input program plus controlled capture faults."""

    name = "v2200-synthetic-capture"

    def __init__(self, case: dict, seed: int):
        self.case = case
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

        delay = int(round(DELAY_S * self.fs))
        tail_n = int(round(max(0.0, float(tail)) * self.fs))
        extra_n = int(round(0.3 * self.fs))
        n = delay + len(program) + tail_n + extra_n

        rng = np.random.default_rng(self.seed)
        noise = (amp(NOISE_DBFS) / math.sqrt(2.0)) * rng.standard_normal((n, 2))
        rec = noise.copy()
        rec[delay:delay + len(program)] += program

        # clock_check() uses a single segment with 0.4 s settling and a 2 s window.
        measure_start = delay + int(round(SETTLE_S * self.fs))
        a = measure_start + int(round(float(self.case.get("start_s", 0.0)) * self.fs))
        b = measure_start + int(round(float(self.case.get("end_s", 0.0)) * self.fs))
        kind = self.case["kind"]
        if kind == "quiet":
            rec[a:b] = noise[a:b]
        elif kind == "scale":
            factor = float(self.case["factor"])
            rec[a:b] = noise[a:b] + factor * (rec[a:b] - noise[a:b])
        elif kind != "none":
            raise ValueError(f"unknown injection kind: {kind}")

        self.capture_meta = {
            "capture_samples": int(n),
            "program_samples": int(len(program)),
            "delay_samples": int(delay),
            "measurement_start_sample_nominal": int(measure_start),
            "fault_start_sample": int(a),
            "fault_end_sample": int(b),
            "fault_start_s_in_measurement_window": float(self.case.get("start_s", 0.0)),
            "fault_end_s_in_measurement_window": float(self.case.get("end_s", 0.0)),
        }
        return rec


def run_case(case: dict, index: int):
    backend = SyntheticCaptureBackend(case, SEED_BASE + index)
    rig = Rig(backend=backend, fs=FS, df=1.0, blocks=2)
    record = {
        "case": case["name"],
        "fault": {k: v for k, v in case.items() if k not in ("name", "kind")},
        "backend": backend.name,
        "rig_run": "not_started",
        "clock_check": "not_started",
        "onset_sample": None,
        "onset_s": None,
        "error": None,
    }
    rig.open()

    original_onset = rig._onset
    def tracked_onset(rec, tone_hz=None):
        onset = original_onset(rec, tone_hz=tone_hz)
        record["onset_sample"] = int(onset)
        record["onset_s"] = float(onset / rig.fs)
        return onset
    rig._onset = tracked_onset

    original_run = rig.run
    def tracked_run(segs):
        try:
            wins, warnings = original_run(segs)
            record["rig_run"] = "pass"
            record["window_shape"] = list(wins[0].shape) if wins else []
            record["run_warnings"] = list(warnings)
            return wins, warnings
        except Exception as exc:
            record["rig_run"] = "reject"
            record["run_error"] = f"{type(exc).__name__}: {exc}"
            raise
    rig.run = tracked_run

    try:
        result = clock_check(rig, level_dbfs=-72.0)
        record["clock_check"] = "returned"
        record["clock_result"] = {
            "ok": bool(result.get("ok")),
            "clock_ok": bool(result.get("clock_ok")),
            "clock_ppm_ok": bool(result.get("clock_ppm_ok")),
            "clock_consistent": bool(result.get("clock_consistent")),
            "signal_ok": bool(result.get("signal_ok")),
            "coherent": bool(result.get("coherent")),
            "clock_channel_spread_ppm": finite(result.get("clock_channel_spread_ppm")),
            "channels": {
                ch: {
                    key: finite(values.get(key))
                    for key in ("f0", "ppm", "level_dbfs", "snr_db", "coh_err_bins")
                } | {
                    key: bool(values.get(key))
                    for key in ("clock_ok", "signal_ok", "coherent")
                }
                for ch, values in result.get("ch", {}).items()
            },
            "warnings": list(result.get("warnings", [])),
        }
    except Exception as exc:
        record["clock_check"] = "raised"
        record["error"] = f"{type(exc).__name__}: {exc}"

    record["capture"] = backend.capture_meta
    record["backend_play_calls"] = backend.play_calls
    return record


def main():
    rows = [run_case(case, i) for i, case in enumerate(CASES)]
    summary = {
        "purpose": (
            "Observe the complete production Rig.run() -> _onset -> stability guard -> "
            "dsp.analyze_tone -> clock_check() path under deterministic synthetic capture faults."
        ),
        "fs_hz": FS,
        "window_s": 2.0,
        "blocks": 2,
        "settle_s": SETTLE_S,
        "pilot_hz": 1000.0,
        "pilot_level_dbfs": -72.0,
        "case_count": len(rows),
        "rig_run_counts": {
            state: sum(row["rig_run"] == state for row in rows)
            for state in sorted({row["rig_run"] for row in rows})
        },
        "clock_check_counts": {
            state: sum(row["clock_check"] == state for row in rows)
            for state in sorted({row["clock_check"] for row in rows})
        },
        "occupancy_40_percent": next(
            row for row in rows if row["case"] == "occupancy_40_percent_front_loaded"
        ),
        "interpretation": (
            "Synthetic diagnostic only. A returned clock_check result does not by itself "
            "validate hardware behavior; compare Rig rejection, signal_ok, clock_ok and "
            "overall ok separately. No thresholds or production code are changed."
        ),
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
    }
    output = {"summary": summary, "per_case": rows}
    out_path = Path(__file__).resolve().parent / "v2200-full-clock-path.json"
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {out_path}", flush=True)


if __name__ == "__main__":
    main()
