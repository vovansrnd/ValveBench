#!/usr/bin/env python3
"""v2.25.4 — sub-band distribution of the 968–1032 Hz L/R PSD asymmetry.

Reads only the original 10-second v2.25.0 probe WAV. No audio-device access,
recording, playback, generated signal, production imports or source mutation.
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
from scipy.signal import periodogram

FS_EXPECTED = 96000
EXPECTED_SOURCE_SHA256 = "d1676fe55e822e703d89c34b1ee2f2fbb96143eb163540e18078081756282ca9"
BAND_LO_HZ = 968.0
BAND_HI_HZ = 1032.0
SUBBAND_WIDTH_HZ = 8.0
WINDOW_S = 2.0
HOP_S = 1.0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def db10(value: float) -> float | None:
    if not math.isfinite(float(value)) or float(value) <= 0.0:
        return None
    return float(10.0 * math.log10(float(value)))


def decode_wav(raw: np.ndarray) -> tuple[np.ndarray, str]:
    if np.issubdtype(raw.dtype, np.signedinteger):
        bits = np.iinfo(raw.dtype).bits
        return raw.astype(np.float64) / float(2 ** (bits - 1)), f"signed_int_{bits}"
    if np.issubdtype(raw.dtype, np.unsignedinteger):
        info = np.iinfo(raw.dtype)
        midpoint = float((info.max + 1) / 2)
        return (raw.astype(np.float64) - midpoint) / midpoint, f"unsigned_int_{info.bits}"
    if np.issubdtype(raw.dtype, np.floating):
        return raw.astype(np.float64), f"float_{raw.dtype.itemsize * 8}"
    raise ValueError(f"unsupported WAV dtype: {raw.dtype}")


def make_subbands() -> list[tuple[float, float]]:
    count = int(round((BAND_HI_HZ - BAND_LO_HZ) / SUBBAND_WIDTH_HZ))
    return [
        (BAND_LO_HZ + i * SUBBAND_WIDTH_HZ,
         BAND_LO_HZ + (i + 1) * SUBBAND_WIDTH_HZ)
        for i in range(count)
    ]


def band_stats(freq: np.ndarray, psd: np.ndarray,
               lo_hz: float, hi_hz: float) -> dict:
    f = np.asarray(freq, dtype=np.float64)
    p = np.asarray(psd, dtype=np.float64)
    # Half-open intervals prevent adjacent sub-bands double-counting an edge bin.
    keep = (f >= lo_hz) & (f < hi_hz) & np.isfinite(p) & (p > 0.0)
    fb, pb = f[keep], p[keep]
    if pb.size < 8:
        raise ValueError(
            f"insufficient PSD bins for {lo_hz:g}–{hi_hz:g} Hz: {pb.size}"
        )

    q10, q25, q50, q75, q90 = np.percentile(pb, [10, 25, 50, 75, 90])
    mean_value = float(np.mean(pb))
    max_index = int(np.argmax(pb))
    # Treat each periodogram sample as one frequency-bin density. The selected
    # half-open grid has a uniform spacing; sum*df preserves the full bin width.
    frequency_step = float(np.median(np.diff(fb)))
    integrated_power = float(np.sum(pb) * frequency_step)
    median_db = db10(q50)
    db_bins = 10.0 * np.log10(pb)
    return {
        "bin_count": int(pb.size),
        "frequency_step_hz": float(np.median(np.diff(fb))),
        "median_psd_dbfs2_per_hz": db10(q50),
        "p10_psd_dbfs2_per_hz": db10(q10),
        "p25_psd_dbfs2_per_hz": db10(q25),
        "p75_psd_dbfs2_per_hz": db10(q75),
        "p90_psd_dbfs2_per_hz": db10(q90),
        "mean_psd_dbfs2_per_hz": db10(mean_value),
        "max_psd_dbfs2_per_hz": db10(pb[max_index]),
        "max_frequency_hz": float(fb[max_index]),
        "mean_to_median_db": (
            float(10.0 * math.log10(mean_value / float(q50)))
            if q50 > 0.0 else None
        ),
        "bins_ge_median_plus_6db": int(
            np.count_nonzero(db_bins >= float(median_db) + 6.0)
        ),
        "bins_ge_median_plus_10db": int(
            np.count_nonzero(db_bins >= float(median_db) + 10.0)
        ),
        "band_integrated_power_fs2": integrated_power,
        "band_integrated_rms_dbfs": db10(integrated_power),
    }


def analyze_pair(freq: np.ndarray, psd_l: np.ndarray, psd_r: np.ndarray,
                 subbands: list[tuple[float, float]]) -> dict:
    full_l = band_stats(freq, psd_l, BAND_LO_HZ, BAND_HI_HZ)
    full_r = band_stats(freq, psd_r, BAND_LO_HZ, BAND_HI_HZ)
    rows = []
    for index, (lo, hi) in enumerate(subbands):
        left = band_stats(freq, psd_l, lo, hi)
        right = band_stats(freq, psd_r, lo, hi)
        rows.append({
            "band_hz": [lo, hi],
            "L": left,
            "R": right,
            "L_minus_R_median_psd_db": float(
                left["median_psd_dbfs2_per_hz"] - right["median_psd_dbfs2_per_hz"]
            ),
            "L_minus_R_p25_psd_db": float(
                left["p25_psd_dbfs2_per_hz"] - right["p25_psd_dbfs2_per_hz"]
            ),
            "L_minus_R_p75_psd_db": float(
                left["p75_psd_dbfs2_per_hz"] - right["p75_psd_dbfs2_per_hz"]
            ),
        })
    return {
        "full_band": {
            "L": full_l,
            "R": full_r,
            "L_minus_R_median_psd_db": float(
                full_l["median_psd_dbfs2_per_hz"] - full_r["median_psd_dbfs2_per_hz"]
            ),
        },
        "subbands": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", required=True, help="existing probe WAV; never captures")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    wav_path = Path(args.wav).expanduser().resolve()
    out_dir = Path(args.output).expanduser().resolve()
    if not wav_path.is_file():
        raise FileNotFoundError(f"original probe missing: {wav_path}; do not recapture")

    source_sha = sha256_file(wav_path)
    if source_sha != EXPECTED_SOURCE_SHA256:
        raise ValueError(
            f"source SHA mismatch: expected {EXPECTED_SOURCE_SHA256}, got {source_sha}; "
            "do not substitute or recapture"
        )

    fs, raw = wavfile.read(str(wav_path))
    if int(fs) != FS_EXPECTED or raw.ndim != 2 or raw.shape[1] != 2:
        raise ValueError(f"expected 96 kHz stereo WAV; got fs={fs}, shape={raw.shape}")
    x, dtype_name = decode_wav(raw)
    if not np.all(np.isfinite(x)) or len(x) != 10 * fs:
        raise ValueError(f"expected exactly 10 seconds of finite audio; got {len(x)/fs:g}s")
    clip_counts = np.count_nonzero(np.abs(x) >= 0.999, axis=0)
    centered = x - np.mean(x, axis=0, keepdims=True)
    subbands = make_subbands()

    full_spectra = []
    for channel in range(2):
        f, p = periodogram(
            centered[:, channel], fs=fs, window="hann",
            detrend="constant", scaling="density", nfft=len(centered),
        )
        full_spectra.append((f, p))
    if not np.array_equal(full_spectra[0][0], full_spectra[1][0]):
        raise RuntimeError("full-record L/R frequency grids differ")

    full_record = analyze_pair(
        full_spectra[0][0], full_spectra[0][1], full_spectra[1][1], subbands
    )

    win_n = int(WINDOW_S * fs)
    hop_n = int(HOP_S * fs)
    windows = []
    for start in range(0, len(centered) - win_n + 1, hop_n):
        stop = start + win_n
        spectra = []
        for channel in range(2):
            fw, pw = periodogram(
                centered[start:stop, channel], fs=fs, window="hann",
                detrend="constant", scaling="density", nfft=win_n,
            )
            spectra.append((fw, pw))
        if not np.array_equal(spectra[0][0], spectra[1][0]):
            raise RuntimeError("windowed L/R frequency grids differ")
        windows.append({
            "start_s": start / fs,
            "end_s": stop / fs,
            "overlaps_previous": bool(start > 0),
            "analysis": analyze_pair(
                spectra[0][0], spectra[0][1], spectra[1][1], subbands
            ),
        })

    result = {
        "task": "v2254 frequency sub-band distribution of pilot-band L/R asymmetry",
        "source": {
            "path": str(wav_path),
            "bytes": int(wav_path.stat().st_size),
            "sha256": source_sha,
            "sample_rate_hz": int(fs),
            "channels": 2,
            "frames": int(len(raw)),
            "duration_s": float(len(raw) / fs),
            "sample_dtype": dtype_name,
            "clip_samples_L": int(clip_counts[0]),
            "clip_samples_R": int(clip_counts[1]),
        },
        "analysis": {
            "band_hz": [BAND_LO_HZ, BAND_HI_HZ],
            "subband_width_hz": SUBBAND_WIDTH_HZ,
            "subband_count": len(subbands),
            "band_interval_convention": "half-open [low_hz, high_hz)",
            "integrated_power_method": "sum(PSD bins) * uniform frequency step",
            "full_record_periodogram_frequency_step_hz": float(
                np.median(np.diff(full_spectra[0][0]))
            ),
            "window_duration_s": WINDOW_S,
            "window_hop_s": HOP_S,
            "window_periodogram_frequency_step_hz": float(fs / win_n),
            "window_count": len(windows),
            "overlapping_windows_are_independent": False,
        },
        "full_record": full_record,
        "windows": windows,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "platform": platform.platform(),
        },
        "interpretation_limit": (
            "One existing 10-second capture with an unknown physical input path. "
            "Sub-band PSD differences characterize this recording only; they do "
            "not identify the source or justify hardware or production changes."
        ),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "v2254-pilotband-subband-distribution.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "source_sha256": source_sha,
        "window_count": len(windows),
        "full_record": full_record,
        "json_path": str(out),
    }, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
