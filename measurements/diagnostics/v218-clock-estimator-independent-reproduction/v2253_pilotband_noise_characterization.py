#!/usr/bin/env python3
"""v2.25.3 — read-only time/spectral characterization of 968–1032 Hz noise.

Reads only the existing /tmp/v2250-probe.wav. No audio-device access,
recording, playback, generated signal, production imports or source mutation.
Purpose: distinguish broadband L/R noise-floor asymmetry from a few narrow
spectral components and determine whether either is time-localized.
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
from scipy.ndimage import median_filter
from scipy.signal import coherence, find_peaks, periodogram, welch

FS_EXPECTED = 96000
BAND_HZ = (968.0, 1032.0)
WINDOW_S = 2.0
HOP_S = 1.0
WELCH_NPERSEG = 65536
WELCH_OVERLAP = WELCH_NPERSEG // 2
PEAK_MIN_PROMINENCE_DB = 6.0
LOCAL_BASELINE_BINS = 101


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def db10(x):
    a = np.asarray(x, dtype=np.float64)
    out = np.full(a.shape, np.nan, dtype=np.float64)
    mask = np.isfinite(a) & (a > 0)
    out[mask] = 10.0 * np.log10(a[mask])
    return out


def describe(values):
    a = np.asarray(values, dtype=np.float64)
    a = a[np.isfinite(a)]
    if not a.size:
        return {"min": None, "p10": None, "median": None, "p90": None, "max": None}
    return {
        "min": float(np.min(a)),
        "p10": float(np.percentile(a, 10)),
        "median": float(np.percentile(a, 50)),
        "p90": float(np.percentile(a, 90)),
        "max": float(np.max(a)),
    }


def decode_wav(raw):
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


def level_dbfs(x):
    x = np.asarray(x, dtype=np.float64)
    rms = float(np.sqrt(np.mean(x * x)))
    return {
        "rms_fs": rms,
        "rms_dbfs": 20.0 * math.log10(rms) if rms > 0 else None,
        "sine_equivalent_dbfs": 20.0 * math.log10(rms * math.sqrt(2.0)) if rms > 0 else None,
    }


def band_summary(freq, psd, band=BAND_HZ):
    f = np.asarray(freq, dtype=np.float64)
    p = np.asarray(psd, dtype=np.float64)
    keep = (f >= band[0]) & (f <= band[1]) & np.isfinite(p) & (p > 0)
    if np.count_nonzero(keep) < 5:
        raise ValueError(f"insufficient PSD bins in {band}")
    fb, pb = f[keep], p[keep]
    db = db10(pb)
    i_max = int(np.argmax(pb))
    p90 = float(np.percentile(pb, 90))
    return {
        "bin_count": int(pb.size),
        "frequency_step_hz": float(np.median(np.diff(fb))),
        "median_psd_fs2_per_hz": float(np.median(pb)),
        "median_psd_dbfs2_per_hz": float(np.median(db)),
        "mean_psd_fs2_per_hz": float(np.mean(pb)),
        "mean_psd_dbfs2_per_hz": float(db10([np.mean(pb)])[0]),
        "p90_psd_dbfs2_per_hz": float(db10([p90])[0]),
        "max_psd_fs2_per_hz": float(pb[i_max]),
        "max_psd_dbfs2_per_hz": float(db[i_max]),
        "max_frequency_hz": float(fb[i_max]),
        "mean_to_median_db": float(10.0 * math.log10(np.mean(pb) / np.median(pb))),
        "p90_to_median_db": float(10.0 * math.log10(p90 / np.median(pb))),
        "band_integrated_power_fs2": float(np.trapezoid(pb, fb)),
        "band_integrated_rms_dbfs": (
            10.0 * math.log10(float(np.trapezoid(pb, fb)))
            if np.trapezoid(pb, fb) > 0 else None
        ),
        "bins_ge_median_plus_6db": int(np.count_nonzero(db >= np.median(db) + 6.0)),
        "bins_ge_median_plus_10db": int(np.count_nonzero(db >= np.median(db) + 10.0)),
    }


def prominent_peaks(freq, psd, band=BAND_HZ):
    f = np.asarray(freq, dtype=np.float64)
    p = np.asarray(psd, dtype=np.float64)
    mask = (f >= band[0]) & (f <= band[1]) & np.isfinite(p) & (p > 0)
    idx = np.flatnonzero(mask)
    if idx.size < LOCAL_BASELINE_BINS:
        baseline_size = max(5, int(idx.size // 3) * 2 + 1)
    else:
        baseline_size = LOCAL_BASELINE_BINS
    db = db10(p)
    baseline = median_filter(db, size=baseline_size, mode="nearest")
    residual = db - baseline
    local_residual = residual[mask]
    candidates, props = find_peaks(
        local_residual,
        prominence=PEAK_MIN_PROMINENCE_DB,
        distance=2,
    )
    peaks = []
    for j, local_idx in enumerate(candidates):
        global_idx = int(idx[local_idx])
        peaks.append({
            "frequency_hz": float(f[global_idx]),
            "psd_dbfs2_per_hz": float(db[global_idx]),
            "local_baseline_dbfs2_per_hz": float(baseline[global_idx]),
            "prominence_db": float(props["prominences"][j]),
        })
    peaks.sort(key=lambda row: row["prominence_db"], reverse=True)
    return peaks[:20]


def welch_psd(x, fs):
    nperseg = min(WELCH_NPERSEG, len(x))
    noverlap = min(WELCH_OVERLAP, nperseg // 2)
    return welch(
        np.asarray(x, dtype=np.float64),
        fs=fs,
        window="hann",
        nperseg=nperseg,
        noverlap=noverlap,
        detrend="constant",
        scaling="density",
        average="mean",
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", required=True, help="existing probe WAV; no capture is performed")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    wav_path = Path(args.wav).expanduser().resolve()
    out_dir = Path(args.output).expanduser().resolve()
    if not wav_path.is_file():
        raise FileNotFoundError(f"existing probe missing: {wav_path}; do not recapture")
    fs, raw = wavfile.read(str(wav_path))
    if int(fs) != FS_EXPECTED or raw.ndim != 2 or raw.shape[1] != 2:
        raise ValueError(f"expected 96 kHz stereo WAV; got fs={fs}, shape={raw.shape}")
    x, dtype_name = decode_wav(raw)
    if not np.all(np.isfinite(x)) or len(x) < 9 * fs:
        raise ValueError("invalid or too-short source WAV")

    out_dir.mkdir(parents=True, exist_ok=True)
    frames = int(len(x))
    duration = frames / float(fs)
    centered = x - np.mean(x, axis=0, keepdims=True)
    clip_counts = np.count_nonzero(np.abs(x) >= 0.999, axis=0)

    # Full-record Welch: stable floor estimate and band outlier metrics.
    full_psd = []
    full_summary = {}
    for ch, label in enumerate(("L", "R")):
        f, p = welch_psd(centered[:, ch], fs)
        full_psd.append((f, p))
        full_summary[label] = band_summary(f, p)
    common_f = full_psd[0][0]
    if not np.array_equal(common_f, full_psd[1][0]):
        raise RuntimeError("L/R Welch frequency grids differ")

    # Full-record Hann periodogram: high-resolution peak locations. Zero-padding
    # is not used; bin width is fs/N and true resolution is still window-limited.
    global_peaks = {}
    global_periodogram_stats = {}
    for ch, label in enumerate(("L", "R")):
        f_per, p_per = periodogram(
            centered[:, ch], fs=fs, window="hann", detrend="constant",
            scaling="density", nfft=frames,
        )
        global_periodogram_stats[label] = band_summary(f_per, p_per)
        global_peaks[label] = prominent_peaks(f_per, p_per)

    # Sliding 2-second windows at 1-second hop. These are overlapping,
    # descriptive snapshots, not independent observations.
    win_n = int(WINDOW_S * fs)
    hop_n = int(HOP_S * fs)
    windows = []
    for start in range(0, frames - win_n + 1, hop_n):
        stop = start + win_n
        win = centered[start:stop]
        row = {
            "start_s": start / fs,
            "end_s": stop / fs,
            "overlaps_previous": bool(start > 0),
            "L": {},
            "R": {},
        }
        window_psd = []
        for ch, label in enumerate(("L", "R")):
            fw, pw = welch_psd(win[:, ch], fs)
            window_psd.append((fw, pw))
            row[label] = band_summary(fw, pw)
            row[label]["prominent_peaks"] = prominent_peaks(fw, pw)
            row[label]["full_window_rms"] = level_dbfs(win[:, ch])
        windows.append(row)

    # L/R magnitude-squared coherence over the whole record; locate coherence
    # at each prominent full-record L/R peak frequency.
    f_coh, coh = coherence(
        centered[:, 0], centered[:, 1], fs=fs,
        window="hann", nperseg=WELCH_NPERSEG,
        noverlap=WELCH_OVERLAP, detrend="constant",
    )
    coherence_at_peaks = []
    candidate_freqs = sorted(set(
        [p["frequency_hz"] for label in ("L", "R") for p in global_peaks[label]]
    ))
    for frequency in candidate_freqs:
        i = int(np.argmin(np.abs(f_coh - frequency)))
        coherence_at_peaks.append({
            "requested_peak_frequency_hz": frequency,
            "coherence_bin_frequency_hz": float(f_coh[i]),
            "magnitude_squared_coherence": float(coh[i]),
        })

    # Summarize per-window asymmetry by ratio of robust median PSDs.
    per_window_asymmetry_db = []
    for row in windows:
        lval = row["L"]["median_psd_dbfs2_per_hz"]
        rval = row["R"]["median_psd_dbfs2_per_hz"]
        per_window_asymmetry_db.append({
            "start_s": row["start_s"],
            "end_s": row["end_s"],
            "L_minus_R_median_psd_db": float(lval - rval),
            "L_minus_R_mean_psd_db": float(
                row["L"]["mean_psd_dbfs2_per_hz"] -
                row["R"]["mean_psd_dbfs2_per_hz"]
            ),
        })

    summary = {
        "task": "v2253 time-resolved 968-1032 Hz noise-band characterization",
        "source_wav_path": str(wav_path),
        "source_bytes": int(wav_path.stat().st_size),
        "source_sha256": sha256_file(wav_path),
        "sample_rate_hz": int(fs),
        "frames": frames,
        "duration_s": duration,
        "sample_dtype": dtype_name,
        "clip_samples_L": int(clip_counts[0]),
        "clip_samples_R": int(clip_counts[1]),
        "fullband_L": level_dbfs(centered[:, 0]),
        "fullband_R": level_dbfs(centered[:, 1]),
        "band_hz": list(BAND_HZ),
        "full_record_welch_band_stats": full_summary,
        "full_record_hann_periodogram_band_stats": global_periodogram_stats,
        "full_record_prominent_periodogram_peaks": global_peaks,
        "full_record_coherence_at_prominent_peaks": coherence_at_peaks,
        "window_duration_s": WINDOW_S,
        "window_hop_s": HOP_S,
        "window_count": len(windows),
        "per_window_L_minus_R_asymmetry": per_window_asymmetry_db,
        "interpretation_limit": (
            "This is one 10-second unknown-path capture. It characterizes "
            "spectral/time properties only; it cannot identify the physical "
            "connection, establish causality, or certify the input path."
        ),
    }
    result = {
        "summary": summary,
        "windows": windows,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "platform": platform.platform(),
        },
    }
    out = out_dir / "v2253-pilotband-noise-characterization.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"JSON: {out}", flush=True)


if __name__ == "__main__":
    main()
