"""Strict comparison of complete, saved DUT measurement sessions.

The JSON session is authoritative. Comparisons retain L/R values and add a
separate two-channel aggregate; no channel is hidden by the aggregate.
"""
from __future__ import annotations

import csv
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np

FREQUENCY_POINTS_HZ = (
    20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0,
    200.0, 315.0, 500.0, 1000.0, 2000.0, 3150.0, 5000.0, 8000.0,
    10000.0, 12500.0, 16000.0, 20000.0,
)


class ComparisonError(ValueError):
    """The supplied DUT sessions cannot be compared safely."""


def _read_session(path):
    source = Path(path).expanduser()
    if source.is_dir():
        source = source / "session.json"
    if not source.is_file():
        raise ComparisonError(f"Файл сессии не найден: {source}")
    try:
        with source.open("r", encoding="utf-8") as fh:
            envelope = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ComparisonError(f"Не удалось прочитать {source}: {exc}") from exc
    if envelope.get("format") != "valvebench-device-measurement" or envelope.get("schema") != 1:
        raise ComparisonError(f"Неподдерживаемый формат сессии: {source}")
    result = envelope.get("result")
    if not isinstance(result, dict) or result.get("kind") != "dut_batch":
        raise ComparisonError(f"Сессия {source} не содержит пакет DUT (dut_batch)")
    if result.get("state") != "complete":
        raise ComparisonError(
            f"Сессия {source} имеет статус {result.get('state')!r}; "
            "для строгого сравнения допустимы только завершённые пакеты без ошибок"
        )
    tests = result.get("tests")
    order = result.get("test_order")
    if not isinstance(tests, dict) or not isinstance(order, list) or not order:
        raise ComparisonError(f"В сессии {source} отсутствует список тестов")
    for key in order:
        if key not in tests or not isinstance(tests[key], dict) or not tests[key].get("kind"):
            raise ComparisonError(f"Тест {key!r} в сессии {source} не завершён")
    return {
        "path": str(source.resolve()),
        "envelope": envelope,
        "result": result,
        "context": envelope.get("measurement_context") or {},
        "tests": tests,
        "test_order": list(order),
        "device_id": str(envelope.get("device_id") or ""),
        "session_id": str(envelope.get("session_id") or source.parent.name),
        "note": str(envelope.get("note") or ""),
    }


def _conditions(session):
    ctx = session["context"]
    audio = ctx.get("audio_io") or {}
    device = ctx.get("device") or {}
    return {
        "device_pair_key": device.get("stable_id") or audio.get("device_pair_key"),
        "dac_device": audio.get("dac_device") or device.get("dac_alsa_pcm"),
        "adc_device": audio.get("adc_device") or device.get("alsa_pcm"),
        "dac_stable_id": audio.get("dac_stable_id") or device.get("dac_stable_id"),
        "adc_stable_id": audio.get("adc_stable_id") or device.get("adc_stable_id"),
        "backend": audio.get("backend"),
        "sample_rate_hz": audio.get("sample_rate_hz"),
        "playback_format": audio.get("playback_format"),
        "capture_format": audio.get("capture_format"),
        "playback_channels": audio.get("playback_channels"),
        "capture_channels": audio.get("capture_channels"),
        "mixer_fingerprint": audio.get("alsa_mixer_fingerprint"),
        "calibration": ctx.get("calibration") or {},
    }


def _json_equal(a, b):
    return json.dumps(a, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == \
           json.dumps(b, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _check_compatibility(sessions):
    first = sessions[0]
    conditions = _conditions(first)
    first_order = first["test_order"]
    first_params = first["result"].get("parameters") or {}
    warnings = []
    first_version = ((first["context"].get("software") or {}).get("version"))
    required = (
        "device_pair_key", "dac_device", "adc_device", "sample_rate_hz",
        "playback_format", "capture_format", "playback_channels", "capture_channels",
        "mixer_fingerprint",
    )
    missing = [key for key in required if conditions.get(key) in (None, "")]
    if missing:
        raise ComparisonError(
            "В первой сессии не зафиксированы обязательные условия сравнения: "
            + ", ".join(missing)
        )
    if not conditions.get("calibration"):
        raise ComparisonError("В первой сессии отсутствует контекст калибровки")
    seen_paths = {first["path"]}
    for current in sessions[1:]:
        if current["path"] in seen_paths:
            raise ComparisonError("Одна и та же сессия указана для сравнения дважды")
        seen_paths.add(current["path"])
        current_conditions = _conditions(current)
        for key, value in conditions.items():
            other = current_conditions.get(key)
            if not _json_equal(value, other):
                raise ComparisonError(
                    f"Несопоставимые условия для аппарата {current['device_id']}: "
                    f"{key}: {value!r} ≠ {other!r}"
                )
        if current["test_order"] != first_order:
            raise ComparisonError(
                f"Набор/порядок тестов различается: {first_order!r} и {current['test_order']!r}"
            )
        current_params = current["result"].get("parameters") or {}
        for test in first_order:
            if not _json_equal(first_params.get(test), current_params.get(test)):
                raise ComparisonError(
                    f"Параметры теста {test.upper()} различаются для аппарата "
                    f"{current['device_id']}"
                )
        # Response calibration is a measurement-chain correction and must match.
        if "response" in first_order:
            for field in ("response_calibration", "calibration_note"):
                a = first["tests"]["response"].get(field)
                b = current["tests"]["response"].get(field)
                if not _json_equal(a, b):
                    raise ComparisonError(
                        f"Поправка АЧХ различается ({field}) для аппарата {current['device_id']}"
                    )
        version = ((current["context"].get("software") or {}).get("version"))
        if version != first_version:
            warnings.append(
                f"Версия ПО различается: {first_version!r} и {version!r}; "
                "перед сравнением проверь совместимость формата измерений"
            )
    return conditions, warnings


def _finite(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _aggregate(values, mode):
    data = [_finite(value) for value in values]
    data = [value for value in data if value is not None]
    if not data:
        return None
    if mode == "power_db":
        power = sum(10.0 ** (value / 10.0) for value in data) / len(data)
        return 10.0 * math.log10(max(power, 1e-300))
    if mode == "amplitude_db":
        amplitude = sum(10.0 ** (value / 20.0) for value in data) / len(data)
        return 20.0 * math.log10(max(amplitude, 1e-300))
    if mode == "rms":
        return math.sqrt(sum(value * value for value in data) / len(data))
    if mode == "mean":
        return sum(data) / len(data)
    raise ValueError(f"Unknown aggregate mode: {mode}")


def _put_metric(metrics, test, name, unit, left, right, aggregate_mode):
    metrics.setdefault(test, {})[name] = {
        "L": _finite(left),
        "R": _finite(right),
        "aggregate": _aggregate((left, right), aggregate_mode),
        "unit": unit,
    }


def _response_curve(test_result, channel, curve_name):
    channel_result = test_result.get("ch", {}).get(channel) or {}
    freq = np.asarray(channel_result.get("freq", []), dtype=float)
    values = np.asarray(channel_result.get(curve_name, []), dtype=float)
    if freq.ndim != 1 or values.ndim != 1 or freq.size < 2 or values.size != freq.size:
        return None, None
    valid = np.isfinite(freq) & np.isfinite(values) & (freq > 0)
    if np.count_nonzero(valid) < 2:
        return None, None
    freq, values = freq[valid], values[valid]
    order = np.argsort(freq)
    return freq[order], values[order]


def _make_response_comparison(test_result):
    mode = test_result.get("mode", "flat")
    curve_name = "riaa_dev_db" if mode == "riaa" else "fr_norm_db"
    left_f, left = _response_curve(test_result, "L", curve_name)
    right_f, right = _response_curve(test_result, "R", curve_name)
    if left_f is None or right_f is None:
        raise ComparisonError("В одной из сессий нет полной кривой АЧХ L/R")
    # Both channels are sampled on their original grids; export each channel
    # unchanged and derive aggregate points only on a common log-frequency grid.
    lo = max(float(left_f[0]), float(right_f[0]))
    hi = min(float(left_f[-1]), float(right_f[-1]))
    if hi <= lo:
        raise ComparisonError("Диапазоны частот кривых L/R не пересекаются")
    # Preserve the full measured sweep grid for graph overlays. The smaller
    # standard-frequency subset is duplicated below for compact scalar metrics.
    full_f = left_f[(left_f >= lo) & (left_f <= hi)]
    full_l = np.interp(np.log10(full_f), np.log10(left_f), left)
    full_r = np.interp(np.log10(full_f), np.log10(right_f), right)
    full_avg = (full_l + full_r) / 2.0
    points = np.asarray([f for f in FREQUENCY_POINTS_HZ if lo <= f <= hi], dtype=float)
    l_points = np.interp(np.log10(points), np.log10(left_f), left) if points.size else np.array([])
    r_points = np.interp(np.log10(points), np.log10(right_f), right) if points.size else np.array([])
    aggregate = (l_points + r_points) / 2.0
    curve = {
        "name": curve_name,
        "mode": mode,
        "frequency_hz": full_f.tolist(),
        "L_db": full_l.tolist(),
        "R_db": full_r.tolist(),
        "aggregate_db": full_avg.tolist(),
        "sampled_frequency_hz": points.tolist(),
        "sampled_L_db": l_points.tolist(),
        "sampled_R_db": r_points.tolist(),
        "sampled_aggregate_db": aggregate.tolist(),
    }
    # RMS deviation from target over the log-spaced sweep bins. Use the full
    # source curve, not only the selected display points.
    left_error = float(np.sqrt(np.mean(left * left)))
    right_error = float(np.sqrt(np.mean(right * right)))
    return curve, left_error, right_error, test_result.get("ch", {})


def _extract_session_metrics(session):
    tests = session["tests"]
    metrics = {}
    curves = {}
    for test in session["test_order"]:
        value = tests[test]
        channels = value.get("ch") or {}
        L, R = channels.get("L") or {}, channels.get("R") or {}
        if test == "noise":
            for key, unit in (("unw_dbfs", "dBFS"), ("a_dbfs", "dBFS"), ("c_dbfs", "dBFS")):
                _put_metric(metrics, test, key, unit, L.get(key), R.get(key), "power_db")
            # Preserve the configured mains harmonics instead of assuming 50 Hz.
            mains = _finite((session["context"].get("calibration") or {}).get("mains_hz")) or 50.0
            for multiple in range(1, 7):
                freq = int(round(mains * multiple))
                lval = next((_finite(h.get("dbfs")) for h in L.get("hum", [])
                             if abs(float(h.get("f", 0)) - freq) < 0.5), None)
                rval = next((_finite(h.get("dbfs")) for h in R.get("hum", [])
                             if abs(float(h.get("f", 0)) - freq) < 0.5), None)
                if lval is not None or rval is not None:
                    _put_metric(metrics, test, f"hum_{freq}_dbfs", "dBFS", lval, rval, "power_db")
        elif test == "thd":
            for key, unit, mode in (
                ("level_dbfs", "dBFS", "power_db"),
                ("thd_pct", "%", "rms"), ("thd_db", "dBc", "power_db"),
                ("thd_upper_pct", "%", "rms"), ("thd_upper_db", "dBc", "power_db"),
                ("thdn_pct", "%", "rms"), ("thdn_db", "dBc", "power_db"),
                ("noise_dbfs", "dBFS", "power_db"),
            ):
                _put_metric(metrics, test, key, unit, L.get(key), R.get(key), mode)
            def thd_state(channel):
                if not channel.get("harmonics"):
                    return "n/a"
                if channel.get("thd_bound"):
                    return "bound"
                if channel.get("thd_partial"):
                    return "partial"
                return "measured"
            metrics[test]["thd_status"] = {
                "L": thd_state(L), "R": thd_state(R),
                "aggregate": None, "unit": "status",
            }
            left_h = {int(item["h"]): item for item in L.get("harmonics", []) if "h" in item}
            right_h = {int(item["h"]): item for item in R.get("harmonics", []) if "h" in item}
            for harmonic in sorted(set(left_h) | set(right_h)):
                lh, rh = left_h.get(harmonic, {}), right_h.get(harmonic, {})
                prefix = f"H{harmonic}"
                for field, unit, mode in (
                    ("dbc", "dBc", "power_db"),
                    ("raw_dbc", "dBc", "power_db"),
                    ("floor_dbc", "dBc", "power_db"),
                    ("margin_db", "dB", "mean"),
                ):
                    _put_metric(metrics, test, f"{prefix}_{field}", unit,
                                lh.get(field), rh.get(field), mode)
                metrics[test][f"{prefix}_status"] = {
                    "L": ("measured" if lh.get("credible") else "bound") if lh else "n/a",
                    "R": ("measured" if rh.get("credible") else "bound") if rh else "n/a",
                    "aggregate": None, "unit": "status",
                }
        elif test == "imd":
            for key, unit, mode in (
                ("imd_pct", "%", "rms"), ("imd_db", "dBc", "power_db"),
                ("d2_pct", "%", "rms"), ("d3_pct", "%", "rms"),
            ):
                _put_metric(metrics, test, key, unit, L.get(key), R.get(key), mode)
            labels = sorted({str(p.get("label")) for channel in (L, R)
                             for p in channel.get("products", []) if p.get("label") is not None})
            for label in labels:
                lp = next((p.get("dbc") for p in L.get("products", []) if p.get("label") == label), None)
                rp = next((p.get("dbc") for p in R.get("products", []) if p.get("label") == label), None)
                _put_metric(metrics, test, f"product_{label}_dbc", "dBc", lp, rp, "power_db")
        elif test == "response":
            curve, left_error, right_error, _ = _make_response_comparison(value)
            curves["response"] = curve
            _put_metric(metrics, test, "rms_error_db", "dB", left_error, right_error, "rms")
            for frequency, left_db, right_db, avg_db in zip(
                    curve["sampled_frequency_hz"], curve["sampled_L_db"],
                    curve["sampled_R_db"], curve["sampled_aggregate_db"]):
                metrics.setdefault(test, {})[f"{curve['name']}_{frequency:g}Hz"] = {
                    "L": left_db, "R": right_db, "aggregate": avg_db, "unit": "dB",
                }
            for channel in ("L", "R"):
                gain = (channels.get(channel) or {}).get("gain_1k_db")
                if gain is not None:
                    metrics[test].setdefault("gain_1k_db", {"L": None, "R": None,
                                                            "aggregate": None, "unit": "dB"})
                    metrics[test]["gain_1k_db"][channel] = _finite(gain)
            gain_metric = metrics[test].get("gain_1k_db")
            if gain_metric:
                gain_metric["aggregate"] = _aggregate(
                    (gain_metric["L"], gain_metric["R"]), "amplitude_db")
    return metrics, curves


def compare_sessions(paths):
    """Compare 2+ complete saved DUT batch sessions under identical conditions."""
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    sessions = [_read_session(path) for path in paths]
    if len(sessions) < 2:
        raise ComparisonError("Для сравнения нужно не менее двух сессий DUT")
    conditions, warnings = _check_compatibility(sessions)
    devices = []
    for session in sessions:
        metrics, curves = _extract_session_metrics(session)
        devices.append({
            "device_id": session["device_id"],
            "session_id": session["session_id"],
            "created_at_local": session["envelope"].get("created_at_local"),
            "started_at_local": session["result"].get("started_at_local"),
            "finished_at_local": session["result"].get("finished_at_local"),
            "software_version": ((session["context"].get("software") or {}).get("version")),
            "note": session["note"],
            "metrics": metrics,
            "curves": curves,
        })
    return {
        "kind": "dut_comparison",
        "schema": 1,
        "created_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
        "conditions": conditions,
        "test_order": list(sessions[0]["test_order"]),
        "parameters": sessions[0]["result"].get("parameters") or {},
        "device_order": [item["device_id"] for item in devices],
        "devices": devices,
        "warnings": warnings,
    }


def _metric_rows(comparison):
    rows = []
    for device in comparison["devices"]:
        for test, values in device["metrics"].items():
            for name, metric in values.items():
                rows.append({
                    "device_id": device["device_id"],
                    "session_id": device["session_id"],
                    "note": device["note"],
                    "test": test,
                    "metric": name,
                    "L": metric.get("L"),
                    "R": metric.get("R"),
                    "aggregate": metric.get("aggregate"),
                    "unit": metric.get("unit"),
                })
    return rows


def _curve_rows(comparison):
    rows = []
    for device in comparison["devices"]:
        for test, curve in device["curves"].items():
            for frequency, left, right, aggregate in zip(
                    curve["frequency_hz"], curve["L_db"], curve["R_db"], curve["aggregate_db"]):
                rows.append({
                    "device_id": device["device_id"], "session_id": device["session_id"],
                    "test": test, "curve": curve["name"], "frequency_hz": frequency,
                    "L_db": left, "R_db": right, "aggregate_db": aggregate,
                })
    return rows


def save_comparison(comparison, output_dir=None):
    """Save a comparison bundle under the project measurements directory."""
    root = Path(output_dir) if output_dir else (
        Path(__file__).resolve().parents[1] / "measurements" / "comparisons"
    )
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = f"{stamp}-dut-comparison"
    number = 1
    while (root / stem).exists():
        stem = f"{stamp}-dut-comparison-{number:02d}"
        number += 1
    final = root / stem
    staging = root / (".pending-" + stem)
    staging.mkdir()
    try:
        with (staging / "comparison.json").open("w", encoding="utf-8") as fh:
            json.dump(comparison, fh, ensure_ascii=False, indent=2, allow_nan=False)
            fh.write("\n")
        metric_rows = _metric_rows(comparison)
        with (staging / "metrics.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=(
                "device_id", "session_id", "note", "test", "metric", "L", "R", "aggregate", "unit"
            ))
            writer.writeheader()
            writer.writerows(metric_rows)
        curve_rows = _curve_rows(comparison)
        with (staging / "curves.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=(
                "device_id", "session_id", "test", "curve", "frequency_hz",
                "L_db", "R_db", "aggregate_db"
            ))
            writer.writeheader()
            writer.writerows(curve_rows)
        (staging / "README.txt").write_text(
            "ValveBench DUT comparison\n"
            "comparison.json is the authoritative result.\n"
            "metrics.csv contains scalar metrics with L, R and aggregate columns.\n"
            "curves.csv contains frequency-response curves with L, R and aggregate columns.\n",
            encoding="utf-8"
        )
        os.replace(staging, final)
    except Exception:
        import shutil
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return str(final)


def text_comparison(comparison):
    lines = [
        f"СРАВНЕНИЕ DUT · {len(comparison['devices'])} аппарата(ов) · "
        f"{comparison['created_at_local']}",
        f"ЦАП/АЦП: {comparison['conditions'].get('dac_device')} → "
        f"{comparison['conditions'].get('adc_device')} · "
        f"{comparison['conditions'].get('sample_rate_hz')} Гц",
        "Каналы L/R сохранены отдельно; «ср.» — агрегат двух каналов.",
    ]
    for device in comparison["devices"]:
        lines.append(f"\n=== DUT {device['device_id']} · {device['session_id']} ===")
        if device["note"]:
            lines.append(f"  {device['note']}")
        for test in comparison["test_order"]:
            metrics = device["metrics"].get(test, {})
            if not metrics:
                continue
            lines.append(f"  {test.upper()}")
            for name, value in metrics.items():
                lval, rval, aggregate = value.get("L"), value.get("R"), value.get("aggregate")
                if isinstance(lval, (int, float)) or isinstance(rval, (int, float)):
                    fmt = lambda x: "—" if x is None else f"{x:.3f}"
                    lines.append(
                        f"    {name}: L {fmt(lval)} · R {fmt(rval)} · "
                        f"ср. {fmt(aggregate)} {value.get('unit', '')}"
                    )
                else:
                    lines.append(f"    {name}: L {lval or '—'} · R {rval or '—'}")
    return "\n".join(lines + [f"! {warning}" for warning in comparison["warnings"]])
