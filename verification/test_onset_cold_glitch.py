#!/usr/bin/env python3
"""Регрессия D3 (приёмка 0.1.21-dev4): узкополосный onset переживает щелчки
открытия устройства.

Запись verification/data/cold-fail-dlx-96k.npy — РЕАЛЬНЫЙ захват DLX@96k,
на котором до исправления clock-тест падал MeasureError: в записи 5 одиночных
сэмплов ±0.5 FS (t=0.010 и 0.300 с), и прежний порог 0.1*max(огибающей)
поднимался выше уровня тона (7.7e-05) выбросом щелчка (0.0036).

Требования:
  1. _onset находит начало тона (до фикса — MeasureError);
  2. измеренные по этой записи ppm лежат в пределах ±2 ppm
     (сравнение с корректным прогоном той же карты: ±0.2 ppm).
"""
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from valvebench import measure as m, calib  # noqa: E402
from valvebench.backend import AlsaCli  # noqa: E402

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                    "cold-fail-dlx-96k.npy")


def main():
    rec = np.load(DATA)
    assert rec.shape[1] == 2 and len(rec) > 96000 * 6, "формат записи изменился"
    # независимая проверка, что в записи действительно есть щелчки-триггеры
    peak_samples = int(np.sum(np.max(np.abs(rec), axis=1) > 0.1))
    assert peak_samples >= 3, f"тестовая запись не содержит щелчков: {peak_samples}"

    cfg = calib.load()
    be = AlsaCli(hw_out="hw:3,0", hw_in="hw:3,0",
                 in_map=cfg["in_map"], out_map=cfg["out_map"])
    rig = m.Rig(be, cfg, fs=96000, blocks=4, settle=0.25)
    rig.open()

    d = rig._onset(rec, tone_hz=1000.0)
    print(f"PASS 1: onset={d} ({d/96000:.3f} с) — до исправления: MeasureError")

    k, a = rig.amp_tone(1000.0, -72.0)
    win = rec[d:d + rig.blocks * rig.n]
    ppm = []
    for c in (0, 1):
        r = rig._tone_an(win, c, k, nharm=2)
        ppm.append((r["f0"] / rig.f_of(k) - 1.0) * 1e6)
    print(f"PASS 2: ppm = {ppm[0]:+.1f} / {ppm[1]:+.1f} (допуск ±2)")
    ok = all(abs(p) <= 2.0 for p in ppm)
    print("ИТОГ:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
