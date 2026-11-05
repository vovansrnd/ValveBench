#!/usr/bin/env python3
"""Extract short stereo WAV clips around steady-test residual excursions.

Run from the ValveBench project root:
  python3 /path/to/extract_steady_events.py \
    emu_measurements/steady-20261102-225630 \
    emu_measurements/steady-20261102-230356 \
    emu_measurements/steady-20261102-231000

For each run directory, this reads steady.json, timeline.csv and
capture-float32.wav. It finds residual windows above each ADC channel's
idle baseline + 6 dB, merges nearby L/R windows into shared time clips,
and writes events-float32.wav plus events-index.csv in that run directory.
The original measurements are not modified.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile


def load_run(run_dir: Path, threshold_db: float, pad_s: float, merge_gap_s: float):
    summary_path = run_dir / "summary.txt"
    json_path = run_dir / "steady.json"
    csv_path = run_dir / "timeline.csv"
    wav_path = run_dir / "capture-float32.wav"
    missing = [p.name for p in (json_path, csv_path, wav_path) if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"{run_dir}: отсутствуют {', '.join(missing)}")

    data = json.loads(json_path.read_text(encoding="utf-8"))
    fs, audio = wavfile.read(wav_path, mmap=False)
    if audio.ndim != 2 or audio.shape[1] < 2:
        raise ValueError(f"{wav_path}: ожидается стерео WAV, получено shape={audio.shape}")
    if audio.dtype != np.float32:
        audio = audio.astype(np.float32)

    hop_s = float(data.get("hop_s", 0.1))
    window_s = float(data.get("window_s", 0.1))
    analysis = data.get("analysis", {})
    baselines = {}
    for ch in ("L", "R"):
        base = analysis.get(ch, {}).get("baseline_dbfs")
        if base is None or not math.isfinite(float(base)):
            raise ValueError(f"{json_path}: нет корректного baseline для ADC {ch}")
        baselines[ch] = float(base)

    events = []
    with csv_path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        required = {"time_s", "state", "channel", "residual_20_20k_dbfs"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError(f"{csv_path}: отсутствуют столбцы {sorted(required - set(reader.fieldnames or []))}")
        for row in reader:
            if row["state"] not in ("L_steady", "R_steady"):
                continue
            ch = row["channel"]
            if ch not in ("L", "R"):
                continue
            value = float(row["residual_20_20k_dbfs"])
            limit = baselines[ch] + threshold_db
            if value > limit:
                center = float(row["time_s"])
                events.append({
                    "center": center,
                    "start": center - window_s / 2.0,
                    "end": center + window_s / 2.0,
                    "state": row["state"],
                    "channel": ch,
                    "level": value,
                    "threshold": limit,
                })

    if not events:
        index_path = run_dir / "events-index.csv"
        with index_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["clip", "source_start_s", "source_end_s", "channels", "states", "peak_L_dbfs", "peak_R_dbfs"])
            writer.writeheader()
        return {"run": run_dir.name, "events": 0, "episodes": 0, "wav": None, "index": index_path}

    events.sort(key=lambda e: (e["start"], e["end"]))
    clusters = []
    current = [events[0]]
    current_end = events[0]["end"]
    for event in events[1:]:
        if event["start"] <= current_end + merge_gap_s:
            current.append(event)
            current_end = max(current_end, event["end"])
        else:
            clusters.append(current)
            current = [event]
            current_end = event["end"]
    clusters.append(current)

    pieces = []
    records = []
    output_sample_cursor = 0
    gap_samples = int(round(0.25 * fs))
    for i, cluster in enumerate(clusters, 1):
        src_start = max(0.0, min(e["start"] for e in cluster) - pad_s)
        src_end = min(audio.shape[0] / fs, max(e["end"] for e in cluster) + pad_s)
        a = max(0, int(math.floor(src_start * fs)))
        b = min(audio.shape[0], int(math.ceil(src_end * fs)))
        clip = np.asarray(audio[a:b, :2], dtype=np.float32)
        if clip.size == 0:
            continue
        if pieces:
            pieces.append(np.zeros((gap_samples, 2), dtype=np.float32))
            output_sample_cursor += gap_samples
        output_start = output_sample_cursor
        pieces.append(clip)
        output_sample_cursor += clip.shape[0]
        channels = ",".join(ch for ch in ("L", "R") if any(e["channel"] == ch for e in cluster))
        states = ",".join(st for st in ("L_steady", "R_steady") if any(e["state"] == st for e in cluster))
        peak_l = max((e["level"] for e in cluster if e["channel"] == "L"), default=None)
        peak_r = max((e["level"] for e in cluster if e["channel"] == "R"), default=None)
        records.append({
            "clip": i,
            "source_start_s": f"{a / fs:.6f}",
            "source_end_s": f"{b / fs:.6f}",
            "channels": channels,
            "states": states,
            "peak_L_dbfs": "" if peak_l is None else f"{peak_l:.2f}",
            "peak_R_dbfs": "" if peak_r is None else f"{peak_r:.2f}",
            "output_start_s": f"{output_start / fs:.6f}",
            "output_end_s": f"{(output_start + clip.shape[0]) / fs:.6f}",
            "windows": len(cluster),
        })

    if pieces:
        out_audio = np.concatenate(pieces, axis=0)
    else:
        out_audio = np.zeros((0, 2), dtype=np.float32)
    out_wav = run_dir / "events-float32.wav"
    wavfile.write(out_wav, fs, out_audio)

    index_path = run_dir / "events-index.csv"
    fields = ["clip", "source_start_s", "source_end_s", "channels", "states",
              "peak_L_dbfs", "peak_R_dbfs", "output_start_s", "output_end_s", "windows"]
    with index_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)

    return {"run": run_dir.name, "events": len(events), "episodes": len(records),
            "wav": out_wav, "index": index_path, "duration_s": len(out_audio) / fs}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", type=Path, help="папки steady-YYYYMMDD-HHMMSS")
    parser.add_argument("--threshold", type=float, default=6.0, help="порог выше baseline, dB (default: 6)")
    parser.add_argument("--pad", type=float, default=0.5, help="контекст до/после эпизода, секунды (default: 0.5)")
    parser.add_argument("--merge-gap", type=float, default=0.2, help="объединять эпизоды при расстоянии до, секунды")
    args = parser.parse_args()
    if args.threshold <= 0 or args.pad < 0 or args.merge_gap < 0:
        parser.error("threshold должен быть >0, pad и merge-gap должны быть ≥0")
    failed = False
    for run_dir in args.run_dirs:
        try:
            result = load_run(run_dir, args.threshold, args.pad, args.merge_gap)
            if result["wav"]:
                print(f"{result['run']}: {result['events']} пороговых окон → {result['episodes']} клипов, "
                      f"{result['duration_s']:.1f} с")
                print(f"  {result['wav']}")
            else:
                print(f"{result['run']}: пороговых событий не найдено; создан пустой {result['index']}")
        except Exception as exc:
            failed = True
            print(f"ОШИБКА {run_dir}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
