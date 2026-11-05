"""Независимая калибровка ЦАП L/R, АЦП L/R и внешнего аттенюатора."""
from __future__ import annotations

import json
import math
import os

PATH = os.path.expanduser("~/.config/valvebench/config.json")

DEFAULTS = {
    "device": "sim",
    "fs": 96000,
    "in_map": [0, 1],
    "out_map": [0, 1],
    "out_fs_vrms": [None, None],  # напряжение ЦАП L/R при 0 dBFS (полный синус)
    "in_fs_vrms": [None, None],   # напряжение АЦП L/R при 0 dBFS
    "att_db": 0.0,                # общий внешний аттенюатор DAC→DUT, ≤0 dB
    "mains": 50.0,
    "band_hi": 20000.0,
}


class Calibration:
    def __init__(self, data=None):
        self.d = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
        if data:
            for k in DEFAULTS:
                if k in data:
                    value = data[k]
                    # Совместимость с ранним config.json, где ЦАП калибровался одним числом.
                    if k == "out_fs_vrms" and isinstance(value, (int, float)):
                        value = [float(value), float(value)]
                    self.d[k] = value
        for key in ("out_fs_vrms", "in_fs_vrms"):
            value = self.d.get(key)
            if not isinstance(value, (list, tuple)):
                self.d[key] = [None, None]
            else:
                vals = list(value[:2])
                while len(vals) < 2:
                    vals.append(None)
                self.d[key] = vals

    def __getitem__(self, k):
        return self.d[k]

    def __setitem__(self, k, v):
        self.d[k] = v

    def out_full_scale_vrms(self, ch):
        return self.d["out_fs_vrms"][int(ch)]

    # ---- физические напряжения
    def out_v(self, dbfs, ch=0):
        """В RMS на выходе ЦАП для синуса dbfs и выбранного канала."""
        v = self.out_full_scale_vrms(ch)
        return None if v is None else float(v) * 10 ** (float(dbfs) / 20)

    def dut_in_v(self, dbfs, ch=0):
        v = self.out_v(dbfs, ch)
        return None if v is None else v * 10 ** (float(self.d["att_db"]) / 20)

    def in_v(self, dbfs, ch):
        v = self.d["in_fs_vrms"][int(ch)]
        return None if v is None else float(v) * 10 ** (float(dbfs) / 20)

    def gain_db(self, gen_dbfs, rec_dbfs, ch):
        vi, vo = self.dut_in_v(gen_dbfs, ch), self.in_v(rec_dbfs, ch)
        if vi is None or vo is None or vi <= 0 or vo <= 0:
            return None
        return 20 * math.log10(vo / vi)

    # ---- калибровка по измерениям
    def set_out_from_meter(self, gen_dbfs, vrms, ch=0):
        self.d["out_fs_vrms"][int(ch)] = float(vrms) / 10 ** (float(gen_dbfs) / 20)

    def set_in_from_loop(self, gen_dbfs, rec_dbfs, ch):
        """Петля DAC→ADC без внешнего аттенюатора: входное напряжение = выходному."""
        v = self.out_v(gen_dbfs, ch)
        if v is None:
            raise ValueError(f"сначала откалибруй выход ЦАП {('L','R')[int(ch)]}")
        self.d["in_fs_vrms"][int(ch)] = v / 10 ** (float(rec_dbfs) / 20)


def fmt_v(v):
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1:
        return f"{v:.3f} В"
    if a >= 1e-3:
        return f"{v * 1e3:.3f} мВ"
    return f"{v * 1e6:.2f} мкВ"


def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            return Calibration(json.load(f))
    except (OSError, ValueError):
        return Calibration()


def save(cal):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(cal.d, f, ensure_ascii=False, indent=1)
