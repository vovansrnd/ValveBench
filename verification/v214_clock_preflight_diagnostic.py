#!/usr/bin/env python3
"""v2.14 — capture-backed clock-pilot level sweep + independent protected-THD edge check.

Hardware route: D10 (152a:85dd) -> E-MU 0404 USB (041e:3f04), direct
loopback, no DUT. The pilot sweep characterizes THIS direct route only; it does
not define a safe global clock-pilot level for arbitrary high-gain DUTs.

Part A: levels -72..-30 dBFS, then reverse order (ABBA-like level sweep).
For every level:
  * GUI-default Rig, edge_protect=True;
  * real m.clock_check(level);
  * exact program/full capture saved even when Rig.run raises;
  * exact Rig._onset index traced when available;
  * per-channel 2-ms tone envelope and 12-dB/50-ms dip analysis saved.
No markers are injected into clock_check, so instrumentation cannot assist a
weak pilot or change the clock-preflight result.

Part B: one real edge-protected thd_point(997 Hz, -30 dBFS). Two unique chirps
are ADDED (never substituted) over the first 80 ms and final 10 ms of the
submitted segment. The 997-Hz signal remains intact. Program-to-capture timing
uses both markers, and measured tone edges are compared with the same 2-ms
boundary detector applied to the original submitted program. This accounts
for the normal 10-ms attack/release ramps instead of misreporting them as
hardware loss. The production analysis window is traced and checked against
the exact slice of the full capture.
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
MARKER_A_S = 0.080
MARKER_B_S = 0.010


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class CaptureContext:
    """Capture exact program and full ADC record for one backend.play_record."""

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


class MarkedThdCaptureContext:
    """Add two chirps in ignored edge regions without removing submitted samples."""

    def __init__(self, backend, rig):
        self.backend = backend
        self.rig = rig
        self.pairs = []
        self.original = backend.play_record
        self.marker_a = chirp(FS, MARKER_A_S, 6500.0, 7500.0, level_dbfs=-40.0)
        self.marker_b = chirp(FS, MARKER_B_S, 14000.0, 18000.0, level_dbfs=-45.0)
        self.marker_a_2ch = np.column_stack((self.marker_a, self.marker_a))
        self.marker_b_2ch = np.column_stack((self.marker_b, self.marker_b))
        self.lead_n = int(round(rig.edge_protect_lead_s * FS))
        self.seg_len = int(round(
            (rig.settle + rig.blocks * rig.n / FS + 0.05) * FS
        ))
        self.marker_b_pos = self.lead_n + self.seg_len - len(self.marker_b)

    def __enter__(self):
        ctx = self

        def wrapped(out, *args, **kwargs):
            program = np.asarray(out, dtype=np.float64).copy()
            marked = program.copy()
            a0, a1 = ctx.lead_n, ctx.lead_n + len(ctx.marker_a)
            b0, b1 = ctx.marker_b_pos, ctx.marker_b_pos + len(ctx.marker_b)
            if a1 > len(marked) or b0 < a1 or b1 > len(marked):
                raise RuntimeError("chirp marker positions do not fit Rig.run program")
            marked[a0:a1] += ctx.marker_a_2ch
            # Additive marker: the original tone and its normal release ramp remain.
            marked[b0:b1] += ctx.marker_b_2ch
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


def normalized_xcorr_match(arr: np.ndarray, tmpl: np.ndarray, fs: int):
    """FFT normalized correlation; return best offset, score and competitor."""
    arr = np.asarray(arr, dtype=np.float64)
    tmpl = np.asarray(tmpl, dtype=np.float64)
    n, mm = len(arr), len(tmpl)
    if mm < 8 or n < mm:
        raise ValueError("invalid correlation lengths")
    fft_len = 1 << int(np.ceil(np.log2(n + mm)))
    corr = np.fft.irfft(
        np.fft.rfft(arr, fft_len) * np.conj(np.fft.rfft(tmpl, fft_len)),
        fft_len
    )[:n - mm + 1]
    cs = np.concatenate(([0.0], np.cumsum(arr * arr)))
    energy = np.maximum(cs[mm:] - cs[:n - mm + 1], 1e-30)
    scores = corr / (np.linalg.norm(tmpl) * np.sqrt(energy))
    best = int(np.argmax(scores))
    best_score = float(scores[best])
    exclusion = max(mm // 2, int(round(0.010 * fs)))
    other = scores.copy()
    other[max(0, best - exclusion):min(len(other), best + exclusion + 1)] = -np.inf
    second = float(np.max(other)) if np.any(np.isfinite(other)) else float("-inf")
    return best, best_score, second


def tone_envelope_2ms(x: np.ndarray, fs: int, f_hz: float) -> np.ndarray:
    """Independent 2-ms least-squares amplitude profile for both channels."""
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
    db = np.empty((n, 2), dtype=np.float64)
    for ch in (0, 1):
        coef = pinv @ blocks[:, :, ch].T
        amp = np.hypot(coef[0], coef[1])
        db[:, ch] = 20.0 * np.log10(np.maximum(amp, 1e-15))
    return db


def find_profile_bounds(db: np.ndarray, fs: int, segment_start: int,
                        segment_end: int, channel: int,
                        floor_db: float | None = None) -> dict:
    """First/last sustained 997-Hz activity and internal gaps at 2-ms resolution."""
    w = int(round(0.002 * fs))
    centers = (np.arange(len(db)) + 0.5) * w
    seg_start_samp = max(0, int(segment_start))
    seg_end_samp = min(len(db) * w, int(segment_end))
    expected = (centers >= seg_start_samp) & (centers < seg_end_samp)
    idx = np.where(expected)[0]
    if len(idx) < 8:
        return {"error": "segment outside profile"}
    outside = (centers < seg_start_samp - w) | (centers >= seg_end_samp + w)
    if floor_db is None:
        floor = float(np.median(db[outside, channel])) if np.count_nonzero(outside) >= 10 \
            else float(np.percentile(db[:, channel], 20))
    else:
        floor = float(floor_db)
    plateau = (centers >= seg_start_samp + 0.2 * (seg_end_samp - seg_start_samp)) & \
              (centers < seg_start_samp + 0.8 * (seg_end_samp - seg_start_samp))
    plateau_db = float(np.median(db[plateau, channel]))
    threshold = max(floor + 12.0, plateau_db - 20.0)
    active = db[:, channel] > threshold
    mask = active[idx]
    run_min = max(1, int(round(0.050 * fs / w)))

    first = None
    for j in range(0, max(0, len(mask) - run_min + 1)):
        if mask[j] and np.count_nonzero(mask[j:j + run_min]) >= int(np.ceil(0.9 * run_min)):
            first = int(idx[j])
            break

    last = None
    for j in range(len(mask) - 1, run_min - 2, -1):
        lo = j - run_min + 1
        if mask[j] and np.count_nonzero(mask[lo:j + 1]) >= int(np.ceil(0.9 * run_min)):
            last = int(idx[j])
            break

    if first is None or last is None or last < first:
        return {
            "error": "no sustained tone region",
            "floor_dbfs": round(floor, 2),
            "plateau_dbfs": round(plateau_db, 2),
            "threshold_dbfs": round(threshold, 2),
        }

    between = active[first:last + 1]
    gaps, current = [], 0
    for value in between:
        if not value:
            current += 1
        elif current:
            gaps.append(current)
            current = 0
    if current:
        gaps.append(current)

    return {
        "start_frame": int(first * w),
        "end_frame": int((last + 1) * w),
        "start_s": round(first * w / fs, 6),
        "end_s": round((last + 1) * w / fs, 6),
        "span_s": round((last + 1 - first) * w / fs, 6),
        "floor_dbfs": round(floor, 2),
        "plateau_dbfs": round(plateau_db, 2),
        "threshold_dbfs": round(threshold, 2),
        "inactive_bins_inside_bounds": int(len(between) - np.count_nonzero(between)),
        "max_internal_gap_ms": round(max(gaps, default=0) * 2.0, 3),
        "resolution_ms": 2.0,
    }


def dip_runs(db: np.ndarray, fs: int, window_start_frame: int | None,
             window_frames: int, channel: int) -> dict:
    """Apply the same 12-dB / 50-ms per-channel dip criterion as Rig.run."""
    w = int(round(0.002 * fs))
    start = 0 if window_start_frame is None else max(0, window_start_frame // w)
    end = len(db) if window_start_frame is None else min(
        len(db), (window_start_frame + window_frames) // w
    )
    values = db[start:end, channel]
    if not len(values):
        return {"error": "no samples"}
    median = float(np.median(values))
    threshold = median - 12.0
    mask = values < threshold
    run_min = max(1, int(round(0.050 * fs / w)))
    runs = []
    i = 0
    while i < len(mask):
        if not mask[i]:
            i += 1
            continue
        j = i
        while j < len(mask) and mask[j]:
            j += 1
        if j - i >= run_min:
            runs.append({
                "start_s": round((start + i + 0.5) * w / fs, 6),
                "duration_s": round((j - i) * w / fs, 6),
                "max_drop_db": round(median - float(np.min(values[i:j])), 2),
            })
        i = j
    return {
        "window_start_s": round(start * w / fs, 6),
        "window_end_s": round(end * w / fs, 6),
        "median_dbfs": round(median, 2),
        "threshold_dbfs": round(threshold, 2),
        "runs": runs,
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

    clock_rows = []
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
            "edge_protect": True,
            "blocks": rig.blocks,
            "clock_settle_s": 0.4,
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
            db = tone_envelope_2ms(capture, FS, 1000.0)
            row["envelope_2ms"] = {
                "resolution_ms": 2.0,
                "channels": {
                    name: {
                        "dip_analysis": dip_runs(
                            db, FS, window_start, int(round(rig.blocks * rig.n)), ch
                        ),
                        "median_full_capture_dbfs": round(float(np.median(db[:, ch])), 2),
                        "min_full_capture_dbfs": round(float(np.min(db[:, ch])), 2),
                    }
                    for ch, name in ((0, "L"), (1, "R"))
                },
            }
        clock_rows.append(row)

    # One real protected THD capture; end marker is additive and outside the
    # 2-s analysis window. This provides a clean tail-boundary comparison.
    rig = m.Rig(backend, cfg, fs=FS, edge_protect=True)
    onset_trace = {}
    original_onset = rig._onset

    def traced_thd_onset(rec, tone_hz=None):
        d = original_onset(rec, tone_hz=tone_hz)
        onset_trace["offset_frames"] = int(d)
        return d

    rig._onset = traced_thd_onset
    window_path = OUT / f"{TAG}-thd-window.wav"
    thd_row = {
        "edge_protect": True,
        "tone_hz": 997.0,
        "level_dbfs": -30.0,
        "lead_s": rig.edge_protect_lead_s,
        "tail_s": rig.edge_protect_tail_s,
    }
    with MarkedThdCaptureContext(backend, rig) as ctx:
        try:
            thd = m.thd_point(
                rig, f=997.0, level_dbfs=-30.0, capture_wav=str(window_path)
            )
            thd_row["thd"] = thd
            print(
                f"THD: L={thd['ch']['L']['thdn_pct']:.3f}% "
                f"R={thd['ch']['R']['thdn_pct']:.3f}%",
                flush=True,
            )
        except Exception as exc:
            thd_row["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            print(f"THD ERROR: {thd_row['error']}", flush=True)
    rig._onset = original_onset

    if len(ctx.pairs) == 1:
        program, marked, capture = ctx.pairs[0]
        ppath = OUT / f"{TAG}-thd-program.wav"
        mpath = OUT / f"{TAG}-thd-marked-program.wav"
        cpath = OUT / f"{TAG}-thd-capture.wav"
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
            win_start = onset_trace["offset_frames"] + int(round(rig.settle * FS))
            win_len = int(round(rig.blocks * rig.n))
            thd_row["window"] = {
                "start_s": round(win_start / FS, 6),
                "end_s": round((win_start + win_len) / FS, 6),
                "frames": win_len,
            }
            if window_path.exists():
                _, window = wavfile.read(window_path)
                sl = capture[win_start:win_start + win_len]
                if len(sl) == len(window):
                    diff = sl.astype(np.float64) - window.astype(np.float64)
                    thd_row["window_slice"] = {
                        "exact_frames": len(sl) == win_len,
                        "max_abs_difference": float(np.max(np.abs(diff))) if diff.size else 0.0,
                    }

        # Align each capture channel to the actual submitted program using
        # both non-periodic chirps; the periodic 997-Hz carrier is not an anchor.
        marker_a_pos = ctx.lead_n
        marker_b_pos = ctx.marker_b_pos
        offsets = {}
        marker_rows = {}
        for ch, name in ((0, "L"), (1, "R")):
            a_off, a_score, a_second = normalized_xcorr_match(
                capture[:, ch], ctx.marker_a, FS
            )
            b_off, b_score, b_second = normalized_xcorr_match(
                capture[:, ch], ctx.marker_b, FS
            )
            off_a = a_off - marker_a_pos
            off_b = b_off - marker_b_pos
            a_gap, b_gap = a_score - a_second, b_score - b_second
            a_ok = a_score >= 0.5 and a_gap >= 0.05
            b_ok = b_score >= 0.5 and b_gap >= 0.05
            marker_rows[name] = {
                "A_score": round(a_score, 6), "A_gap": round(a_gap, 6),
                "A_valid": bool(a_ok), "offset_from_A_s": round(off_a / FS, 6),
                "B_score": round(b_score, 6), "B_gap": round(b_gap, 6),
                "B_valid": bool(b_ok), "offset_from_B_s": round(off_b / FS, 6),
                "A_B_disagreement_ms": round(abs(off_a - off_b) * 1000.0 / FS, 3),
            }
            if a_ok and b_ok and abs(off_a - off_b) <= int(round(0.002 * FS)):
                offsets[name] = int(round((off_a + off_b) / 2))
        thd_row["marker_binding"] = marker_rows
        thd_row["marker_binding_valid"] = len(offsets) == 2 and \
            abs(offsets["L"] - offsets["R"]) <= int(round(0.002 * FS))

        # Compute expected and captured boundaries using the SAME detector.
        ppm_values = [
            float(row["clock_result"]["ch"][name]["ppm"])
            for row in clock_rows if isinstance(row.get("clock_result"), dict)
            and row["clock_result"].get("ok")
            for name in ("L", "R")
        ]
        ppm = float(np.median(ppm_values)) if ppm_values else -23.5
        f_tone = 997.0 * (1.0 + ppm * 1e-6)
        thd_row["boundary_frequency_hz"] = f_tone
        p_db = tone_envelope_2ms(program, FS, f_tone)
        c_db = tone_envelope_2ms(capture, FS, f_tone)
        seg_start = ctx.lead_n
        seg_end = ctx.lead_n + ctx.seg_len
        boundary = {"resolution_ms": 2.0, "channels": {}}
        if thd_row["marker_binding_valid"]:
            for ch, name in ((0, "L"), (1, "R")):
                expected = find_profile_bounds(
                    p_db, FS, seg_start, seg_end, ch
                )
                measured = find_profile_bounds(
                    c_db, FS, offsets[name] + seg_start, offsets[name] + seg_end, ch
                )
                item = {"submitted_program": expected, "captured_program": measured}
                if "start_frame" in expected and "start_frame" in measured:
                    expected_start = offsets[name] + expected["start_frame"]
                    expected_end = offsets[name] + expected["end_frame"]
                    item["head_loss_ms_vs_submitted_envelope"] = round(
                        (measured["start_frame"] - expected_start) * 1000.0 / FS, 3
                    )
                    item["tail_loss_ms_vs_submitted_envelope"] = round(
                        (expected_end - measured["end_frame"]) * 1000.0 / FS, 3
                    )
                boundary["channels"][name] = item
        else:
            boundary["error"] = "independent A/B markers did not validate on both channels"
        thd_row["tone_boundaries_vs_program"] = boundary

    result = {
        "version": "v2.14-clock-preflight-capture",
        "hardware": {
            "dac_usbid": "152a:85dd",
            "adc_usbid": "041e:3f04",
            "dac_pcm": f"hw:{dac},0",
            "adc_pcm": f"hw:{adc},0",
            "sample_rate_hz": FS,
        },
        "clock_level_order_dbfs": list(LEVELS),
        "safety_note": (
            "Direct D10→E-MU loopback only; no DUT connected. This sweep does not "
            "authorize -30 dBFS for arbitrary high-gain phono stages."
        ),
        "clock_results": clock_rows,
        "protected_thd_tail_check": thd_row,
    }
    json_path = OUT / f"{TAG}-results.json"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\nJSON: {json_path}", flush=True)

    close = getattr(backend, "close", None)
    if callable(close):
        close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
