#!/usr/bin/env python3
"""v2.13 — edge OFF/ON аппаратный A/B на D10→E-MU.

Цель: проверить, что 1.5 s lead + 0.6 s tail действительно улучшают
измерение по сравнению с исходным Rig.run без edge-защиты. Один и тот же
AlsaCli, GUI-дефолты blocks=2/settle=0.5, порядок OFF/ON/ON/OFF.
Каждый опыт: clock_check(-30 dBFS) -> настоящий thd_point(997 Hz, -30 dBFS).
Сохраняются программа, полный захват и production-окно. В привязке эталон —
поданный сегмент программы, не capture_wav.

Границы измеряются по 2-ms RMS-блокам. Помимо первой/последней активной
границы сохраняются количество пропущенных внутренних блоков и самый длинный
внутренний разрыв. Дробные head/tail числа не интерпретировать точнее
разрешения метода (2 ms).

Запуск на аппаратной машине:
    python3 verification/v213_edge_ab.py
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

from v211_full_capture_anchored import FullCaptureCtx, find_card, norm_xcorr
from valvebench import calib, measure as m
from valvebench.backend import AlsaCli

FS = 192000
OUT = ROOT / "measurements" / "diagnostics" / "v213-edge-ab"
TAG = datetime.now().strftime("%Y%m%d-%H%M%S")
# ABBA controls some slow drift / temperature / USB state changes.
CASES = (("OFF-1", False), ("ON-1", True), ("ON-2", True), ("OFF-2", False))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def capture_tone_profile(cap: np.ndarray, offset: int, seg_len: int, fs: int,
                         settle_s: float, block_ms: float = 2.0,
                         floor_margin_db: float = 12.0,
                         plateau_margin_db: float = 20.0) -> dict:
    """Measure tone bounds and internal gaps in 2-ms RMS blocks.

    Noise floor comes from capture outside the submitted segment; the signal
    reference comes from the segment's central 60 %. The threshold is the
    larger of floor+12 dB and plateau-20 dB. A sustained 50-ms run defines
    the first/last tone boundary. All inactive blocks between those boundaries
    are reported rather than silently bridged.
    """
    w = max(1, int(round(block_ms * 1e-3 * fs)))
    n = len(cap) // w
    if n < 8:
        return {"error": "capture too short", "resolution_ms": block_ms}
    x = np.asarray(cap[:n * w], dtype=np.float64).reshape(n, w, 2)
    rms = np.sqrt(np.mean(x * x, axis=1)) * np.sqrt(2.0)
    db = 20.0 * np.log10(np.maximum(rms, 1e-15))
    centers = (np.arange(n) + 0.5) * w

    seg_end = offset + seg_len
    outside = (centers < offset - w) | (centers >= seg_end + w)
    # Avoid classifying an empty/short margin as a meaningful floor.
    if np.count_nonzero(outside) < max(10, int(0.05 * fs / w)):
        floor_db = np.percentile(db, 20, axis=0)
        floor_source = "capture_p20_fallback"
    else:
        floor_db = np.median(db[outside], axis=0)
        floor_source = "outside_submitted_segment_median"

    plateau = (centers >= offset + 0.2 * seg_len) & (centers < offset + 0.8 * seg_len)
    if not np.any(plateau):
        return {"error": "no plateau bins", "resolution_ms": block_ms}

    result = {
        "resolution_ms": block_ms,
        "sustain_ms": 50.0,
        "floor_source": floor_source,
        "channels": {},
    }
    run_min = max(1, int(round(0.050 * fs / w)))
    expected = (centers >= offset) & (centers < seg_end)

    for c, name in ((0, "L"), (1, "R")):
        f_db = float(floor_db[c])
        ref_db = float(np.median(db[plateau, c]))
        threshold = max(f_db + floor_margin_db, ref_db - plateau_margin_db)
        active = db[:, c] > threshold
        inside_idx = np.where(expected)[0]
        mask = active[inside_idx]
        if not len(mask):
            result["channels"][name] = {
                "error": "submitted segment absent from capture",
                "floor_dbfs": f_db,
                "plateau_dbfs": ref_db,
                "threshold_dbfs": threshold,
            }
            continue

        # First sustained run (>=90 % active bins in 50 ms), searched within
        # the submitted segment; same criterion mirrored from the onset logic.
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
                "error": "no 50-ms sustained tone region",
                "floor_dbfs": f_db,
                "plateau_dbfs": ref_db,
                "threshold_dbfs": threshold,
            }
            continue

        between = active[first:last + 1]
        gaps = []
        run = 0
        for value in between:
            if not value:
                run += 1
            elif run:
                gaps.append(run)
                run = 0
        if run:
            gaps.append(run)

        start_s = first * w / fs
        end_s = (last + 1) * w / fs
        seg_start_s = offset / fs
        seg_end_s = seg_end / fs
        win_start_s = seg_start_s + settle_s
        win_end_s = win_start_s + 2.0  # GUI defaults: blocks=2, df=1 Hz

        result["channels"][name] = {
            "floor_dbfs": round(f_db, 2),
            "plateau_dbfs": round(ref_db, 2),
            "threshold_dbfs": round(threshold, 2),
            "tone_start_s": round(start_s, 6),
            "tone_end_s": round(end_s, 6),
            "tone_span_s": round(end_s - start_s, 6),
            "head_loss_ms_estimate": round(max(0.0, (start_s - seg_start_s) * 1000.0), 3),
            "tail_loss_ms_estimate": round(max(0.0, (seg_end_s - end_s) * 1000.0), 3),
            "boundary_uncertainty_ms": block_ms,
            "active_bins_inside_bounds": int(np.count_nonzero(between)),
            "bins_inside_bounds": int(len(between)),
            "inactive_bins_inside_bounds": int(len(between) - np.count_nonzero(between)),
            "max_internal_gap_ms": round(max(gaps, default=0) * w * 1000.0 / fs, 3),
            "window_start_s": round(win_start_s, 6),
            "window_end_s": round(win_end_s, 6),
            "window_inside_detected_tone": bool(start_s <= win_start_s and end_s >= win_end_s),
        }

    return result


def main() -> int:
    dac = find_card("152a:85dd")
    adc = find_card("041e:3f04")
    if dac is None or adc is None:
        raise SystemExit("D10/E-MU не найдены по USB VID:PID")
    OUT.mkdir(parents=True, exist_ok=True)

    cfg = calib.Calibration()
    be = AlsaCli(
        hw_out=f"hw:{dac},0",
        hw_in=f"hw:{adc},0",
        in_map=cfg["in_map"],
        out_map=cfg["out_map"],
    )
    be.open(FS)

    results = []
    for label, edge_on in CASES:
        rig = m.Rig(be, cfg, fs=FS, edge_protect=edge_on)
        print(
            f"\n=== {label}: edge_protect={edge_on}, "
            f"blocks={rig.blocks}, settle={rig.settle}, "
            f"lead={rig.edge_protect_lead_s}, tail={rig.edge_protect_tail_s} ===",
            flush=True,
        )
        row = {
            "label": label,
            "edge_protect": edge_on,
            "blocks": rig.blocks,
            "settle_s": rig.settle,
            "lead_s": rig.edge_protect_lead_s if edge_on else 0.0,
            "tail_s": rig.edge_protect_tail_s if edge_on else 0.0,
            "clock_check": None,
            "thd": None,
        }

        # Reliable diagnostic preflight is used identically in both arms.
        # The production default -72 dBFS was already shown unreliable;
        # it is intentionally not allowed to confound this edge A/B.
        try:
            ck = m.clock_check(rig, level_dbfs=-30.0)
            row["clock_check"] = ck
            print(
                f"clock_check -30: ok={ck.get('ok')} "
                f"ppm={ {c: round(ck['ch'][c]['ppm'], 2) for c in ck['ch']} }",
                flush=True,
            )
        except Exception as exc:
            row["clock_check"] = {
                "error": f"{type(exc).__name__}: {str(exc)[:160]}"
            }
            print(f"clock_check -30 ERROR: {row['clock_check']['error']}", flush=True)

        win_path = OUT / f"{TAG}-{label}-window.wav"
        thd = None
        with FullCaptureCtx(be) as ctx:
            try:
                thd = m.thd_point(
                    rig, f=997.0, level_dbfs=-30.0, capture_wav=str(win_path)
                )
                row["thd"] = thd
            except Exception as exc:
                # Keep the raw full capture even when Rig.run/onset/validation
                # rejects the measurement; a failed OFF arm is diagnostically
                # important and must not discard the only source evidence.
                row["measurement_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                print(f"thd_point ERROR: {row['measurement_error']}", flush=True)

        if len(ctx.pairs) != 1:
            row["capture_error"] = f"expected one play_record, got {len(ctx.pairs)}"
            print(f"capture ERROR: {row['capture_error']}", flush=True)
        else:
            prog, cap = ctx.pairs[0]
            seg_len = int(round((rig.settle + rig.blocks * rig.n / FS + 0.05) * FS))
            lead_n = int(round(rig.edge_protect_lead_s * FS)) if edge_on else 0

            prog_path = OUT / f"{TAG}-{label}-program.wav"
            cap_path = OUT / f"{TAG}-{label}-full-capture.wav"
            wavfile.write(prog_path, FS, np.asarray(prog, dtype=np.float32))
            wavfile.write(cap_path, FS, np.asarray(cap, dtype=np.float32))

            row["files"] = {}
            for key, path, frames in (
                ("program", prog_path, len(prog)),
                ("full_capture", cap_path, len(cap)),
                ("window", win_path, 2 * FS),
            ):
                row["files"][key] = {
                    "path": str(path),
                    "frames": int(frames),
                    "sha256": sha256_file(path) if path.exists() else None,
                }

            try:
                if lead_n + seg_len > len(prog):
                    raise RuntimeError("submitted segment bounds exceed program")
                segment = prog[lead_n:lead_n + seg_len]

                binding = {}
                for c, name in ((0, "L"), (1, "R")):
                    off, score = norm_xcorr(cap[:, c], segment[:, c])
                    binding[name] = {
                        "offset_frames": off,
                        "offset_s": round(off / FS, 6),
                        "score": round(score, 6),
                    }

                # Channel offsets should agree within a few frames. Use the mean
                # only for common window timing; retain both per-channel values.
                off_l = binding["L"]["offset_frames"]
                off_r = binding["R"]["offset_frames"]
                offset = int(round((off_l + off_r) / 2))
                row["program_binding"] = binding
                row["channel_offset_difference_ms"] = round(abs(off_l - off_r) * 1000 / FS, 3)
                row["capture_analysis"] = capture_tone_profile(
                    cap, offset, seg_len, FS, rig.settle
                )

                print(
                    f"program binding scores: "
                    f"L={binding['L']['score']:.4f}, R={binding['R']['score']:.4f}; "
                    f"offset Δ={row['channel_offset_difference_ms']:.3f} ms",
                    flush=True,
                )
                print(f"tone analysis: {row['capture_analysis']}", flush=True)
            except Exception as exc:
                row["capture_analysis_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                print(f"capture analysis ERROR: {row['capture_analysis_error']}", flush=True)

            if thd is not None:
                thd_ch = thd.get("ch", {})
                row["thd_summary"] = {
                    ch: {
                        "thdn_pct": thd_ch[ch].get("thdn_pct"),
                        "thd_pct": thd_ch[ch].get("thd_pct"),
                        "thd_upper_pct": thd_ch[ch].get("thd_upper_pct"),
                        "level_dbfs": thd_ch[ch].get("level_dbfs"),
                        "noise_dbfs": thd_ch[ch].get("noise_dbfs"),
                    }
                    for ch in ("L", "R") if ch in thd_ch
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
            "ABBA: OFF, ON, ON, OFF",
            "Each arm uses clock_check(-30 dBFS) then real thd_point(997 Hz, -30 dBFS).",
            "The unstable production preflight level -72 dBFS is intentionally excluded from this controlled edge A/B.",
            "Program-to-capture binding uses the submitted tone segment, not capture_wav.",
            "Boundary uncertainty is 2 ms; sub-2-ms head/tail estimates are not independently resolved.",
            "Internal inactive bins and maximum internal gap are reported separately.",
        ],
        "results": results,
    }
    json_path = OUT / f"{TAG}-results.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nJSON: {json_path}", flush=True)

    close = getattr(be, "close", None)
    if callable(close):
        close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
