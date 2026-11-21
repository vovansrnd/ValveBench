#!/usr/bin/env python3
"""v2.26.1: level-margin validation of physical D10 -> E-MU Gain Match.

Reuses the v2.26.0 production-path wrapper and acceptance gate. No production
code, persistent calibration, ALSA controls or cabling are modified.
"""
from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

import numpy as np
import scipy

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from v2260_gainmatch_hardware_validation import (  # noqa: E402
    FS, FREQUENCIES, INJECT_DB, LEAD_SECONDS, TAIL_SECONDS,
    CAPTURE_TAIL_SECONDS, assess_series, add_d10_edge_guards,
    inject_channel_attenuation, write_json, _write_manifest,
)

LEVELS_DBFS = (-24.0, -18.0)
FREQUENCY_PROFILE = "mains_avoidance_50"
REPEATS = 3
BLOCKS = 2
SETTLE_SECONDS = 0.5
RESULT_TOLERANCE_DB = 0.20
MIN_CARRIER_RESIDUAL_DB = 30.0


def _level_tag(level: float) -> str:
    return "m" + str(int(abs(level)))


def _series_metrics(result: dict) -> dict:
    carrier = []
    sinad = []
    peaks = []
    invalid = []
    for row in result.get("rows", []):
        for channel in ("L", "R"):
            channel_result = row.get("channels", {}).get(channel, {})
            for repeat in channel_result.get("repeats", []):
                for key, target in (("carrier_residual_db", carrier),
                                    ("sinad_db", sinad),
                                    ("peak_dbfs", peaks)):
                    value = repeat.get(key)
                    if value is not None and np.isfinite(float(value)):
                        target.append(float(value))
                if repeat.get("valid") is not True:
                    invalid.append({
                        "frequency_hz": row.get("frequency_hz"),
                        "channel": channel,
                        "repeat": repeat.get("repeat"),
                        "reasons": repeat.get("validity_reasons", []),
                        "carrier_residual_db": repeat.get("carrier_residual_db"),
                        "sinad_db": repeat.get("sinad_db"),
                        "envelope": repeat.get("envelope"),
                    })
    return {
        "min_carrier_residual_db": min(carrier) if carrier else None,
        "min_sinad_db": min(sinad) if sinad else None,
        "max_peak_dbfs": max(peaks) if peaks else None,
        "min_peak_headroom_db": -max(peaks) if peaks else None,
        "invalid_repeats": invalid,
    }


def _summarize_level(loopback: dict, inject_r: dict | None,
                     inject_l: dict | None, loopback_failures: list[str],
                     failures_r: list[str] | None,
                     failures_l: list[str] | None) -> dict:
    """Level PASS requires loopback + both polarities to meet unchanged QC."""
    all_failures = list(loopback_failures)
    if failures_r is not None:
        all_failures.extend("inject-R: " + item for item in failures_r)
    if failures_l is not None:
        all_failures.extend("inject-L: " + item for item in failures_l)

    return {
        "passed": bool(not all_failures and inject_r is not None and inject_l is not None),
        "failures": all_failures,
        "loopback": {
            "mixer_fingerprint_available": bool(loopback.get("mixer_fingerprint")),
            "failures": loopback_failures,
            "metrics": _series_metrics(loopback),
            "rows": [
                {"frequency_hz": row.get("frequency_hz"),
                 "delta_adc_db": row.get("delta_adc_db"),
                 "repeat_spread_db": row.get("delta_output_spread_db"),
                 "valid": row.get("valid"),
                 "repeat_stable": row.get("repeat_stable")}
                for row in loopback.get("rows", [])],
        },
        "inject_R": None if inject_r is None else {
            "expected_delta_gain_db": INJECT_DB,
            "failures": failures_r or [],
            "metrics": _series_metrics(inject_r),
            "rows": [
                {"frequency_hz": row.get("frequency_hz"),
                 "delta_adc_db": row.get("delta_adc_db"),
                 "correction_db": row.get("loopback_correction_db"),
                 "delta_gain_db": row.get("delta_gain_db"),
                 "repeat_spread_db": row.get("delta_output_spread_db"),
                 "valid": row.get("valid"),
                 "repeat_stable": row.get("repeat_stable")}
                for row in inject_r.get("rows", [])]},
        "inject_L": None if inject_l is None else {
            "expected_delta_gain_db": -INJECT_DB,
            "failures": failures_l or [],
            "metrics": _series_metrics(inject_l),
            "rows": [
                {"frequency_hz": row.get("frequency_hz"),
                 "delta_adc_db": row.get("delta_adc_db"),
                 "correction_db": row.get("loopback_correction_db"),
                 "delta_gain_db": row.get("delta_gain_db"),
                 "repeat_spread_db": row.get("delta_output_spread_db"),
                 "valid": row.get("valid"),
                 "repeat_stable": row.get("repeat_stable")}
                for row in inject_l.get("rows", [])]},
    }


def run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=False)
    captures_dir = out_dir / "captures"
    captures_dir.mkdir()

    if args.fs != FS:
        raise ValueError(f"v2.26.1 is pinned to {FS} Hz; got {args.fs}")

    from valvebench import __version__
    from valvebench.backend import AlsaCli, common_pcm_rates
    from valvebench.calib import Calibration
    from valvebench.cli import split_clock_preflight
    from valvebench.gainmatch import build_loopback_reference, measure_series, text_series
    from valvebench.measure import Rig

    common = common_pcm_rates(args.dac, args.adc)
    if FS not in common:
        raise ValueError(f"192000 Hz is not a declared common mode for {args.dac} → {args.adc}; "
                         f"common rates={common}")

    cal = Calibration()  # relative-only; does not read or write the user's config
    backend = AlsaCli(hw_out=args.dac, hw_in=args.adc,
                      in_map=(0, 1), out_map=(0, 1), buffer_ms=100)
    backend.open(FS)
    if backend.ch_in < 2 or backend.ch_out < 2:
        raise RuntimeError(f"stereo PCM required, opened {backend.ch_out} playback / {backend.ch_in} capture")
    rig = Rig(backend, cal, fs=FS, df=1.0, blocks=BLOCKS, settle=SETTLE_SECONDS)

    state = {"phase": "clock_preflight", "level_dbfs": None,
             "attenuation_channel": None, "capture_index": 0}
    original_play_record = backend.play_record

    def guarded_play_record(signal, tail=CAPTURE_TAIL_SECONDS):
        state["capture_index"] += 1
        submitted = inject_channel_attenuation(
            signal, state["attenuation_channel"], INJECT_DB)
        submitted = add_d10_edge_guards(submitted, fs=FS)
        captured = original_play_record(submitted, tail=tail)
        if not np.all(np.isfinite(captured)):
            raise RuntimeError(f"{state['phase']}: capture contains non-finite samples")
        level_tag = "preflight" if state["level_dbfs"] is None else _level_tag(state["level_dbfs"])
        channel_tag = state["attenuation_channel"] or "none"
        name = (f"{level_tag}-{state['phase']}-{channel_tag}-"
                f"{state['capture_index']:02d}.npz")
        np.savez_compressed(
            captures_dir / name,
            submitted_to_backend=submitted.astype(np.float32),
            capture=captured.astype(np.float32),
            fs=np.int32(FS),
            lead_seconds=np.float64(LEAD_SECONDS),
            tail_seconds=np.float64(TAIL_SECONDS),
            attenuation_channel=np.asarray(state["attenuation_channel"] or "none"),
            attenuation_db=np.float64(INJECT_DB if state["attenuation_channel"] else 0.0),
            generator_level_dbfs=np.float64(
                np.nan if state["level_dbfs"] is None else state["level_dbfs"]))
        print(f"CAPTURE_SAVED: {captures_dir / name} "
              f"frames_out={len(submitted)} frames_in={len(captured)}")
        return captured

    backend.play_record = guarded_play_record
    summary = {
        "stage": "v2.26.1",
        "status": "RUNNING",
        "host": platform.node(),
        "platform": platform.platform(),
        "python": sys.version,
        "numpy": np.__version__,
        "scipy": scipy.__version__,
        "valvebench_version": __version__,
        "git_head": None,
        "dac": args.dac,
        "adc": args.adc,
        "fs": FS,
        "playback_format": backend.fmt_out,
        "capture_format": backend.fmt_in,
        "playback_channels": backend.ch_out,
        "capture_channels": backend.ch_in,
        "channel_maps": {"out": list(backend.out_map), "in": list(backend.in_map)},
        "frequencies_hz": list(FREQUENCIES),
        "levels_dbfs": list(LEVELS_DBFS),
        "injected_attenuation_db": INJECT_DB,
        "edge_guard": {"lead_s": LEAD_SECONDS, "tail_s": TAIL_SECONDS,
                       "capture_tail_s": CAPTURE_TAIL_SECONDS},
        "measurement": {"blocks": BLOCKS, "settle_s": SETTLE_SECONDS,
                        "frequency_profile": FREQUENCY_PROFILE, "repeats": REPEATS},
        "production_qc": {"carrier_residual_min_db": MIN_CARRIER_RESIDUAL_DB,
                          "corrected_delta_tolerance_db": RESULT_TOLERANCE_DB,
                          "production_code_modified": False},
        "backend_notes_before": list(backend.notes),
        "steps": {},
    }
    write_json(out_dir / "run-summary.json", summary)

    try:
        preflight = split_clock_preflight(rig, args.dac, args.adc)
        write_json(out_dir / "clock-preflight.json", preflight)
        summary["steps"]["clock_preflight"] = {
            "ok": bool(preflight.get("ok")),
            "test_level_dbfs": preflight.get("test_level_dbfs"),
            "channels": preflight.get("ch"),
            "warnings": preflight.get("warnings", []),
        }
        if not preflight.get("ok"):
            raise RuntimeError("clock preflight failed; no Gain Match level series attempted")

        kwargs_base = dict(frequencies=FREQUENCIES, repeats=REPEATS,
                           frequency_profile=FREQUENCY_PROFILE)
        any_level_pass = False

        for level in LEVELS_DBFS:
            tag = _level_tag(level)
            level_dir = out_dir / f"level_{tag}"
            level_dir.mkdir()
            state["level_dbfs"] = level
            kwargs = dict(kwargs_base, level_dbfs=level)
            print(f"\n===== GAIN MATCH LEVEL {level:.1f} dBFS =====")

            state["phase"] = "loopback"
            state["attenuation_channel"] = None
            loopback = measure_series(
                rig, purpose="loopback",
                metadata={"stage": "v2.26.1", "level_dbfs": level,
                          "role": "unattenuated DAC→ADC reference",
                          "edge_guard_lead_s": LEAD_SECONDS,
                          "edge_guard_tail_s": TAIL_SECONDS},
                **kwargs)
            write_json(level_dir / "loopback-series.json", loopback)
            loopback_failures = assess_series(loopback, expected_delta_db=None)
            inject_r = inject_l = None
            failures_r = failures_l = None

            if not loopback_failures:
                reference = build_loopback_reference(loopback)
                write_json(level_dir / "loopback-reference.json", reference)
                print(text_series(loopback))

                state["attenuation_channel"] = "R"
                state["phase"] = "inject_R"
                inject_r = measure_series(
                    rig, purpose="dut", reference=reference,
                    metadata={"stage": "v2.26.1", "level_dbfs": level,
                              "injected_channel": "R",
                              "injected_attenuation_db": INJECT_DB},
                    **kwargs)
                write_json(level_dir / "inject-R-series.json", inject_r)
                failures_r = assess_series(inject_r, expected_delta_db=INJECT_DB)
                print(text_series(inject_r))

                state["attenuation_channel"] = "L"
                state["phase"] = "inject_L"
                inject_l = measure_series(
                    rig, purpose="dut", reference=reference,
                    metadata={"stage": "v2.26.1", "level_dbfs": level,
                              "injected_channel": "L",
                              "injected_attenuation_db": INJECT_DB},
                    **kwargs)
                write_json(level_dir / "inject-L-series.json", inject_l)
                failures_l = assess_series(inject_l, expected_delta_db=-INJECT_DB)
                print(text_series(inject_l))
            else:
                print(f"LEVEL {level:.1f} BLOCKED: loopback reference invalid: "
                      + " | ".join(loopback_failures))

            level_result = _summarize_level(
                loopback, inject_r, inject_l, loopback_failures,
                failures_r, failures_l)
            summary["steps"][tag] = level_result
            summary["steps"][tag]["level_dbfs"] = level
            if level_result["passed"]:
                any_level_pass = True

        summary["status"] = "PASS" if any_level_pass else "FAIL"
        summary["any_level_passed"] = any_level_pass
        summary["lowest_passing_test_level_dbfs"] = next(
            (level for level in LEVELS_DBFS
             if summary["steps"][_level_tag(level)]["passed"]), None)
        summary["backend_notes_after"] = list(backend.notes)
        summary["mixer_fingerprint_available"] = any(
            item.get("loopback", {}).get("mixer_fingerprint_available", False)
            for item in summary["steps"].values()
        )
        write_json(out_dir / "run-summary.json", summary)
        print(f"V2261_STATUS={summary['status']}")
        return 0 if any_level_pass else 2

    except Exception as exc:
        summary["status"] = "BLOCKED_OR_FAIL"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["backend_notes_after"] = list(backend.notes)
        write_json(out_dir / "run-summary.json", summary)
        print(f"V2261_STATUS=BLOCKED_OR_FAIL: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2
    finally:
        backend.play_record = original_play_record
        _write_manifest(out_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dac", required=True,
                        help="current numeric ALSA playback endpoint, e.g. hw:1,0")
    parser.add_argument("--adc", required=True,
                        help="current numeric ALSA capture endpoint, e.g. hw:2,0")
    parser.add_argument("--fs", type=int, default=FS)
    parser.add_argument("--out-dir", required=True,
                        help="new results directory inside the project checkout")
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as exc:
        print(f"V2261_SETUP_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
