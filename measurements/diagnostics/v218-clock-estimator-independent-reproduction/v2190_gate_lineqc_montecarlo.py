#!/usr/bin/env python3
"""Coordinator-authored v2.19.0 validation: Rig stability guard + residual line-QC.

No audio I/O. The stability guard is invoked directly on stereo synthetic
windows with the same 96 kHz / 2 s geometry used by the clock preflight.
The frozen v2.18.8 LSQ estimator is exercised on seeded line-free and
injected-spur populations. This is diagnostic validation only.
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

from valvebench.measure import Rig
from v2188_estimators import est_lsq

OUT = Path(__file__).resolve().parent
FS = 96000.0
DURATION_S = 2.0
N = int(FS * DURATION_S)
PILOT_NOM = 1000.0
PILOT_TRUE = PILOT_NOM * (1.0 - 23.5e-6)
PILOT_DBFS = -72.0
REPLAY_SEED0 = 218900
FOCUS_SEED0 = 219000
TRIALS_FOCUS = 30


def amp(dbfs: float) -> float:
    return 10.0 ** (float(dbfs) / 20.0)


def white_noise(dbfs: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return amp(dbfs) / math.sqrt(2.0) * rng.standard_normal(N)


def colored_noise(dbfs: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(N)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(N, 1.0 / FS)
    X /= np.sqrt(1.0 + (f / 250.0) ** 2)
    y = np.fft.irfft(X, n=N)
    rms = float(np.sqrt(np.mean(y * y)))
    target = amp(dbfs) / math.sqrt(2.0)
    return y * (target / max(rms, 1e-30))


def pilot(level_dbfs: float, seed: int) -> np.ndarray:
    t = np.arange(N, dtype=np.float64) / FS
    phase = 2.0 * math.pi * ((seed * 37) % 997) / 997.0
    return amp(level_dbfs) * np.sin(2.0 * math.pi * PILOT_TRUE * t + phase)


def finite(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def wilson(k: int, n: int, z: float = 1.96):
    if n <= 0:
        return None
    p = k / n
    den = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / den
    half = z * math.sqrt((p * (1.0 - p) / n) + z * z / (4.0 * n * n)) / den
    return [max(0.0, center - half), min(1.0, center + half)]


def stability_guard_matrix():
    rig = Rig(backend=None, fs=int(FS), df=1.0, blocks=2)
    t = np.arange(N, dtype=np.float64) / FS
    base = amp(PILOT_DBFS) * np.sin(2.0 * math.pi * PILOT_TRUE * t)
    # The noise remains present during the dropouts, as it would in an ADC capture.
    noise = white_noise(-110.0, 219099)
    cases = {}

    def store(name, mask=None, scale=None):
        x = base.copy()
        if mask is not None:
            x *= mask
        if scale is not None:
            x *= scale
        x += noise
        stereo = np.column_stack((x, x))
        reason = rig._window_stability_reason(
            stereo, tones_l=[(1, 1.0)], tones_r=[(1, 1.0)]
        )
        cases[name] = {
            "guard_result": "reject" if reason else "pass",
            "reason": reason,
            "active_fraction": float(np.mean(mask)) if mask is not None else 1.0,
        }

    tt = t
    store("steady")
    store("dropout_first_quarter", (tt >= 0.5).astype(float))
    store("dropout_tail_quarter", (tt < 1.5).astype(float))
    store("dropout_half", (tt < 1.0).astype(float))
    store("occupancy_40_percent", (tt < 0.8).astype(float))
    store("gap_40ms", (~((tt >= 0.400) & (tt < 0.440))).astype(float))
    store("gap_60ms", (~((tt >= 0.400) & (tt < 0.460))).astype(float))
    store("sag_200ms_minus6dB", scale=1.0 - 0.5 * ((tt >= 0.400) & (tt < 0.600)))
    return cases


def run_lsq_case(name: str, category: str, x: np.ndarray, seed: int, meta=None):
    result = est_lsq(x, FS, PILOT_NOM)
    return {
        "trial": None,
        "name": name,
        "category": category,
        "seed": int(seed),
        "pilot_level_dbfs": (meta or {}).get("pilot_dbfs"),
        "noise_level_dbfs": (meta or {}).get("noise_dbfs"),
        "injected_spur_hz": (meta or {}).get("spur_hz"),
        "injected_spur_dbc": (meta or {}).get("spur_dbc"),
        "snr_carrier_db": finite(result.get("snr_carrier_db")),
        "status": result.get("status"),
        "line_hz": finite(result.get("line_hz")),
        "line_dbc": finite(result.get("line_dbc")),
        "line_bins": result.get("line_bins"),
    }


def make_replay_cases(rows):
    """Rebuild the original v2.18.9 white-noise grid using its seed sequence."""
    idx = 0
    for level in (-72.0, -80.0, -85.0, -90.0, -95.0, -100.0, -105.0):
        for noise_level in (-96.0, -100.0, -104.0):
            for tr in range(12):
                seed = REPLAY_SEED0 + idx
                x = pilot(level, seed) + white_noise(noise_level, seed + 1)
                rows.append(run_lsq_case(
                    f"replay-p{level:g}-n{noise_level:g}-r{tr}",
                    "replay_steady_white", x, seed,
                    {"pilot_dbfs": level, "noise_dbfs": noise_level}
                ))
                idx += 1


def make_focus_cases(rows):
    idx = 0
    # Deliberately concentrates trials where v2.18.9 observed random line-QC hits.
    for level in (-95.0, -100.0, -105.0):
        for noise_level in (-96.0, -100.0, -104.0):
            for tr in range(TRIALS_FOCUS):
                seed = FOCUS_SEED0 + idx
                x = pilot(level, seed) + white_noise(noise_level, seed + 1)
                rows.append(run_lsq_case(
                    f"focus-white-p{level:g}-n{noise_level:g}-r{tr}",
                    "focus_steady_white", x, seed,
                    {"pilot_dbfs": level, "noise_dbfs": noise_level}
                ))
                idx += 1

    # Colored, line-free controls at the same pilot levels.
    for level in (-95.0, -100.0, -105.0):
        for tr in range(20):
            seed = FOCUS_SEED0 + 10000 + idx
            x = pilot(level, seed) + colored_noise(-100.0, seed + 1)
            rows.append(run_lsq_case(
                f"focus-colored-p{level:g}-r{tr}",
                "focus_steady_colored", x, seed,
                {"pilot_dbfs": level, "noise_dbfs": -100.0}
            ))
            idx += 1

    # Noise-only controls test tone gate false accepts independently of line-QC.
    for noise_level in (-96.0, -100.0, -104.0):
        for tr in range(20):
            seed = FOCUS_SEED0 + 20000 + idx
            x = white_noise(noise_level, seed)
            rows.append(run_lsq_case(
                f"noise-only-n{noise_level:g}-r{tr}",
                "noise_only", x, seed,
                {"noise_dbfs": noise_level}
            ))
            idx += 1

    # Known discrete residual lines at several locations and levels.
    t = np.arange(N, dtype=np.float64) / FS
    for spur_hz in (12345.0, 23456.0, 34567.0):
        for spur_dbc in (-30.0, -35.0):
            for tr in range(8):
                seed = FOCUS_SEED0 + 30000 + idx
                rng = np.random.default_rng(seed + 1)
                phase = float(rng.uniform(0.0, 2.0 * math.pi))
                x = pilot(-72.0, seed)
                x += white_noise(-104.0, seed + 2)
                x += amp(-72.0) * amp(spur_dbc) * np.sin(
                    2.0 * math.pi * spur_hz * t + phase
                )
                rows.append(run_lsq_case(
                    f"spur-{spur_hz:g}-{spur_dbc:g}-r{tr}",
                    "injected_spur", x, seed,
                    {"pilot_dbfs": -72.0, "noise_dbfs": -104.0,
                     "spur_hz": spur_hz, "spur_dbc": spur_dbc}
                ))
                idx += 1


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row["category"]].append(row)
    result = {}
    for category, group in sorted(groups.items()):
        n = len(group)
        flagged = sum(r["status"] == "line_contaminated" for r in group)
        tone_missing = sum(r["status"] == "tone_missing" for r in group)
        snr_low = sum(r["status"] == "snr_low" for r in group)
        result[category] = {
            "n": n,
            "line_contaminated": flagged,
            "line_contaminated_rate": flagged / n if n else None,
            "line_contaminated_wilson95": wilson(flagged, n),
            "tone_missing": tone_missing,
            "snr_low": snr_low,
            "status_counts": {
                s: sum(r["status"] == s for r in group)
                for s in sorted({r["status"] for r in group})
            },
            "snr_min_db": min((r["snr_carrier_db"] for r in group
                               if r["snr_carrier_db"] is not None), default=None),
            "snr_median_db": float(np.median([
                r["snr_carrier_db"] for r in group
                if r["snr_carrier_db"] is not None
            ])) if any(r["snr_carrier_db"] is not None for r in group) else None,
        }
    return result


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    guard = stability_guard_matrix()
    rows = []
    make_replay_cases(rows)
    make_focus_cases(rows)
    for i, row in enumerate(rows):
        row["trial"] = i

    summary = {
        "capture": {
            "fs_hz": FS, "duration_s": DURATION_S, "samples": N,
            "pilot_nominal_hz": PILOT_NOM, "pilot_true_hz": PILOT_TRUE,
            "pilot_level_dbfs_replay": [-72.0, -80.0, -85.0, -90.0, -95.0, -100.0, -105.0],
        },
        "stability_guard": guard,
        "line_qc": {
            "trials": len(rows),
            "categories": summarize(rows),
            "interpretation": (
                "A line_contaminated result on a generated line-free case is a "
                "candidate false positive. Injected-spur hits are sensitivity controls. "
                "This is a seeded synthetic population, not a hardware false-positive rate."
            ),
        },
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
    }
    output = {"summary": summary, "per_trial": rows}
    out_path = OUT / "v2190-gate-lineqc-montecarlo.json"
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {out_path}", flush=True)


if __name__ == "__main__":
    main()
