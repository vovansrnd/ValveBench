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
                         fs: int, f_tone: float, window_start_s: float | None,
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
        window_start = window_start_s
        window_end = None if window_start is None else window_start + window_s
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
            "window_start_s": None if window_start is None else round(window_start, 6),
            "window_end_s": None if window_end is None else round(window_end, 6),
            "window_inside_detected_tone": (
                None if window_start is None else bool(start_s <= window_start and end_s >= window_end)
            ),
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

        # Match the actual clock-sensitive GUI job cadence for EVERY arm:
        # clock_check on the same Rig, immediately followed by thd_point.
        try:
            clock = m.clock_check(rig, level_dbfs=-30.0)
            row["clock_check_minus30"] = clock
            ppm_values = [
                float(v["ppm"]) for v in clock["ch"].values()
                if np.isfinite(float(v.get("ppm", float("nan"))))
            ]
            ppm = float(np.median(ppm_values)) if ppm_values else -23.5
            print(
                f"clock_check -30: ok={clock.get('ok')} "
                f"ppm={ {c: round(clock['ch'][c]['ppm'], 2) for c in clock['ch']} }",
                flush=True,
            )
        except Exception as exc:
            row["clock_check_minus30"] = {
                "error": f"{type(exc).__name__}: {str(exc)[:160]}"
            }
            ppm = -23.5
            print(f"clock_check -30 ERROR: {row['clock_check_minus30']['error']}", flush=True)
        tone_f = 997.0 * (1.0 + ppm * 1e-6)
        row["tone_frequency_for_boundary_hz"] = tone_f

        window_path = OUT / f"{TAG}-{label}-window.wav"
        thd = None
        onset_trace = {}
        original_onset = rig._onset

        def traced_onset(rec, tone_hz=None):
            d = original_onset(rec, tone_hz=tone_hz)
            onset_trace["offset_frames"] = int(d)
            return d

        # Observe the exact onset index used by Rig.run to slice the production
        # window. This avoids using capture_wav autocorrelation as a fake
        # independent time anchor.
        rig._onset = traced_onset
        try:
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
        finally:
            rig._onset = original_onset

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
            anchor_quality = {}
            channel_offsets = {}
            for name in ("L", "R"):
                has_a = name in offsets_a
                has_b = name in offsets_b
                if has_a and has_b:
                    disagreement_ms = abs(offsets_a[name] - offsets_b[name]) * 1000.0 / FS
                    valid = disagreement_ms <= 2.0
                    chosen_offset = int(round((offsets_a[name] + offsets_b[name]) / 2))
                    anchor_quality[name] = {
                        "valid": bool(valid),
                        "source": "A+B" if valid else "A/B disagree",
                        "A_B_disagreement_ms": round(disagreement_ms, 3),
                    }
                    if valid:
                        channel_offsets[name] = chosen_offset
                elif has_a:
                    anchor_quality[name] = {
                        "valid": True,
                        "source": "A only (B marker absent or invalid)",
                        "A_B_disagreement_ms": None,
                    }
                    channel_offsets[name] = offsets_a[name]
                elif has_b:
                    anchor_quality[name] = {
                        "valid": True,
                        "source": "B only (A marker absent or invalid)",
                        "A_B_disagreement_ms": None,
                    }
                    channel_offsets[name] = offsets_b[name]
                else:
                    anchor_quality[name] = {
                        "valid": False,
                        "source": "no unique marker",
                        "A_B_disagreement_ms": None,
                    }

            lr_offset_ms = (
                abs(channel_offsets["L"] - channel_offsets["R"]) * 1000.0 / FS
                if "L" in channel_offsets and "R" in channel_offsets else None
            )
            anchors_valid = (
                len(channel_offsets) == 2
                and all(anchor_quality[ch]["valid"] for ch in ("L", "R"))
                and lr_offset_ms is not None and lr_offset_ms <= 2.0
            )
            row["program_alignment_quality"] = {
                "valid": bool(anchors_valid),
                "L_R_program_offset_disagreement_ms": (
                    None if lr_offset_ms is None else round(lr_offset_ms, 3)
                ),
                "tolerance_ms": 2.0,
                "per_channel": anchor_quality,
            }

            if not anchors_valid:
                row["capture_analysis_error"] = (
                    "no mutually consistent absolute program anchor within 2 ms; "
                    "do not report absolute tone-loss bounds"
                )
            else:
                program_offset_by_channel = {
                    name: channel_offsets[name] for name in ("L", "R")
                }
                program_offset = int(round(float(np.median(list(channel_offsets.values())))))
                row["program_offset_s"] = round(program_offset / FS, 6)
                row["channel_program_offsets_s"] = {
                    name: round(program_offset_by_channel[name] / FS, 6)
                    for name in ("L", "R")
                }

                # The actual analysis-window index comes directly from the
                # production Rig._onset call observed above, plus the segment's
                # unchanged settle offset. Verify the saved WAV equals that slice.
                window_start_s = None
                window_start_frames = None
                window_len_frames = rig.blocks * rig.n
                if "offset_frames" in onset_trace:
                    window_start_frames = (
                        onset_trace["offset_frames"] + int(round(rig.settle * FS))
                    )
                    window_start_s = window_start_frames / FS
                    row["window_position"] = {
                        "source": "traced production Rig._onset + segment settle",
                        "onset_offset_frames": onset_trace["offset_frames"],
                        "window_start_frames": window_start_frames,
                        "window_length_frames": window_len_frames,
                        "start_s": round(window_start_s, 6),
                        "end_s": round((window_start_frames + window_len_frames) / FS, 6),
                    }
                    if window_path.exists():
                        _, window_samples = wavfile.read(str(window_path))
                        expected_slice = capture[
                            window_start_frames:window_start_frames + window_len_frames
                        ]
                        if len(expected_slice) == len(window_samples):
                            delta = expected_slice.astype(np.float64) - window_samples.astype(np.float64)
                            row["window_slice_verification"] = {
                                "frames_equal": len(expected_slice) == window_len_frames,
                                "max_abs_difference": float(np.max(np.abs(delta))) if delta.size else 0.0,
                                "rms_difference": float(np.sqrt(np.mean(delta * delta))) if delta.size else 0.0,
                                "note": "capture_wav is checked against the exact sample slice selected by Rig.run",
                            }
                        else:
                            row["window_slice_verification"] = {
                                "frames_equal": False,
                                "capture_slice_frames": len(expected_slice),
                                "window_wav_frames": len(window_samples),
                            }
                else:
                    row["window_position"] = {
                        "note": "Rig.run did not return an onset index; actual window position unavailable",
                    }


                window_s = rig.blocks * rig.n / FS
                profile = capture_tone_profile(
                    capture, program_offset + lead_n, seg_len, FS,
                    tone_f, window_start_s, window_s,
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
                    elif name in offsets_a:
                        expected_start = (offsets_a[name] + lead_n) / FS
                        info["head_loss_ms_estimate"] = round(
                            (info["tone_start_s"] - expected_start) * 1000.0, 3
                        )
                        info["head_anchor"] = "marker_A + known segment start"
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
                        info["tail_anchor"] = "marker_A + known segment duration"
                    elif name in offsets_b:
                        expected_end = (
                            offsets_b[name] + lead_n + seg_len - len(ctx.marker_b)
                        ) / FS
                        info["tail_loss_ms_estimate"] = round(
                            (expected_end - info["tone_end_s"]) * 1000.0, 3
                        )
                        info["tail_anchor"] = "marker_B + known segment duration"
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
            "Each ABBA arm performs clock_check(-30 dBFS) on the same Rig immediately before thd_point, matching GUI job cadence.",
            "No extra lead/post-roll duration is added by timing markers.",
            "Marker A is superimposed on the initial 80 ms; marker B replaces the final 10 ms, outside the 2-s measurement window.",
            "Absolute timing uses non-periodic chirps; both anchors and L/R offsets must agree within 2 ms or absolute loss estimates are withheld.",
            "The actual analysis-window position is traced from Rig._onset + the production settle offset, then verified against the exact full-capture slice.",
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
