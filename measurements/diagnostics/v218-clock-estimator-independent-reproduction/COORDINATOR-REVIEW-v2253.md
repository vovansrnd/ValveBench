# Coordinator review — v2.25.3

## Status

**Execution and numerical output accepted as a useful read-only characterization, but the agent report is not accepted as final.** The analyzer ran successfully on the pinned source and the original probe. Several report statements are inaccurate, and one explicit objective (frequency sub-band localization) was not actually measured.

## Provenance and publication

- Pinned analyzer/source SHA: `ae65d7caf41758c9275a3af5c0db68ff3f208dba`.
- Result commit: `79de22ba2ace66d6e888aeead098bf1e8a880fab`, parent `64055dcde5839c945b8bc48b1fe894ab11c169b`.
- Result commit changes exactly the four success artifacts: JSON, run log, execution manifest, and agent report. No analyzer or production file was changed in that result commit.
- Input WAV: 5,760,044 bytes, SHA-256 `d1676fe55e822e703d89c34b1ee2f2fbb96143eb163540e18078081756282ca9`, 96 kHz stereo, 960,000 frames, 10 s. It matches the earlier v2.25.0 probe identity.
- No hardware or capture action is evidenced by the execution manifest/log; the task explicitly prohibited them. The corrected manifest must include an explicit truthful attestation of no ALSA/device access, recording, playback, or hardware changes.

## Verified measurements

### L/R level asymmetry by 2-second window

The nine windows overlap by 1 s and are descriptive snapshots, not independent observations.

- L−R median PSD difference: **16.19 to 17.30 dB**.
- L−R mean PSD difference: **16.96 to 17.06 dB**.

Per-window median differences are 16.51, 17.28, 16.37, 16.46, 16.19, 17.30, 16.97, 16.92, and 16.95 dB. The mean differences remain around 17 dB. Thus, the channel asymmetry is persistent across this one recording and is not confined to its first 2 seconds.

### Actual within-channel mean/median ratios

The report currently labels the 16.2–17.3 dB L−R median asymmetry as a “mean/median” quantity. That is a category error.

The JSON's actual `mean_to_median_db` for each channel and window is:

| Window (s) | L mean/median (dB) | R mean/median (dB) |
|---|---:|---:|
| 0–2 | 13.02 | 12.52 |
| 1–3 | 15.54 | 15.77 |
| 2–4 | 20.38 | 19.73 |
| 3–5 | 21.14 | 20.62 |
| 4–6 | 22.10 | 21.28 |
| 5–7 | 20.87 | 21.11 |
| 6–8 | 21.84 | 21.77 |
| 7–9 | 20.76 | 20.72 |
| 8–10 | 20.99 | 20.95 |

Therefore the within-channel mean/median gap is **not** stable at 16.2–17.3 dB. It grows after the initial windows and then remains around 20–22 dB. Report the actual ranges: L 13.02–22.10 dB; R 12.52–21.77 dB.

### Time-resolved peaks

From the 2-second Welch-window peak lists (bin spacing 1.46484375 Hz):
- 1000.488 Hz is the strongest detected peak in **all nine** windows; prominence L/R is 28.48/27.85 dB in 0–2 s and approximately 33.6–40.6 / 32.1–40.0 dB thereafter.
- 984.375 Hz and 1019.531 Hz are both detected in L and R in every window from 2–4 s through 8–10 s. Their early-window detections differ because the prominence is lower relative to the first two windows' local spectral profile.
- Do not claim that every peak is present at identical prominence in all nine windows. State the actual window resolution and detection threshold.

### Full-record peak-list threshold and one table error

The analyzer sets `PEAK_MIN_PROMINENCE_DB = 6.0` and returns at most the top 20 peaks per channel. The report's heading “Prominent peaks (>25 dB)” therefore does not describe the detector or the returned lists.

Across the returned top-20 lists there are 14 shared frequency values, 6 L-only and 6 R-only. That count is consistent with the JSON. It is **not** a count of peaks passing a >25 dB threshold.

The report table also incorrectly marks the R value at 1007.9 Hz as absent. JSON gives:
- L: −117.6169 dBFS²/Hz, prominence 33.2414 dB;
- R: −133.1573 dBFS²/Hz, prominence 27.7841 dB.

The report must either label the table as selected top-ranked examples from the top-20 output (detector threshold 6 dB), or independently filter at >25 dB and recompute the counts. Do not mix the two.

### Coherence limitation

The 26 listed peak frequencies are evaluated at the nearest coherence-grid bin, whose spacing is 1.46484375 Hz. Some nearby candidate peak frequencies map to the same coherence bin; these are not 26 independent coherence estimates. The resulting values (median approximately 0.941, minimum approximately 0.847, maximum approximately 0.99993) support strong L/R coherence at those sampled bins. They do **not** identify a physical source, establish where in the signal chain it enters, or prove an upstream/common analog source.

### “Broadband throughout the band” was not established

The analyzer calculates statistics over the entire 968–1032 Hz band, but it does not split that band into sub-bands. The whole-band median and p90 offsets cannot establish that the excess is distributed throughout every part of the band rather than concentrated in selected sub-bands.

The report must withdraw the categorical statement “raised broadband floor across the entire band” and mark the frequency distribution of the elevated floor as **not resolved by v2.25.3**. A separate sub-band analysis is needed.

## Corrections required

1. Correct the mean/median interpretation and include the actual per-window values/ranges.
2. Correct the peak threshold wording, the 1007.9 Hz R-channel table entry, and the description of the 14/6/6 top-20 intersection.
3. Describe the persistent per-window peaks with their 2-second Welch resolution; do not overstate peak invariance.
4. Qualify the coherence interpretation; no physical source/location inference.
5. Withdraw the unsupported “broadband throughout the entire band” conclusion; explicitly say sub-band distribution was not measured.
6. Complete the execution manifest: include the JSON artifact's exact byte count and SHA-256, `git diff --check` result, and explicit no-hardware/no-ALSA/no-recording/no-playback attestation. Include WAV owner/mode if the existing file is still present and those metadata can be read without hardware access; otherwise clearly state unavailable rather than guessing.

## Scope

Documentation/manifest correction only. Do not rerun the analyzer, do not access hardware, do not capture or play audio, do not modify the analyzer or production code, and do not change any existing JSON values. A separate coordinator-authored task will investigate the 968–1032 Hz sub-band distribution using only the same existing WAV (and will block if it is absent).
