# Coordinator review — v2.18.2 diagnostic evidence

Date: 2026-11-15  
Reviewed commit: `895c6e0a2383898fd0675de9cd19f6c45c25fef1`  
Agent branch: `agent/v2182-clock-estimator-evidence-repair`  
Parent/base: `d312a23be367f22d8609d4f26a499e74f2ec4461`

## Executive decision

**v2.18.2 is a substantial improvement, but is not accepted as final evidence.** The two noise-model generators are now separated and the coloured generator's frequency grid and FFT scale are corrected. The timing schedule is much better anchored to production code. However, the LSQ implementation imported by v2.18.2 is the old v2.18 implementation, not the independent-start, single-decimation function from v2.18.1. Thus the claimed independent LSQ results, claimed 48 kHz fit rate, and pilot conclusions are not established. Several validation/reporting defects also remain.

No production changes, main changes, PR changes, pilot frequency changes or clock-gate changes are authorized.

## 1. Repository and scope

GitHub confirms:
- branch `agent/v2182-clock-estimator-evidence-repair` HEAD is `895c6e0a2383898fd0675de9cd19f6c45c25fef1`;
- parent is the requested coordinator base `d312a23be367f22d8609d4f26a499e74f2ec4461`;
- compare: one commit ahead, zero behind.

The commit adds diagnostic scripts, reports, JSON, CSV batches and generated Markdown tables, plus a .gitignore rule. No production module, main, or PR #4 was changed. The source `wav/` directory is explicitly excluded from Git and is not present in the pushed tree; therefore the coordinator/auditor cannot independently access those WAVs from the branch, despite the report saying they were copied into the agent's local project directory.

## 2. Blocking defect A — wrong LSQ implementation is still imported

The v2.18.2 Monte-Carlo and pilot scripts import:

`from v218_estimators_real import est_lsq`

But the `v218_estimators_real.py` committed on the v2.18.2 base still contains the old v2.18 estimator:

- it calls `dsp.analyze_tone(...)` and initializes `f0` from the production centroid;
- it searches only `f0 ± 0.05 Hz`, not an independent coarse frequency range;
- it unconditionally calls `resample_poly(seg, 1, 4)`.

The Monte-Carlo and pilot scripts pass `FS_M = 48000` / `FS = 48000` into this function. Therefore its internal fit rate is **12 kHz**, not the 48 kHz claimed in the report. This reintroduces both v2.18.1 defects: centroid-dependent initialization and double decimation.

The agent's v2.18.1 branch already contains a newer independent-start estimator with a periodogram coarse search and conditional decimation. That implementation was not carried into the v2.18.2 diagnostic branch. Port that code into the diagnostic estimator, then improve its coarse-bin parabolic interpolation: require a valid concave peak and constrain/refuse an extrapolation outside the declared acquisition interval. Preserve a trace of coarse peak bin, interpolation offset, final fit interval and actual fit sample rate.

Consequences:
- The v2.18.2 MC's LSQ-vs-centroid ratios may describe a local nonlinear refinement initialized by the centroid, but they do **not** demonstrate independent LSQ acquisition or ±100 ppm capture.
- The statement “fit at 48 kHz” is false for the current source.
- The v2.18.2 pilot results use the same centroid-seeded LSQ and do not test an independent LSQ candidate.

This is the principal blocker. Do not use the current LSQ tables to approve a production estimator.

## 3. Blocking defect B — the “868 Hz impossible / stale JSON” explanation is not proven

The v2.18.1 independent-start function performs a parabolic interpolation of the coarse FFT peak with only a `denom != 0` check. It does not require a concave local maximum and does not constrain the interpolated offset to the local half-bin interval. If the largest allowed coarse bin is at the search boundary and its neighbouring magnitudes are not a concave peak, the parabolic expression can extrapolate far outside the nominal ±2 Hz search band.

Therefore the argument “coarse search is ±2 Hz, hence 868 Hz is impossible” is invalid for that implementation. The saved v2.18.1 result may be a pathological parabola extrapolation, not necessarily stale output. The v2.18.2 rerun uses a different, centroid-seeded estimator, so its 999.87 Hz result does not settle what happened in v2.18.1.

Required: rerun the exact v2.18.1 seed/case against the exact v2.18.1 script, log the selected FFT bin and three magnitudes, denominator, interpolation offset, refined grid and final frequency. Then implement a bounded coarse estimator and test the same interference case. Label any output outside the coarse acquisition interval as acquisition failure, not as a valid frequency.

## 4. Noise generators — substantially improved; one verifier bug

### WHITE model

The new `noise_white_corr()` implementation is mathematically consistent with the stated construction: common component plus independent additions, target channel RMS floors −80/−96 dBFS, transfer −16 dB and target zero-lag Pearson correlation 0.97. `verify_white()` checks measured correlation but does not report measured per-channel RMS. Add explicit measured RMS/floor checks to the JSON so the level claim is independently auditable.

The white MC results are conditional on this idealized spectrally white, zero-delay correlation model. Their very favourable jGLS result is not evidence for colored real noise.

### COLORED model

The main MC now calls the requested model; it uses the 48 kHz grid `fs/n` and the correct one-sided PSD synthesis scale `S·fs·n/2`. The model is built from 129 quiet windows; the report correctly says this is an idle-D10/E-MU-floor model and does not include the full during-tone D10 noise/asymmetry.

However, `verify_colored()` accumulates 15 periodograms into `aLL/aRR/aLR` and then compares the sums against a single target PSD. It never divides those sums by `n_check`. This explains the almost constant +12 dB in the saved PSD deviations: `10·log10(15) = 11.761 dB`. Correcting the averaging gives approximate PSD deviations:

| Band (Hz) | L after /15 (dB) | R after /15 (dB) |
|---|---:|---:|
| 900–1100 | +0.09 | +0.61 |
| 100–500 | −0.13 | −0.13 |
| 500–2000 | −0.07 | 0.00 |
| 2000–8000 | −0.13 | −0.13 |
| 8000–24000 | −0.14 | −0.14 |

Thus the +12 dB is a verifier accumulation error, not evidence of a +12 dB model mismatch. Conversely, the report's “unit convention” explanation is wrong. Fix the verifier by dividing all accumulated spectra by `n_check`, save both target and generated PSD/coherence per band, and regenerate the JSON. The common factor cancels in the coherence ratio, so the saved synthesized gamma² values may remain close, but the verifier must show that explicitly.

The verifier currently saves generated gamma² only; the report compares it with measured gamma² values that are not written into the verifier JSON. Store target and synthesized gamma² side-by-side, including exact bands and averaging convention.

The MC itself uses direct PSD-based synthesis, not a separate post-hoc band normalization function. Document that accurately. The generated noise level still needs a reproducible same-units band-power check from the shared source WAVs.

## 5. Pilot test — corrected ppm formula, but not yet a realistic harmonic profile

The v2.18.2 pilot code correctly computes error relative to the tested pilot. That fixes the v2.18.1 nominal-frequency arithmetic error.

However, the script creates 40 mains harmonics (50 Hz through 2 kHz), setting **every line to the same level** for a given trial. Only the 1 kHz line is cited as measured at about −93 dBFS. Repeating that single measured line level across all 40 harmonics is a flat-comb stress model, not the measured mains spectrum of the device. Label the current test as a flat-harmonic-comb stress test; do not call −93 dBFS an actual measured harmonic profile.

For a realistic pilot comparison, estimate/integrate each mains-line power from the source captures individually, preserve the measured harmonic vector, and rerun 997/1000/1013 with the same measured profile. Keep a separate stress test with a specified single line near each pilot. Continue to save raw per-trial `f_est`, seeds and exact script hash.

Because the current estimator is centroid-seeded, no pilot conclusion about independent LSQ is accepted yet. 1013 Hz may remain a candidate for further offline tests, but do not change production's 1 kHz clock pilot on this evidence alone.

## 6. Monte-Carlo data counts and interpretation

There are 20 batch files per model, each containing 75 data rows (25 trials × 3 levels): **1500 rows per model, 3000 rows across WHITE and COLORED**. The report's “2000 rows total” statement is wrong; regenerate this count from the files. The summaries contain 500 trials per level for each model.

The saved summaries are arithmetically consistent with the batch data sampled/re-aggregated during review, but their LSQ fields inherit the wrong estimator described in §2. The colored model's jGLS `sigma_pred` remains severely under-dispersed (pred/actual about 0.045) and must not be called calibrated. The white model's per-trial coverage is conditional on the white model and the current whitening assumptions only.

The report is right to defer production sigma calibration for real hardware. The observed during-tone L/R asymmetry and the D10 noise not represented in the idle colored model remain open.

## 7. Schedule — arithmetic supported, comments need correction

Production source inspection confirms the 1.85 s schedule arithmetic, with two distinct 50 ms quantities that the diagnostic comments currently conflate:

- `Rig._seg()` includes a 50 ms **tail reserve** after the generated tone segment; `settle=0.4` is part of the tone segment before the measurement window, not a 0.4 s silent lead.
- `Rig.run()` with edge protection prepends the 1.5 s lead to the full programme.
- `backend.play_record()` separately prepends and appends a 50 ms zero pad to the playback stream.
- `play_record()` launches `arecord`, waits 0.30 s, and then launches `aplay`.

The expected tone onset relative to the nominal capture launch is therefore 0.30 + 0.05 + 1.50 = 1.85 s; the generated segment lasts 0.4 + 2.0 + 0.05 = 2.45 s. The program WAV showing tone onset at 1.50 s is consistent with the edge-protect lead. The script should name the 50 ms term the backend **leading pad**, not a measured D10 latency or the _seg tail reserve.

The observed capture #1 onset around 2.225 s / end around 4.300 s versus the code-derived nominal [1.850, 4.300] s supports an approximately 375 ms late start with the normal end. This is a strong timing anomaly, but it still does not prove the physical mechanism or literal count of source frames discarded: the 0.30 s is measured from process launch, not from an explicit arecord-first-sample timestamp. Keep the mechanism provisional.

The production window is still hard-coded from old per-capture onset values; retain the +350 ms overrun as a cross-check, not as an independently reconstructed production detector result.

## 8. Resampling — analytical result only

The `1e6/N_padded` quantization step remains arithmetically correct. The numeric residual table is not reliable evidence of per-tone resampling accuracy: the script creates simultaneous 1, 10 and 20 kHz tones, then uses a single-tone LSQ fit on the full multi-tone signal to estimate each component. The residual includes cross-tone fit bias, so it cannot isolate the resampler's timebase error. Replace the demo with one tone at a time or a properly isolated band-limited tone. Keep linear interpolation clearly marked as demonstration only; no production patch.

## 9. Source data availability

The WAV directory is absent from the GitHub tree because the new .gitignore rule excludes it. The only pushed source manifest is the earlier `v2181-wav-manifest.json`; no v2.18.2 WAV payload or accessible package link was committed. A local, ignored worktree directory is not a reproducible package for the coordinator/auditor. Provide the 7 capture WAVs and 7 corresponding programme WAVs through an accessible shared archive/location with hashes, sample format/dtype, sample rate, channel count and frame count.

## Required v2.18.3 — diagnostics only

1. Port the independent-start LSQ function from the v2.18.1 agent branch, but repair the unconstrained parabolic interpolation. Make the fit-rate logic conditional: 192 kHz input may decimate once to 48 kHz; 48 kHz input must remain at 48 kHz. Add deterministic tests for ±100 ppm, low SNR, 997/1000/1013 and a strong line just outside the coarse search interval. Log coarse peak and interpolation boundary outcomes.
2. Rerun the two 500×3 MC experiments with that exact estimator; regenerate per-trial CSV and summaries. Include actual fit rate in each output. Report LSQ as independent only after the implementation proves it.
3. Fix colored PSD verification by averaging over `n_check`; store target and synthesized PSD and gamma² per band. Add explicit white-model measured RMS checks. Correct the claimed total row count to 3000.
4. Rerun pilot tests with the actual measured harmonic-level vector from the source WAVs; retain the flat-comb case separately as a stress test. Revisit the 868 Hz result using the exact v2.18.1 code/seed and log the parabola values; do not label it stale until demonstrated.
5. Correct schedule comments to distinguish the backend leading pad, backend trailing pad, _seg tail reserve, settle/analysis offset and edge-protect lead. Preserve 1.85/4.30 timing result but state the process-launch/capture-start limitation.
6. Repair the resample numerical demonstration by testing each tone separately; preserve the analytical quantization result and do not implement production resampling.
7. Provide the WAV package through an accessible shared location, with complete hashes and format metadata. The package must not remain only in an ignored local directory.
8. Regenerate every report table mechanically from the corrected artifacts. Include exact commands, script hashes, file counts and established/provisional/unresolved statuses.

No production changes, main changes, PRs, playback, hardware or DUT measurements in v2.18.3.

## Current decision gate

v2.18.2 is **received and partially validated, but not accepted as final evidence**. The two noise generators are materially improved. The independent-LSQ claim, its 48 kHz fit-rate claim, pilot robustness, and 868 Hz explanation are not established because the branch imports the old centroid-started, 12 kHz-fit estimator. The colored PSD verification has a clear missing-average error. The schedule anomaly is now substantially better grounded in source code and remains a serious provisional finding.

The leading production direction remains **per-channel LSQ with explicit uncertainty, tone-presence and window-validity QC on both channels**. Do not hard-code R-only and do not introduce joint GLS until the full frequency-local covariance model is validated. No production implementation is authorized until v2.18.3 is independently reviewed.
