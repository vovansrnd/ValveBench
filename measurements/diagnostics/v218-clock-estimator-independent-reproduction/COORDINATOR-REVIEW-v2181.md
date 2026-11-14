# Coordinator review — v2.18.1 evidence repair

Date: 2026-11-14  
Reviewed agent commit: `8d8b040969a9a872941ecfe502975f45419d4186`  
Agent branch: `agent/v2181-clock-estimator-evidence-repair`  
Parent/base: `753dfe1ef96729bbe8808e85314e4d37372b406c`

## Executive decision

**Not accepted as final evidence.** The agent completed a substantial diagnostic pass and did not modify production code, but the committed Monte-Carlo implementation does not match its report, the pilot LSQ metrics use the wrong reference frequency, and at least one stored pilot result cannot be reproduced from the committed estimator. No production patch, merge, pilot-frequency change, or clock-gate change is authorized.

## Repository and scope check

GitHub compare reports one commit ahead / zero behind and 32 changed files. The changed paths are confined to `measurements/diagnostics/v218-clock-estimator-independent-reproduction/`; the single modified Python file is the diagnostic estimator script `v218_estimators_real.py`. No production module, main, or PR #4 was touched.

## 1. Monte-Carlo implementation does not match the claimed model

The report says the 500×3 primary MC is a white correlated equivalent with floors L=−80 dBFS, R=−96 dBFS and rho≈0.97. The committed implementation does not do that:

- `run_batch()` calls `synth_pair_from_spectra()`. The available `white_corr_pair()` function is not called. The data are therefore generated from the quiet-window PSD/cross-PSD path, not from the stated white-correlated proxy.
- In `synth_pair_from_spectra()`, `df_t = FS / n` uses global FS=192000, while the actual synthesis call uses fs=48000 and n=96000. The interpolation grid is 2 Hz/bin instead of the actual 0.5 Hz/bin. The source spectrum is sampled at frequencies four times the actual output frequencies.
- The Fourier coefficient scale uses `FS * n` instead of `fs * n`; the actual sampling rate must be used consistently in spectral synthesis.
- `normalize_pair_to_band()` targets the integral of the measured PSD in 900–1100 Hz but divides by the full-band time-domain variance `np.var(nL)` / `np.var(nR)`. This sets total signal variance to a narrow-band target; it does not normalize the power in the target band.
- `verify_synthesis()` treats FFT-bin indices as Hz (the calculated `df_bins` is unused) and estimates PSD with `2/n²·|FFT|²` without the required sampling-rate normalization. Its frequency-band comparisons are therefore invalid; in particular, the 48 kHz synthesizer cannot validate the claimed 30–80 kHz band.
- The saved JSON reports 900–1100 Hz measured gamma²=0.784 versus synthesized gamma²=0.971. This is not a successful match in the principal pilot band. Other bands also show material mismatch.

A second inconsistency affects the claimed “D10 noise explains the 12× sigma gap” conclusion. The report compares a production aggregate in dBFS with a quiet-window spectral-density value in dBFS/Hz-equivalent without first integrating both over the same bandwidth and bins. The current MC also has the normalization and frequency-axis defects above. Therefore the +13.6 dB / 12× causal explanation is unproven.

## 2. Pilot LSQ metrics are calculated against 1000 Hz for all pilots

`v218_estimators_real.py:ppm_of()` is hard-coded to `(f / 1000 - 1) * 1e6`. The pilot experiment calls it for 997, 1000 and 1013 Hz. Correct error is:

`((f_est / pilot) - 1) * 1e6 - PPM_TRUE`

Consequently the reported LSQ results near −3000 ppm at 997 Hz and +13000 ppm at 1013 Hz are nominal-frequency offsets, not estimator bias. The report's claim that the 1013 Hz LSQ minimum was shifted by Dirichlet side lobes is unsupported.

There is also a reproducibility mismatch in the saved JSON: the 997 Hz / −30 dBFS LSQ summary corresponds to an estimated frequency around 868 Hz. The committed `est_lsq()` coarse-searches only within ±2 Hz of 997 Hz and refines within ±0.15 Hz, so that result cannot be produced by the committed function. The JSON is stale or belongs to another code revision unless a rerun demonstrates otherwise.

Rerun all pilot cells with pilot-relative ppm, per-trial raw frequency estimates, and an exact script hash. Integrate measured mains-line power across its spectral lobe; do not compare a dBFS/Hz bin value directly with pilot dBFS.

## 3. Sigma derivation: factor two is correct conditionally; calibration is not accepted

For Gaussian residuals with known unit variance after correct whitening, `var(f̂)=2/RSS''` is the correct local profile-likelihood result. The implementation retains `var_f=step²/denom` and the MC caller applies `sqrt(2·var_f)`, so the factor of two is effectively applied.

However, the GLS implementation whitens only an instantaneous 2×2 L/R residual covariance; it does not whiten temporal/frequency colour. In addition, the aggregate coverage compares every error with the mean predicted sigma rather than the per-trial sigma saved in CSV. Recompute coverage as `abs(error_i) <= sigma_pred_i` and `<= 2*sigma_pred_i`. Until the MC model and whitening assumptions are corrected, the current 1σ/2σ figures are conditional simulation results, not real-noise or production calibration.

## 4. Tone bounds: useful observation, expected schedule remains provisional

The new analysis finds capture #1 crossing the chosen threshold around 2.225 s and ending around 4.300 s, while six comparison captures begin around 1.85–1.86 s and end around 4.30 s. That supports an early-capture anomaly and no comparable tail loss.

The expected start is nevertheless the median of those same six normal received onsets; the script fixes the duration at 2.45 s and hard-codes per-capture production onsets. Assertions test those chosen bounds, not an independently reconstructed source-frame schedule. Keep “about 370 ms later than the normal captures” as a provisional observation, not a proven count of discarded source frames or a causal hardware diagnosis.

## 5. Resampling result

The analytical step `1e6/N_padded` and values 0.947/0.801/0.694 ppm for the stated padded lengths are arithmetically correct. The numerical fractional comparison uses `np.interp` (linear interpolation) on synthetic tones, not a production band-limited fractional resampler. It demonstrates the possibility of removing integer-length quantization; it is not a production implementation or approval to patch `resample_capture_timebase()`.

## 6. Data availability

The manifest contains seven WAV hashes and metadata, but the WAV package is not included in the pushed branch. The report points to an absolute local `/opt/MyApps/Adudio/` path, which is not accessible to an external reviewer through this branch. Provide the package to the coordinator/auditor through a shared artifact location and keep source hashes tied to the report.

## Required v2.18.2 (diagnostics only)

1. Split the tests clearly: (a) white correlated proxy with explicitly verified per-channel RMS and clearly defined Pearson correlation vs magnitude-squared coherence; (b) coloured PSD/cross-PSD synthesis only after fixing target frequency axis, sample-rate scaling, PSD normalization, band-power normalization and verification. Ensure the function called by `run_batch()` is exactly the model named in the report.
2. Validate every generated model using per-channel RMS, Welch PSD, cross-PSD/coherence and an explicit 900–1100 Hz band-power check. Save generated noise levels and model parameters in JSON. Make no D10 causal claim without identical bandwidth and units.
3. Re-run MC, preserve seeds/per-trial values, and evaluate uncertainty coverage per trial. State that the result is conditional on the simulated model.
4. Fix pilot-relative ppm; rerun 997/1000/1013; save raw per-trial frequencies, exact script hash, and correct line-power integration. Resolve the impossible 997/−30 artifact.
5. Keep the factor-two derivation, but label sigma calibration as model-conditional. Do not call zero-lag covariance full coloured-noise GLS.
6. Derive tone schedule from exact source programme frames and backend timeline; use normal captures only as a cross-check.
7. Leave production/resampling code unchanged.
8. Make the source WAV package accessible and document exact hashes, commands, scripts and outputs.

No playback or hardware tests. No production changes, PRs, pilot changes or gate-policy changes. After v2.18.2 is independently reviewed, the coordinator may design the production estimator and tests on a separate coordinator branch.

## Current architecture direction

The leading production candidate remains per-channel LSQ returning frequency/ppm, uncertainty, tone presence, SNR and window-validity/QC metadata. Preserve quality checks for both channels; choose the most informative valid channel rather than hard-coding L or R. Defer joint GLS until the frequency-local cross-spectral covariance model is validated. This is a design direction only, not production approval.
