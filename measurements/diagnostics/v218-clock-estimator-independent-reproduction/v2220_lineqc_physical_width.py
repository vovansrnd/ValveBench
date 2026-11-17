#!/usr/bin/env python3
"""v2.22.0 line-QC persistence with a physically normalized local floor.

The frozen v2.18.8 LSQ estimator is run unchanged. Its reconstructed
full-window residual is tested with the original 161-bin local floor, then
each 1-second half is tested with both 161 bins (legacy physical-width
mismatch) and 81 bins (approximately the same ±40 Hz physical width as the
2-second 0.5 Hz/bin analysis). Controls include line-free noise, stable
off-bin/on-bin spurs, and first/second-half-only spurs.

This is an offline diagnostic comparison only. No QC_CFG, estimator,
production code, or threshold is changed.
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

import v2188_estimators as estimator

OUT = Path(__file__).resolve().parent
FS = 96000.0
DURATION_S = 2.0
N = int(FS * DURATION_S)
PILOT_NOM = 1000.0
PILOT_TRUE = PILOT_NOM * (1.0 - 23.5e-6)
WHITE_SEED0 = 222100
COLOR_SEED0 = 223100
SPUR_SEED0 = 224100
TOLERANCES_HZ = (0.5, 1.0, 2.0)
LEGACY_BLOCK_CFG = dict(estimator.QC_CFG)
NORMALIZED_BLOCK_CFG = dict(estimator.QC_CFG)
NORMALIZED_BLOCK_CFG["floor_bins"] = 81


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


def wilson(k: int, n: int, z: float = 1.96):
    if n <= 0:
        return None
    p = k / n
    den = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / den
    half = z * math.sqrt(
        p * (1.0 - p) / n + z * z / (4.0 * n * n)
    ) / den
    return [max(0.0, center - half), min(1.0, center + half)]


def residual_for_fit(x: np.ndarray, f_est: float):
    t = np.arange(len(x), dtype=np.float64) / FS
    D = np.column_stack((
        np.sin(2.0 * math.pi * f_est * t),
        np.cos(2.0 * math.pi * f_est * t),
        np.ones(len(x)),
    ))
    coef, *_ = np.linalg.lstsq(D, x, rcond=None)
    residual = x - D @ coef
    carrier_amp = float(math.hypot(coef[0], coef[1]))
    return residual, carrier_amp


def qc_one_block(residual_block: np.ndarray, fs: float, f_est: float,
                 carrier_amp: float, cfg: dict):
    n = len(residual_block)
    win = np.hanning(n)
    W = np.abs(np.fft.rfft(residual_block * win))
    freqs = np.fft.rfftfreq(n, 1.0 / fs)
    carrier_ref = carrier_amp * float(win.sum()) / 2.0
    q = estimator.residual_line_qc(
        W, freqs, carrier_ref, f_est, fs, cfg
    )
    return {
        "line": bool(q["line"]),
        "line_hz": finite(q["line_hz"]),
        "line_dbc": finite(q["line_dbc"]),
        "line_bins": int(q["line_bins"]),
    }


def run_case(name: str, category: str, x: np.ndarray, seed: int,
             meta: dict, active_half: str = "both"):
    result = estimator.est_lsq(x, FS, PILOT_NOM)
    f_est = finite(result.get("f_est"))
    if f_est is None:
        residual = np.zeros_like(x)
        carrier_amp = 0.0
        full = {"line": False, "line_hz": None, "line_dbc": None, "line_bins": 0}
        b1 = b2 = {"line": False, "line_hz": None, "line_dbc": None, "line_bins": 0}
        l161_1 = l161_2 = l81_1 = l81_2 = dict(b1)
    else:
        residual, carrier_amp = residual_for_fit(x, f_est)
        win = np.hanning(N)
        W = np.abs(np.fft.rfft(residual * win))
        freqs = np.fft.rfftfreq(N, 1.0 / FS)
        carrier_ref = carrier_amp * float(win.sum()) / 2.0
        q = estimator.residual_line_qc(
            W, freqs, carrier_ref, f_est, FS, estimator.QC_CFG
        )
        full = {
            "line": bool(q["line"]),
            "line_hz": finite(q["line_hz"]),
            "line_dbc": finite(q["line_dbc"]),
            "line_bins": int(q["line_bins"]),
        }
        half_n = N // 2
        b1_resid, b2_resid = residual[:half_n], residual[half_n:]
        b1 = qc_one_block(b1_resid, FS, f_est, carrier_amp, LEGACY_BLOCK_CFG)
        b2 = qc_one_block(b2_resid, FS, f_est, carrier_amp, LEGACY_BLOCK_CFG)
        l161_1, l161_2 = b1, b2
        l81_1 = qc_one_block(b1_resid, FS, f_est, carrier_amp, NORMALIZED_BLOCK_CFG)
        l81_2 = qc_one_block(b2_resid, FS, f_est, carrier_amp, NORMALIZED_BLOCK_CFG)

    estimator_line = result.get("line_hz") is not None
    reconstruct_match = (
        bool(full["line"]) == bool(estimator_line)
        and (not estimator_line or (
            abs(float(full["line_hz"]) - float(result["line_hz"])) <= 0.011
            and abs(float(full["line_dbc"]) - float(result["line_dbc"])) <= 0.11
        ))
    )

    def persistent(a, b, tol):
        return bool(a["line"] and b["line"] and a["line_hz"] is not None
                    and b["line_hz"] is not None
                    and abs(a["line_hz"] - b["line_hz"]) <= tol)

    return {
        "name": name,
        "category": category,
        "seed": int(seed),
        "pilot_dbfs": meta.get("pilot_dbfs"),
        "noise_dbfs": meta.get("noise_dbfs"),
        "spur_hz": meta.get("spur_hz"),
        "spur_dbc": meta.get("spur_dbc"),
        "active_half": active_half,
        "f_est_hz": f_est,
        "estimator_status": result.get("status"),
        "estimator_line_hz": finite(result.get("line_hz")),
        "estimator_line_dbc": finite(result.get("line_dbc")),
        "estimator_line_bins": result.get("line_bins"),
        "reconstructed_full": full,
        "reconstruction_matches_estimator": reconstruct_match,
        "legacy_block_1": l161_1,
        "legacy_block_2": l161_2,
        "physical_block_1": l81_1,
        "physical_block_2": l81_2,
        "legacy_block_frequency_difference_hz": (
            abs(l161_1["line_hz"] - l161_2["line_hz"])
            if l161_1["line_hz"] is not None and l161_2["line_hz"] is not None else None
        ),
        "physical_block_frequency_difference_hz": (
            abs(l81_1["line_hz"] - l81_2["line_hz"])
            if l81_1["line_hz"] is not None and l81_2["line_hz"] is not None else None
        ),
        "legacy_two_block_persistent": {
            str(tol): persistent(l161_1, l161_2, tol) for tol in TOLERANCES_HZ
        },
        "physical_two_block_persistent": {
            str(tol): persistent(l81_1, l81_2, tol) for tol in TOLERANCES_HZ
        },
    }


def build_cases():
    rows = []
    idx = 0

    for pilot_dbfs in (-105.0, -100.0, -95.0):
        for noise_dbfs in (-104.0, -100.0, -96.0):
            for trial in range(100):
                seed = WHITE_SEED0 + idx
                x = pilot(pilot_dbfs, seed) + white_noise(noise_dbfs, seed + 1)
                rows.append(run_case(
                    f"white-p{pilot_dbfs:g}-n{noise_dbfs:g}-r{trial}",
                    "line_free_white", x, seed,
                    {"pilot_dbfs": pilot_dbfs, "noise_dbfs": noise_dbfs}
                ))
                idx += 1

    for pilot_dbfs in (-95.0, -100.0, -105.0):
        for trial in range(20):
            seed = COLOR_SEED0 + idx
            x = pilot(pilot_dbfs, seed) + colored_noise(-100.0, seed + 1)
            rows.append(run_case(
                f"colored-p{pilot_dbfs:g}-r{trial}",
                "line_free_colored", x, seed,
                {"pilot_dbfs": pilot_dbfs, "noise_dbfs": -100.0}
            ))
            idx += 1

    t = np.arange(N, dtype=np.float64) / FS
    spur_freqs = (12345.0, 12345.35, 23456.25, 34567.60)
    for level in (-30.0, -35.0, -40.0):
        for spur_hz in spur_freqs:
            for trial in range(12):
                seed = SPUR_SEED0 + idx
                rng = np.random.default_rng(seed + 1)
                phase = float(rng.uniform(0.0, 2.0 * math.pi))
                x = pilot(-72.0, seed) + white_noise(-104.0, seed + 2)
                spur = amp(-72.0) * amp(level) * np.sin(
                    2.0 * math.pi * spur_hz * t + phase
                )
                x += spur
                meta = {"pilot_dbfs": -72.0, "noise_dbfs": -104.0,
                        "spur_hz": spur_hz, "spur_dbc": level}
                rows.append(run_case(
                    f"stable-spur-{spur_hz:g}-{level:g}-r{trial}",
                    "stable_spur", x, seed, meta
                ))
                idx += 1

    # A real line present in one half is an intentional transient control.
    for active_half in ("first", "second"):
        mask = np.zeros(N, dtype=bool)
        mask[:N // 2] = True if active_half == "first" else False
        mask[N // 2:] = True if active_half == "second" else False
        for level in (-30.0, -35.0):
            for spur_hz in spur_freqs:
                for trial in range(12):
                    seed = SPUR_SEED0 + 100000 + idx
                    rng = np.random.default_rng(seed + 1)
                    phase = float(rng.uniform(0.0, 2.0 * math.pi))
                    x = pilot(-72.0, seed) + white_noise(-104.0, seed + 2)
                    spur = amp(-72.0) * amp(level) * np.sin(
                        2.0 * math.pi * spur_hz * t + phase
                    )
                    x += spur * mask
                    meta = {"pilot_dbfs": -72.0, "noise_dbfs": -104.0,
                            "spur_hz": spur_hz, "spur_dbc": level}
                    rows.append(run_case(
                        f"{active_half}-half-spur-{spur_hz:g}-{level:g}-r{trial}",
                        f"{active_half}_half_spur", x, seed, meta,
                        active_half=active_half
                    ))
                    idx += 1
    return rows


def summarize(rows):
    summary = {}
    categories = sorted({r["category"] for r in rows})
    for category in categories:
        group = [r for r in rows if r["category"] == category]
        n = len(group)
        full = sum(r["reconstructed_full"]["line"] for r in group)
        legacy_both = sum(r["legacy_two_block_persistent"]["0.5"] for r in group)
        physical_both = sum(r["physical_two_block_persistent"]["0.5"] for r in group)
        row = {
            "n": n,
            "full_window_hits": full,
            "full_window_rate": full / n if n else None,
            "full_window_wilson95": wilson(full, n),
            "legacy_block_1_hits": sum(r["legacy_block_1"]["line"] for r in group),
            "legacy_block_2_hits": sum(r["legacy_block_2"]["line"] for r in group),
            "legacy_both_within_0_5hz": legacy_both,
            "legacy_both_rate": legacy_both / n if n else None,
            "legacy_both_wilson95": wilson(legacy_both, n),
            "physical_block_1_hits": sum(r["physical_block_1"]["line"] for r in group),
            "physical_block_2_hits": sum(r["physical_block_2"]["line"] for r in group),
            "physical_both_within_0_5hz": physical_both,
            "physical_both_rate": physical_both / n if n else None,
            "physical_both_wilson95": wilson(physical_both, n),
            "reconstruction_mismatches": sum(
                not r["reconstruction_matches_estimator"] for r in group
            ),
        }
        for tol in TOLERANCES_HZ:
            row[f"legacy_both_within_{tol:g}hz"] = sum(
                r["legacy_two_block_persistent"][str(tol)] for r in group
            )
            row[f"physical_both_within_{tol:g}hz"] = sum(
                r["physical_two_block_persistent"][str(tol)] for r in group
            )
        summary[category] = row
    return summary


def summarize_injected_cells(rows):
    groups = defaultdict(list)
    for row in rows:
        if row["category"] in ("stable_spur", "first_half_spur", "second_half_spur"):
            key = (row["category"], row["spur_hz"], row["spur_dbc"])
            groups[key].append(row)
    out = {}
    for key, group in sorted(groups.items()):
        n = len(group)
        out["%s %gHz %gdBc" % key] = {
            "n": n,
            "full_window_hits": sum(r["reconstructed_full"]["line"] for r in group),
            "legacy_both_0_5hz": sum(r["legacy_two_block_persistent"]["0.5"] for r in group),
            "physical_both_0_5hz": sum(r["physical_two_block_persistent"]["0.5"] for r in group),
            "physical_any_block_hits": sum(
                r["physical_block_1"]["line"] or r["physical_block_2"]["line"]
                for r in group
            ),
        }
    return out


def main():
    rows = build_cases()
    output = {
        "summary": {
            "purpose": (
                "Compare full-window line-QC with two-block persistence under "
                "legacy and physically normalized local-floor bandwidths."
            ),
            "fs_hz": FS,
            "duration_s": DURATION_S,
            "pilot_nominal_hz": PILOT_NOM,
            "pilot_true_hz": PILOT_TRUE,
            "df_full_hz": FS / N,
            "df_half_hz": FS / (N // 2),
            "legacy_floor_bins": estimator.QC_CFG["floor_bins"],
            "full_window_floor_half_width_hz": (
                (estimator.QC_CFG["floor_bins"] - 1) / 2 * FS / N
            ),
            "legacy_half_window_floor_half_width_hz": (
                (LEGACY_BLOCK_CFG["floor_bins"] - 1) / 2 * FS / (N // 2)
            ),
            "physical_half_window_floor_bins": NORMALIZED_BLOCK_CFG["floor_bins"],
            "physical_half_window_floor_half_width_hz": (
                (NORMALIZED_BLOCK_CFG["floor_bins"] - 1) / 2 * FS / (N // 2)
            ),
            "tolerances_hz": list(TOLERANCES_HZ),
            "trial_count": len(rows),
            "categories": summarize(rows),
            "injected_cells": summarize_injected_cells(rows),
            "interpretation": (
                "The normalized two-block rule is a candidate diagnostic policy only. "
                "It may suppress transient real spurs by design. Do not change QC_CFG "
                "or use it as a hard refusal without a separate coordinator decision."
            ),
            "environment": {
                "python": platform.python_version(),
                "numpy": np.__version__,
                "scipy": scipy.__version__,
            },
        },
        "per_trial": rows,
    }
    out_path = OUT / "v2220-lineqc-physical-width.json"
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, ensure_ascii=False, indent=2)
    print(json.dumps(output["summary"], ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {out_path}", flush=True)


if __name__ == "__main__":
    main()
