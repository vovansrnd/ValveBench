#!/usr/bin/env python3
"""v2.25.1 — read-only analysis of the already existing 10-second probe WAV.

No ALSA, hardware, playback, recording, production imports, estimator edits,
or source-file modification. Results describe the captured signal only and
cannot identify the physical source of any component.
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
from scipy.optimize import minimize_scalar
from scipy.signal import butter, coherence, periodogram, sosfiltfilt, welch

FS_EXPECTED = 96000
BLOCK_S = 0.5
LF_BAND_HZ = (8.0, 20.0)
PILOT_BAND_HZ = (968.0, 1032.0)
WELCH_NPERSEG = 65536


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def finite_db(value: float, power: bool = False):
    value = float(value)
    if not math.isfinite(value) or value <= 0.0:
        return None
    return 10.0 * math.log10(value) if power else 20.0 * math.log10(value)


def level_metrics(x: np.ndarray):
    x = np.asarray(x, dtype=np.float64)
    rms = float(np.sqrt(np.mean(x * x)))
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    return {
        "rms_fs": rms,
        "rms_dbfs": finite_db(rms),
        "sine_equivalent_dbfs": finite_db(rms * math.sqrt(2.0)),
        "peak_fs": peak,
        "peak_dbfs": finite_db(peak),
    }


def decode_wav(raw: np.ndarray):
    if np.issubdtype(raw.dtype, np.signedinteger):
        bits = np.iinfo(raw.dtype).bits
        return raw.astype(np.float64) / float(2 ** (bits - 1)), f"signed_int_{bits}"
    if np.issubdtype(raw.dtype, np.unsignedinteger):
        info = np.iinfo(raw.dtype)
        midpoint = float((info.max + 1) / 2)
        return (raw.astype(np.float64) - midpoint) / midpoint, f"unsigned_int_{info.bits}"
    if np.issubdtype(raw.dtype, np.floating):
        return raw.astype(np.float64), f"float_{raw.dtype.itemsize * 8}"
    raise ValueError(f"unsupported WAV dtype {raw.dtype}")


def spectral_peak(freq: np.ndarray, psd: np.ndarray, lo: float, hi: float):
    keep = (freq >= lo) & (freq <= hi)
    if not np.any(keep):
        raise RuntimeError(f"no spectral bins in {lo}..{hi} Hz")
    idxs = np.flatnonzero(keep)
    idx = int(idxs[np.argmax(psd[keep])])
    return {
        "frequency_hz": float(freq[idx]),
        "psd_fs2_per_hz": float(psd[idx]),
        "psd_dbfs2_per_hz": finite_db(psd[idx], power=True),
        "bin_index": idx,
    }


def fit_sine(x: np.ndarray, fs: float, frequency: float):
    t = np.arange(len(x), dtype=np.float64) / fs
    s = np.sin(2.0 * np.pi * frequency * t)
    c = np.cos(2.0 * np.pi * frequency * t)
    design = np.column_stack((s, c, np.ones(len(x))))
    coef, *_ = np.linalg.lstsq(design, x, rcond=None)
    residual = x - design @ coef
    amp = float(math.hypot(coef[0], coef[1]))
    return {
        "frequency_hz": float(frequency),
        "peak_amplitude_fs": amp,
        "peak_amplitude_dbfs": finite_db(amp),
        "phase_rad": float(math.atan2(coef[1], coef[0])),
        "residual_rms_fs": float(np.sqrt(np.mean(residual * residual))),
    }


def best_sine_frequency(x: np.ndarray, fs: float, center_hz: float):
    t = np.arange(len(x), dtype=np.float64) / fs
    x = np.asarray(x, dtype=np.float64)
    x = x - float(np.mean(x))

    def objective(frequency):
        z = np.sum(x * np.exp(-2j * np.pi * frequency * t))
        return -float(abs(z))

    result = minimize_scalar(
        objective,
        bounds=(max(1.0, center_hz - 0.5), center_hz + 0.5),
        method="bounded",
        options={"xatol": 1e-7, "maxiter": 100},
    )
    return fit_sine(x, fs, float(result.x))


def describe(values):
    x = np.asarray([v for v in values if v is not None and math.isfinite(float(v))], dtype=float)
    if not x.size:
        return {"min": None, "p10": None, "median": None, "p90": None, "max": None}
    return {
        "min": float(np.min(x)),
        "p10": float(np.percentile(x, 10)),
        "median": float(np.percentile(x, 50)),
        "p90": float(np.percentile(x, 90)),
        "max": float(np.max(x)),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", required=True, help="existing probe WAV; this program never records")
    parser.add_argument("--output", required=True, help="directory for JSON result")
    args = parser.parse_args()

    wav_path = Path(args.wav).expanduser().resolve()
    out_dir = Path(args.output).expanduser().resolve()
    if not wav_path.is_file():
        raise FileNotFoundError(
            f"existing probe missing: {wav_path}; stop, do not record another file"
        )

    fs, raw = wavfile.read(str(wav_path))
    if int(fs) != FS_EXPECTED:
        raise ValueError(f"expected {FS_EXPECTED} Hz, got {fs}")
    if raw.ndim != 2 or raw.shape[1] != 2:
        raise ValueError(f"expected stereo WAV, got shape={raw.shape}")
    x, dtype_name = decode_wav(raw)
    if not np.all(np.isfinite(x)):
        raise ValueError("NaN/Inf in input WAV")
    if len(x) < int(9.0 * fs):
        raise ValueError(f"probe shorter than 9 seconds: {len(x) / fs:.3f}s")

    out_dir.mkdir(parents=True, exist_ok=True)
    frames = int(len(x))
    duration = frames / float(fs)
    centered = x - np.mean(x, axis=0, keepdims=True)
    clip_counts = np.count_nonzero(np.abs(x) >= 0.999, axis=0)

    sos_lf = butter(4, LF_BAND_HZ, btype="bandpass", fs=fs, output="sos")
    sos_hp = butter(4, 300.0, btype="highpass", fs=fs, output="sos")
    sos_pilot = butter(4, PILOT_BAND_HZ, btype="bandpass", fs=fs, output="sos")
    lf = sosfiltfilt(sos_lf, centered, axis=0)
    hp300 = sosfiltfilt(sos_hp, centered, axis=0)
    pilotband = sosfiltfilt(sos_pilot, centered, axis=0)

    # One full-record Hann periodogram: high frequency resolution for the
    # short 10-second capture. It is descriptive, not a calibrated PSD average.
    nfft = 1 << int(math.ceil(math.log2(frames)))
    spectra = []
    for channel in range(2):
        freq, psd = periodogram(
            centered[:, channel], fs=fs, window="hann", nfft=nfft,
            detrend="constant", scaling="density",
        )
        spectra.append((freq, psd))
    bin_spacing = float(freq[1] - freq[0])
    peak_l = spectral_peak(freq, spectra[0][1], *LF_BAND_HZ)
    peak_r = spectral_peak(freq, spectra[1][1], *LF_BAND_HZ)
    common_band = (freq >= LF_BAND_HZ[0]) & (freq <= LF_BAND_HZ[1])
    joint_psd = spectra[0][1] + spectra[1][1]
    joint_peak = spectral_peak(freq, joint_psd, *LF_BAND_HZ)
    common_frequency = joint_peak["frequency_hz"]

    # Refine each channel's dominant low-frequency sinusoid independently.
    sine_l = best_sine_frequency(centered[:, 0], fs, common_frequency)
    sine_r = best_sine_frequency(centered[:, 1], fs, common_frequency)

    # Two-second sliding fits, one-second hop, to quantify LF drift and
    # amplitude variation without creating or acquiring new samples.
    lf_windows = []
    window_frames = int(2.0 * fs)
    hop_frames = int(1.0 * fs)
    for start in range(0, frames - window_frames + 1, hop_frames):
        stop = start + window_frames
        lf_windows.append({
            "start_s": start / fs,
            "end_s": stop / fs,
            "L": best_sine_frequency(lf[start:stop, 0], fs, common_frequency),
            "R": best_sine_frequency(lf[start:stop, 1], fs, common_frequency),
            "search_center_hz": common_frequency,
            "search_half_width_hz": 0.5,
        })

    # Welch PSD close to the 1 kHz pilot band. No pilot was present in this
    # capture. Keep median and mean separately; neither identifies the source.
    pilot_noise = []
    welch_freq = None
    for channel in range(2):
        welch_freq, welch_psd = welch(
            centered[:, channel], fs=fs, window="hann",
            nperseg=min(WELCH_NPERSEG, frames),
            noverlap=min(WELCH_NPERSEG // 2, max(0, frames - min(WELCH_NPERSEG, frames))),
            detrend="constant", scaling="density",
        )
        mask = (welch_freq >= PILOT_BAND_HZ[0]) & (welch_freq <= PILOT_BAND_HZ[1])
        vals = welch_psd[mask]
        pilot_noise.append({
            "bin_count": int(vals.size),
            "median_psd_fs2_per_hz": float(np.median(vals)),
            "median_psd_dbfs2_per_hz": finite_db(np.median(vals), power=True),
            "mean_psd_fs2_per_hz": float(np.mean(vals)),
            "mean_psd_dbfs2_per_hz": finite_db(np.mean(vals), power=True),
            "max_psd_dbfs2_per_hz": finite_db(np.max(vals), power=True),
        })

    # Band-limited L/R correlation and coherence at the dominant LF frequency.
    lf_corr = float(np.corrcoef(lf[:, 0], lf[:, 1])[0, 1])
    coh_f, coh = coherence(
        centered[:, 0], centered[:, 1], fs=fs, window="hann",
        nperseg=min(2 * fs, frames), noverlap=min(fs, max(0, frames - min(2 * fs, frames))),
    )
    coh_index = int(np.argmin(np.abs(coh_f - common_frequency)))

    # Harmonic-bin maxima around the shared peak. These are descriptive only.
    harmonics = []
    for h in range(1, 11):
        f0 = common_frequency * h
        if f0 >= fs / 2:
            break
        row = {"harmonic": h, "center_frequency_hz": f0}
        for label, (f, p) in zip(("L", "R"), spectra):
            peak = spectral_peak(f, p, max(0.1, f0 - 0.5), f0 + 0.5)
            row[f"{label}_peak_frequency_hz"] = peak["frequency_hz"]
            row[f"{label}_peak_psd_dbfs2_per_hz"] = peak["psd_dbfs2_per_hz"]
        harmonics.append(row)

    block_count = int(math.floor(duration / BLOCK_S))
    blocks = []
    for index in range(block_count):
        start = int(round(index * BLOCK_S * fs))
        stop = int(round((index + 1) * BLOCK_S * fs))
        rec = {
            "block": index,
            "start_s": index * BLOCK_S,
            "end_s": (index + 1) * BLOCK_S,
        }
        for name, signal in (
            ("fullband", centered),
            ("8_20hz", lf),
            ("above_300hz", hp300),
            ("pilot_968_1032hz", pilotband),
        ):
            for ch, label in enumerate(("L", "R")):
                rec[f"{name}_{label}"] = level_metrics(signal[start:stop, ch])
        blocks.append(rec)

    summary = {
        "task": "v2251 read-only characterization of existing probe",
        "source_wav_path": str(wav_path),
        "source_wav_bytes": int(wav_path.stat().st_size),
        "source_wav_sha256": sha256_file(wav_path),
        "sample_rate_hz": int(fs),
        "frames": frames,
        "duration_s": duration,
        "sample_dtype": dtype_name,
        "clip_samples_L": int(clip_counts[0]),
        "clip_samples_R": int(clip_counts[1]),
        "dc_mean_fs_L": float(np.mean(x[:, 0])),
        "dc_mean_fs_R": float(np.mean(x[:, 1])),
        "fullband_L": level_metrics(centered[:, 0]),
        "fullband_R": level_metrics(centered[:, 1]),
        "above_300hz_L": level_metrics(hp300[:, 0]),
        "above_300hz_R": level_metrics(hp300[:, 1]),
        "8_20hz_L": level_metrics(lf[:, 0]),
        "8_20hz_R": level_metrics(lf[:, 1]),
        "pilot_968_1032hz_L": level_metrics(pilotband[:, 0]),
        "pilot_968_1032hz_R": level_metrics(pilotband[:, 1]),
        "full_record_periodogram": {
            "window": "hann",
            "nfft": int(nfft),
            "bin_spacing_hz": bin_spacing,
            "peak_8_20hz_L": peak_l,
            "peak_8_20hz_R": peak_r,
            "joint_peak_8_20hz": joint_peak,
        },
        "refined_low_frequency_sine_L": sine_l,
        "refined_low_frequency_sine_R": sine_r,
        "low_frequency_sliding_2s_windows_1s_hop": lf_windows,
        "8_20hz_filtered_LR_correlation": lf_corr,
        "coherence_at_joint_lf_peak": float(coh[coh_index]),
        "coherence_frequency_bin_hz": float(coh_f[coh_index]),
        "pilot_adjacent_noise_psd": {
            "band_hz": list(PILOT_BAND_HZ),
            "L": pilot_noise[0],
            "R": pilot_noise[1],
        },
        "harmonic_band_peaks": harmonics,
        "block_count": block_count,
        "block_duration_s": BLOCK_S,
        "interpretation_limit": (
            "This characterizes the captured waveform only. It cannot identify "
            "the physical source, certify the unknown analog connection as quiet, "
            "or authorize another hardware capture."
        ),
    }
    result = {
        "summary": summary,
        "blocks": blocks,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "platform": platform.platform(),
        },
    }
    out = out_dir / "v2251-probe-characterization.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {out}", flush=True)


if __name__ == "__main__":
    main()
