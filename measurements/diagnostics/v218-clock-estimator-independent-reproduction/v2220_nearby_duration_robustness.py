#!/usr/bin/env python3
"""v2.22.0 paired 2/3/4/6 s robustness test for nearby-line LSQ bias.

All fits use the unchanged frozen v2.18.8 estimator. Every duration uses the
same pilot phase, noise seed, relative spur phase, offset sign and amplitude
for each paired trial. Both phase-locked and random-phase populations are
kept separate. No playback, hardware, DUT, WAV, or production edits.
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
DURATIONS_S = (2.0, 3.0, 4.0, 6.0)
PILOT_NOM = 1000.0
PILOT_TRUE = PILOT_NOM * (1.0 - 23.5e-6)
PILOT_DBFS = -72.0
NOISE_DBFS = -96.0
OFFSETS_HZ = (0.25, 0.35, 0.40, 0.50, 0.75)
LINE_LEVELS_DBC = (-10.0, -20.0)
PHASE_MODES = ("same_initial_phase", "independent_phase")
TRIALS_PER_ABS_OFFSET_PHASE = 16  # eight positive and eight negative signs
SEED_BASE = 222200


def amp(dbfs: float) -> float:
    return 10.0 ** (float(dbfs) / 20.0)


def noise(n: int, seed: int) -> np.ndarray:
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
    vals = [
        float(x) for x in values
        if x is not None and math.isfinite(float(x))
    ]
    return float(np.percentile(vals, p)) if vals else None


def summarize(rows):
    baseline = {}
    nearby_groups = defaultdict(list)
    for row in rows:
        if row["kind"] == "pilot_only_baseline":
            baseline.setdefault(row["duration_s"], []).append(row)
        elif row["kind"] == "nearby_line":
            nearby_groups[(
                row["duration_s"], row["phase_mode"],
                row["delta_abs_hz"], row["line_dbc"]
            )].append(row)

    baseline_summary = {}
    for duration in DURATIONS_S:
        group = baseline.get(duration, [])
        errors = [abs(r["fit_error_ppm"]) for r in group if r["fit_error_ppm"] is not None]
        baseline_summary[str(duration)] = {
            "n": len(group),
            "fit_error_abs_median_ppm": percentile(errors, 50),
            "fit_error_abs_p90_ppm": percentile(errors, 90),
            "fit_error_abs_max_ppm": max(errors) if errors else None,
            "status_counts": {
                s: sum(r["status"] == s for r in group)
                for s in sorted({r["status"] for r in group})
            },
        }

    cell_summary = {}
    for key, group in sorted(nearby_groups.items()):
        duration, phase, offset, level = key
        shifts = [
            abs(r["induced_shift_hz"]) for r in group
            if r["induced_shift_hz"] is not None
        ]
        fit = [
            abs(r["fit_error_ppm"]) for r in group
            if r["fit_error_ppm"] is not None
        ]
        cell_summary["%gs %s d%.2f %+.0fdBc" % key] = {
            "n": len(group),
            "positive_trials": sum(r["delta_hz"] > 0 for r in group),
            "negative_trials": sum(r["delta_hz"] < 0 for r in group),
            "nearby_probe_hits": sum(r["nearby_detected"] for r in group),
            "status_counts": {
                s: sum(r["status"] == s for r in group)
                for s in sorted({r["status"] for r in group})
            },
            "induced_shift_abs_median_ppm": (
                percentile(shifts, 50) / PILOT_TRUE * 1e6 if shifts else None
            ),
            "induced_shift_abs_p90_ppm": (
                percentile(shifts, 90) / PILOT_TRUE * 1e6 if shifts else None
            ),
            "induced_shift_abs_max_ppm": (
                max(shifts) / PILOT_TRUE * 1e6 if shifts else None
            ),
            "fit_error_abs_p90_ppm": percentile(fit, 90),
            "fit_error_abs_max_ppm": max(fit) if fit else None,
        }

    # Paired comparison against 2 s for each exact seed/sign/phase/offset/level.
    paired = defaultdict(dict)
    for row in rows:
        if row["kind"] != "nearby_line" or row["induced_shift_hz"] is None:
            continue
        key = (
            row["phase_mode"], row["delta_abs_hz"], row["line_dbc"],
            row["trial_seed"], row["delta_hz"] > 0
        )
        paired[key][row["duration_s"]] = abs(row["induced_shift_hz"])

    comparison = {}
    for phase in PHASE_MODES:
        subset = [
            (key, values) for key, values in paired.items()
            if key[0] == phase and 2.0 in values
        ]
        phase_result = {}
        for duration in DURATIONS_S[1:]:
            both = [(v[2.0], v[duration]) for _, v in subset if duration in v]
            better = sum(after < before for before, after in both)
            worse = sum(after > before for before, after in both)
            same = len(both) - better - worse
            before_ppm = [x[0] / PILOT_TRUE * 1e6 for x in both]
            after_ppm = [x[1] / PILOT_TRUE * 1e6 for x in both]
            phase_result[str(duration)] = {
                "paired_n": len(both),
                "improved_vs_2s": better,
                "worsened_vs_2s": worse,
                "unchanged_vs_2s": same,
                "median_abs_shift_2s_ppm": percentile(before_ppm, 50),
                "median_abs_shift_duration_ppm": percentile(after_ppm, 50),
                "p90_abs_shift_2s_ppm": percentile(before_ppm, 90),
                "p90_abs_shift_duration_ppm": percentile(after_ppm, 90),
            }
        comparison[phase] = phase_result

    cell_paired = {}
    for phase in PHASE_MODES:
        for offset in OFFSETS_HZ:
            for level in LINE_LEVELS_DBC:
                subset = [
                    v for key, v in paired.items()
                    if key[0] == phase and key[1] == offset and key[2] == level
                    and 2.0 in v
                ]
                if not subset:
                    continue
                row = {}
                for duration in DURATIONS_S[1:]:
                    both = [(v[2.0], v[duration]) for v in subset if duration in v]
                    better = sum(after < before for before, after in both)
                    worse = sum(after > before for before, after in both)
                    before_ppm = [x[0] / PILOT_TRUE * 1e6 for x in both]
                    after_ppm = [x[1] / PILOT_TRUE * 1e6 for x in both]
                    row[str(duration)] = {
                        "paired_n": len(both),
                        "improved": better,
                        "worsened": worse,
                        "median_abs_shift_2s_ppm": percentile(before_ppm, 50),
                        "median_abs_shift_duration_ppm": percentile(after_ppm, 50),
                    }
                cell_paired["%s d%.2f %+.0fdBc" % (phase, offset, level)] = row

    return {
        "pilot_only_baselines": baseline_summary,
        "nearby_line_cells": cell_summary,
        "paired_vs_2s": comparison,
        "paired_by_cell_vs_2s": cell_paired,
        "nearby_trials": sum(r["kind"] == "nearby_line" for r in rows),
    }


def main():
    rows = []
    trial_id = 0
    baseline_cache = {}

    for duration_s in DURATIONS_S:
        n = int(round(FS * duration_s))
        for phase_mode in PHASE_MODES:
            for offset in OFFSETS_HZ:
                for trial in range(TRIALS_PER_ABS_OFFSET_PHASE):
                    seed = (
                        SEED_BASE
                        + (0 if phase_mode == PHASE_MODES[0] else 10000)
                        + int(round(offset * 1000)) * 100
                        + trial
                    )
                    p, pilot_phase, _ = make_pilot(n, seed)
                    noise_data = noise(n, seed + 500000)
                    result = est_lsq(p + noise_data, FS, PILOT_NOM)
                    f_est = finite(result.get("f_est"))
                    baseline_cache[(duration_s, phase_mode, offset, trial)] = f_est
                    err = f_est - PILOT_TRUE if f_est is not None else None
                    rows.append({
                        "trial": trial_id,
                        "kind": "pilot_only_baseline",
                        "duration_s": duration_s,
                        "phase_mode": phase_mode,
                        "delta_abs_hz": offset,
                        "trial_index": trial,
                        "trial_seed": seed,
                        "f_est_hz": f_est,
                        "fit_error_hz": err,
                        "fit_error_ppm": err / PILOT_TRUE * 1e6 if err is not None else None,
                        "status": result.get("status"),
                        "nearby_detected": result.get("nearby_tone_hz") is not None,
                    })
                    trial_id += 1

        for phase_mode in PHASE_MODES:
            for offset in OFFSETS_HZ:
                for level in LINE_LEVELS_DBC:
                    for trial in range(TRIALS_PER_ABS_OFFSET_PHASE):
                        seed = (
                            SEED_BASE
                            + (0 if phase_mode == PHASE_MODES[0] else 10000)
                            + int(round(offset * 1000)) * 100
                            + trial
                        )
                        p, pilot_phase, t = make_pilot(n, seed)
                        noise_data = noise(n, seed + 500000)
                        rng = np.random.default_rng(seed + 900000)
                        line_phase = (
                            pilot_phase if phase_mode == "same_initial_phase"
                            else float(rng.uniform(0.0, 2.0 * math.pi))
                        )
                        delta = (-1.0 if trial % 2 else +1.0) * offset
                        line_hz = PILOT_TRUE + delta
                        line = amp(PILOT_DBFS) * amp(level) * np.sin(
                            2.0 * math.pi * line_hz * t + line_phase
                        )
                        result = est_lsq(p + line + noise_data, FS, PILOT_NOM)
                        f_est = finite(result.get("f_est"))
                        base_f = baseline_cache[(duration_s, phase_mode, offset, trial)]
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
                            "delta_abs_hz": offset,
                            "delta_hz": delta,
                            "line_dbc": level,
                            "line_hz": line_hz,
                            "trial_index": trial,
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

    summary = {
        "purpose": (
            "Map the duration dependence of primary-frequency bias under paired "
            "nearby-component records, extending the earlier 2 s / 4 s test."
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
        "trials_per_signed_offset_phase_level": TRIALS_PER_ABS_OFFSET_PHASE // 2,
        "results": summarize(rows),
        "interpretation": (
            "This validates only the frozen diagnostic LSQ estimator. Longer windows "
            "must not be assumed to reduce bias monotonically; inspect paired absolute "
            "shifts by phase, offset and level. No production capture-duration change "
            "is authorized."
        ),
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
    }
    output = {"summary": summary, "per_trial": rows}
    out_path = OUT / "v2220-nearby-duration-robustness.json"
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {out_path}", flush=True)


if __name__ == "__main__":
    main()
