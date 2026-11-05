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
