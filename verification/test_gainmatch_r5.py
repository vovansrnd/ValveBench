#!/usr/bin/env python3
"""Targeted R5 regressions for the 0.1.19 Gain Match / transport fixes."""
from __future__ import annotations

import math
import sys
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from valvebench import gainmatch  # noqa: E402
from valvebench.backend import AlsaCli  # noqa: E402


def amp(db):
    return 10.0 ** (float(db) / 20.0)


def ring_for(fs=48000, seconds=0.6, f=1000.03, level_db=-20.0, h9_db=None,
             outband_db=None, noise_db=None, seed=1):
    n = int(round(fs * seconds))
    t = np.arange(n, dtype=np.float64) / fs
    a = amp(level_db)
    x = a * np.sin(2 * np.pi * f * t + 0.23)
    if h9_db is not None:
        x += a * amp(h9_db) * np.sin(2 * np.pi * 9 * f * t + 0.71)
    if outband_db is not None:
        x += a * amp(outband_db) * np.sin(2 * np.pi * 25000.0 * t + 0.4)
    if noise_db is not None:
        rng = np.random.default_rng(seed)
        x += rng.normal(0.0, amp(noise_db), n)
    return np.column_stack((x, x))


def check(name, ok, detail):
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    if not ok:
        raise AssertionError(name + ": " + detail)


def test_right_channel_frequency_fallback():
    fs = 48000
    ring = ring_for(fs=fs, f=1000.03, level_db=-20.0, noise_db=-105.0)
    calls = []

    def fake_estimate(x, sample_rate, nominal):
        calls.append(x)
        if len(calls) == 1:  # left estimator fails; right estimator succeeds
            return float(nominal), False
        return 1000.03, True

    tracker = gainmatch.GainTracker()
    with patch.object(gainmatch, "estimate_signal_frequency", side_effect=fake_estimate):
        state = tracker.update(ring, fs, 1000.0, -20.0, 0, None, now=0.0)
    check("Right-channel frequency fallback retains success flag",
          len(calls) == 2 and state.get("valid") and abs(state["freq_est_hz"] - 1000.03) < 1e-9,
          f"estimator calls={len(calls)}, valid={state.get('valid')}, f_fit={state.get('freq_est_hz')}")


def test_frequency_quantized_before_cache_lookup():
    fs = 48000
    ring = ring_for(fs=fs, f=1000.03, level_db=-20.0, noise_db=-100.0)
    original_fit = gainmatch.tone_fit_harmonics
    seen = []
    estimates = [1000.033 + (i % 5) * 0.0005 for i in range(30)]
    counter = {"i": 0}

    def fake_estimate(x, sample_rate, nominal):
        value = estimates[counter["i"] % len(estimates)]
        counter["i"] += 1
        return value, True

    def record_fit(x, sample_rate, frequency, *args, **kwargs):
        seen.append(float(frequency))
        return original_fit(x, sample_rate, frequency, *args, **kwargs)

    gainmatch._PINV_CACHE.clear()
    gainmatch._SUB_CACHE.clear()
    gainmatch._PINV_STATS.update(hit=0, miss=0)
    tracker = gainmatch.GainTracker()
    with patch.object(gainmatch, "estimate_signal_frequency", side_effect=fake_estimate), \
         patch.object(gainmatch, "tone_fit_harmonics", side_effect=record_fit):
        states = [tracker.update(ring, fs, 1000.0, -20.0, 0, None, now=i * 0.075)
                  for i in range(30)]
    unique_f = sorted({round(v, 6) for v in seen})
    ok_freqs = {1000.0, 1000.03}
    check("Frequency candidates are quantized before residual scoring",
          set(unique_f).issubset(ok_freqs) and len(unique_f) == 2,
          f"basis frequencies={unique_f}")
    check("Detuned Live updates reuse bounded cache keys",
          len(gainmatch._PINV_CACHE) <= 2 and gainmatch._PINV_STATS["hit"] > 0
          and all(st.get("valid") for st in states),
          f"cache entries={len(gainmatch._PINV_CACHE)}, stats={gainmatch._PINV_STATS}")


def test_live_harmonics_and_sinad_band():
    fs = 96000
    ring = ring_for(fs=fs, f=1000.0, level_db=-20.0, h9_db=-25.0,
                    outband_db=-10.0, noise_db=-105.0)
    state = gainmatch.GainTracker().update(ring, fs, 1000.0, -20.0, 0, None, now=0.0)
    # Static-series C/R excludes H2..H9. Live must use the same harmonic family.
    ok_live = state.get("valid") and min(state["carrier_residual_db"]) > 30.0
    # The out-of-band 25-kHz component is deliberately much larger than H9;
    # it must not contaminate Live SINAD when the measurement band ends at 20 kHz.
    x = ring[-int(0.5 * fs):, 0]
    _, _, _, r_full = gainmatch.tone_fit_harmonics(
        x, fs, 1000.0, nharm=9, f_max=0.45 * fs, f_band_hi=20000.0)
    fundamental_peak = amp(-20.0)
    sinad = 20.0 * math.log10((fundamental_peak / math.sqrt(2.0)) / max(r_full, 1e-300))
    check("Live C/R removes harmonics through H9 like Static series",
          ok_live, f"valid={state.get('valid')}, C/R={state.get('carrier_residual_db')}")
    check("Live SINAD uses the Static 20 Hz–20 kHz band",
          abs(sinad - 25.0) < 0.5,
          f"SINAD={sinad:.2f} dB with a -10 dBc spur at 25 kHz excluded")


def test_fresh_window_and_tare_preservation():
    fs = 96000
    baseline = 123456
    f0 = gainmatch.live_fresh_frames(baseline + int(0.6 * fs) - 1, baseline)
    f1 = gainmatch.live_fresh_frames(baseline + int(0.6 * fs), baseline)
    check("Transport recovery waits for 0.6 s of new capture frames",
          not gainmatch.live_fresh_window_ready(f0, fs, 0.6)
          and gainmatch.live_fresh_window_ready(f1, fs, 0.6),
          f"one frame short={f0}, ready={f1}")
    tracker = gainmatch.GainTracker()
    tracker.tare(value=0.375, ctx={"freq": 1000.0})
    tracker.reset()
    check("Clearing Live filter history preserves user TARE",
          tracker.tare_db == 0.375 and tracker.tare_ctx == {"freq": 1000.0},
          f"tare_db={tracker.tare_db}, tare_ctx={tracker.tare_ctx}")


def test_xrun_counter_transitions_and_transport_staleness():
    initial_clean = gainmatch.live_xrun_state(None, None, 1.0, 0)
    initial_bad = gainmatch.live_xrun_state(None, None, 1.0, 2)
    held = gainmatch.live_xrun_state(2, 1.0, 1.4, 2)
    expired = gainmatch.live_xrun_state(2, 1.0, 2.1, 2)
    increased = gainmatch.live_xrun_state(2, 1.0, 3.0, 3)
    reset = gainmatch.live_xrun_state(3, 3.0, 4.0, 0)
    ok = (not initial_clean[0] and initial_bad[0] and held[0] and not expired[0]
          and increased[0] and reset[0])
    check("XRUN detects first nonzero, increment, counter reset and hold expiry", ok,
          f"clean={initial_clean[0]}, first={initial_bad}, held={held[0]}, expired={expired[0]}, "
          f"increment={increased}, reset={reset}")

    class Proc:
        def poll(self):
            return None

    backend = AlsaCli("hw:0,0")
    now = time.monotonic()
    backend.live_running = True
    backend._live_cap = Proc()
    backend._live_play = Proc()
    backend._live_err_files = []
    backend._live_log_offsets = [0, 0]
    backend._live_started_at = now - 1.0
    backend._live_capture_last_progress_at = None
    backend._live_playback_last_progress_at = now
    backend._live_progress_timeout_s = 0.5
    stale = backend.live_status()
    issue = gainmatch.live_transport_problem(stale)
    backend._live_capture_last_progress_at = time.monotonic()
    fresh = backend.live_status()
    recovered = gainmatch.live_transport_problem(fresh)
    check("Live backend detects a live-but-stalled capture process",
          stale["capture_stale"] and issue is not None and "АЦП" in issue
          and not fresh["capture_stale"] and recovered is None,
          f"stale={stale['capture_stale']} ({issue}); after progress={fresh['capture_stale']} ({recovered})")


def test_cache_byte_budget_is_strict():
    old_budget = gainmatch._PINV_CACHE_BUDGET
    old_cache = gainmatch._PINV_CACHE.copy()
    try:
        gainmatch._PINV_CACHE.clear()
        gainmatch._PINV_CACHE_BUDGET = 16
        gainmatch._cache_put(gainmatch._PINV_CACHE, ("resident",), np.zeros(1, dtype=np.float64),
                             gainmatch._PINV_CACHE_MAX)
        gainmatch._cache_put(gainmatch._PINV_CACHE, ("oversize",), np.zeros(4, dtype=np.float64),
                             gainmatch._PINV_CACHE_MAX)
        rejected = len(gainmatch._PINV_CACHE) == 1 and ("resident",) in gainmatch._PINV_CACHE
    finally:
        gainmatch._PINV_CACHE_BUDGET = old_budget
        gainmatch._PINV_CACHE.clear()
        gainmatch._PINV_CACHE.update(old_cache)
    check("LRU cache rejects an oversized profile without evicting resident entries", rejected,
          f"temporary budget=16 bytes; resident entry preserved={rejected}")


def test_static_carrier_residual_includes_hum():
    analysis = {"noise_dbfs": -90.0, "hum": [{"f": 50.0, "dbfs": -40.0},
                                              {"f": 100.0, "dbfs": -46.0}]}
    got = gainmatch.calc_carrier_residual_db(-24.0, analysis)
    expected = -24.0 - 10.0 * math.log10(10.0 ** (-40.0 / 10.0) + 10.0 ** (-46.0 / 10.0)
                                          + 10.0 ** (-90.0 / 10.0))
    check("Static C/R includes mains hum power while keeping hum separately reported",
          abs(got - expected) < 1e-10 and got < 16.0,
          f"carrier/residual={got:.3f} dB (expected {expected:.3f} dB)")


def main():
    test_right_channel_frequency_fallback()
    test_frequency_quantized_before_cache_lookup()
    test_live_harmonics_and_sinad_band()
    test_fresh_window_and_tare_preservation()
    test_xrun_counter_transitions_and_transport_staleness()
    test_cache_byte_budget_is_strict()
    test_static_carrier_residual_includes_hum()
    print("R5 targeted regressions: 11 checks PASS")


if __name__ == "__main__":
    main()
