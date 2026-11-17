#!/usr/bin/env python3
"""Coordinator-authored v2.20.0 expanded dense-offset Monte Carlo test of primary LSQ frequency bias.

Quantifies how a nearby residual component and AM/FM modulation change the
primary pilot-frequency estimate from the frozen v2.18.8 estimator.
No playback, audio devices, or DUT are used. The output distinguishes the
primary fitted frequency from the nearby residual peak metadata.
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
DURATION_S = 2.0
N = int(FS * DURATION_S)
PILOT_NOM = 1000.0
PILOT_TRUE = PILOT_NOM * (1.0 - 23.5e-6)
PILOT_DBFS = -72.0
NOISE_DBFS = -96.0
OFFSETS_HZ = (0.25, 0.30, 0.35, 0.40, 0.50, 0.65, 0.75, 1.00, 1.50)
LINE_LEVELS_DBC = (-10.0, -20.0, -30.0)
PHASE_MODES = ("same_initial_phase", "independent_phase")
TRIALS_PER_CELL = 20
SEED_BASE = 220100


def amp(dbfs: float) -> float:
    return 10.0 ** (float(dbfs) / 20.0)


def white_noise(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return amp(NOISE_DBFS) / math.sqrt(2.0) * rng.standard_normal(N)


def make_pilot(seed: int):
    t = np.arange(N, dtype=np.float64) / FS
    phase = 2.0 * math.pi * ((seed * 37) % 997) / 997.0
    x = amp(PILOT_DBFS) * np.sin(2.0 * math.pi * PILOT_TRUE * t + phase)
    return x, phase, t


def safe_float(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def ppm_error(f_est):
    if f_est is None:
        return None
    return (f_est / PILOT_TRUE - 1.0) * 1e6


def percentile(values, p):
    values = sorted(float(x) for x in values if x is not None and math.isfinite(float(x)))
    if not values:
        return None
    return float(np.percentile(values, p))


def status_counts(group):
    return {
        s: sum(row["status"] == s for row in group)
        for s in sorted({row["status"] for row in group})
    }


def summarize(rows):
    injected = [r for r in rows if r["kind"] == "nearby_line"]
    grouped = defaultdict(list)
    for row in injected:
        key = (row["phase_mode"], row["delta_hz"], row["line_dbc"])
        grouped[key].append(row)

    per_cell = {}
    for key, group in sorted(grouped.items()):
        fit_errors = [r["fit_error_hz"] for r in group]
        abs_fit_errors = [abs(x) for x in fit_errors if x is not None]
        induced = [r["induced_shift_hz"] for r in group]
        abs_induced = [abs(x) for x in induced if x is not None]
        per_cell["%s d%+.2f %+.0fdBc" % key] = {
            "n": len(group),
            "nearby_probe_hits": sum(r["nearby_detected"] for r in group),
            "nearby_probe_hit_status_counts": {
                s: sum(r["nearby_detected"] and r["status"] == s for r in group)
                for s in sorted({r["status"] for r in group})
            },
            "n_status_ok": sum(r["status"] == "ok" for r in group),
            "n_probe_hit_and_status_ok": sum(r["nearby_detected"] and r["status"] == "ok" for r in group),
            "status_counts": status_counts(group),
            "fit_error_median_hz": percentile(fit_errors, 50),
            "fit_error_abs_p90_hz": percentile(abs_fit_errors, 90),
            "fit_error_abs_max_hz": max(abs_fit_errors) if abs_fit_errors else None,
            "induced_shift_median_hz": percentile(induced, 50),
            "induced_shift_abs_p90_hz": percentile(abs_induced, 90),
            "induced_shift_abs_p90_hz": percentile(abs_induced, 90),
            "max_abs_induced_shift_hz": max(abs_induced) if abs_induced else None,
            "max_abs_induced_shift_ppm": (max(abs_induced) / PILOT_TRUE * 1e6) if abs_induced else None,
            "max_abs_induced_shift_ppm_when_probe_hit_and_status_ok": max(
                (abs(r["induced_shift_hz"]) / PILOT_TRUE * 1e6 for r in group
                 if r["induced_shift_hz"] is not None and r["nearby_detected"] and r["status"] == "ok"),
                default=None
            ),
            "max_abs_fit_error_ppm": max(
                (abs(r["fit_error_ppm"]) for r in group
                 if r["fit_error_ppm"] is not None), default=None
            ),
        }

    mod_rows = [r for r in rows if r["kind"] == "modulation_control"]
    modulation = {}
    for category in sorted({r["category"] for r in mod_rows}):
        group = [r for r in mod_rows if r["category"] == category]
        errs = [r["fit_error_hz"] for r in group]
        abs_errs = [abs(x) for x in errs if x is not None]
        modulation[category] = {
            "n": len(group),
            "nearby_probe_hits": sum(r["nearby_detected"] for r in group),
            "status_counts": status_counts(group),
            "fit_error_median_hz": percentile(errs, 50),
            "fit_error_abs_p90_hz": percentile(abs_errs, 90),
            "fit_error_abs_max_hz": max(abs_errs) if abs_errs else None,
        }

    baseline = [r for r in rows if r["kind"] == "pilot_only_baseline"]
    baseline_errors = [r["fit_error_hz"] for r in baseline]
    return {
        "trials_total": len(rows),
        "pilot_only_baseline": {
            "n": len(baseline),
            "fit_error_median_hz": percentile(baseline_errors, 50),
            "fit_error_abs_p90_hz": percentile(
                [abs(x) for x in baseline_errors if x is not None], 90
            ),
        },
        "nearby_line_trials": len(injected),
        "nearby_line_detected": sum(r["nearby_detected"] for r in injected),
        "nearby_line_cells": per_cell,
        "modulation_controls": modulation,
    }


def main():
    t = np.arange(N, dtype=np.float64) / FS
    rows = []
    trial_id = 0
    baseline_cache = {}

    # Paired baseline is cached per phase/offset/trial seed. Every injected
    # record therefore has a no-nearby-line baseline with identical pilot phase
    # and identical noise realization.
    for phase_mode in PHASE_MODES:
        for delta_abs in OFFSETS_HZ:
            for trial in range(TRIALS_PER_CELL):
                seed = SEED_BASE + (0 if phase_mode == PHASE_MODES[0] else 10000) \
                       + int(round(delta_abs * 1000)) * 100 + trial
                p, pilot_phase, _ = make_pilot(seed)
                noise = white_noise(seed + 500000)
                base_result = est_lsq(p + noise, FS, PILOT_NOM)
                base_f = safe_float(base_result.get("f_est"))
                baseline_cache[(phase_mode, delta_abs, trial)] = {
                    "f_est": base_f,
                    "status": base_result.get("status"),
                }
                rows.append({
                    "trial": trial_id,
                    "kind": "pilot_only_baseline",
                    "category": "pilot_only",
                    "phase_mode": phase_mode,
                    "delta_hz": delta_abs,
                    "line_dbc": None,
                    "seed": seed,
                    "f_est_hz": base_f,
                    "fit_error_hz": (base_f - PILOT_TRUE if base_f is not None else None),
                    "fit_error_ppm": ((base_f - PILOT_TRUE) / PILOT_TRUE * 1e6
                                      if base_f is not None else None),
                    "status": base_result.get("status"),
                    "nearby_detected": base_result.get("nearby_tone_hz") is not None,
                    "nearby_hz": safe_float(base_result.get("nearby_tone_hz")),
                    "nearby_db": safe_float(base_result.get("nearby_tone_db")),
                })
                trial_id += 1

    for phase_mode in PHASE_MODES:
        for delta_abs in OFFSETS_HZ:
            for line_dbc in LINE_LEVELS_DBC:
                for trial in range(TRIALS_PER_CELL):
                    seed = SEED_BASE + (0 if phase_mode == PHASE_MODES[0] else 10000) \
                           + int(round(delta_abs * 1000)) * 100 + trial
                    p, pilot_phase, _ = make_pilot(seed)
                    noise = white_noise(seed + 500000)
                    rng = np.random.default_rng(seed + 900000)
                    line_phase = (pilot_phase if phase_mode == "same_initial_phase"
                                  else float(rng.uniform(0.0, 2.0 * math.pi)))
                    delta = (-1.0 if trial % 2 else +1.0) * delta_abs
                    # Six trials per cell: three negative and three positive offsets.
                    line_hz = PILOT_TRUE + delta
                    line = amp(PILOT_DBFS) * amp(line_dbc) * np.sin(
                        2.0 * math.pi * line_hz * t + line_phase
                    )
                    result = est_lsq(p + line + noise, FS, PILOT_NOM)
                    f_est = safe_float(result.get("f_est"))
                    base_f = baseline_cache[(phase_mode, delta_abs, trial)]["f_est"]
                    shift = f_est - base_f if f_est is not None and base_f is not None else None
                    err = f_est - PILOT_TRUE if f_est is not None else None
                    rows.append({
                        "trial": trial_id,
                        "kind": "nearby_line",
                        "category": "nearby_independent_component",
                        "phase_mode": phase_mode,
                        "delta_hz": delta,
                        "delta_abs_hz": delta_abs,
                        "line_dbc": line_dbc,
                        "line_hz": line_hz,
                        "seed": seed,
                        "f_est_hz": f_est,
                        "fit_error_hz": err,
                        "fit_error_ppm": (err / PILOT_TRUE * 1e6 if err is not None else None),
                        "baseline_f_est_hz": base_f,
                        "induced_shift_hz": shift,
                        "status": result.get("status"),
                        "nearby_detected": result.get("nearby_tone_hz") is not None,
                        "nearby_hz": safe_float(result.get("nearby_tone_hz")),
                        "nearby_db": safe_float(result.get("nearby_tone_db")),
                        "line_qc_hz": safe_float(result.get("line_hz")),
                        "line_qc_dbc": safe_float(result.get("line_dbc")),
                    })
                    trial_id += 1
            print(f"  phase={phase_mode}, offset={delta_abs:.2f} Hz: "
                  f"{trial_id} rows", flush=True)

    # AM/FM are controls, not false positives: their spectral sidebands are real.
    for mod_type in ("AM", "FM"):
        for mod_hz in (0.30, 0.75, 1.40):
            for trial in range(12):
                seed = SEED_BASE + 700000 + (0 if mod_type == "AM" else 10000) \
                       + int(round(mod_hz * 1000)) * 100 + trial
                p, phase, _ = make_pilot(seed)
                noise = white_noise(seed + 500000)
                rng = np.random.default_rng(seed + 900000)
                mod_phase = float(rng.uniform(0.0, 2.0 * math.pi))
                if mod_type == "AM":
                    depth = 0.20
                    envelope = 1.0 + depth * np.cos(
                        2.0 * math.pi * mod_hz * t + mod_phase
                    )
                    x = amp(PILOT_DBFS) * envelope * np.sin(
                        2.0 * math.pi * PILOT_TRUE * t + phase
                    )
                else:
                    beta = 0.15
                    x = amp(PILOT_DBFS) * np.sin(
                        2.0 * math.pi * PILOT_TRUE * t
                        + beta * np.sin(2.0 * math.pi * mod_hz * t + mod_phase)
                        + phase
                    )
                result = est_lsq(x + noise, FS, PILOT_NOM)
                f_est = safe_float(result.get("f_est"))
                err = f_est - PILOT_TRUE if f_est is not None else None
                rows.append({
                    "trial": trial_id,
                    "kind": "modulation_control",
                    "category": mod_type,
                    "modulation_hz": mod_hz,
                    "seed": seed,
                    "f_est_hz": f_est,
                    "fit_error_hz": err,
                    "fit_error_ppm": (err / PILOT_TRUE * 1e6 if err is not None else None),
                    "status": result.get("status"),
                    "nearby_detected": result.get("nearby_tone_hz") is not None,
                    "nearby_hz": safe_float(result.get("nearby_tone_hz")),
                    "nearby_db": safe_float(result.get("nearby_tone_db")),
                })
                trial_id += 1

    summary = {
        "capture": {
            "fs_hz": FS,
            "duration_s": DURATION_S,
            "samples": N,
            "fft_bin_hz": 1.0 / DURATION_S,
            "pilot_nominal_hz": PILOT_NOM,
            "pilot_true_hz": PILOT_TRUE,
            "pilot_level_dbfs": PILOT_DBFS,
            "noise_level_dbfs": NOISE_DBFS,
            "offset_abs_hz": list(OFFSETS_HZ),
            "line_levels_dbc": list(LINE_LEVELS_DBC),
            "phase_modes": list(PHASE_MODES),
            "trials_per_abs_offset_phase_level": TRIALS_PER_CELL,
            "trials_per_signed_offset_phase_level": TRIALS_PER_CELL // 2,
        },
        "results": summarize(rows),
        "interpretation": (
            "The target is the primary LSQ fitted pilot frequency. A nearby residual "
            "peak is separate diagnostic metadata. AM/FM controls create legitimate "
            "sidebands and do not identify an independent oscillator."
        ),
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
    }
    output = {"summary": summary, "per_trial": rows}
    out_path = OUT / "v2200-nearby-primary-fit-bias.json"
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {out_path}", flush=True)


if __name__ == "__main__":
    main()
