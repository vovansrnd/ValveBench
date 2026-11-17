# Coordinator review — v2.21.0 (18 Nov 2026)

## Execution acceptance

Agent result:
`416a06be49d7a4a87217e05d672e4a299fe764b7` on
`agent/v2210-coordinator-authored-validation`.

GitHub verification:
- Direct child of pinned task-branch head
  `b676663921e2a0fc61e70f987b5fcf77d4c26f7c`.
- Exactly seven contracted files added: three complete JSON files, three
  complete run logs, and execution manifest.
- Manifest checked out frozen test-source commit
  `d42c6b4a3677c8f0521d41926b8782c6a4d23447`.
- `py_compile` ×4 passed; all three tests exited 0; `git diff --check`
  clean.
- Runtimes: clock boundary 2.14 s; two-block line-QC 3:56.42;
  nearby-duration comparison 9:31.30. Maximum RSS: 167,852 KB,
  125,892 KB, and 162,128 KB respectively.
- Manifest and commit confirm no test, frozen-estimator or production source
  modifications.

Execution is accepted. Algorithmic conclusions are reviewed separately.

## 1. Production clock preflight — confirmed occupancy false accept

The full `Rig.run()` → `clock_check()` test contains 21 cases:
12 return `ok=True`; 9 reject in `Rig.run()`.

### 1.1 Contiguous front window

| Tone active fraction | Result |
|---:|---|
| 20% | PASS / `ok=True` |
| 30% | PASS / `ok=True` |
| 35% | PASS / `ok=True` |
| 40% | PASS / `ok=True` |
| 45% | PASS / `ok=True` |
| 49% | PASS / `ok=True` |
| 50–90% | rejected by `Rig.run()` |

At exactly 20%, both channels barely pass the production local-SNR floor:
20.29 / 20.32 dB. The measured frequency estimates remain within the
production ppm gate (−1.85 / −3.01 ppm), so the overall result is
`ok=True`, despite the capture containing only 0.4 s of tone in a 2 s
analysis window. The analyzer also emits onset/coherence warnings.

At 40% front, the signal is present for 0.8 s and absent for 1.2 s;
`Rig.run()` still passes and `clock_check()` returns `ok=True`.
The output includes low inter-block carrier-phase coherence warnings.
The current gate excludes those warnings from `signal_ok` and overall
`ok`.

### 1.2 Other 40% placements

All these cases also pass with `ok=True`:
- centred 0.8 s interval;
- final 0.8 s interval;
- 0.4 s in each of the two 1 s blocks;
- two 0.4 s bursts in the first block;
- four 0.2 s bursts distributed across the window.

For 40% centre and split cases, channel SNR is only about 20.3–20.4 dB.
The 40% back case reports about 47 dB; the four-burst case about 24.4 dB.
Thus equal total duty fraction does not imply equal spectral/SNR behaviour.

50% and 60% split-across-blocks cases reject. The test therefore supports
the exact result of this matrix, not the claim that every arbitrary waveform
has a universal 50% boundary.

### 1.3 Mechanism and qualification

`_window_stability_reason()` estimates a separate median for each active
channel and calls a run a dropout when it is at least 12 dB below that
median for at least 50 ms. When quiet frames form the majority, the median
moves into the quiet region, so the quiet intervals do not look like drops.
When the tone occupies the majority, the median moves to the tone and
longer quiet intervals are detected.

The 50 ms contiguous-run condition also leaves a second untested boundary:
rapidly repeated short dropouts may evade the guard even when total
occupancy is high. v2.21.0 did not test a dense burst train with each gap
shorter than 50 ms.

**Decision:** confirmed false acceptance of incomplete clock-preflight
windows. Do not modify production yet. Next test must characterize
sub-50-ms interruption patterns, measure one-second-block tone presence
independently, and compare candidate occupancy metrics against genuine
continuous tones over the supported clock-error range. Phase coherence alone
must not be made a hard failure: it can fall when the DAC/ADC clocks differ,
which is precisely what the clock check measures.

## 2. Two-block line-QC persistence — promising candidate, not accepted

The test evaluates the frozen independent v2.18.8 LSQ estimator, not
production `clock_check()`.

- Line-free white noise: 56/540 full-window flags (10.37%), but 0/540
  persistent flags requiring both blocks to report a line within 0.5 Hz.
- Colored noise: 1/60 full-window flag; 0/60 persistent flags.
- Stable injected spurs: 31/48 full-window detections (64.6%) and 28/48
  persistent detections (58.3%).
- All 24 stable injected −30 dBc cases were detected by both full-window and
  two-block persistence.
- At −35 dBc, full-window detected 7/24; two-block persistence detected
  4/24. This weak-line sensitivity is already limited by the frozen
  detector's threshold and cluster policy.

The false-positive reduction is significant in this seeded synthetic
matrix, and strong injected spurs are retained. However, there is a
methodological mismatch in the per-block implementation: the frozen
`QC_CFG.floor_bins=161` spans approximately ±40 Hz at 0.5 Hz/bin in the
two-second FFT, but spans approximately ±80 Hz at 1 Hz/bin in each
one-second FFT. The per-block local-noise estimator therefore uses a
different physical bandwidth than the full-window estimator.

**Decision:** do not adopt the candidate yet. Repeat with a physical-width
normalized local floor (approximately 81 bins at 1 Hz/bin), retain the
161-bin version as a comparison, and include off-bin and one-block-only
spur controls. No `QC_CFG` threshold or production status changes.

## 3. Nearby-line LSQ duration — longer is often better, not a universal fix

These results again concern the independent diagnostic v2.18.8 LSQ
estimator only.

- Pilot-only baseline absolute fit-error p90 improves from 0.0659 ppm at
  2 s to 0.0241 ppm at 4 s; maximum improves from 0.1107 to 0.0351 ppm.
- In paired injected-line records (192 independent-phase pairs), 4 s gives
  a smaller induced shift in 163/192 pairs and a larger shift in 29/192.
- In paired same-initial-phase records (192 pairs), 4 s gives a smaller
  induced shift in 128/192 and a larger shift in 64/192.

Examples:
- Independent phase, Δ=0.25 Hz / −10 dBc: median |shift| 47.63 ppm
  (2 s) → 13.40 ppm (4 s); 4 s maximum 28.37 ppm.
- Independent phase, Δ=0.40 Hz / −10 dBc: 50.65 → 5.36 ppm.
- Same initial phase, Δ=0.25 Hz / −10 dBc: 12.63 → 20.05 ppm (worse).
- Same initial phase, Δ=0.75 Hz / −20 dBc: 0.046 → 2.51 ppm (worse).

The p90 ratio can look very large where the 2 s baseline shift is close to
zero; compare absolute ppm shifts and paired improvement counts, not ratios
alone. The 4 s window improves pilot-only numerical precision, but it does
not eliminate the deterministic nearby-component bias and is not uniformly
better for all phase/offset combinations.

**Decision:** do not change production capture length or the frozen LSQ
estimator based on this run. A follow-up compares more durations (2/3/4/6 s)
for the close-offset cases, while keeping relative phase classes and paired
seeds explicit.

## 4. v2.22.0 coordinator-authored follow-up

The next package is diagnostic-only and has three targets:

1. Full production path: add pulse trains with duty fractions on both sides
   of 50%, including repeated silent gaps shorter than 50 ms; instrument
   per-block SNR/level as well as the full-window result; add continuous
   tones with controlled relative clock offsets up to the production
   ±100 ppm limit. Evaluate upper-quantile-relative occupancy and
   per-block criteria offline; do not implement them.
2. Line-QC persistence: compare the existing 161-bin per-block floor with
   a physically normalized 81-bin floor; add off-bin stable spurs and
   first-block-only / second-block-only spur controls; report false alarms
   and sensitivity by frequency and level.
3. Nearby-line duration robustness: paired 2/3/4/6 s fits for the
   close-offset cells, preserving both phase-locked and random-phase
   populations and reporting absolute induced shifts, not just ratios.

No production, main, PR #4, pilot-frequency, clock gate, threshold or
frozen-estimator edits are authorized.
