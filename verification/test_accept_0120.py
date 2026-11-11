#!/usr/bin/env python3
"""Приёмочные проверки 0.1.20-dev3-reviewfix2 по ТЗ (разделы 3-4, 6-7)."""
from __future__ import annotations

import math
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from valvebench import gainmatch
from valvebench.calib import Calibration
from valvebench.measure import Rig
from valvebench.backend import SimBackend, SimChannel

RES = []


def check(name, cond, detail=""):
    RES.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ("\n      " + detail if detail else ""))


FS = 48000


def ring(f, dur=0.6, level=-20.0, noise=None, seed=1, ppm=0.0):
    n = int(dur * FS)
    t = np.arange(n) / FS
    fr = f * (1 + ppm * 1e-6)
    s = 10 ** (level / 20) * np.sin(2 * np.pi * fr * t)
    if noise:
        rng = np.random.default_rng(seed)
        s = s + rng.standard_normal(n) * 10 ** (noise / 20) / math.sqrt(2)
    return np.column_stack([s, s])


# ===== §3.2 C/R: двойной учёт hum в Static (calc_carrier_residual_db) =====
def cr_hum_double_count():
    # Тон −24, сеть 50 Гц −50 dBFS, шум −110. noise_dbfs анализатора НЕ содержит
    # hum (он вычтен и отчитан отдельно). Правильный C/R = level − 10lg(noise+hum).
    n = int(2.0 * FS)
    t = np.arange(n) / FS
    x = (10 ** (-24 / 20) * np.sin(2 * np.pi * 1000 * t)
         + 10 ** (-50 / 20) * np.sin(2 * np.pi * 50 * t)
         + 10 ** (-110 / 20) / math.sqrt(2) * np.random.default_rng(3).standard_normal(n))
    from valvebench import dsp as dsp_mod
    an = dsp_mod.analyze_tone(x, FS, 1000.0, n, nharm=9, mains=50.0)
    cr = gainmatch.calc_carrier_residual_db(an["level_dbfs"], an)
    # ожидание: уровень −24; остаток = шум(−110)+hum(−50) → −49.96 dBFS
    p_n, p_h = 10 ** (-110 / 10), 10 ** (-50 / 10)
    expected_cr = -24.0 - 10 * math.log10(p_n + p_h)
    ok = abs(cr - expected_cr) < 0.5
    # двойной учёт НЕ допускается: C/R не должен быть ниже из-за второго вхождения hum
    check("CR-HUM: Static C/R включает hum ровно один раз "
          f"({cr:.1f} дБ, ожидалось {expected_cr:.1f})", ok,
          f"noise_dbfs={an['noise_dbfs']:.1f}, hum50={an['hum'][0]['dbfs']:.1f}")


# ===== §3.2 C/R не зависит от длины FFT (размер окна серии) =====
def cr_fft_size():
    vals = []
    for blocks in (1, 2, 4):
        be = SimBackend(SimChannel(gain=1.0, noise_dbfs=-90, hum50_dbfs=-130, hum100_dbfs=-130),
                        SimChannel(gain=1.0, noise_dbfs=-90, hum50_dbfs=-130, hum100_dbfs=-130),
                        seed=11)
        rig = Rig(be, Calibration(), fs=FS, blocks=blocks, settle=0.15)
        rig.open()
        res = gainmatch.measure_series(rig, frequencies=(1000.0, 1500.0),
                                       level_dbfs=-24.0, repeats=1, purpose="loopback")
        vals.append(res["rows"][0]["channels"]["L"]["carrier_residual_db"])
    ok = max(vals) - min(vals) < 3.0 and all(v > 40 for v in vals)
    check("CR-FFT: C/R стабилен при разном числе блоков окна "
          f"({['%.1f' % v for v in vals]})", ok)


# ===== §3.3 TARE: 10 сценариев через контекст =====
def tare_scenarios():
    tr = gainmatch.GainTracker()
    base = {"freq": 1000.0, "level_dbfs": -20.0, "fs": FS, "route": 0,
            "device_key": "sim", "mixer_hash": "h1", "cal_sig": "c1"}
    n = int(0.6 * FS)
    t = np.arange(n) / FS
    tone = 0.1 * np.sin(2 * np.pi * 1000 * t)
    r = np.column_stack([tone, tone])

    results = {}
    tr.update(r, FS, 1000.0, -20.0, 0, None, now=0.0, ctx=base)
    tr.tare(ctx=base)
    st = tr.update(r, FS, 1000.0, -20.0, 0, None, now=1.0, ctx=base)
    results["те же условия"] = st["valid"] and abs(st["delta"]) < 1e-9
    for name, ctx, key in (
            ("частота 100 Гц", dict(base, freq=100.0), "freq"),
            ("частота 10 кГц", dict(base, freq=10000.0), "freq"),
            ("уровень ген.", dict(base, level_dbfs=-12.0), "level_dbfs"),
            ("Fs 96k", dict(base, fs=96000), "fs"),
            ("карта", dict(base, device_key="other"), "device_key"),
            ("маршрут", dict(base, route=1), "route"),
            ("микшер", dict(base, mixer_hash="h2"), "mixer_hash"),
            ("калибровка", dict(base, cal_sig="c2"), "cal_sig")):
        # каждый сценарий: пере-обнулить в базовом контексте, затем сменить условие
        tr.update(r, FS, 1000.0, -20.0, 0, None, now=2.0, ctx=base)
        tr.tare(ctx=base)
        st = tr.update(r, FS, 1000.0, -20.0, 0, None, now=3.0, ctx=ctx)
        results[name] = (st["tare_db"] == 0.0
                         and st.get("tare_reset_reason") is not None
                         and len(st["tare_reset_reason"]) > 0)
    # возврат к исходным: TARE остаётся сброшенным до явного повторного нажатия
    st = tr.update(r, FS, 1000.0, -20.0, 0, None, now=3.0, ctx=base)
    results["возврат: ноль не воскресает"] = st["tare_db"] == 0.0
    ok = all(results.values())
    bad = [k for k, v in results.items() if not v]
    check("TARE-10: смена любого условия сбрасывает ноль с причиной; возврат не воскресает",
          ok, f"отклонения: {bad if bad else 'нет'}")


# ===== §4 Сеть: 50/60 Гц, до Найквиста, 9994/200-я, альтернатива =====
def mains_checks():
    # 9994 Гц vs 200-я гармоника 49.97
    w = gainmatch.mains_proximity_warnings((9994.0,), 49.97, 1.0)
    ok9994 = any("9994" in x and "200" in x for x in w)
    # 60 Гц: 997 Гц близок к 17-й (1020)? нет, 23 бина; 1200 — к 20-й (1200): 0 бинов
    w60 = gainmatch.mains_proximity_warnings((1200.0,), 60.0, 1.0)
    ok60 = any("1200" in x for x in w60)
    # динамическая альтернатива: 1000 Гц при 50 Гц → безопасная частота упоминается
    w_alt = gainmatch.mains_proximity_warnings((1000.0,), 50.0, 1.0)
    alt_ok = any(("безопасная частота" in x) for x in w_alt)
    # альтернатива сама не на другой гармонике (для 9994/200-й при 49.97)
    w9994alt = gainmatch.mains_proximity_warnings((9994.0,), 49.97, 1.0)
    import re as _re
    alts = []
    for x in w9994alt:
        m = _re.search(r"около ([0-9.]+) Гц", x)
        if m:
            alts.append(float(m.group(1)))
    alt_clean = all(all(abs(a - j * 49.97) >= 6.0
                        for j in range(1, int(a / 49.97) + 2)) for a in alts) if alts else True
    check("MAINS-9994: 200-я гармоника 49.97 Гц обнаружена (предупреждение выдано)", ok9994,
          f"{[x[:60] for x in w9994alt]}")
    check("MAINS-60: 1200 Гц = 20-я гармоника 60 Гц обнаружена", ok60,
          f"{[x[:60] for x in w60]}")
    check("MAINS-ALT: предупреждение содержит динамическую безопасную альтернативу", alt_ok,
          f"{[x[-70:] for x in w_alt]}")
    check("MAINS-ALT-CLEAN: альтернативы не попадают на другие гармоники сети", alt_clean,
          f"альтернативы: {alts}")


# ===== §6 Производительность: hit/miss/p50/p95/p99/max + вращение =====
def perf_checks():
    tr = gainmatch.GainTracker()
    n = int(0.6 * FS)
    t = np.arange(n) / FS

    def ring_at(f):
        s = 0.1 * np.sin(2 * np.pi * f * t)
        return np.column_stack([s, s])

    for i in range(7):
        tr.update(ring_at(600.0 + i), FS, 600.0 + i, -20.0, 0, None, now=0.1 * i)
    h0, m0 = gainmatch._PINV_STATS["hit"], gainmatch._PINV_STATS["miss"]
    times = []
    for i in range(35):
        f = 600.0 + (i % 7)
        t0 = time.perf_counter()
        tr.update(ring_at(f), FS, f, -20.0, 0, None, now=5.0 + 0.1 * i)
        times.append((time.perf_counter() - t0) * 1000)
    h1, m1 = gainmatch._PINV_STATS["hit"], gainmatch._PINV_STATS["miss"]
    miss_rate = (m1 - m0) / max(1, (h1 - h0) + (m1 - m0))
    times.sort()
    p50, p95, p99, mx = times[17], times[33], times[34], times[-1]
    check("PERF-HIT: вращение 7 частот — промахов кеша < 10 %",
          miss_rate < 0.10, f"hit={h1 - h0}, miss={m1 - m0}, доля miss={miss_rate:.2%}")
    check("PERF-P: p50 ≤ 30 мс, p95 ≤ 60 мс, p99/max — единичные выбросы ≤ 100 мс",
          p50 <= 30 and p95 <= 60 and p99 <= 100 and mx <= 100,
          f"p50={p50:.1f}, p95={p95:.1f}, p99={p99:.1f}, max={mx:.1f} мс")


# ===== §7 Live: провал/выброс/низкий уровень/DC =====
def live_stability():
    n = int(0.6 * FS)
    t = np.arange(n) / FS
    tr = gainmatch.GainTracker()
    # низкий уровень (−70 dBFS, шум −95): C/R должен честно отказать
    st_lo = tr.update(ring(1000.0, level=-70.0, noise=-95.0, seed=5), FS, 1000.0,
                      -70.0, 0, None, now=0.0)
    lo_ok = (not st_lo["valid"]) and "C/R" in st_lo["msg"]
    # DC-смещение 0.3 в обоих каналах не должно ломать оценку
    ring_dc = ring(1000.0) + 0.3
    st_dc = tr.update(ring_dc, FS, 1000.0, -20.0, 0, None, now=1.0)
    dc_ok = st_dc["valid"] and abs(st_dc["delta"]) < 1e-6
    check("LIVE-LOW: −70 dBFS при шуме −95 — честный отказ по C/R", lo_ok,
          f"msg={st_lo['msg'][:60]}")
    check("LIVE-DC: DC-смещение 0.3 не влияет на ΔG", dc_ok,
          f"ΔG={st_dc['delta']:.6f}")


cr_hum_double_count()
cr_fft_size()
tare_scenarios()
mains_checks()
perf_checks()
live_stability()

npass = sum(1 for _, ok, _ in RES if ok)
print(f"\nИТОГ приёмки: {npass}/{len(RES)}")
sys.exit(0 if npass == len(RES) else 1)
