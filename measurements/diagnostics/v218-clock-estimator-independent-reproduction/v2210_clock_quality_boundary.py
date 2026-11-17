#!/usr/bin/env python3
"""v2.21.0 full Rig/clock_check acceptance matrix for intermittent signal windows.

Uses the real production Rig.run() and clock_check() with a deterministic,
offline synthetic capture backend. No devices, playback, recording, DUT, WAV,
or production-code modifications.
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
SETTLE_S = 0.4
DELAY_S = 0.137
NOISE_DBFS = -110.0
SEED_BASE = 221001
WINDOW_S = 2.0


def amp(dbfs: float) -> float:
    return 10.0 ** (float(dbfs) / 20.0)


def make_case(name: str, intervals):
    total = sum(float(b) - float(a) for a, b in intervals)
    if any(a < 0 or b > WINDOW_S or b <= a for a, b in intervals):
        raise ValueError(f"invalid interval in {name}: {intervals}")
    return {
        "name": name,
        "active_intervals_s": [[float(a), float(b)] for a, b in intervals],
        "active_fraction": total / WINDOW_S,
    }


CASES = [make_case("clean_100pct", [(0.0, 2.0)])]
for duty in (0.20, 0.30, 0.35, 0.40, 0.45, 0.49, 0.50, 0.51, 0.55, 0.60, 0.70, 0.80, 0.90):
    CASES.append(make_case(
        f"front_contiguous_{int(duty * 100):02d}pct",
        [(0.0, WINDOW_S * duty)]
    ))
CASES.extend([
    make_case("40pct_center", [(0.6, 1.4)]),
    make_case("40pct_back", [(1.2, 2.0)]),
    make_case("40pct_split_across_blocks", [(0.0, 0.4), (1.0, 1.4)]),
    make_case("50pct_split_across_blocks", [(0.0, 0.5), (1.0, 1.5)]),
    make_case("60pct_split_across_blocks", [(0.0, 0.6), (1.0, 1.6)]),
    make_case("40pct_two_bursts_first_block", [(0.0, 0.4), (0.6, 1.0)]),
    make_case("40pct_four_bursts", [(0.0, 0.2), (0.45, 0.65), (0.9, 1.1), (1.35, 1.55)]),
])


def finite(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


class SyntheticCaptureBackend:
    name = "v2210-synthetic-window-mask"

    def __init__(self, case, seed):
        self.case = case
        self.seed = int(seed)
        self.fs = FS
        self.notes = []
        self.capture_meta = {}
        self.play_calls = 0

    def open(self, fs):
        self.fs = int(fs)

    def play_record(self, out, tail=0.8):
        self.play_calls += 1
        program = np.asarray(out, dtype=np.float64)
        if program.ndim == 1:
            program = np.column_stack((program, program))
        if program.ndim != 2 or program.shape[1] != 2:
            raise ValueError(f"unexpected program shape: {program.shape}")

        delay_n = int(round(DELAY_S * self.fs))
        settle_n = int(round(SETTLE_S * self.fs))
        window_n = int(round(WINDOW_S * self.fs))
        tail_n = int(round(max(0.0, float(tail)) * self.fs))
        n = delay_n + len(program) + tail_n + int(round(0.3 * self.fs))

        rng = np.random.default_rng(self.seed)
        noise = amp(NOISE_DBFS) / math.sqrt(2.0) * rng.standard_normal((n, 2))
        rec = noise.copy()
        rec[delay_n:delay_n + len(program)] += program

        nominal_start = delay_n + settle_n
        nominal_end = nominal_start + window_n
        source_start = settle_n
        source_end = source_start + window_n
        if source_end > len(program) or nominal_end > n:
            raise RuntimeError("synthetic capture is shorter than requested analysis window")

        # Keep the settling tone intact; mask only the intended 2-second window.
        source_window = program[source_start:source_end].copy()
        rec[nominal_start:nominal_end] = noise[nominal_start:nominal_end]
        active_samples = 0
        for start_s, end_s in self.case["active_intervals_s"]:
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
            "settle_samples": int(settle_n),
            "nominal_window_start_sample": int(nominal_start),
            "nominal_window_samples": int(window_n),
            "active_samples": int(active_samples),
            "active_fraction": float(active_samples / window_n),
        }
        return rec


def run_case(case, index):
    backend = SyntheticCaptureBackend(case, SEED_BASE + index)
    rig = Rig(backend=backend, fs=FS, df=1.0, blocks=2)
    row = {
        "case": case["name"],
        "active_intervals_s": case["active_intervals_s"],
        "active_fraction": case["active_fraction"],
        "rig_run": "not_started",
        "clock_check": "not_started",
        "onset_sample": None,
        "onset_s": None,
        "onset_shift_from_dac_delay_s": None,
        "selected_window_start_s": None,
        "run_error": None,
        "error": None,
    }
    rig.open()

    original_onset = rig._onset

    def tracked_onset(rec, tone_hz=None):
        onset = original_onset(rec, tone_hz=tone_hz)
        row["onset_sample"] = int(onset)
        row["onset_s"] = float(onset / rig.fs)
        row["onset_shift_from_dac_delay_s"] = float(onset / rig.fs - DELAY_S)
        row["selected_window_start_s"] = float(
            (onset + int(round(SETTLE_S * rig.fs))) / rig.fs
        )
        return onset

    rig._onset = tracked_onset

    tone_metrics = {}
    original_tone_an = rig._tone_an

    def tracked_tone_an(win, ch, k, nharm=9, coherent_line_floors=None):
        result = original_tone_an(
            win, ch, k, nharm=nharm,
            coherent_line_floors=coherent_line_floors
        )
        label = "L" if ch == 0 else "R"
        tone_metrics[label] = {
            "f0_hz": finite(result.get("f0")),
            "level_dbfs": finite(result.get("level_dbfs")),
            "local_snr_db": finite(result.get("local_snr_db")),
            "phase_coherence": finite(result.get("phase_coherence")),
            "phase_spread_deg": finite(result.get("phase_spread_deg")),
            "coh_err_bins": finite(result.get("coh_err_bins")),
            "warnings": list(result.get("warnings", [])),
        }
        return result

    rig._tone_an = tracked_tone_an

    original_run = rig.run

    def tracked_run(segs):
        try:
            wins, warns = original_run(segs)
            row["rig_run"] = "pass"
            row["window_shape"] = list(wins[0].shape) if wins else []
            row["run_warnings"] = list(warns)
            return wins, warns
        except Exception as exc:
            row["rig_run"] = "reject"
            row["run_error"] = f"{type(exc).__name__}: {exc}"
            raise

    rig.run = tracked_run

    try:
        result = clock_check(rig, level_dbfs=-72.0)
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
                }
                for ch, values in result.get("ch", {}).items()
            },
            "warnings": list(result.get("warnings", [])),
        }
    except Exception as exc:
        row["clock_check"] = "raised"
        row["error"] = f"{type(exc).__name__}: {exc}"

    row["tone_analysis"] = tone_metrics
    row["capture"] = backend.capture_meta
    row["backend_play_calls"] = backend.play_calls
    return row


def main():
    rows = [run_case(case, i) for i, case in enumerate(CASES)]
    summary = {
        "purpose": (
            "Characterize when the real production Rig.run() stability guard and "
            "clock_check() accept or reject intermittent 1 kHz pilot occupancy."
        ),
        "fs_hz": FS,
        "window_s": WINDOW_S,
        "blocks": 2,
        "settle_s": SETTLE_S,
        "pilot_hz": 1000.0,
        "pilot_level_dbfs": -72.0,
        "case_count": len(rows),
        "rig_run_counts": {
            state: sum(r["rig_run"] == state for r in rows)
            for state in sorted({r["rig_run"] for r in rows})
        },
        "clock_check_counts": {
            state: sum(r["clock_check"] == state for r in rows)
            for state in sorted({r["clock_check"] for r in rows})
        },
        "returned_ok_cases": [
            r["case"] for r in rows if r.get("clock_result", {}).get("ok")
        ],
        "front_contiguous_boundary": [
            {
                "active_fraction": r["active_fraction"],
                "rig_run": r["rig_run"],
                "clock_check": r["clock_check"],
                "ok": r.get("clock_result", {}).get("ok"),
                "onset_shift_s": r["onset_shift_from_dac_delay_s"],
            }
            for r in rows if r["case"].startswith("front_contiguous_")
        ],
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
    }
    output = {"summary": summary, "per_case": rows}
    out_path = Path(__file__).resolve().parent / "v2210-clock-quality-boundary.json"
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {out_path}", flush=True)


if __name__ == "__main__":
    main()
