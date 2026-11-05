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
            "frequency_hz", "generator_level_dbfs", "L_level_dbfs", "R_level_dbfs",
            "delta_output_L_minus_R_db", "L_gain_db", "R_gain_db", "delta_gain_L_minus_R_db",
            "L_snr_db", "R_snr_db", "L_spread_db", "R_spread_db",
            "delta_output_spread_db", "calibrated", "valid",
        ])
        for row in res.get("rows", []):
            left, right = row["channels"]["L"], row["channels"]["R"]
            writer.writerow([
                f"{row['frequency_hz']:.6f}", f"{res['generator_level_dbfs']:.3f}",
                f"{left['level_dbfs']:.6f}", f"{right['level_dbfs']:.6f}",
                f"{row['delta_output_db']:.6f}",
                "" if left["gain_db"] is None else f"{left['gain_db']:.6f}",
                "" if right["gain_db"] is None else f"{right['gain_db']:.6f}",
                "" if row["delta_gain_db"] is None else f"{row['delta_gain_db']:.6f}",
                f"{left['snr_db']:.3f}", f"{right['snr_db']:.3f}",
                f"{left['level_spread_db']:.6f}", f"{right['level_spread_db']:.6f}",
                f"{row['delta_output_spread_db']:.6f}", int(row["calibrated"]), int(row["valid"]),
            ])
    return json_path, csv_path
