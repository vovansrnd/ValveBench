# Coordinator review — v2.18.9 execution (17 Nov 2026)

## 1. Decision

**Execution accepted. Diagnostic conclusions partially accepted. No production approval.**

The coordinator-authored harnesses were executed without edits on the exact published test HEAD `7eb4fb9dc3200dc59228b74b213cb342e2748bfb`. The result commit is `399878851f75af528c102a7d521cad9e2ab5d749`, with the test HEAD as its sole parent. The published comparison is 10 commits ahead / 0 behind the coordinator base and changes diagnostic files only.

- [Execution commit](https://github.com/vovansrnd/ValveBench/commit/399878851f75af528c102a7d521cad9e2ab5d749)
- [Execution manifest](V2189-EXECUTION-MANIFEST.md)
- [Paired SNR JSON](v2189-paired-snr-clockcheck.json)
- [Nearby sweep JSON](v2189-nearby-offbin.json)
- [SNR full log](v2189-snr-run.log)
- [Nearby full log](v2189-nearby-run.log)

Preflight `py_compile` passed on Python 3.14.4 / NumPy 2.3.5 / SciPy 1.16.3. Both harnesses exited 0; `git diff --check` was clean. Runtime/RSS: paired SNR 2:25.27 / 794,596 KiB; nearby sweep 8:33.66 / 127,004 KiB. The agent reports a remote-only local commit `f35e365`; it is not present in the published GitHub history and is not part of this review.

## 2. Paired SNR experiment

### 2.1 What is valid

The v2.18.9 correction fixed the v2.18.8 segmentation mismatch. The harness compares 448 pairs from identical 96 kHz, 2-second sample vectors. Production analysis receives two complete 96,000-sample blocks (1 second each), matching `Rig(fs=96000, df=1, blocks=2)`; `est_lsq` receives the same two-second vector. All 448 pairs are finite.

Observed paired statistics:

- mean diagnostic − production: −7.245 dB;
- median: −3.723 dB;
- standard deviation: 9.584 dB;
- descriptive regression: slope 1.057, intercept −9.753 dB, correlation 0.908.

The regression is descriptive only. This establishes that the metrics are not numerically interchangeable by copying a 20 dB threshold.

### 2.2 Why the thresholds are not equivalent

The metrics estimate noise differently, even with matched capture segmentation.

Production `dsp.analyze_tone` averages block power spectra and estimates local noise from the median of a small neighbourhood, multiplied by `2*half_f+1` bins. At 1 kHz, `HALF=5`, so the local noise integration is 11 bins on a 1 Hz grid.

The frozen LSQ estimator fits one sinusoid over the full two-second record, forms a time-domain residual, and estimates its noise from a much wider local residual band (±32 Hz, excluding ±2 Hz; approximately 120 bins on the 0.5 Hz grid). Nonstationarity and other residual structure therefore contribute differently. These are different SNR definitions, not two implementations of the same measurement.

The diagnostic 20 dB threshold is consequently **not** an equivalent form of production `CLOCK_MIN_SNR_DB=20`. The old ≈8.8 dB conversion from v2.18.8 remains rejected.

The numeric decision matrix happens to match 408 accepts / 40 refusals for diagnostic thresholds from −5 through 15 dB, including 8.8 dB. This is not a calibrated equivalent threshold: the matrix compares only `production_snr >= 20` with `diagnostic_snr >= threshold`; it does not model the complete production preconditions or the LSQ estimator's other QC statuses.

### 2.3 Important production-path qualification

The harness intentionally calls `dsp.analyze_tone` directly. It does not execute the full `Rig.run()` / `clock_check()` path.

The actual `Rig.run()` performs `_window_stability_reason()` before returning the analysis window. The guard rejects a per-channel run at least 12 dB below that channel's own window median when it lasts at least 50 ms. However, whether a particular dropout is rejected depends on the median of the whole window.

The coordinator-authored v2.19.0 direct guard matrix established the following: first-quarter and tail-quarter dropouts are rejected; a half-window dropout is rejected; **40% signal occupancy passes the guard** because the 60% quiet portion shifts the median into the quiet region. This is a demonstrated blind spot of the median-referenced guard itself.

The v2.18.9 paired experiment did not execute `Rig.run()` or `clock_check()`, and the v2.19.0 test only called `_window_stability_reason()` directly. Thus neither experiment establishes whether the complete production clock-check path returns a valid result for the 40%-occupancy capture: onset handling and the production `dsp.analyze_tone` / signal-validity gate still need to be exercised end to end.

### 2.4 Useful signal-quality observations

| Synthetic class | n | Production median | LSQ residual-SNR median | LSQ status / observation |
|---|---:|---:|---:|---|
| steady white noise | 252 | 47.0 dB | 43.5 dB | min LSQ SNR 23.0 dB |
| steady colored noise | 36 | 39.7 dB | 36.1 dB | min LSQ SNR 25.3 dB |
| no pilot | 40 | 0.5 dB | −16.4 dB | all `tone_missing` |
| half-window dropout | 20 | 58.0 dB | 18.5 dB | all `snr_low` |
| 40% window occupancy | 20 | 46.1 dB | 17.4 dB | all `snr_low` |
| first/tail-quarter dropout | 40 | 28.5 dB | 28.1 dB | all `line_contaminated`; production stability guard is the decisive precheck |

The 20 dB LSQ floor separates the tested steady pilot+noise cases (minimum 23.0 dB) from no-pilot controls (maximum −8.6 dB), and it rejects the half-window / partial-occupancy cases in this LSQ-only metric comparison. This does not mean the production median guard rejects the same vectors; its 40%-occupancy case passes in the v2.19.0 direct guard matrix. This supports keeping 20 dB as a **provisional LSQ residual-fit quality floor**. It does not establish equivalence to the production SNR metric, nor does it calibrate all noise distributions.

### 2.5 New candidate issue: line-QC false alarms in white-noise controls

13 of 252 steady-white trials (5.2%) received `line_contaminated` despite having no injected discrete spur. The reported peaks are between approximately 5.9 kHz and 47.8 kHz, mostly in the lower-pilot-level / higher-noise cases. This is a small synthetic sample, not yet a confirmed population false-positive rate, but it is sufficient to require a larger seeded Monte Carlo check before relying on the line-QC status as a hard rejection.

## 3. Nearby residual spectral-line probe

### 3.1 Accepted coverage

The sweep contains 1,440 injected-line trials: both signs, nine offsets from 0.30 to 1.50 Hz, five levels, two phase relations, and eight trials per cell. It also contains 164 controls.

- injected-line hits: 904 / 1,440 (62.8%);
- by level: −10 dBc 288/288; −20 dBc 288/288; −30 dBc 252/288; −35 dBc 76/288; −40 dBc 0/288;
- clean pilot: 0/24 hits;
- pilot + broadband noise: 0/24 hits;
- measured-profile controls: 0/20 hits;
- AM controls: 48/48 hits;
- FM controls: 48/48 hits.

Positive/negative offsets and both initial-phase modes are now covered. The quiet-control sample is useful but small. AM/FM hits are expected: modulation creates legitimate sidebands, so the detector is responding to residual spectral components, not identifying their physical source.

### 3.2 The reported frequency and amplitude are not quantitative

The residual peak is selected on a 0.5 Hz FFT grid; it is not sub-bin-refined. Across detected injections:

- absolute frequency error: median 0.0735 Hz, 90th percentile 0.2735 Hz, maximum 0.4765 Hz;
- amplitude error: median −0.5 dB, 10th percentile −4.4 dB, worst −12.9 dB.

The larger errors occur near the inner and outer edges of the ±1.5 Hz search region, where the single-tone fit can absorb part of the second component and the search window truncates its residual lobe. Therefore `nearby_tone_hz` and `nearby_tone_db` must not be presented as calibrated frequency/amplitude measurements. For now they are diagnostic peak metadata only.

### 3.3 Decision

Keep the implementation classified as a **nearby residual spectral-component probe**. It does not distinguish an independent oscillator from AM/FM sidebands. Keep it diagnostic-only: no new refusal status, no change to the ±1.5 Hz exclusion, and no production/pilot/clock-gate edits.

## 4. Coordinator decisions and next validation

1. Accept the v2.18.9 execution and retain its raw JSON/logs as reproducibility artifacts.
2. Keep production `CLOCK_MIN_SNR_DB=20` unchanged.
3. Keep the frozen LSQ `snr_min_db=20` unchanged for this diagnostic baseline, but stop describing it as numerically equal to production SNR. The definitions differ.
4. Do not infer an 8.8 dB equivalent threshold from the decision matrix.
5. Keep nearby probe diagnostic-only. Do not treat its frequency or amplitude output as a measurement.
6. Prepare v2.19.0 validation for: (a) a full production stability-guard cross-check on the same dropout vectors; (b) a larger white/colored-noise false-line Monte Carlo for residual line-QC; and (c) primary LSQ frequency bias with/without a nearby injected component, including modulation controls. The next harnesses remain coordinator-authored; the agent executes them and returns raw results.

No production, main, PR #4, pilot-frequency or clock-gate changes were made. No playback, physical hardware or DUT testing was part of this synthetic validation.
