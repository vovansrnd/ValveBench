"""Повторяемые проверки математики и всего измерительного конвейера на симуляторе."""
from __future__ import annotations

import math
import time
import tempfile
import json
from pathlib import Path
import numpy as np

from . import dsp, gainmatch, scans, looptest, passport, steady
from .backend import SimBackend, SimChannel, AltSet
from . import backend as backend_module
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

    # FFT display scales: a full-scale sine is 0 dBFS/bin, and integrating
    # the one-sided ASD recovers the original time-domain noise level.
    line_block = dsp.Block(np.sin(2 * np.pi * f0 * t), fs)
    line_level = float(np.max(dsp.spectrum_line_dbfs(line_block)))
    rng_spec = np.random.default_rng(2301)
    noise_signal = rng_spec.normal(0.0, 10.0 ** (-90.0 / 20.0) / np.sqrt(2.0), n)
    noise_block = dsp.Block(noise_signal, fs)
    asd = np.asarray(dsp.spectrum_asd_dbfs(noise_block), dtype=float)
    asd_integrated_dbfs = 10.0 * np.log10(np.sum(10.0 ** (asd[1:-1] / 10.0)) * noise_block.df)
    expected_noise_dbfs = float(dsp.rms_dbfs(np.sqrt(np.mean(noise_signal ** 2))))
    ok_spectrum = abs(line_level) < 0.05 and abs(asd_integrated_dbfs - expected_noise_dbfs) < 0.15
    check("FFT line / noise-PSD normalization", ok_spectrum,
          f"full-scale sine={line_level:+.3f} dBFS; integrated ASD={asd_integrated_dbfs:.2f} dBFS; "
          f"time-domain noise={expected_noise_dbfs:.2f} dBFS")

    limits_192 = dsp.spectrum_view_limits(192000, 2)
    limits_96 = dsp.spectrum_view_limits(96000, 2)
    limits_44 = dsp.spectrum_view_limits(44100, 2)
    nyquist_bins = [float(np.fft.rfftfreq(8192, 1.0 / fs)[-1]) for fs in
                    (44100, 96000, 176400, 192000)]
    expected_nyquist = [fs / 2.0 for fs in (44100, 96000, 176400, 192000)]
    check("Spectrum range reaches exact Nyquist at supported sample rates",
          limits_192 == (10.0, 96000.0) and limits_96 == (10.0, 48000.0)
          and limits_44 == (10.0, 22050.0) and nyquist_bins == expected_nyquist,
          f"view limits: 192k={limits_192}, 96k={limits_96}, 44.1k={limits_44}; "
          f"FFT endpoints={nyquist_bins}")

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
    live_status = live_be.live_status()
    check("Live backend status / XRUN field", live_status.get("xruns") == 0 and live_status.get("backend") == "sim",
          f"backend={live_status.get('backend')}; xruns={live_status.get('xruns')}; simulator marks hardware XRUN not applicable")

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

    # Когерентные линии: гармоники ниже прежнего пола лобеного метода должны находиться,
    # а недостоверные линии — помечаться границей, а не выдаваться за измерение.
    fs2, n2 = 96000, 96000
    rng2 = np.random.default_rng(7)
    t2 = np.arange(8 * n2) / fs2
    amp = 10 ** (-25.39 / 20)
    xh = amp * np.sin(2 * np.pi * 1000 * t2)
    for hh, dbc in ((2, -112.0), (3, -125.0)):
        xh += amp * 10 ** (dbc / 20) * np.sin(2 * np.pi * hh * 1000 * t2 + 0.3 * hh)
    xh += rng2.normal(0, 10 ** (-110 / 20) / math.sqrt(2) * math.sqrt(0.5 * fs2 / 20000), xh.size)
    rh = dsp.analyze_tone(xh, fs2, 1000.0, n2, nharm=4, mains=0)
    hb = {q["h"]: q for q in rh["harmonics"]}
    check("Гармоники по когерентным линиям: H2 −112 / H3 −125 дБc под шумом −110 dBFS",
          rh["line_mode"] == "rect-phase-normalized" and abs(hb[2]["dbc"] + 112.0) < 1.5 and abs(hb[3]["dbc"] + 125.0) < 2.5
          and hb[2]["credible"] and not hb[4]["credible"],
          f"режим={rh['line_mode']}; H2={hb[2]['dbc']:.1f}, H3={hb[3]['dbc']:.1f}, H4 ≤{hb[4]['dbc']:.1f}; "
          f"пол линии {rh['line_floor_dbc']:.1f} dBc")

    # Сдвиг фазы между отдельными блоками не должен гасить фундамент и гармоники.
    # Такой сдвиг может возникнуть при изменении точки старта захвата; гармоники
    # нелинейного отклика должны сохранять фазу относительно h*phi(fundamental).
    phase_blocks = []
    for j in range(8):
        tt = np.arange(n2) / fs2
        ph = 0.47 * j
        sig = amp * np.sin(2 * np.pi * 1000 * tt + ph)
        sig += amp * 10 ** (-112.0 / 20) * np.sin(2 * np.pi * 2000 * tt + 2 * ph + 0.4)
        sig += amp * 10 ** (-125.0 / 20) * np.sin(2 * np.pi * 3000 * tt + 3 * ph - 0.2)
        phase_blocks.append(sig)
    xphase = np.concatenate(phase_blocks)
    rp = dsp.analyze_tone(xphase, fs2, 1000.0, n2, nharm=4, mains=0)
    hp = {q["h"]: q for q in rp["harmonics"]}
    check("Когерентные гармоники сохраняются при фазовом дрейфе между блоками",
          rp["line_mode"] == "rect-phase-normalized" and abs(rp["level_dbfs"] - (-25.39)) < 0.1
          and abs(hp[2]["raw_dbc"] + 112.0) < 0.1 and abs(hp[3]["raw_dbc"] + 125.0) < 0.2
          and rp["phase_coherence"] < 0.8,
          f"уровень={rp['level_dbfs']:.2f} dBFS; H2={hp[2]['raw_dbc']:.1f}; H3={hp[3]['raw_dbc']:.1f}; "
          f"phase coherence={rp['phase_coherence']:.3f}")

    # Чистый когерентный тон с белым шумом не должен выдавать случайные бины
    # за достоверные гармоники при консервативном пороге 10 dB.
    rng3 = np.random.default_rng(1234)
    t3 = np.arange(8 * n2) / fs2
    clean = amp * np.sin(2 * np.pi * 1000 * t3)
    clean += rng3.normal(0, 10 ** (-110 / 20) / math.sqrt(2) * math.sqrt(0.5 * fs2 / 20000), clean.size)
    rc = dsp.analyze_tone(clean, fs2, 1000.0, n2, nharm=9, mains=0)
    check("Чистый тон + шум: случайные гармоники не проходят порог достоверности",
          not any(q["credible"] for q in rc["harmonics"]),
          f"достоверных линий={sum(q['credible'] for q in rc['harmonics'])}; "
          f"порог={dsp.MARGIN_OK_DB:.1f} dB")

    # Профиль тишины должен отделять устойчивую одночастотную линию от белого шума.
    fs3, n3, m3 = 96000, 96000, 10
    tt3 = np.arange(m3 * n3) / fs3
    rng4 = np.random.default_rng(4401)
    base_noise = rng4.normal(0.0, 1.0e-8, tt3.size)
    spur_amp = dsp.dbfs_to_amp(-140.0)
    silent_with_spur = base_noise + spur_amp * np.sin(2 * np.pi * 3000.0 * tt3 + 0.2)
    spur_profile = dsp.coherent_spur_profile(silent_with_spur, fs3, n3)
    k3 = 3000
    spur_level = dsp.rms_dbfs(math.sqrt(spur_profile["spur_power"][k3]))
    spur_coh = float(spur_profile["phase_coherence"][k3])
    noise_only = rng4.normal(0.0, 1.0e-8, tt3.size)
    noise_profile = dsp.coherent_spur_profile(noise_only, fs3, n3)
    noise_spur = float(noise_profile["spur_power"][k3])
    check("Silence profile: stable 3-kHz spur is found, random noise is rejected",
          abs(spur_level + 140.0) < 2.0 and spur_coh > 0.95 and noise_spur == 0.0,
          f"spur={spur_level:.1f} dBFS, coherence={spur_coh:.3f}; noise-only spur power={noise_spur:.2e}")

    # Устойчивый spur, присутствующий и в тишине, не должен объявляться новой
    # гармоникой; достаточно сильная добавочная гармоника поверх него должна.
    t_signal = np.arange(4 * n3) / fs3
    main_amp = dsp.dbfs_to_amp(-20.0)
    spur_component = spur_amp * np.sin(2 * np.pi * 3000.0 * t_signal + 0.2)
    weak_h3_amp = dsp.dbfs_to_amp(-137.0)  # лишь на 3 dB выше baseline spur по мощности
    weak_signal = (main_amp * np.sin(2 * np.pi * 1000.0 * t_signal) + spur_component
                   + weak_h3_amp * np.sin(2 * np.pi * 3000.0 * t_signal + 0.2)
                   + rng4.normal(0.0, 1.0e-8, t_signal.size))
    p_spur = float(spur_profile["spur_power"][k3])
    weak_result = dsp.analyze_tone(weak_signal, fs3, 1000.0, n3, nharm=4, mains=0,
                                   coherent_line_floors={3: p_spur})
    weak_harm = next(q for q in weak_result["harmonics"] if q["h"] == 3)

    strong_h3_amp = dsp.dbfs_to_amp(-125.0)
    strong_signal = (main_amp * np.sin(2 * np.pi * 1000.0 * t_signal) + spur_component
                     + strong_h3_amp * np.sin(2 * np.pi * 3000.0 * t_signal + 0.2)
                     + rng4.normal(0.0, 1.0e-8, t_signal.size))
    strong_result = dsp.analyze_tone(strong_signal, fs3, 1000.0, n3, nharm=4, mains=0,
                                     coherent_line_floors={3: p_spur})
    strong_harm = next(q for q in strong_result["harmonics"] if q["h"] == 3)
    check("THD gate includes stationary spur from silence baseline",
          not weak_harm["credible"] and strong_harm["credible"]
          and weak_harm["stable_spur_floor_dbfs"] is not None,
          f"weak H3 margin={weak_harm['margin_db']:.1f} dB (rejected); strong H3 margin={strong_harm['margin_db']:.1f} dB (accepted)")

    # Gain Match: ΔG известной пары каналов, tare и классификация.
    be = SimBackend(SimChannel(gain=3.0, a3=0.02, noise_dbfs=-90, hum50_dbfs=-50),
                    SimChannel(gain=2.9, a3=0.02, noise_dbfs=-90, hum50_dbfs=-50))
    be.open(fs2)
    gi = np.arange(fs2)
    gs = 0.1 * np.sin(2 * np.pi * 1000 * gi / fs2)
    be.live_set_loop(np.column_stack((gs, gs)))
    tr = gainmatch.GainTracker()
    st = None
    for k in range(30):
        st = tr.update(be.live_recent(0.6), fs2, 1000.0, -20.0, 0, None, now=0.1 * k)
    truth = 20 * math.log10(3.0 / 2.9)
    tr.tare()
    st2 = tr.update(be.live_recent(0.6), fs2, 1000.0, -20.0, 0, None, now=10.0)
    tr.tare()  # повторное принятие того же текущего значения не должно удвоить поправку
    st3 = tr.update(be.live_recent(0.6), fs2, 1000.0, -20.0, 0, None, now=10.1)
    tr.clear_tare()
    st4 = tr.update(be.live_recent(0.6), fs2, 1000.0, -20.0, 0, None, now=10.2)
    check("Gain Match: ΔG пары 3.0/2.9, повторный tare и сброс нуля",
          st["valid"] and abs(st["filt"] - truth) < 0.005 and abs(st2["delta"]) < 0.005
          and abs(st3["delta"]) < 0.005 and abs(st4["delta"] - truth) < 0.005
          and gainmatch.classify(0.04, 0.05) == "ok" and gainmatch.pick_range(0.37, 0.05) == 0.5,
          f"ΔG={st['filt']:.4f} dB (истина {truth:.4f}); после tare={st2['delta']:+.4f}; "
          f"повторный tare={st3['delta']:+.4f}; после сброса={st4['delta']:+.4f}")

    # --- 0.1.6: наклоны гармоник, периодичность тона, валидность серии
    lv = np.array([-6.0, -12.0, -18.0, -24.0, -30.0])
    sl3 = scans.classify_slope(3, lv, -108.0 + 3.0 * (lv + 6.0), [True] * 5)
    slf = scans.classify_slope(5, lv, np.full(5, -136.0), [True] * 5)
    check("Наклон гармоники: кубическая H3 → distortion, фиксированная линия → fixed",
          sl3 and slf and sl3["kind"] == "distortion" and slf["kind"] == "fixed"
          and abs(sl3["slope"] - 3.0) < 0.05 and abs(slf["slope"]) < 0.05,
          f"H3 наклон={sl3['slope']:+.2f}; фиксированная={slf['slope']:+.2f}")

    be_q = SimBackend(SimChannel(gain=0.65, noise_dbfs=-120, quant_bits=19),
                      SimChannel(gain=0.65, noise_dbfs=-120, quant_bits=19))
    rq = Rig(be_q, Calibration(), fs=48000, blocks=6, settle=0.25)
    rq.open()
    sc = scans.run_freq_scan(rq, (1000.0, 997.0, 1234.0), level=-6.0)
    vd = scans.freq_scan_verdict(sc)
    per = {r["f"]: r["period"] for r in sc["rows"] if r["ch"] == "L"}
    check("Freqscan distinguishes short-period 1000 Hz and long-period 997 Hz (not aperiodic)",
          per[1000.0] == 48 and per[997.0] == 48000 and len(vd) >= 1
          and "короткопериодичном" in vd[0] and "длиннопериодичном" in vd[0],
          f"периоды={per}; вывод: {vd[0] if vd else 'нет'}")

    sil = [{"section": "SILENCE BEFORE", "channel": "L", "peak_dbfs": -95.0, "noise_unweighted_dbfs": -110.0},
           {"section": "SILENCE AFTER", "channel": "L", "peak_dbfs": -94.0, "noise_unweighted_dbfs": -110.1},
           {"section": "SILENCE BEFORE", "channel": "R", "peak_dbfs": -95.0, "noise_unweighted_dbfs": -115.0},
           {"section": "SILENCE AFTER", "channel": "R", "peak_dbfs": 0.0, "noise_unweighted_dbfs": -27.8}]
    bad = looptest.series_validity(sil, [], {"ok": True})
    good = looptest.series_validity(sil[:2], [], {"ok": True})
    check("Валидность серии: правый канал с шумом +87 дБ помечается INVALID, чистый — OK",
          len(bad) >= 2 and all(x.startswith("R:") for x in bad) and good == [],
          f"причины: {bad}")

    # --- 0.1.8: unstable slopes and explicit series states.
    lv8 = np.array([-6.0, -12.0, -18.0, -24.0, -30.0])
    unstable = scans.classify_slope(2, lv8,
                                    np.array([-110.0, -133.0, -141.0, -135.0, -145.0]),
                                    [True] * 5)
    check("levelscan: unstable points are not classified as a fixed spur",
          unstable is not None and unstable["kind"] == "unstable" and unstable["rms"] > 3.0,
          f"kind={unstable['kind'] if unstable else None}; fit RMS={unstable['rms'] if unstable else None}")
    st_ok = looptest.validity_state(sil[:2] + sil[2:], [], {"ok": True}, True, [])
    st_inc = looptest.validity_state([], [], {"ok": True}, False, [])
    st_xrun = looptest.validity_state(sil[:2], [], {"ok": True}, True,
                                      ["aplay: underrun!!! at least 3.2 ms long"])
    check("loopback validity distinguishes OK / INCOMPLETE / INVALID XRUN",
          st_ok[0] == "INVALID" and st_inc[0] == "INCOMPLETE" and st_xrun[0] == "INVALID",
          f"with deliberately noisy R: {st_ok[0]}, no silence: {st_inc[0]}, XRUN: {st_xrun[0]}")

    # Passport metadata must prevent an accidental A/B comparison across different interfaces.
    mismatch = passport.passport_mismatches(
        {"device": "hw:2,0", "tone_hz": 997.0, "levels_dbfs": [-1.0, -12.0],
         "metadata": {"in_map": [0, 1], "out_map": [0, 1], "blocks": 4}},
        {"device": "hw:3,0", "tone_hz": 997.0, "levels_dbfs": [-1.0, -12.0],
         "metadata": {"in_map": [0, 1], "out_map": [0, 1], "blocks": 4}})
    check("passport A/B: different ALSA devices are reported as incompatible",
          any("устройства различаются" in x for x in mismatch), f"mismatches={mismatch}")

    # Matched-level test: the robust frequency estimator must not jump to a neighbouring
    # correlation lobe separated by 1/T, as the previous broad bounded search did.
    fs_st = 96000
    f_st = 997.092996
    t_st = np.arange(fs_st * 2, dtype=np.float64) / fs_st
    rng_st = np.random.default_rng(919)
    tone_st = 10 ** (-23.86 / 20) * np.sin(2 * np.pi * f_st * t_st)
    tone_st += rng_st.normal(0.0, 1e-6, tone_st.size)
    f_fit, lev_fit = steady._tone_fit(tone_st, fs_st, 997.0)
    check("steady: bounded single-lobe tone fit returns true frequency and level",
          abs(f_fit - f_st) < 0.001 and abs(lev_fit + 23.86) < 0.02,
          f"f={f_fit:.6f} Hz (truth {f_st:.6f}); level={lev_fit:.3f} dBFS")

    # Long-record fit must not jump by 1/T to an adjacent correlation maximum.
    fs_long = 48000
    f_long = 997.092996
    t_long = np.arange(fs_long * 12, dtype=np.float64) / fs_long
    long_tone = 10 ** (-23.4 / 20) * np.sin(2 * np.pi * f_long * t_long + 0.37)
    f_long_fit, l_long_fit = steady._tone_fit(long_tone, fs_long, 997.0)
    check("steady: long tone frequency search remains on the central FFT lobe",
          abs(f_long_fit - f_long) < 0.001 and abs(l_long_fit + 23.4) < 0.02,
          f"f={f_long_fit:.6f} Hz (truth {f_long:.6f}); level={l_long_fit:.3f} dBFS")

    # Coherent projection reports the main tone and a known -60 dB cross-channel leak.
    t_map = np.arange(fs_st * 2, dtype=np.float64) / fs_st
    map_main = 10 ** (-23.4 / 20) * np.sin(2 * np.pi * f_st * t_map + 0.4)
    map_leak = 10 ** (-83.4 / 20) * np.sin(2 * np.pi * f_st * t_map - 0.2)
    main_level = steady._tone_level_at(map_main, fs_st, f_st)
    leak_level = steady._tone_level_at(map_leak, fs_st, f_st)
    check("steady: coherent L/R map quantifies a -60 dB cross-channel component",
          abs(main_level + 23.4) < 0.02 and abs((leak_level - main_level) + 60.0) < 0.02,
          f"main={main_level:.2f} dBFS; cross={leak_level:.2f} dBFS; Δ={leak_level-main_level:.2f} dB")

    # Residual analysis: a pure carrier + H2 should vanish from the non-harmonic residual,
    # while one deliberately injected broadband burst must be counted in exactly one window.
    fs_res = 48000
    f_res = 997.093
    t_res = np.arange(fs_res * 5, dtype=np.float64) / fs_res
    rng_res = np.random.default_rng(1919)
    base_res = rng_res.normal(0.0, 1e-5, t_res.size)
    amp_res = 10 ** (-23.4 / 20)
    for a_s, b_s in ((1.0, 3.0), (3.0, 5.0)):
        ix = (t_res >= a_s) & (t_res < b_s)
        base_res[ix] += amp_res * np.sin(2 * np.pi * f_res * t_res[ix])
        base_res[ix] += amp_res * 10 ** (-80 / 20) * np.sin(4 * np.pi * f_res * t_res[ix] + 0.3)
    burst_ix = (t_res >= 1.5) & (t_res < 1.6)
    base_res[burst_ix] += rng_res.normal(0.0, 1e-3, int(np.count_nonzero(burst_ix)))
    _rows, residual_stats = steady._analyse_channel(
        base_res, fs_res, {"idle_pre": (0.0, 1.0), "L_steady": (1.0, 3.0), "R_steady": (3.0, 5.0)},
        "L", f_res, -23.4, 0.1, 0.1, 20)
    stat_l = residual_stats["states"]["L_steady"]["residual_20_20k"]
    stat_r = residual_stats["states"]["R_steady"]["residual_20_20k"]
    check("steady: LS harmonic removal rejects tone leakage and detects a real broadband burst",
          stat_l["bursts"] == 1 and stat_r["bursts"] == 0 and stat_l["max"] > stat_l["median"] + 20,
          f"L burst windows={stat_l['bursts']}; R={stat_r['bursts']}; L median/max={stat_l['median']:.1f}/{stat_l['max']:.1f} dBFS")

    # The active playback L and R may have slightly different measured clock frequencies.
    _mapped_rows, mapped_stats = steady._analyse_channel(
        base_res, fs_res, {"idle_pre": (0.0, 1.0), "L_steady": (1.0, 3.0), "R_steady": (3.0, 5.0)},
        "L", {"L_steady": f_res, "R_steady": f_res + 0.02}, -23.4, 0.1, 0.1, 20)
    mapped_freqs = {r["state"]: r["projection_frequency_hz"] for r in _mapped_rows if r["state"] != "idle_pre"}
    check("steady: per-output measured frequency is used for each residual projection",
          abs(mapped_freqs["L_steady"] - f_res) < 1e-9 and abs(mapped_freqs["R_steady"] - (f_res + 0.02)) < 1e-9,
          f"L-state={mapped_freqs['L_steady']:.6f} Hz; R-state={mapped_freqs['R_steady']:.6f} Hz")

    # At 48 kHz there is no 40 kHz-to-Nyquist band. It must be N/A, not -300 dBFS.
    _rows_48, stats_48 = steady._analyse_channel(
        base_res, fs_res, {"idle_pre": (0.0, 1.0), "L_steady": (1.0, 3.0), "R_steady": (3.0, 5.0)},
        "L", f_res, -23.4, 0.1, 0.1, 20)
    hi_band_na = stats_48["states"]["L_steady"]["raw_40_nyquist"] is None
    check("steady: ultrasonic band below Nyquist is reported as N/A, not a false -300 dBFS",
          hi_band_na, f"raw 40k–Nyquist={stats_48['states']['L_steady']['raw_40_nyquist']}")

    # 0.1.10: high-frequency spectrum is broken into real bands clipped to Nyquist.
    bands_192 = steady._ultrasonic_band_edges(192000)
    bands_176 = steady._ultrasonic_band_edges(176400)
    bands_96 = steady._ultrasonic_band_edges(96000)
    check("steady: ultrasonic band map is clipped to the actual Nyquist frequency",
          [x[0] for x in bands_192] == ["40_50k", "50_60k", "60_70k", "70_80k", "80_90k", "90_96k"]
          and [x[0] for x in bands_176][-1] == "80_88p2k"
          and [x[0] for x in bands_96] == ["40_48k"],
          f"192k={[x[0] for x in bands_192]}; 176.4k={bands_176[-1][0]}; 96k={[x[0] for x in bands_96]}")

    # Integrated band level and ASD normalization must describe the same measured power.
    fs_band = 192000
    n_band = 19200
    t_band = np.arange(n_band, dtype=np.float64) / fs_band
    rng_band = np.random.default_rng(1910)
    x_band = rng_band.normal(0.0, 1e-4, n_band)
    win_band = np.ones(n_band, dtype=np.float64)
    f_band, p_band, df_band = steady._window_spectrum(x_band, fs_band, win_band)
    m_band = steady._band_metrics(f_band, p_band, df_band, 40000, 50000)
    expected_eq_asd = m_band["dbfs"] - 10.0 * math.log10(m_band["bandwidth_hz"])
    check("steady: ultrasonic band ASD normalization matches integrated band power",
          abs(m_band["equivalent_asd_dbfs_sqrt_hz"] - expected_eq_asd) < 1e-9
          and m_band["bins"] > 0,
          f"band={m_band['dbfs']:.2f} dBFS; equivalent ASD={m_band['equivalent_asd_dbfs_sqrt_hz']:.2f} dBFS/√Hz")

    # A narrow ultrasonic component must land in its own band, not be hidden in 40k-Nyquist.
    hf_t = np.arange(fs_band, dtype=np.float64) / fs_band
    hf = 10 ** (-40.0 / 20.0) * np.sin(2.0 * np.pi * 45000.0 * hf_t)
    hf_win = np.ones(hf.size, dtype=np.float64)
    hf_f, hf_psd, hf_df = steady._window_spectrum(hf, fs_band, hf_win)
    hf_40_50 = steady._band_metrics(hf_f, hf_psd, hf_df, 40000, 50000)
    hf_50_60 = steady._band_metrics(hf_f, hf_psd, hf_df, 50000, 60000)
    check("steady: a 45-kHz line is isolated in the 40–50-kHz band",
          abs(hf_40_50["dbfs"] + 40.0) < 0.1 and hf_50_60["dbfs"] < -120.0,
          f"40–50k={hf_40_50['dbfs']:.2f} dBFS; 50–60k={hf_50_60['dbfs']:.1f} dBFS")

    # Baseline excursions are grouped into episodes, rather than reported only as window counts.
    burst_rows = [{"time_s": t, "residual_20_20k_dbfs": v}
                  for t, v in ((0.05, -90), (0.15, -89), (0.25, -88),
                              (0.55, -87), (0.75, -110))]
    burst_groups = steady._burst_episodes(burst_rows, -100.0, 0.1)
    check("steady: adjacent burst windows are grouped into physical episodes",
          burst_groups["windows"] == 4 and burst_groups["episodes"] == 2
          and abs(burst_groups["longest_s"] - 0.3) < 1e-9,
          f"windows={burst_groups['windows']}; episodes={burst_groups['episodes']}; longest={burst_groups['longest_s']:.2f}s")

    # Common-mode statistics must distinguish simultaneous from one-channel events.
    common_rows = []
    for tt, lv, rv in ((1.05, -90, -91), (1.15, -89, -90), (1.25, -90, -110),
                       (1.35, -110, -110), (1.45, -110, -90)):
        common_rows.extend((
            {"time_s": tt, "state": "L_steady", "channel": "L", "residual_20_20k_dbfs": lv},
            {"time_s": tt, "state": "L_steady", "channel": "R", "residual_20_20k_dbfs": rv},
        ))
    common_summary = {"L": {"baseline_dbfs": -110.0}, "R": {"baseline_dbfs": -110.0}}
    common_stats = steady._shared_excursion_stats(common_rows, common_summary, 0.1)["L_steady"]
    check("steady: simultaneous L/R excursions are counted separately",
          common_stats["both_excursion_windows"] == 2
          and common_stats["either_excursion_windows"] == 4
          and common_stats["shared_episodes"] == 1,
          f"both={common_stats['both_excursion_windows']}; either={common_stats['either_excursion_windows']}; shared episodes={common_stats['shared_episodes']}")

    # The new program must contain long pre/post silence while keeping total duration exact.
    with tempfile.TemporaryDirectory(prefix="valvebench-steady-") as td:
        test_program = Path(td) / "test.raw"
        test_duration = steady._write_test_program(test_program, "S24_3LE", 48000, 997.0,
                                                   -18.0, duration_s=5, idle_s=3, gap_s=2)
        expected_duration = 2 * 5 + 2 * 3 + 2 + 4
        check("steady: long baseline test program has symmetric pre/post silence",
              abs(test_duration - expected_duration) < 1e-9
              and test_program.stat().st_size == int(expected_duration * 48000 * 2 * 3),
              f"duration={test_duration:.1f}s; expected={expected_duration}s; bytes={test_program.stat().st_size}")

    # A changed post-run idle floor must be visible as baseline drift.
    drift_fs = 48000
    drift_t = np.arange(drift_fs * 3, dtype=np.float64) / drift_fs
    drift_rng = np.random.default_rng(101010)
    drift_signal = drift_rng.normal(0.0, 1e-5, drift_t.size)
    post = drift_t >= 2.2
    drift_signal[post] = drift_rng.normal(0.0, 1e-4, np.count_nonzero(post))
    tone_mask = (drift_t >= 1.0) & (drift_t < 2.0)
    drift_signal[tone_mask] += 10 ** (-23.4 / 20) * np.sin(2 * np.pi * 997.0 * drift_t[tone_mask])
    _drift_rows, drift_stats = steady._analyse_channel(
        drift_signal, drift_fs,
        {"idle_pre": (0.0, 0.8), "L_steady": (1.0, 2.0), "idle_post": (2.2, 3.0)},
        "L", 997.0, -23.4, 0.1, 0.1, 20)
    check("steady: idle pre/post drift is reported instead of silently folding into baseline",
          drift_stats["baseline_stable"] is False and drift_stats["baseline_drift_db"] > 15.0,
          f"pre={drift_stats['baseline_pre_dbfs']:.2f}; post={drift_stats['baseline_post_dbfs']:.2f}; "
          f"drift={drift_stats['baseline_drift_db']:+.2f} dB")

    # Comparison command must preserve all source pairings rather than averaging unlike devices.
    with tempfile.TemporaryDirectory(prefix="valvebench-steady-compare-") as td:
        p_json = Path(td) / "steady.json"
        sample = {"playback_device": "hw:3,0", "capture_device": "hw:2,0", "sample_rate": 192000,
                  "playback_format": "S24_3LE", "capture_format": "S24_3LE", "series_level_valid": True,
                  "channel_map": {"PLAY_L_only": {"cross_channel_leakage_db": -82.0},
                                  "PLAY_R_only": {"cross_channel_leakage_db": -83.0}},
                  "analysis": {}}
        for ch in ("L", "R"):
            sample["analysis"][ch] = {"baseline_dbfs": -100.0, "states": {}}
            for state in ("L_steady", "R_steady"):
                sample["analysis"][ch]["states"][state] = {
                    "residual_20_20k": {"median": -95.0, "p95": -88.0, "bursts": 5, "episodes": 2},
                    "raw_40_nyquist": {"median": -78.0},
                }
        p_json.write_text(json.dumps(sample), encoding="utf-8")
        compare_text, compare_rows = steady.compare_steady_runs([p_json])
        check("steady-compare: report keeps explicit DAC→ADC pairing and metrics",
              len(compare_rows) == 1 and compare_rows[0]["playback"] == "hw:3,0"
              and compare_rows[0]["capture"] == "hw:2,0" and "hw:3,0 → hw:2,0" in compare_text,
              f"rows={len(compare_rows)}; row={compare_rows[0] if compare_rows else None}")

    # ALSA format probing preference/fallback is tested without real hardware.
    old_probe = steady._probe_play
    try:
        tried = []
        def fake_probe(dev, rate, fmt, channels=2):
            tried.append(fmt)
            return fmt == "S16_LE", "unsupported"
        steady._probe_play = fake_probe
        fmt_pick = steady.choose_device_format("hw:3,0", 192000, "playback")
    finally:
        steady._probe_play = old_probe
    check("steady: ALSA format negotiation falls back to a supported 16-bit mode",
          fmt_pick.fmt == "S16_LE" and tried == ["S24_3LE", "S24_LE", "S32_LE", "S16_LE"],
          f"selected={fmt_pick.fmt}; tried={tried}")

    # Playback-only and capture-only interfaces must remain visible for the
    # separate-DAC/separate-ADC workflow; do not reduce the list to duplex cards.
    old_parse_stream = backend_module.parse_stream
    try:
        backend_module.parse_stream = lambda card: {
            "Playback": [AltSet("S24_3LE", 2, (48000, 96000))],
            "Capture": [AltSet("S16_LE", 2, (48000,))],
        }
        play_rates = backend_module.supported_pcm_rates("hw:7,0", "Playback")
        cap_rates = backend_module.supported_pcm_rates("hw:7,0", "Capture")
        both_rates = backend_module.supported_duplex_rates("hw:7,0")
    finally:
        backend_module.parse_stream = old_parse_stream
    check("ALSA capabilities: separate playback/capture rates are reported independently",
          play_rates == (48000, 96000) and cap_rates == (48000,) and both_rates == (48000,),
          f"playback={play_rates}; capture={cap_rates}; duplex={both_rates}")

    # Strict A/B must also reject same-device passports with changed PCM formats.
    cfg_mismatch = {
        "time": "before", "label": "a", "device": "hw:2,0", "tone_hz": 997.0,
        "levels_dbfs": [-1.0], "repeats": 3, "silence_seconds": 4.0,
        "metadata": {"in_map": [0, 1], "out_map": [0, 1]},
        "rates": {"96000": {"io_config": {"device": "hw:2,0", "capture_format": "S24_3LE", "playback_format": "S24_3LE"}}},
    }
    cfg_mismatch_after = {**cfg_mismatch, "time": "after", "label": "b", "rates": {"96000": {"io_config": {
        "device": "hw:2,0", "capture_format": "S16_LE", "playback_format": "S24_3LE"}}}}
    format_mismatch = passport.passport_mismatches(cfg_mismatch, cfg_mismatch_after)
    text_mismatch = ""
    try:
        passport.compare_passports(cfg_mismatch, cfg_mismatch_after)
    except ValueError as exc:
        text_mismatch = str(exc)
    check("passport A/B: a changed PCM capture format is not silently compared",
          bool(format_mismatch) and "PCM-конфигурация" in text_mismatch,
          f"mismatches={format_mismatch}")

    passed = sum(q["ok"] for q in checks)
    return {"passed": passed, "total": len(checks), "checks": checks}


def format_selftest(r):
    lines = [f"ValveBench self-test: {r['passed']}/{r['total']} PASS"]
    for q in r["checks"]:
        lines.append(f"{'PASS' if q['ok'] else 'FAIL'}  {q['name']}: {q['detail']}")
    return "\n".join(lines)
