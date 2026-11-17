#!/usr/bin/env python3
"""v2.21.0 diagnostic test of two-block persistence for v2.18.8 line-QC.

The frozen estimator is executed once per synthetic record. Its full-window
residual-line result is compared with a candidate requiring a line at a
consistent frequency in BOTH one-second halves. No thresholds or production
code are changed; this only tests a diagnostic acceptance hypothesis.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import v2188_estimators as estimator

OUT = Path(__file__).resolve().parent
FS = 96000.0
DURATION_S = 2.0
N = int(FS * DURATION_S)
PILOT_NOM = 1000.0
PILOT_TRUE = PILOT_NOM * (1.0 - 23.5e-6)
FOCUS_SEED0 = 219000
SPUR_SEED0 = 249000
TRIALS_PER_WHITE_CELL = 60
TOLERANCES_HZ = (0.5, 1.0, 2.0)


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
    return y * (amp(dbfs) / math.sqrt(2.0) / max(rms, 1e-30))


def pilot(level_dbfs: float, seed: int) -> np.ndarray:
    t = np.arange(N, dtype=np.float64) / FS
    phase = 2.0 * math.pi * ((seed * 37) % 997) / 997.0
    return amp(level_dbfs) * np.sin(2.0 * math.pi * PILOT_TRUE * t + phase)


def finite(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def block_line_qc(x: np.ndarray, f_est: float, carrier_amp: float):
    """Rebuild the exact LSQ time-domain residual at f_est; test each half."""
    n = len(x)
    t = np.arange(n, dtype=np.float64) / FS
    D = np.column_stack((
        np.sin(2.0 * math.pi * f_est * t),
        np.cos(2.0 * math.pi * f_est * t),
        np.ones(n),
    ))
    coef, *_ = np.linalg.lstsq(D, x, rcond=None)
    residual = x - D @ coef

    full_win = np.hanning(n)
    full_w = np.abs(np.fft.rfft(residual * full_win))
    full_f = np.fft.rfftfreq(n, 1.0 / FS)
    full_ref = carrier_amp * float(full_win.sum()) / 2.0
    full_qc = estimator.residual_line_qc(
        full_w, full_f, full_ref, f_est, FS, estimator.QC_CFG
    )

    half_n = n // 2
    block_results = []
    for block_index in range(2):
        a = block_index * half_n
        b = a + half_n
        xb = residual[a:b]
        win = np.hanning(half_n)
        W = np.abs(np.fft.rfft(xb * win))
        f = np.fft.rfftfreq(half_n, 1.0 / FS)
        ref = carrier_amp * float(win.sum()) / 2.0
        qc = estimator.residual_line_qc(
            W, f, ref, f_est, FS, estimator.QC_CFG
        )
        block_results.append({
            "line": bool(qc["line"]),
            "line_hz": finite(qc["line_hz"]),
            "line_dbc": finite(qc["line_dbc"]),
            "line_bins": int(qc["line_bins"]),
        })
    return full_qc, block_results


def run_case(name, category, x, seed, meta):
    result = estimator.est_lsq(x, FS, PILOT_NOM)
    f_est = finite(result.get("f_est"))
    if f_est is None:
        full_qc = {"line": False, "line_hz": None, "line_dbc": None, "line_bins": 0}
        halves = [
            {"line": False, "line_hz": None, "line_dbc": None, "line_bins": 0},
            {"line": False, "line_hz": None, "line_dbc": None, "line_bins": 0},
        ]
    else:
        t = np.arange(N, dtype=np.float64) / FS
        D = np.column_stack((
            np.sin(2.0 * math.pi * f_est * t),
            np.cos(2.0 * math.pi * f_est * t),
            np.ones(N),
        ))
        coef, *_ = np.linalg.lstsq(D, x, rcond=None)
        carrier_amp = float(math.hypot(coef[0], coef[1]))
        full_qc, halves = block_line_qc(x, f_est, carrier_amp)

    est_line = result.get("line_hz") is not None
    reconstruction_match = (
        bool(full_qc["line"]) == bool(est_line)
        and (not est_line or (
            abs(float(full_qc["line_hz"]) - float(result["line_hz"])) <= 0.011
            and abs(float(full_qc["line_dbc"]) - float(result["line_dbc"])) <= 0.11
        ))
    )
    block_difference = (
        abs(halves[0]["line_hz"] - halves[1]["line_hz"])
        if halves[0]["line_hz"] is not None and halves[1]["line_hz"] is not None
        else None
    )
    return {
        "name": name,
        "category": category,
        "seed": int(seed),
        "pilot_dbfs": meta.get("pilot_dbfs"),
        "noise_dbfs": meta.get("noise_dbfs"),
        "spur_hz": meta.get("spur_hz"),
        "spur_dbc": meta.get("spur_dbc"),
        "f_est_hz": f_est,
        "estimator_status": result.get("status"),
        "estimator_line_hz": finite(result.get("line_hz")),
        "estimator_line_dbc": finite(result.get("line_dbc")),
        "estimator_line_bins": result.get("line_bins"),
        "reconstructed_full_line": bool(full_qc["line"]),
        "reconstructed_full_line_hz": finite(full_qc["line_hz"]),
        "reconstructed_full_line_dbc": finite(full_qc["line_dbc"]),
        "reconstruction_matches_estimator": reconstruction_match,
        "block_1": halves[0],
        "block_2": halves[1],
        "block_frequency_difference_hz": block_difference,
        "two_block_persistent": {
            str(tol): bool(
                halves[0]["line"] and halves[1]["line"]
                and block_difference is not None
                and block_difference <= tol
            )
            for tol in TOLERANCES_HZ
        },
    }


def build_cases():
    rows = []
    idx = 0
    for pilot_dbfs in (-105.0, -100.0, -95.0):
        for noise_dbfs in (-104.0, -100.0, -96.0):
            for trial in range(TRIALS_PER_WHITE_CELL):
                seed = FOCUS_SEED0 + idx
                x = pilot(pilot_dbfs, seed) + white_noise(noise_dbfs, seed + 1)
                rows.append(run_case(
                    f"white-p{pilot_dbfs:g}-n{noise_dbfs:g}-r{trial}",
                    "line_free_white", x, seed,
                    {"pilot_dbfs": pilot_dbfs, "noise_dbfs": noise_dbfs}
                ))
                idx += 1

    for pilot_dbfs in (-95.0, -100.0, -105.0):
        for trial in range(20):
            seed = FOCUS_SEED0 + 10000 + idx
            x = pilot(pilot_dbfs, seed) + colored_noise(-100.0, seed + 1)
            rows.append(run_case(
                f"colored-p{pilot_dbfs:g}-r{trial}",
                "line_free_colored", x, seed,
                {"pilot_dbfs": pilot_dbfs, "noise_dbfs": -100.0}
            ))
            idx += 1

    t = np.arange(N, dtype=np.float64) / FS
    for spur_hz in (12345.0, 23456.0, 34567.0):
        for spur_dbc in (-30.0, -35.0):
            for trial in range(8):
                seed = SPUR_SEED0 + idx
                rng = np.random.default_rng(seed + 1)
                phase = float(rng.uniform(0.0, 2.0 * math.pi))
                x = pilot(-72.0, seed) + white_noise(-104.0, seed + 2)
                x += amp(-72.0) * amp(spur_dbc) * np.sin(
                    2.0 * math.pi * spur_hz * t + phase
                )
                rows.append(run_case(
                    f"spur-{spur_hz:g}-{spur_dbc:g}-r{trial}",
                    "injected_stable_spur", x, seed,
                    {"pilot_dbfs": -72.0, "noise_dbfs": -104.0,
                     "spur_hz": spur_hz, "spur_dbc": spur_dbc}
                ))
                idx += 1
    return rows


def summarize(rows):
    output = {}
    for category in sorted({r["category"] for r in rows}):
        group = [r for r in rows if r["category"] == category]
        n = len(group)
        full_hits = sum(r["estimator_status"] == "line_contaminated" for r in group)
        row = {
            "n": n,
            "full_window_line_hits": full_hits,
            "full_window_line_hit_rate": full_hits / n if n else None,
            "reconstruction_mismatches": sum(
                not r["reconstruction_matches_estimator"] for r in group
            ),
            "block_1_hits": sum(r["block_1"]["line"] for r in group),
            "block_2_hits": sum(r["block_2"]["line"] for r in group),
        }
        for tol in TOLERANCES_HZ:
            persistent = sum(r["two_block_persistent"][str(tol)] for r in group)
            row[f"persistent_both_blocks_within_{tol:g}hz"] = persistent
            row[f"persistent_rate_within_{tol:g}hz"] = persistent / n if n else None
        output[category] = row
    return output


def main():
    rows = build_cases()
    output = {
        "summary": {
            "purpose": (
                "Test whether requiring the same residual line in both 1-second "
                "blocks reduces white-noise false alarms while retaining stable injected spurs."
            ),
            "fs_hz": FS,
            "duration_s": DURATION_S,
            "pilot_nominal_hz": PILOT_NOM,
            "pilot_true_hz": PILOT_TRUE,
            "df_full_hz": FS / N,
            "df_per_block_hz": FS / (N // 2),
            "trials": len(rows),
            "tolerances_hz": list(TOLERANCES_HZ),
            "categories": summarize(rows),
            "interpretation": (
                "The two-block persistence rule is only a candidate diagnostic policy. "
                "Do not implement it or change QC_CFG from these synthetic data alone."
            ),
            "environment": {
                "python": platform.python_version(),
                "numpy": np.__version__,
                "scipy": scipy.__version__,
            },
        },
        "per_trial": rows,
    }
    out_path = OUT / "v2210-lineqc-two-block.json"
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    print(json.dumps(output["summary"], ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {out_path}", flush=True)


if __name__ == "__main__":
    main()
