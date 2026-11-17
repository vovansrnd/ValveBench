# Coordinator review — v2.18.6 QC calibration and CSD convention

Review target: `agent/v2186-qc-calibration-csd-convention`, commit
`fb12df62321f453a73db8d13dc2faf5824afd2df`, based on
`7376cc18103a920e1cbbf32add31d7990d2e1dee`.

Branch comparison: one commit ahead / zero behind the agreed base. The
compare contains diagnostic artifacts only; no production source, main or
PR #4 changes are present. No playback, hardware or DUT testing is claimed.

## Decision

**Partially accepted as diagnostic progress; NOT accepted as a production-ready
clock estimator or as fully closed evidence.**

The CSD generator convention is corrected, the anchored single-channel fit
and the line-QC are significant improvements, the tested 997/1000 and ±3/±5 Hz
cases are detected, and the pilot line-power estimate is much better
calibrated than v2.18.5.

There are still blockers:
1. `ok_partial` does not actually remove the contaminated channel from joint
   GLS; the bad channel is still used in covariance estimation and fitting.
2. The estimator reports `snr_local_db` but has no minimum-SNR/tone-presence
   acceptance gate. The negative test set contains a tone in every case.
3. The Monte Carlo calculation applies an extra factor of sqrt(2) to
   `sigma_pred`; the published prediction/actual ratios are therefore
   mis-scaled.
4. The historical −127 ppm outcome is not reproduced by the exact v2.18.4
   estimator/input. The report identifies the arithmetic of a possible grid
   endpoint, but this is not an exact replay of the historical failure.
5. The CSD verifier still does not save target-vs-generated coherence and
   cross-spectrum magnitude errors; the unit known-phase test checks phase
   but has inconsistent amplitude normalization.
6. The zero-FP/zero-FN matrix is limited to its synthetic test population; it
   does not include tone-absent/noise-only negatives or a calibrated
   below-carrier interference sweep.

## 1. Accepted progress

### 1.1 CSD convention — generator direction corrected

In `v2186_montecarlo.py`, the generated relation now uses
`gR_c = S_LR_model / S_LL_s`; the verifier accumulates
`conj(FL) * FR`. These now use the same `csd(L,R)` convention.
The previous v2.18.5 conjugation mismatch is genuinely corrected in the
colored noise generator.

The full colored verification JSON reports the +0.37 rad known-phase
control as +0.3685 rad, with auto-PSD deviations within 0.15 dB and
band-averaged phase differences within 0.010 rad. I accept the direction/sign
fix and the marginal auto-PSD evidence.

The evidence is not yet a full cross-spectral calibration; see §6 below.

### 1.2 Anchored fit and line-QC — meaningful diagnostic improvement

`est_lsq` no longer uses the coarse peak as the center of its fine fit. It
fits the known nominal pilot in ±0.15 Hz, checks the RSS boundary, then
examines residual clusters outside ±1.5 Hz. The QC requires at least three
adjacent bins above a local median floor and a carrier-relative threshold.
This addresses the identified failure mode in which a strong nearby line
pulled the coarse/RSS search away from the pilot.

The committed 1700-case matrix reports:
- 1000/1000 positive synthetic interferer cases rejected and attributed;
- 700/700 negative tone-present cases accepted;
- no unnamed positives, false negatives or false positives in that sample.

I accept this as evidence that the specific detector construction works on
the stated synthetic cases. It is not a general 100% recall / 0% FPR guarantee;
the test population and missing negative classes are noted in §5.

The ±150 ppm fit window is a reasonable diagnostic margin relative to the
existing ±100 ppm clock gate, and the +100 ppm / +300 ppm tests make the
intended boundary explicit.

### 1.3 Pilot line-power extraction — substantial improvement

The pilot extraction now uses `nperseg=32768`, a calibrated recovery factor
for each 50 Hz line, side-band background subtraction and L/R CSD phase. The
reported recovery factor `rho=0.9991..1.0000` addresses the earlier 1.9–2.4 dB
main-lobe truncation error. The profile records SHA256 hashes for the source
capture WAVs and the pilot script.

The validation still reports mean absolute amplitude error 1.21 dB and
maximum 5.24 dB on weak lines near the noise floor. Treat the weak-line
amplitudes as uncertain, rather than fully calibrated to sub-dB accuracy.
The JSON also omits the `v2186_estimators.py` hash; add it for complete
method provenance.

### 1.4 New Monte Carlo populations

The branch contains fresh white and colored MC batches with separate accepted
and rejected statistics. The line-free colored model is now deliberately
separated from discrete mains interference, which is tested as an independent
signal class. This is a sensible modeling separation.

The white model reports 500/500 accepted per level. The colored line-free model
also reports 500/500 accepted per level. The colored `sigma_pred` remains
under-dispersed and must not be published as an uncertainty estimate.
Because the noise model changed (mains peaks are removed), v2.18.5 colored
MC results are not a direct apples-to-apples benchmark.

### 1.5 Scope

The commit contains only diagnostic files under
`measurements/diagnostics/v218-clock-estimator-independent-reproduction/`.
No production, main or PR changes are present. The archive itself remains
local and has not been published; no upload is requested by this review.

## 2. BLOCKER — `ok_partial` still includes the rejected channel in joint GLS

File: `v2186_estimators.py`, function `est_joint`.

The function correctly runs `est_lsq` on L and R, selects a valid channel
with the better SNR, and sets `partial=True` if only one passes QC.

But after selecting `q0`, it still unconditionally:
- computes `rL` and `rR` from both original input channels;
- estimates the full 2×2 covariance matrix from both residuals;
- whitens both channels;
- builds the joint design matrix using both L and R observations;
- returns `ok_partial`.

There is no mask or channel selection in the GLS objective. Therefore the
contaminated channel has not actually been excluded. `ok_partial` currently
means “the start frequency came from one clean channel”, not “the joint
estimate uses only the clean channel”.

The present test uses a 1050 Hz contaminant on a 1000 Hz signal (50 Hz away).
It checks only that the result is non-null and labelled `ok_partial`; it does
not show that the result is unaffected by the bad channel. It does not test
the relevant adjacent-line case at ±3 Hz.

Required:
- When exactly one channel passes QC, either return the accepted single-channel
  estimate directly, or run a genuinely single-channel objective. Do not put
  the rejected channel into covariance estimation or the fit.
- Add paired tests: clean L/R baseline; L contaminated at +3 Hz with clean R;
  R contaminated at −3 Hz with clean L; strong contamination on both.
- The one-good-channel result must agree with the corresponding good-channel
  single-channel estimate within a declared tolerance, independent of the
  contaminated channel's level/phase.
- Report which channel(s) contributed to the final estimate.

## 3. BLOCKER — no minimum SNR or tone-presence acceptance gate

File: `v2186_estimators.py`.

The function calculates `snr_local_db`, but does not compare it with any
minimum threshold. The status may remain `ok` if the anchored fit has an
interior RSS minimum and the residual detector finds no qualifying line.
The direct residual-skip branch also returns `ok` without a tone-presence
check.

The 700 negative confusion cases all contain the wanted pilot: 100 clean
sinusoids, 300 white-noise-plus-tone cases and 300 colored-noise-plus-tone
cases. There are no noise-only / missing-tone / dropout negatives. Thus the
matrix does not test whether the estimator can reject a capture window in
which the pilot is absent.

I independently replayed the committed LSQ/RSS/residual-QC logic on
200 two-second, 48 kHz Gaussian-noise-only records at −80 dBFS, with no
pilot. Results: 55 returned `ok`, 110 `grid_edge`, and 35
`line_contaminated`. This is a direct consequence of reporting SNR without
using it as a validity gate: a random projection at the nominal frequency can
look like a fitted carrier, and if no residual cluster passes QC the record
is accepted.

Required:
- Define a minimum carrier-local SNR / tone-presence threshold. Use the
  engineering limit already present in the clock-measurement path as the
  starting reference, but validate it against expected signal levels and
  actual capture noise rather than copying it blindly.
- Add no-pilot noise-only tests, tone dropout, short/partially missing tone,
  low-level pilot, clipping and near-threshold SNR sweeps.
- A no-pilot record must never receive `ok` or be used by joint GLS.
- Keep line contamination and missing carrier as different statuses
  (e.g. `line_contaminated` vs `tone_missing` / `snr_low`).

## 4. BLOCKER — sigma_pred is multiplied by sqrt(2) twice

Files: `v2186_estimators.py`, `v2186_montecarlo.py`.

In `est_joint`, the returned `var_f` is calculated as:

`var_f = 2.0 / (denom / step**2)`

The function/documentation describes this as the variance
`2 / RSS''` after whitening. In the MC runner, however, the reported
standard deviation is calculated as:

`sqrt(2.0 * jg[1])`

Since `jg[1]` is already `var_f`, this applies another factor of 2 to the
variance (and sqrt(2) to sigma). The current white `pred_over_actual=1.462`
is therefore not evidence that the estimator's native prediction is
conservative by 46%; after removing this extra factor it would be about
`1.462 / sqrt(2) = 1.034`, before recalculating per-trial coverage.
Similarly the colored ratio would be about `0.511 / sqrt(2) = 0.361`.

Re-derive the curvature/variance convention in one place, give the returned
field an unambiguous name (`variance_hz2` or `sigma_hz`), and apply the
conversion exactly once. Recalculate summaries and coverage from the stored
per-trial curvature/variance values. Do not use the current `sigma_pred`
tables for uncertainty claims.

## 5. Confusion matrix and historical −127 ppm — evidence scope is overstated

### 5.1 Confusion matrix

The reported 1000 TP / 700 TN is a useful deterministic synthetic matrix,
not an operational false-positive/false-negative rate. It tests the defined
line-free tone populations and the inserted high-level interferers. It does
not cover:
- records with no wanted pilot (see §3);
- a systematic line-level sweep below the carrier, especially around the
  `dbc_min=-35 dBc` boundary;
- real during-tone noise and measured L/R asymmetry with inserted lines of
  known level/phase;
- uncertainty intervals on the observed zero-error counts.

The positive matrix's weakest specified injected line is at −60 dBFS against
a −72 dBFS pilot (+12 dBc, i.e. 12 dB ABOVE the carrier), not a line 12 dB
below the carrier. The measured pilot-profile test does detect a real 1000 Hz
line around −16 dBc, which is useful, but those trials are not included as
labelled cases in the 1700-case confusion matrix.

Add a calibrated sweep of line level in dBc from clearly dominant through
below the selected threshold (e.g. +20, +10, 0, −10, −20, −30, −35, −40,
−45 dBc), at offsets ±1, ±1.5, ±2, ±3 and ±5 Hz, against white and colored
floors. Record attribution accuracy, frequency bias when accepted, and
TP/FP/TN/FN. Treat 0/700 FP as “none observed in this sample”, not a
population FPR of exactly zero.

### 5.2 Exact historical −127 ppm replay not yet completed

The current `v2186_legacy_counterexample.py` uses a two-second, 48 kHz
record (`N=96000`) and its first positive uses a line frequency constructed
as `f_pilot * (1000/997)`. It uses the L-side `noise_white_corr` floor
rather than the original −96 dBFS noise construction. The historical
v2.18.1 reproduction used a one-second 192 kHz input which was decimated
once to 48 kHz, with the original pilot/line construction and seed formula.

More importantly, the historical −127 ppm was reported from the v2.18.4
bounded-coarse / ±0.15 Hz RSS estimator. The new legacy script calls the
8d8b040 unbounded estimator, whose present two-second construction yields
about +1775.7 ppm. The report correctly shows that −127 ppm is the arithmetic
of the lower endpoint 996.85 Hz, but arithmetic alone is not reproduction.

Required:
- Import the exact v2.18.4 estimator revision that produced the −127 ppm
  result (do not substitute 8d8b040).
- Replay the original signal construction, Fs, duration, resampling, noise,
  phase and seed(s) that generated the saved −127 ppm JSON.
- Log coarse peak, RSS grid, argmin index, endpoint, final frequency and
  line attribution.
- Keep the current +1775.7 / ±145…+156 ppm tests as a separate family of
  related legacy failures. Do not label them as the exact −127 replay.

## 6. CSD verifier — phase convention fixed, full cross-spectral evidence incomplete

The generator now uses `S_LR/S_LL` and the verifier uses
`conj(FL)*FR`; the sign fix is accepted.

However, `v2186-verify-colored.json` stores synthesized gamma², auto-PSD
deviations and band-averaged model/synthesized phases. It does not store the
target gamma² or the cross-spectrum magnitude error on the same bins. The
known-phase unit test computes `g` by dividing SciPy CSD density by
`abs(rfft(L)[k])**2 / n`, which has different normalization units; the test
checks only phase, so the generated R amplitude is not validated by that
assertion.

Required:
- In the known-phase test, estimate `S_LL` and `S_LR` with the same PSD/CSD
  convention and normalization, then use `g=S_LR/S_LL`; assert both
  recovered phase and amplitude/PSD.
- Save target and generated `S_LL`, `S_RR`, `S_LR`, gamma², CSD magnitude
  and phase on the same frequency grid, plus errors and per-band summaries.
- Keep the colored MC model explicitly labelled “line-free smoothed noise
  floor”; it is not the full during-tone interference model.

## 7. Pilot provenance and uncertainty

The line-power calibration is a substantial improvement. The saved pilot JSON
contains source capture hashes and the pilot script hash, but not the
estimator-module hash. Add `v2186_estimators.py` SHA256 and the exact source
manifest identifier. Preserve the reported 1.21 dB mean / 5.24 dB maximum
validation error for weak lines as an uncertainty/limitation; do not present
`rho` calibration alone as proof of sub-dB accuracy for every measured line.

## 8. Current status and next diagnostic iteration

Accepted: CSD synthesis direction, coarse/fine fit behavior, anchored nominal
fit, tested line-QC detections, improved line-power extraction, and the
explicit separation of line-free noise from discrete interference.

Not accepted: `ok_partial` as truly channel-masked GLS, tone-presence/SNR
validity, current `sigma_pred` values, exact historical −127 replay, and
complete cross-spectrum magnitude/coherence verification.

Next iteration should be diagnostic-only and focus on:
1. true channel masking/fallback for `ok_partial`;
2. explicit tone-presence and SNR gate with no-pilot/dropout negatives;
3. correct variance-to-sigma conversion and fresh coverage summaries;
4. exact replay of the v2.18.4 −127 ppm estimator/input;
5. dBc/offset sensitivity sweep including below-carrier interferers;
6. amplitude-normalized known-phase test and target/generated full CSD check;
7. estimator hash provenance for the pilot JSON.

No production estimator, clock gate, pilot frequency, main, PR, playback,
hardware or DUT changes are authorized by this review.
