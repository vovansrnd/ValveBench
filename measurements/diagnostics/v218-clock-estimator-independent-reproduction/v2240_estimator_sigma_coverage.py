#!/usr/bin/env python3
"""v2.24.0 — empirical coverage of est_lsq sigma under white/colored noise.

Uses the frozen v2.18.8 est_lsq estimator unchanged. For each independent
L/R noise pair, compares:
  * the estimator's existing residual-curvature sigma;
  * an oracle local-PSD CRLB using the injected noise;
  * a practical local-PSD estimate from the post-fit residual.
No production code, estimator, thresholds, or hardware are modified.
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
from scipy.signal import welch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from v2188_estimators import est_lsq

FS = 96000.0
PILOT_NOMINAL_HZ = 1000.0
PILOT_OFFSET_PPM = -23.5
PILOT_TRUE_HZ = PILOT_NOMINAL_HZ * (1.0 + PILOT_OFFSET_PPM * 1e-6)
PILOT_DBFS = -72.0
NOISE_DBFS = -80.0
DURATIONS_S = (2.0, 4.0, 6.0)
NOISE_MODELS = ("white", "colored_lp1400")
PAIRS_PER_CELL = 128
CHANNEL_RHO = 0.95
SEED_BASE = 224000
LOCAL_PSD_MIN_OFFSET_HZ = 2.0
LOCAL_PSD_MAX_OFFSET_HZ = 32.0
WELCH_NPERSEG = 65536


def amp(db: float) -> float:
    return 10.0 ** (float(db) / 20.0)


def finite(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def make_noise_pair(n: int, seed: int, model: str) -> np.ndarray:
    """Reproduce the v2.23 white/colored model at fixed total RMS.

    The L/R channels share a common component with target rho=0.95.
    The colored model is the same one-pole low-pass family used in v2.23.0.
    """
    rng = np.random.default_rng(seed)
    a = rng.standard_normal(n)
    b = rng.standard_normal(n)

    if model == "colored_lp1400":
        freq = np.fft.rfftfreq(n, 1.0 / FS)
        for signal in (a, b):
            spectrum = np.fft.rfft(signal)
            spectrum /= np.sqrt(1.0 + (freq / 1400.0) ** 2)
            signal[:] = np.fft.irfft(spectrum, n=n)
    elif model != "white":
        raise ValueError(f"unknown noise model: {model}")

    b = CHANNEL_RHO * a + math.sqrt(1.0 - CHANNEL_RHO ** 2) * b
    pair = np.column_stack((a, b))
    pair_rms = float(np.sqrt(np.mean(pair * pair)))
    target_sample_rms = amp(NOISE_DBFS) / math.sqrt(2.0)
    return pair * (target_sample_rms / max(pair_rms, 1e-30))


def local_psd(signal: np.ndarray):
    """Median/mean one-sided Welch PSD in the pilot-adjacent noise band."""
    nperseg = min(WELCH_NPERSEG, len(signal))
    freq, power = welch(
        signal,
        fs=FS,
        nperseg=nperseg,
        noverlap=nperseg // 2,
        scaling="density",
    )
    offset = np.abs(freq - PILOT_TRUE_HZ)
    keep = (
        (offset >= LOCAL_PSD_MIN_OFFSET_HZ)
        & (offset <= LOCAL_PSD_MAX_OFFSET_HZ)
    )
    if not np.any(keep):
        raise RuntimeError("local PSD band has no bins")
    return (
        float(np.median(power[keep])),
        float(np.mean(power[keep])),
        float(freq[1] - freq[0]),
    )


def crlb_sigma_hz(n0_one_sided: float, peak_amplitude: float, duration_s: float):
    if n0_one_sided <= 0.0 or peak_amplitude <= 0.0 or duration_s <= 0.0:
        return None
    return math.sqrt(
        3.0 * n0_one_sided
        / (math.pi ** 2 * peak_amplitude ** 2 * duration_s ** 3)
    )


def fit_and_measure(signal: np.ndarray, noise: np.ndarray, duration_s: float):
    fit = est_lsq(signal, FS, PILOT_NOMINAL_HZ)
    f_est = finite(fit.get("f_est"))
    sigma_hz2 = finite(fit.get("variance_hz2"))
    sigma_est_hz = (
        math.sqrt(sigma_hz2)
        if sigma_hz2 is not None and sigma_hz2 >= 0.0
        else None
    )
    error_ppm = (
        (f_est - PILOT_TRUE_HZ) / PILOT_TRUE_HZ * 1e6
        if f_est is not None
        else None
    )

    if f_est is None:
        return {
            "f_est_hz": None,
            "fit_error_ppm": None,
            "estimator_status": fit.get("status"),
            "estimator_sigma_ppm": None,
            "fitted_peak_amplitude": None,
            "oracle_local_psd_median_v2_per_hz": None,
            "oracle_local_psd_mean_v2_per_hz": None,
            "residual_local_psd_median_v2_per_hz": None,
            "residual_local_psd_mean_v2_per_hz": None,
            "psd_bin_width_hz": None,
            "oracle_psd_sigma_ppm": None,
            "residual_psd_sigma_ppm": None,
            "abs_error_over_estimator_sigma": None,
            "abs_error_over_oracle_psd_sigma": None,
            "abs_error_over_residual_psd_sigma": None,
        }

    t = np.arange(len(signal), dtype=np.float64) / FS
    design = np.column_stack((
        np.sin(2.0 * np.pi * f_est * t),
        np.cos(2.0 * np.pi * f_est * t),
        np.ones(len(t)),
    ))
    coef, *_ = np.linalg.lstsq(design, signal, rcond=None)
    fitted = design @ coef
    residual = signal - fitted
    fitted_peak = float(math.hypot(coef[0], coef[1]))

    oracle_med, oracle_mean, df = local_psd(noise)
    resid_med, resid_mean, _ = local_psd(residual)

    # Use the known injected peak amplitude for the oracle benchmark and the
    # fitted amplitude for the residual-derived, operationally plausible value.
    oracle_sigma_hz = crlb_sigma_hz(oracle_med, amp(PILOT_DBFS), duration_s)
    residual_sigma_hz = crlb_sigma_hz(resid_med, fitted_peak, duration_s)
    sigma_est_ppm = (
        sigma_est_hz / PILOT_TRUE_HZ * 1e6
        if sigma_est_hz is not None
        else None
    )
    oracle_sigma_ppm = (
        oracle_sigma_hz / PILOT_TRUE_HZ * 1e6
        if oracle_sigma_hz is not None
        else None
    )
    residual_sigma_ppm = (
        residual_sigma_hz / PILOT_TRUE_HZ * 1e6
        if residual_sigma_hz is not None
        else None
    )
    abs_error = abs(error_ppm) if error_ppm is not None else None

    def ratio(value):
        if abs_error is None or value is None or value <= 0.0:
            return None
        return abs_error / value

    return {
        "f_est_hz": f_est,
        "fit_error_ppm": error_ppm,
        "estimator_status": fit.get("status"),
        "estimator_sigma_ppm": sigma_est_ppm,
        "fitted_peak_amplitude": fitted_peak,
        "oracle_local_psd_median_v2_per_hz": oracle_med,
        "oracle_local_psd_mean_v2_per_hz": oracle_mean,
        "residual_local_psd_median_v2_per_hz": resid_med,
        "residual_local_psd_mean_v2_per_hz": resid_mean,
        "psd_bin_width_hz": df,
        "oracle_psd_sigma_ppm": oracle_sigma_ppm,
        "residual_psd_sigma_ppm": residual_sigma_ppm,
        "abs_error_over_estimator_sigma": ratio(sigma_est_ppm),
        "abs_error_over_oracle_psd_sigma": ratio(oracle_sigma_ppm),
        "abs_error_over_residual_psd_sigma": ratio(residual_sigma_ppm),
    }


def quantile(values, q):
    vals = np.asarray([v for v in values if v is not None and math.isfinite(v)], dtype=float)
    return float(np.percentile(vals, q)) if vals.size else None


def sample_std(values):
    vals = np.asarray([v for v in values if v is not None and math.isfinite(v)], dtype=float)
    return float(np.std(vals, ddof=1)) if vals.size > 1 else None


def coverage(values, limit):
    vals = [v for v in values if v is not None and math.isfinite(v)]
    return float(np.mean(np.asarray(vals) <= limit)) if vals else None


def summarize_cell(rows):
    valid = [r for r in rows if r["fit_error_ppm"] is not None]
    by_channel = {
        channel: [r for r in valid if r["channel"] == channel]
        for channel in ("L", "R")
    }
    left = [r["fit_error_ppm"] for r in by_channel["L"]]
    right = [r["fit_error_ppm"] for r in by_channel["R"]]
    correlation = (
        float(np.corrcoef(left, right)[0, 1])
        if len(left) > 2 and len(right) > 2
        else None
    )
    err_est = [r["abs_error_over_estimator_sigma"] for r in valid]
    err_oracle = [r["abs_error_over_oracle_psd_sigma"] for r in valid]
    err_resid = [r["abs_error_over_residual_psd_sigma"] for r in valid]

    def med_ratio(key):
        return quantile([r[key] for r in valid], 50)

    def status_counts():
        counts = defaultdict(int)
        for r in rows:
            counts[str(r["estimator_status"])] += 1
        return dict(sorted(counts.items()))

    return {
        "independent_pairs": PAIRS_PER_CELL,
        "channel_rows": len(rows),
        "valid_fit_rows": len(valid),
        "status_counts": status_counts(),
        "signed_error_mean_ppm": (
            float(np.mean([r["fit_error_ppm"] for r in valid])) if valid else None
        ),
        "signed_error_std_ppm": sample_std([r["fit_error_ppm"] for r in valid]),
        "fit_error_std_L_ppm": sample_std(left),
        "fit_error_std_R_ppm": sample_std(right),
        "L_R_error_correlation": correlation,
        "estimator_sigma_over_oracle_psd_sigma_median": med_ratio_ratio(
            valid, "estimator_sigma_ppm", "oracle_psd_sigma_ppm"
        ),
        "residual_psd_over_oracle_psd_median": med_ratio_ratio(
            valid, "residual_psd_sigma_ppm", "oracle_psd_sigma_ppm"
        ),
        "abs_error_over_estimator_sigma_median": quantile(err_est, 50),
        "abs_error_over_estimator_sigma_p90": quantile(err_est, 90),
        "abs_error_over_estimator_sigma_max": max(err_est) if err_est else None,
        "estimator_sigma_coverage_1": coverage(err_est, 1.0),
        "estimator_sigma_coverage_2": coverage(err_est, 2.0),
        "estimator_sigma_coverage_3": coverage(err_est, 3.0),
        "abs_error_over_oracle_psd_sigma_median": quantile(err_oracle, 50),
        "abs_error_over_oracle_psd_sigma_p90": quantile(err_oracle, 90),
        "oracle_psd_sigma_coverage_1": coverage(err_oracle, 1.0),
        "oracle_psd_sigma_coverage_2": coverage(err_oracle, 2.0),
        "oracle_psd_sigma_coverage_3": coverage(err_oracle, 3.0),
        "abs_error_over_residual_psd_sigma_median": quantile(err_resid, 50),
        "abs_error_over_residual_psd_sigma_p90": quantile(err_resid, 90),
        "residual_psd_sigma_coverage_1": coverage(err_resid, 1.0),
        "residual_psd_sigma_coverage_2": coverage(err_resid, 2.0),
        "residual_psd_sigma_coverage_3": coverage(err_resid, 3.0),
        "median_estimator_sigma_ppm": quantile(
            [r["estimator_sigma_ppm"] for r in valid], 50
        ),
        "median_oracle_psd_sigma_ppm": quantile(
            [r["oracle_psd_sigma_ppm"] for r in valid], 50
        ),
        "median_residual_psd_sigma_ppm": quantile(
            [r["residual_psd_sigma_ppm"] for r in valid], 50
        ),
        "median_oracle_local_psd_v2_per_hz": quantile(
            [r["oracle_local_psd_median_v2_per_hz"] for r in valid], 50
        ),
        "median_residual_local_psd_v2_per_hz": quantile(
            [r["residual_local_psd_median_v2_per_hz"] for r in valid], 50
        ),
    }


def med_ratio_ratio(rows, numerator_key, denominator_key):
    ratios = []
    for row in rows:
        a, b = row.get(numerator_key), row.get(denominator_key)
        if a is not None and b is not None and b > 0.0:
            ratios.append(a / b)
    return quantile(ratios, 50)


def main():
    rows = []
    for duration in DURATIONS_S:
        n = int(round(duration * FS))
        t = np.arange(n, dtype=np.float64) / FS
        pilot = amp(PILOT_DBFS) * np.sin(2.0 * np.pi * PILOT_TRUE_HZ * t + 0.27)
        for model_index, model in enumerate(NOISE_MODELS):
            for trial in range(PAIRS_PER_CELL):
                seed = SEED_BASE + int(duration * 10) * 100000 + model_index * 10000 + trial
                noise_pair = make_noise_pair(n, seed, model)
                for channel_index, channel in enumerate(("L", "R")):
                    result = fit_and_measure(
                        pilot + noise_pair[:, channel_index],
                        noise_pair[:, channel_index],
                        duration,
                    )
                    rows.append({
                        "duration_s": duration,
                        "noise_model": model,
                        "trial": trial,
                        "seed": seed,
                        "channel": channel,
                        "target_noise_rms_dbfs": NOISE_DBFS,
                        "target_channel_correlation": CHANNEL_RHO,
                        **result,
                    })

    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["duration_s"], row["noise_model"])].append(row)
    summary_by_cell = {
        f"{duration:g}s {model}": summarize_cell(group)
        for (duration, model), group in sorted(grouped.items())
    }
    output = {
        "summary": {
            "task": "v2240 empirical coverage of est_lsq uncertainty",
            "fs_hz": FS,
            "pilot_nominal_hz": PILOT_NOMINAL_HZ,
            "pilot_true_hz": PILOT_TRUE_HZ,
            "pilot_offset_ppm": PILOT_OFFSET_PPM,
            "pilot_level_dbfs": PILOT_DBFS,
            "noise_rms_dbfs": NOISE_DBFS,
            "durations_s": list(DURATIONS_S),
            "noise_models": list(NOISE_MODELS),
            "independent_pairs_per_cell": PAIRS_PER_CELL,
            "channel_rows_expected": len(DURATIONS_S) * len(NOISE_MODELS) * PAIRS_PER_CELL * 2,
            "target_L_R_noise_correlation": CHANNEL_RHO,
            "local_psd_band_hz": [
                LOCAL_PSD_MIN_OFFSET_HZ,
                LOCAL_PSD_MAX_OFFSET_HZ,
            ],
            "welch_nperseg": WELCH_NPERSEG,
            "crlb_formula": "sigma_f = sqrt(3*N0/(pi^2*A^2*T^3)); N0 is one-sided local PSD and A is sinusoidal peak amplitude",
            "coverage_definition": "fraction of channel rows with abs(fit_error) <= k*sigma, reported for k=1,2,3; L/R rows are paired and correlated, not independent samples",
            "summary_by_cell": summary_by_cell,
            "interpretation": "The oracle PSD uses injected noise, while the residual PSD is estimated after subtracting the fitted sine/DC. The estimator sigma is the frozen est_lsq residual-curvature field. This is diagnostic evidence only; no production change is proposed.",
        },
        "per_trial": rows,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
    }
    path = HERE / "v2240-estimator-sigma-coverage.json"
    path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(output["summary"], ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {path}", flush=True)


if __name__ == "__main__":
    main()
