#!/usr/bin/env python3
"""v2.14 — capture-backed clock-pilot level diagnostic + protected THD tail check.

Hardware route: D10 (152a:85dd) -> E-MU 0404 USB (041e:3f04), direct
loopback, no DUT. This is deliberately a diagnostic level sweep, NOT a proposed
global default. It measures which pilot levels are reliable on this particular
direct route; a real DUT may have gain and may require a lower safe level.

For every clock level:
  * instantiate GUI-default Rig, edge_protect=True;
  * call the real m.clock_check(rig, level_dbfs=level);
  * preserve the exact program and full capture even when Rig.run raises;
  * trace the actual production onset index when available;
  * store per-channel 2-ms tone envelope and exact stability-window dip runs.

Levels are tested low→high and then high→low to expose state-dependent drift.
No markers are injected into clock-check: instrumentation must not make a weak
pilot easier to detect.

Finally one protected thd_point(997 Hz, -30 dBFS) is captured with an additive
non-periodic end marker. The marker is added over the final 10 ms; it does NOT
replace or truncate the submitted tone. This checks actual final-edge activity.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.io import wavfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "verification"))

from verification.v211_full_capture_anchored import find_card
from verification.timing_probe import chirp
from valvebench import calib, measure as m
from valvebench.backend import AlsaCli

FS = 192000
OUT = ROOT / "measurements" / "diagnostics" / "v214-clock-diagnostic"
TAG = datetime.now().strftime("%Y%m%d-%H%M%S")
LEVELS = (-72.0, -66.0, -60.0, -54.0, -48.0, -42.0, -36.0, -30.0,
          -30.0, -36.0, -42.0, -48.0, -54.0, -60.0, -66.0, -72.0)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class CaptureContext:
    """Capture the exact input program and output of one backend.play_record."""

    def __init__(self, backend):
        self.backend = backend
        self.pairs = []
        self.original = backend.play_record

    def __enter__(self):
        ctx = self

        def wrapped(out, *args, **kwargs):
            program = np.asarray(out, dtype=np.float64).copy()
            capture = ctx.original(out, *args, **kwargs)
            ctx.pairs.append((program, np.asarray(capture, dtype=np.float64).copy()))
            return capture

        self.backend.play_record = wrapped
        return self

    def __exit__(self, exc_type, exc, tb):
        self.backend.play_record = self.original


class AdditiveEndMarkerContext:
    """Add a unique marker without removing the final samples of the signal."""

    def __init__(self, backend, rig):
        self.backend = backend
        self.rig = rig
        self.pairs = []
        self.original = backend.play_record
        self.marker = chirp(FS, 0.010, 14000.0, 18000.0, level_dbfs=-30.0)
        self.marker_2ch = np.column_stack((self.marker, self.marker))

    def __enter__(self):
        ctx = self

        def wrapped(out, *args, **kwargs):
            program = np.asarray(out, dtype=np.float64).copy()
            marked = program.copy()
            seg_len = int(round(
                (ctx.rig.settle + ctx.rig.blocks * ctx.rig.n / FS + 0.05) * FS
            ))
            lead = int(round(ctx.rig.edge_protect_lead_s * FS))
            start = lead + seg_len - len(ctx.marker)
            end = start + len(ctx.marker)
            if start < lead or end > len(marked):
                raise RuntimeError("end marker does not fit protected THD segment")
            # IMPORTANT: additive, not replacement. The submitted sine/fade
            # remains present beneath the 14–18 kHz marker.
            marked[start:end] += ctx.marker_2ch
            capture = ctx.original(marked, *args, **kwargs)
            ctx.pairs.append((
                program.copy(), marked.copy(),
                np.asarray(capture, dtype=np.float64).copy()
            ))
            return capture

        self.backend.play_record = wrapped
        return self

    def __exit__(self, exc_type, exc, tb):
        self.backend.play_record = self.original


def tone_envelope_2ms(x: np.ndarray, fs: int, f_hz: float) -> np.ndarray:
    """Independent 2-ms lock-in amplitude for each channel."""
    w = int(round(0.002 * fs))
    n = len(x) // w
    if n <= 0:
        return np.zeros((0, 2), dtype=np.float64)
    blocks = np.asarray(x[:n * w], dtype=np.float64).reshape(n, w, 2)
    tt = np.arange(w, dtype=np.float64) / fs
    design = np.column_stack((
        np.sin(2 * np.pi * f_hz * tt),
        np.cos(2 * np.pi * f_hz * tt),
        np.ones(w),
    ))
    pinv = np.linalg.pinv(design)
    out = np.empty((n, 2), dtype=np.float64)
    for ch in (0, 1):
        coef = pinv @ blocks[:, :, ch].T
        amp = np.hypot(coef[0], coef[1])
        out[:, ch] = 20.0 * np.log10(np.maximum(amp, 1e-15))
    return out


def dip_runs(db: np.ndarray, fs: int, window_start_frame: int | None,
             window_frames: int, channel: int) -> dict:
    """Reproduce the diagnostic rule: >=12 dB below same-window median for >=50 ms."""
    w = int(round(0.002 * fs))
    start = 0 if window_start_frame is None else max(0, window_start_frame // w)
    if window_start_frame is None:
        end = len(db)
        mode = "whole_capture_no_onset"
    else:
        end = min(len(db), (window_start_frame + window_frames) // w)
        mode = "traced_production_window"
    values = db[start:end, channel]
    if not len(values):
        return {"mode": mode, "error": "no samples"}
    median = float(np.median(values))
    limit = median - 12.0
    below = values < limit
    min_bins = max(1, int(round(0.050 * fs / w)))
    runs = []
    i = 0
    while i < len(below):
        if not below[i]:
            i += 1
            continue
        j = i
        while j < len(below) and below[j]:
            j += 1
        if j - i >= min_bins:
            runs.append({
                "start_s": round((start + i + 0.5) * w / fs, 6),
                "duration_s": round((j - i) * w / fs, 6),
                "max_drop_db": round(median - float(np.min(values[i:j])), 2),
            })
        i = j
    return {
        "mode": mode,
        "window_start_s": round(start * w / fs, 6),
        "window_end_s": round(end * w / fs, 6),
        "median_dbfs": round(median, 2),
        "drop_limit_dbfs": round(limit, 2),
        "min_run_ms": 50.0,
        "dip_runs": runs,
    }


def main() -> int:
    dac, adc = find_card("152a:85dd"), find_card("041e:3f04")
    if dac is None or adc is None:
        raise SystemExit("D10/E-MU не найдены по USB VID:PID")
    OUT.mkdir(parents=True, exist_ok=True)

    cfg = calib.Calibration()
    backend = AlsaCli(
        hw_out=f"hw:{dac},0",
        hw_in=f"hw:{adc},0",
        in_map=cfg["in_map"],
        out_map=cfg["out_map"],
    )
    backend.open(FS)

    results = []
    for index, level in enumerate(LEVELS, start=1):
        rig = m.Rig(backend, cfg, fs=FS, edge_protect=True)
        onset_trace = {}
        original_onset = rig._onset

        def traced_onset(rec, tone_hz=None):
            d = original_onset(rec, tone_hz=tone_hz)
            onset_trace["offset_frames"] = int(d)
            return d

        rig._onset = traced_onset
        row = {
            "index": index,
            "level_dbfs": level,
            "edge_protect": rig.edge_protect_enabled,
            "blocks": rig.blocks,
            "settle_s_clock": 0.4,
            "lead_s": rig.edge_protect_lead_s,
            "tail_s": rig.edge_protect_tail_s,
        }
        tag = f"{TAG}-clock-{index:02d}-{int(level)}dB"
        with CaptureContext(backend) as ctx:
            try:
                clock = m.clock_check(rig, level_dbfs=level)
                row["clock_result"] = clock
                print(
                    f"[{index:02d}/{len(LEVELS)}] {level:.0f} dBFS "
                    f"ok={clock.get('ok')} "
                    f"ppm={ {ch: round(clock['ch'][ch]['ppm'], 2) for ch in clock['ch']} }",
                    flush=True,
                )
            except Exception as exc:
                row["clock_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                print(
                    f"[{index:02d}/{len(LEVELS)}] {level:.0f} dBFS "
                    f"ERROR {row['clock_error']}",
                    flush=True,
                )
        rig._onset = original_onset

        if len(ctx.pairs) != 1:
            row["capture_error"] = f"expected one capture, got {len(ctx.pairs)}"
        else:
            program, capture = ctx.pairs[0]
            ppath = OUT / f"{tag}-program.wav"
            cpath = OUT / f"{tag}-capture.wav"
            wavfile.write(ppath, FS, program.astype(np.float32))
            wavfile.write(cpath, FS, capture.astype(np.float32))
            row["files"] = {
                "program": {"path": str(ppath), "frames": len(program),
                            "sha256": sha256_file(ppath)},
                "capture": {"path": str(cpath), "frames": len(capture),
                            "sha256": sha256_file(cpath)},
            }

            row["onset"] = onset_trace or None
            window_start = None
            if "offset_frames" in onset_trace:
                window_start = onset_trace["offset_frames"] + int(round(0.4 * FS))
                row["window"] = {
                    "source": "traced Rig._onset + clock_check settle=0.4",
                    "start_s": round(window_start / FS, 6),
                    "frames": int(round(rig.blocks * rig.n)),
                    "end_s": round((window_start + rig.blocks * rig.n) / FS, 6),
                }

            # Clock pilot is 1 kHz; fit at 1 kHz for a time-local envelope.
            db = tone_envelope_2ms(capture, FS, 1000.0)
            row["envelope_2ms"] = {
                "resolution_ms": 2.0,
                "channels": {},
            }
            for ch, name in ((0, "L"), (1, "R")):
                profile = dip_runs(
                    db, FS, window_start,
                    int(round(rig.blocks * rig.n)), ch
                )
                row["envelope_2ms"]["channels"][name] = profile
                row["envelope_2ms"]["channels"][name]["median_full_dbfs"] = round(
                    float(np.median(db[:, ch])), 2
                )
                row["envelope_2ms"]["channels"][name]["min_full_dbfs"] = round(
                    float(np.min(db[:, ch])), 2
                )

        results.append(row)

    # One protected THD capture with a non-destructive end marker.
    rig = m.Rig(backend, cfg, fs=FS, edge_protect=True)
    thd_tag = f"{TAG}-thd-997"
    window_path = OUT / f"{thd_tag}-window.wav"
    thd_row = {"edge_protect": True, "tone_hz": 997.0, "level_dbfs": -30.0}
    onset_trace = {}
    original_onset = rig._onset

    def traced_thd_onset(rec, tone_hz=None):
        d = original_onset(rec, tone_hz=tone_hz)
        onset_trace["offset_frames"] = int(d)
        return d

    rig._onset = traced_thd_onset
    with AdditiveEndMarkerContext(backend, rig) as ctx:
        try:
            thd = m.thd_point(
                rig, f=997.0, level_dbfs=-30.0, capture_wav=str(window_path)
            )
            thd_row["thd"] = thd
            print(
                f"THD edge check: L={thd['ch']['L']['thdn_pct']:.3f}% "
                f"R={thd['ch']['R']['thdn_pct']:.3f}%",
                flush=True,
            )
        except Exception as exc:
            thd_row["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            print(f"THD edge check ERROR: {thd_row['error']}", flush=True)
    rig._onset = original_onset

    if len(ctx.pairs) == 1:
        program, marked, capture = ctx.pairs[0]
        ppath = OUT / f"{thd_tag}-program.wav"
        mpath = OUT / f"{thd_tag}-marked-program.wav"
        cpath = OUT / f"{thd_tag}-capture.wav"
        wavfile.write(ppath, FS, program.astype(np.float32))
        wavfile.write(mpath, FS, marked.astype(np.float32))
        wavfile.write(cpath, FS, capture.astype(np.float32))
        thd_row["files"] = {
            key: {"path": str(path), "sha256": sha256_file(path)}
            for key, path in (("program", ppath), ("marked_program", mpath),
                              ("capture", cpath), ("window", window_path))
            if path.exists()
        }
        thd_row["onset"] = onset_trace or None
        if "offset_frames" in onset_trace:
            thd_row["window_position"] = {
                "start_s": round((onset_trace["offset_frames"] + int(0.5 * FS)) / FS, 6),
                "length_s": 2.0,
                "end_s": round((onset_trace["offset_frames"] + int(2.5 * FS)) / FS, 6),
            }
        # Last 100 ms of capture: per-channel 2-ms 997-Hz envelope so the
        # additive 14–18 kHz chirp cannot be mistaken for continued tone.
        db = tone_envelope_2ms(capture, FS, 997.0)
        thd_row["tone_envelope_2ms"] = {
            "resolution_ms": 2.0,
            "last_200ms_dbfs": {
                name: np.round(db[-100:, ch], 2).tolist()
                for ch, name in ((0, "L"), (1, "R"))
            },
        }

    result = {
        "version": "v2.14-clock-preflight-capture",
        "hardware": {
            "dac_usbid": "152a:85dd",
            "adc_usbid": "041e:3f04",
            "dac_pcm": f"hw:{dac},0",
            "adc_pcm": f"hw:{adc},0",
            "sample_rate_hz": FS,
        },
        "order_dbfs": list(LEVELS),
        "safety_note": (
            "Direct D10→E-MU loopback only; do not connect a high-gain DUT for this level sweep. "
            "The results do not define a safe pilot level for arbitrary phono stages."
        ),
        "results": results,
        "protected_thd_tail_check": thd_row,
    }
    outpath = OUT / f"{TAG}-results.json"
    outpath.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\nJSON: {outpath}", flush=True)
    close = getattr(backend, "close", None)
    if callable(close):
        close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
