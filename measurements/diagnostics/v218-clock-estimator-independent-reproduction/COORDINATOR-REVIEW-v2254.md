# Coordinator review — v2.25.4 report precision

## Acceptance of execution and main result

The result commit `2413cc886e8eb7e3d877fadff95238c58860edee` is a direct child of the expected task-branch tip `8a6427dd1f92e6102b8539aff0acb9c0ddb495d3`. It adds exactly the four expected artifacts:
- `v2254-pilotband-subband-distribution.json`
- `v2254-pilotband-subband-distribution-run.log`
- `V2254-EXECUTION-MANIFEST.md`
- `V2254-AGENT-REPORT.md`

The pinned analyzer is `a10e88f8c47751a2b12f504372ba8ee4005a8968`. The manifest documents a single successful run (exit 0, wall 2.28 s, max RSS 259204 kB), the exact source SHA `d1676fe55e822e703d89c34b1ee2f2fbb96143eb163540e18078081756282ca9`, JSON size 173789 bytes, and no hardware/ALSA/capture/playback activity. The JSON structure contains 8 full-record sub-bands and 9 windows × 8 sub-bands.

The main numerical conclusion is supported: the full-record L−R median PSD offsets are 15.45–17.03 dB across all eight sub-bands (full-band offset 16.80 dB); every one of the 72 window/sub-band cells remains positive (14.15–19.93 dB). This indicates an approximately 17 dB L/R asymmetry throughout this band in this one existing capture. It does not identify the physical source or input topology.

## Report corrections required

Only `V2254-AGENT-REPORT.md` needs correction. Do not rerun the analyzer or alter JSON/log/manifest/source.

### 1. Integrated RMS is not a peak-robust statistic

The report calls the 72-cell integrated-RMS L−R metric “peak-robust”. This is incorrect. Integrated RMS is based on integrated PSD power and is sensitive to strong spectral components/outliers. It must not be presented as a robust statistic equivalent to median or percentile summaries.

The full-record integrated-RMS differences calculated from the JSON are:
- 968–976: 16.8441 dB
- 976–984: 16.9047 dB
- 984–992: 16.8388 dB
- 992–1000: 17.0160 dB
- 1000–1008: 17.0131 dB
- 1008–1016: 17.0783 dB
- 1016–1024: 16.9164 dB
- 1024–1032: 16.4675 dB

The two peak-dominated sub-bands are 992–1000 and 1000–1008 Hz (within-channel mean/median ratios about 25.5–28.5 dB). Their integrated-RMS differences are still about 17 dB, but that does not make the metric peak-robust. The six less peak-dominated sub-bands have integrated-RMS differences 16.47–17.08 dB. State clearly that this is a power-integrated complementary measure, not a robust estimator; it includes the peaks in the peak-containing sub-bands.

The median offset and the channel-specific p25/p75 offsets provide the robust-percentile evidence. Note that JSON fields `L_minus_R_p25_psd_db` and `L_minus_R_p75_psd_db` mean (L channel percentile) minus (R channel corresponding percentile); they are not percentiles of a per-bin paired L−R distribution. Avoid implying otherwise.

### 2. Do not explain the 72-cell spread as sampling noise

The report says the 14.15–19.93 dB spread is “consistent with noise of 16-bin samples”. No confidence interval, bootstrap or sampling-uncertainty model was computed. The windows overlap and their estimates are not independent. Replace the statement with a purely descriptive one: the observed 72-cell spread is 14.15–19.93 dB; the 16-bin sub-band windows have limited frequency-bin support, but v2.25.4 does not quantify the statistical uncertainty or establish why the spread occurs. Do not label it random noise or sampling noise as a finding.

### 3. Correct “monotonic/smooth” wording about sub-band offsets

The report says the pattern is “монотонно ровный”. It is not monotonic: the full-record offset dips from 17.03 dB in 1008–1016 Hz to 15.45 dB in 1016–1024 Hz, then rises to 17.02 dB in 1024–1032 Hz. Keep the overall conclusion (no narrow frequency-localized L/R asymmetry; all eight bands remain in a 15.45–17.03 dB range), but describe this as a modest local dip of 1.58 dB, not a strictly flat/monotonic pattern.

### 4. Preserve distinctions in conclusions

- Keep the central finding that median L−R offset persists in all eight full-record sub-bands and all 72 window/sub-band cells.
- Keep the distinction that strong 1 kHz-adjacent peaks raise mean/max in two sub-bands, while their median L−R offsets remain similar to neighboring sub-bands.
- Do not call integrated RMS peak-robust.
- Do not infer physical source, shared path, calibration error or signal-chain location.
- Keep the one 10-second unknown-path capture limitation and no-production/no-hardware-conclusion boundary.

## Scope and output

Documentation-only correction. Update only:
`measurements/diagnostics/v218-clock-estimator-independent-reproduction/V2254-AGENT-REPORT.md`

Do not modify analyzer, JSON, run log, manifest or production files. Do not rerun, access hardware/ALSA, make recordings/playback or recreate the WAV.

Validate `git diff --check`, inspect the one-file diff, commit and push normally. Return commit SHA/URL and confirm exact changed path. Stop on any unexpected branch movement or extra modified file.
