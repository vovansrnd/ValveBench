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
        captured = original_play_record(out, tail=tail)
        item = {
            "label": state["label"],
            "stimulus_frames": int(len(sent)),
            "stimulus_seconds": float(len(sent) / fs),
            "capture_frames": int(len(captured)),
            "capture_seconds": float(len(captured) / fs),
            "backend_notes": list(getattr(backend, "notes", [])),
        }
        if state["label"] == "thd":
            write_float_wav(out_dir / "stimulus_thd.wav", fs, sent)
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
