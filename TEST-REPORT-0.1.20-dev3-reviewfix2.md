# Test report — ValveBench 0.1.20-dev3 reviewfix2

## Targeted review regressions

- T1: PASS — H2=−32 dBc, H3=−40 dBc relative to a −20 dBFS carrier; Live SINAD=31.36 dB,
  independent reference=31.36 dB.
- C2: PASS — numerical result matches SINAD and `gainview.py` visibly labels it SINAD.
- T8: PASS — warning for 9994 Hz / 200×49.97 Hz.
- T8b: PASS — 60-Hz dynamic alternative generated for 1020 Hz collision (1026.5 Hz in this run).
- T8c + UI logic: PASS — LivePage shows an explicit collision warning beside generator settings.
- U5: PASS — one recorded run: cache miss 22.0 ms (warm-up max 56.1 ms); hit p50 7.0 ms,
  hit p95 21.0 ms, hit max 41.1 ms; stationary p50 7.3 ms, p95 11.5 ms,
  p99 28.8 ms, max 40.4 ms. Timing is environment-dependent.

## Regression groups

- `valvebench.selftest.run_selftest()`: 96/96 PASS.
- `verification/test_gainmatch_0119.py`, run by independent groups:
  - A–E: 44/44 PASS
  - F–M: 24/24 PASS
  - N–R: 20/20 PASS
  - S: 5/5 PASS
  - T–U: 17/17 PASS
  - Total across these separately executed groups: 110/110 PASS.
- `verification/test_gainmatch_r5.py`: 11/11 PASS.
- `verification/test_dev3_reviewfix.py`: 25/25 PASS.
- `verification/test_peakhold_0120.py`: 17/17 PASS.
- `verification/test_backend_events_0120.py`: 4/4 PASS.
- `verification/test_ui_logic_0120.py`: PASS, including Live generator mains-collision banner.
- `verification/test_glitch_0120.py`: 17/17 PASS.
- `verification/test_analysis_worker_spawn.py`: PASS; process mode, 997-Hz tone detected.
- `python3 -m py_compile valvebench/*.py verification/test_gainmatch_0119.py verification/test_gainmatch_r5.py`: PASS.

## Limits

- Tests are synthetic/software tests. They do not prove analog accuracy of E-MU 0404 USB or
  ASUS Strix Raid DLX, and no hardware test was run.
- GTK tests use the repository's stub harness; native GTK4/libadwaita rendering and the
  user's 4K/Wayland desktop were not visually accepted in this run.
- Cache timings are measurements in this runtime, not guaranteed upper bounds on the user's PC.
