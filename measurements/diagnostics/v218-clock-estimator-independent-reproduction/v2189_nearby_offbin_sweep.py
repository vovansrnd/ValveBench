#!/usr/bin/env python3
"""Coordinator-authored v2.18.9 nearby residual spectral-line validation.

Tests both signs and off-bin offsets at the standard 96 kHz capture rate.
The probe is evaluated as a local residual spectral-component detector;
the script does NOT claim that a detected sideband is an independent
oscillator. AM/FM controls are reported separately for that reason.
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
PILOT_LEVEL_DBFS = -72.0
NOISE_LEVEL_DBFS = -96.0
OFFSETS_HZ = (0.30, 0.35, 0.55, 0.75, 0.95, 1.05, 1.25, 1.45, 1.50)
LINE_LEVELS_DBC = (-10.0, -20.0, -30.0, -35.0, -40.0)
PHASE_MODES = ("same_initial_phase", "independent_phase")
TRIALS_PER_CELL = 8


def amp(dbfs: float) -> float:
    return 10.0 ** (float(dbfs) / 20.0)


def white_noise(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return amp(NOISE_LEVEL_DBFS) / math.sqrt(2.0) * rng.standard_normal(N)


def make_pilot(seed: int, phase: float | None = None) -> tuple[np.ndarray, float]:
    t = np.arange(N, dtype=np.float64) / FS
    if phase is None:
        phase = 2.0 * math.pi * ((seed * 37) % 997) / 997.0
    x = amp(PILOT_LEVEL_DBFS) * np.sin(
        2.0 * math.pi * PILOT_TRUE * t + phase
    )
    return x, phase


def run_estimator(x: np.ndarray) -> dict:
    return est_lsq(x, FS, PILOT_NOM)


def summarize(rows: list[dict]) -> dict:
    positives = [r for r in rows if r["kind"] == "injected_line"]
    controls = [r for r in rows if r["kind"] != "injected_line"]
    detected = [r for r in positives if r["detected"]]
    groups = defaultdict(list)
    for r in positives:
        key = (r["phase_mode"], r["delta_hz"], r["line_dbc"])
        groups[key].append(r)

    per_cell = {}
    for key, group in sorted(groups.items()):
        found = [r for r in group if r["detected"]]
        db_errors = [r["nearby_db"] - r["line_dbc"] for r in found
                     if r["nearby_db"] is not None]
        freq_errors = [r["nearby_hz"] - r["true_line_hz"] for r in found
                       if r["nearby_hz"] is not None]
        per_cell["%s d%+.2f %+.0fdBc" % key] = {
            "n": len(group),
            "detected": len(found),
            "frequency_error_median_hz": (float(np.median(freq_errors))
                                          if freq_errors else None),
            "frequency_error_max_abs_hz": (float(np.max(np.abs(freq_errors)))
                                           if freq_errors else None),
            "amplitude_error_median_db": (float(np.median(db_errors))
                                          if db_errors else None),
            "amplitude_within_2_5db": sum(abs(v) <= 2.5 for v in db_errors),
        }

    controls_by_class = {}
    for category in sorted({r["category"] for r in controls}):
        group = [r for r in controls if r["category"] == category]
        controls_by_class[category] = {
            "n": len(group),
            "probe_fired": sum(r["detected"] for r in group),
            "probe_quiet": sum(not r["detected"] for r in group),
        }

    return {
        "trials_total": len(rows),
        "injected_line_trials": len(positives),
        "injected_lines_detected": len(detected),
        "injected_lines_missed": len(positives) - len(detected),
        "controls": controls_by_class,
        "per_cell": per_cell,
    }


def main():
    rows = []
    seed_base = 218910
    trial_id = 0

    # Nearby tones: both frequency signs, off-bin offsets, two phase relations.
    for phase_mode in PHASE_MODES:
        for delta_abs in OFFSETS_HZ:
            for sign in (-1.0, +1.0):
                for line_dbc in LINE_LEVELS_DBC:
                    for tr in range(TRIALS_PER_CELL):
                        seed = seed_base + trial_id
                        rng = np.random.default_rng(seed)
                        pilot, pilot_phase = make_pilot(seed)
                        line_phase = (pilot_phase if phase_mode == "same_initial_phase"
                                      else float(rng.uniform(0.0, 2.0 * math.pi)))
                        delta = sign * delta_abs
                        line_hz = PILOT_TRUE + delta
                        line = (amp(PILOT_LEVEL_DBFS) * amp(line_dbc) *
                                np.sin(2.0 * math.pi * line_hz *
                                       (np.arange(N) / FS) + line_phase))
                        x = pilot + line + white_noise(seed + 100000)
                        qc = run_estimator(x)
                        nearby_hz = qc.get("nearby_tone_hz")
                        nearby_db = qc.get("nearby_tone_db")
                        detected = nearby_hz is not None
                        rows.append({
                            "trial": trial_id,
                            "kind": "injected_line",
                            "category": "nearby_line",
                            "phase_mode": phase_mode,
                            "delta_hz": delta,
                            "line_dbc": line_dbc,
                            "true_line_hz": line_hz,
                            "nearby_hz": nearby_hz,
                            "nearby_db": nearby_db,
                            "frequency_error_hz": (float(nearby_hz) - line_hz
                                                   if detected else None),
                            "amplitude_error_db": (float(nearby_db) - line_dbc
                                                   if detected and nearby_db is not None
                                                   else None),
                            "detected": detected,
                            "estimator_status": qc.get("status"),
                        })
                        trial_id += 1
        print(f"  phase={phase_mode}: {trial_id} total rows", flush=True)

    # Quiet controls: clean pilot and pilot plus broadband noise.
    for category in ("clean_pilot", "pilot_plus_noise"):
        for tr in range(24):
            seed = seed_base + 300000 + trial_id
            pilot, _ = make_pilot(seed)
            x = pilot if category == "clean_pilot" else pilot + white_noise(seed + 1)
            qc = run_estimator(x)
            nearby_hz = qc.get("nearby_tone_hz")
            rows.append({
                "trial": trial_id, "kind": "control", "category": category,
                "nearby_hz": nearby_hz, "nearby_db": qc.get("nearby_tone_db"),
                "detected": nearby_hz is not None,
                "estimator_status": qc.get("status"),
            })
            trial_id += 1

    # WAV-derived real profile control (same provenance JSON as v2.18.8).
    profile_path = OUT / "v2187-lsq-pilots.json"
    if profile_path.exists():
        profile = json.loads(profile_path.read_text(encoding="utf-8"))["measured_profile"]
        for tr in range(20):
            seed = seed_base + 350000 + trial_id
            rng = np.random.default_rng(seed)
            pilot, _ = make_pilot(seed)
            t = np.arange(N, dtype=np.float64) / FS
            x = pilot.copy()
            for fh, values in profile.items():
                a = float(values.get("amp_L", 0.0))
                if a > 0:
                    phase = float(rng.uniform(0.0, 2.0 * math.pi))
                    x += a * np.sin(2.0 * math.pi * float(fh) * t + phase)
            x += white_noise(seed + 1)
            qc = run_estimator(x)
            nearby_hz = qc.get("nearby_tone_hz")
            rows.append({
                "trial": trial_id, "kind": "control",
                "category": "measured_real_profile",
                "nearby_hz": nearby_hz, "nearby_db": qc.get("nearby_tone_db"),
                "detected": nearby_hz is not None,
                "estimator_status": qc.get("status"),
            })
            trial_id += 1

    # AM/FM controls deliberately produce sidebands; a probe hit is not
    # automatically a false positive for a spectral-component detector.
    t = np.arange(N, dtype=np.float64) / FS
    for mod_type in ("am", "fm"):
        for mod_hz in (0.5, 1.0, 1.4):
            for tr in range(16):
                seed = seed_base + 400000 + trial_id
                rng = np.random.default_rng(seed)
                phase = float(rng.uniform(0.0, 2.0 * math.pi))
                mod_phase = float(rng.uniform(0.0, 2.0 * math.pi))
                if mod_type == "am":
                    depth = 0.20
                    envelope = 1.0 + depth * np.cos(
                        2.0 * math.pi * mod_hz * t + mod_phase
                    )
                    x = amp(PILOT_LEVEL_DBFS) * envelope * np.sin(
                        2.0 * math.pi * PILOT_TRUE * t + phase
                    )
                else:
                    beta = 0.15
                    x = amp(PILOT_LEVEL_DBFS) * np.sin(
                        2.0 * math.pi * PILOT_TRUE * t
                        + beta * np.sin(2.0 * math.pi * mod_hz * t + mod_phase)
                        + phase
                    )
                x += white_noise(seed + 1)
                qc = run_estimator(x)
                nearby_hz = qc.get("nearby_tone_hz")
                rows.append({
                    "trial": trial_id, "kind": "modulation_control",
                    "category": mod_type.upper(),
                    "modulation_hz": mod_hz,
                    "nearby_hz": nearby_hz,
                    "nearby_db": qc.get("nearby_tone_db"),
                    "detected": nearby_hz is not None,
                    "estimator_status": qc.get("status"),
                })
                trial_id += 1

    summary = {
        "capture": {
            "fs_hz": FS, "duration_s": DURATION_S, "samples": N,
            "fft_bin_hz": 1.0 / DURATION_S,
            "pilot_nominal_hz": PILOT_NOM,
            "pilot_true_hz": PILOT_TRUE,
            "pilot_level_dbfs": PILOT_LEVEL_DBFS,
            "noise_level_dbfs": NOISE_LEVEL_DBFS,
        },
        "sweep": {
            "offsets_hz_abs": list(OFFSETS_HZ),
            "signs": [-1, +1],
            "line_levels_dbc": list(LINE_LEVELS_DBC),
            "phase_modes": list(PHASE_MODES),
            "trials_per_cell": TRIALS_PER_CELL,
        },
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
        "results": summarize(rows),
        "interpretation": (
            "A hit means a nearby residual spectral component was found. "
            "AM/FM controls may produce legitimate sidebands; this probe "
            "does not prove an independent oscillator."
        ),
    }
    output = {"summary": summary, "per_trial": rows}
    out_path = OUT / "v2189-nearby-offbin.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {out_path}", flush=True)


if __name__ == "__main__":
    main()
