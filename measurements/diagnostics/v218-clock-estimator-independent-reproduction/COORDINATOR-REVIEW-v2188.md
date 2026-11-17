# Coordinator review — v2.18.8 SNR comparison and nearby residual-line probe

Review target: `agent/v2188-snr-calibration-nearby-tone-design`, commit
`89569a1e4753d6c56cbb33e9ae19252929c9f170`, based on
`0fbf2e2284b61b07347d0570c347673cc89e536c`.

Branch comparison: 7 commits ahead / 0 behind. The changed files are
diagnostic reports, scripts, JSON and tables under
`measurements/diagnostics/v218-clock-estimator-independent-reproduction/`.
No production source, main or PR #4 changes are present. No playback,
hardware or DUT testing is claimed. WAVs were not retransmitted.

## Decision

**Partially accepted as diagnostic progress. Not accepted as final SNR
calibration or as a validated nearby-tone discriminator. No production
approval.**

Accepted:
- The v2.18.7 tone gate and partial-channel changes are preserved in
  `v2188_estimators.py`.
- The new experiment confirms that the v2.18.7 and production SNR values
  are not interchangeable by simply copying the same numeric 20 dB limit.
- The original −127 ppm test is still honestly labelled a synthetic
  reconstruction; the source script/input was not found in the available
  Git history.
- The beat-envelope attempt was abandoned rather than silently retained.
  The current implementation is a local spectral-peak probe and does not
  change the estimator status.
- The probe's current boundary, ±1.5 Hz, is unchanged.

Blocking qualifications:
1. The SNR comparison does not reproduce the standard production
   `Rig` analysis block layout: it feeds one 2-second block to
   `dsp.analyze_tone`, whereas the standard `Rig(fs=96000, df=1,
   blocks=2)` path analyzes two 1-second blocks. Its regression and
   derived 8.8 dB threshold are therefore not the production-path result.
2. The nearby-tone experiment only tests positive offsets and offsets
   exactly at 0.5 Hz multiples. It does not justify claims for both signs
   or general off-bin interferers.
3. The nearby probe is a residual local spectral-line detector, not a
   general discriminator of an independent tone from modulation sidebands.
   Its code comments and experiment-script docstring still contain stale
   descriptions of the rejected beat-envelope method.

## 1. SNR metric comparison — useful, but not matched to production configuration

Files: `v2188_snr_compare.py`, `v2188-snr-compare.json`,
`tables/snr-compare.md`.

The source correctly pairs both calculations on the same generated sample
array and computes:
- production `dsp.analyze_tone(...).local_snr_db`;
- diagnostic `est_lsq(...).snr_carrier_db`.

The measured metrics are clearly not numerically equivalent in the current
test: median difference −7.87 dB, standard deviation 13.84 dB; a pooled
linear regression gives slope 1.192 and offset −15.09 dB. At the tested
20/20 dB thresholds, no case was accepted by the diagnostic gate when
production rejected it; 88 cases were accepted by production and rejected
by the diagnostic gate.

However, the production call at the comparison loop is:
`dsp.analyze_tone(x, FS, PILOT_NOM, N, ...)`, with `FS=48000` and
`N=96000`. That is one 2-second analysis block.

The standard `Rig` defaults in `valvebench/measure.py` are
`fs=96000`, `df=1`, `blocks=2`; `self.n = fs/df = 96000`.
Its `_tone_an` passes that one-second `self.n` to
`dsp.analyze_tone`, while `clock_check` supplies the two-block
capture window. Thus production's ordinary clock-check path computes its
BH spectrum from two 1-second blocks, not one 2-second block. These
different block lengths change frequency-bin spacing and the local SNR
estimate. The comparison uses the same underlying waveform duration, but
not the same production analysis segmentation.

Therefore:
- accept the conclusion **“the two formulas are not interchangeable by
  copying 20 dB blindly”**;
- do NOT accept the numerical regression as a production calibration;
- do NOT accept `diag ≈ 8.8 dB` as a validated equivalent of
  production 20 dB;
- retain the current diagnostic 20 dB threshold provisionally and
  diagnostic-only; do not change production `CLOCK_MIN_SNR_DB`.

The 111-versus-37 “errors against engineering expectation” is a useful
class summary, not a calibrated error-rate comparison. The labels are
chosen in the generator, and clipping is explicitly outside the SNR
metrics' responsibility (it is detected by line-QC). Do not present the
3× count difference as an independent accuracy guarantee.

Required correction: rerun the comparison using the actual standard clock
path: 96 kHz sampling, a 2-second record, and
`dsp.analyze_tone(x, fs=96000, f0=1000, n=96000, ...)` so that it
processes two one-second blocks. Compare it to `est_lsq` on that exact
same 2-second record. Save per-record paired values and class-level
decision tables. If a non-default Rig configuration is relevant, identify
it explicitly and parameterize block size rather than assuming one layout.

## 2. Nearby probe — accepted as an informational spectral-line probe only

Files: `v2188_estimators.py`, `v2188_nearby_tone.py`,
`v2188-nearby-tone.json`, `tables/nearby-tone.md`.

The implemented `nearby_tone_probe()` is not the beat-envelope probe
described at the top of the module and in the experiment script docstring.
The actual code searches the Hann-windowed LSQ residual spectrum for
clusters within 0.3…1.5 Hz of the fitted carrier, using a local median
floor, at least two bins and a −35 dBc threshold. It reports
`nearby_tone_hz` and `nearby_tone_db`; it does not change status.
This is a useful additional diagnostic field.

Current evidence:
- 540 total trials;
- 60 quiet controls, 0 false fires in that small sample;
- 480 injected-line trials;
- pooled results: Δ=0.5 and 1.0 Hz have full detection at −10/−20/−30 dBc;
- Δ=1.5 Hz: 38/40 detected at −10 dBc, 22/40 at −20 dBc, 0/40 at
  −30/−40 dBc.

Important omissions:
- `v2188_nearby_tone.py` iterates only `delta in (0.5, 1.0, 1.5)`.
  All injected interferers are above the pilot. The report's ±Δ claim is
  not supported by this script.
- The offsets are exact multiples of 0.5 Hz, matching the 2-second FFT
  grid. There are no sub-bin offsets (for example 0.35, 0.65, 0.9, 1.1,
  1.3 or 1.4 Hz) to show robustness to non-bin-centered interferers.
- Results pool free-phase and same-initial-phase cases by stripping the
  final `sync/free` label. The summary does not show separate performance
  for each population.
- The controls do not include amplitude-modulated and frequency-modulated
  pilots. Such modulation naturally produces sidebands; a local residual
  spectral peak alone cannot prove that a detected sideband is a separate
  oscillator. Nor does the current probe distinguish a coherent nearby
  component from a modulation sideband.
- The assertion that fit leakage is confined to `|δ| < 0.02 Hz` is not
  demonstrated by the saved tests. A finite Hann-windowed 2-second
  residual has a spectral main-lobe shape; the small amplitude of the
  residual may keep it below threshold, but its spectral support is not
  established as a sub-0.02-Hz line.

Decision: keep this probe diagnostic-only. Call it a **nearby residual
spectral-line probe**, not a fully validated independent-tone discriminator.
Do not promote it to a refusal condition or alter the ±1.5 Hz exclusion.

Required follow-up:
- test both positive and negative offsets;
- add non-bin-centered offsets throughout 0.3…1.5 Hz;
- report free-phase and same-initial-phase results separately;
- include AM/FM and other nonstationary pilot controls;
- replace stale beat-envelope comments with the method actually implemented;
- preserve status and the existing exclusion boundary.

## 3. Historical −127 ppm evidence

The agent's search supports the earlier qualification: the original inline
v2.18.4 script and input are absent from available history. The v2.18.7
synthetic reconstruction remains correctly labelled as such. No new claim
of exact historical input reproduction is accepted.

## 4. Required next iteration — diagnostics only

1. Rebuild the SNR paired comparison with the exact standard production
   block segmentation. The coordinator will supply the test harness;
   the agent's task is to run it and return raw logs/JSON, not redesign
   the methodology.
2. Revalidate the nearby spectral probe using both signs and off-bin
   offsets, split synchronous/free-phase results, AM/FM controls and
   honest per-class metrics. The coordinator will supply the harness.
3. Fix the stale method descriptions so all docs match the actual spectral
   residual-peak implementation.
4. Keep diagnostic 20 dB provisional; do not infer 8.8 dB from the pooled
   regression. No production SNR gate, main, PR #4 or pilot-frequency
   changes; no playback, hardware or DUT work.
