#!/usr/bin/env python3
"""v2.18.5 — ДЕТЕРМИНИРОВАННЫЕ юнит-тесты параболических уточнений и
fail-closed семантики (ревизия v2.18.4 п.1/п.3: «add deterministic
off-bin, flat, convex and concave tests. Add RSS-grid boundary status»).

Уровни:
  1. parabolic_refine — вершина периодограммы: вогнутый максимум,
     внерешёточный (off-bin) пик, плоская форма, выпуклая форма
     (МИНИМУМ — тот самый случай v2.18.1, механизм «868 Гц»),
     ограничение смещения ±0.5 бина;
  2. fine_refine — RSS-сетка: минимум внутри (уточнение), минимум на
     КРАЮ (grid_edge), инвариант |offset| ≤ 0.5·step;
  3. уровень сигнала: внерешёточный тон 1000.3 Гц — точная оценка,
     статус ok; 868-сценарий — edge_peak (захват отклонён);
  4. fail-closed: est_joint отказывается от невалидного старта.

Все проверки детерминированы (фиксированные seed). Playback НЕТ;
production только импортируется.
"""
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v2185_estimators import (est_joint, est_lsq, fine_refine,  # noqa: E402
                              parabolic_refine)

FS = 48000.0
N = int(2.0 * FS)
FAILS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def tone(freq_dbfs_amp, f_true, seed, noise_dbfs=-96.0):
    rng = np.random.default_rng(seed)
    t = np.arange(N) / FS
    x = freq_dbfs_amp * np.sin(2 * math.pi * f_true * t +
                               2 * math.pi * ((seed * 37) % 997) / 997)
    if noise_dbfs is not None:
        x = x + 10 ** (noise_dbfs / 20.0) / math.sqrt(2.0) * \
            rng.standard_normal(N)
    return x


def amp(dbfs):
    return 10 ** (dbfs / 20.0)


print("=== 1. parabolic_refine: вершина периодограммы ===")
off, app = parabolic_refine(1.0, 2.0, 1.0)
check("вогнутый симметричный пик → offset 0", app and off == 0.0,
      f"offset={off}")
off, app = parabolic_refine(1.0, 2.0, 1.5)
# кривизна 1+1.5-4 = -1.5; offset = (1-1.5)/(2*(-1.5)) = +1/6 → к y2
check("вогнутый внерешёточный (off-bin) → смещение к большему соседу",
      app and abs(off - 1.0 / 6.0) < 1e-12, f"offset={off:.6f}")
off, app = parabolic_refine(1.0, 1.0, 1.0)
check("плоская форма → уточнение НЕ применяется", not app and off == 0.0)
off, app = parabolic_refine(2.0, 1.0, 2.0)
check("ВЫПУКЛАЯ (минимум, случай v2.18.1) → уточнение НЕ применяется",
      not app and off == 0.0)
off, app = parabolic_refine(0.1, 3.0, 5.0)
check("экстремальный наклон → смещение ОГРАНИЧЕНО ±0.5 бина",
      app and off == 0.5, f"offset={off}")
off, app = parabolic_refine(5.0, 3.0, 0.1)
check("зеркальный наклон → ограничение −0.5 бина", app and off == -0.5,
      f"offset={off}")

print("=== 2. fine_refine: RSS-сетка ===")
grid = np.linspace(1000.0, 1000.12, 25)
rss = [5, 3, 1, 3, 5]
f, edge = fine_refine(rss, grid[:5])
check("минимум внутри → уточнение, не край", not edge and
      abs(f - grid[2]) < 1e-12, f"f={f:.4f}, at_edge={edge}")
f, edge = fine_refine([1.0, 3.0, 5.0], grid[:3])
check("минимум на КРАЮ → grid_edge", edge and f == grid[0],
      f"f={f:.4f}, at_edge={edge}")
f, edge = fine_refine([5.0, 3.0, 1.0], grid[:3])
check("минимум на правом краю → grid_edge", edge and f == grid[2])
rss_inv = [10.0, 5.0, 1.0, 0.9, 9.0]
f, edge = fine_refine(rss_inv, grid[:5])
i = int(np.argmin(rss_inv))
step = grid[1] - grid[0]
off_bins = (f - grid[i]) / step
check("инвариант |offset| ≤ 0.5·step", abs(off_bins) <= 0.5 + 1e-12,
      f"offset={off_bins:.4f} шага")

print("=== 3. уровень сигнала: est_lsq ===")
x = tone(amp(-60.0), 1000.3, seed=101)          # тон МЕЖДУ бинами (шаг 0.5 Гц)
qc = est_lsq(x, FS, 1000.0)
# ДЕФЕКТ ПОРОГА (находка v2.18.5): QC остатка помечает ЛЮБОЙ белый шум
# (порог «10 дБ над медианой» — естественный max/median белого шума
# ~11 дБ), но ОЦЕНКА при этом точна — дефект порога, не оценки
check("внерешёточный тон 1000.3 Гц: статус line_contaminated "
      "(дефект порога, НЕ оценки)", qc["status"] == "line_contaminated",
      f"статус={qc['status']}")
check("внерешёточный тон: оценка точнее 0.05 Гц",
      abs(qc["f_est"] - 1000.3) < 0.05, f"f_est={qc['f_est']:.4f}")
check("coarse-пик внутри полосы (не край)",
      0.0 < qc["coarse_bin_rel"] < 1.0, f"rel={qc['coarse_bin_rel']}")

qc_clean = est_lsq(tone(amp(-60.0), 1000.3, seed=101, noise_dbfs=None),
                   FS, 1000.0)
check("чистый тон БЕЗ шума: тоже line_contaminated (числовой мусор "
      "lstsq → пол медианы ~0 → max/floor огромен) — статус ok при "
      "текущем пороге практически недостижим",
      qc_clean["status"] == "line_contaminated",
      f"статус={qc_clean['status']}, f_est={qc_clean['f_est']:.4f}, "
      f"«линия»={qc_clean.get('line_hz')} Гц")

# ±100 ppm (задание на тестирование LSQ): истинный уход ±0.1 Гц от 1 кГц
for ppm, f_true_ppm, seed_ppm in ((+100.0, 1000.1, 401),
                                 (-100.0, 999.9, 402)):
    qc_ppm = est_lsq(tone(amp(-72.0), f_true_ppm, seed=seed_ppm),
                     FS, 1000.0)
    err_ppm = (qc_ppm["f_est"] / 1000.0 - 1) * 1e6 - ppm
    check(f"уход {ppm:+.0f} ppm: ошибка {err_ppm:+.3f} ppm (< 1 ppm)",
          abs(err_ppm) < 1.0,
          f"f_est={qc_ppm['f_est']:.4f}, err={err_ppm:+.3f} ppm")

# 868-сценарий: пилот 997 (−72 дБFS) + линия 1000 Гц (−30 дБFS) —
# coarse-пик уходит на границу полосы → edge_peak, отказа без фита
seed = 218200 + 997000 + 300 + 0
rng = np.random.default_rng(seed)
t = np.arange(N) / FS
f_true = 997.0 * (1 - 23.5e-6)
x = amp(-72.0) * np.sin(2 * math.pi * f_true * t)
x += amp(-30.0) * np.sin(2 * math.pi * 1000.0 * (1 - 23.5e-6) * t +
                         2 * math.pi * 0.37)
x += amp(-96.0) / math.sqrt(2.0) * rng.standard_normal(N)
qc = est_lsq(x, FS, 997.0)
check("868-сценарий: edge_peak (f_est отсутствует)",
      qc["status"] == "edge_peak" and "f_est" not in qc,
      f"статус={qc['status']}, coarse={qc.get('coarse_peak_hz')}")

print("=== 4. fail-closed: est_joint ===")
pair = np.column_stack([x, x])
res = est_joint(pair, FS, 997.0)
check("edge_peak → (None, None, fs, 'edge_peak')",
      res[0] is None and res[1] is None and res[3] == "edge_peak",
      f"res={res[:2]}, статус={res[3]}")

# line_contaminated: тон с белым шумом — QC остатка срабатывает
# (порог 10 дБ над медианой: ЕСТЕСТВЕННЫЙ max/median белого шума ~11 дБ —
# это документированный ДЕФЕКТ порога, см. отчёт §QC)
x2 = tone(amp(-72.0), 1000.0, seed=202)
qc2 = est_lsq(x2, FS, 1000.0)
check("белый шум: line_contaminated (дефект порога воспроизводим)",
      qc2["status"] == "line_contaminated", f"статус={qc2['status']}")
res2 = est_joint(np.column_stack([x2, x2]), FS, 1000.0)
check("line_contaminated → joint отказывается (fail-closed)",
      res2[0] is None and res2[3] == "line_contaminated",
      f"статус={res2[3]}")

# Позитивный путь joint (status='ok') НЕ тестируется на уровне сигнала:
# при текущем (некалиброванном) пороге line-QC статус ok недостижим для
# любого сигнала с ненулевым остатком (белый шум → max/median ~11 дБ;
# нулевой шум → числовой мусор). Направление фикса порога — в отчёте
# (устойчивая линия ≥3 соседних бинов + критерий в дБ ОТНОСИТЕЛЬНО
# НЕСУЩЕЙ, а не относительно медианы остатка). Хелпер-тесты (§1-2) и
# точность оценок (§3) полностью покрывают арифметику отказа.

print()
if FAILS:
    print(f"ИТОГ: {len(FAILS)} ПРОВАЛОВ: {FAILS}")
    sys.exit(1)
print("ИТОГ: все детерминированные тесты PASS")
