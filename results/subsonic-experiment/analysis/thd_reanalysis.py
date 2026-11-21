#!/usr/bin/env python3
"""Пересчёт THD по заданию координатора: (а) одинаковый уровень генератора,
(б) одинаковый фактический уровень основного тона на АЦП (интерполяция THD
по колонке «вх АЦП dBFS» на общих точках −6.5/−12.5/−18.5/−23.5 dBFS).
Плюс перепроверка катодного байпаса (L до/после, R — контроль) по АЧХ и THD.

Запуск из корня ветки:
  python3 results/subsonic-experiment/analysis/thd_reanalysis.py
Вывод: analysis/outputs/thd_reanalysis.txt
"""
import csv
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import numpy as np

from valvebench import dsp

THD = Path(__file__).resolve().parent.parent / "thd"
CAP = Path(__file__).resolve().parent.parent / "captures"
OUT = Path(__file__).resolve().parent / "outputs" / "thd_reanalysis.txt"

POSITIONS = ("P0", "P47k", "P22k", "P10k")
SERIES = ("rin10k", "rin33k")
TARGETS = (-6.5, -12.5, -18.5, -23.5)


def parse_thdlevel(path):
    """→ {ch: [(gen_dBFS, adc_dBFS, thd_%, thdn_%), ...]}"""
    data = {"L": [], "R": []}
    ch = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\[([LR])\]", line.strip())
        if m:
            ch = m.group(1)
            continue
        m = re.match(r"\s*(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(\d+\.?\d*)\s+(\d+\.?\d*)\s*$", line)
        if m and ch:
            data[ch].append(tuple(float(x) for x in m.groups()))
    return data


def thd_db(thd_pct):
    return 20.0 * math.log10(thd_pct)


def interp_at_adc(rows, target):
    """THD (%) при заданном уровне на АЦП; интерполяция в дБ-пространстве."""
    adc = np.array([r[1] for r in rows])
    thd = np.array([thd_db(r[2]) for r in rows])
    order = np.argsort(adc)
    adc, thd = adc[order], thd[order]
    if target < adc[0] - 0.01 or target > adc[-1] + 0.01:
        return None
    return 10 ** (np.interp(target, adc, thd) / 20.0)


lines = []

# ---------- 1. THD по 8 конфигурациям ----------
for series in SERIES:
    lines.append(f"=== Серия {series}: THD @ 997 Гц ===")
    data = {p: parse_thdlevel(THD / f"thdlevel-997-{series}_{p}.txt") for p in POSITIONS}

    for ch in ("L", "R"):
        lines.append(f"\n--- канал {ch} ---")
        lines.append("(а) Одинаковый уровень ГЕНЕРАТОРА (−3/−6/−12/−18/−24 dBFS), THD %:")
        lines.append("Позиция |   −3    |   −6    |  −12    |  −18    |  −24")
        for p in POSITIONS:
            vals = " | ".join(f"{r[2]:.5f}" for r in data[p][ch])
            lines.append(f"{p:>6} | {vals}")
        lines.append("Дельта к P0 (дБ):")
        for p in POSITIONS[1:]:
            d = [thd_db(data[p][ch][i][2]) - thd_db(data["P0"][ch][i][2]) for i in range(5)]
            lines.append(f"{p:>6} | " + " | ".join(f"{x:+.2f}" for x in d))

        lines.append(f"(б) Одинаковый фактический уровень на АЦП (интерполяция), THD %:")
        lines.append("Позиция |  −6.5   |  −12.5  |  −18.5  |  −23.5  | диапазон АЦП")
        interp = {}
        for p in POSITIONS:
            vals = [interp_at_adc(data[p][ch], t) for t in TARGETS]
            interp[p] = vals
            rng = f"{data[p][ch][0][1]:.2f}…{data[p][ch][-1][1]:.2f}"
            cells = " | ".join(f"{v:.5f}" if v is not None else "  вне  " for v in vals)
            lines.append(f"{p:>6} | {cells} | {rng}")
        lines.append("Дельта к P0 (дБ):")
        for p in POSITIONS[1:]:
            d = []
            for v0, vp in zip(interp["P0"], interp[p]):
                d.append(thd_db(vp) - thd_db(v0) if (v0 and vp) else float("nan"))
            lines.append(f"{p:>6} | " + " | ".join(f"{x:+.2f}" for x in d))
    lines.append("")

# ---------- 2. Подозрительное P22 в rin33k ----------
lines.append("=== Проверка P22 в rin33k (канал L, ген −3 dBFS: P0 0.28593 vs P22 0.32188) ===")
r33p0 = parse_thdlevel(THD / "thdlevel-997-rin33k_P0.txt")
r33p22 = parse_thdlevel(THD / "thdlevel-997-rin33k_P22k.txt")
for ch in ("L", "R"):
    lines.append(f"канал {ch}: P22/P0 по уровням генератора:")
    for i, g in enumerate((-3.0, -6.0, -12.0, -18.0, -24.0)):
        ratio = r33p22[ch][i][2] / r33p0[ch][i][2]
        lines.append(f"  ген {g:5.1f}: P0 {r33p0[ch][i][2]:.5f}"
                     f"  P22 {r33p22[ch][i][2]:.5f}"
                     f"  отношение {ratio:.3f} ({20*math.log10(ratio):+.2f} дБ)")
lines.append("Повторяемость THD той же конфигурации (контроль): R-канал до/после катодной замены")
p0r = parse_thdlevel(THD / "thdlevel-997-rin10k_P0.txt")["R"]
fixr = parse_thdlevel(THD / "thdlevel-997-cathode_L220_R470.txt")["R"]
for i, g in enumerate((-3.0, -6.0, -12.0, -18.0, -24.0)):
    lines.append(f"  ген {g:5.1f}: {p0r[i][2]:.5f} → {fixr[i][2]:.5f}"
                 f" ({20*math.log10(fixr[i][2]/p0r[i][2]):+.2f} дБ)")
lines.append("")

# ---------- 3. H2/H3 ----------
lines.append("=== H2/H3 ===")
lines.append("Полные гармонические данные есть только в двух одноточечных thd-замерах")
lines.append("(rin10k_P22k, rin10k_P10k, ген −12 dBFS) — сравнение H2/H3 между позициями")
lines.append("по сохранённым данным НЕВОЗМОЖНО (в thdlevel-файлах гармоники не пишутся).")
for f in ("thd-997-rin10k_P22k.txt", "thd-997-rin10k_P10k.txt"):
    txt = (THD / f).read_text(encoding="utf-8")
    for ch in ("L", "R"):
        m = re.search(rf"\[{ch}\].*?гармоники: (H2 [^·]+?H3 [^·]+?H4)", txt, re.S)
        if m:
            lines.append(f"{f} [{ch}]: {m.group(1).strip()}")
lines.append("")

# ---------- 4. Катодный байпас: THD L до/после, R — контроль ----------
lines.append("=== Катодный байпас: THD L до (P0, 470u) / после (220u); R — контроль ===")
p0l = parse_thdlevel(THD / "thdlevel-997-rin10k_P0.txt")["L"]
fixl = parse_thdlevel(THD / "thdlevel-997-cathode_L220_R470.txt")["L"]
lines.append("ген  | L до    | L после | ΔL дБ  | R до    | R после | ΔR дБ (контроль)")
for i, g in enumerate((-3.0, -6.0, -12.0, -18.0, -24.0)):
    dl = 20 * math.log10(fixl[i][2] / p0l[i][2])
    dr = 20 * math.log10(fixr[i][2] / p0r[i][2])
    lines.append(f"{g:5.1f}| {p0l[i][2]:.5f} | {fixl[i][2]:.5f} | {dl:+.3f} | "
                 f"{p0r[i][2]:.5f} | {fixr[i][2]:.5f} | {dr:+.3f}")
lines.append("")

# ---------- 5. Катодный байпас: АЧХ L и R отдельно ----------
lines.append("=== Катодный байпас: АЧХ отдельно L и R (fixed vs rin10k_P0), 20–800 Гц ===")

def read_deemph(path, ch):
    rows = list(csv.DictReader(open(path)))
    freqs = np.array([float(r["freq_hz"]) for r in rows])
    pb = dsp.riaa_playback_db(freqs, False)
    return [(float(r["freq_hz"]), float(r[f"{ch}_RIAA_dev_db"]) + p, float(r[f"{ch}_coherence"]))
            for r, p in zip(rows, pb)]

def at(rows, f):
    if f < rows[0][0] or f > rows[-1][0]:
        return None
    for (f0, d0, c0), (f1, d1, c1) in zip(rows, rows[1:]):
        if f0 <= f <= f1:
            r = math.log(f / f0) / math.log(f1 / f0) if f1 > f0 and f > f0 else 0.0
            return d0 + r * (d1 - d0)
    return None

newL = read_deemph(CAP / "cathode_L220u_R470u-fixed.csv", "L")
newR = read_deemph(CAP / "cathode_L220u_R470u-fixed.csv", "R")
oldL = read_deemph(CAP / "rin10k_P0.csv", "L")
oldR = read_deemph(CAP / "rin10k_P0.csv", "R")
lines.append("Гц  | ΔL (после−до) | coh L | ΔR (после−до, контроль) | coh R")
for f in (20.0, 31.5, 63.0, 125.0, 250.0, 500.0, 800.0):
    dl = at(newL, f) - at(oldL, f)
    dr = at(newR, f) - at(oldR, f)
    cl = min(c for ff, d, c in newL if abs(ff - f) < f * 0.06)
    cr = min(c for ff, d, c in newR if abs(ff - f) < f * 0.06)
    lines.append(f"{f:5.1f}| {dl:+13.2f} | {cl:5.2f} | {dr:+23.2f} | {cr:5.2f}")

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
print(f"\nСохранено: {OUT}")
