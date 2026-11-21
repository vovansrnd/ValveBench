#!/usr/bin/env python3
"""Карта позиций переключателя P0–P3 для двух входных нагрузок (rin10k, rin33k).

Запуск из корня ветки после появления захватов:
  python3 results/subsonic-experiment/analysis/switch_map.py
Вывод: analysis/outputs/switch_map.txt. Отсутствующие захваты пропускаются.
"""
import csv
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np

from valvebench import dsp

CAP = Path(__file__).resolve().parent.parent / "captures"
OUT = Path(__file__).resolve().parent / "outputs" / "switch_map.txt"

POSITIONS = ("P0", "P47k", "P22k", "P10k")
SERIES = ("rin10k", "rin33k")
TABLE_F = (5.0, 8.0, 10.0, 15.0, 20.0, 25.0, 31.5, 40.0, 80.0)
FITS = (0.50, 0.70)


def read_mean(path):
    """Кривая позиции = среднее (L+R)/2 (каналы одинаковы по схеме) + расхождение L/R."""
    L, R, cohs = [], [], []
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        f = float(row["freq_hz"])
        dl, dr = float(row["L_RIAA_dev_db"]), float(row["R_RIAA_dev_db"])
        coh = min(float(row["L_coherence"]), float(row["R_coherence"]))
        L.append((f, dl, coh))
        R.append((f, dr, coh))
        cohs.append((f, coh, dl - dr))
    freqs = np.array([r[0] for r in L])
    pb = dsp.riaa_playback_db(freqs, False)
    mean = [(f, (dl + dr) / 2 + p, coh)
            for (f, dl, coh), (f2, dr, _), p in zip(L, R, pb)]
    lr_spread = max((abs(dl - dr) for (f, dl, coh), (f2, dr, _) in zip(L, R)
                     if coh >= 0.8 and 100 <= f <= 1000), default=0.0)
    return mean, cohs, lr_spread


def at(rows, f):
    if f < rows[0][0] or f > rows[-1][0]:
        return None
    for (f0, d0, c0), (f1, d1, c1) in zip(rows, rows[1:]):
        if f0 <= f <= f1:
            r = math.log(f / f0) / math.log(f1 / f0) if f1 > f0 and f > f0 else 0.0
            return d0 + r * (d1 - d0)
    return None


def fit_hp(pts):
    best = min(((sum((d - (-10 * math.log10(1 + (fc / f) ** 2))) ** 2
                     for f, d in pts) / len(pts)), fc)
               for fc in np.geomspace(0.5, 80.0, 4000))
    return best[1], math.sqrt(best[0])


def diff_pts(ref, pos, coh_min):
    pts = []
    for f, d, coh in ref:
        if 6.0 <= f <= 200.0 and coh >= coh_min:
            v = at(pos, f)
            if v is not None:
                pts.append((f, v - d))
    return pts


def main():
    lines = []
    for series in SERIES:
        files = {p: CAP / f"{series}_{p}.csv" for p in POSITIONS}
        if not all(p.exists() for p in files.values()):
            missing = [p.name for p in files.values() if not p.exists()]
            lines.append(f"=== {series}: не хватает {', '.join(missing)} — серия пропущена ===\n")
            continue
        lines.append(f"=== Серия {series} ===")
        curves = {}
        for p in POSITIONS:
            mean, cohs, lr = read_mean(files[p])
            curves[p] = mean
            fc_raw, rms_raw = fit_hp([(f, d) for f, d, c in mean if 5 <= f <= 100 and c >= 0.6])
            bad_lr = max((abs(d) for f, coh, d in cohs if coh >= 0.8 and 100 <= f <= 1000),
                         default=0.0)
            lines.append(f"{p}: сырой угол абс. кривой ≈{fc_raw:.1f} Гц (RMS {rms_raw:.2f}; "
                         f"включает полюс карты); max|L−R| при coh≥0.8, 100–1000 Гц: {lr:.2f}/{bad_lr:.2f} дБ")
        ref = curves["P0"]
        lines.append("")
        header = "Позиция | " + " | ".join(f"{f:g} Гц" for f in TABLE_F)
        lines.append("Ослабление относительно P0 (дельта средних кривых, дБ):")
        lines.append(header)
        fits_row = {}
        for p in POSITIONS[1:]:
            row = []
            for f in TABLE_F:
                d = at(ref, f)
                v = at(curves[p], f)
                row.append(f"{v - d:+.2f}" if (d is not None and v is not None) else "—")
            lines.append(f"{p:>6} | " + " | ".join(row))
            for coh_min in FITS:
                pts = diff_pts(ref, curves[p], coh_min)
                fc, rms = fit_hp(pts)
                fits_row.setdefault(p, {})[coh_min] = (fc, rms, len(pts))
        lines.append("")
        lines.append("Эффективный угол добавочной нагрузки (подгонка дельты к P0):")
        for p in POSITIONS[1:]:
            for coh_min in FITS:
                fc, rms, n = fits_row[p][coh_min]
                lines.append(f"  {p}: coh≥{coh_min:.2f} → fc = {fc:.2f} Гц, RMS {rms:.3f} дБ ({n} точек)")
        lines.append("")
        deltas = {p: fits_row[p][0.70][0] for p in POSITIONS[1:]}
        for p, fc in deltas.items():
            others = {q: f2 for q, f2 in deltas.items() if q != p}
            dup = [q for q, f2 in others.items() if abs(f2 - fc) / fc < 0.15]
            if dup:
                lines.append(f"  ВЕРДИКТ: {p} дублирует {', '.join(dup)} (углы в пределах 15%)")
        lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nСохранено: {OUT}")


if __name__ == "__main__":
    main()
