#!/usr/bin/env python3
"""v2.13 — controlled D10 edge OFF/ON A/B with in-program chirp markers.

One AlsaCli, GUI-default Rig (blocks=2, settle=0.5), ABBA order:
OFF -> ON -> ON -> OFF. A single clock_check(-30 dBFS) precedes the sequence,
then each arm calls the real thd_point(997 Hz, -30 dBFS). The unreliable
production -72 dBFS preflight is kept out of this edge-only comparison.

No extra playback duration or pre-roll is added by the markers:
* Marker A is a short 6.5–7.5 kHz chirp ADDED over the first 80 ms of the
  existing tone segment. The 997-Hz tone remains present from sample zero,
  so this does not create an artificial lead or change onset timing.
* Marker B is a 14–18 kHz chirp occupying the final 10 ms of the segment,
  after the 2.0-s production analysis window (the segment has 50 ms padding).

Marker A and B are non-periodic unique anchors. The 997-Hz tone itself is
never used as an absolute time anchor. Marker peak uniqueness is checked.
Tone boundaries are derived from a 2-ms least-squares lock-in at the measured
997-Hz frequency; internal gaps are reported. Sub-2-ms losses are not claimed
as resolved measurements.
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
MARKER_A_S = 0.080
MARKER_B_S = 0.010


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
    """Best normalized correlation and strongest separated competing peak."""
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
    """Overlay unique markers in settle/padding without changing program length."""

    def __init__(self, backend, rig, fs: int):
        self.backend = backend
        self.rig = rig
        self.fs = int(fs)
        self.pairs = []
        self.marker_a_mono = chirp(fs, MARKER_A_S, 6500.0, 7500.0, level_dbfs=-30.0)
        self.marker_b_mono = chirp(fs, MARKER_B_S, 14000.0, 18000.0, level_dbfs=-30.0)
        self.marker_a = np.column_stack((self.marker_a_mono, self.marker_a_mono))
        self.marker_b = np.column_stack((self.marker_b_mono, self.marker_b_mono))
        self._orig = backend.play_record
        self.lead_n = int(round(rig.edge_protect_lead_s * fs)) if rig.edge_protect_enabled else 0
        self.seg_len = int(round((rig.settle + rig.blocks * rig.n / fs + 0.05) * fs))
        self.marker_b_pos = self.lead_n + self.seg_len - len(self.marker_b)

    def __enter__(self):
        ctx = self

        def wrapped(out, *args, **kwargs):
            original = np.asarray(out, dtype=np.float64).copy()
            marked = original.copy()
            a0 = ctx.lead_n
            a1 = a0 + len(ctx.marker_a)
            b0 = ctx.marker_b_pos
            b1 = b0 + len(ctx.marker_b)
            if a1 > len(marked) or b0 < a1 or b1 > len(marked):
                raise RuntimeError("marker positions do not fit the submitted Rig.run program")
            # Marker A is additive: the 997-Hz tone stays intact at onset.
            marked[a0:a1] += ctx.marker_a
            # Marker B replaces only the final 10 ms, after the production window.
            marked[b0:b1] = ctx.marker_b
            capture = ctx._orig(marked, *args, **kwargs)
            ctx.pairs.append((
                original,
                marked.copy(),
                np.asarray(capture, dtype=np.float64).copy(),
            ))
            return capture

        self.backend.play_record = wrapped
        return self

    def __exit__(self, exc_type, exc, tb):
        self.backend.play_record = self._orig


def tone_envelope_2ms(cap: np.ndarray, fs: int, f_tone: float):
    """Per-2-ms, per-channel LSQ amplitude of f_tone; high-frequency markers reject."""
    w = int(round(0.002 * fs))
    n = len(cap) // w
    blocks = np.asarray(cap[:n * w], dtype=np.float64).reshape(n, w, 2)
    tt = np.arange(w, dtype=np.float64) / fs
    design = np.column_stack((
        np.sin(2.0 * np.pi * f_tone * tt),
        np.cos(2.0 * np.pi * f_tone * tt),
        np.ones(w),
    ))
    projection = np.linalg.pinv(design)
    result = {}
    for c, name in ((0, "L"), (1, "R")):
        coef = projection @ blocks[:, :, c].T
        amp = np.hypot(coef[0], coef[1])
        result[name] = 20.0 * np.log10(np.maximum(amp, 1e-15))
    return result, w


def capture_tone_profile(cap: np.ndarray, segment_offset: int, seg_len: int,
                         fs: int, f_tone: float, settle_s: float,
                         window_s: float, block_ms: float = 2.0) -> dict:
    """Tone-selective boundaries, dropout gaps, and measurement-window coverage."""
    db, w = tone_envelope_2ms(cap, fs, f_tone)
    centers = (np.arange(len(db["L"])) + 0.5) * w
    segment_end = segment_offset + seg_len
    outside = (centers < segment_offset - w) | (centers >= segment_end + w)
    expected = (centers >= segment_offset) & (centers < segment_end)
    inside_idx = np.where(expected)[0]
    if len(inside_idx) < 8:
        return {"error": "segment outside capture", "resolution_ms": block_ms}

    result = {"resolution_ms": block_ms, "sustain_ms": 50.0, "channels": {}}
    run_min = max(1, int(round(0.050 * fs / w)))
    for name in ("L", "R"):
        values = db[name]
        floor = float(np.median(values[outside])) if np.count_nonzero(outside) >= 25 \
            else float(np.percentile(values, 20))
        plateau_mask = (centers >= segment_offset + 0.2 * seg_len) & \
                       (centers < segment_offset + 0.8 * seg_len)
        plateau = float(np.median(values[plateau_mask]))
        threshold = max(floor + 12.0, plateau - 20.0)
        active = values > threshold
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
                "error": "no sustained 997-Hz region",
                "floor_997_dbfs": round(floor, 2),
                "plateau_997_dbfs": round(plateau, 2),
                "threshold_997_dbfs": round(threshold, 2),
            }
            continue

        between = active[first:last + 1]
        gaps, current_gap = [], 0
        for value in between:
            if not value:
                current_gap += 1
            elif current_gap:
                gaps.append(current_gap)
                current_gap = 0
        if current_gap:
            gaps.append(current_gap)

        start_s = first * w / fs
        end_s = (last + 1) * w / fs
        window_start = segment_offset / fs + settle_s
        window_end = window_start + window_s
        result["channels"][name] = {
            "floor_997_dbfs": round(floor, 2),
            "plateau_997_dbfs": round(plateau, 2),
            "threshold_997_dbfs": round(threshold, 2),
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

    # A single reliable preflight before ABBA. This preserves the real
    # preflight→THD state transition without repeating the questionable -72
    # pilot before every arm.
    preflight_rig = m.Rig(backend, cfg, fs=FS, edge_protect=False)
    try:
        clock = m.clock_check(preflight_rig, level_dbfs=-30.0)
    except Exception as exc:
        clock = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    print(f"initial clock_check -30: {clock.get('ok', 'exception')}", flush=True)

    # Use the measured DAC/ADC drift to tune the narrowband 2-ms lock-in.
    ppm_values = []
    if isinstance(clock, dict) and isinstance(clock.get("ch"), dict):
        ppm_values = [
            float(v["ppm"]) for v in clock["ch"].values()
            if isinstance(v, dict) and np.isfinite(float(v.get("ppm", float("nan"))))
        ]
    ppm = float(np.median(ppm_values)) if ppm_values else -23.5
    tone_f = 997.0 * (1.0 + ppm * 1e-6)

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
            "clock_preflight_before_abba": clock,
            "tone_frequency_for_boundary_hz": tone_f,
        }

        window_path = OUT / f"{TAG}-{label}-window.wav"
        thd = None
        with MarkedCaptureCtx(backend, rig, FS) as ctx:
            try:
                thd = m.thd_point(
                    rig, f=997.0, level_dbfs=-30.0, capture_wav=str(window_path)
                )
                row["thd"] = thd
            except Exception as exc:
                # Preserve the full capture even if the OFF arm fails.
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

            seg_len = ctx.seg_len
            lead_n = ctx.lead_n
            marker_a_pos = lead_n
            marker_b_pos = lead_n + seg_len - len(ctx.marker_b)
            binding = {}
            offsets_a = {}
            offsets_b = {}

            for c, name in ((0, "L"), (1, "R")):
                a_off, a_score, a_second = normalized_xcorr_match(
                    capture[:, c], ctx.marker_a_mono, FS
                )
                b_off, b_score, b_second = normalized_xcorr_match(
                    capture[:, c], ctx.marker_b_mono, FS
                )
                a_gap = a_score - a_second
                b_gap = b_score - b_second
                a_valid = a_score >= 0.50 and a_gap >= 0.05
                b_valid = b_score >= 0.75 and b_gap >= 0.05
                offset_a = a_off - marker_a_pos
                offset_b = b_off - marker_b_pos
                if a_valid:
                    offsets_a[name] = offset_a
                if b_valid:
                    offsets_b[name] = offset_b
                binding[name] = {
                    "marker_a_offset_s": round(a_off / FS, 6),
                    "marker_a_score": round(a_score, 6),
                    "marker_a_competitor": round(a_second, 6),
                    "marker_a_peak_gap": round(a_gap, 6),
                    "marker_a_valid": bool(a_valid),
                    "program_offset_from_a_s": round(offset_a / FS, 6),
                    "marker_b_offset_s": round(b_off / FS, 6),
                    "marker_b_score": round(b_score, 6),
                    "marker_b_competitor": round(b_second, 6),
                    "marker_b_peak_gap": round(b_gap, 6),
                    "marker_b_valid": bool(b_valid),
                    "program_offset_from_b_s": round(offset_b / FS, 6),
                }

            row["unique_marker_binding"] = binding
            common_offsets = [
                v for name in ("L", "R")
                for v in (offsets_a.get(name), offsets_b.get(name))
                if v is not None
            ]
            if not common_offsets:
                row["capture_analysis_error"] = "no unique chirp marker matched"
            else:
                program_offset = int(round(float(np.median(common_offsets))))
                row["program_offset_s"] = round(program_offset / FS, 6)
                row["marker_anchor_disagreement_ms"] = {
                    name: round(abs(offsets_a[name] - offsets_b[name]) * 1000.0 / FS, 3)
                    for name in ("L", "R") if name in offsets_a and name in offsets_b
                }
                window_s = rig.blocks * rig.n / FS
                profile = capture_tone_profile(
                    capture, program_offset + lead_n, seg_len, FS,
                    tone_f, rig.settle, window_s,
                )
                row["capture_analysis"] = profile

                for name in ("L", "R"):
                    info = profile.get("channels", {}).get(name, {})
                    if not info or "tone_start_s" not in info:
                        continue
                    if name in offsets_b:
                        expected_start = (offsets_b[name] + lead_n) / FS
                        info["head_loss_ms_estimate"] = round(
                            (info["tone_start_s"] - expected_start) * 1000.0, 3
                        )
                        info["head_anchor"] = "marker_B"
                    else:
                        info["head_loss_ms_estimate"] = None
                        info["head_anchor"] = None
                    if name in offsets_a:
                        expected_end = (
                            offsets_a[name] + lead_n + seg_len - len(ctx.marker_b)
                        ) / FS
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
                    name: {
                        "thdn_pct": thd["ch"][name].get("thdn_pct"),
                        "thd_pct": thd["ch"][name].get("thd_pct"),
                        "thd_upper_pct": thd["ch"][name].get("thd_upper_pct"),
                        "level_dbfs": thd["ch"][name].get("level_dbfs"),
                        "noise_dbfs": thd["ch"][name].get("noise_dbfs"),
                    }
                    for name in ("L", "R") if name in thd.get("ch", {})
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
        "order": [name for name, _ in CASES],
        "notes": [
            "ABBA order: OFF, ON, ON, OFF.",
            "One real clock_check(-30 dBFS) precedes the four thd_point arms.",
            "No extra lead/post-roll duration is added by timing markers.",
            "Marker A is superimposed on the initial 80 ms; marker B replaces the final 10 ms, outside the 2-s measurement window.",
            "Absolute timing uses non-periodic chirps with peak-uniqueness checks, not the periodic 997-Hz tone.",
            "2-ms boundary uncertainty applies; sub-2-ms head/tail values are estimates only.",
            "Tone-selective 2-ms LSQ rejects the high-frequency markers and reports internal dropouts.",
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
