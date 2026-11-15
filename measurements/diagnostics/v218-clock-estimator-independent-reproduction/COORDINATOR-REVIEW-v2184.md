# Coordinator review — v2.18.4 evidence closure

Review target: `agent/v2184-clock-estimator-evidence-closure`, commit
`4fc938e9b970655081bd82362336249fb32df461`, based on
`24b5eeab02c11cdbf9d7ffb889bc61d0ed7e8f27`.

The branch is one commit ahead / zero behind the agreed base. The commit adds
diagnostic artifacts under `measurements/diagnostics/`; no production source,
main branch or PR #4 changes are present in this compare. No playback, hardware
or DUT measurement is claimed or authorized.

## Decision

**Partially accepted as diagnostic progress; NOT accepted as a closed evidence
package and NOT approved for production.**

The 868 Hz failure has now been reproduced against the correct historical
v2.18.1 estimator. The white-noise dBFS correction and one-tone resampling
test are useful fixes. However, the new estimator has a curvature-sign defect,
the known out-of-band-line failure remains without post-fit QC, the pilot
harmonic profile is still under-integrated, the colored verification output is
missing/incomplete, the timeline still mislabels backend padding, and the raw
WAV evidence is not accessible from the branch.

## 1. Historical 868 Hz failure — accepted

I compared `v2184_868_reproduction.py` with the historical
`v218_estimators_real.py` at `8d8b040` and with
`v2181_lsq_pilots.py` from that revision.

The v2.18.4 reproducer uses the same signal construction and seed formula as
the original 997 Hz / −30 dBFS case: 997 Hz pilot at −72 dBFS, 40 mains
harmonics, −96 dBFS white noise, `BASE_SEED + 997000 + 300 + trial`.
The reproduction is algorithmically faithful to the historical unbounded
coarse parabola; diagnostic logging is added.

Saved result: 100/100 estimates fall within 5 Hz of 868 Hz; range
865.387–870.506 Hz, mean 868.085 Hz, sample σ 1.946 Hz. Trial 0 records
coarse bin 999 Hz, neighbors [1.4535, 189.8412, 379.6506], positive second
difference +1.4216, unbounded offset −133.015 bins and final estimate
865.8348 Hz. The result is consistent with the v2.18.1 JSON's implied mean
near 868.098 Hz.

**This closes the historical reproduction question.** The cause is the
unvalidated coarse interpolation on a rising, convex flank at the acquisition
band edge, followed by a narrow RSS search centred on the corrupted frequency.
It does not imply that every trial returns exactly 868.000 Hz; it reproduces
the same catastrophic ~868 Hz failure cluster.

## 2. New v2.18.4 estimator — blocking implementation defects

File: `v2184_estimators.py`.

### 2.1 Curvature sign is reversed

The coarse array is `X = abs(rfft(...))`; the acquisition point is selected
as a **maximum**. The code defines

`curv = y[k-1] + y[k+1] - 2*y[k]`

For a local maximum, this second difference is negative (concave down).
The current code performs parabolic interpolation only when `curv > 0`,
which is the convex/minimum case. Thus normal spectral maxima are not
parabolically refined; convex minima may be refined instead.

Correct the sign/validity test and add unit tests for a known off-bin sinusoid,
flat curvature, convex neighbourhood and a concave peak. Keep the vertex
offset bounded to ±0.5 bin. Do not allow a failed interpolation to masquerade
as a valid coarse acquisition.

### 2.2 Edge status is only metadata, not a fail-closed result

`est_lsq` returns a frequency even when `status == "edge_peak"`. The caller
must reject it, but `est_joint` currently unpacks the status and discards it.
The pilots also record the status but still score the estimate.

Propagate acquisition validity through all callers. An edge peak must not be
used as a valid start for RSS refinement or joint GLS unless an explicit,
tested recovery procedure (e.g. widened acquisition interval) succeeds.

### 2.3 RSS-grid boundary is not checked

The RSS search uses a fixed ±0.15 Hz grid. If the minimum occurs at either
endpoint, the current code returns that endpoint without setting an invalid
or boundary status. Add an explicit `fit_grid_edge`/non-converged state and
do not report it as a normal estimate.

### 2.4 Known −127 ppm case remains unresolved in code

The report itself records a 997 Hz pilot plus a 1000 Hz line at −30 dBFS:
coarse maximum is inside the search band and status is `ok`, yet the fit is
biased by −127 ppm. This proves that edge detection alone is insufficient.

No post-fit residual line detector or residual-quality gate is implemented in
`v2184_estimators.py`. The function currently returns only frequency,
fit-rate and coarse status. It does not expose residual RMS, in-band SNR,
prominent residual lines, RSS-grid convergence or a valid per-channel
uncertainty. The agent has correctly identified the next requirement, but has
not implemented it.

Required: retain residuals after the sine/cosine/offset fit; measure local
residual level and prominent line amplitude/frequency near the carrier; test
a strong 1000 Hz interferer with 997 Hz pilot (and the converse), both
inside/outside coarse band, plus white/colored floors and ±100 ppm acquisition.
An invalid fit must be distinguishable from a valid frequency estimate.

## 3. White and colored Monte Carlo

### 3.1 White — level fix accepted, conditional model evidence

`v2184-verify-white-rms.json` reports −79.99 dBFS and −96.00 dBFS against
declared −80/−96 dBFS under the production `RMS × sqrt(2)` convention.
The white generator correction is accepted.

The branch contains 20 white and 20 colored CSV batches; each batch adds
75 trial rows plus a header (3000 trial rows total). Sampled rows show
48 kHz fit rate. The reported white-model LSQ/centroid comparison and
`sigma_pred` coverage are useful **within this synthetic white model only**.
The prediction is conservative (pred/actual about 1.40–1.46); do not call
this a calibration for real during-tone noise.

### 3.2 Colored — MC result conditional; full verification not closed

The colored MC summary supports LSQ σ around 3.17/3.34 ppm versus centroid
around 10.47/10.67 ppm at −72 dBFS within this constructed model. It also
correctly shows the white-model `sigma_pred` is unusable for colored noise
(pred/actual around 0.06). The source model is based on quiet/idle windows and
does not reproduce the known during-tone L/R asymmetry; this limitation must
remain explicit.

The report's table leaves the `gamma² model` column as “—”. The verifier
code outputs synthesized gamma² plus band-mean PSD deviations and band-mean
cross phases; it does not save a complete target/generated comparison of
gamma² and complex CSD magnitude/phase by frequency bin. It does not report
phase residuals/dispersion. In addition, the script writes
`v2184-verify-colored-full.json`, but that file is absent from the pushed
branch (404 on the branch).

Therefore “full colored verification” is **not accepted**. Commit the actual
verification JSON and provide per-band (preferably per-bin) target/generated
auto-PSD, gamma², complex CSD magnitude and phase, plus phase/magnitude error
using consistent normalization.

## 4. Pilot harmonic profile — derived from WAV, but not yet a valid line-power estimate

The source code does load WAV captures, which fixes the previous hard-coded
profile. However:

- `nperseg=8192` at 192 kHz gives Δf = 23.4375 Hz.
- The integration interval is only ±12 Hz (24 Hz total), so it contains
  typically one Welch bin. A Hann-window spectral line spreads over a wider
  main lobe; one bin does not integrate its full power.
- Independent numerical check using the same Welch settings: a unit-RMS
  1 kHz sine yields only about 0.577 of total integrated power in that
  interval (estimated peak amplitude about −2.39 dB low); a 50 Hz sine gives
  about 0.651 (about −1.86 dB low). The exact capture-dependent loss varies
  with bin alignment and line frequency.
- `A = sqrt(2·P)` is the correct RMS-power-to-peak conversion only after
  `P` has been correctly integrated. It does not fix the narrow integration
  window.
- `S_LR` is calculated but never used. Synthetic lines receive the same
  random phase in L and R, and the pilot runner scores only `x[:, 0]`; it
  does not run a separate LSQ/centroid result on channel R. Thus the report's
  “per-channel” profile is not a complete per-channel robustness test.
- The JSON contains a hash of `v2184_pilots.py`, but not hashes/metadata for
  source WAVs or the estimator module.

Use a line-power extraction method that captures the full lobe (e.g. a longer
Welch segment such as the full 0.25 s quiet window, with an appropriate
integration band and injected-tone calibration, or explicit sinusoidal
regression). Verify recovered power against known off-bin injected lines.
Preserve source WAV SHA256 and format metadata, estimator/script hashes,
and the measured L/R cross-spectrum/relative phase if the pilot model claims
to represent a two-channel noise environment. Run/score both channels.

## 5. Schedule — arithmetic still has a mislabeled 50 ms term

Production `valvebench/backend.py` confirms `play_record()` creates a
50 ms zero array and concatenates it both **before and after** the output:
`[pad, out, pad]`. It then launches capture, waits 0.30 s, and launches
playback. The program has a 1.50 s edge lead. `Rig._seg` has its own
50 ms tail reserve; settle is 0.40 s and analysis duration is 2.00 s.

The nominal tone start arithmetic remains 1.85 s, but the 50 ms term in that
start equation is the **backend leading playback pad**, not a D10 hardware
latency and not a “backend tail pad”. The backend also has a separate 50 ms
trailing playback pad; it is not included in the v2.18.4 component list.
The `v2182_schedule.py` variable `BACKEND_TAIL_PAD_S` is used in
`SCHEDULE_START`, and its comments call it latency. This still conflates
the backend pre-pad, backend post-pad and `_seg` tail reserve.

Rename and report all three 50 ms items separately:
1. backend leading pre-pad (contributes to tone start);
2. `Rig._seg` tail reserve (part of the segment timeline);
3. backend trailing post-pad (after the programme; not tone onset).
Do not describe either zero pad as measured D10 latency. Preserve the
0.30 s process wait and the separate 1.50 s programme lead. The observed
capture-1 +375 ms head loss remains a strong observation, but the physical
cause is unestablished; the +350 ms window-overrun check still uses
hard-coded old onset values.

## 6. Resampling

The numerical test now uses one isolated 1 kHz tone per run; this closes the
previous multi-tone/single-tone-estimator contamination for that 1 kHz test.
The exact `1e6/N_padded` quantization formula remains sound. The fractional
linear-interpolation path is explicitly a demonstration, not production.
The 10/20 kHz phase-impact table is analytical rather than a numerical
high-frequency resampler validation. No production resampler change follows.

## 7. Source artifacts and reproducibility

The commit includes the report, scripts, JSON summaries and 40 CSV batches.
It does **not** include the claimed 271 MB `wav/` payload. The referenced
`v2183-wav-manifest.json` is absent on the branch, and
`v2184-verify-colored-full.json` is also absent. The pilot script defaults
to a host-specific absolute source directory. Local ignored files cannot be
independently audited from a pushed branch.

Provide an accessible archive/release asset containing every source capture
and programme WAV required by the scripts, with complete SHA256 and metadata
(sample format/dtype, sample rate, channels, frame count). Keep the scripts
able to run with an explicit `--source-dir` and record the actual source
hashes used for each generated profile/result.

## 8. Next diagnostic-only iteration

1. Fix coarse-peak parabolic interpolation sign/validity; add deterministic
   off-bin, flat, convex and concave tests. Add RSS-grid boundary status.
2. Implement line-aware post-fit residual QC and explicit acquisition/fit
   validity. Re-run the exact 868 reproduction and the 997/1000/1013 tests.
3. Make callers fail closed on `edge_peak`, `fit_grid_edge` or residual-QC
   failure; do not let joint GLS silently consume an invalid single-channel
   start.
4. Rework the pilot profile extraction with calibrated line-power recovery;
   include L/R phase/cross-spectrum as required and score both channels.
5. Commit the colored verification JSON with target/generated PSD, gamma²
   and complex CSD metrics.
6. Correct schedule labels for the backend pre-pad, backend post-pad and
   `_seg` tail reserve; keep the timing observation and its limitations.
7. Publish the complete source WAV archive/manifest. Regenerate all tables
   from the committed artifacts.
8. Keep all work diagnostic-only: no production estimator, clock gate,
   default pilot, main branch, PR, playback, hardware or DUT changes.

