# Coordinator review — independent repeat audit and supplied script package (18 Nov 2026)

## 1. Scope and provenance

The supplied archive was inspected only under
`audit-scripts/valvebench-audit-scripts/`. The separate `audit_package/`
directory was not opened.

The selected directory contains README, 23 Python files, a shell runner,
and saved result logs / JSON. All 23 Python files pass `py_compile`.
There are no WAVs or input datasets inside this directory. Its real-capture
scripts require the external `AUDITOR-PACKAGE` (WAV + previous code) and
`ValveBench-for-audit` paths configured through `AUDIT_PKG` and `REPO2`.
Therefore the archive is a useful review bundle containing sources and
reported outputs, but it is not independently self-contained for replaying
the real-data results.

The stored results contain no `Traceback`, `Exception`, `FAIL`,
`ОШИБКА` or nonzero runner-status marker. One runner caveat: `run_all.sh`
continues after a failed script and the final `echo` makes the overall shell
exit status zero. Individual per-script return codes must be inspected.

## 2. Production-code change audit

The independent auditor's main statement is confirmed for the project
history examined here:

- Base/revision: `02e9591d807a73e4d1229a1377be1137c79df36c`.
- Current frozen test-source commit used for v2.22.0:
  `d42c6b4a3677c8f0521d41926b8782c6a4d23447`.
- GitHub compare reports 36 commits ahead, 0 behind, and **no changed files
  under `valvebench/`**. The later changes are diagnostics, reports,
  execution records and verification journal additions.

Thus the measured product has not improved in this period. The team has
improved its evidence and test apparatus; no production fix has yet been
implemented. This is expected from the execution-only agent contracts, but
the next stage must transition from diagnostics to a separately reviewed
implementation plan.

## 3. Findings accepted as materially supported

### 3.1 Production window-presence gate

The existing `Rig._window_stability_reason()` evaluates 2 ms broadband RMS
frames against each channel's own median. It rejects a run of at least
50 ms that is 12 dB below the median. This test has two weaknesses:

1. If the quiet portion is the majority, the median moves into the quiet
   region; the quiet interval no longer appears to be a drop.
2. If the noise floor is high relative to the pilot, broadband RMS changes
   little when the pilot disappears.

The full-path v2.21.0 synthetic test confirms accepted incomplete windows
from 20–49% contiguous pilot occupancy. The independent real-WAV audit
also shows that the broadband q90 occupancy metric can remain 1.00 with
800 ms of a 2 s window lacking the tone at −66/−72 dBFS.

The auditor's pilot-demodulated 10 ms envelope produces 0.93 / 0.82 / 0.60
for 150 / 350 / 800 ms tail gaps and at least 1.000 on the tested legitimate
windows. This is a promising candidate, not a validated threshold:
the data are only seven captures, with heavily overlapping 20 ms-spaced
legitimate windows. In addition, a 0.97 occupancy floor by itself permits
up to about 3% low frames and does not guarantee detection of one isolated
50 ms gap in a 2 s window.

**Decision:** do not use broadband q90 occupancy as a production criterion.
Evaluate narrowband pilot-envelope occupancy separately for each channel
and each 1 s block, plus maximum contiguous low-envelope run. Include
noise-only, stable pilot-leakage, realistic broadband noise, frequency
offset, AM and real continuous-tone controls. The 0.97 value remains a
candidate only.

Phase coherence must not be turned into a standalone clock-preflight
failure: a continuous pilot with legitimate DAC/ADC frequency offset can
lose inter-block phase coherence.

### 3.2 The 120 Hz line and the meaning of line-QC

The real-window LSQ prototype repeatedly identifies a 120 Hz residual line.
The supplied `hum_lines.py` also shows a comb at multiples of 60 Hz, while
the tested 50 Hz harmonics are near the floor. The source is not identified.
The 120 Hz peak is far from the 1 kHz pilot and is not automatically a
frequency-estimation threat.

The supplied `near_far.py` estimates the worst LSQ frequency effect over
60 phases. At 880 Hz separation and −20 dBc the measured maximum is about
0.0102 ppm. Scaling to the observed stronger 120 Hz line gives an effect
still well below one ppm (order of hundredths of a ppm), while line-QC
marks the channel `line_contaminated`. This confirms a mismatch between
the broad residual-line QC objective and the clock-frequency error budget.

The `near_far.py` printed theoretical column
`0.8·rho·Delta/f0` increases with separation and does not match the
measured LSQ errors. It must not be used as the bias law. The empirical
large-offset trend in these tested cases is approximately proportional to
`rho / Delta` for offsets around 1 Hz and above; treat this only as a
heuristic, not a calibrated bound.

**Decision:** do not make distant residual lines a hard clock-estimator
failure. Develop a pilot-relative bias budget. Keep the v2.22 physical-width
two-block experiment diagnostic-only; it does not by itself define the
correct QC policy.

### 3.3 Nearby components and modulation

The auditor is right that the current `nearby_tone_probe` leaves the
primary LSQ result at `ok` even when it reports a nearby component. Our
v2.19/v2.20 controls also show the opposite problem with a naive hard gate:
AM and FM sidebands trigger the nearby probe. Therefore “any nearby hit =
reject” is not acceptable without a modulation discriminator or a model
that estimates the expected frequency bias of the detected component(s).

The probe's inner exclusion around 0.3 Hz also leaves a region closer than
that uncharacterized. The 1 kHz leakage line observed in silence is reported
at about 1000.00 Hz while the captured pilot reference is about 999.9765 Hz.
That is only about 0.0235 Hz separation, well inside the current nearby
probe's core exclusion. The two-tone interaction may create a systematic
frequency pull even though the leakage level is about 30 dB below the
−72 dBFS input pilot. This is a high-priority test case, not yet a proven
source attribution.

The suggestion to move the pilot to 997 or 1013 Hz is plausible but not
verified. Candidate frequencies must be tested against the 1 kHz leakage
and measured 60 Hz comb, including actual frequency-estimator bias and
detection status. Do not change the production pilot frequency yet.

### 3.4 Noise-derived uncertainty

The CRLB-style formula used by the auditor,
`sigma_f = sqrt(3*N0 / (pi^2*A^2*T^3))`, is consistent with a real
sinusoid in stationary white noise when `N0` is the one-sided PSD and
`A` is peak amplitude. Its application to measured colored noise is a
promising improvement over the prototype's residual-RMS variance.

However, the supplied calibration uses seven captures and 42 highly
overlapping 2 s noise windows, then three injected phases per window.
Those 126 rows are not 126 independent noise realizations. The reported
normalized-error standard deviations near 1.0 are encouraging but not
sufficient calibration for a hard `sigma <= 0.3 ppm` gate.

**Decision:** retain the local-PSD variance model as a candidate; validate
it on independent non-overlapping windows / new captures and test
cross-channel covariance. Do not assume independent L/R errors: the
reported noise coherence near 1 kHz is high, and simple inverse-PSD channel
weights do not model covariance.

### 3.5 Window timing and onset

The narrowband envelope bounds place the end of the tone near 4.30 s in
all seven captures. In capture #1 the tone starts around 2.256 s rather
than around 1.86 s, so the abnormality is loss of the beginning, not a
longer tail. A fixed onset-plus-0.4 s, 2 s window can therefore extend
past the real end of tone.

This is consistent with the fixed settle/window placement remaining an
open production weakness. The next implementation design should determine
the measured tone interval and choose a window fully within it, with a
sufficient safety margin, rather than relying on a fixed tail assumption.
The supplied bounds still depend on a fixed reference plateau and threshold,
so the exact start/end estimator needs its own calibration before adoption.

### 3.6 Resampler quantization and noise transfer

The supplied tests confirm that `resample_capture_timebase()` quantizes
the resampling ratio through integer output length. The step is approximately
one sample divided by the padded record length (around 0.7 ppm for the tested
multi-second captures). The synthetic tone tests show this quantization can
move high-frequency harmonics outside the coherent tolerance; the 8 s
noise-transfer test shows that residual correction error around 3 ppm can
produce approximately −0.67 dB at 10 kHz and −1.9 dB at 18 kHz in H1.

This is a substantive production risk for noise-response measurements.
The sweep test `sweep_eps.py` is insensitive to this effect and should not
be used as evidence that the resampler is harmless.

**Decision:** design a fractional-ratio resampler or a sample-time-domain
analysis path, with tests over record length, residual ppm, frequency and
noise transfer. Do not replace the resampler until phase/amplitude fidelity
and boundary handling are verified.

### 3.7 60 Hz comb

The measured 60/120/180/... Hz comb and absence of comparable 50 Hz lines
are supported by the supplied spectra. The source remains unknown.
The 1000/2000/8000 Hz peaks are also observations; attributing them to
USB frame leakage is a hypothesis only. Monitor refresh-rate and power-supply
tests are reasonable controlled experiments, but no source should be
declared until the line levels respond reproducibly to one isolated change.

## 4. Limits of this audit package

- `selftest.py` checks the fast estimator against direct LSQ on only three
  synthetic white-noise cases. It establishes basic numerical parity, not
  full correctness under the real colored / line-rich noise.
- `xcheck.py` compares the prototype with the packaged `lsq_fast`
  reference on 14 real channel-windows (maximum delta about 0.009 ppm).
  The reference itself is a demodulated/decimated LSQ implementation;
  this is not a second independent direct full-rate LSQ calculation on
  all 14 real windows.
- `replay.py` uses 42 heavily overlapping noise windows from seven captures.
  `replay2.py` and `replay3.py` multiply those windows by repeated injected
  phase trials. These are useful sensitivity replays, but not independent
  sample counts for uncertainty confidence intervals.
- The archive omits all real WAVs and source repository snapshots, as stated
  by its README. Its saved output is reviewable, but a fresh real-data rerun
  needs the external inputs.

## 5. Updated engineering priorities

1. **Window validity first:** replace the broad-RMS median concept with
   pilot-specific per-channel/per-block presence diagnostics; test occupancy
   and max-gap metrics against realistic noise, real pilot-off intervals,
   legitimate clock drift and noise-only captures.
2. **Clock estimator next:** suppress distant-spur false refusals; quantify
   bias from close components (including <0.3 Hz) and the 1000 Hz leakage;
   distinguish single nearby tones from AM/FM sidebands.
3. **Uncertainty:** validate local-PSD CRLB prediction with independent
   noise windows and cross-channel covariance.
4. **Window placement:** establish tone start/end and sufficient capture
   tail before selecting measurement windows.
5. **Resampler:** resolve ratio quantization and validate noise-transfer
   accuracy at high frequencies.
6. **Interference source:** separately investigate the 60 Hz comb and
   1/2/8 kHz leakage; keep source attribution provisional.

All of the above remains diagnostic/design work. No production code, main,
PR #4, pilot frequency, thresholds, clock gate or resampler was changed by
this review.
