#!/usr/bin/env python3
"""Coordinator-authored v2.18.9 paired SNR validation.

Mirror the standard Rig clock-check segmentation:
  fs=96 kHz, df=1 Hz, blocks=2 -> n=96,000 per block.
  The two-second capture is analyzed by production dsp.analyze_tone as
  TWO one-second blocks. The diagnostic estimator receives the same samples.

This script is intentionally self-contained in its signal generation.
It records raw paired metrics; it does not choose or modify either threshold.
"""
from __future__ import annotations

import json
import math
import platform
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import scipy

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from valvebench import calib, dsp
from v2188_estimators import est_lsq

OUT = Path(__file__).resolve().parent
FS = 96000.0
DURATION_S = 2.0
N = int(FS * DURATION_S)
BLOCK_N = int(FS)                  # df=1 Hz, one second per production block
BLOCKS = N // BLOCK_N              # exactly two blocks
PILOT_NOM = 1000.0
PPM_TRUE = -23.5
PROD_THRESHOLD_DB = 20.0
DIAG_THRESHOLDS_DB = (-5.0, 0.0, 5.0, 8.8, 10.0, 15.0, 20.0, 25.0, 30.0)
TRIALS = 12


def amp(dbfs: float) -> float:
    return 10.0 ** (float(dbfs) / 20.0)


def white_noise(dbfs: float, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return amp(dbfs) / math.sqrt(2.0) * rng.standard_normal(N)


def colored_noise(dbfs: float, seed: int) -> np.ndarray:
    """Smooth low-frequency-heavy noise, normalized to the requested dBFS."""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(N)
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(N, 1.0 / FS)
    X /= np.sqrt(1.0 + (f / 250.0) ** 2)
    y = np.fft.irfft(X, n=N)
    rms = float(np.sqrt(np.mean(y * y)))
    target_rms = amp(dbfs) / math.sqrt(2.0)
    return y * (target_rms / max(rms, 1e-30))


def pilot(level_dbfs: float, seed: int) -> np.ndarray:
    t = np.arange(N, dtype=np.float64) / FS
    phase = 2.0 * math.pi * ((seed * 37) % 997) / 997.0
    f_true = PILOT_NOM * (1.0 + PPM_TRUE * 1e-6)
    return amp(level_dbfs) * np.sin(2.0 * math.pi * f_true * t + phase)


def make_cases():
    cases = []
    seed0 = 218900

    # Steady tones over a signal/noise grid.
    idx = 0
    for level in (-72.0, -80.0, -85.0, -90.0, -95.0, -100.0, -105.0):
        for noise_level in (-96.0, -100.0, -104.0):
            for tr in range(TRIALS):
                seed = seed0 + idx
                x = pilot(level, seed) + white_noise(noise_level, seed + 1)
                cases.append((f"steady-white-p{level:g}-n{noise_level:g}",
                              x, "steady_white"))
                idx += 1

    # Colored line-free noise at selected pilot levels.
    for level in (-72.0, -85.0, -95.0):
        for tr in range(TRIALS):
            seed = seed0 + 10000 + idx
            x = pilot(level, seed) + colored_noise(-96.0, seed + 1)
            cases.append((f"steady-colored-p{level:g}", x, "steady_colored"))
            idx += 1

    # Noise-only controls.
    for noise_level in (-80.0, -96.0):
        for tr in range(20):
            seed = seed0 + 20000 + idx
            cases.append((f"no-pilot-n{noise_level:g}",
                          white_noise(noise_level, seed), "no_pilot"))
            idx += 1

    # Pilot dropout inside the same two-second capture.
    t = np.arange(N, dtype=np.float64) / FS
    for name, mask in (
        ("dropout-half", t < 1.0),
        ("dropout-tail-quarter", t < 1.5),
        ("dropout-first-quarter", t >= 0.5),
    ):
        for tr in range(20):
            seed = seed0 + 30000 + idx
            x = pilot(-72.0, seed) * mask
            x += white_noise(-96.0, seed + 1)
            cases.append((name, x, name))
            idx += 1

    # 40% window occupancy.
    for tr in range(20):
        seed = seed0 + 40000 + idx
        x = np.zeros(N, dtype=np.float64)
        n_on = int(0.8 * FS)
        x[:n_on] = pilot(-72.0, seed)[:n_on]
        x += white_noise(-96.0, seed + 1)
        cases.append(("partial-40pct", x, "partial"))
        idx += 1

    # Clipping: SNR is not expected to detect this; line-QC is a later layer.
    for tr in range(20):
        seed = seed0 + 50000 + idx
        x = pilot(-60.0, seed)
        x += white_noise(-110.0, seed + 1)
        limit = 0.35 * amp(-60.0)
        cases.append(("clipped-pilot", np.clip(x, -limit, limit), "clipped"))
        idx += 1

    return cases


def finite(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def median(values):
    values = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return float(np.median(values)) if values else None


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    calibration = calib.Calibration()
    cases = make_cases()
    rows = []

    print(f"FS={FS:g} Hz; duration={DURATION_S:g} s; production blocks="
          f"{BLOCKS} x {BLOCK_N} frames; cases={len(cases)}", flush=True)

    for i, (name, x, category) in enumerate(cases):
        # Exact standard production segmentation: 2s capture, two 1s blocks.
        prod_result = dsp.analyze_tone(
            x, FS, PILOT_NOM, BLOCK_N, nharm=2,
            band=(20.0, calibration["band_hi"]), mains=calibration["mains"]
        )
        prod_snr = finite(prod_result.get("local_snr_db"))

        # Diagnostic estimator receives the exact same two-second sample vector.
        diag_result = est_lsq(x, FS, PILOT_NOM)
        diag_snr = finite(diag_result.get("snr_carrier_db"))

        prod_decision = ("accept" if prod_snr is not None and
                         prod_snr >= PROD_THRESHOLD_DB else "refuse")
        rows.append({
            "trial": i,
            "name": name,
            "category": category,
            "fs_hz": FS,
            "samples": int(len(x)),
            "production_block_samples": BLOCK_N,
            "production_blocks": BLOCKS,
            "production_snr_db": prod_snr,
            "diagnostic_snr_db": diag_snr,
            "diag_minus_prod_db": (diag_snr - prod_snr
                                   if diag_snr is not None and prod_snr is not None
                                   else None),
            "production_decision_20db": prod_decision,
            "diagnostic_status": diag_result.get("status"),
            "diagnostic_line_hz": diag_result.get("line_hz"),
        })
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(cases)}", flush=True)

    finite_pairs = [r for r in rows if r["production_snr_db"] is not None
                    and r["diagnostic_snr_db"] is not None]
    prod_v = np.asarray([r["production_snr_db"] for r in finite_pairs])
    diag_v = np.asarray([r["diagnostic_snr_db"] for r in finite_pairs])
    if len(finite_pairs) >= 2:
        slope, intercept = np.polyfit(prod_v, diag_v, 1)
        corr = float(np.corrcoef(prod_v, diag_v)[0, 1])
    else:
        slope = intercept = corr = float("nan")

    class_summary = {}
    for category in sorted({r["category"] for r in rows}):
        group = [r for r in rows if r["category"] == category]
        p = [r["production_snr_db"] for r in group]
        d = [r["diagnostic_snr_db"] for r in group]
        class_summary[category] = {
            "n": len(group),
            "production_median_db": median(p),
            "diagnostic_median_db": median(d),
            "difference_median_db": median([
                r["diag_minus_prod_db"] for r in group
            ]),
        }

    threshold_sweep = {}
    for threshold in DIAG_THRESHOLDS_DB:
        matrix = Counter()
        for r in rows:
            prod_decision = r["production_decision_20db"]
            diag = r["diagnostic_snr_db"]
            diag_decision = ("accept" if diag is not None and diag >= threshold
                             else "refuse")
            matrix[f"prod_{prod_decision}/diag_{diag_decision}"] += 1
        threshold_sweep[str(threshold)] = dict(matrix)

    summary = {
        "trials": len(rows),
        "pairs_finite": len(finite_pairs),
        "production_config": {
            "fs_hz": FS, "duration_s": DURATION_S,
            "df_hz": 1.0, "blocks": BLOCKS,
            "samples_per_block": BLOCK_N,
            "note": "Mirrors standard Rig(fs=96000, df=1, blocks=2) segmentation."
        },
        "thresholds_db": {
            "production": PROD_THRESHOLD_DB,
            "diagnostic_sweep": list(DIAG_THRESHOLDS_DB),
        },
        "paired_metric_stats": {
            "difference_mean_db": float(np.mean([
                r["diag_minus_prod_db"] for r in finite_pairs
            ])),
            "difference_median_db": float(np.median([
                r["diag_minus_prod_db"] for r in finite_pairs
            ])),
            "difference_std_db": float(np.std([
                r["diag_minus_prod_db"] for r in finite_pairs
            ])),
            "regression_diag_equals_slope_prod_plus_intercept": {
                "slope": float(slope), "intercept_db": float(intercept),
                "correlation": corr,
                "note": "Descriptive only; do not infer a threshold from pooled regression."
            },
        },
        "class_summary": class_summary,
        "threshold_decision_matrix": threshold_sweep,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
    }
    output = {"summary": summary, "per_trial": rows}
    out_path = OUT / "v2189-paired-snr-clockcheck.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {out_path}", flush=True)


if __name__ == "__main__":
    main()
