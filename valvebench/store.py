"""Сохранение результатов: JSON (полностью) и CSV (кривые) для сравнения «до/после»."""
from __future__ import annotations

import csv
import json
import os
import time

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
            "corrected_delta_gain_L_minus_R_db", "repeat_spread_db", "SNR_L_db",
            "SNR_R_db", "valid", "repeat_stable",
        ])
        for row in res.get("rows", []):
            left, right = row["channels"]["L"], row["channels"]["R"]
            writer.writerow([
                f"{row['frequency_hz']:.6f}", f"{row['delta_adc_db']:.6f}",
                "" if row["loopback_correction_db"] is None else f"{row['loopback_correction_db']:.6f}",
                "" if row["delta_gain_db"] is None else f"{row['delta_gain_db']:.6f}",
                f"{row['delta_output_spread_db']:.6f}", f"{left['snr_db']:.2f}",
                f"{right['snr_db']:.2f}", int(row["valid"]), int(row["repeat_stable"]),
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
