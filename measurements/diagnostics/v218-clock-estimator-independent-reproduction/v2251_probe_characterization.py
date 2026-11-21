#!/usr/bin/env python3
"""v2.25.1 — read-only characterization of the already captured 10 s probe.

No hardware access, recording, playback, filtering of source data for the
main analysis, production imports, or estimator changes. This script only
describes the existing probe WAV and cannot identify its physical source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
from pathlib import Path

import numpy as np
import scipy
from scipy.io import wavfile
from scipy.signal import (
    butter,
    coherence,
    csd,
    hilbert,
    sosfiltfilt,
    welch,
)

FS_EXPECTED = 96000
BLOCK_S = 0.5
LF_BAND_HZ = (8.0, 20.0)
PILOT_LOCAL_BAND_HZ = (968.0, 1032.0)
PILOT_EXCLUSION_HZ = 2.0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def db10(value: float):
    if value is None or not math.isfinite(float(value)) or value <= 0:
        return None
    return float(10.0 * math.log10(float(value)))


def db20(value: float):
    if value is None or not math.isfinite(float(value)) or value <= 0:
        return None
    return float(20.0 * math.log10(float(value)))


def sine_rms_dbfs(x: np.ndarray):
    rms = float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))
    return db20(rms * math.sqrt(2.0))


def convert_wav(raw: np.ndarray):
    if np.issubdtype(raw.dtype, np.signedinteger):
        scale = float(2 ** (np.iinfo(raw.dtype).bits - 1))
        return raw.astype(np.float64) / scale, f"signed_int_{np.iinfo(raw.dtype).bits}"
    if np.issubdtype(raw.dtype, np.unsignedinteger):
        info = np.iinfo(raw.dtype)
        midpoint = float((info.max + 1) / 2)
        return (raw.astype(np.float64) - midpoint) / midpoint, f"unsigned_int_{np.iinfo(raw.dtype).bits}"
    if np.issubdtype(raw.dtype, np.floating):
        return raw.astype(np.float64), f"float_{raw.dtype.itemsize * 8}"
    raise ValueError(f"unsupported WAV dtype: {raw.dtype}")


def sinusoid_fit(x: np.ndarray, fs: float, frequency: float):
    t = np.arange(len(x), dtype=np.float64) / fs
    matrix = np.column_stack((
        np.sin(2.0 * np.pi * frequency * t),
        np.cos(2.0 * np.pi * frequency * t),
        np.ones(len(t)),
    ))
    coef, *_ = np.linalg.lstsq(matrix, x, rcond=None)
    amp = float(math.hypot(coef[0], coef[1]))
    phase = float(math.atan2(coef[1], coef[0]))
    residual = x - matrix @ coef
    return {
        "frequency_hz": float(frequency),
        "peak_amplitude_fs": amp,
        "peak_amplitude_dbfs": db20(amp),
        "phase_rad": phase,
        "residual_rms_fs": float(np.sqrt(np.mean(residual * residual))),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", required=True, help="existing probe WAV; no capture is performed")
    parser.add_argument("--output", required=True, help="JSON output directory")
    args = parser.parse_args()

    wav_path = Path(args.wav).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()
    if not wav_path.is_file():
        raise FileNotFoundError(
            f"existing probe is missing: {wav_path}; do not create a new capture"
        )

    fs, raw = wavfile.read(str(wav_path))
    if int(fs) != FS_EXPECTED:
        raise ValueError(f"expected {FS_EXPECTED} Hz, got {fs}")
    if raw.ndim != 2 or raw.shape[1] != 2:
        raise ValueError(f"expected stereo WAV, got shape {raw.shape}")
    data, dtype_label = convert_wav(raw)
    if not np.all(np.isfinite(data)):
        raise ValueError("non-finite samples")
    if len(data) < int(9.0 * fs):
        raise ValueError(f"probe is unexpectedly short: {len(data) / fs:.3f} s")

    output_dir.mkdir(parents=True, exist_ok=True)
    seconds = len(data) / fs
    capture_sha = sha256_file(wav_path)
    capture_bytes = wav_path.stat().st_size
    peak = np.max(np.abs(data), axis=0)
    clip_count = np.count_nonzero(np.abs(data) >= 0.999, axis=0)

    centered = data - np.mean(data, axis=0, keepdims=True)
    sos_hp = butter(4, 300.0, btype="highpass", fs=fs, output="sos")
    highpassed = sosfiltfilt(sos_hp, centered, axis=0)
    sos_lf = butter(4, LF_BAND_HZ, btype="bandpass", fs=fs, output="sos")
    lowband = sosfiltfilt(sos_lf, centered, axis=0)

    # Full-record spectra with 0.1 Hz nominal bin spacing.
    nperseg = len(data)
    freq_l, psd_l = welch(
        centered[:, 0], fs=fs, window="hann", nperseg=nperseg,
        noverlap=0, nfft=1 << int(math.ceil(math.log2(nperseg))),
        detrend="constant", scaling="density",
    )
    freq_r, psd_r = welch(
        centered[:, 1], fs=fs, window="hann", nperseg=nperseg,
        noverlap=0, nfft=1 << int(math.ceil(math.log2(nperseg))),
        detrend="constant", scaling="density",
    )
    band = (freq_l >= LF_BAND_HZ[0]) & (freq_l <= LF_BAND_HZ[1])
    if not np.any(band):
        raise RuntimeError("no 8–20 Hz PSD bins")
    band_indices = np.flatnonzero(band)
    peak_index_l = int(band_indices[np.argmax(psd_l[band])])
    peak_index_r = int(band_indices[np.argmax(psd_r[band])])
    lf_peak_hz_l = float(freq_l[peak_index_l])
    lf_peak_hz_r = float(freq_r[peak_index_r])

    # Refine the dominant shared frequency over the combined 8–20 Hz power.
    combined = psd_l + psd_r
    shared_peak_index = int(band_indices[np.argmax(combined[band])])
    shared_peak_hz = float(freq_l[shared_peak_index])
    freq_step = float(freq_l[1] - freq_l[0])
    grid = np.arange(shared_peak_hz - 0.5, shared_peak_hz + 0.5001, freq_step)
    fits = {
        "L": max(
            (sinusoid_fit(centered[:, 0], fs, f) for f in grid),
            key=lambda item: item["peak_amplitude_fs"],
        ),
        "R": max(
            (sinusoid_fit(centered[:, 1], fs, f) for f in grid),
            key=lambda item: item["peak_amplitude_fs"],
        ),
    }

    # Estimate narrowband coherence/phase around the strongest common LF line.
    coh_f, coh = coherence(
        centered[:, 0], centered[:, 1], fs=fs,
        window="hann", nperseg=min(int(2 * fs), len(data)),
        noverlap=min(int(fs), max(0, len(data) - int(2 * fs))),
    )
    csd_f, cross = csd(
        centered[:, 0], centered[:, 1], fs=fs,
        window="hann", nperseg=min(int(2 * fs), len(data)),
        noverlap=min(int(fs), max(0, len(data) - int(2 * fs))),
        scaling="density",
    )
    ci = int(np.argmin(np.abs(coh_f - shared_peak_hz)))
    xi = int(np.argmin(np.abs(csd_f - shared_peak_hz)))
    lf_corr = float(np.corrcoef(lowband[:, 0], lowband[:, 1])[0, 1])

    analytic = hilbert(lowband, axis=0)
    envelope = np.abs(analytic)
    inst_phase = np.unwrap(np.angle(analytic), axis=0)
    inst_freq = np.diff(inst_phase, axis=0) * fs / (2.0 * np.pi)

    block_count = int(math.floor(seconds / BLOCK_S))
    blocks = []
    for block_index in range(block_count):
        start = int(round(block_index * BLOCK_S * fs))
        end = int(round((block_index + 1) * BLOCK_S * fs))
        slice_raw = centered[start:end]
        slice_hp = highpassed[start:end]
        slice_lf = lowband[start:end]
        slice_env = envelope[start:end]
        slice_if = inst_freq[max(0, start):max(0, end - 1)]
        blocks.append({
            "block": block_index,
            "start_s": block_index * BLOCK_S,
            "end_s": (block_index + 1) * BLOCK_S,
            "fullband_rms_dbfs_L": sine_rms_dbfs(slice_raw[:, 0]),
            "fullband_rms_dbfs_R": sine_rms_dbfs(slice_raw[:, 1]),
            "above_300hz_rms_dbfs_L": sine_rms_dbfs(slice_hp[:, 0]),
            "above_300hz_rms_dbfs_R": sine_rms_dbfs(slice_hp[:, 1]),
            "8_20hz_rms_dbfs_L": sine_rms_dbfs(slice_lf[:, 0]),
            "8_20hz_rms_dbfs_R": sine_rms_dbfs(slice_lf[:, 1]),
            "lf_envelope_rms_dbfs_L": sine_rms_dbfs(slice_env[:, 0]),
            "lf_envelope_rms_dbfs_R": sine_rms_dbfs(slice_env[:, 1]),
            "lf_instantaneous_frequency_median_hz_L": float(np.median(slice_if[:, 0])) if len(slice_if) else None,
            "lf_instantaneous_frequency_median_hz_R": float(np.median(slice_if[:, 1])) if len(slice_if) else None,
        })

    # Harmonic fit around n*f0; do not claim source identity from this pattern.
    harmonics = []
    f0 = shared_peak_hz
    for harmonic in range(1, min(10, int((fs / 2) // f0)) + 1):
        target = f0 * harmonic
        if target >= fs / 2:
            break
        item_l = sinusoid_fit(centered[:, 0], fs, target)
        item_r = sinusoid_fit(centered[:, 1], fs, target)
        harmonics.append({
            "harmonic": harmonic,
            "frequency_hz": target,
            "L_peak_dbfs": item_l["peak_amplitude_dbfs"],
            "R_peak_dbfs": item_r["peak_amplitude_dbfs"],
        })

    # Noise level in the pilot-adjacent band; no pilot is present in this probe.
    freq_p, psd_p_l = welch(centered[:, 0], fs=fs, window="hann",
                            nperseg=min(65536, len(data)),
                            noverlap=min(32768, max(0, len(data)-min(65536,len(data)))),
                            detrend="constant", scaling="density")
    _, psd_p_r = welch(centered[:, 1], fs=fs, window="hann",
                       nperseg=min(65536, len(data)),
                       noverlap=min(32768, max(0, len(data)-min(65536,len(data)))),
                       detrend="constant", scaling="density")
    pilot_band = (freq_p >= PILOT_LOCAL_BAND_HZ[0]) & (freq_p <= PILOT_LOCAL_BAND_HZ[1])
    pilot_band_psd = {
        "frequency_bins": int(np.count_nonzero(pilot_band)),
        "median_psd_dbfs2_per_hz_L": db10(float(np.median(psd_p_l[pilot_band]))) if np.any(pilot_band) else None,
        "median_psd_dbfs2_per_hz_R": db10(float(np.median(psd_p_r[pilot_band]))) if np.any(pilot_band) else None,
        "mean_psd_dbfs2_per_hz_L": db10(float(np.mean(psd_p_l[pilot_band]))) if np.any(pilot_band) else None,
        "mean_psd_dbfs2_per_hz_R": db10(float(np.mean(psd_p_r[pilot_band]))) if np.any(pilot_band) else None,
    }

    summary = {
        "task": "v2251 read-only characterization of existing 10s probe",
        "source_wav": str(wav_path),
        "source_bytes": capture_bytes,
        "source_sha256": capture_sha,
        "sample_rate_hz": int(fs),
        "channels": 2,
        "frames": int(len(data)),
        "duration_s": seconds,
        "sample_dtype": dtype_label,
        "peak_dbfs_L": db20(float(peak[0])),
        "peak_dbfs_R": db20(float(peak[1])),
        "clip_samples_L": int(clip_count[0]),
        "clip_samples_R": int(clip_count[1]),
        "fullband_rms_dbfs_L": sine_rms_dbfs(centered[:, 0]),
        "fullband_rms_dbfs_R": sine_rms_dbfs(centered[:, 1]),
        "above_300hz_rms_dbfs_L": sine_rms_dbfs(highpassed[:, 0]),
        "above_300hz_rms_dbfs_R": sine_rms_dbfs(highpassed[:, 1]),
        "dominant_8_20hz_psd_peak_hz_L": lf_peak_hz_l,
        "dominant_8_20hz_psd_peak_hz_R": lf_peak_hz_r,
        "shared_8_20hz_peak_hz": shared_peak_hz,
        "frequency_bin_spacing_hz": freq_step,
        "shared_peak_fit_L": fits["L"],
        "shared_peak_fit_R": fits["R"],
        "8_20hz_filtered_LR_correlation": lf_corr,
        "coherence_at_shared_peak": float(coh[ci]),
        "cross_spectral_phase_at_shared_peak_rad": float(np.angle(cross[xi])),
        "median_8_20hz_envelope_rms_dbfs_L": sine_rms_dbfs(envelope[:, 0]),
        "median_8_20hz_envelope_rms_dbfs_R": sine_rms_dbfs(envelope[:, 1]),
        "median_instantaneous_frequency_hz_L": float(np.median(inst_freq[:, 0])),
        "median_instantaneous_frequency_hz_R": float(np.median(inst_freq[:, 1])),
        "pilot_adjacent_noise_band": pilot_band_psd,
        "harmonics_of_shared_peak": harmonics,
        "block_count": block_count,
        "block_duration_s": BLOCK_S,
        "interpretation_limit": "This analysis describes signal components only. It cannot identify their physical source or establish that the unknown analog path is safe/known/quiet.",
    }
    output = {
        "summary": summary,
        "blocks": blocks,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "platform": platform.platform(),
        },
    }
    out = output_dir / "v2251-probe-characterization.json"
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {out}", flush=True)


if __name__ == "__main__":
    main()
