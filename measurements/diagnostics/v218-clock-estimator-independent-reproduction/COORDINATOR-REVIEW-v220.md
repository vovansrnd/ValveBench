# Coordinator review — v2.20.0 (18 Nov 2026)

## Execution acceptance

Agent result: `43f1cb6cbbb973e5ba05260fe2dc832e2f38f896` on
`agent/v2200-coordinator-authored-validation`.

GitHub verification:
- The result commit is a direct child of `d5e29814dc6746a7fc196492539b4cb7959ff7ab`.
- The agent branch was one commit ahead of that exact execution-contract head.
- Exactly the seven contracted result files were added: three complete JSON
  files, three complete `/usr/bin/time -v` logs, and the execution manifest.
- Manifest checkout: frozen source HEAD
  `8fcbe642132d957edc239f52e85edbe8e854b698`.
- `py_compile` ×4 and all three scripts exited 0; `git diff --check` clean.
- Manifest runtimes / maximum RSS: full clock path 1.08 s / 167,840 KB;
  line-QC Monte Carlo 6:58.17 / 124,956 KB; nearby-fit bias 8:03.01 /
  131,988 KB.
- No harness, frozen estimator, or production source was modified.

Execution is accepted. This does not mean the tested algorithms are accepted.

## 1. Full production `Rig.run()` → `clock_check()` result

The 40%-occupancy front-loaded case is an end-to-end production-path false
accept:

- Pilot present for the first 0.8 s of the 2.0 s analysis window, absent for
  the remaining 1.2 s.
- `Rig.run()`: pass; `clock_check()`: returned; overall `ok=True`.
- `signal_ok=True`; local SNR was 46.90 dB (L) / 46.81 dB (R).
- Frequency estimates were +0.087 ppm (L) / −0.040 ppm (R).
- The analyzer emitted a warning about unstable carrier phase between blocks
  (coherence 0.585 / 0.767), but that warning does not affect
  `clock_check.signal_ok` or its overall `ok` result.

This confirms that the production preflight can declare a partially silent
capture valid. It is no longer just a direct private-guard observation. The
median-referenced stability check is insufficient, and the current signal
validity test does not promote inter-block phase instability to a refusal.

Other observed outcomes:
- Clean: pass / `ok=True`.
- 50% front-loaded occupancy: rejected by `Rig.run()`.
- 25% tail dropout: rejected.
- 40 ms gap: pass / `ok=True`.
- 60 ms gap: rejected.
- 200 ms, −6 dB sag: pass / `ok=True`.
- 500 ms head dropout: rejected, but onset moved from the DAC delay
  (0.137 s) to 1.030 s. The resulting 2 s window extended past the end of
  the generated tone and included about 0.85 s of trailing quiet. Therefore
  this is not a clean isolated measurement of a head dropout; it also tests
  onset re-anchoring plus insufficient post-program tail.

**Decision:** do not alter production thresholds or code yet. The next test
must map the full-path acceptance boundary over duty fraction, location,
split/burst patterns, onset displacement and inter-block phase metrics.

## 2. Frozen v2.18.8 LSQ line-QC: white-noise false alarms

This is the independent diagnostic LSQ estimator, not production
`clock_check()`.

- Focused white-noise, no-injected-line population:
  86/900 `line_contaminated` = 9.56%
  (Wilson 95% interval approximately 7.8–11.7%).
- Seeded replay population: 13/252 = 5.16%
  (Wilson 95% approximately 3.0–8.6%).
- The populations use different pilot/noise level mixes and are not one
  homogeneous false-positive estimate.
- False line peaks span 658.5–47,843.5 Hz; median frequency 20,701.5 Hz.
  Their `line_dbc` ranges from −34.9 to −27.6 dBc (median −33.1 dBc).
- 95/99 white-noise false flags formed exactly three bins. However, all
  42/48 detected injected-spur controls also formed three-bin clusters.
  Raising `min_bins` from 3 to 4 would therefore discard the measured
  injected-spur sensitivity as well as most false alarms; it is not an
  acceptable untested fix.
- Injected stable spurs were flagged in 42/48 cases (87.5%).

**Decision:** keep `QC_CFG` unchanged. Test whether a residual line persists
at the same frequency in both one-second blocks before considering a new
diagnostic policy. The v2.21.0 test evaluates 0.5 / 1 / 2 Hz cross-block
frequency tolerances against white noise, colored noise and stable injected
spurs.

## 3. Frozen v2.18.8 LSQ nearby-line primary-frequency bias

Again, this concerns the independent diagnostic LSQ estimator, not the
production `clock_check()` path.

- 1,512 records: 360 pilot-only baselines, 1,080 nearby-line records.
- Nearby probe hits: 995/1,080 (92.1%).
- Worst observed absolute induced shift:
  **67.03 ppm**, independent phase, +0.40 Hz, −10 dBc;
  20 trials in the cell (10 positive and 10 negative offsets).
- All 20 records in that worst cell had estimator status `ok`, and all 20
  had a nearby-probe hit.
- Other high-bias independent-phase cells at −10 dBc:
  +0.30 Hz: 65.70 ppm; +0.35 Hz: 65.42 ppm;
  +0.25 Hz: 61.07 ppm; +0.50 Hz: 58.06 ppm.
- The agent's quoted 61 ppm at +0.25 Hz is correct for that cell but is not
  the maximum in the JSON.
- AM controls triggered the nearby probe 36/36 times; FM controls 34/36.
  Thus the probe does not distinguish independent oscillators from legitimate
  modulation sidebands.

**Decision:** retain diagnostic-only behaviour. Do not blindly turn every
nearby-probe hit into a refusal state: AM/FM controls also trigger it. The
next test compares 2 s and 4 s LSQ windows on paired seeds to see how much
longer observation reduces the bias.

## 4. v2.21.0 prepared

Execution-only branch:
`agent/v2210-coordinator-authored-validation` (created after the final
execution contract and review are committed).

Three coordinator-authored tests:
1. `v2210_clock_quality_boundary.py`: production full-path occupancy and
   placement matrix.
2. `v2210_lineqc_two_block.py`: candidate two-block persistence test for
   independent-estimator line-QC.
3. `v2210_nearby_duration_comparison.py`: paired 2 s / 4 s nearby-line
   bias comparison for the frozen independent LSQ estimator.

No code or gate change is authorized. No hardware playback/capture, DUT,
WAV or production/main/PR #4 changes are authorized.
