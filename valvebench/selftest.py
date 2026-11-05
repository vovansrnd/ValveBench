"""Повторяемые проверки математики и всего измерительного конвейера на симуляторе."""
from __future__ import annotations

import math
import time
import numpy as np

from . import dsp
from .backend import SimBackend, SimChannel
from .calib import Calibration
from .measure import Rig, sweep, noise_response, noise_transfer_capture
from .vinyl import analyze_3150_stability, analyze_vta_sum, analyze_frequency_response, analyze_phase, analyze_crosstalk


def run_selftest():
    checks = []

    def check(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    # Точная синусоида с известной H2=-60 dBc.
    fs, n, f0 = 48000, 48000, 1000.0
    t = np.arange(n) / fs
    x = 0.5 * np.sin(2 * np.pi * f0 * t) + 0.0005 * np.sin(4 * np.pi * f0 * t)
    r = dsp.analyze_tone(x, fs, f0, n, nharm=4, mains=0)
    h2 = next(q["dbc"] for q in r["harmonics"] if q["h"] == 2)
    check("THD/H2 известного тона", abs(h2 + 60.0) < 0.15,
          f"H2={h2:.2f} dBc; ожидалось -60.00 dBc")

    # Добавляем известный белый шум. Уровень задаётся относительно full-scale,
    # а THD+N выводится относительно несущей и только в полосе 20 Гц–20 кГц.
    rng = np.random.default_rng(20261031)
    noise_rms = 10 ** (-80 / 20) / math.sqrt(2)
    xn = 0.5 * np.sin(2 * np.pi * f0 * t) + rng.normal(0, noise_rms, n)
    rn = dsp.analyze_tone(xn, fs, f0, n, nharm=2, mains=0)
    check("THD+N: известный белый шум и исключение несущей", -76.0 < rn["thdn_db"] < -73.5,
          f"THD+N={rn['thdn_db']:.2f} dB; расчётное значение около -74.8 dB относительно несущей")

    # Двухтоновый CCIF: вводим продукт 1 кГц на -60 dBc.
    x2 = 0.3*np.sin(2*np.pi*19000*t) + 0.3*np.sin(2*np.pi*20000*t) + 0.0003*np.sin(2*np.pi*1000*t)
    ri = dsp.analyze_imd(x2, fs, 19000, 20000, n, mode="ccif", band_hi=21000)
    d2 = next(p["dbc"] for p in ri["products"] if p["label"] == "fв−fн")
    check("IMD CCIF difference product", abs(d2 + 60.0) < 0.3,
          f"1 кГц продукт={d2:.2f} dBc; ожидалось -60.00 dBc")

    # Измерительная цепочка RIAA через виртуальный аналоговый DUT.
    be = SimBackend(
        SimChannel(gain=0.65, riaa=True, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130),
        SimChannel(gain=0.65, riaa=True, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130),
        seed=4,
    )
    rig = Rig(be, Calibration(), fs=48000, blocks=2)
    rig.open()
    sr = sweep(rig, f_lo=30, f_hi=16000, dur=4, level_1k_dbfs=-30, mode="riaa")
    dev = max(float(np.nanmax(np.abs(sr["ch"][c]["riaa_dev_db"]))) for c in ("L", "R"))
    check("Sweep inverse-RIAA через SimBackend", dev < 0.45,
          f"максимальное отклонение={dev:.3f} dB; критерий <0.45 dB")

    nr = noise_response(rig, seconds=4, kind="pink", level_dbfs=-42, f_lo=30, f_hi=16000)
    ndev = max(float(np.nanmax(np.abs(nr["ch"][c]["riaa_dev_db"]))) for c in ("L", "R"))
    coh = min(float(np.nanmin(nr["ch"][c]["coherence"])) for c in ("L", "R"))
    check("Noise H1 + coherence через SimBackend", ndev < 0.8 and coh > 0.75,
          f"max |RIAA deviation|={ndev:.3f} dB; min coherence={coh:.3f}")

    # Тот же H1-алгоритм в live-режиме с циклическим тестовым буфером.
    from .signals import make_test_noise
    live_x, _ = make_test_noise(fs, 8.0, "pink", -42.0, 30.0, 16000.0, seed=24680)
    live_be = SimBackend(
        SimChannel(gain=0.65, riaa=True, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130),
        SimChannel(gain=0.65, riaa=True, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130),
        seed=9,
    )
    live_be.open(fs)
    live_be.live_start()
    live_be.live_set_loop(np.column_stack((live_x, live_x)))
    live_rec = live_be.live_recent(10.0)
    live_h1 = noise_transfer_capture(live_x, live_rec, fs, 30.0, 16000.0, False)
    live_dev = max(float(np.nanmax(np.abs(live_h1["ch"][c]["riaa_dev_db"]))) for c in ("L", "R"))
    live_be.live_stop()
    check("Live noise H1 / cyclic buffer", live_dev < 1.0,
          f"max |RIAA deviation|={live_dev:.3f} dB; periodic source aligned and analysed")

    # TACET A3 — именно pink-noise: моно и переворот полярности должны различаться.
    from .signals import make_test_noise
    pink, _ = make_test_noise(fs, 4.0, "pink", -24.0, 30.0, 16000.0, seed=12)
    mono = np.column_stack((pink, pink))
    inv = np.column_stack((pink, -pink))
    pm, pi = analyze_phase(mono, fs), analyze_phase(inv, fs)
    check("TACET A3 mono pink-noise", pm["correlation"] > 0.98 and abs(pm["phase_r_minus_l_deg"]) < 5,
          f"corr={pm['correlation']:+.4f}; phase={pm['phase_r_minus_l_deg']:+.2f}°")
    check("TACET A3 out-of-phase pink-noise", pi["correlation"] < -0.98 and abs(abs(pi["phase_r_minus_l_deg"]) - 180) < 5,
          f"corr={pi['correlation']:+.4f}; phase={pi['phase_r_minus_l_deg']:+.2f}°")

    # TACET A2: тон 3150 Гц должен корректно определяться.
    tx = 0.2 * np.sin(2*np.pi*3150*np.arange(fs*4)/fs)
    tr = analyze_3150_stability(tx, fs)
    check("TACET A2 3150 Hz", abs(tr["mean_hz"] - 3150) < 0.1,
          f"средняя частота={tr['mean_hz']:.4f} Hz; это оценка стабильности, не DIN W&F")

    # TACET A4 — заданная утечка -60 dB между каналами должна вернуться.
    tx = np.arange(fs * 3) / fs
    left_tone = 0.4 * np.sin(2 * np.pi * 1000 * tx)
    xtalk_x = np.column_stack((left_tone, left_tone * 1e-3))
    xtalk = analyze_crosstalk(xtalk_x, fs, active="L")
    check("TACET A4 crosstalk", abs(xtalk["leak_db"] + 60.0) < 0.3,
          f"утечка={xtalk['leak_db']:.2f} dB; ожидалось -60 dB")

    # TACET A5: известный побочный 1 кГц должен находиться.
    tv = (0.2*np.sin(2*np.pi*341*np.arange(fs*2)/fs)
          + 0.2*np.sin(2*np.pi*659*np.arange(fs*2)/fs)
          + 0.0002*np.sin(2*np.pi*1000*np.arange(fs*2)/fs))
    vr = analyze_vta_sum(tv, fs)
    check("TACET A5 sum tone", abs(vr["sum_dbc_vs_mean_tone"] + 60.0) < 0.6,
          f"1000 Hz product={vr['sum_dbc_vs_mean_tone']:.2f} dB; ожидалось около -60 dB")

    # TACET B2: распознавание известной последовательности 1k / 20 / 1k.
    pieces = []
    for fq in (1000.0, 20.0, 1000.0):
        tt = np.arange(2 * fs) / fs
        tone = 0.05 * np.sin(2 * np.pi * fq * tt)
        pieces.append(np.column_stack((tone, tone * 0.9)))
    b2 = analyze_frequency_response(np.concatenate(pieces), fs)
    b2_freqs = [round(q["frequency_hz"]) for q in b2["segments"]]
    check("TACET B2 tone sequence", 20 in b2_freqs and 1000 in b2_freqs and len(b2["segments"]) >= 3,
          f"распознаны ступени: {b2_freqs}")

    passed = sum(q["ok"] for q in checks)
    return {"passed": passed, "total": len(checks), "checks": checks}


def format_selftest(r):
    lines = [f"ValveBench self-test: {r['passed']}/{r['total']} PASS"]
    for q in r["checks"]:
        lines.append(f"{'PASS' if q['ok'] else 'FAIL'}  {q['name']}: {q['detail']}")
    return "\n".join(lines)
