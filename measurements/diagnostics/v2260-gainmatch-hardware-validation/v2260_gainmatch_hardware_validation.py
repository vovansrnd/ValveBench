#!/usr/bin/env python3
"""v2.26.0: real D10 -> E-MU end-to-end Gain Match validation.

The harness only wraps the backend's submitted playback array in the D10
edge guards established by historical hardware tests. It does not change the
production package, ALSA mixer, persistent calibration, or physical wiring.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from pathlib import Path

import numpy as np
import scipy

FS = 192000
FREQUENCIES = (94.0, 994.0, 9994.0)
LEVEL_DBFS = -30.0
INJECT_DB = 20.0 * np.log10(2.0)  # exact half-amplitude: 6.020599913 dB
LEAD_SECONDS = 1.5
TAIL_SECONDS = 0.6
CAPTURE_TAIL_SECONDS = 0.8
RESULT_TOLERANCE_DB = 0.20


def inject_channel_attenuation(signal: np.ndarray, channel: str | None,
                                attenuation_db: float = INJECT_DB) -> np.ndarray:
    """Return a stereo copy with known attenuation on one logical channel."""
    x = np.asarray(signal, dtype=np.float64)
    if x.ndim != 2 or x.shape[1] != 2:
        raise ValueError(f"expected stereo (frames, 2) signal, got {x.shape}")
    if not np.all(np.isfinite(x)):
        raise ValueError("signal contains non-finite samples")
    out = x.copy()
    if channel is not None:
        if channel not in ("L", "R"):
            raise ValueError("channel must be L, R or None")
        if not np.isfinite(attenuation_db) or not 0.0 < attenuation_db < 60.0:
            raise ValueError("attenuation must be finite and between 0 and 60 dB")
        idx = 0 if channel == "L" else 1
        out[:, idx] *= 10.0 ** (-float(attenuation_db) / 20.0)
    return out


def add_d10_edge_guards(signal: np.ndarray, fs: int = FS,
                        lead_seconds: float = LEAD_SECONDS,
                        tail_seconds: float = TAIL_SECONDS) -> np.ndarray:
    """Add known-safe digital-zero lead/tail without changing the source."""
    x = np.asarray(signal, dtype=np.float64)
    if x.ndim != 2 or x.shape[1] != 2:
        raise ValueError(f"expected stereo (frames, 2) signal, got {x.shape}")
    if not np.all(np.isfinite(x)):
        raise ValueError("signal contains non-finite samples")
    lead_n = int(round(float(lead_seconds) * int(fs)))
    tail_n = int(round(float(tail_seconds) * int(fs)))
    if lead_n < 0 or tail_n < 0:
        raise ValueError("guard durations cannot be negative")
    return np.concatenate((np.zeros((lead_n, 2), dtype=np.float64), x,
                           np.zeros((tail_n, 2), dtype=np.float64)), axis=0)


def assess_series(result: dict, expected_delta_db: float | None) -> list[str]:
    """Strict validation of production Gain Match output; empty list means pass."""
    failures: list[str] = []
    events = result.get("events") or {}
    if events.get("fatal"):
        failures.append("fatal hardware events: " + "; ".join(map(str, events["fatal"])))
    rows = result.get("rows") or []
    if len(rows) != len(FREQUENCIES):
        failures.append(f"expected {len(FREQUENCIES)} result rows, got {len(rows)}")
    for row in rows:
        f = float(row.get("frequency_hz", row.get("requested_frequency_hz", float("nan"))))
        label = f"{f:g} Hz"
        if row.get("valid") is not True:
            failures.append(f"{label}: row invalid ({row.get('validity_reasons')})")
        if row.get("repeat_stable") is not True:
            failures.append(f"{label}: repeatability not confirmed")
        if expected_delta_db is not None:
            measured = row.get("delta_gain_db")
            if measured is None or not np.isfinite(float(measured)):
                failures.append(f"{label}: corrected ΔG is missing/non-finite")
            elif abs(float(measured) - float(expected_delta_db)) > RESULT_TOLERANCE_DB:
                failures.append(
                    f"{label}: corrected ΔG={float(measured):+.3f} dB; "
                    f"expected {float(expected_delta_db):+.3f} ±{RESULT_TOLERANCE_DB:.2f} dB")
    return failures


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist())
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return f if np.isfinite(f) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2,
                               allow_nan=False) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _git_head() -> str | None:
    import subprocess
    try:
        p = subprocess.run(["git", "rev-parse", "HEAD"], text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=3, check=False)
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _write_manifest(out_dir: Path) -> None:
    entries = {}
    for path in sorted(p for p in out_dir.rglob("*")
                       if p.is_file() and p.name != "SHA256SUMS.json"):
        entries[str(path.relative_to(out_dir))] = {
            "size_bytes": path.stat().st_size, "sha256": sha256(path)}
    write_json(out_dir / "SHA256SUMS.json",
               {"algorithm": "sha256", "files": entries})


def run(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=False)
    captures_dir = out_dir / "captures"
    captures_dir.mkdir()

    if args.fs != FS:
        raise ValueError(f"v2.26.0 is pinned to {FS} Hz; got {args.fs}")

    # Import the project only for execution; helper tests remain fully offline.
    from valvebench import __version__
    from valvebench.backend import AlsaCli, common_pcm_rates
    from valvebench.calib import Calibration
    from valvebench.gainmatch import build_loopback_reference, measure_series, text_series
    from valvebench.measure import Rig
    from valvebench.cli import split_clock_preflight

    common = common_pcm_rates(args.dac, args.adc)
    if FS not in common:
        raise ValueError(f"192000 Hz is not a declared common mode for {args.dac} → {args.adc}; "
                         f"common rates={common}")

    cal = Calibration()  # fresh relative-only calibration; never reads/writes user config
    backend = AlsaCli(hw_out=args.dac, hw_in=args.adc,
                      in_map=(0, 1), out_map=(0, 1), buffer_ms=100)
    backend.open(FS)
    if backend.ch_in < 2 or backend.ch_out < 2:
        raise RuntimeError(f"stereo PCM required; opened {backend.ch_out} playback / {backend.ch_in} capture")
    rig = Rig(backend, cal, fs=FS, df=1.0, blocks=2, settle=0.5)

    state = {"phase": "clock_preflight", "attenuation_channel": None,
             "attenuation_db": INJECT_DB, "capture_index": 0}
    original_play_record = backend.play_record

    def guarded_play_record(signal, tail=CAPTURE_TAIL_SECONDS):
        state["capture_index"] += 1
        submitted = inject_channel_attenuation(
            signal, state["attenuation_channel"], state["attenuation_db"])
        submitted = add_d10_edge_guards(submitted, fs=FS)
        captured = original_play_record(submitted, tail=tail)
        if not np.all(np.isfinite(captured)):
            raise RuntimeError(f"{state['phase']}: capture contains non-finite samples")
        name = f"{state['phase']}-{state['capture_index']:02d}.npz"
        np.savez_compressed(
            captures_dir / name,
            submitted_to_backend=submitted.astype(np.float32),
            capture=captured.astype(np.float32),
            fs=np.int32(FS),
            lead_seconds=np.float64(LEAD_SECONDS),
            tail_seconds=np.float64(TAIL_SECONDS),
            attenuation_channel=np.asarray(state["attenuation_channel"] or "none"),
            attenuation_db=np.float64(
                state["attenuation_db"] if state["attenuation_channel"] else 0.0))
        print(f"CAPTURE_SAVED: {captures_dir / name} "
              f"frames_out={len(submitted)} frames_in={len(captured)}")
        return captured

    # Instance attribute deliberately wraps the existing bound backend method;
    # device identity and mixer fingerprint remain those of the real AlsaCli.
    backend.play_record = guarded_play_record

    summary = {
        "stage": "v2.26.0",
        "status": "RUNNING",
        "host": platform.node(),
        "platform": platform.platform(),
        "python": sys.version,
        "numpy": np.__version__,
        "scipy": scipy.__version__,
        "valvebench_version": __version__,
        "git_head": _git_head(),
        "dac": args.dac,
        "adc": args.adc,
        "fs": FS,
        "playback_format": backend.fmt_out,
        "capture_format": backend.fmt_in,
        "playback_channels": backend.ch_out,
        "capture_channels": backend.ch_in,
        "channel_maps": {"out": list(backend.out_map), "in": list(backend.in_map)},
        "frequencies_hz": list(FREQUENCIES),
        "generator_level_dbfs": LEVEL_DBFS,
        "injected_attenuation_db": INJECT_DB,
        "edge_guard": {"lead_s": LEAD_SECONDS, "tail_s": TAIL_SECONDS,
                       "capture_tail_s": CAPTURE_TAIL_SECONDS},
        "measurement": {"blocks": rig.blocks, "settle_s": rig.settle,
                        "frequency_profile": "mains_avoidance_50", "repeats": 3},
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
            "warnings": preflight.get("warnings", [])}
        if not preflight.get("ok"):
            raise RuntimeError("clock preflight failed; no Gain Match series attempted")

        common_kwargs = dict(frequencies=FREQUENCIES, level_dbfs=LEVEL_DBFS,
                             repeats=3, frequency_profile="mains_avoidance_50")

        state["phase"] = "loopback"
        loopback = measure_series(
            rig, purpose="loopback",
            metadata={"stage": "v2.26.0", "role": "unattenuated DAC→ADC reference",
                      "edge_guard_lead_s": LEAD_SECONDS,
                      "edge_guard_tail_s": TAIL_SECONDS},
            **common_kwargs)
        write_json(out_dir / "loopback-series.json", loopback)
        loopback_failures = assess_series(loopback, expected_delta_db=None)
        if loopback_failures:
            raise RuntimeError("loopback series invalid: " + " | ".join(loopback_failures))
        reference = build_loopback_reference(loopback)
        write_json(out_dir / "loopback-reference.json", reference)
        summary["steps"]["loopback"] = {
            "passed": True,
            "rows": [{"frequency_hz": r["frequency_hz"],
                      "delta_adc_db": r["delta_adc_db"],
                      "repeat_spread_db": r["delta_output_spread_db"]}
                     for r in loopback["rows"]]}
        print(text_series(loopback))

        state["attenuation_channel"] = "R"
        state["phase"] = "inject_R"
        inject_r = measure_series(
            rig, purpose="dut", reference=reference,
            metadata={"stage": "v2.26.0", "injected_channel": "R",
                      "injected_attenuation_db": INJECT_DB},
            **common_kwargs)
        write_json(out_dir / "inject-R-series.json", inject_r)
        failures_r = assess_series(inject_r, expected_delta_db=INJECT_DB)
        summary["steps"]["inject_R"] = {
            "passed": not failures_r, "expected_delta_gain_db": INJECT_DB,
            "failures": failures_r,
            "rows": [{"frequency_hz": r["frequency_hz"],
                      "delta_adc_db": r["delta_adc_db"],
                      "correction_db": r["loopback_correction_db"],
                      "delta_gain_db": r["delta_gain_db"]}
                     for r in inject_r["rows"]]}
        print(text_series(inject_r))

        state["attenuation_channel"] = "L"
        state["phase"] = "inject_L"
        inject_l = measure_series(
            rig, purpose="dut", reference=reference,
            metadata={"stage": "v2.26.0", "injected_channel": "L",
                      "injected_attenuation_db": INJECT_DB},
            **common_kwargs)
        write_json(out_dir / "inject-L-series.json", inject_l)
        failures_l = assess_series(inject_l, expected_delta_db=-INJECT_DB)
        summary["steps"]["inject_L"] = {
            "passed": not failures_l, "expected_delta_gain_db": -INJECT_DB,
            "failures": failures_l,
            "rows": [{"frequency_hz": r["frequency_hz"],
                      "delta_adc_db": r["delta_adc_db"],
                      "correction_db": r["loopback_correction_db"],
                      "delta_gain_db": r["delta_gain_db"]}
                     for r in inject_l["rows"]]}
        print(text_series(inject_l))

        all_failures = failures_r + failures_l
        summary["status"] = "PASS" if not all_failures else "FAIL"
        summary["failures"] = all_failures
        summary["backend_notes_after"] = list(backend.notes)
        summary["mixer_fingerprint_available"] = bool(loopback.get("mixer_fingerprint"))
        write_json(out_dir / "run-summary.json", summary)
        print(f"V2260_STATUS={summary['status']}")
        return 0 if not all_failures else 2
    except Exception as exc:
        summary["status"] = "BLOCKED_OR_FAIL"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["backend_notes_after"] = list(backend.notes)
        write_json(out_dir / "run-summary.json", summary)
        print(f"V2260_STATUS=BLOCKED_OR_FAIL: {type(exc).__name__}: {exc}",
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
    parser.add_argument("--fs", type=int, default=FS,
                        help="pinned common sample rate (192000 only)")
    parser.add_argument("--out-dir", required=True,
                        help="new results directory inside the project checkout")
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as exc:
        print(f"V2260_SETUP_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
