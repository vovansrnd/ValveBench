#!/usr/bin/env python3
"""Capture the complete ALSA recording and exact THD analysis window for diagnosis.

Run from the repository root:
  /usr/bin/python3 verification/diagnose_thd_full_capture.py

Defaults match the reported direct SMSL D10 -> E-MU 0404 loopback.
The -30 dBFS clock pilot is ONLY for that previously verified direct cable path.
Do not use this diagnostic through a phono stage or other DUT.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from valvebench import calib, measure as m
from valvebench.backend import AlsaCli
from valvebench.cli import split_clock_preflight, validate_pair_rate


def write_float_wav(path: Path, fs: int, samples: np.ndarray) -> None:
    data = np.asarray(samples, dtype=np.float32)
    if data.ndim == 1:
        data = np.column_stack((data, data))
    if data.ndim != 2 or data.shape[1] != 2:
        raise ValueError(f"expected stereo samples, got shape {data.shape}")
    wavfile.write(str(path), int(fs), data)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dac", default="hw:3,0")
    parser.add_argument("--adc", default="hw:2,0")
    parser.add_argument("--rate", type=int, default=192000)
    parser.add_argument("--frequency", type=float, default=997.0)
    parser.add_argument("--level", type=float, default=-30.0)
    parser.add_argument("--clock-level", type=float, default=-30.0)
    parser.add_argument("--blocks", type=int, default=2)
    parser.add_argument("--settle", type=float, default=0.5)
    parser.add_argument(
        "--dac-preroll",
        type=float,
        default=0.0,
        help="digital silence prepended inside the same aplay stream before the test programme",
    )
    parser.add_argument(
        "--dac-postroll",
        type=float,
        default=0.0,
        help="digital silence appended inside the same aplay stream after the test programme",
    )
    parser.add_argument(
        "--tone-postroll",
        type=float,
        default=0.0,
        help="continue the generated test tone after the nominal programme, before any DAC silence postroll",
    )
    parser.add_argument(
        "--out-dir",
        default="measurements/diagnostics/d10-emu-thd-full-capture",
        help="output directory relative to the project root (must not already exist)",
    )
    args = parser.parse_args()

    out_dir = (ROOT / args.out_dir).resolve()
    if out_dir.exists():
        raise SystemExit(
            f"Отказ от перезаписи существующей папки: {out_dir}\n"
            "Укажи другой --out-dir или перенеси старую папку."
        )
    out_dir.mkdir(parents=True, exist_ok=False)

    fs = int(args.rate)
    cfg = calib.load()
    validate_pair_rate(args.dac, args.adc, fs)
    backend = AlsaCli(
        hw_out=args.dac,
        hw_in=args.adc,
        in_map=cfg["in_map"],
        out_map=cfg["out_map"],
    )
    rig = m.Rig(
        backend,
        cfg,
        fs=fs,
        blocks=int(args.blocks),
        settle=float(args.settle),
    )

    state = {"label": None, "runs": []}
    original_play_record = backend.play_record
    original_onset = rig._onset

    def tapped_play_record(out, tail=0.8):
        sent = np.asarray(out, dtype=np.float64)
        source_frames = int(len(sent))
        tone_postroll_frames = 0
        if state["label"] == "thd":
            tone_postroll_frames = int(round(float(args.tone_postroll) * fs))
            if tone_postroll_frames < 0:
                raise ValueError("--tone-postroll must be non-negative")
            if tone_postroll_frames:
                # Rig._seg() ends with a 10 ms fade. Remove that fade, continue
                # the exact coherent sine phase, then put the fade at the new end.
                ramp_n = min(int(0.01 * fs), len(sent))
                k, amplitude = rig.amp_tone(args.frequency, args.level)
                start_index = len(sent) - ramp_n
                indices = np.arange(
                    start_index, start_index + tone_postroll_frames + ramp_n,
                    dtype=np.int64,
                )
                phase = 2.0 * np.pi * ((k * indices) % rig.n) / rig.n
                extension = np.zeros(
                    (len(indices), 2), dtype=np.float64
                )
                for channel in (0, 1):
                    if np.max(np.abs(sent[:-ramp_n, channel])) > 0.0:
                        extension[:, channel] = amplitude * np.sin(phase)
                fade = 0.5 - 0.5 * np.cos(
                    np.pi * np.arange(ramp_n, dtype=np.float64) / ramp_n
                )
                extension[-ramp_n:, :] *= fade[::-1, None]
                sent = np.vstack((sent[:-ramp_n], extension))

        preroll_frames = int(round(float(args.dac_preroll) * fs))
        if preroll_frames < 0:
            raise ValueError("--dac-preroll must be non-negative")
        postroll_frames = int(round(float(args.dac_postroll) * fs))
        if postroll_frames < 0:
            raise ValueError("--dac-postroll must be non-negative")
        preroll = np.zeros((preroll_frames, 2), dtype=np.float64)
        postroll = np.zeros((postroll_frames, 2), dtype=np.float64)
        playback = np.vstack((preroll, sent, postroll))
        captured = original_play_record(playback, tail=tail)
        item = {
            "label": state["label"],
            "dac_preroll_frames": preroll_frames,
            "dac_preroll_seconds": float(preroll_frames / fs),
            "dac_postroll_frames": postroll_frames,
            "dac_postroll_seconds": float(postroll_frames / fs),
            "source_frames_before_tone_postroll": source_frames,
            "tone_postroll_frames": tone_postroll_frames,
            "tone_postroll_seconds": float(tone_postroll_frames / fs),
            "stimulus_frames": int(len(sent)),
            "stimulus_seconds": float(len(sent) / fs),
            "playback_frames_including_preroll": int(len(playback)),
            "playback_seconds_including_preroll": float(len(playback) / fs),
            "capture_frames": int(len(captured)),
            "capture_seconds": float(len(captured) / fs),
            "backend_notes": list(getattr(backend, "notes", [])),
        }
        if state["label"] == "thd":
            write_float_wav(out_dir / "stimulus_thd.wav", fs, playback)
            write_float_wav(out_dir / "capture_full_thd.wav", fs, captured)
            item["stimulus_file"] = "stimulus_thd.wav"
            item["capture_file"] = "capture_full_thd.wav"
        state["runs"].append(item)
        return captured

    def tapped_onset(rec, tone_hz=None):
        detected = original_onset(rec, tone_hz=tone_hz)
        if state["runs"]:
            item = state["runs"][-1]
            settle_s = 0.4 if item["label"] == "clock-preflight" else float(args.settle)
            settle_frames = int(round(settle_s * fs))
            start = int(detected) + settle_frames
            item.update({
                "detected_tone_onset_frame": int(detected),
                "detected_tone_onset_seconds": float(detected / fs),
                "tone_hz_used_by_onset_detector": tone_hz,
                "settle_seconds": settle_s,
                "settle_frames": settle_frames,
                "analysis_window_start_frame": start,
                "analysis_window_end_frame": start + int(args.blocks) * rig.n,
            })
        return detected

    backend.play_record = tapped_play_record
    rig._onset = tapped_onset

    manifest = {
        "created": datetime.now().astimezone().isoformat(),
        "repository": "vovansrnd/ValveBench",
        "branch": "diagnostics/d10-full-capture",
        "dac": args.dac,
        "adc": args.adc,
        "sample_rate_hz": fs,
        "test_frequency_hz": float(args.frequency),
        "generator_level_dbfs": float(args.level),
        "clock_level_dbfs": float(args.clock_level),
        "blocks": int(args.blocks),
        "settle_seconds": float(args.settle),
        "dac_preroll_seconds": float(args.dac_preroll),
        "dac_postroll_seconds": float(args.dac_postroll),
        "tone_postroll_seconds": float(args.tone_postroll),
        "analysis_window_seconds": float(args.blocks),
        "runs": state["runs"],
    }

    try:
        rig.open()
        state["label"] = "clock-preflight"
        clock = split_clock_preflight(
            rig, args.dac, args.adc, level_dbfs=float(args.clock_level)
        )
        manifest["clock_preflight"] = clock
        if clock:
            print(m.text_clock(clock))

        state["label"] = "thd"
        result = m.thd_point(
            rig,
            f=float(args.frequency),
            level_dbfs=float(args.level),
            capture_wav=out_dir / "analysis_window.wav",
        )
        manifest["thd_report"] = m.text_thd(result)
        print(m.text_thd(result))
    except Exception as exc:
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        manifest["runs"] = state["runs"]
        (out_dir / "report.txt").write_text(
            manifest.get("thd_report", manifest.get("error", "No THD report")) + "\n",
            encoding="utf-8",
        )
        (out_dir / "metadata.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=True) + "\n",
            encoding="utf-8",
        )

        files = sorted(p for p in out_dir.iterdir() if p.is_file())
        manifest["files"] = {
            p.name: {"bytes": p.stat().st_size, "sha256": sha256(p)}
            for p in files
        }
        (out_dir / "metadata.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=True) + "\n",
            encoding="utf-8",
        )

        archive = out_dir.with_suffix(".zip")
        with zipfile.ZipFile(
            archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zf:
            for path in sorted(p for p in out_dir.iterdir() if p.is_file()):
                zf.write(path, arcname=path.name)
        print(f"\nПолный диагностический архив: {archive}")
        print(f"Папка с исходными файлами: {out_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
