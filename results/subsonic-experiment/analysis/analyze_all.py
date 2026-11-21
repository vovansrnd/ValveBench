#!/usr/bin/env python3
"""Полный анализ захватов субсоник-эксперимента. Воспроизводит все таблицы отчёта.

Запуск из корня ветки:
  python3 results/subsonic-experiment/analysis/analyze_all.py
Зависимости: numpy, scipy; функция RIAA берётся из valvebench.dsp самого среза.
"""
import csv
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np
from scipy.optimize import minimize

from valvebench import dsp

CAP = Path(__file__).resolve().parent.parent / "captures"


def read(path):
    ch = {"L": [], "R": []}
    with open(path) as f:
        for row in csv.DictReader(f):
            for c in ("L", "R"):
                ch[c].append((float(row["freq_hz"]), float(row[f"{c}_RIAA_dev_db"]),
                              float(row[f"{c}_coherence"])))
    return ch


def at(rows, f):
    """Лог-интерполяция (dev, coh) на частоте f."""
    if f < rows[0][0] or f > rows[-1][0]:
        return None, None
    for (f0, d0, c0), (f1, d1, c1) in zip(rows, rows[1:]):
        if f0 <= f <= f1:
            r = math.log(f / f0) / math.log(f1 / f0) if f1 > f0 and f > f0 else 0.0
            return d0 + r * (d1 - d0), min(c0, c1)
    return None, None


def deemph(ch, c):
    """Реальная форма тракта: dev + riaa_playback(f) (см. замечание A1 отчёта)."""
    freqs = np.array([r[0] for r in ch[c]])
    pb = dsp.riaa_playback_db(freqs, False)
    return [(f, d + p, coh) for (f, d, coh), p in zip(ch[c], pb)]


def fit_fc(pts):
    """Подгонка однополюсного ВЧ-фильтра: минимум RMS по сетке fc."""
    best = min(((sum((d - (-10 * math.log10(1 + (fc / f) ** 2))) ** 2
                     for f, d in pts) / len(pts)), fc)
               for fc in np.geomspace(0.3, 80.0, 2000))
    return best[1], math.sqrt(best[0])


def model_delta(f, c2, r_load, rout, r_mid=100_000.0, c1=2.2e-6):
    z_c1 = 1.0 / (1j * 2 * math.pi * f * c1)
    z_c2 = 1.0 / (1j * 2 * math.pi * f * c2)
    z_a = (r_mid * (z_c2 + r_load)) / (r_mid + z_c2 + r_load)
    h1 = (z_a / (z_c1 + complex(rout, 0) + z_a)) * (r_load / (r_load + z_c2))
    z0 = complex(r_mid * r_load / (r_mid + r_load), 0)
    h0 = z0 / (z_c1 + complex(rout, 0) + z0)
    return 20 * math.log10(abs(h1)) - 20 * math.log10(abs(h0))


def sec(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


# ---------- 1. Повторяемость метода (петля) ----------
sec("1. ПОВТОРЯЕМОСТЬ МЕТОДА: две независимые петлевые захвата 60 c (loopback-a vs loopback-b)")
a, b = read(CAP / "loopback-a.csv"), read(CAP / "loopback-b.csv")
print(f"{'Гц':>6} | {'дельта L':>9} | {'дельта R':>9}")
for f in (3.0, 5.0, 8.0, 10.0, 15.0, 20.0, 31.5, 50.0, 100.0, 500.0):
    dl, _ = at(a["L"], f); dlb, _ = at(b["L"], f)
    dr, _ = at(a["R"], f); drb, _ = at(b["R"], f)
    print(f"{f:6.1f} | {dlb - dl:+9.2f} | {drb - dr:+9.2f}")

# ---------- 2. Три конфигурации тракта ----------
sec("2. ФОРМА ТРАКТА (dev + riaa_playback), канал L: петля / буфер / напрямую")
data = {}
for name, fn in (("ПЕТЛЯ", "loopback-a.csv"), ("С БУФЕРОМ", "dut-buffer.csv"),
                 ("НАПРЯМУЮ", "dut-direct.csv")):
    ch = read(CAP / fn)
    data[name] = (deemph(ch, "L"), deemph(ch, "R"))
print(f"{'Гц':>6} | {'ПЕТЛЯ':>9} | {'С БУФЕРОМ':>10} | {'НАПРЯМУЮ':>9}")
for f in (2.2, 3.0, 5.0, 8.0, 10.0, 15.0, 20.0, 40.0, 100.0, 300.0):
    row = []
    for name in data:
        rows = data[name][0]
        v, _ = at(rows, f)
        row.append(f"{v:+9.2f}" if v is not None else "       —")
    print(f"{f:6.1f} | {row[0]} | {row[1]:>10} | {row[2]}")

# ---------- 3. Нагрузка 10к: L против R в одном захвате ----------
sec("3. ШУНТ 10к В КАНАЛЕ L (bypass-10k-L.csv): L с нагрузкой, R без")
ch = read(CAP / "bypass-10k-L.csv")
L, R = deemph(ch, "L"), deemph(ch, "R")
print(f"{'Гц':>6} | {'L':>8} | {'R':>8} | {'L−R':>8} | coh L/R")
for f in (2.2, 3.0, 5.0, 8.0, 10.0, 15.0, 20.0, 25.0, 40.0, 100.0, 300.0):
    dl, cl = at(L, f); dr, cr = at(R, f)
    print(f"{f:6.1f} | {dl:+8.2f} | {dr:+8.2f} | {dl - dr:+8.2f} | {cl:.2f}/{cr:.2f}")
for name, rows in (("L", L), ("R", R)):
    pts = [(f, d) for f, d, coh in rows if 3.0 <= f <= 40.0 and coh >= 0.5]
    fc, rms = fit_fc(pts)
    r_eq = 1 / (2 * math.pi * fc * 2.2e-6) / 1000
    print(f"  {name}: fc ≈ {fc:.2f} Гц (RMS {rms:.2f} дБ) → нагрузка на 2,2 мкФ ≈ {r_eq:.1f} кОм")

# ---------- 4. Эффект добавленного C2=1.0 мкФ ----------
sec("4. ДЕЛЬТА FILTERED − BYPASS (C2=1,0 мкФ в L, нагрузка 10к), R — контроль")
byp = read(CAP / "bypass-10k-L.csv")
flt = read(CAP / "filter-1uF-10k-L.csv")
print(f"{'Гц':>6} | {'дельта L':>9} | {'дельта R':>9} | {'L−R':>8} | coh L/R")
dpts = []
for f in (3.0, 5.0, 8.0, 10.0, 12.5, 15.0, 20.0, 25.0, 31.5, 40.0, 50.0, 80.0):
    dl, cl = at(byp["L"], f); dlf, clf = at(flt["L"], f)
    dr, cr = at(byp["R"], f); drf, crf = at(flt["R"], f)
    if None in (dl, dlf, dr, drf):
        continue
    print(f"{f:6.1f} | {dlf - dl:+9.2f} | {drf - dr:+9.2f} | {dlf - dl - (drf - dr):+8.2f} | {clf:.2f}/{crf:.2f}")
    dpts.append((f, dlf - dl, clf))

fit_pts = []
for (f, d, coh) in byp["L"]:
    if 6.0 <= f <= 200.0 and coh >= 0.5:
        v, _ = at(flt["L"], f)
        if v is not None:
            fit_pts.append((f, v - d))
fc1, rms1 = fit_fc(fit_pts)
print(f"\n  Однополюсная подгонка дельты L (6–200 Гц, coh≥0.5, {len(fit_pts)} точек): "
      f"fc = {fc1:.1f} Гц, RMS {rms1:.2f} дБ")

def loss(p):
    c2, r_load, rout = p
    if not (0.3e-6 <= c2 <= 2.0e-6 and 3e3 <= r_load <= 25e3 and 0 <= rout <= 4000):
        return 1e6
    return sum((d - model_delta(f, c2, r_load, rout)) ** 2 for f, d in fit_pts) / len(fit_pts)

best = None
for c2g in (0.6e-6, 0.8e-6, 1.0e-6, 1.2e-6):
    for rlg in (6e3, 9e3, 12e3):
        r = minimize(loss, [c2g, rlg, 500.0], method="Nelder-Mead",
                     options={"xatol": 1e-9, "fatol": 1e-12, "maxiter": 2000})
        if best is None or r.fun < best.fun:
            best = r
c2, r_load, rout = best.x
print(f"  Двухполюсная подгонка: C2 = {c2 * 1e6:.2f} мкФ, R_нагр = {r_load / 1000:.1f} кОм, "
      f"Rout = {rout:.0f} Ом, RMS {math.sqrt(best.fun):.2f} дБ "
      f"(τ = C2·R = {c2 * r_load * 1e3:.1f} мс → fc ≈ {1 / (2 * math.pi * c2 * r_load):.1f} Гц)")

# ---------- 5. Полный тракт клиента ----------
sec("5. ПОЛНЫЙ ТРАКТ КЛИЕНТА (L): было/стало относительно высокоомного источника")
for f in (5.0, 10.0, 15.0, 20.0, 31.5):
    dl, _ = at(L, f)
    dlf, _ = at(deemph(flt, "L"), f)
    print(f"  {f:5.1f} Гц: без C2 {dl:+7.2f} дБ | с C2 {dlf:+7.2f} дБ | выигрыш {dlf - dl:+6.2f} дБ")
