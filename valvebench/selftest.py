"""Повторяемые проверки математики и всего измерительного конвейера на симуляторе."""
from __future__ import annotations

import math
import time
import hashlib
import tempfile
import json
from pathlib import Path
import numpy as np

from . import dsp, gainmatch, scans, looptest, passport, steady, store, preferences
from . import pcm as pcm_mod
from . import io_health as io_health_mod
from .pcm import to_pcm
from .backend import SimBackend, SimChannel, AltSet, usb_profile_identity
from . import backend as backend_module
from .calib import Calibration
from .measure import (Rig, sweep, noise_response, noise_transfer_capture,
                      build_response_calibration, apply_response_calibration)
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

    # Per-card full-band response calibration: the loop profile contains separate
    # L/R magnitude and phase curves. Applying it must remove the soundcard curve
    # while retaining the synthetic DUT response.
    fcal = np.geomspace(20.0, 20000.0, 256)
    log_ratio = np.log10(fcal / 1000.0)
    card_l, card_r = 0.35 * log_ratio, -0.22 * log_ratio
    dut_l, dut_r = 0.12 * log_ratio, -0.08 * log_ratio
    ph_l, ph_r = 8.0 * log_ratio, -5.0 * log_ratio
    dut_ph_l, dut_ph_r = 0.6 * log_ratio, -0.4 * log_ratio
    loop_result = {"kind": "sweep", "mode": "flat", "fs": fs, "f_lo": 20.0, "f_hi": 20000.0,
                   "warnings": [], "ch": {
                       "L": {"freq": fcal, "fr_db": np.zeros_like(fcal), "fr_norm_db": card_l, "phase_deg": ph_l},
                       "R": {"freq": fcal, "fr_db": np.zeros_like(fcal), "fr_norm_db": card_r, "phase_deg": ph_r}}}
    fr_profile = build_response_calibration(loop_result, "sim-card", "mixer-A", fs=fs)
    no_loop = {"kind": "sweep", "mode": "flat", "fs": fs, "f_lo": 20.0, "f_hi": 20000.0,
               "warnings": [], "ch": {}}
    for ch in ("L", "R"):
        no_loop["ch"][ch] = {"freq": fcal, "fr_db": np.full_like(fcal, -90.0),
                             "fr_norm_db": np.zeros_like(fcal), "phase_deg": np.zeros_like(fcal)}
    try:
        build_response_calibration(no_loop, "sim-card", "mixer-A", fs=fs)
        rejected_no_loop = False
    except (ValueError, RuntimeError):
        rejected_no_loop = True
    check("Soundcard response calibration refuses an open/no-signal loopback",
          rejected_no_loop, "no loop signal at 1 kHz must not become a saved correction")
    measured_fr = {"kind": "sweep", "fs": fs, "iec": False, "warnings": [], "ch": {}}
    for channel, card_curve, dut_curve, card_phase in (("L", card_l, dut_l, ph_l), ("R", card_r, dut_r, ph_r)):
        measured_fr["ch"][channel] = {"freq": fcal.copy(),
                                      "fr_norm_db": card_curve + dut_curve,
                                      "phase_deg": card_phase + (dut_ph_l if channel == "L" else dut_ph_r),
                                      "riaa_curve_db": dsp.riaa_playback_db(fcal)}
    apply_response_calibration(measured_fr, fr_profile)
    cal_fr_err = max(float(np.max(np.abs(measured_fr["ch"][ch]["fr_norm_db"] - dut)))
                     for ch, dut in (("L", dut_l), ("R", dut_r)))
    cal_phase_err = max(float(np.max(np.abs(measured_fr["ch"][ch]["phase_deg"] - (dut_ph_l if ch == "L" else dut_ph_r))))
                        for ch in ("L", "R"))
    check("Soundcard FR calibration removes separate L/R loop curves but preserves DUT response",
          cal_fr_err < 1e-9 and cal_phase_err < 0.005 and measured_fr["response_calibration"]["applied"],
          f"max FR error={cal_fr_err:.2e} dB; phase residual={cal_phase_err:.2e} deg; saved Fs={fr_profile['fs']}")

    # Device profiles must remain isolated across card selection and serialization.
    cal_profiles = Calibration()
    cal_profiles.activate_profile("usb:aaa:port=1-1:pcm0", "hw:2,0")
    cal_profiles["out_fs_vrms"] = [2.0, 2.1]
    cal_profiles["frequency_response_profiles"] = {"192000": {"device_key": "card-A"}}
    cal_profiles.activate_profile("usb:bbb:port=1-2:pcm0", "hw:3,0")
    cal_profiles["out_fs_vrms"] = [1.0, 1.1]
    cal_profiles.activate_profile("usb:aaa:port=1-1:pcm0", "hw:2,0")
    restored_cal = Calibration(cal_profiles.serialize())
    check("Per-device calibration profiles survive switching and serialization",
          restored_cal["out_fs_vrms"] == [2.0, 2.1]
          and "192000" in restored_cal["frequency_response_profiles"],
          f"active card A DAC fullscale={restored_cal['out_fs_vrms']}; response profiles={list(restored_cal['frequency_response_profiles'])}")

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
    # 0.1.19-R3: ворота C/R ≥30 дБ действуют и в Live — фон сети −50 dBFS при
    # тоне −20 dBFS (−30 дБс) теперь корректно бракуется, сценарий адаптирован
    be = SimBackend(SimChannel(gain=3.0, a3=0.02, noise_dbfs=-90, hum50_dbfs=-70),
                    SimChannel(gain=2.9, a3=0.02, noise_dbfs=-90, hum50_dbfs=-70))
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

    # Two-step Gain Match: first characterize L/R measurement-chain imbalance,
    # then subtract it from the DUT result. No voltage calibration is required
    # for relative L/R gain matching.
    loop_backend = SimBackend(
        SimChannel(gain=0.97, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130),
        SimChannel(gain=1.03, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130), seed=17)
    rig_loop = Rig(loop_backend, Calibration(), fs=48000, blocks=2, settle=0.15)
    rig_loop.open()
    # Add a deterministic frequency-dependent L-path response to the analyzer:
    # this emulates unequal DAC/ADC response that must be measured separately at
    # each frequency, not removed with one global zero/tare value.
    interface_freq_error = {100.0: -0.25, 1000.0: 0.10, 10000.0: 0.35}
    def inject_interface_frequency_error(rig):
        original = rig._tone_an
        def wrapped(win, ch, k, nharm=9, coherent_line_floors=None):
            result = original(win, ch, k, nharm=nharm, coherent_line_floors=coherent_line_floors)
            if int(ch) == 0:
                f_meas = round(float(rig.f_of(k)), 6)
                result["level_dbfs"] += interface_freq_error.get(f_meas, 0.0)
            return result
        rig._tone_an = wrapped
    inject_interface_frequency_error(rig_loop)
    loop_series = gainmatch.measure_series(rig_loop, level_dbfs=-24.0, repeats=3, purpose="loopback")
    loop_reference = gainmatch.build_loopback_reference(loop_series)
    interface_delta = 20.0 * math.log10(0.97 / 1.03)
    expected_offsets = [interface_delta + interface_freq_error[f] for f in gainmatch.SERIES_FREQUENCIES]
    check("Gain Match step 1: loopback reference records frequency-dependent interface L/R offset",
          len(loop_reference["rows"]) == 3
          and all(row["valid"] for row in loop_reference["rows"])
          and all(abs(row["delta_adc_db"] - expected) < 0.03
                  for row, expected in zip(loop_reference["rows"], expected_offsets)),
          f"reference={[round(row['delta_adc_db'], 4) for row in loop_reference['rows']]} dB; "
          f"expected={ [round(x, 4) for x in expected_offsets] }")

    # The DUT is 1.5x left and 1.0x right, on top of the same asymmetric
    # measurement interface (0.97x / 1.03x). Subtraction must recover 20log10(1.5).
    dut_backend = SimBackend(
        SimChannel(gain=0.97 * 1.5, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130),
        SimChannel(gain=1.03 * 1.0, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130), seed=19)
    rig_dut = Rig(dut_backend, Calibration(), fs=48000, blocks=2, settle=0.15)
    rig_dut.open()
    inject_interface_frequency_error(rig_dut)
    series_gm = gainmatch.measure_series(rig_dut, level_dbfs=-24.0, repeats=3,
                                         metadata={"dut": "synthetic 1.5x / 1.0x"},
                                         purpose="dut", reference=loop_reference)
    expected_gm = 20.0 * math.log10(1.5)
    deltas_gm = [row["delta_gain_db"] for row in series_gm["rows"]]
    check("Gain Match step 2: loopback correction removes interface imbalance",
          len(series_gm["rows"]) == 3 and series_gm["loopback_corrected"]
          and all(row["valid"] and row["repeat_stable"] for row in series_gm["rows"])
          and all(abs(value - expected_gm) < 0.04 for value in deltas_gm),
          f"corrected ΔG={['%.4f' % v for v in deltas_gm]} dB; DUT truth={expected_gm:.4f} dB; "
          f"uncorrected interface offset={interface_delta:.4f} dB")

    # The second step must not silently report raw ADC L/R as DUT gain.
    missing_ref_rejected = False
    try:
        gainmatch.measure_series(rig_dut, level_dbfs=-24.0, repeats=1, purpose="dut", reference=None)
    except ValueError as exc:
        missing_ref_rejected = "эталона петли" in str(exc)
    check("Gain Match: DUT measurement is blocked without loopback reference", missing_ref_rejected,
          "нет эталона rejected" if missing_ref_rejected else "missing reference was accepted")

    wrong_level_rejected = False
    try:
        gainmatch.measure_series(rig_dut, level_dbfs=-23.0, repeats=1,
                                 purpose="dut", reference=loop_reference)
    except ValueError as exc:
        wrong_level_rejected = "уровень генератора" in str(exc)
    check("Gain Match: mismatched generator level invalidates reference", wrong_level_rejected,
          "wrong level rejected" if wrong_level_rejected else "wrong level was accepted")

    mixer_reference = dict(loop_reference, mixer_fingerprint="baseline-mixer")
    original_fingerprint = gainmatch.mixer_fingerprint_from_hw
    gainmatch.mixer_fingerprint_from_hw = lambda _hw: "changed-mixer"
    mixer_change_rejected = False
    try:
        gainmatch.measure_series(rig_dut, level_dbfs=-24.0, repeats=1,
                                 purpose="dut", reference=mixer_reference)
    except ValueError as exc:
        mixer_change_rejected = "микшера" in str(exc)
    finally:
        gainmatch.mixer_fingerprint_from_hw = original_fingerprint
    check("Gain Match: changed ALSA mixer invalidates loopback reference", mixer_change_rejected,
          "mixer change rejected" if mixer_change_rejected else "mixer change was accepted")

    with tempfile.TemporaryDirectory(prefix="valvebench-gain-series-") as tmp_gain:
        ref_path = store.save_gain_reference(loop_reference, tmp_gain)
        loaded_ref = store.load_gain_reference("sim", 48000, -24.0,
                                               gainmatch.SERIES_FREQUENCIES, tmp_gain)
        gain_json, gain_csv = store.save_gain_series(series_gm, tmp_gain)
        saved_gain = json.loads(Path(gain_json).read_text(encoding="utf-8"))
        csv_lines = Path(gain_csv).read_text(encoding="utf-8").splitlines()
        check("Gain Match: loopback reference and DUT result persist in JSON/CSV",
              Path(ref_path).exists() and loaded_ref is not None
              and len(saved_gain["rows"]) == 3 and len(csv_lines) == 4
              and saved_gain["metadata"].get("dut") == "synthetic 1.5x / 1.0x"
              and "corrected_delta_gain_L_minus_R_db" in csv_lines[0],
              f"reference={'loaded' if loaded_ref else 'missing'}; JSON rows={len(saved_gain['rows'])}; "
              f"CSV columns={len(csv_lines[0].split(','))}")

    human = gainmatch.text_series(series_gm, tolerance_db=0.20)
    check("Gain Match: user report contains corrected verdict, not raw dBFS table",
          "ΔG L−R" in human and "КАНАЛЫ НЕ СОГЛАСОВАНЫ" in human
          and "dBFS" not in human,
          human.splitlines()[-2] if len(human.splitlines()) > 1 else human)

    # --- 0.1.19: достоверность Gain Match (события, C/R, огибающая, TARE, повторы) ---
    be_x = SimBackend(SimChannel(gain=0.97, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130),
                      SimChannel(gain=1.03, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130), seed=23)
    rig_x = Rig(be_x, Calibration(), fs=48000, blocks=2, settle=0.15)
    rig_x.open()
    be_x.notes = ["arecord: overrun!!! (at least 65107 frames)"]
    res_x = gainmatch.measure_series(rig_x, frequencies=(1000.0, 1500.0), level_dbfs=-24.0,
                                     repeats=1, purpose="loopback")
    check("0.1.19: аппаратные события (XRUN) фатальны — серия невалидна и события видны",
          res_x["events"]["fatal"] and not all(row["valid"] for row in res_x["rows"])
          and all("hardware_fault" in row["channels"]["L"]["validity_reasons"]
                  for row in res_x["rows"]),
          f"fatal={res_x['events']['fatal']}; "
          f"reasons={[row['channels']['L']['validity_reasons'] for row in res_x['rows']]}")

    be_n = SimBackend(SimChannel(gain=0.97, noise_dbfs=-34, hum50_dbfs=-130, hum100_dbfs=-130),
                      SimChannel(gain=1.03, noise_dbfs=-34, hum50_dbfs=-130, hum100_dbfs=-130), seed=24)
    rig_n = Rig(be_n, Calibration(), fs=48000, blocks=2, settle=0.15)
    rig_n.open()
    res_n = gainmatch.measure_series(rig_n, frequencies=(1000.0, 1500.0), level_dbfs=-24.0,
                                     repeats=1, purpose="loopback")
    reasons_n = sorted({r for row in res_n["rows"] for c in ("L", "R")
                        for r in row["channels"][c]["validity_reasons"]})
    noise_only_ok = "low_carrier_residual" in reasons_n and not all(
        row["valid"] for row in res_n["rows"])
    be_c = SimBackend(SimChannel(gain=0.97, noise_dbfs=-110, hum50_dbfs=-130, hum100_dbfs=-130,
                                 a3=6.0),
                      SimChannel(gain=1.03, noise_dbfs=-110, hum50_dbfs=-130, hum100_dbfs=-130,
                                 a3=6.0), seed=25)
    rig_c = Rig(be_c, Calibration(), fs=48000, blocks=2, settle=0.15)
    rig_c.open()
    res_c = gainmatch.measure_series(rig_c, frequencies=(1000.0, 1500.0), level_dbfs=-24.0,
                                     repeats=2, purpose="loopback")
    lamp_ok = all(row["valid"] for row in res_c["rows"]) and \
        res_c["rows"][0]["channels"]["L"]["sinad_db"] < 60.0
    check("0.1.19: ворота — низкий C/R бракует шум, гармоничный каскад (THD≈10%) остаётся валидным",
          noise_only_ok and lamp_ok,
          f"шум-34: reasons={reasons_n}; лампа a3=6.0: "
          f"valid={[row['valid'] for row in res_c['rows']]}, "
          f"SINAD={res_c['rows'][0]['channels']['L']['sinad_db']:.1f}")

    be_b = SimBackend(SimChannel(gain=0.97, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130),
                      SimChannel(gain=1.03, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130), seed=26)
    rig_b = Rig(be_b, Calibration(), fs=48000, blocks=2, settle=0.15)
    rig_b.open()
    original_pr = be_b.play_record

    def burst_pr(out, tail=0.8):
        rec = original_pr(out, tail)
        n = int(0.05 * rig_b.fs)
        tt = np.arange(n) / rig_b.fs
        rec[int(0.9 * rig_b.fs):int(0.9 * rig_b.fs) + n, 0] += 10 ** (-37.0 / 20) * np.sin(2 * np.pi * 1000.0 * tt)
        return rec

    be_b.play_record = burst_pr
    res_b = gainmatch.measure_series(rig_b, frequencies=(1000.0, 1500.0), level_dbfs=-24.0,
                                     repeats=1, purpose="loopback")
    be_b.play_record = original_pr
    check("0.1.19: всплеск внутри окна бракуется подоконным контролем (unstable_carrier)",
          not res_b["rows"][0]["valid"]
          and "unstable_carrier" in res_b["rows"][0]["channels"]["L"]["validity_reasons"],
          f"valid={[row['valid'] for row in res_b['rows']]}; "
          f"reasons={res_b['rows'][0]['channels']['L']['validity_reasons']}; "
          f"envelope={res_b['rows'][0]['channels']['L']['repeats'][0]['envelope']}")

    orig_an = rig_c._tone_an

    def warn_an(win, ch, k, nharm=9, coherent_line_floors=None):
        r = orig_an(win, ch, k, nharm=nharm, coherent_line_floors=coherent_line_floors)
        r = dict(r)
        r["warnings"] = ["тон не когерентен окну (тест)"]
        return r

    rig_c._tone_an = warn_an
    res_w = gainmatch.measure_series(rig_c, frequencies=(1000.0, 1500.0), level_dbfs=-24.0,
                                     repeats=1, purpose="loopback")
    rig_c._tone_an = orig_an
    check("0.1.19: предупреждения analyze_tone сохраняются в записи повторов",
          any("не когерентен" in w
              for row in res_w["rows"] for c in ("L", "R")
              for q in row["channels"][c]["repeats"] for w in q["analyzer_warnings"]),
          "analyzer_warnings в повторах присутствуют")

    tr = gainmatch.GainTracker()
    fs_t = 48000
    ring = np.column_stack([0.1 * np.sin(2 * np.pi * 1000 * np.arange(fs_t) / fs_t)] * 2)
    ctx1 = {"freq": 1000.0, "level_dbfs": -20.0, "fs": fs_t, "route": 0,
            "device_key": "sim", "mixer_hash": "h", "cal_sig": "c"}
    tr.update(ring, fs_t, 1000.0, -20.0, 0, None, now=0.0, ctx=ctx1)
    tr.tare(ctx=ctx1)
    ring10 = np.column_stack([0.1 * np.sin(2 * np.pi * 10000 * np.arange(fs_t) / fs_t)] * 2)
    ctx2 = dict(ctx1, freq=10000.0)
    st_t = tr.update(ring10, fs_t, 10000.0, -20.0, 0, None, now=1.0, ctx=ctx2)
    check("0.1.19: контекстный TARE — смена частоты сбрасывает ноль с причиной",
          st_t["valid"] and st_t.get("tare_reset_reason") == "частота"
          and abs(st_t["tare_db"]) < 1e-12 and "TARE сброшен" in st_t["msg"],
          f"reason={st_t.get('tare_reset_reason')}; tare={st_t['tare_db']}; msg={st_t['msg'][:50]}")

    be_1 = SimBackend(SimChannel(gain=0.97, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130),
                      SimChannel(gain=1.03, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130), seed=27)
    rig_1 = Rig(be_1, Calibration(), fs=48000, blocks=2, settle=0.15)
    rig_1.open()
    res_1 = gainmatch.measure_series(rig_1, frequencies=(1000.0, 1500.0),
                                     level_dbfs=-24.0, repeats=1, purpose="loopback")
    ref_rejected = False
    try:
        gainmatch.build_loopback_reference(res_1)
    except ValueError as exc:
        ref_rejected = "повторяемость" in str(exc)
    be_ref = SimBackend(SimChannel(gain=0.97, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130),
                        SimChannel(gain=1.03, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130), seed=31)
    rig_ref = Rig(be_ref, Calibration(), fs=48000, blocks=2, settle=0.15)
    rig_ref.open()
    loop_ref = gainmatch.build_loopback_reference(
        gainmatch.measure_series(rig_ref, frequencies=(1000.0, 1500.0),
                                 level_dbfs=-24.0, repeats=2, purpose="loopback"))
    res_1d = gainmatch.measure_series(rig_1, frequencies=(1000.0, 1500.0),
                                      level_dbfs=-24.0, repeats=1, purpose="dut",
                                      reference=loop_ref)
    text_1 = gainmatch.text_series(res_1d, 0.20)
    check("0.1.19: repeats=1 — «повторяемость не оценена», эталон петли не создаётся, "
          "вердикт предварительный",
          res_1["repeatability"] == "not_assessed"
          and res_1["rows"][0]["repeat_stable"] is None
          and ref_rejected and "ПРЕДВАРИТЕЛЬНЫЙ" in text_1
          and "не оценена (один повтор)" in text_1,
          f"repeatability={res_1['repeatability']}; repeat_stable="
          f"{res_1['rows'][0]['repeat_stable']}; эталон отклонён={ref_rejected}")

    be_r = SimBackend(SimChannel(gain=0.97, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130),
                      SimChannel(gain=1.03, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130), seed=28)
    rig_r = Rig(be_r, Calibration(), fs=48000, blocks=2, settle=0.15)
    rig_r.open()
    res_r = gainmatch.measure_series(rig_r, frequencies=(100.0, 1000.0, 10000.0),
                                     level_dbfs=-20.0, repeats=1, purpose="loopback",
                                     riaa_compensate=True)
    lv = res_r["generator_levels_dbfs"]
    overflow_rejected = False
    try:
        gainmatch.measure_series(rig_r, frequencies=(100.0, 1000.0, 10000.0),
                                 level_dbfs=-12.0, repeats=1, purpose="loopback",
                                 riaa_compensate=True)
    except ValueError as exc:
        overflow_rejected = "переполнение" in str(exc) or "полной шкалы" in str(exc)
    check("0.1.19: RIAA-компенсация — уровни по обратной RIAA, переполнение ЦАП отклоняется",
          abs(lv[100.0] - (-20.0 - 13.09)) < 0.2 and abs(lv[10000.0] - (-20.0 + 13.73)) < 0.2
          and lv[10000.0] < -0.5 and overflow_rejected,
          f"уровни={{{', '.join(f'{f:g}: {v:+.2f}' for f, v in sorted(lv.items()))}}}; "
          f"отказ при базе −12: {overflow_rejected}")

    res_p = gainmatch.measure_series(rig_r, frequencies=gainmatch.FREQUENCY_PROFILES["mains_avoidance_50"][1],
                                     level_dbfs=-24.0, repeats=1, purpose="loopback",
                                     frequency_profile="mains_avoidance_50")
    check("0.1.19: профиль Mains Avoidance (94/994/9994) измеряется и фиксируется в результате",
          res_p["frequency_profile"] == "mains_avoidance_50"
          and all(row["valid"] for row in res_p["rows"])
          and sorted(gainmatch.FREQUENCY_PROFILES) == ["mains_avoidance_50", "standard"],
          f"profile={res_p['frequency_profile']}; valid={[row['valid'] for row in res_p['rows']]}")

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
                  "tone_continuity": {"L_steady": {"dropout_episodes": 1, "longest_dropout_ms": 80.0},
                                      "R_steady": {"dropout_episodes": 0, "longest_dropout_ms": 0.0}},
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
              and compare_rows[0]["capture"] == "hw:2,0" and "hw:3,0 → hw:2,0" in compare_text
              and compare_rows[0]["L_carrier_dropout_episodes"] == 1
              and compare_rows[0]["R_carrier_dropout_episodes"] == 0,
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

    # 0.1.11: carrier continuity is independent from the harmonic-residual detector.
    # A constant tone must not be declared interrupted.
    fs_car, dur_car, f_car = 48000, 1.0, 997.0
    t_car = np.arange(int(fs_car * dur_car), dtype=np.float64) / fs_car
    amp_car = 10 ** (-23.4 / 20)
    clean_carrier = amp_car * np.sin(2 * np.pi * f_car * t_car + 0.31)
    ref_car = steady._tone_level_at(clean_carrier, fs_car, f_car)
    clean_cont, _clean_track = steady._tone_continuity_track(
        clean_carrier, fs_car, "L_steady", "L", 0.0, dur_car, f_car, ref_car)
    check("steady: stable carrier has no dropout episodes",
          clean_cont["available"] and clean_cont["dropout_episodes"] == 0
          and clean_cont["longest_dropout_ms"] == 0.0
          and abs(clean_cont["median_tone_dbfs"] - ref_car) < 0.05,
          f"episodes={clean_cont['dropout_episodes']}; ref/median={ref_car:.2f}/{clean_cont['median_tone_dbfs']:.2f} dBFS")

    # A true 80-ms loss of carrier must be reported as one carrier dropout,
    # rather than just an additive residual burst.
    dropped_carrier = clean_carrier.copy()
    dropped_carrier[int(0.40 * fs_car):int(0.48 * fs_car)] = 0.0
    drop_cont, drop_track = steady._tone_continuity_track(
        dropped_carrier, fs_car, "L_steady", "L", 0.0, dur_car, f_car, ref_car)
    check("steady: 80-ms carrier dropout is detected independently of residual noise",
          drop_cont["dropout_episodes"] == 1 and 70.0 <= drop_cont["longest_dropout_ms"] <= 90.0
          and drop_cont["below_threshold_subwindows"] >= 7,
          f"episodes={drop_cont['dropout_episodes']}; longest={drop_cont['longest_dropout_ms']:.1f} ms; "
          f"low windows={drop_cont['below_threshold_subwindows']}")

    # An additive broadband disturbance can raise the residual while the
    # carrier remains present; it must not be confused with a dropout.
    rng_car = np.random.default_rng(11011)
    additive_carrier = clean_carrier.copy()
    additive_carrier[int(0.40 * fs_car):int(0.50 * fs_car)] += rng_car.normal(0.0, 0.015, int(0.10 * fs_car))
    additive_cont, _add_track = steady._tone_continuity_track(
        additive_carrier, fs_car, "L_steady", "L", 0.0, dur_car, f_car, ref_car)
    check("steady: additive noise burst is not classified as carrier dropout",
          additive_cont["dropout_episodes"] == 0,
          f"dropout episodes={additive_cont['dropout_episodes']}; minimum carrier={additive_cont['min_tone_dbfs']:.2f} dBFS")

    # Excluding a guard around active boundaries prevents fade-in/out windows
    # from leaking into the steady-state statistics.
    guarded = steady._guard_steady_ranges(
        {"idle_pre": (0.0, 2.0), "L_steady": (10.0, 50.0), "R_steady": (60.0, 100.0)}, 0.25)
    check("steady: analysis guard excludes active-state fade boundaries only",
          guarded["L_steady"] == (10.25, 49.75) and guarded["R_steady"] == (60.25, 99.75)
          and guarded["idle_pre"] == (0.0, 2.0),
          f"L={guarded['L_steady']}; R={guarded['R_steady']}; idle={guarded['idle_pre']}")

    state_ranges, guard_s = steady._build_steady_state_ranges(1.0, 15.0, 40.0, 8.0, 1.0, 0.1)
    check("steady: idle gap excludes the R fade-in and active ranges exclude both fades",
          abs(guard_s - 0.25) < 1e-12
          and state_ranges["L_steady"] == (17.25, 56.75)
          and state_ranges["idle_gap"] == (58.5, 65.5)
          and state_ranges["R_steady"] == (67.25, 106.75),
          f"guard={guard_s:.2f}s; L={state_ranges['L_steady']}; gap={state_ranges['idle_gap']}; R={state_ranges['R_steady']}")


    # 0.1.12-P0: потоковый декодер и WAV-писатель; память ограничена блоком.
    with tempfile.TemporaryDirectory(prefix="valvebench-p0-") as td:
        td = Path(td)
        rng_p0 = np.random.default_rng(1204)
        n_p0 = 3 * 48000 * 2
        x_p0 = rng_p0.normal(0.0, 0.1, (n_p0 // 2, 2))
        raw_p0 = td / "cap.raw"
        raw_p0.write_bytes(to_pcm(x_p0, "S24_3LE"))
        reader_p0 = pcm_mod.StreamPcmReader(raw_p0, "S24_3LE", 2)
        full_p0 = pcm_mod.from_pcm(raw_p0.read_bytes(), "S24_3LE", 2)
        part_p0 = reader_p0.read_frames(1000, 120000)   # границы блоков внутри
        wav_p0 = td / "cap.wav"
        frames_p0 = pcm_mod.stream_to_float32_wav(raw_p0, "S24_3LE", 2, wav_p0, 48000)
        from scipy.io import wavfile as _wav
        fs_w, data_w = _wav.read(wav_p0)
        check("P0: StreamPcmReader.read_frames совпадает с from_pcm (включая границы блоков)",
              reader_p0.frames == n_p0 // 2
              and np.array_equal(part_p0, full_p0[1000:121000]),
              f"frames={reader_p0.frames}; макс. расхождение="
              f"{float(np.max(np.abs(part_p0 - full_p0[1000:121000]))):.2e}")
        check("P0: stream_to_float32_wav → scipy-читаемый float32 WAV со точными значениями",
              frames_p0 == n_p0 // 2 and fs_w == 48000 and data_w.dtype == np.float32
              and np.array_equal(data_w, full_p0.astype(np.float32)),
              f"frames={frames_p0}; dtype={data_w.dtype}; макс. расхождение с float32(decoded)="
              f"{float(np.max(np.abs(data_w - full_p0.astype(np.float32)))):.2e}")


    # 0.1.12-p1: XRUN-скан, классификация длины, onset по огибающей, мёртвая зона.
    big = "ok\n" + ("aplay: underrun!!! (at least 3.2 ms long)\n" * 4000)  # ~150 КБ
    sc = io_health_mod.scan_xrun_stderr(big)
    check("P1: XRUN-скан распознаёт underrun/overrun и не давится >64 КБ stderr",
          sc["underrun_count"] == 4000 and sc["count"] == 4000
          and len(sc["messages"]) == 20 and sc["messages_truncated"]
          and sc["stderr_bytes"] > 65536,
          f"underruns={sc['underrun_count']}; сохранено строк={len(sc['messages'])} "
          f"(лимит {sc['stderr_bytes']} B stderr)")
    sc2 = io_health_mod.scan_xrun_stderr("ok\narecord: overrun!!! (at least 1.1 ms long)\n")
    check("P1: overrun записи распознаётся отдельно",
          sc2["overrun_count"] == 1 and sc2["count"] == 1,
          f"overruns={sc2['overrun_count']}")
    ls = (io_health_mod.length_status(100, 100, 48000)["status"],
          io_health_mod.length_status(100 - 4800, 100, 48000)["status"],
          io_health_mod.length_status(100 + 4800, 100, 48000)["status"])
    check("P1: длина записи классифицируется ok/short/long",
          ls == ("ok", "short", "long"), f"статусы={ls}")
    # onset: синтетический сигнал с известным смещением +0.25 с (fade 1 с)
    fs_on = 48000
    dur_on = 10.0
    t_on = np.arange(int(fs_on * dur_on)) / fs_on
    tau_true = 0.25
    t0_exp = 4.0
    env_on = np.clip((t_on - (t0_exp + tau_true)) / 1.0, 0.0, 1.0)
    rng_on = np.random.default_rng(1408)
    sig_on = rng_on.normal(0, 1e-5, t_on.size) + 10 ** (-12 / 20) * np.sin(2 * np.pi * 997.0 * t_on) * env_on
    sig_st = np.column_stack((sig_on, sig_on))   # R = L: одинаковый onset
    on = io_health_mod.detect_playback_onset(sig_st, fs_on, 997.0,
                                             t0_exp, t0_exp, 1.0)
    check("P1: onset оценивается по форме огибающей (сдвиг +0.25 с восстанавливается)",
          on["reliable"] and on["applied"] and abs(on["deviation_s"] - tau_true) < 0.02,
          f"dev={on['deviation_s']:+.4f} с (истина +0.25); SNR L {on['confidence']['snr_db_L']:.0f} dB; "
          f"residual {on['confidence']['residual_fraction_L']:.3f}; applied={on['applied']}")
    # мёртвая зона: сдвиг 0.02 с не должен двигать диапазоны
    env_db = np.clip((t_on - (t0_exp + 0.02)) / 1.0, 0.0, 1.0)
    sig_db = rng_on.normal(0, 1e-5, t_on.size) + 10 ** (-12 / 20) * np.sin(2 * np.pi * 997.0 * t_on) * env_db
    on2 = io_health_mod.detect_playback_onset(np.column_stack((sig_db, sig_db)), fs_on, 997.0,
                                              t0_exp, t0_exp, 1.0)
    check("P1: мёртвая зона — смещение 0.02 с не применяется (метрики остаются прежними)",
          on2["reliable"] and not on2["applied"] and abs(on2["deviation_s"] - 0.02) < 0.02,
          f"dev={on2['deviation_s']:+.4f}; applied={on2['applied']} (ожидалось False)")
    # асимметрия раннего/позднего старта, граница поиска, None при неудаче
    def mk_onset(tau, with_tone=True):
        tt = np.arange(int(fs_on * 14.0)) / fs_on
        env = np.clip((tt - (t0_exp + tau)) / 1.0, 0.0, 1.0) if with_tone else np.zeros_like(tt)
        xx = rng_on.normal(0, 1e-5, tt.size) + 10 ** (-12 / 20) * np.sin(2 * np.pi * 997.0 * tt) * env
        return np.column_stack((xx, xx))
    ok_all = True
    det = []
    for tau, tone in ((-0.3, True), (0.3, True), (-0.8, True), (0.8, True), (1.2, True), (0.0, False)):
        on = io_health_mod.detect_playback_onset(mk_onset(tau, tone), fs_on, 997.0,
                                                 t0_exp, t0_exp, 1.0)
        if not tone:
            good = ((not on["reliable"]) and on["measured_onset_s"] is None
                    and on["confidence"]["snr_db_L"] is not None
                    and on["confidence"]["snr_db_L"] < 26.0)
        elif abs(tau) <= 1.0:
            good = (on["reliable"] and on["deviation_s"] is not None
                    and abs(on["deviation_s"] - tau) < 0.02
                    and on["measured_onset_s"] is not None
                    and not on["confidence"]["search_boundary_hit_L"])
        else:
            good = ((not on["reliable"]) and on["measured_onset_s"] is None
                    and on["confidence"]["search_boundary_hit_L"])
        ok_all &= good
        det.append(f"tau={tau:+.1f}{'' if tone else ' (без тона)'}: rel={on['reliable']} "
                   f"dev={on['deviation_s']} hit={on['confidence']['search_boundary_hit_L']}")
    xm = mk_onset(0.3)
    on_m = io_health_mod.detect_playback_onset(
        np.column_stack((mk_onset(0.3)[:, 0], mk_onset(0.5)[:, 0])), fs_on, 997.0,
        t0_exp, t0_exp, 1.0)
    ok_all &= (not on_m["reliable"]) and on_m["measured_onset_s"] is None
    det.append(f"L/R 0.3/0.5: rel={on_m['reliable']} (рассогласование)")
    # R-канал не подтверждён (L надёжен, R не обнаружен): диагностический статус,
    # основной алгоритм не меняется
    sig_lr = np.column_stack((mk_onset(0.3)[:, 0],
                              rng_on.normal(0, 0.5, int(fs_on * 14.0))))
    on_u = io_health_mod.detect_playback_onset(sig_lr, fs_on, 997.0, t0_exp, t0_exp, 1.0)
    on_s = io_health_mod.detect_playback_onset(mk_onset(0.3), fs_on, 997.0,
                                               t0_exp, t0_exp, 1.0)
    check("P1: right_channel_unverified — привязка только по L видна в JSON, алгоритм прежний",
          on_u["reliable"] and on_u["right_channel_unverified"] and on_u["applied"]
          and not on_s["right_channel_unverified"],
          f"L+шумный R: reliable={on_u['reliable']}, right_unverified="
          f"{on_u['right_channel_unverified']}, dev={on_u['deviation_s']:+.3f}; "
          f"симметричный случай: right_unverified={on_s['right_channel_unverified']}")
    check("P1: onset симметричен для раннего/позднего старта; граница поиска и отсутствие тона дают None",
          ok_all, " | ".join(det))

    # UI preference migration is one-time: update only old defaults, preserve user choices.
    h = lambda title: "pref_" + hashlib.sha1(str(title).encode("utf-8")).hexdigest()[:12]
    old_ui = {
        "resp/" + h("Показывать"): 0,
        "resp/" + h("Генератор"): 0,
        "resp/" + h("Уровень @1 кГц, dBFS"): -20.0,
        "imd/" + h("Пик суммы, dBFS"): -12.0,
        "noise_response/" + h("График"): 0,
        "live/" + h("Уровень генератора, dBFS"): -20.0,
        "thd/" + h("Уровень генератора, dBFS"): -12.0,
        "live/" + h("Уровень теста, dBFS"): -30.0,
        "noise_response/" + h("Цифровой RMS-уровень, dBFS"): -42.0,
        "spectrum/pref_spectrum_fft": 0,
    }
    # Fix the helper to use the same hashlib function in a clean independent expression.
    old_ui = {k: v for k, v in old_ui.items()}
    # USB calibration identity survives port changes when VID:PID is unique,
    # but remains disambiguated by port for identical serial-less cards.
    usb_a = usb_profile_identity("0b05", "180c", "", "1-1", 1, 0)
    usb_b = usb_profile_identity("0B05", "180C", "", "1-2", 1, 0)
    usb_dup_a = usb_profile_identity("0b05", "180c", "", "1-1", 2, 0)
    usb_dup_b = usb_profile_identity("0b05", "180c", "", "1-2", 2, 0)
    check("Per-card calibration identity survives moving a unique USB card to another port",
          usb_a == usb_b and usb_dup_a != usb_dup_b,
          f"unique={usb_a == usb_b}; duplicate ports distinct={usb_dup_a != usb_dup_b}")

    migrated_ui = preferences.migrate_0_1_16_defaults(old_ui)
    migrated_again = preferences.migrate_0_1_16_defaults(migrated_ui)
    check("UI migration updates RIAA/low-level defaults once and preserves user choices",
          migrated_ui["resp/" + h("Показывать")] == 1
          and migrated_ui["noise_response/" + h("График")] == 1
          and migrated_ui["live/" + h("Уровень генератора, dBFS")] == -6
          and migrated_ui["thd/" + h("Уровень генератора, dBFS")] == -12.0
          and migrated_ui["live/" + h("Уровень теста, dBFS")] == -12
          and migrated_ui["noise_response/" + h("Желаемый RMS-уровень шума, dBFS")] == -12.0
          and migrated_ui["resp/" + h("Тестовый сигнал")] == 0
          and migrated_ui["resp/" + h("Уровень генератора @1 кГц, dBFS")] == -6.0
          and migrated_ui["imd/" + h("Пиковый уровень суммы двух тонов, dBFS")] == -6.0
          and migrated_ui["spectrum/pref_spectrum_fft"] == 5
          and migrated_again == migrated_ui,
          "старые значения по умолчанию исправлены; выбранные значения и повторный запуск миграции не изменяют настройки")
    user_ui = {"resp/" + h("Показывать"): 3, "live/" + h("Уровень генератора, dBFS"): -9.5}
    migrated_user = preferences.migrate_0_1_16_defaults(user_ui)
    check("UI migration does not overwrite non-default user selections",
          migrated_user["resp/" + h("Показывать")] == 3
          and migrated_user["live/" + h("Уровень генератора, dBFS")] == -9.5,
          f"RIAA view={migrated_user['resp/' + h('Показывать')]}; generator={migrated_user['live/' + h('Уровень генератора, dBFS')]} dBFS")

    passed = sum(q["ok"] for q in checks)
    return {"passed": passed, "total": len(checks), "checks": checks}


def format_selftest(r):
    lines = [f"ValveBench self-test: {r['passed']}/{r['total']} PASS"]
    for q in r["checks"]:
        lines.append(f"{'PASS' if q['ok'] else 'FAIL'}  {q['name']}: {q['detail']}")
    return "\n".join(lines)
