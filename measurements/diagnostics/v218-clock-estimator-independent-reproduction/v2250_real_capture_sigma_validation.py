#!/usr/bin/env python3
"""v2.25.0 — calibrate est_lsq uncertainty against captured real input noise.

The WAV contains a real, no-playback, two-channel audio capture. A known
pilot is injected digitally into non-overlapping noise windows so the true
frequency is known exactly in the captured sample-timebase. This isolates
noise/PSD behaviour; it does NOT test DAC/ADC clock mismatch or analog pilot
transfer. The frozen v2.18.8 est_lsq implementation is used unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import scipy
from scipy.io import wavfile
from scipy.signal import welch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from v2188_estimators import est_lsq

FS = 96000.0
PILOT_NOMINAL_HZ = 1000.0
PILOT_OFFSET_PPM = -23.5
PILOT_TRUE_HZ = PILOT_NOMINAL_HZ * (1.0 + PILOT_OFFSET_PPM * 1e-6)
DURATIONS_S = (2.0, 4.0, 6.0)
TARGET_SNR_DB = (8.0, 16.0)
PAIRS = 64
BLOCK_S = 6.0
SEED = 225000
LOCAL_PSD_MIN_HZ = 2.0
LOCAL_PSD_MAX_HZ = 32.0
WELCH_NPERSEG = 65536
MAX_PILOT_PEAK = 10.0 ** (-12.0 / 20.0)
MAX_COMBINED_PEAK = 0.98


def finite(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_wav(path: Path):
    fs, raw = wavfile.read(str(path))
    if int(fs) != int(FS):
        raise ValueError(f"capture sample rate must be 96000 Hz, got {fs}")
    if raw.ndim != 2 or raw.shape[1] != 2:
        raise ValueError(f"capture must be stereo (2 channels), got shape {raw.shape}")

    if np.issubdtype(raw.dtype, np.signedinteger):
        scale = float(2 ** (np.iinfo(raw.dtype).bits - 1))
        data = raw.astype(np.float64) / scale
    elif np.issubdtype(raw.dtype, np.unsignedinteger):
        info = np.iinfo(raw.dtype)
        midpoint = float((info.max + 1) / 2)
        data = (raw.astype(np.float64) - midpoint) / midpoint
    elif np.issubdtype(raw.dtype, np.floating):
        data = raw.astype(np.float64)
    else:
        raise ValueError(f"unsupported WAV dtype {raw.dtype}")

    if not np.all(np.isfinite(data)):
        raise ValueError("capture contains NaN/Inf")
    frames_required = int(FS * BLOCK_S * PAIRS)
    if len(data) < frames_required:
        raise ValueError(
            f"capture too short: {len(data)} frames; require at least {frames_required}"
        )
    clipped = int(np.count_nonzero(np.abs(data) >= 0.999))
    if clipped:
        raise ValueError(
            f"capture has {clipped} samples at or above 0.999 FS; reject clipped source"
        )
    return int(fs), data, raw.dtype.name, clipped


def ac_rms(x: np.ndarray) -> float:
    centered = np.asarray(x, dtype=np.float64) - float(np.mean(x))
    return float(np.sqrt(np.mean(centered * centered)))


def sine_equivalent_dbfs(rms: float):
    if rms <= 0.0:
        return None
    return 20.0 * math.log10(rms * math.sqrt(2.0))


def local_psd(x: np.ndarray):
    nperseg = min(WELCH_NPERSEG, len(x))
    freq, power = welch(
        np.asarray(x, dtype=np.float64),
        fs=FS,
        nperseg=nperseg,
        noverlap=nperseg // 2,
        scaling="density",
        detrend="constant",
    )
    offset = np.abs(freq - PILOT_TRUE_HZ)
    keep = (offset >= LOCAL_PSD_MIN_HZ) & (offset <= LOCAL_PSD_MAX_HZ)
    if not np.any(keep):
        raise RuntimeError("local PSD band has no bins")
    return float(np.median(power[keep])), float(np.mean(power[keep])), float(freq[1] - freq[0])


def crlb_sigma_hz(n0_one_sided: float, peak_amplitude: float, duration_s: float):
    if n0_one_sided <= 0.0 or peak_amplitude <= 0.0 or duration_s <= 0.0:
        return None
    return math.sqrt(
        3.0 * n0_one_sided /
        (math.pi * math.pi * peak_amplitude * peak_amplitude * duration_s ** 3)
    )


def describe(values):
    x = np.asarray([v for v in values if v is not None and math.isfinite(v)], dtype=float)
    if not x.size:
        return {"median": None, "p90": None, "max": None}
    return {
        "median": float(np.percentile(x, 50)),
        "p90": float(np.percentile(x, 90)),
        "max": float(np.max(x)),
    }


def coverage(rows, ratio_key):
    values = [r[ratio_key] for r in rows if r.get(ratio_key) is not None]
    if not values:
        return {"1sigma": None, "2sigma": None, "3sigma": None}
    a = np.asarray(values, dtype=float)
    return {
        "1sigma": float(np.mean(a <= 1.0)),
        "2sigma": float(np.mean(a <= 2.0)),
        "3sigma": float(np.mean(a <= 3.0)),
    }


def summarize_population(rows):
    valid = [r for r in rows if r.get("fit_error_ppm") is not None]
    left = {r["trial"]: r["fit_error_ppm"] for r in valid if r["channel"] == "L"}
    right = {r["trial"]: r["fit_error_ppm"] for r in valid if r["channel"] == "R"}
    common = sorted(set(left) & set(right))
    corr = (
        float(np.corrcoef([left[i] for i in common], [right[i] for i in common])[0, 1])
        if len(common) > 2 else None
    )

    def vals(key):
        return [r.get(key) for r in valid if r.get(key) is not None]

    errors = [abs(r["fit_error_ppm"]) for r in valid]
    return {
        "fit_rows": len(valid),
        "paired_trials_with_both_fits": len(common),
        "signed_bias_L_ppm": float(np.mean(list(left.values()))) if left else None,
        "signed_bias_R_ppm": float(np.mean(list(right.values()))) if right else None,
        "std_L_ppm": float(np.std(list(left.values()), ddof=1)) if len(left) > 1 else None,
        "std_R_ppm": float(np.std(list(right.values()), ddof=1)) if len(right) > 1 else None,
        "L_R_error_correlation": corr,
        "abs_error_ppm": describe(errors),
        "abs_error_over_estimator_sigma": describe(vals("abs_error_over_estimator_sigma")),
        "abs_error_over_oracle_psd_sigma": describe(vals("abs_error_over_oracle_psd_sigma")),
        "abs_error_over_residual_psd_sigma": describe(vals("abs_error_over_residual_psd_sigma")),
        "estimator_sigma_coverage": coverage(valid, "abs_error_over_estimator_sigma"),
        "oracle_psd_sigma_coverage": coverage(valid, "abs_error_over_oracle_psd_sigma"),
        "residual_psd_sigma_coverage": coverage(valid, "abs_error_over_residual_psd_sigma"),
        "median_estimator_sigma_ppm": float(np.median(vals("estimator_sigma_ppm"))) if vals("estimator_sigma_ppm") else None,
        "median_oracle_psd_sigma_ppm": float(np.median(vals("oracle_psd_sigma_ppm"))) if vals("oracle_psd_sigma_ppm") else None,
        "median_residual_psd_sigma_ppm": float(np.median(vals("residual_psd_sigma_ppm"))) if vals("residual_psd_sigma_ppm") else None,
        "median_estimator_over_oracle_sigma": float(np.median([
            r["estimator_sigma_ppm"] / r["oracle_psd_sigma_ppm"]
            for r in valid if r.get("estimator_sigma_ppm") and r.get("oracle_psd_sigma_ppm")
        ])) if any(r.get("estimator_sigma_ppm") and r.get("oracle_psd_sigma_ppm") for r in valid) else None,
        "median_residual_over_oracle_sigma": float(np.median([
            r["residual_psd_sigma_ppm"] / r["oracle_psd_sigma_ppm"]
            for r in valid if r.get("residual_psd_sigma_ppm") and r.get("oracle_psd_sigma_ppm")
        ])) if any(r.get("residual_psd_sigma_ppm") and r.get("oracle_psd_sigma_ppm") for r in valid) else None,
        "median_actual_snr_L_db": float(np.median([
            r["actual_snr_db"] for r in valid if r["channel"] == "L" and r.get("actual_snr_db") is not None
        ])) if any(r["channel"] == "L" and r.get("actual_snr_db") is not None for r in valid) else None,
        "median_actual_snr_R_db": float(np.median([
            r["actual_snr_db"] for r in valid if r["channel"] == "R" and r.get("actual_snr_db") is not None
        ])) if any(r["channel"] == "R" and r.get("actual_snr_db") is not None for r in valid) else None,
    }


def measure_one(noise: np.ndarray, pilot_peak: float, phase: float, duration_s: float):
    n = len(noise)
    t = np.arange(n, dtype=np.float64) / FS
    pilot = pilot_peak * np.sin(2.0 * math.pi * PILOT_TRUE_HZ * t + phase)
    signal = noise + pilot

    fit = est_lsq(signal, FS, PILOT_NOMINAL_HZ)
    f_est = finite(fit.get("f_est"))
    var_hz2 = finite(fit.get("variance_hz2"))
    sigma_est_hz = math.sqrt(var_hz2) if var_hz2 is not None and var_hz2 > 0 else None
    error_ppm = (
        (f_est - PILOT_TRUE_HZ) / PILOT_TRUE_HZ * 1e6
        if f_est is not None else None
    )

    empty = {
        "f_est_hz": f_est,
        "fit_error_ppm": error_ppm,
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
    if f_est is None:
        return empty

    design = np.column_stack((
        np.sin(2.0 * math.pi * f_est * t),
        np.cos(2.0 * math.pi * f_est * t),
        np.ones(n),
    ))
    coef, *_ = np.linalg.lstsq(design, signal, rcond=None)
    residual = signal - design @ coef
    fitted_peak = float(math.hypot(coef[0], coef[1]))

    oracle_med, oracle_mean, df = local_psd(noise)
    residual_med, residual_mean, _ = local_psd(residual)
    oracle_hz = crlb_sigma_hz(oracle_med, pilot_peak, duration_s)
    residual_hz = crlb_sigma_hz(residual_med, fitted_peak, duration_s)

    def ppm(sigma_hz):
        return sigma_hz / PILOT_TRUE_HZ * 1e6 if sigma_hz is not None else None

    sigma_est_ppm = ppm(sigma_est_hz)
    oracle_ppm = ppm(oracle_hz)
    residual_ppm = ppm(residual_hz)
    abs_error = abs(error_ppm) if error_ppm is not None else None

    def ratio(sigma):
        return abs_error / sigma if abs_error is not None and sigma is not None and sigma > 0 else None

    empty.update({
        "estimator_sigma_ppm": sigma_est_ppm,
        "fitted_peak_amplitude": fitted_peak,
        "oracle_local_psd_median_v2_per_hz": oracle_med,
        "oracle_local_psd_mean_v2_per_hz": oracle_mean,
        "residual_local_psd_median_v2_per_hz": residual_med,
        "residual_local_psd_mean_v2_per_hz": residual_mean,
        "psd_bin_width_hz": df,
        "oracle_psd_sigma_ppm": oracle_ppm,
        "residual_psd_sigma_ppm": residual_ppm,
        "abs_error_over_estimator_sigma": ratio(sigma_est_ppm),
        "abs_error_over_oracle_psd_sigma": ratio(oracle_ppm),
        "abs_error_over_residual_psd_sigma": ratio(residual_ppm),
    })
    return empty


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", required=True, help="real 96 kHz stereo WAV captured with no playback")
    parser.add_argument("--output", required=True, help="directory for JSON output")
    args = parser.parse_args()

    wav_path = Path(args.wav).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()
    if not wav_path.is_file():
        raise FileNotFoundError(wav_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    fs, data, dtype_name, clipped = read_wav(wav_path)
    required_frames = int(FS * BLOCK_S * PAIRS)
    frames_available = int(len(data))
    data = data[:required_frames]
    capture_sha = sha256_file(wav_path)
    capture_bytes = wav_path.stat().st_size

    rows = []
    block_noise_metrics = []
    grouped = defaultdict(list)

    for trial in range(PAIRS):
        block_start = int(trial * BLOCK_S * FS)
        block = data[block_start:block_start + int(BLOCK_S * FS), :]
        phase = float(np.random.default_rng(SEED + trial).uniform(0.0, 2.0 * math.pi))

        for duration_s in DURATIONS_S:
            n = int(duration_s * FS)
            noise_pair = block[:n, :]
            noise_rms_ch = [ac_rms(noise_pair[:, ch]) for ch in range(2)]
            pair_rms = math.sqrt((noise_rms_ch[0] ** 2 + noise_rms_ch[1] ** 2) / 2.0)
            if pair_rms <= 0.0:
                raise ValueError(f"zero input noise in trial {trial}, duration {duration_s}")
            block_noise_metrics.append({
                "trial": trial,
                "duration_s": duration_s,
                "noise_rms_dbfs_L": sine_equivalent_dbfs(noise_rms_ch[0]),
                "noise_rms_dbfs_R": sine_equivalent_dbfs(noise_rms_ch[1]),
            })

            for target_snr_db in TARGET_SNR_DB:
                pilot_peak = math.sqrt(2.0) * pair_rms * 10.0 ** (target_snr_db / 20.0)
                combined_peak = float(np.max(np.abs(noise_pair))) + pilot_peak
                skip_reason = None
                if pilot_peak > MAX_PILOT_PEAK:
                    skip_reason = "pilot_peak_above_-12dBFS"
                elif combined_peak >= MAX_COMBINED_PEAK:
                    skip_reason = "combined_peak_headroom"
                cell = (duration_s, target_snr_db)

                for channel_index, channel in enumerate(("L", "R")):
                    noise = noise_pair[:, channel_index]
                    noise_rms = noise_rms_ch[channel_index]
                    actual_snr = (
                        20.0 * math.log10((pilot_peak / math.sqrt(2.0)) / noise_rms)
                        if noise_rms > 0 else None
                    )
                    if skip_reason:
                        result = {
                            "f_est_hz": None, "fit_error_ppm": None,
                            "estimator_status": "skipped_headroom",
                            "estimator_sigma_ppm": None, "fitted_peak_amplitude": None,
                            "oracle_local_psd_median_v2_per_hz": None,
                            "oracle_local_psd_mean_v2_per_hz": None,
                            "residual_local_psd_median_v2_per_hz": None,
                            "residual_local_psd_mean_v2_per_hz": None,
                            "psd_bin_width_hz": None, "oracle_psd_sigma_ppm": None,
                            "residual_psd_sigma_ppm": None,
                            "abs_error_over_estimator_sigma": None,
                            "abs_error_over_oracle_psd_sigma": None,
                            "abs_error_over_residual_psd_sigma": None,
                        }
                    else:
                        result = measure_one(noise, pilot_peak, phase, duration_s)

                    row = {
                        "duration_s": duration_s,
                        "target_snr_db": target_snr_db,
                        "trial": trial,
                        "channel": channel,
                        "noise_rms_dbfs": sine_equivalent_dbfs(noise_rms),
                        "pilot_peak_dbfs": 20.0 * math.log10(pilot_peak) if pilot_peak > 0 else None,
                        "actual_snr_db": actual_snr,
                        "input_peak": float(np.max(np.abs(noise))),
                        "skip_reason": skip_reason,
                        **result,
                    }
                    rows.append(row)
                    grouped[cell].append(row)

    summary_by_cell = {}
    for (duration_s, target_snr_db), cell_rows in sorted(grouped.items()):
        status_counts = defaultdict(int)
        for row in cell_rows:
            status_counts[str(row["estimator_status"])] += 1
        fit_rows = [r for r in cell_rows if r["fit_error_ppm"] is not None]
        accepted_rows = [r for r in fit_rows if r["estimator_status"] == "ok"]
        summary_by_cell[f"{duration_s:g}s SNR+{target_snr_db:g}dB"] = {
            "independent_nonoverlap_pairs": PAIRS,
            "channel_rows": len(cell_rows),
            "status_counts": dict(sorted(status_counts.items())),
            "all_fit": summarize_population(fit_rows),
            "status_ok_only": summarize_population(accepted_rows),
            "noise_rms_dbfs_L": describe([r["noise_rms_dbfs"] for r in cell_rows if r["channel"] == "L"]),
            "noise_rms_dbfs_R": describe([r["noise_rms_dbfs"] for r in cell_rows if r["channel"] == "R"]),
            "actual_snr_db_L": describe([r["actual_snr_db"] for r in cell_rows if r["channel"] == "L"]),
            "actual_snr_db_R": describe([r["actual_snr_db"] for r in cell_rows if r["channel"] == "R"]),
            "pilot_peak_dbfs": describe([r["pilot_peak_dbfs"] for r in cell_rows]),
        }

    summary = {
        "task": "v2250 real captured input-noise calibration of est_lsq sigma",
        "sample_rate_hz": fs,
        "channels": 2,
        "capture_path": str(wav_path),
        "capture_bytes": capture_bytes,
        "capture_sha256": capture_sha,
        "capture_dtype": dtype_name,
        "capture_frames_used": required_frames,
        "capture_seconds_used": required_frames / FS,
        "capture_frames_available": frames_available,
        "capture_clipped_samples": clipped,
        "pairs_per_cell": PAIRS,
        "durations_s": list(DURATIONS_S),
        "target_snr_db": list(TARGET_SNR_DB),
        "pilot_nominal_hz": PILOT_NOMINAL_HZ,
        "pilot_true_hz": PILOT_TRUE_HZ,
        "pilot_offset_ppm": PILOT_OFFSET_PPM,
        "local_psd_band_offset_hz": [LOCAL_PSD_MIN_HZ, LOCAL_PSD_MAX_HZ],
        "welch_nperseg": WELCH_NPERSEG,
        "coverage_definition": "fraction of channel rows with abs(error) <= k*sigma, k=1,2,3; paired L/R rows are not independent",
        "windowing": "64 non-overlapping 6-second source blocks; 2s and 4s windows use prefixes of each block; same blocks are reused across durations and SNR cells, so cells are paired",
        "production_acceptance": "status_ok_only population uses estimator_status == 'ok'; all_fit is diagnostic and includes non-ok fit statuses",
        "summary_by_cell": summary_by_cell,
        "block_noise_metrics": block_noise_metrics,
        "interpretation": "The captured noise is real hardware input noise. The pilot is added digitally after capture, so this validates estimator/noise uncertainty on the captured sample-timebase only; it does not test analog pilot transfer, DAC/ADC clock mismatch, or sampling jitter.",
    }
    output = {
        "summary": summary,
        "per_trial": rows,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "platform": platform.platform(),
        },
    }
    out_path = output_dir / "v2250-real-noise-sigma-validation.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {out_path}", flush=True)


if __name__ == "__main__":
    main()
