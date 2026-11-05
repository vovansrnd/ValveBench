#!/usr/bin/env python3
"""Offline re-analysis of an emu_loopback_steady.py float32 WAV capture.

Removes the fitted 997 Hz fundamental and harmonics 2..20 independently in
每 100 ms window before measuring 20 Hz..20 kHz residual power. This prevents
phase-dependent leakage from the strong 997 Hz sine from being misclassified
as bursts. It does not modify the source WAV.

Usage:
  python3 analyze_emu_loopback_steady.py path/to/emu-capture-float32.wav
"""
from __future__ import annotations
import argparse
import csv
import math
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal.windows import blackmanharris

FS_EXPECTED = 192000
F0 = 997.0
LEAD = 1.0
WINDOW = 0.100
HOP = 0.050
MAX_HARMONIC = 20
NFFT = 32768

SEGMENTS = [
    ("idle_pre", 0.0, 3.0),
    ("L_fade_in", 3.0, 4.0),
    ("L_steady", 4.0, 44.0),
    ("L_fade_out", 44.0, 45.0),
    ("idle_gap", 45.0, 50.0),
    ("R_fade_in", 50.0, 51.0),
    ("R_steady", 51.0, 91.0),
    ("R_fade_out", 91.0, 92.0),
    ("idle_post", 92.0, 95.0),
]


def dbfs_from_power(power: float) -> float:
    # Sine-reference dBFS: 1.0 peak sine is 0 dBFS.
    return 10.0 * math.log10(max(2.0 * float(power), 1e-300))


def state_for_window(playback_start: float, playback_end: float) -> str:
    # A frame is assigned to a state only when the WHOLE window lies inside it.
    # This keeps the last half-window of a fade from entering steady statistics.
    for name, start, end in SEGMENTS:
        if playback_start >= start and playback_end <= end:
            return name
    return "pre_capture" if playback_end <= 0 else "post_playback" if playback_start >= 95 else "transition"


def build_harmonic_projector(fs: int, n: int, f0: float, max_harmonic: int):
    t = np.arange(n, dtype=np.float64) / fs
    cols = [np.ones(n, dtype=np.float64)]
    for h in range(1, max_harmonic + 1):
        phase = 2.0 * np.pi * (h * f0) * t
        cols.extend((np.sin(phase), np.cos(phase)))
    design = np.column_stack(cols)
    # QR produces an orthonormal basis for the fitted DC + harmonic subspace.
    q, _ = np.linalg.qr(design, mode="reduced")
    return q


def frame_power_dbfs(frame: np.ndarray, window: np.ndarray, nfft: int,
                     freqs: np.ndarray, fs: int, band: tuple[float, float],
                     projector: np.ndarray | None = None) -> float:
    y = np.asarray(frame, dtype=np.float64)
    if projector is not None:
        # Remove only deterministic DC/fundamental/harmonics; sidebands and
        # non-harmonic noise remain in the residual.
        y = y - projector @ (projector.T @ y)
    else:
        y = y - np.mean(y)
    spec = np.fft.rfft(y * window, n=nfft)
    psd = (np.abs(spec) ** 2) / (fs * np.sum(window ** 2))
    if nfft > 1:
        psd[1:-1] *= 2.0
    df = fs / nfft
    lo, hi = band
    mask = (freqs >= lo) & (freqs < hi)
    return dbfs_from_power(float(np.sum(psd[mask]) * df))


def tone_level(frame: np.ndarray, fs: int, f0: float) -> float:
    y = np.asarray(frame, dtype=np.float64)
    y = y - np.mean(y)
    t = np.arange(len(y), dtype=np.float64) / fs
    s = np.sin(2 * np.pi * f0 * t)
    c = np.cos(2 * np.pi * f0 * t)
    a = 2.0 * float(np.dot(y, s)) / len(y)
    b = 2.0 * float(np.dot(y, c)) / len(y)
    peak = math.hypot(a, b)
    return 20.0 * math.log10(max(peak, 1e-30))


def stats(values: list[float], baseline: float):
    a = np.asarray(values, dtype=np.float64)
    return {
        "n": int(a.size),
        "median": float(np.median(a)),
        "p95": float(np.percentile(a, 95)),
        "max": float(np.max(a)),
        "min": float(np.min(a)),
        "elevated": int(np.sum(a > baseline + 6.0)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("wav", type=Path, help="float32 WAV created by emu_loopback_steady.py")
    ap.add_argument("--f0", type=float, default=F0)
    ap.add_argument("--lead", type=float, default=LEAD,
                    help="seconds between start of ADC file and start of playback (default: 1.0)")
    ap.add_argument("--window-ms", type=float, default=100.0)
    ap.add_argument("--hop-ms", type=float, default=50.0)
    ap.add_argument("--harmonics", type=int, default=MAX_HARMONIC)
    args = ap.parse_args()

    if not args.wav.is_file():
        raise SystemExit(f"File not found: {args.wav}")
    fs, x = wavfile.read(args.wav, mmap=True)
    if x.ndim != 2 or x.shape[1] != 2:
        raise SystemExit(f"Expected stereo WAV, got shape={x.shape}")
    if fs != FS_EXPECTED:
        raise SystemExit(f"Expected {FS_EXPECTED} Hz, got {fs} Hz")
    if x.dtype != np.float32 and x.dtype != np.float64:
        raise SystemExit(f"Expected float WAV, got {x.dtype}")

    n = int(round(args.window_ms * fs / 1000.0))
    hop = int(round(args.hop_ms * fs / 1000.0))
    if n <= 0 or hop <= 0 or n > x.shape[0]:
        raise SystemExit("Invalid window/hop or recording is too short")
    q = build_harmonic_projector(fs, n, args.f0, args.harmonics)
    win = blackmanharris(n, sym=False)
    freqs = np.fft.rfftfreq(NFFT, 1.0 / fs)
    rows = []

    for start in range(0, x.shape[0] - n + 1, hop):
        capture_start = start / fs
        capture_end = (start + n) / fs
        playback_start = capture_start - args.lead
        playback_end = capture_end - args.lead
        playback_centre = (playback_start + playback_end) / 2
        state = state_for_window(playback_start, playback_end)
        row = {
            "capture_time_s": (capture_start + capture_end) / 2,
            "playback_time_s": playback_centre,
            "state": state,
        }
        for ch, name in enumerate(("L", "R")):
            frame = np.asarray(x[start:start+n, ch], dtype=np.float64)
            row[f"{name}_20-20k_residual_dbfs"] = frame_power_dbfs(
                frame, win, NFFT, freqs, fs, (20.0, 20000.0), projector=q)
            row[f"{name}_40-96k_raw_dbfs"] = frame_power_dbfs(
                frame, win, NFFT, freqs, fs, (40000.0, 96000.0), projector=None)
        rows.append(row)

    idle_states = {"idle_pre", "idle_gap", "idle_post"}
    steady_states = {"L_steady", "R_steady"}
    baselines = {}
    for ch in ("L", "R"):
        k = f"{ch}_20-20k_residual_dbfs"
        vals = [r[k] for r in rows if r["state"] in idle_states]
        if not vals:
            raise SystemExit("No idle windows found; check --lead and recording duration")
        baselines[ch] = float(np.median(vals))

    lines = [
        "E-MU 0404 · CORRECTED STEADY-STATE RESIDUAL ANALYSIS",
        f"File={args.wav}",
        f"Fs={fs} Hz · f0={args.f0:g} Hz · window={args.window_ms:g} ms · hop={args.hop_ms:g} ms",
        f"Removed per window: DC + harmonics 1..{args.harmonics} by least-squares projection before FFT.",
        "20-20k values are non-harmonic residuals; 40-96k values are raw-band levels for reference.",
        "Elevated windows = residual > same-channel idle median +6 dB.",
        "",
        f"IDLE BASELINE: L={baselines['L']:.2f} dBFS · R={baselines['R']:.2f} dBFS",
        "STATE       ADC   20-20k median / p95 / max dBFS       elevated     40-96k median / p95 / max dBFS",
    ]
    for state in ("L_steady", "R_steady"):
        for ch in ("L", "R"):
            k = f"{ch}_20-20k_residual_dbfs"
            vals = [r[k] for r in rows if r["state"] == state]
            st = stats(vals, baselines[ch])
            ku = f"{ch}_40-96k_raw_dbfs"
            uv = [r[ku] for r in rows if r["state"] == state]
            ust = stats(uv, float(np.median([r[ku] for r in rows if r["state"] in idle_states])))
            lines.append(
                f"{state:10s} {ch}   {st['median']:7.2f} / {st['p95']:7.2f} / {st['max']:7.2f}"
                f"       {st['elevated']:3d}/{st['n']:<3d}"
                f"       {ust['median']:7.2f} / {ust['p95']:7.2f} / {ust['max']:7.2f}"
            )

    lines.append("")
    lines.append("TOP CORRECTED 20-20k WINDOWS:")
    for ch in ("L", "R"):
        k = f"{ch}_20-20k_residual_dbfs"
        subset = [r for r in rows if r["state"] in steady_states]
        for r in sorted(subset, key=lambda z: z[k], reverse=True)[:12]:
            lines.append(
                f"  ADC {ch} t={r['playback_time_s']:6.2f}s state={r['state']:8s}"
                f" residual={r[k]:7.2f} dBFS ({r[k]-baselines[ch]:+.2f} dB vs idle)"
            )

    out_txt = args.wav.with_name(args.wav.stem + "-corrected.txt")
    out_csv = args.wav.with_name(args.wav.stem + "-corrected.csv")
    out_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print("\n".join(lines))
    print(f"\nSaved: {out_txt}")
    print(f"Saved: {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
