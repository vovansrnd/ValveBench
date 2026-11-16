#!/usr/bin/env python3
"""v2.18.6 — детерминированные юнит-тесты: ОБА пути QC (принятие и отказ),
конвенция CSD с известной фазой, joint по обоим каналам.

Ревизия v2.18.5 (журнал §20.7 п.1/2/3/5):
  * НОРМАЛЬНЫЙ путь ok: чистый синус, белый шум, цветной шум (без линий);
  * ОБНАРУЖЕНИЕ интерференции: пилот 997 + помеха 1000 Гц (−30 дБFS,
    контрпример −127 ppm), сдвиги ±3 и ±5 Гц;
  * КОНВЕНЦИЯ CSD: csd(L,R) = conj(FFT(L))·FFT(R); синтез R = g·L с
    измеренным g = S_LR/S_LL (БЕЗ сопряжения) воспроизводит известную
    межканальную фазу +0.37 рад;
  * joint: старт из валидного канала при отказе другого (ok_partial),
    отказ при отказе обоих.

Все проверки детерминированы (фиксированные seed). Playback НЕТ.
"""
import math
import sys
from pathlib import Path

import numpy as np
from scipy.signal import csd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v2186_estimators import (est_joint, est_lsq, fine_refine,  # noqa: E402
                              parabolic_refine, residual_line_qc)

FS = 48000.0
N = int(2.0 * FS)
FAILS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def amp(dbfs):
    return 10 ** (dbfs / 20.0)


def tone(f_true, level_dbfs, seed, noise_dbfs=-96.0):
    rng = np.random.default_rng(seed)
    t = np.arange(N) / FS
    x = amp(level_dbfs) * np.sin(2 * math.pi * f_true * t +
                                 2 * math.pi * ((seed * 37) % 997) / 997)
    if noise_dbfs is not None:
        x = x + amp(noise_dbfs) / math.sqrt(2.0) * rng.standard_normal(N)
    return x


print("=== 1. parabolic_refine / fine_refine (регресс v2.18.5) ===")
off, app = parabolic_refine(1.0, 2.0, 1.0)
check("вогнутый симметричный пик → offset 0", app and off == 0.0)
off, app = parabolic_refine(2.0, 1.0, 2.0)
check("ВЫПУКЛАЯ (минимум, механизм 868) → НЕ применяется", not app)
off, app = parabolic_refine(0.1, 3.0, 5.0)
check("экстремальный наклон → клип ±0.5 бина", app and off == 0.5)
f, edge = fine_refine([5, 3, 1, 3, 5], np.linspace(1000, 1000.12, 5))
check("минимум RSS внутри → уточнение", not edge and
      abs(f - 1000.06) < 1e-12, f"f={f:.4f}")
f, edge = fine_refine([1.0, 3.0, 5.0], np.linspace(1000, 1000.12, 3))
check("минимум RSS на КРАЮ → grid_edge", edge and f == 1000.0)

print("=== 2. НОРМАЛЬНЫЙ путь ok (ревизия п.2: обязательные контроли) ===")
# окно фита ±0.15 Гц = ±150 ppm при 1 кГц (якорь на известный номинал)
qc = est_lsq(tone(1000.1, -60.0, seed=101, noise_dbfs=None), FS, 1000.0)
check("чистый синус +100 ppm БЕЗ шума → status ok",
      qc["status"] == "ok", f"статус={qc['status']}, f_est={qc.get('f_est')}")
check("и оценка точна (100 ppm)", abs(qc["f_est"] - 1000.1) < 0.02,
      f"f_est={qc['f_est']:.4f}")
qc = est_lsq(tone(1000.3, -60.0, seed=101, noise_dbfs=None), FS, 1000.0)
check("уход +300 ppm (вне окна фита) → grid_edge, отказ (проектная "
      "граница ±150 ppm)", qc["status"] == "grid_edge",
      f"статус={qc['status']}")
qc = est_lsq(tone(1000.0, -72.0, seed=102, noise_dbfs=-96.0), FS, 1000.0)
check("тон −72 дБFS + белый шум −96 → ok (ложных линий нет)",
      qc["status"] == "ok", f"статус={qc['status']}, "
      f"line={qc.get('line_hz')}/{qc.get('line_dbc')} дБc")
check("ошибка ≤ 1 ppm (сигнал без ухода, err = f_est − 1000)",
      abs((qc["f_est"] / 1000.0 - 1) * 1e6) < 1.0,
      f"err={((qc['f_est'] / 1000.0 - 1) * 1e6):+.3f} ppm")
# серия белого шума: FP должен быть редок
rng_seeds = range(300, 340)
fp = sum(1 for s in rng_seeds
         if est_lsq(tone(1000.0, -72.0, seed=s, noise_dbfs=-96.0),
                    FS, 1000.0)["status"] != "ok")
check("белый шум, 40 прогонов: FP ≤ 1", fp <= 1, f"FP={fp}/40")

print("=== 3. ОБНАРУЖЕНИЕ интерференции (ревизия п.3) ===")
# точный контрпример v2.18.4: пилот 997 −72 дБFS + линия 1000 Гц −30 дБFS
seed = 218200 + 997000 + 300 + 0
rng = np.random.default_rng(seed)
t = np.arange(N) / FS
f_true = 997.0 * (1 - 23.5e-6)
x = amp(-72.0) * np.sin(2 * math.pi * f_true * t)
x += amp(-30.0) * np.sin(2 * math.pi * 1000.0 * (1 - 23.5e-6) * t +
                         2 * math.pi * 0.37)
x += amp(-96.0) / math.sqrt(2.0) * rng.standard_normal(N)
qc = est_lsq(x, FS, 997.0)
check("контрпример 997+1000 (−30 дБFS): line_contaminated",
      qc["status"] == "line_contaminated", f"статус={qc['status']}")
check("линия найдена У ПОМЕХИ (не маскирована ±5 Гц)",
      qc["line_hz"] is not None and abs(qc["line_hz"] - 1000.0) <= 1.0,
      f"line_hz={qc['line_hz']}, {qc['line_dbc']} дБc, "
      f"{qc['line_bins']} бинов")

for off_hz, lvl in ((+3, -30.0), (-3, -30.0), (+5, -30.0), (-5, -30.0),
                    (+3, -50.0), (+3, -60.0)):
    seed_i = 500000 + int(abs(off_hz) * 1000) + int(-lvl)
    rng = np.random.default_rng(seed_i)
    f_line = 997.0 + off_hz * (1 - 23.5e-6)
    x = amp(-72.0) * np.sin(2 * math.pi * f_true * t + 0.11)
    x += amp(lvl) * np.sin(2 * math.pi * f_line * t + 2 * math.pi * 0.63)
    x += amp(-96.0) / math.sqrt(2.0) * rng.standard_normal(N)
    qc = est_lsq(x, FS, 997.0)
    near = (qc["line_hz"] is not None and
            abs(qc["line_hz"] - f_line) <= 1.5)
    # сильная помеха (−30/−50 дБFS, ≥32/12 дБ над пилотом): ландшафт RSS
    # повреждён → фит отказывает (grid_edge), но QC НАЗЫВАЕТ помеху —
    # отказ с атрибуцией; оценка не публикуется в принятые
    if lvl >= -50.0:
        check(f"помеха 997{off_hz:+d} Гц ({lvl:.0f} дБFS): отказ "
              f"(line_contaminated), линия у помехи",
              qc["status"] == "line_contaminated" and near,
              f"статус={qc['status']}, line={qc['line_hz']} Гц "
              f"{qc['line_dbc']} дБc")
    # слабая помеха (−60 дБFS, +12 дБc): ландшафт чист, фит внутри окна
    else:
        est_ok = (qc.get("f_est") is not None and
                  abs(qc["f_est"] - f_true) < 0.15)
        check(f"помеха 997{off_hz:+d} Гц ({lvl:.0f} дБFS): отказ "
              f"(line_contaminated), линия у помехи, фит внутри окна",
              qc["status"] == "line_contaminated" and near and est_ok,
              f"статус={qc['status']}, line={qc['line_hz']} Гц "
              f"{qc['line_dbc']} дБc, f_est={qc.get('f_est')}")

print("=== 4. конвенция CSD: известная межканальная фаза (ревизия п.1) ===")
fs_t, n_t = FS, N
t = np.arange(n_t) / fs_t
L = np.sin(2 * math.pi * 1000.0 * t)
R = np.sin(2 * math.pi * 1000.0 * t + 0.37)      # R опережает на +0.37
f, S = csd(L, R, fs_t, nperseg=n_t)
k = int(np.argmin(np.abs(f - 1000.0)))
check("csd(L, R=сдвиг+0.37) фаза = +0.37", abs(np.angle(S[k]) - 0.37) < 1e-6,
      f"{np.angle(S[k]):+.4f}")
# генератор: g = S_LR/S_LL БЕЗ сопряжения → синтез воспроизводит фазу
g = S[k] / (np.abs(np.fft.rfft(L)[k]) ** 2 / n_t)
R2 = np.real(np.fft.irfft(g * np.fft.rfft(L), n_t))
_, S2 = csd(L, R2, fs_t, nperseg=n_t)
check("синтез R = (S_LR/S_LL)·L: csd фаза +0.37 (конвенция верна)",
      abs(np.angle(S2[k]) - 0.37) < 0.01, f"{np.angle(S2[k]):+.4f}")
g_bad = np.conj(g)
R3 = np.real(np.fft.irfft(g_bad * np.fft.rfft(L), n_t))
_, S3 = csd(L, R3, fs_t, nperseg=n_t)
check("вариант с conj(g) действительно даёт −0.37 (ошибка v2.18.5)",
      abs(np.angle(S3[k]) + 0.37) < 0.01, f"{np.angle(S3[k]):+.4f}")

print("=== 5. joint по ОБОИМ каналам (ревизия п.5) ===")
# оба канала чистые с шумом → ok
xl = tone(1000.0, -72.0, seed=601)
xr = tone(1000.0, -72.0, seed=602)
res = est_joint(np.column_stack([xl, xr]), FS, 1000.0)
check("оба канала ok → joint ok", res[0] is not None and res[3] == "ok",
      f"статус={res[3]}")
# L контаминирован (сильная линия), R чист → ok_partial, старт из R
xl_c = tone(1000.0, -72.0, seed=601)
rng = np.random.default_rng(701)
xl_c = xl_c + amp(-30.0) * np.sin(2 * math.pi * 1050.0 * t +
                                  2 * math.pi * rng.random())
res = est_joint(np.column_stack([xl_c, xr]), FS, 1000.0)
check("L контаминирован, R чист → ok_partial (старт из R)",
      res[0] is not None and res[3] == "ok_partial",
      f"статус={res[3]}, f={res[0]:.3f}" if res[0] else f"статус={res[3]}")
# оба контаминированы → отказ
xr_c = tone(1000.0, -72.0, seed=602)
rng = np.random.default_rng(702)
xr_c = xr_c + amp(-30.0) * np.sin(2 * math.pi * 1050.0 * t +
                                  2 * math.pi * rng.random())
res = est_joint(np.column_stack([xl_c, xr_c]), FS, 1000.0)
check("оба контаминированы → отказ (None, None, fs, статус)",
      res[0] is None and res[1] is None and
      res[3] == "line_contaminated", f"статус={res[3]}")
# 868-сценарий (гребёнка 50·k −30 дБFS): якорный фит не телепортируется
# (механизм 868 исключён), гребёнка НАЗЫВАЕТСЯ QC, joint отказывается
seed = 218200 + 997000 + 300 + 0
rng = np.random.default_rng(seed)
t192 = np.arange(192000) / 192000
f_true = 997.0 * (1 - 23.5e-6)
x868 = amp(-72.0) * np.sin(2 * math.pi * f_true * t192)
for kk in range(1, 41):
    fh = 50.0 * kk
    if fh >= 96000:
        break
    x868 += amp(-30.0) * np.sin(2 * math.pi * fh * t192 +
                                2 * math.pi * rng.random())
x868 += amp(-96.0) / math.sqrt(2.0) * rng.standard_normal(192000)
qc = est_lsq(x868, 192000.0, 997.0)
comb_line = (qc["line_hz"] is not None and
             abs(qc["line_hz"] / 50.0 - round(qc["line_hz"] / 50.0)) < 0.1)
in_window = (qc.get("f_est") is not None and
             abs(qc["f_est"] - 997.0) <= 0.16)
check("868-сценарий: line_contaminated (гребёнка названа), фит внутри "
      "окна (телепорт исключён), coarse_edge=True",
      qc["status"] == "line_contaminated" and comb_line and
      qc.get("coarse_edge") is True and in_window,
      f"статус={qc['status']}, line={qc.get('line_hz')} Гц, "
      f"coarse={qc.get('coarse_peak_hz')}, coarse_edge={qc.get('coarse_edge')}, "
      f"f_est={qc.get('f_est')}")
res = est_joint(np.column_stack([x868, x868]), 192000.0, 997.0)
check("868: joint fail-closed (line_contaminated)",
      res[0] is None and res[3] == "line_contaminated", f"статус={res[3]}")

print()
if FAILS:
    print(f"ИТОГ: {len(FAILS)} ПРОВАЛОВ: {FAILS}")
    sys.exit(1)
print("ИТОГ: все детерминированные тесты PASS")
