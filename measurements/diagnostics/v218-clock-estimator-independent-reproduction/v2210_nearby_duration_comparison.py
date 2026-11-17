#!/usr/bin/env python3
"""v2.21.0 compare v2.18.8 nearby-line primary-fit bias at 2 s vs 4 s.

All LSQ calls use the unchanged frozen v2.18.8 estimator. The test compares
paired pilot-only baselines and independent/same-phase nearby components.
No playback, audio hardware, DUT, WAV, or production-code change.
"""
from __future__ import annotations

import json
import math
import platform
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import scipy

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from v2188_estimators import est_lsq

OUT = Path(__file__).resolve().parent
FS = 96000.0
DURATIONS_S = (2.0, 4.0)
PILOT_NOM = 1000.0
PILOT_TRUE = PILOT_NOM * (1.0 - 23.5e-6)
PILOT_DBFS = -72.0
NOISE_DBFS = -96.0
OFFSETS_HZ = (0.25, 0.35, 0.40, 0.50, 0.75, 1.00)
LINE_LEVELS_DBC = (-10.0, -20.0)
PHASE_MODES = ("same_initial_phase", "independent_phase")
TRIALS_PER_ABS_OFFSET_PHASE = 16  # eight positive and eight negative offsets
SEED_BASE = 221100


def amp(dbfs: float) -> float:
    return 10.0 ** (float(dbfs) / 20.0)


def white_noise(n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return amp(NOISE_DBFS) / math.sqrt(2.0) * rng.standard_normal(n)


def make_pilot(n: int, seed: int):
    t = np.arange(n, dtype=np.float64) / FS
    phase = 2.0 * math.pi * ((seed * 37) % 997) / 997.0
    x = amp(PILOT_DBFS) * np.sin(2.0 * math.pi * PILOT_TRUE * t + phase)
    return x, phase, t


def finite(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def percentile(values, p):
    vals = [float(x) for x in values if x is not None and math.isfinite(float(x))]
    return float(np.percentile(vals, p)) if vals else None


def summarize(rows):
    output = {}
    key_groups = defaultdict(list)
    for row in rows:
        if row["kind"] == "nearby_line":
            key = (
                row["duration_s"], row["phase_mode"],
                row["delta_abs_hz"], row["line_dbc"]
            )
            key_groups[key].append(row)

    for key, group in sorted(key_groups.items()):
        shift_hz = [r["induced_shift_hz"] for r in group]
        shift_abs = [abs(x) for x in shift_hz if x is not None]
        fit_abs = [
            abs(r["fit_error_ppm"]) for r in group
            if r["fit_error_ppm"] is not None
        ]
        output["%gs %s d%.2f %+.0fdBc" % key] = {
            "n": len(group),
            "positive_trials": sum(r["delta_hz"] > 0 for r in group),
            "negative_trials": sum(r["delta_hz"] < 0 for r in group),
            "nearby_probe_hits": sum(r["nearby_detected"] for r in group),
            "status_counts": {
                s: sum(r["status"] == s for r in group)
                for s in sorted({r["status"] for r in group})
            },
            "induced_shift_abs_median_hz": percentile(shift_abs, 50),
            "induced_shift_abs_p90_hz": percentile(shift_abs, 90),
            "induced_shift_abs_max_hz": max(shift_abs) if shift_abs else None,
            "induced_shift_abs_max_ppm": (
                max(shift_abs) / PILOT_TRUE * 1e6 if shift_abs else None
            ),
            "fit_error_abs_p90_ppm": percentile(fit_abs, 90),
            "fit_error_abs_max_ppm": max(fit_abs) if fit_abs else None,
        }

    paired = defaultdict(dict)
    for row in rows:
        if row["kind"] == "nearby_line" and row["induced_shift_hz"] is not None:
            pair_key = (
                row["phase_mode"], row["delta_abs_hz"], row["line_dbc"],
                row["trial_seed"], row["delta_hz"] > 0
            )
            paired[pair_key][row["duration_s"]] = abs(row["induced_shift_hz"])
    reductions = defaultdict(list)
    for key, values in paired.items():
        if 2.0 in values and 4.0 in values:
            before, after = values[2.0], values[4.0]
            reductions[(key[0], key[1], key[2])].append({
                "ratio": after / before if before > 1e-15 else None,
                "shift_2s_ppm": before / PILOT_TRUE * 1e6,
                "shift_4s_ppm": after / PILOT_TRUE * 1e6,
            })
    reduction_summary = {}
    for key, group in sorted(reductions.items()):
        ratios = [x["ratio"] for x in group if x["ratio"] is not None]
        before = [x["shift_2s_ppm"] for x in group]
        after = [x["shift_4s_ppm"] for x in group]
        reduction_summary["%s d%.2f %+.0fdBc" % key] = {
            "paired_n": len(group),
            "median_abs_shift_2s_ppm": percentile(before, 50),
            "median_abs_shift_4s_ppm": percentile(after, 50),
            "median_4s_to_2s_ratio": percentile(ratios, 50),
            "p90_4s_to_2s_ratio": percentile(ratios, 90),
        }

    baseline = {}
    for duration in DURATIONS_S:
        group = [
            r for r in rows
            if r["kind"] == "pilot_only_baseline" and r["duration_s"] == duration
        ]
        errs = [
            abs(r["fit_error_ppm"]) for r in group
            if r["fit_error_ppm"] is not None
        ]
        baseline[str(duration)] = {
            "n": len(group),
            "fit_error_abs_p90_ppm": percentile(errs, 90),
            "fit_error_abs_max_ppm": max(errs) if errs else None,
        }
    return {
        "trial_count": len(rows),
        "pilot_only_baselines": baseline,
        "nearby_line_cells": output,
        "paired_duration_comparison": reduction_summary,
    }


def main():
    rows = []
    trial_id = 0
    baseline_cache = {}

    for duration_s in DURATIONS_S:
        n = int(round(FS * duration_s))
        for phase_mode in PHASE_MODES:
            for delta_abs in OFFSETS_HZ:
                for trial in range(TRIALS_PER_ABS_OFFSET_PHASE):
                    seed = SEED_BASE + (
                        0 if phase_mode == PHASE_MODES[0] else 10000
                    ) + int(round(delta_abs * 1000)) * 100 + trial
                    p, pilot_phase, _ = make_pilot(n, seed)
                    noise = white_noise(n, seed + 500000)
                    result = est_lsq(p + noise, FS, PILOT_NOM)
                    f_est = finite(result.get("f_est"))
                    baseline_cache[(duration_s, phase_mode, delta_abs, trial)] = f_est
                    err = f_est - PILOT_TRUE if f_est is not None else None
                    rows.append({
                        "trial": trial_id,
                        "kind": "pilot_only_baseline",
                        "duration_s": duration_s,
                        "phase_mode": phase_mode,
                        "delta_abs_hz": delta_abs,
                        "trial_seed": seed,
                        "f_est_hz": f_est,
                        "fit_error_hz": err,
                        "fit_error_ppm": err / PILOT_TRUE * 1e6 if err is not None else None,
                        "status": result.get("status"),
                        "nearby_detected": result.get("nearby_tone_hz") is not None,
                    })
                    trial_id += 1

        for phase_mode in PHASE_MODES:
            for delta_abs in OFFSETS_HZ:
                for line_dbc in LINE_LEVELS_DBC:
                    for trial in range(TRIALS_PER_ABS_OFFSET_PHASE):
                        seed = SEED_BASE + (
                            0 if phase_mode == PHASE_MODES[0] else 10000
                        ) + int(round(delta_abs * 1000)) * 100 + trial
                        p, pilot_phase, t = make_pilot(n, seed)
                        noise = white_noise(n, seed + 500000)
                        rng = np.random.default_rng(seed + 900000)
                        line_phase = (
                            pilot_phase if phase_mode == "same_initial_phase"
                            else float(rng.uniform(0.0, 2.0 * math.pi))
                        )
                        delta = (-1.0 if trial % 2 else +1.0) * delta_abs
                        line_hz = PILOT_TRUE + delta
                        line = amp(PILOT_DBFS) * amp(line_dbc) * np.sin(
                            2.0 * math.pi * line_hz * t + line_phase
                        )
                        result = est_lsq(p + line + noise, FS, PILOT_NOM)
                        f_est = finite(result.get("f_est"))
                        base_f = baseline_cache[(duration_s, phase_mode, delta_abs, trial)]
                        shift = (
                            f_est - base_f
                            if f_est is not None and base_f is not None else None
                        )
                        err = f_est - PILOT_TRUE if f_est is not None else None
                        rows.append({
                            "trial": trial_id,
                            "kind": "nearby_line",
                            "duration_s": duration_s,
                            "phase_mode": phase_mode,
                            "delta_abs_hz": delta_abs,
                            "delta_hz": delta,
                            "line_dbc": line_dbc,
                            "line_hz": line_hz,
                            "trial_seed": seed,
                            "f_est_hz": f_est,
                            "fit_error_hz": err,
                            "fit_error_ppm": err / PILOT_TRUE * 1e6 if err is not None else None,
                            "baseline_f_est_hz": base_f,
                            "induced_shift_hz": shift,
                            "status": result.get("status"),
                            "nearby_detected": result.get("nearby_tone_hz") is not None,
                            "nearby_hz": finite(result.get("nearby_tone_hz")),
                            "nearby_db": finite(result.get("nearby_tone_db")),
                        })
                        trial_id += 1
            print(
                f"duration={duration_s:g}s phase={phase_mode}: {trial_id} rows",
                flush=True
            )

    output = {
        "summary": {
            "purpose": (
                "Determine whether doubling the LSQ analysis duration from 2 s to 4 s "
                "reduces primary-frequency bias caused by close independent components."
            ),
            "fs_hz": FS,
            "durations_s": list(DURATIONS_S),
            "pilot_nominal_hz": PILOT_NOM,
            "pilot_true_hz": PILOT_TRUE,
            "pilot_level_dbfs": PILOT_DBFS,
            "noise_level_dbfs": NOISE_DBFS,
            "offsets_hz": list(OFFSETS_HZ),
            "line_levels_dbc": list(LINE_LEVELS_DBC),
            "phase_modes": list(PHASE_MODES),
            "trials_per_abs_offset_phase": TRIALS_PER_ABS_OFFSET_PHASE,
            "trials_per_signed_offset_cell": TRIALS_PER_ABS_OFFSET_PHASE // 2,
            "results": summarize(rows),
            "interpretation": (
                "This validates only the frozen diagnostic LSQ estimator. "
                "It is not a production clock_check test and does not authorize "
                "a production capture-duration or gate change."
            ),
            "environment": {
                "python": platform.python_version(),
                "numpy": np.__version__,
                "scipy": scipy.__version__,
            },
        },
        "per_trial": rows,
    }
    out_path = OUT / "v2210-nearby-duration-comparison.json"
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    print(json.dumps(output["summary"], ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {out_path}", flush=True)


if __name__ == "__main__":
    main()
