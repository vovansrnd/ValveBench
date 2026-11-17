# Coordinator review — v2.18.7 partial-channel, tone gate and uncertainty repair

Review target: `agent/v2187-partial-channel-snr-uncertainty-repair`, commit
`12d64d914c336d6db3aba865bcaf89fb70d4aa7b`, based on
`47657b350b2cd58aaf2c1209ea6248a34aa846d0`.

Branch comparison: 5 commits ahead / 0 behind the agreed coordinator base.
All files introduced by this work are diagnostic material under
`measurements/diagnostics/v218-clock-estimator-independent-reproduction/`
(reports, scripts, JSON/CSV/NPZ, tables and WAV manifest). No production
source, main or PR #4 changes are present. No playback, hardware or DUT
testing is claimed. The WAV files were transferred to the remote compute
host for the declared jobs; the archive was not published to GitHub.

## Decision

**Partially accepted as diagnostic progress. Not accepted as a production-ready
clock estimator and not closed as a complete historical reproduction. No
production approval.**

The v2.18.7 branch fixes three important defects from v2.18.6: partial-channel
selection is now genuinely isolated, a tone-presence/SNR gate is active, and
variance-to-sigma conversion is no longer double-counted. The expanded sweep
also exposes the detector's blind zone rather than hiding it. These are
substantial improvements.

Two points remain unresolved before this can be considered closed evidence:

1. The new estimator's carrier-local SNR is not the same calculation as the
   production `dsp.analyze_tone.local_snr_db`. Reusing the numeric value
   `CLOCK_MIN_SNR_DB = 20 dB` does not establish that the thresholds are
   equivalent. A paired calibration on identical signals is required.
2. The −127 ppm failure mechanism is reproduced with the exact v2.18.4
   estimator code, but the original historical input construction was not
   recovered. The new waveform is a parameter-search reconstruction, not an
   exact replay of the historical capture/script.

The residual line detector also has a measured architectural blind zone around
±1/±1.5 Hz, where strong interferers can bias accepted estimates substantially.
This is clearly documented and must remain an explicit limitation.

## 1. Accepted — real partial-channel isolation

File: `v2187_estimators.py`, `est_joint()`.

The v2.18.7 control flow now does what v2.18.6 did not:

- both channels passing QC enter the two-channel GLS;
- if exactly one channel passes, the function immediately returns that
  channel's `est_lsq` result, with `channels_used` containing only that
  channel; the rejected channel is not used in covariance estimation,
  whitening or the frequency objective;
- if both fail, no channel is used and the joint estimate is refused.

This is consistent with the paired tests in `v2187_unit_tests.py`: clean L /
contaminated R, the mirrored case, several interference levels/phases, and
both channels contaminated. The accepted single-channel result is compared
directly with the corresponding channel-only LSQ result.

I accept the code-side fix and its diagnostic tests.

## 2. Partially accepted — tone-presence gate works, but its 20 dB calibration is not established

File: `v2187_estimators.py`, `est_lsq()`; production references:
`valvebench/measure.py` and `valvebench/dsp.py`.

The gate is active before line-QC:
- below 0 dB local SNR: `tone_missing`;
- 0 to below 20 dB: `snr_low`;
- at or above 20 dB: proceed to line-QC.

The committed tests include 200 noise-only records at −80 dBFS (0/200 now
return `ok`, versus 55/200 in the v2.18.6 independent replay), additional
noise-only records, low-frequency-heavy noise, dropout, short/partial tone
windows, low-level pilot and clipping. This is valid evidence that the new
gate prevents the previously demonstrated noise-only false acceptance for
those tested populations.

However, the statement that the new metric is “aligned with production
`CLOCK_MIN_SNR_DB = 20`” is not yet supported by the source:

- production `clock_check()` obtains `local_snr_db` from
  `dsp.analyze_tone()`;
- production forms its local noise estimate from a median of the local
  spectrum and multiplies it by the fundamental-lobe bin count
  (`2*HALF+1`, normally 11 bins), using the production Block/BH spectrum;
- v2.18.7 estimates noise from a Hann-windowed residual over approximately
  ±32 Hz with a ±2 Hz exclusion, integrates the local median over all
  selected bins, and compares that integrated noise with the fitted peak
  amplitude.

Those are different spectral windows, effective noise bandwidths and
normalizations. The same numeric threshold therefore cannot be assumed to
represent the same SNR. In particular, the 20 dB value in production is
commented as an engineering floor, not an uncertainty guarantee.

**Required before accepting the threshold as calibrated:** run both SNR
calculations on the same deterministic signal records, with matched effective
capture segments and known signal/noise levels. Include white and colored
noise, line/hum interference, clean tones, pilot-level sweep, near-threshold
SNR, dropout and partial windows. Publish paired per-record values and
classification differences. Then either harmonize the implementation or
justify a separately calibrated v2.18.7 threshold. Do not change the
production clock gate or the selected pilot frequency in this work.

Until then, I accept the gate's presence and the no-tone rejection evidence,
but not the claim that 20 dB is numerically equivalent to production.

## 3. Accepted for the tested white model — variance conversion corrected

File: `v2187_estimators.py`; MC summaries and per-trial CSV batches.

The implementation now uses the explicit `variance_hz2` field and derives
`sigma_hz = sqrt(variance_hz2)` once. The MC runner uses the resulting sigma
rather than applying another `sqrt(2)`.

The new white-noise results are internally consistent:
- −72 dBFS: predicted/actual sigma 1.03; 1σ/2σ coverage 0.708/0.948;
- −66 dBFS: ratio 0.987; coverage 0.672/0.948;
- −60 dBFS: ratio 1.003; coverage 0.698/0.952.

I accept the correction and the white-model result as synthetic white-noise
evidence only.

The colored, line-free model is explicitly **not calibrated for uncertainty**:
predicted/actual sigma is 0.361, 0.371 and 0.363 across the three levels;
1σ coverage is only 0.27–0.33 and 2σ coverage 0.526–0.554. The estimator
underestimates uncertainty by roughly 2.7–2.8×. Do not publish or consume
`sigma_pred` as a valid uncertainty interval for colored noise. The report
correctly recognizes that instantaneous 2×2 channel whitening does not
whiten temporal correlation.

## 4. Partially accepted — v2.18.4 failure family reproduced, not the exact historical input

Files: `v2187_legacy_replay_127.py`,
`v2187-legacy-replay-127.json`.

The replay script copies the `est_lsq` algorithm from the identified v2.18.4
file at ref `4fc938e`; it is not the unrelated 8d8b040 estimator. The
coarse-peak curvature sign, skip of refinement for a concave maximum,
bounded RSS grid and status handling are consistent with that source.

The reconstructed two-second 192 kHz construction is processed through the
same 1/4 resampling path. In 100 trials it gives:
- coarse peak at 997.0 Hz;
- RSS minimum at one of the ±0.15 Hz grid endpoints;
- 48/100 at 996.85 Hz (−126.95 ppm relative to the true pilot);
- 52/100 at 997.15 Hz (+173.95 ppm);
- legacy status `ok` in 100/100;
- v2.18.7 refuses 100/100 with `line_contaminated` and attributes the
  1000 Hz interferer.

This convincingly demonstrates a **v2.18.4 failure family**: a nearby
interferer can dominate the RSS landscape and drive the bounded fit to an
endpoint while the old estimator still reports `ok`.

It does not prove the historical capture was generated with these exact
parameters. The original inline test was not committed; the −60 dBFS pilot
level and two-second duration were selected by a systematic search for the
reported −127 ppm fingerprint. The report itself acknowledges this. The
right label is therefore “reconstructed synthetic replay of the v2.18.4
failure mechanism”, not “exact historical replay”.

Keep the separate 8d8b040 / +1775.7 ppm family distinct. If the original
historical script or input is later recovered from a local history/archive,
replay it separately and record its actual provenance.

## 5. Partially accepted — broader line-QC sweep, with a significant blind zone

Files: `v2187_qc_sweep.py`, `v2187-qc-sweep.json`,
`tables/confusion.md`.

The defined sweep covers 9 relative levels (+20 through −45 dBc), 10
signed offsets (±1, ±1.5, ±2, ±3, ±5 Hz), 30 trials per cell, measured
profiles and no-pilot negatives.

The reported totals are:
- 3090 trials, 2730 positive and 360 negative;
- line-QC TP: 1303, with 1303/1303 correct line attribution;
- additional TP_unnamed / other refusals: 48;
- positive cases accepted: 1379 (FN for the any-refusal gate);
- TN: 180; FP_line: 0; FP_other: 0;
- any-refusal recall: 1351/2730 = 49.49%; line-specific recall:
  1303/2730 = 47.73%.

These definitions matter: the 48 unnamed refusals increase “any-refusal”
recall, but they are not line-QC detections. Report both rates and do not
describe the 1303 line detections alone as 49.49% line-QC recall.

The sweep confirms a true blind zone: interference inside the ±1.5 Hz
exclusion is not independently detected. Strong lines there can bias an
accepted result by tens to >100 ppm (the report records up to 118.5 ppm).
This is not a small detail that can be dismissed as a false negative. The
guard is protecting the fit from counting its own carrier residual, but also
hides genuinely separate nearby tones.

At −40/−45 dBc the observed frequency bias is ≤0.5 ppm, so acceptance of
those weak lines is reasonable within this synthetic construction. Around
−35 dBc, measured amplitude uncertainty straddles the configured threshold
and yields partial detection. These results are useful calibration evidence,
not a general operating guarantee: zero observed false positives is limited
to the 360 defined negatives.

I accept the expanded diagnostic sweep and the transparent accounting.
The ±1.5 Hz blind zone remains an architectural limitation; do not widen or
alter the exclusion band without a separate design/test task.

## 6. Accepted — CSD convention and verification improved

Files: `v2187_montecarlo.py`,
`v2187-verify-colored.json`,
`v2187-verify-colored-spectra.npz`.

The generator and verifier consistently use the SciPy convention
`csd(L,R) = conj(FFT(L))*FFT(R)`. The known-phase test reports +0.3685 rad
for a +0.37 rad model and amplitude ratio 1.025 using a common PSD/CSD
normalization. The target/generated NPZ stores S_LL, S_RR, S_LR and gamma²
on the common frequency grid; the JSON reports auto-PSD differences within
0.15 dB and band-averaged CSD phase differences within 0.010 rad.

This closes the v2.18.6 phase-sign and amplitude-normalization objections
for the tested synthetic model. One presentational defect: the JSON rounds
absolute known-phase cross-PSD magnitudes to six decimal places, displaying
both as zero even though the ratio is valid. Prefer scientific notation or
more significant digits in the next regenerated JSON; it does not invalidate
the underlying ratio check.

## 7. Accepted — pilot provenance; weak-line uncertainty remains

File: `v2187-lsq-pilots.json`.

The JSON records script, estimator and WAV-manifest SHA256 values, plus the
individual source-WAV hashes. This closes the missing estimator-provenance
item from v2.18.6. The 22-line extraction validation still reports mean
absolute error 1.21 dB and maximum 5.24 dB; weak line amplitudes remain
uncertain and should not be treated as sub-dB accurate.

The R-channel pilot-1000 result (88 accepted / 12 refused) is consistent
with a line measured close to the −35 dBc gate. It demonstrates threshold
sensitivity at the calibration boundary, not a reason to silently relax
the threshold. The WAV package remains unpublished.

## 8. Required next iteration — diagnostics only

1. **Paired SNR calibration (blocking):** create one common corpus and compute
   both v2.18.7 `snr_carrier_db` and production
   `dsp.analyze_tone.local_snr_db` for the same underlying records and
   matched analysis segments. Sweep known pilot/noise levels and include
   white, colored, hum/line, dropout, partial-window and noise-only cases.
   Save paired values, deltas and accept/reject matrices. Determine whether
   20 dB can legitimately be carried across or whether the v2.18.7 threshold
   needs a separate calibrated value. No production gate changes.
2. **Historical input provenance:** search only available local/branch/history
   artifacts for the original v2.18.4 inline −127 test. If unavailable, retain
   the honest reconstructed-replay label and do not claim exact historical
   input reproduction. Do not synthesize provenance.
3. **Blind-zone design note:** preserve the measured ±1/±1.5 Hz limits and
   propose a separate discriminator (for example, a joint nearby-tone model
   or explicit carrier-residual amplitude comparison) with tests against
   clean pilot, noise, coherent side-tone and real line profiles. Do not
   change the current ±1.5 Hz exclusion in this iteration.
4. Retain the corrected channel isolation, variance semantics, CSD evidence
   and provenance hashes. Colored-noise sigma remains non-publishable unless
   a temporal-noise model is implemented and independently validated.

No production/main/PR changes, pilot-frequency or clock-gate changes,
playback, hardware or DUT work.
