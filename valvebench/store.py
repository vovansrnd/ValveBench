"""Сохранение результатов: JSON (полностью) и CSV (кривые) для сравнения «до/после»."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from datetime import datetime

from .measure import to_jsonable

DIR = os.path.expanduser("~/.local/share/valvebench")


def save_json(res, path=None, label=""):
    os.makedirs(DIR, exist_ok=True)
    path = path or os.path.join(DIR, time.strftime("%Y%m%d-%H%M%S-") + res.get("kind", "res")
                                + (f"-{label}" if label else "") + ".json")
    d = to_jsonable(res)
    d["saved"] = time.strftime("%Y-%m-%d %H:%M:%S")
    d["label"] = label
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    return path


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def sweep_csv(res, path):
    """Кривые свипа в CSV: freq, отклонение/АЧХ и THD по каналам."""
    f = res["ch"]["L"]["freq"]
    tf = res["ch"]["L"]["thd_freq"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["freq_hz", "fr_norm_L_db", "fr_norm_R_db", "riaa_dev_L_db", "riaa_dev_R_db"])
        for i in range(len(f)):
            w.writerow([f"{f[i]:.3f}", f"{res['ch']['L']['fr_norm_db'][i]:.4f}",
                        f"{res['ch']['R']['fr_norm_db'][i]:.4f}",
                        f"{res['ch']['L']['riaa_dev_db'][i]:.4f}", f"{res['ch']['R']['riaa_dev_db'][i]:.4f}"])
        w.writerow([])
        w.writerow(["freq_hz", "thd_L_pct", "thd_R_pct"])
        for i in range(len(tf)):
            w.writerow([f"{tf[i]:.3f}", f"{res['ch']['L']['thd_pct'][i]:.6f}", f"{res['ch']['R']['thd_pct'][i]:.6f}"])



def save_gain_series(res, directory=None):
    """Save a Gain Match frequency series under the current project directory.

    Unlike the generic legacy save_json() default, this deliberately avoids
    the home directory: default output is ./measurements/gain-match/.
    Returns (json_path, csv_path).
    """
    directory = directory or os.path.join(os.getcwd(), "measurements", "gain-match")
    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = f"gain-match-{stamp}"
    suffix = 1
    while os.path.exists(os.path.join(directory, stem + ".json")):
        stem = f"gain-match-{stamp}-{suffix:02d}"
        suffix += 1
    json_path = os.path.join(directory, stem + ".json")
    csv_path = os.path.join(directory, stem + ".csv")

    payload = to_jsonable(res)
    payload["saved"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp_json = json_path + ".tmp"
    with open(tmp_json, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp_json, json_path)

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "frequency_hz", "delta_adc_L_minus_R_db", "loopback_correction_db",
            "corrected_delta_gain_L_minus_R_db", "repeat_spread_db", "SINAD_L_db",
            "SINAD_R_db", "carrier_residual_L_db", "carrier_residual_R_db",
            "valid", "repeat_stable", "repeatability",
        ])
        for row in res.get("rows", []):
            left, right = row["channels"]["L"], row["channels"]["R"]
            writer.writerow([
                f"{row['frequency_hz']:.6f}", f"{row['delta_adc_db']:.6f}",
                "" if row["loopback_correction_db"] is None else f"{row['loopback_correction_db']:.6f}",
                "" if row["delta_gain_db"] is None else f"{row['delta_gain_db']:.6f}",
                f"{row['delta_output_spread_db']:.6f}", f"{left['sinad_db']:.2f}",
                f"{right['sinad_db']:.2f}", f"{left['carrier_residual_db']:.2f}",
                f"{right['carrier_residual_db']:.2f}", int(row["valid"]),
                ("" if row.get("repeat_stable") is None else int(row["repeat_stable"])),
                res.get("repeatability", "assessed"),
            ])
    return json_path, csv_path


def save_gain_reference(reference, directory=None):
    """Persist the per-device loopback correction in the project directory."""
    directory = directory or os.path.join(os.getcwd(), "measurements", "gain-match")
    os.makedirs(directory, exist_ok=True)
    import re
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(reference.get("device_key", "device"))).strip("_.") or "device"
    level_tag = f"{float(reference['generator_level_dbfs']):.1f}".replace("-", "m").replace(".", "p")
    path = os.path.join(directory,
                        f"loopback-reference-{slug}-{int(reference['fs'])}Hz-{level_tag}dBFS.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(to_jsonable(reference), fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def load_gain_reference(device_key, fs, level_dbfs, frequencies, directory=None):
    """Load only a reference whose device, sample rate, level and tones all match."""
    directory = directory or os.path.join(os.getcwd(), "measurements", "gain-match")
    import glob
    paths = sorted(glob.glob(os.path.join(directory, "loopback-reference-*.json")),
                   key=os.path.getmtime, reverse=True)
    wanted = {round(float(f), 6) for f in frequencies}
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                ref = json.load(fh)
            if ref.get("kind") != "gain_match_loopback_reference":
                continue
            if ref.get("device_key") != device_key or int(ref.get("fs", -1)) != int(fs):
                continue
            if abs(float(ref.get("generator_level_dbfs", 999.0)) - float(level_dbfs)) > 1e-6:
                continue
            got = {round(float(f), 6) for f in ref.get("frequencies_hz", [])}
            if got != wanted or not all(row.get("valid") for row in ref.get("rows", [])):
                continue
            return ref
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return None


# ---------------------------------------------------------------- device measurement sessions

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CURVE_AXES = ("freq", "frequency_hz", "frequency", "freq_hz", "thd_freq",
               "centers", "levels", "levels_dbfs", "gen", "x")


def _clean_json(value):
    """Convert a nested payload to strict JSON (NaN/Inf become null)."""
    value = to_jsonable(value)
    def clean(item):
        if isinstance(item, dict):
            return {str(k): clean(v) for k, v in item.items()}
        if isinstance(item, list):
            return [clean(v) for v in item]
        if isinstance(item, float) and not math.isfinite(item):
            return None
        return item
    return clean(value)


def _is_numeric_vector(value, allow_missing=False):
    if not isinstance(value, list) or not value:
        return False
    return all((allow_missing and v is None) or
               (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)))
               for v in value)


def _write_metrics_csv(payload, path):
    """Flat index of scalar metadata/metrics; full structured data stays in JSON."""
    rows = []
    def walk(value, prefix):
        if isinstance(value, dict):
            for key in sorted(value):
                walk(value[key], f"{prefix}.{key}" if prefix else str(key))
        elif isinstance(value, list):
            # Arrays are represented in curves.csv / session.json, not flattened
            # into thousands of hard-to-read metric rows.
            return
        elif value is None or isinstance(value, (str, int, float, bool)):
            rows.append((prefix, value))
    walk(payload, "")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(("metric_path", "value"))
        for key, value in rows:
            writer.writerow((key, "" if value is None else value))


def _write_curves_csv(result, path):
    """Export numeric x/y vectors as long-form CSV without discarding source arrays."""
    written = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(("group", "x_name", "x_value", "series", "y_value"))

        def walk(value, prefix):
            nonlocal written
            if not isinstance(value, dict):
                return
            axes = [(key, value[key]) for key in _CURVE_AXES
                    if key in value and _is_numeric_vector(value[key])]
            for axis_name, x_values in axes:
                for series_name, y_values in value.items():
                    if series_name == axis_name or not _is_numeric_vector(y_values, allow_missing=True):
                        continue
                    if len(y_values) != len(x_values):
                        continue
                    for x, y in zip(x_values, y_values):
                        writer.writerow((prefix, axis_name, x, series_name, "" if y is None else y))
                        written += 1
            for key, child in value.items():
                if isinstance(child, dict):
                    walk(child, f"{prefix}.{key}" if prefix else str(key))
        walk(result, "result")
    return written


def device_measurements_root():
    """Return the measurements folder beside the active ValveBench project.

    Resolve from this module rather than the process working directory: a
    desktop launcher, shell shortcut, or ``python -m`` invocation may start
    with an unrelated cwd (including the home directory).
    """
    project_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    return os.path.join(project_root, "measurements", "devices")


def save_device_measurement(result, device_id, note="", context=None, directory=None):
    """Save one DUT measurement as a portable, immutable session directory.

    Default location is <ValveBench-project>/measurements/devices/<device-id>/.
    The original numeric arrays are retained in session.json; CSV files are
    convenience indexes for spreadsheets and quick review. ``directory`` is
    primarily intended for tests and explicit programmatic exports.
    Returns the final session directory.
    """
    device_id = str(device_id or "").strip()
    if not _DEVICE_ID_RE.fullmatch(device_id):
        raise ValueError("ID аппарата: 1–64 символа; допустимы латинские буквы, цифры, точка, дефис и подчёркивание; первый символ — буква или цифра")
    if not isinstance(result, dict) or not result:
        raise ValueError("нет результата измерения для сохранения")

    created = datetime.now().astimezone().isoformat(timespec="seconds")
    payload_result = _clean_json(result)
    inherited_context = payload_result.pop("_valvebench_context", None)
    measurement_context = _clean_json(context if context else inherited_context or {})
    kind = str(payload_result.get("kind") or "measurement")
    safe_kind = re.sub(r"[^A-Za-z0-9_-]+", "-", kind).strip("-")[:32] or "measurement"
    root = os.path.abspath(directory or device_measurements_root())
    os.makedirs(root, exist_ok=True)
    root_real = os.path.realpath(root)
    device_root = os.path.join(root_real, device_id)
    if os.path.islink(device_root):
        raise ValueError("каталог аппарата является символической ссылкой; экспорт остановлен")
    if os.path.commonpath((root_real, os.path.realpath(device_root))) != root_real:
        raise ValueError("путь каталога аппарата выходит за пределы каталога измерений")
    os.makedirs(device_root, exist_ok=True)

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    stem = f"{stamp}-{safe_kind}"
    final = os.path.join(device_root, stem)
    suffix = 1
    while os.path.exists(final):
        final = os.path.join(device_root, f"{stem}-{suffix:02d}")
        suffix += 1

    envelope = {
        "format": "valvebench-device-measurement",
        "schema": 1,
        "device_id": device_id,
        "session_id": os.path.basename(final),
        "created_at_local": created,
        "note": str(note or "").strip(),
        "result_kind": kind,
        "measurement_context": measurement_context,
        "result": payload_result,
    }

    staging = tempfile.mkdtemp(prefix=".pending-", dir=device_root)
    try:
        json_path = os.path.join(staging, "session.json")
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, ensure_ascii=False, indent=2, allow_nan=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        metrics_path = os.path.join(staging, "metrics.csv")
        _write_metrics_csv(envelope, metrics_path)
        curves_path = os.path.join(staging, "curves.csv")
        curve_rows = _write_curves_csv(payload_result, curves_path)
        readme_path = os.path.join(staging, "README.txt")
        with open(readme_path, "w", encoding="utf-8") as fh:
            fh.write("ValveBench device measurement session\n")
            fh.write(f"Device ID: {device_id}\nSession: {os.path.basename(final)}\n")
            fh.write(f"Created: {created}\nType: {kind}\n")
            if note:
                fh.write(f"Note: {str(note).strip()}\n")
            fh.write("\nFiles:\n  session.json  complete result and measurement context\n")
            fh.write("  metrics.csv   scalar values and metadata\n")
            fh.write(f"  curves.csv    numeric curves ({curve_rows} rows)\n")
            fh.write("\nThe JSON file is authoritative; CSV files are convenience exports.\n")
            fh.write("Raw PCM samples are not added by this generic exporter unless the measurement result itself contains them.\n")
        manifest = {}
        for name in ("session.json", "metrics.csv", "curves.csv", "README.txt"):
            file_path = os.path.join(staging, name)
            digest = hashlib.sha256()
            with open(file_path, "rb") as fh:
                for block in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(block)
            manifest[name] = {"size_bytes": os.path.getsize(file_path), "sha256": digest.hexdigest()}
        with open(os.path.join(staging, "MANIFEST.json"), "w", encoding="utf-8") as fh:
            json.dump({"schema": 1, "files": manifest}, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(staging, final)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return final
