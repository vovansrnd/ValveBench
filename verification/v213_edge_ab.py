#!/usr/bin/env python3
"""v2.13 — controlled D10 edge OFF/ON A/B with non-periodic time markers.

One AlsaCli, GUI-default Rig (blocks=2, settle=0.5), ABBA order:
OFF -> ON -> ON -> OFF. Every arm runs clock_check(-30 dBFS), then the real
thd_point(997 Hz, -30 dBFS). -72 dBFS is intentionally excluded because it
was shown unreliable and would confound this edge-only comparison.

The play_record wrapper adds two low-level, non-periodic chirp markers around
the actual Rig.run program. They are outside the analysis window. The tone
itself is NEVER used as an absolute timing anchor. Marker A anchors the start;
marker B independently anchors the end. Full original program, marked
program, full capture and production window are retained.

Tone boundaries are determined from 2-ms RMS bins using noise measured
outside the submitted tone segment. The analysis reports internal inactive
bins and the largest internal gap, not just first/last activity. Sub-2-ms
head/tail values are not interpreted as precise measurements.
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

from verification.timing_probe import chirp
from valvebench import calib, measure as m
from valvebench.backend import AlsaCli

FS = 192000
OUT = ROOT / "measurements" / "diagnostics" / "v213-edge-ab"
TAG = datetime.now().strftime("%Y%m%d-%H%M%S")
CASES = (("OFF-1", False), ("ON-1", True), ("ON-2", True), ("OFF-2", False))
MARKER_S = 0.080
GAP_S = 0.150
POST_S = 0.250


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_card(usbid: str):
    for item in os.listdir("/proc/asound"):
        if item.startswith("card"):
            try:
                if Path(f"/proc/asound/{item}/usbid").read_text().strip() == usbid:
                    return int(item[4:])
            except OSError:
                pass
    return None


def normalized_xcorr_match(arr: np.ndarray, tmpl: np.ndarray, fs: int):
    """Return best offset/score and strongest competing peak outside marker lobe."""
    arr = np.asarray(arr, dtype=np.float64)
    tmpl = np.asarray(tmpl, dtype=np.float64)
    n, mm = len(arr), len(tmpl)
    if mm < 8 or n < mm:
        raise ValueError(f"invalid xcorr lengths: capture={n}, template={mm}")
    length = 1 << int(np.ceil(np.log2(n + mm)))
    corr = np.fft.irfft(
        np.fft.rfft(arr, length) * np.conj(np.fft.rfft(tmpl, length)), length
    )[:n - mm + 1]
    cs = np.concatenate(([0.0], np.cumsum(arr * arr)))
    energy = np.maximum(cs[mm:] - cs[:n - mm + 1], 1e-30)
    scores = corr / (np.linalg.norm(tmpl) * np.sqrt(energy))
    best = int(np.argmax(scores))
    best_score = float(scores[best])
    exclusion = max(mm // 2, int(round(0.010 * fs)))
    competitor = scores.copy()
    competitor[max(0, best - exclusion):min(len(scores), best + exclusion + 1)] = -np.inf
    second = float(np.max(competitor)) if np.any(np.isfinite(competitor)) else float("-inf")
    return best, best_score, second


class MarkedCaptureCtx:
    """Wrap exactly one play_record; add markers outside the generated program."""

    def __init__(self, backend, fs: int):
        self.backend = backend
        self.fs = int(fs)
        self.pairs = []
        self.marker_a_mono = chirp(fs, MARKER_S, 6500.0, 7500.0, level_dbfs=-30.0)
        self.marker_b_mono = chirp(fs, MARKER_S, 10500.0, 11500.0, level_dbfs=-30.0)
        self.marker_a = np.column_stack((self.marker_a_mono, self.marker_a_mono))
        self.marker_b = np.column_stack((self.marker_b_mono, self.marker_b_mono))
        self.gap = np.zeros((int(round(GAP_S * fs)), 2), dtype=np.float64)
        self.post = np.zeros((int(round(POST_S * fs)), 2), dtype=np.float64)
        self.prefix_frames = len(self.marker_a) + len(self.gap)
        self._orig = backend.play_record

    def __enter__(self):
        ctx = self

        def wrapped(out, *args, **kwargs):
            original_program = np.asarray(out, dtype=np.float64).copy()
            marked_program = np.concatenate(
                [ctx.marker_a, ctx.gap, original_program, ctx.gap, ctx.marker_b, ctx.post],
                axis=0,
            )
            capture = ctx._orig(marked_program, *args, **kwargs)
            ctx.pairs.append((
                original_program,
                marked_program.copy(),
                np.asarray(capture, dtype=np.float64).copy(),
            ))
            return capture

        self.backend.play_record = wrapped
        return self

    def __exit__(self, exc_type, exc, tb):
        self.backend.play_record = self._orig


def capture_tone_profile(cap: np.ndarray, segment_offset: int, seg_len: int,
                         fs: int, settle_s: float, window_s: float,
                         block_ms: float = 2.0) -> dict:
    """Per-channel tone boundaries and internal gaps at block_ms resolution."""
    w = max(1, int(round(block_ms * 1e-3 * fs)))
    n = len(cap) // w
    if n < 8:
        return {"error": "capture too short", "resolution_ms": block_ms}
    x = np.asarray(cap[:n * w], dtype=np.float64).reshape(n, w, 2)
    rms = np.sqrt(np.mean(x * x, axis=1)) * np.sqrt(2.0)
    db = 20.0 * np.log10(np.maximum(rms, 1e-15))
    centers = (np.arange(n) + 0.5) * w
    segment_end = segment_offset + seg_len
    outside = (centers < segment_offset - w) | (centers >= segment_end + w)
    if np.count_nonzero(outside) >= 25:
        floor_db = np.median(db[outside], axis=0)
        floor_source = "outside_segment_median"
    else:
        floor_db = np.percentile(db, 20, axis=0)
        floor_source = "capture_20th_percentile_fallback"

    plateau = (centers >= segment_offset + 0.2 * seg_len) & (
        centers < segment_offset + 0.8 * seg_len
    )
    if not np.any(plateau):
        return {"error": "no plateau bins", "resolution_ms": block_ms}

    result = {
        "resolution_ms": block_ms,
        "floor_source": floor_source,
        "sustain_ms": 50.0,
        "channels": {},
    }
    run_min = max(1, int(round(0.050 * fs / w)))
    expected = (centers >= segment_offset) & (centers < segment_end)
    inside_idx = np.where(expected)[0]
    for c, name in ((0, "L"), (1, "R")):
        floor = float(floor_db[c])
        plateau_level = float(np.median(db[plateau, c]))
        threshold = max(floor + 12.0, plateau_level - 20.0)
        active = db[:, c] > threshold
        mask = active[inside_idx]

        first = None
        for j in range(0, max(0, len(mask) - run_min + 1)):
            if mask[j] and np.count_nonzero(mask[j:j + run_min]) >= int(np.ceil(0.9 * run_min)):
                first = int(inside_idx[j])
                break

        last = None
        for j in range(len(mask) - 1, run_min - 2, -1):
            lo = j - run_min + 1
            if mask[j] and np.count_nonzero(mask[lo:j + 1]) >= int(np.ceil(0.9 * run_min)):
                last = int(inside_idx[j])
                break

        if first is None or last is None or last < first:
            result["channels"][name] = {
                "error": "no sustained tone region",
                "floor_dbfs": round(floor, 2),
                "plateau_dbfs": round(plateau_level, 2),
                "threshold_dbfs": round(threshold, 2),
            }
            continue

        between = active[first:last + 1]
        gaps = []
        gap = 0
        for value in between:
            if not value:
                gap += 1
            elif gap:
                gaps.append(gap)
                gap = 0
        if gap:
            gaps.append(gap)

        start_s = first * w / fs
        end_s = (last + 1) * w / fs
        window_start = segment_offset / fs + settle_s
        window_end = window_start + window_s
        result["channels"][name] = {
            "floor_dbfs": round(floor, 2),
            "plateau_dbfs": round(plateau_level, 2),
            "threshold_dbfs": round(threshold, 2),
            "tone_start_s": round(start_s, 6),
            "tone_end_s": round(end_s, 6),
            "tone_span_s": round(end_s - start_s, 6),
            "active_bins_inside_bounds": int(np.count_nonzero(between)),
            "bins_inside_bounds": int(len(between)),
            "inactive_bins_inside_bounds": int(len(between) - np.count_nonzero(between)),
            "max_internal_gap_ms": round(max(gaps, default=0) * w * 1000.0 / fs, 3),
            "window_start_s": round(window_start, 6),
            "window_end_s": round(window_end, 6),
            "window_inside_detected_tone": bool(start_s <= window_start and end_s >= window_end),
        }
    return result


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

    for label, edge_on in CASES:
        rig = m.Rig(backend, cfg, fs=FS, edge_protect=edge_on)
        print(
            f"\n=== {label}: edge={edge_on}, blocks={rig.blocks}, settle={rig.settle}, "
            f"lead={rig.edge_protect_lead_s if edge_on else 0}, "
            f"tail={rig.edge_protect_tail_s if edge_on else 0} ===",
            flush=True,
        )
        row = {
            "label": label,
            "edge_protect": edge_on,
            "blocks": rig.blocks,
            "settle_s": rig.settle,
            "lead_s": rig.edge_protect_lead_s if edge_on else 0.0,
            "tail_s": rig.edge_protect_tail_s if edge_on else 0.0,
        }

        # The known reliable level is identical in both arms. Do not use -72:
        # its unreliable detection was already established separately.
        try:
            ck = m.clock_check(rig, level_dbfs=-30.0)
            row["clock_check_minus30"] = ck
            print(
                f"clock_check -30: ok={ck.get('ok')} "
                f"ppm={ {c: round(ck['ch'][c]['ppm'], 2) for c in ck['ch']} }",
                flush=True,
            )
        except Exception as exc:
            row["clock_check_minus30"] = {
                "error": f"{type(exc).__name__}: {str(exc)[:160]}"
            }
            print(f"clock_check -30 ERROR: {row['clock_check_minus30']['error']}", flush=True)

        window_path = OUT / f"{TAG}-{label}-window.wav"
        thd = None
        with MarkedCaptureCtx(backend, FS) as ctx:
            try:
                thd = m.thd_point(
                    rig, f=997.0, level_dbfs=-30.0, capture_wav=str(window_path)
                )
                row["thd"] = thd
            except Exception as exc:
                # Keep the full capture even when the unprotected arm fails.
                row["measurement_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                print(f"thd_point ERROR: {row['measurement_error']}", flush=True)

        if len(ctx.pairs) != 1:
            row["capture_error"] = f"expected one play_record, got {len(ctx.pairs)}"
        else:
            original, marked, capture = ctx.pairs[0]
            program_path = OUT / f"{TAG}-{label}-program.wav"
            marked_path = OUT / f"{TAG}-{label}-marked-program.wav"
            capture_path = OUT / f"{TAG}-{label}-full-capture.wav"
            wavfile.write(program_path, FS, original.astype(np.float32))
            wavfile.write(marked_path, FS, marked.astype(np.float32))
            wavfile.write(capture_path, FS, capture.astype(np.float32))

            seg_len = int(round((rig.settle + rig.blocks * rig.n / FS + 0.05) * FS))
            lead_n = int(round(rig.edge_protect_lead_s * FS)) if edge_on else 0
            if lead_n + seg_len > len(original):
                row["capture_analysis_error"] = "tone segment bounds exceed original program"
            else:
                marker_a = ctx.marker_a_mono
                marker_b = ctx.marker_b_mono
                gap_n = len(ctx.gap)
                marker_b_program_pos = ctx.prefix_frames + len(original) + gap_n
                binding = {"marker_a_s": MARKER_S, "marker_b_s": MARKER_S, "gap_s": GAP_S}

                offsets_a = {}
                offsets_b = {}
                for c, name in ((0, "L"), (1, "R")):
                    a_off, a_score, a_second = normalized_xcorr_match(
                        capture[:, c], marker_a, FS
                    )
                    b_off, b_score, b_second = normalized_xcorr_match(
                        capture[:, c], marker_b, FS
                    )
                    a_gap = a_score - a_second
                    b_gap = b_score - b_second
                    a_valid = a_score >= 0.75 and a_gap >= 0.05
                    b_valid = b_score >= 0.75 and b_gap >= 0.05
                    offset_a = a_off + ctx.prefix_frames
                    offset_b = b_off - marker_b_program_pos
                    if a_valid:
                        offsets_a[name] = offset_a
                    if b_valid:
                        offsets_b[name] = offset_b
                    binding[name] = {
                        "marker_a_capture_s": round(a_off / FS, 6),
                        "marker_a_score": round(a_score, 6),
                        "marker_a_competitor": round(a_second, 6),
                        "marker_a_peak_gap": round(a_gap, 6),
                        "marker_a_valid": bool(a_valid),
                        "program_offset_from_a_s": round(offset_a / FS, 6),
                        "marker_b_capture_s": round(b_off / FS, 6),
                        "marker_b_score": round(b_score, 6),
                        "marker_b_competitor": round(b_second, 6),
                        "marker_b_peak_gap": round(b_gap, 6),
                        "marker_b_valid": bool(b_valid),
                        "program_offset_from_b_s": round(offset_b / FS, 6),
                    }

                row["marker_binding"] = binding
                common_offsets = [
                    v for channel in ("L", "R")
                    for v in (offsets_a.get(channel), offsets_b.get(channel))
                    if v is not None
                ]
                if not common_offsets:
                    row["capture_analysis_error"] = "neither chirp marker had a unique valid match"
                else:
                    program_offset = int(round(float(np.median(common_offsets))))
                    row["program_offset_s"] = round(program_offset / FS, 6)
                    row["marker_anchor_disagreement_ms"] = {
                        ch: round(abs(offsets_a[ch] - offsets_b[ch]) * 1000.0 / FS, 3)
                        for ch in ("L", "R") if ch in offsets_a and ch in offsets_b
                    }

                    window_s = rig.blocks * rig.n / FS
                    profile = capture_tone_profile(
                        capture, program_offset + lead_n, seg_len, FS,
                        rig.settle, window_s,
                    )
                    row["capture_analysis"] = profile
                    for ch in ("L", "R"):
                        info = profile.get("channels", {}).get(ch, {})
                        if not info or "tone_start_s" not in info:
                            continue
                        # Start loss is measured from the END marker anchor;
                        # tail loss is measured from the START marker anchor.
                        if ch in offsets_b:
                            expected_start = (offsets_b[ch] + lead_n) / FS
                            info["head_loss_ms_estimate"] = round(
                                (info["tone_start_s"] - expected_start) * 1000.0, 3
                            )
                            info["head_anchor"] = "marker_B"
                        else:
                            info["head_loss_ms_estimate"] = None
                            info["head_anchor"] = None
                        if ch in offsets_a:
                            expected_end = (offsets_a[ch] + lead_n + seg_len) / FS
                            info["tail_loss_ms_estimate"] = round(
                                (expected_end - info["tone_end_s"]) * 1000.0, 3
                            )
                            info["tail_anchor"] = "marker_A"
                        else:
                            info["tail_loss_ms_estimate"] = None
                            info["tail_anchor"] = None

            row["files"] = {}
            for key, path in (
                ("program", program_path),
                ("marked_program", marked_path),
                ("full_capture", capture_path),
                ("window", window_path),
            ):
                row["files"][key] = {
                    "path": str(path),
                    "sha256": sha256_file(path) if path.exists() else None,
                }

            if thd is not None:
                row["thd_summary"] = {
                    ch: {
                        "thdn_pct": thd["ch"][ch].get("thdn_pct"),
                        "thd_pct": thd["ch"][ch].get("thd_pct"),
                        "thd_upper_pct": thd["ch"][ch].get("thd_upper_pct"),
                        "level_dbfs": thd["ch"][ch].get("level_dbfs"),
                        "noise_dbfs": thd["ch"][ch].get("noise_dbfs"),
                    }
                    for ch in ("L", "R") if ch in thd.get("ch", {})
                }

        results.append(row)

    result = {
        "version": "v2.13-edge-ab",
        "hardware": {
            "dac_usbid": "152a:85dd",
            "adc_usbid": "041e:3f04",
            "dac_pcm": f"hw:{dac},0",
            "adc_pcm": f"hw:{adc},0",
            "sample_rate_hz": FS,
        },
        "order": [x[0] for x in CASES],
        "notes": [
            "ABBA order: OFF, ON, ON, OFF.",
            "Each arm uses the real clock_check(-30 dBFS) and thd_point(997 Hz, -30 dBFS).",
            "The known unreliable -72 dBFS production preflight is excluded from this controlled edge-only comparison.",
            "Absolute binding uses two non-periodic chirp markers, not the periodic 997-Hz tone or capture_wav self-correlation.",
            "Marker A anchors the program start; marker B independently anchors the program end.",
            "2-ms boundary uncertainty applies; sub-2-ms loss values are only estimates, not resolved measurements.",
            "Internal inactive bins and maximum internal gap are recorded.",
        ],
        "results": results,
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
