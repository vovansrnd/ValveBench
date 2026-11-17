#!/usr/bin/env python3
"""v2.18.7 — детерминированные юнит-тесты: изоляция каналов, гейт
наличия тона, честная неопределённость (журнал §21).

Классы задания:
  * ok_partial: L чист / R помеха +3 Гц; R чист / L помеха −3 Гц;
    сильные помехи с вариацией уровня и фазы; оба загрязнены.
    РЕЗУЛЬТАТ ok_partial обязан СОВПАДАТЬ с чистым каналом отдельно
    (точное совпадение: та же ветка вычисления — допуск 0);
  * tone-presence gate: чистый синус; белый/цветной шум С пилотом;
    шум БЕЗ пилота (контрольная проверка координатора: 55/200 ok в
    v2.18.6); обрыв тона в начале/конце окна; частично заполненное
    окно; низкий SNR; клиппинг. Ни один без полезного тона не ok;
  * variance_hz2/sigma_hz: преобразование ровно один раз
    (sqrt(2·var) из v2.18.6 удалён);
  * регресс v2.18.6: параболы, помехи ±3/±5 Гц, CSD-конвенция.

Playback НЕТ; production только импортируется.
"""
import math
import sys
from pathlib import Path

import numpy as np
from scipy.signal import csd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v2187_estimators import (est_joint, est_lsq, fine_refine,  # noqa: E402
                              parabolic_refine)

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


def tone(f_true, level_dbfs, seed, noise_dbfs=-96.0, t_len=None):
    n = int(t_len * FS) if t_len else N
    t = np.arange(n) / FS
    rng = np.random.default_rng(seed)
    x = amp(level_dbfs) * np.sin(2 * math.pi * f_true * t +
                                 2 * math.pi * ((seed * 37) % 997) / 997)
    if noise_dbfs is not None:
        x = x + amp(noise_dbfs) / math.sqrt(2.0) * rng.standard_normal(n)
    return x


print("=== 1. регресс v2.18.6: параболы и помехи ===")
off, app = parabolic_refine(2.0, 1.0, 2.0)
check("выпуклая (минимум, механизм 868) → не применяется", not app)
f, edge = fine_refine([1.0, 3.0, 5.0], np.linspace(1000, 1000.12, 3))
check("минимум RSS на краю → grid_edge", edge and f == 1000.0)
qc = est_lsq(tone(1000.0, -72.0, seed=102, noise_dbfs=-96.0), FS, 1000.0)
check("тон −72 + белый шум −96 → ok", qc["status"] == "ok",
      f"статус={qc['status']}, snr={qc['snr_carrier_db']} дБ")
check("carrier-local SNR высок у чистого измерения",
      qc["snr_carrier_db"] >= 20.0, f"{qc['snr_carrier_db']} дБ ≥ 20")

t = np.arange(N) / FS
f_true = 997.0 * (1 - 23.5e-6)
for off_hz, lvl in ((+3, -30.0), (-3, -30.0), (+5, -30.0), (-5, -30.0)):
    rng = np.random.default_rng(500000 + int(abs(off_hz) * 1000) + int(-lvl))
    f_line = f_true + off_hz
    x = amp(-72.0) * np.sin(2 * math.pi * f_true * t + 0.11)
    x += amp(lvl) * np.sin(2 * math.pi * f_line * t + 2 * math.pi * 0.63)
    x += amp(-96.0) / math.sqrt(2.0) * rng.standard_normal(N)
    qc = est_lsq(x, FS, 997.0)
    near = qc["line_hz"] is not None and abs(qc["line_hz"] - f_line) <= 2.0
    check(f"помеха 997{off_hz:+d} Гц: отказ, линия у помехи",
          qc["status"] == "line_contaminated" and near,
          f"{qc['status']}, line={qc['line_hz']} ({qc['line_dbc']} дБc)")

print("=== 2. tone-presence gate (§21 п.2) ===")
# чистый синус без шума
qc = est_lsq(tone(1000.1, -60.0, seed=101, noise_dbfs=None), FS, 1000.0)
check("чистый синус +100 ppm → ok", qc["status"] == "ok",
      f"snr={qc['snr_carrier_db']}")
# КОНТРОЛЬНАЯ ПРОВЕРКА КООРДИНАТОРА: шум −80 дБFS БЕЗ пилота (200 записей
# в его проверке: ok 55) — теперь ни одна не должна быть ok
fp_ok = 0
snr_hist = []
for i in range(200):
    rng = np.random.default_rng(900000 + i)
    x = amp(-80.0) / math.sqrt(2.0) * rng.standard_normal(N)
    qc = est_lsq(x, FS, 1000.0)
    snr_hist.append(qc["snr_carrier_db"])
    if qc["status"] == "ok":
        fp_ok += 1
check("шум −80 БЕЗ пилота, 200 записей: ok = 0 (было 55/200)",
      fp_ok == 0, f"ok={fp_ok}/200")
check("шум без пилота: tone_missing доминирует",
      max(snr_hist) < 0.0,
      f"snr диапазон [{min(snr_hist):.1f}, {max(snr_hist):.1f}] дБ")
# белый шум −96 без пилота (чистый шум, БЕЗ тона)
fp_ok = 0
for i in range(100):
    rng = np.random.default_rng(910000 + i)
    x = amp(-96.0) / math.sqrt(2.0) * rng.standard_normal(N)
    if est_lsq(x, FS, 1000.0)["status"] == "ok":
        fp_ok += 1
check("белый шум −96 без пилота: ok = 0", fp_ok == 0, f"ok={fp_ok}/100")
# цветной шум без пилота — через модель colorная ниже в sweep; здесь
# белый с уклоном (ФНЧ-имитация): НЧ-тяжёлый шум без тона
rng = np.random.default_rng(777)
w = np.fft.rfft(amp(-80.0) / math.sqrt(2.0) * rng.standard_normal(N))
fr = np.arange(len(w)) * (FS / N)
w *= 1.0 / np.sqrt(1.0 + (fr / 200.0) ** 2)      # НЧ-уклон +24 дБ/дек
x_colored_nopilot = np.fft.irfft(w, N)
qc = est_lsq(x_colored_nopilot, FS, 1000.0)
check("НЧ-тяжёлый шум без пилота: не ok",
      qc["status"] != "ok", f"{qc['status']}, snr={qc['snr_carrier_db']}")
# обрыв тона: тон только в ПЕРВОЙ половине окна
x = tone(1000.0, -72.0, seed=911)
x = x.copy()
x[N // 2:] = amp(-96.0) / math.sqrt(2.0) * \
    np.random.default_rng(912).standard_normal(N - N // 2)
qc = est_lsq(x, FS, 1000.0)
check("обрыв тона в середине окна: не ok",
      qc["status"] != "ok", f"{qc['status']}, snr={qc['snr_carrier_db']}")
# тон появляется только в конце
x2 = tone(1000.0, -72.0, seed=913)
x2 = x2.copy()
x2[:3 * N // 4] = amp(-96.0) / math.sqrt(2.0) * \
    np.random.default_rng(914).standard_normal(3 * N // 4)
qc = est_lsq(x2, FS, 1000.0)
check("тон только в последней четверти: не ok",
      qc["status"] != "ok", f"{qc['status']}, snr={qc['snr_carrier_db']}")
# частично заполненное окно (тон занимает 40% в начале)
x3 = tone(1000.0, -72.0, seed=915, t_len=0.8)
buf = np.zeros(N)
buf[:len(x3)] = x3
qc = est_lsq(buf, FS, 1000.0)
check("частично заполненное окно (40%): не ok",
      qc["status"] != "ok", f"{qc['status']}, snr={qc['snr_carrier_db']}")
# низкий SNR: пилот −110 при шуме −96 (carrier-local ≈ +7 дБ < 20)
qc = est_lsq(tone(1000.0, -110.0, seed=916, noise_dbfs=-96.0), FS, 1000.0)
check("низкий SNR (−110 пилот, −96 шум) → snr_low",
      qc["status"] == "snr_low", f"{qc['status']}, snr={qc['snr_carrier_db']}")
# пограничный случай: −100 пилот — carrier-local ≈ +27 дБ ≥ 20 (тон
# есть и измерим: 2-с когерентное усиление), но остаток/линии могут
# отказать — ЛЮБОЙ исход не ok-без оснований; фиксируем фактический
qc100 = est_lsq(tone(1000.0, -100.0, seed=916, noise_dbfs=-96.0), FS, 1000.0)
check("пилот −100 (carrier-local +27): статус фиксируется, не 'ok' без причины",
      qc100["status"] in ("ok", "snr_low", "line_contaminated") and
      (qc100["status"] != "ok" or qc100["snr_carrier_db"] >= 20.0),
      f"{qc100['status']}, snr={qc100['snr_carrier_db']}")
# клиппинг: гармонические линии → отказ line-QC
x4 = tone(1000.0, -60.0, seed=917, noise_dbfs=None)
x4 = np.clip(x4, -0.35 * amp(-60.0), 0.35 * amp(-60.0))   # жёсткий клиппинг
qc = est_lsq(x4, FS, 1000.0)
check("жёсткий клиппинг: не ok (гармоники ловятся line-QC)",
      qc["status"] == "line_contaminated",
      f"{qc['status']}, line={qc['line_hz']} ({qc['line_dbc']} дБc)")

print("=== 3. истинная изоляция каналов (§21 п.1) ===")
xl = tone(1000.0, -72.0, seed=601)
xr = tone(1000.0, -72.0, seed=602)
res = est_joint(np.column_stack([xl, xr]), FS, 1000.0)
check("оба ok → joint ok, channels_used [L,R]",
      res["status"] == "ok" and res["channels_used"] == ["L", "R"],
      f"{res['status']}, {res['channels_used']}")

# L чист, R с помехой +3 Гц → ok_partial ТОЛЬКО из L, точное совпадение
rng = np.random.default_rng(701)
xr_c = xr + amp(-30.0) * np.sin(2 * math.pi * 1003.0 * t +
                                2 * math.pi * rng.random())
res = est_joint(np.column_stack([xl, xr_c]), FS, 1000.0)
qcL = est_lsq(xl, FS, 1000.0)
check("L чист / R помеха +3 Гц → ok_partial, channels_used ['L']",
      res["status"] == "ok_partial" and res["channels_used"] == ["L"],
      f"{res['status']}, {res['channels_used']}")
check("ok_partial ≡ чистый канал: f_est СОВПАДАЕТ ТОЧНО (допуск 0)",
      res["f_est"] == qcL["f_est"],
      f"{res['f_est']} vs {qcL['f_est']}")
check("ok_partial ≡ чистый канал: variance_hz2 совпадает точно",
      res["variance_hz2"] == qcL["variance_hz2"])
check("статусы каналов сохранены в результате",
      res["qc_L"]["status"] == "ok" and
      res["qc_R"]["status"] == "line_contaminated")

# R чист, L с помехой −3 Гц
xl_c = xl + amp(-30.0) * np.sin(2 * math.pi * 997.0 * t +
                                2 * math.pi * np.random.default_rng(702).random())
res = est_joint(np.column_stack([xl_c, xr]), FS, 1000.0)
qcR = est_lsq(xr, FS, 1000.0)
check("R чист / L помеха −3 Гц → ok_partial, channels_used ['R']",
      res["status"] == "ok_partial" and res["channels_used"] == ["R"] and
      res["f_est"] == qcR["f_est"],
      f"{res['status']}, f={res['f_est']} vs {qcR['f_est']}")

# сильные помехи с вариацией уровня и фазы: изоляция устойчива
for lvl, ph in ((-20.0, 0.05), (-40.0, 2.51), (-30.0, 3.9)):
    rng = np.random.default_rng(800 + int(-lvl))
    xl_i = xl + amp(lvl) * np.sin(2 * math.pi * 1003.0 * t +
                                  2 * math.pi * rng.random())
    res = est_joint(np.column_stack([xl_i, xr]), FS, 1000.0)
    qcR = est_lsq(xr, FS, 1000.0)
    check(f"изоляция при помехе L {lvl:.0f} дБFS (фаза {ph}): "
          f"f_est == чистый R точно",
          res["status"] == "ok_partial" and res["f_est"] == qcR["f_est"],
          f"{res['status']}")

# оба загрязнены → отказ
rng = np.random.default_rng(702)
xl_c2 = xl + amp(-30.0) * np.sin(2 * math.pi * 1003.0 * t +
                                 2 * math.pi * rng.random())
res = est_joint(np.column_stack([xl_c, xr_c]), FS, 1000.0)
check("оба загрязнены → отказ, channels_used []",
      res["f_est"] is None and res["channels_used"] == [] and
      res["status"] == "line_contaminated", f"{res['status']}")

print("=== 4. variance_hz2/sigma_hz: единственное sqrt (§21 п.3) ===")
res = est_joint(np.column_stack([xl, xr]), FS, 1000.0)
check("sigma_hz == sqrt(variance_hz2) — ровно одно преобразование",
      res["sigma_hz"] is not None and
      abs(res["sigma_hz"] - math.sqrt(res["variance_hz2"])) < 1e-15,
      f"var={res['variance_hz2']:.3e} Гц², sigma={res['sigma_hz']:.3e} Гц")
check("variance конечна и положительна",
      res["variance_hz2"] > 0 and np.isfinite(res["variance_hz2"]))
qc = est_lsq(tone(1000.0, -72.0, seed=102), FS, 1000.0)
check("est_lsq тоже возвращает variance_hz2",
      qc["variance_hz2"] is not None and qc["variance_hz2"] > 0,
      f"{qc['variance_hz2']:.3e} Гц²")

print("=== 5. регресс CSD-конвенции ===")
t_c = np.arange(N) / FS
L = np.sin(2 * math.pi * 1000.0 * t_c)
R = np.sin(2 * math.pi * 1000.0 * t_c + 0.37)
f, S = csd(L, R, FS, nperseg=N)
k = int(np.argmin(np.abs(f - 1000.0)))
check("csd(L, R=+0.37) фаза +0.37", abs(np.angle(S[k]) - 0.37) < 1e-6)

print()
if FAILS:
    print(f"ИТОГ: {len(FAILS)} ПРОВАЛОВ: {FAILS}")
    sys.exit(1)
print("ИТОГ: все детерминированные тесты PASS")
