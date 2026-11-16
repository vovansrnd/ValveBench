# Coordinator review — v2.18.5 line-aware estimator repair

Review target: `agent/v2185-line-aware-estimator-repair`, commit
`31c1f001e133b3464f4739fff9fc32da5e8d44fa`, based on
`65e44ea360744bbd7a01b41b6785f251f7ea7b44`.

Branch comparison: one commit ahead / zero behind the agreed base. The commit
contains diagnostic artifacts only; no production source, main branch or PR #4
changes are present in the compare. The agent reports no playback, hardware or
DUT testing.

## Decision

**Partially accepted as diagnostic progress; NOT accepted as a working
line-aware estimator or as closed evidence. No production approval.**

The coarse-peak interpolation sign/limits and RSS-grid boundary handling are
improved. The old 868 Hz failure is correctly rejected by the new edge guard.
Fail-closed behavior is explicit. The schedule labels and manifest are better.

However, the line-QC currently rejects essentially every valid signal, the
specific in-band −127 ppm failure from v2.18.4 is not tested and is excluded
by the QC mask, the pilot line powers remain under-integrated, and the colored
CSD “phase fix” is mathematically inconsistent with SciPy's CSD convention.
The MC summaries and pilot accuracy numbers therefore cannot establish a
working operational QC path.

## 1. Accepted diagnostic progress

### 1.1 Coarse and fine interpolation

`v2185_estimators.py` corrects the v2.18.4 coarse curvature sign: a spectral
maximum is refined only for negative second difference, with the offset
limited to ±0.5 bin. The RSS minimum is refined only for positive curvature;
a minimum at the local grid edge is exposed as `grid_edge`.

The 19 deterministic helper/signal assertions cover concave, flat and convex
shapes, off-bin interpolation, clipping, RSS boundary behavior and ±100 ppm
acquisition. The ±100 ppm point tests are useful but are not a substitute for
a positive/negative QC acceptance matrix.

### 1.2 Historical 868 Hz failure and fail-closed status

The v2.18.5 reproducer uses the correct v2.18.1 historical estimator for the
legacy side and the same signal construction/seeds for the repaired side.
The corrected estimator rejects all 100 reproduced edge-acquisition trials;
`est_joint` also rejects all 100 rather than silently consuming an invalid
start. This is an accepted diagnostic closure of the specific edge-peak
failure.

It does **not** close the different v2.18.4 case where a strong line at
1000 Hz biased a 997 Hz estimate by −127 ppm while the coarse maximum was
inside the search interval.

### 1.3 Other scope improvements

- The pilot runner now records both channels, deterministic seeds, and an L/R
  phase field for injected mains lines.
- The schedule script now distinguishes backend pre-pad, backend post-pad and
  `Rig._seg` reserve. It no longer calls a zero pad a measured D10 latency.
- `wav/MANIFEST.json` contains 32 unique entries. Its entry count and summed
  byte sizes agree with `count=32` and `total_bytes=283854656`; the declared
  uniform sample format is float32, stereo, 192 kHz.
- The numerical resampling test remains isolated to a single 1 kHz tone.
- The worktree compare contains no production file changes.

The manifest is internally consistent, but this review cannot verify the
individual file hashes because the actual archive remains on the agent's local
filesystem and is not attached to the branch. The manifest is not the WAV
payload.

## 2. Blocking defect: residual line-QC is not calibrated and is fail-all

File: `v2185_estimators.py`.

The current detector:
1. fits one sine/cosine/DC model;
2. computes an FFT of the residual;
3. excludes every bin within ±5 Hz of the fitted frequency;
4. takes the single largest remaining bin;
5. flags it when it is more than 10 dB above the median of all remaining
   residual bins.

This is an extreme-value test against the median of a very large set of
periodogram bins, not a line detector. The observed natural maximum of a
white-noise periodogram can exceed its median by more than this threshold.
Near-zero numerical residuals from a noise-free fit also have a nearly-zero
median, making the ratio unstable.

The committed summaries confirm the detector is not operational:
- White MC: `line_contaminated` 500/500 for L and 500/500 for R at each of
  −72/−66/−60 dBFS.
- Colored MC: the same 500/500 rejection in both channels at each level.
- Measured-profile pilots: 100/100 flagged for both channels at 997, 1000
  and 1013 Hz.
- Uniform-comb pilots: 50/50 flagged for both channels.
- The unit tests explicitly expect `line_contaminated` for a pure single tone
  with white noise and for a pure tone with no noise.

Therefore 19/19 PASS means the tests reproduce the current rejection behavior;
it does not mean the QC distinguishes clean and contaminated records.
There is no demonstrated positive `status='ok'` path for ordinary captured
signals. A fail-closed system that rejects clean data 100% of the time is
safe from false acceptance but is not a usable estimator.

Additional implementation issues:
- `qc_line_db` is an exposed parameter but is not used; the threshold is
  hard-coded as ratio 3.16.
- `line_amp_db` is the absolute FFT-bin magnitude in the current numeric
  scaling, not an amplitude in dBc relative to the fitted carrier.
- The detector tests a single maximum bin; it does not require a coherent
  multi-bin line shape or establish persistence across adjacent bins/windows.

### 2.1 The known −127 ppm case is not protected

The QC mask explicitly removes `abs(fr - f_est) <= 5 Hz`. The v2.18.4
counterexample used a 997 Hz pilot and a strong 1000 Hz line: separation is
about 3 Hz. That is exactly inside the masked region.

The v2.18.5 868 test only proves the separate `edge_peak` rejection path.
The committed tests do not rerun the in-band-coarse-peak / −127 ppm
counterexample and demonstrate a correctly attributed `line_contaminated`
status. The current residual search can therefore miss the contaminant that
caused the earlier bias. A separate spurious maximum elsewhere may still
cause the record to be rejected, which is not detection of the 1000 Hz line.

Required before acceptance:
- Add the exact v2.18.4 −127 ppm construction and a controlled sweep of
  interferer offsets around the carrier, including ±3 Hz and ±5 Hz.
- Do not blanket-mask the region where a neighbouring line can bias the
  estimate. Use a carrier-plus-nuisance-line fit or another local residual
  model that can distinguish carrier leakage from a real adjacent line.
- Calibrate thresholds in dBc relative to the fitted carrier and against
  clean white/colored residual distributions. A multi-bin/persistence test
  is a useful starting hypothesis, but must be validated, not assumed.
- Require explicit positive controls (`ok`) for noise-free tones,
  clean white-noise floors and representative colored floors; require
  rejection for known strong lines and edge/grid failures.
- Record confusion counts (TP/FP/TN/FN), not only total status counts.
- Keep raw frequency estimates and status-separated metrics distinct.
  Do not count a rejected estimate as an accepted measurement.

## 3. Fail-closed joint estimator — safer but not yet usable

`est_joint` now returns `(None, None, fs, status)` for a rejected start.
That is preferable to falling back silently to the nominal frequency.

The current implementation obtains its single-channel start only from R:
`qc0 = est_lsq(R, ...)`. It does not try L or select the better valid
channel. Thus a bad R channel still blocks joint GLS even if L is clean.
The production architecture previously agreed was to evaluate both channels,
then use the most informative valid channel or reject if neither is valid.

The success path is also untested: the unit test comments explicitly say no
normal `status='ok'` path is available because the current QC rejects all
nonzero residuals. Do not treat fail-closed as complete until both rejection
and successful accepted measurements are exercised.

## 4. Blocking defect: colored CSD conjugation is still reversed

Files: `v2185_montecarlo.py`, `v2185-verify-colored.json`.

This is a convention mismatch, not simply a question of whether to use a
conjugate in one line.

The source spectra are obtained with:

`S_LR = scipy.signal.csd(L, R, ...)`

SciPy documents that `Pxy` is calculated as the conjugate FFT of X
multiplied by the FFT of Y, i.e. `conj(X) * Y`:
https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.csd.html

For a generated frequency-domain relation `R = g * L`, this convention
therefore gives `S_LR = g * S_LL`, so the transfer coefficient required to
reproduce the measured `csd(L, R)` is:

`g = S_LR / S_LL`

But the current generator uses:

`gR_c = conj(S_LR_model) / S_LL_s`

and the verifier accumulates:

`FL * conj(FR)`

That last product is the **opposite cross-spectrum convention** from
`scipy.signal.csd(L, R)`. The verifier then compares this opposite-convention
quantity with the un-conjugated SciPy target `S_LR`. Consequently, the
reported phase agreement is produced by conjugating the generated relation
and the verification product in opposite places; it does not show that
`scipy.signal.csd(generated_L, generated_R)` matches the measured target.

I independently checked this with a synthetic 1 kHz pair where R leads L by
+0.37 rad:
- SciPy `csd(L, R)` target phase: +0.37 rad;
- transfer phase required to reproduce that target: +0.37 rad;
- v2.18.5 transfer phase: −0.37 rad;
- resulting generated SciPy `csd(L, R)` phase: −0.37 rad;
- v2.18.5 verifier's `L * conj(R)` phase: +0.37 rad.

The committed `v2185-verify-colored.json` therefore does **not** validate the
complex phase convention. Its `gamma2_synthesized` column has no corresponding
target `gamma²` column; it stores band-averaged generated coherence, PSD
deviations and band-averaged phases, not a full target/generated complex-CSD
comparison.

Required:
1. Declare one cross-spectrum convention in code and names.
2. If using SciPy's `csd(L, R)` convention, generate with
   `g = S_LR / S_LL` and verify using `conj(FL) * FR` (or use a
   mathematically equivalent implementation with the same convention).
3. Add a deterministic synthetic phase test with a known +φ channel relation
   and assert the sign of both measured and synthesized CSD.
4. Save target and synthesized auto-PSDs, `gamma²`, complex CSD magnitude
   and phase, plus residual errors on the same frequency grid.
5. Rerun colored MC after the corrected cross-spectrum construction.
   Per-channel marginal statistics may remain unchanged, but do not assume
   joint GLS/`sigma_pred` equivalence without a new run or a complete proof
   tied to the estimator actually used.

## 5. Pilot harmonic profile — still not calibrated

File: `v2185_pilots.py`.

The extraction code still uses `nperseg=8192` at 192 kHz, so its Welch
frequency spacing is 23.4375 Hz. The integration interval is only ±12 Hz
(total width 24 Hz), which captures one bin for these line frequencies, not
the full Hann main lobe.

I independently evaluated the same extraction rule on unit-RMS pure
sinusoids using SciPy Welch:
- 1 kHz: integrated power fraction 0.577 (−2.388 dB);
- 50 Hz: integrated power fraction 0.652 (−1.861 dB).

Thus `A = sqrt(2 * P)` is the right RMS-power-to-peak conversion only after
`P` has been measured correctly. With the current one-bin integration,
the inferred mains amplitudes remain about 1.9–2.4 dB low depending on
bin alignment. This is the same unresolved issue from the v2.18.4 review;
v2.18.5 has added L/R phase and both-channel scoring, but not calibrated
line-power extraction.

Also, `v2185-lsq-pilots.json` stores a hash of the pilot script only. It does
not store hashes for the source WAVs or the estimator module. The manifest
lists per-file SHA256 values but the actual WAV archive is not available in
the Git branch, so the exact input bytes used for this run cannot be checked.

Required: calibrate the extraction with injected on-bin and off-bin lines,
integrate a sufficiently broad main lobe (or use explicit sinusoidal
regression), compare recovered versus known power over the mains frequencies,
and save source WAV hashes plus both script and estimator hashes in the
result JSON. Keep separate L/R phases and score each channel.

## 6. Monte Carlo — results are useful only as conditional raw-estimator evidence

The 40 CSV batch artifacts represent 20 batches per model, 75 rows per
batch, or 3000 trial rows total. Summary JSON reports 500 trials at each
−72/−66/−60 dBFS level per model. The results are still useful for comparing
the underlying frequency estimates on the exact synthetic realizations.

However:
- all per-channel rows are flagged `line_contaminated` by the current gate;
- the CSV batches predate the fail-closed joint-GLS semantics and do not
  include `status_joint_gls`;
- the joint-GLS frequency and `sigma_pred` summaries are calculated from
  these pre-fail-closed rows, not from accepted `status='ok'` results;
- the colored model used the old cross-spectrum construction/convention and
  requires a new run after the CSD fix.

Therefore, retain the LSQ/centroid figures only as **raw estimate accuracy
under their stated synthetic noise construction**. They do not validate the
current QC gate, and the saved joint-GLS/`sigma_pred` results are not
accepted as evidence for the corrected colored model or fail-closed flow.

## 7. Schedule and source package

The schedule script now separates backend pre-pad, backend post-pad and
`Rig._seg` tail reserve. The 0.30 s process wait and 1.50 s program lead
remain explicit. The +375 ms capture-1 head loss remains an observation
without an established physical cause. This part is accepted as a
diagnostic timeline correction, subject to the prior caveat that 0.30 s is
measured from process launch, not the timestamp of the first captured sample.

The WAV manifest is internally consistent (32 files, unique names, total
byte sum matches the manifest). The archive itself remains local at
`/opt/MyApps/Adudio/v2185-wav-archive/v2185-wav.tar.zst`; it was not
uploaded or published. I have not independently validated the manifest's
SHA256 values against the WAV payload. Do not publish a release asset until
the user explicitly decides where/how the evidence archive should be shared.

## 8. Required next diagnostic iteration

1. Fix the CSD convention end-to-end and add a known-phase unit test; then
   regenerate full colored verification and rerun colored MC.
2. Repair residual QC: detect nearby nuisance lines rather than masking
   ±5 Hz; calibrate dBc/local-noise thresholds; demonstrate clean positive
   `ok` cases and strong-line negative cases; publish TP/FP/TN/FN.
3. Add the exact 997 Hz + 1000 Hz / −127 ppm in-band-coarse case and nearby
   interferer offset sweep; confirm correct attribution of the rejection.
4. Calibrate pilot line-power extraction against injected on/off-bin lines;
   capture the full main lobe; include source-WAV and estimator hashes.
5. Update joint-start selection to try both channels or select the best
   valid channel; test both success and fail-closed paths.
6. Regenerate MC and pilot summaries with accepted/rejected counts; do not
   score invalid statuses as accepted estimates.
7. Publish the complete WAV archive through a user-approved accessible
   location; retain the manifest and full metadata. No blind upload.
8. Keep the entire next iteration diagnostic-only: no production estimator,
   clock gate, pilot frequency, main branch, PR, playback, hardware or DUT
   changes.

