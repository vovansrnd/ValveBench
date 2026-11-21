#!/usr/bin/env python3
"""Синтетическая проверка tools/analyze_subsonic_filter.py до подключения железа.

Генерирует два CSV в формате ValveBench noise-response (96 кГц, бины ~0.732 Гц):
  bypass.csv      — цепь с исходным выходным C1=2.2u (узел A: 100k||10k)
  filter-15Hz.csv — та же цепь + новый C2=2.2u (топология: 100k ДО C2)
Общая часть цепи (ДД/АЦП, «косой» НЧ-подъём) одинакова — в дельте должна
сокращаться. Затем запускает штатный анализатор и сверяет его таблицу с
аналитикой. Коherence ниже 3 Гц специально занижена, чтобы проверить
гейт coherence >= 0.80.
"""
import csv
import math
import random
import subprocess
import sys

OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/synth"
REPO = sys.argv[1] if len(sys.argv) > 1 else "/opt/MyApps/Adudio/ValveBench-subsonic-filter"
BIN = 96000 / 131072          # 0.732421875 Гц — бин Уэлча при 96 кГц
F_LO, F_HI = 3 * BIN, 1000.4
R_MID, C1, C2 = 100_000.0, 2.2e-6, 2.2e-6
R_LOAD = 10_000.0


def cap_db(f: float, c: float) -> float:
    z = 1.0 / (2 * math.pi * f * c)
    return z


def h0_db(f: float) -> float:
    z_c = cap_db(f, C1)
    z_a = 1.0 / (1.0 / R_MID + 1.0 / R_LOAD)
    return 20 * math.log10(z_a / math.hypot(z_c, z_a))


def h1_db(f: float) -> float:
    # 100k ДО нового C2: A = 100k || (Zc2 + 10k)
    z_c2 = cap_db(f, C2)
    mag_branch = math.hypot(z_c2, R_LOAD)
    z_a = (R_MID * mag_branch) / math.hypot(R_MID, mag_branch)  # |100k || branch| (фаза ~0)
    # точная комплексная версия для аккуратности:
    import cmath
    jw = 2j * math.pi * f
    zc1, zc2c = 1.0 / (jw * C1), 1.0 / (jw * C2)
    z_a_c = 1.0 / (1.0 / (R_MID + 0j) + 1.0 / (zc2c + R_LOAD))
    h = (z_a_c / (zc1 + z_a_c)) * (complex(R_LOAD) / (complex(R_LOAD) + zc2c))
    return 20 * math.log10(abs(h))


def chain_db(f: float) -> float:
    """Общий отклик цепи вне DUT: небольшой НЧ-подъём, сократится в дельте."""
    return 0.35 / (1.0 + (f / 25.0) ** 2)


def write_csv(path: str, resp_db) -> None:
    rng = random.Random(42)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["freq_hz", "L_RIAA_dev_db", "R_RIAA_dev_db", "L_coherence", "R_coherence"])
        f = F_LO
        while f <= F_HI:
            coh = 0.55 + 0.15 * rng.random() if f < 3.0 else 0.90 + 0.08 * rng.random()
            w.writerow([
                f,
                chain_db(f) + resp_db(f) + rng.gauss(0, 0.03) + 0.05,   # L + офсет канала
                chain_db(f) + resp_db(f) + rng.gauss(0, 0.03),          # R
                coh, min(1.0, coh + rng.uniform(-0.02, 0.02)),
            ])
            f += BIN


write_csv(f"{OUT}/bypass.csv", h0_db)
write_csv(f"{OUT}/filter-15Hz.csv", h1_db)

print("=== Штатный анализатор ===")
proc = subprocess.run(
    [sys.executable, "-I",
     f"{REPO}/tools/analyze_subsonic_filter.py",
     f"{OUT}/bypass.csv", f"{OUT}/filter-15Hz.csv",
     "--json-out", f"{OUT}/report-{REPO.split('/')[-1]}.json"],
    capture_output=True, text=True)
print(proc.stdout)
if proc.stderr:
    print("STDERR:", proc.stderr)
if proc.returncode != 0:
    sys.exit(f"анализатор завершился с кодом {proc.returncode}")

print("=== Аналитика (H1 − H0), для сверки ===")
print(f"{'f, Гц':>7} | {'дельта, дБ':>10}")
for f in (5.0, 10.0, 15.0, 20.0, 30.0, 50.0):
    print(f"{f:7.1f} | {h1_db(f) - h0_db(f):10.2f}")
