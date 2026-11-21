#!/usr/bin/env python3
"""v2.25.2 — raw-domain transient analysis of the existing 10 s probe.

Strictly offline. Reads only the existing WAV; no ALSA, recording, playback,
device access, production imports, or source mutation. Tests whether the
reported ~15.4 Hz feature is a coherent sinusoid or merely part of a short
broadband transient plus filtering/window-edge behaviour.
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
from scipy.signal import periodogram, welch

FS_EXPECTED = 96000
LF_BAND = (8.0, 20.0)
NARROW_BAND = (14.0, 17.0)
PILOT_BAND = (968.0, 1032.0)
REFERENCE_HZ = 15.380859375
OLD_APPROX_HZ = 12.0
BLOCK_S = 0.5
SLIDE_S = 0.5
SLIDE_HOP_S = 0.125


def sha256_file(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def db20(v):
    v = float(v)
    return 20.0 * math.log10(v) if math.isfinite(v) and v > 0 else None


def db10(v):
    v = float(v)
    return 10.0 * math.log10(v) if math.isfinite(v) and v > 0 else None


def rms_metrics(x):
    x = np.asarray(x, dtype=np.float64)
    rms = float(np.sqrt(np.mean(x * x)))
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    return {
        "rms_fs": rms,
        "rms_dbfs": db20(rms),
        "sine_equivalent_dbfs": db20(rms * math.sqrt(2.0)),
        "peak_fs": peak,
        "peak_dbfs": db20(peak),
    }


def decode(raw):
    if np.issubdtype(raw.dtype, np.signedinteger):
        bits = np.iinfo(raw.dtype).bits
        return raw.astype(np.float64) / float(2 ** (bits - 1)), f"signed_int_{bits}"
    if np.issubdtype(raw.dtype, np.unsignedinteger):
        info = np.iinfo(raw.dtype)
        mid = float((info.max + 1) / 2)
        return (raw.astype(np.float64) - mid) / mid, f"unsigned_int_{info.bits}"
    if np.issubdtype(raw.dtype, np.floating):
        return raw.astype(np.float64), f"float_{raw.dtype.itemsize * 8}"
    raise ValueError(f"unsupported WAV dtype {raw.dtype}")


def periodogram_metrics(x, fs, lo, hi, nfft_multiplier=8):
    x = np.asarray(x, dtype=np.float64)
    nfft = 1 << int(math.ceil(math.log2(len(x)) + math.log2(nfft_multiplier)))
    freq, psd = periodogram(
        x, fs=fs, window="hann", nfft=nfft,
        detrend="linear", scaling="density",
    )
    df = float(freq[1] - freq[0])
    mask = (freq >= lo) & (freq <= hi)
    if not np.any(mask):
        raise RuntimeError(f"no bins in {lo}..{hi} Hz")
    idxs = np.flatnonzero(mask)
    idx = int(idxs[np.argmax(psd[mask])])
    power = float(np.sum(psd[mask]) * df)
    peak = {
        "frequency_hz": float(freq[idx]),
        "psd_fs2_per_hz": float(psd[idx]),
        "psd_dbfs2_per_hz": db10(psd[idx]),
    }
    return {
        "nfft": int(nfft),
        "bin_spacing_hz": df,
        "band_rms_fs": math.sqrt(max(power, 0.0)),
        "band_rms_dbfs": db20(math.sqrt(max(power, 0.0))),
        "band_sine_equivalent_dbfs": db20(math.sqrt(max(power, 0.0)) * math.sqrt(2.0)),
        "peak": peak,
    }


def fit_tone_with_trend(x, fs, start_frame, frequency):
    """Fit sine/cosine + DC + linear trend using absolute-time phase."""
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    t = (start_frame + np.arange(n, dtype=np.float64)) / fs
    tc = t - float(np.mean(t))
    angle = 2.0 * np.pi * frequency * t
    s = np.sin(angle)
    c = np.cos(angle)
    baseline = np.column_stack((np.ones(n), tc))
    design = np.column_stack((s, c, np.ones(n), tc))
    base_coef, *_ = np.linalg.lstsq(baseline, x, rcond=None)
    tone_coef, *_ = np.linalg.lstsq(design, x, rcond=None)
    base_resid = x - baseline @ base_coef
    tone_resid = x - design @ tone_coef
    base_ss = float(np.dot(base_resid, base_resid))
    tone_ss = float(np.dot(tone_resid, tone_resid))
    amplitude = float(math.hypot(tone_coef[0], tone_coef[1]))
    reduction = (base_ss - tone_ss) / base_ss if base_ss > 0 else None
    return {
        "frequency_hz_fixed": float(frequency),
        "peak_amplitude_fs": amplitude,
        "peak_amplitude_dbfs": db20(amplitude),
        "phase_rad_absolute_time": float(math.atan2(tone_coef[1], tone_coef[0])),
        "baseline_linear_rms_fs": float(np.sqrt(np.mean(base_resid * base_resid))),
        "tone_model_residual_rms_fs": float(np.sqrt(np.mean(tone_resid * tone_resid))),
        "fraction_of_linear_detrended_energy_explained": float(reduction) if reduction is not None else None,
    }


def band_noise_psd(x, fs):
    nperseg = min(65536, len(x))
    freq, psd = welch(
        x, fs=fs, window="hann", nperseg=nperseg,
        noverlap=nperseg // 2, detrend="constant", scaling="density",
    )
    mask = (freq >= PILOT_BAND[0]) & (freq <= PILOT_BAND[1])
    v = psd[mask]
    return {
        "bins": int(v.size),
        "median_fs2_per_hz": float(np.median(v)),
        "median_dbfs2_per_hz": db10(np.median(v)),
        "mean_fs2_per_hz": float(np.mean(v)),
        "mean_dbfs2_per_hz": db10(np.mean(v)),
        "max_dbfs2_per_hz": db10(np.max(v)),
    }


def describe(values):
    a = np.asarray(values, dtype=float)
    return {
        "min": float(np.min(a)),
        "median": float(np.median(a)),
        "p90": float(np.percentile(a, 90)),
        "max": float(np.max(a)),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--wav", required=True, help="existing probe WAV; no recording is performed")
    p.add_argument("--output", required=True)
    args = p.parse_args()
    wav = Path(args.wav).expanduser().resolve()
    outdir = Path(args.output).expanduser().resolve()
    if not wav.is_file():
        raise FileNotFoundError(f"existing probe absent: {wav}; do not recreate or recapture")

    fs, raw = wavfile.read(str(wav))
    if int(fs) != FS_EXPECTED or raw.ndim != 2 or raw.shape[1] != 2:
        raise ValueError(f"expected 96 kHz stereo WAV, got fs={fs}, shape={raw.shape}")
    x, dtype_name = decode(raw)
    if len(x) < 9 * fs or not np.all(np.isfinite(x)):
        raise ValueError("source too short or contains non-finite samples")

    outdir.mkdir(parents=True, exist_ok=True)
    frames = int(len(x))
    duration = frames / fs
    sha = sha256_file(wav)
    clip = np.count_nonzero(np.abs(x) >= 0.999, axis=0)
    whole = x - np.mean(x, axis=0, keepdims=True)

    # Entire-record raw spectra, without any band-pass filtering.
    global_spectra = []
    for ch in range(2):
        global_spectra.append(periodogram_metrics(whole[:, ch], fs, *LF_BAND, 16))

    # Non-overlapping 0.5 s blocks: raw band power + coherent matched-tone fit.
    block_rows = []
    block_count = frames // int(BLOCK_S * fs)
    for bi in range(block_count):
        start = int(round(bi * BLOCK_S * fs))
        stop = int(round((bi + 1) * BLOCK_S * fs))
        row = {"block": bi, "start_s": bi * BLOCK_S, "end_s": (bi + 1) * BLOCK_S}
        for ch, label in enumerate(("L", "R")):
            seg = whole[start:stop, ch]
            row[f"fullband_{label}"] = rms_metrics(seg)
            row[f"raw_8_20hz_{label}"] = periodogram_metrics(seg, fs, *LF_BAND, 8)
            row[f"raw_14_17hz_{label}"] = periodogram_metrics(seg, fs, *NARROW_BAND, 8)
            row[f"fixed_15p3809hz_fit_{label}"] = fit_tone_with_trend(
                seg, fs, start, REFERENCE_HZ
            )
            row[f"fixed_12hz_fit_{label}"] = fit_tone_with_trend(
                seg, fs, start, OLD_APPROX_HZ
            )
        block_rows.append(row)

    # Overlapping raw-domain 0.5 s windows at 125 ms hop, first 4 seconds
    # densely sampled and remainder at the same hop for direct comparison.
    slide_rows = []
    win_n = int(SLIDE_S * fs)
    hop_n = int(SLIDE_HOP_S * fs)
    for start in range(0, frames - win_n + 1, hop_n):
        stop = start + win_n
        row = {"start_s": start / fs, "end_s": stop / fs}
        for ch, label in enumerate(("L", "R")):
            seg = whole[start:stop, ch]
            row[f"raw_8_20hz_{label}"] = periodogram_metrics(seg, fs, *LF_BAND, 8)
            row[f"fixed_15p3809hz_fit_{label}"] = fit_tone_with_trend(
                seg, fs, start, REFERENCE_HZ
            )
        slide_rows.append(row)

    # Raw, unfiltered, overlapping two-second windows at one-second hop.
    # The peak is a descriptive periodogram maximum, not a sub-bin precision claim.
    two_sec_rows = []
    win2 = int(2.0 * fs)
    hop2 = int(1.0 * fs)
    for start in range(0, frames - win2 + 1, hop2):
        stop = start + win2
        row = {"start_s": start / fs, "end_s": stop / fs}
        for ch, label in enumerate(("L", "R")):
            seg = whole[start:stop, ch]
            row[f"raw_8_20hz_{label}"] = periodogram_metrics(seg, fs, *LF_BAND, 16)
            row[f"fixed_15p3809hz_fit_{label}"] = fit_tone_with_trend(
                seg, fs, start, REFERENCE_HZ
            )
        two_sec_rows.append(row)

    pilot_psd = {
        label: band_noise_psd(whole[:, ch], fs)
        for ch, label in enumerate(("L", "R"))
    }

    # Summary ranges for key raw metrics.
    summary = {
        "task": "v2252 raw-domain transient vs coherent tone analysis",
        "source_wav_path": str(wav),
        "source_bytes": int(wav.stat().st_size),
        "source_sha256": sha,
        "sample_rate_hz": int(fs),
        "frames": frames,
        "duration_s": duration,
        "sample_dtype": dtype_name,
        "clip_samples_L": int(clip[0]),
        "clip_samples_R": int(clip[1]),
        "fullband_L": rms_metrics(whole[:, 0]),
        "fullband_R": rms_metrics(whole[:, 1]),
        "global_raw_8_20hz_periodogram_L": global_spectra[0],
        "global_raw_8_20hz_periodogram_R": global_spectra[1],
        "raw_1khz_adjacent_noise_psd": pilot_psd,
        "block_count": block_count,
        "block_duration_s": BLOCK_S,
        "sliding_window_count": len(slide_rows),
        "sliding_window_duration_s": SLIDE_S,
        "sliding_window_hop_s": SLIDE_HOP_S,
        "two_second_window_count": len(two_sec_rows),
        "reference_frequency_hz": REFERENCE_HZ,
        "interpretation_limit": (
            "No physical source identification. Fixed-frequency fits quantify "
            "projection onto a sine but do not prove a sinusoidal source. Raw "
            "window spectra have finite resolution and should be interpreted "
            "together with fit-energy reduction and phase continuity."
        ),
    }
    out = {
        "summary": summary,
        "nonoverlap_500ms_blocks": block_rows,
        "overlap_500ms_windows_125ms_hop": slide_rows,
        "raw_2s_windows_1s_hop": two_sec_rows,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "platform": platform.platform(),
        },
    }
    out_path = outdir / "v2252-raw-probe-transient-analysis.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {out_path}", flush=True)


if __name__ == "__main__":
    main()
