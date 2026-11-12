#!/usr/bin/env python3
"""Print a compact, upload-free summary of the D10→E-MU diagnostic ZIP.

Usage:
  /usr/bin/python3 verification/summarize_thd_archive.py \
      measurements/diagnostics/d10-emu-thd-full-capture.zip

Only numeric summaries and metadata are printed; no WAV data is uploaded.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import sys
import zipfile
from pathlib import Path

import numpy as np
from scipy.io import wavfile


def dbfs(value: float) -> float:
    return 20.0 * math.log10(max(float(value), 1e-15))


def estimate_frequency(x: np.ndarray, fs: int, nominal: float) -> float:
    """Parabolic FFT peak estimate from the loudest 1 s region."""
    mono = np.asarray(x[:, 0], dtype=np.float64)
    win_n = min(len(mono), int(fs))
    if win_n < fs // 2:
        return nominal
    hop = max(1, win_n // 4)
    starts = range(0, max(1, len(mono) - win_n + 1), hop)
    best = None
    hann = np.hanning(win_n)
    lo = max(1, int((nominal - 5.0) * win_n / fs))
    hi = min(win_n // 2, int((nominal + 5.0) * win_n / fs) + 2)
    for start in starts:
        block = mono[start:start + win_n]
        if len(block) != win_n:
            continue
        spec = np.abs(np.fft.rfft((block - np.mean(block)) * hann))
        if hi <= lo:
            continue
        k = lo + int(np.argmax(spec[lo:hi]))
        score = float(spec[k])
        if best is None or score > best[0]:
            delta = 0.0
            if 1 <= k < len(spec) - 1:
                a, b, c = np.log(np.maximum(spec[k - 1:k + 2], 1e-30))
                den = a - 2.0 * b + c
                if abs(den) > 1e-15:
                    delta = float(np.clip(0.5 * (a - c) / den, -0.5, 0.5))
            best = (score, (k + delta) * fs / win_n)
    return float(best[1]) if best else nominal


def summarize(name: str, data: np.ndarray, fs: int, frequency: float, step_s: float) -> None:
    x = np.asarray(data, dtype=np.float64)
    if x.ndim == 1:
        x = np.column_stack((x, x))
    if x.ndim != 2 or x.shape[1] < 2:
        raise ValueError(f"{name}: expected stereo audio, got {x.shape}")
    step = max(1, int(round(step_s * fs)))
    print(f"\n[{name}] Fs={fs} Hz, frames={len(x)}, duration={len(x)/fs:.4f} s")
    print("time(s)     L total   L tone    L resid   | R total   R tone    R resid  (dBFS RMS)")
    for start in range(0, len(x), step):
        end = min(len(x), start + step)
        if end - start < int(0.05 * fs):
            continue
        t = np.arange(end - start, dtype=np.float64) / fs
        row = []
        for ch in (0, 1):
            y = x[start:end, ch]
            total = dbfs(float(np.sqrt(np.mean(y * y))))
            mat = np.column_stack((
                np.sin(2.0 * np.pi * frequency * t),
                np.cos(2.0 * np.pi * frequency * t),
                np.ones(len(t)),
            ))
            coef, *_ = np.linalg.lstsq(mat, y, rcond=None)
            fitted = mat @ coef
            tone_rms = dbfs(float(np.hypot(coef[0], coef[1]) / np.sqrt(2.0)))
            residual = dbfs(float(np.sqrt(np.mean((y - fitted) ** 2))))
            row.extend((total, tone_rms, residual))
        print(
            f"{start/fs:7.3f}-{end/fs:7.3f}  "
            f"{row[0]:8.1f} {row[1]:8.1f} {row[2]:9.1f} |"
            f" {row[3]:8.1f} {row[4]:8.1f} {row[5]:9.1f}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "archive",
        nargs="?",
        default="measurements/diagnostics/d10-emu-thd-full-capture.zip",
    )
    parser.add_argument("--step", type=float, default=0.2, help="summary window in seconds")
    args = parser.parse_args()

    path = Path(args.archive).expanduser()
    if not path.is_file():
        raise SystemExit(f"ZIP not found: {path}")
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        metadata_name = next((n for n in names if n.endswith("metadata.json")), None)
        if metadata_name is None:
            raise SystemExit("metadata.json is missing from archive")
        metadata = json.loads(archive.read(metadata_name).decode("utf-8"))

        fs = int(metadata["sample_rate_hz"])
        nominal = float(metadata.get("test_frequency_hz", 997.0))
        print("=== DIAGNOSTIC METADATA ===")
        for key in (
            "created", "branch", "dac", "adc", "sample_rate_hz",
            "test_frequency_hz", "generator_level_dbfs", "clock_level_dbfs",
            "blocks", "settle_seconds", "dac_preroll_seconds", "dac_postroll_seconds",
            "tone_postroll_seconds",
        ):
            print(f"{key}: {metadata.get(key)}")
        clock = metadata.get("clock_preflight", {})
        for ch in ("L", "R"):
            q = clock.get("ch", {}).get(ch, {})
            if q:
                print(
                    f"clock {ch}: f0={q.get('f0')} Hz, ppm={q.get('ppm')}, "
                    f"level={q.get('level_dbfs')} dBFS, SNR={q.get('snr_db')} dB, "
                    f"coherent={q.get('coherent')}"
                )
        for run in metadata.get("runs", []):
            print("run:", json.dumps(run, ensure_ascii=False, sort_keys=True))

        members = (
            "stimulus_thd.wav",
            "capture_full_thd.wav",
            "analysis_window.wav",
        )
        audio = {}
        for name in members:
            match = next((n for n in names if n.endswith(name)), None)
            if match is None:
                print(f"\nMISSING: {name}")
                continue
            rate, samples = wavfile.read(io.BytesIO(archive.read(match)))
            if int(rate) != fs:
                print(f"WARNING: {name}: WAV Fs={rate}, metadata Fs={fs}")
            audio[name] = np.asarray(samples)
        capture = audio.get("capture_full_thd.wav")
        if capture is not None:
            frequency = estimate_frequency(capture, fs, nominal)
        else:
            frequency = nominal
        print(f"\nEstimated captured tone frequency: {frequency:.6f} Hz")
        for name in members:
            if name in audio:
                summarize(name, audio[name], fs, frequency, args.step)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
