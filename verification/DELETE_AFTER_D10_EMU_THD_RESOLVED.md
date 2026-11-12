# DELETE AFTER RESOLUTION — D10 → E-MU THD diagnostic handoff

> **Temporary investigation log. Delete this file after the D10 → E-MU direct-loopback issue is resolved and the final fix / regression test is documented elsewhere.**
>
> Purpose: preserve the hardware symptoms, exact test history, code facts, disproved assumptions, and a ready-to-run task for an agent with access to the physical D10 and E-MU 0404. This is an investigation note, not a diagnosis or a production specification.

## 1. Problem statement

ValveBench's direct hardware THD test is being checked with a cable directly from the SMSL D10 DAC to the E-MU 0404 USB ADC, with **no phono stage / DUT in the path**.

At 192 kHz, 997 Hz, generator level −30 dBFS, the analyzer initially returned implausibly high THD+N (roughly 6–9%) even though the captured fundamental was present and there were no reported XRUNs or clipping. Full-capture instrumentation showed that the analysis window included the end fade / disappearing tone in earlier runs. Extending the test tone beyond the normal analysis programme greatly improved THD+N, but did not yet establish a fully trustworthy THD measurement or the exact cause of the timing discrepancy.

The current issue is therefore not "measure the phono stage"; it is to validate the **direct DAC → ADC playback/capture timing and residual noise**, then make sure ValveBench selects a stable tone-only analysis window.

## 2. Hardware and fixed test configuration

- DAC: SMSL D10, playback only, ALSA `hw:3,0`, USB VID:PID `152a:85dd`.
- ADC: E-MU 0404 USB, capture only, ALSA `hw:2,0`, USB VID:PID `041e:3f04`.
- Direct cable loopback only. No DUT / phono stage.
- Sample rate: 192000 Hz.
- THD test tone: 997 Hz, −30.0 dBFS.
- Clock preflight: 1 kHz, −30.0 dBFS.
- Standard THD test: `blocks=4`, `settle=0.5 s`.
- Channel maps are loaded from the existing ValveBench calibration; do not change them without evidence.
- Check current card numbers with `./run.sh cli cards` before any new manual run; ALSA card numbers can change after reconnect.

## 3. Relevant current code facts (do not re-investigate from memory)

### `valvebench/measure.py`

`Rig._seg(tones_l, tones_r, settle)` constructs a signal with:

- `s_n = round(settle * fs)`;
- total length `s_n + blocks * n + 0.05 * fs`;
- the tone is generated starting at sample zero (with a 10 ms fade-in);
- the last 10 ms is faded out;
- it returns both the waveform and `s_n`.

`Rig.run()` concatenates programme segments, calls `play_record()`, finds the onset `d`, then selects each analysis window from `d + s_n` through `d + s_n + blocks*n`. For this THD run, that is a 4.000 s window beginning 0.500 s after the detected onset.

**Important correction:** a previous assistant incorrectly described this as "double-adding settle". That was a misreading. The generated segment contains the settle interval, and `Rig.run()` skips that interval when selecting the analysis window. Do not repeat the double-settle diagnosis.

The onset number saved by the diagnostic wrapper is the detector's result; it is not an independent hardware timestamp of the first non-zero DAC sample.

### `valvebench/backend.py` — `AlsaCli.play_record()`

Relevant current implementation:

- Converts output to hardware channels.
- Adds 50 ms of digital zero before and 50 ms after the supplied programme.
- Starts `arecord` first.
- Sleeps 0.30 s to let capture open/start.
- Runs `aplay` with the raw data on stdin via `subprocess.run()`.
- `aplay` uses `--buffer-time=100000` (100 ms, from default `buffer_ms=100`).
- `arecord` keeps recording for a duration calculated from programme length, padding, `tail`, and a 0.45 s margin.
- There is no explicit independent measurement of when the final DAC sample physically emerges from the D10 output.

For the 5.15 s stimulus in the latest test, the full capture was 8.0 s, so the missing/fading signal seen in the capture is not explained by `arecord` simply stopping at the end of the intended test programme.

## 4. Clock findings

Clock preflight measurements have been repeatable:

- Earlier runs: approximately −24 to −26 ppm.
- Latest run: L −23.4848 ppm, R −23.4814 ppm.
- L/R spread is essentially zero.
- The ±100 ppm rate-mismatch preflight passes.
- Local carrier SNR is about 36.3 dB, so the ≥20 dB signal check passes.
- The analyzer's coherent-window requirement is ±0.002 bin; measured offsets are around −0.0235 to −0.0248 bin, so **coherence fails**.

This clock mismatch is real and must be accounted for in THD analysis, but its magnitude is far too small to explain a few tenths of a second of missing signal. Do not conflate the ppm clock offset with the apparent programme timing / tail issue.

The analyzer reports harmonics as not detected above its credibility threshold. The current results are **not validated THD numbers**. THD+N is a residual measurement and remains affected by actual input noise / spurs and by the exact analysis window.

## 5. Diagnostic instrumentation already added

Branch: `diagnostics/d10-full-capture`.

The diagnostic runner saves:
- `stimulus_thd.wav`: exact digital programme passed by the wrapper to `AlsaCli.play_record()`;
- `capture_full_thd.wav`: full arecord output before onset/window extraction;
- `analysis_window.wav`: exact data passed into THD analysis;
- `metadata.json`: devices, sample rate, settings, detected onset, settle, analysis window frame bounds, lengths;
- `report.txt`: analyzer report;
- ZIP containing the above.

Summarizer: `verification/summarize_thd_archive.py`; prints metadata plus per-window least-squares fitted fundamental, total RMS, and residual RMS.

Recent commits on this diagnostic branch (the local pull in the latest user run fast-forwarded to abbreviated SHA `3de555d`):
- `3067a028fc130b21d173c79ecc53a316c26b6722` — continuous tone-tail option in diagnostic runner.
- `3de555df953251012ba80fd16b98ce75bbcf3a33` — summarizer prints tone-postroll metadata.
- Earlier diagnostic additions were already present before these commits.

No production backend/measurement implementation was changed as part of these diagnostic additions. **Do not change PR #4 while working on this hardware diagnosis.** Keep experimental work on this diagnostic branch or another explicitly named diagnostic branch.

## 6. Runs completed and what they show

All runs were direct D10 → E-MU loopback at 192 kHz, −30 dBFS; no DUT was connected.

### A. 2 blocks, no extra DAC padding

- Programme length: 2.55 s.
- Detected onset: 0.775 s.
- Analysis window: 1.275–3.275 s.
- Captured signal was stable for most of the window but tapered / disappeared near its end.
- THD+N approximately 9.39% L / 9.37% R.
- Harmonics were not credible.

### B. 4 blocks, no extra DAC padding

- Programme length: 4.55 s.
- Detected onset: 0.775 s.
- Analysis window: 1.275–5.275 s.
- Full capture showed the signal becoming stable after its start transition and fading near the end; the analysis window included a tail transition.
- THD+N approximately 6.31% L / 6.31% R.
- Harmonics were not credible.

### C. 4 blocks, `--dac-postroll 0.6` (0.6 s of digital silence after the programme)

- Original signal still 4.55 s; an additional 0.6 s of zeros was appended within the same aplay stream.
- Onset remained 0.775 s; analysis window remained 1.275–5.275 s.
- Captured signal still faded around the same point; the zero postroll did not make the tone last longer.
- THD+N approximately 6.38% L / 6.34% R.
- This did **not** support the simple theory that 0.6 s of trailing digital silence would drain a DAC queue and restore the missing tone. It does not, by itself, identify the actual cause.

### D. 4 blocks, `--tone-postroll 0.6` (latest run)

Command used:

```bash
/usr/bin/python3 verification/diagnose_thd_full_capture.py \
  --blocks 4 \
  --tone-postroll 0.6 \
  --out-dir measurements/diagnostics/d10-emu-thd-tone-tail-06

/usr/bin/python3 verification/summarize_thd_archive.py \
  measurements/diagnostics/d10-emu-thd-tone-tail-06.zip \
  --step 0.5
```

Metadata:
- Test programme before the extension: 4.55 s.
- Continuous tone extension: 0.60 s.
- Total digital programme: 5.15 s.
- Captured full recording: 8.0 s.
- Detected onset: 0.775 s.
- Analysis window: frame 244800 through 1012800 = 1.275–5.275 s, exactly 4.0 s.
- Clock preflight: about −23.48 ppm, L/R agree; coherent-window test fails at about −0.0238 bin.

Analyzer:
- THD was **not detected above credibility threshold** on either channel.
- THD+N: **1.03801% L**, **0.12622% R**.
- Input levels reported: −37.16 dBFS L, −36.79 dBFS R.
- No backend XRUN note or clipping warning was reported.

Summarizer:
- `analysis_window.wav` shows the fitted tone near −40.2 dBFS RMS L and −39.8 dBFS RMS R in every 0.5 s chunk, including the final chunk.
- The final 0.5 s chunk has a higher residual (about −74.6 dBFS L / −84.0 dBFS R) than the earlier stable chunks (roughly −78 / −84 dBFS).
- In the full capture, the tone is stable across most of the programme and disappears after the late transition around the 5.5–6.0 s region (summarizer resolution is only 0.5 s in this run).
- The added tone extended the captured stable programme and reduced the gross THD+N error. This is evidence that the earlier analysis windows were contaminated by the end transition / tone loss.
- However, the available 0.5 s summaries are not precise enough to establish the exact captured first/last sample or whether the DAC loses a fixed tail interval. Do not claim a fixed 0.3–0.4 s hardware truncation as proven. It remains a hypothesis requiring sample-level measurement.
- The L residual is much worse than R. The 300 Hz line in this run is reported at −53.6 dBc L versus −72.7 dBc R. This may be a channel-specific spur / noise issue, but it has not been isolated yet.

Archive and source data remain on the user's machine:
`measurements/diagnostics/d10-emu-thd-tone-tail-06.zip`
`measurements/diagnostics/d10-emu-thd-tone-tail-06/`

## 7. Hypotheses: status

1. **Analysis window includes end fade / missing tone in previous runs — supported.** The full-capture and analysis-window WAV summaries show this in earlier runs. Extending the tone by 0.6 s substantially lowers THD+N and keeps the test tone in the selected 4 s window.
2. **Appending 0.6 s of digital silence drains the output queue and fixes the tail — not supported by run C.** The captured tone did not become longer in that test.
3. **The DAC/ALSA playback path drops a fixed amount of the end of the stream when aplay exits — plausible but unproven.** Run D is not summarized with enough temporal precision to quantify the exact first/last tone sample. It needs direct waveform alignment / length measurement.
4. **Onset detector is placing the analysis window too late — possible but not established.** The saved onset (0.775 s) is detector-defined, not an external timing reference. Need compare the captured waveform against a known digital stimulus with unambiguous start/end markers.
5. **Clock mismatch causes the multi-percent THD+N — disproved as an explanation of the timing issue.** Approximately 24 ppm is too small to account for tenths of a second. It does explain why the coherent-window test fails and must still be handled by the measurement method.
6. **The earlier assistant's "double settle" theory — wrong.** See code facts above.
7. **The D10→E-MU input path has an L/R noise asymmetry / spur — observed, not yet diagnosed.** In the latest run L's residual/noise is materially higher than R's; investigate only after the playback timing is independently characterized.

## 8. Recommended task for the hardware agent

The agent has access to the actual D10 and E-MU. Ask it to independently characterize the raw ALSA path before changing ValveBench's production measurement code.

### Objective

Determine, with sample-level evidence, whether:
- all samples submitted to `aplay` emerge from the D10 output;
- there is a fixed start latency and/or fixed tail truncation;
- the captured tone duration equals the submitted duration;
- the issue is affected by ALSA buffer settings or process close/drain;
- E-MU L/R have a separate noise / spur difference.

### Required procedure

1. Re-identify physical cards / PCM endpoints at the time of test. Confirm D10 playback-only and E-MU capture-only; inspect `/proc/asound/cardN/stream0`.
2. Use only the direct D10 → E-MU cable, no DUT. Use a safe level around −30 dBFS.
3. Build a deterministic test WAV whose envelope makes alignment unambiguous: leading silence, a distinctive start marker (e.g. a short tone burst or coded amplitude pattern), a steady 997 Hz tone, a distinctive end marker, and trailing silence. Ensure the digital file itself is inspected and its exact frame counts recorded.
4. Start E-MU capture before D10 playback. Capture sufficiently longer than the whole test (at least 2–3 s of extra recording). Do not rely on the THD onset detector to find the sample-level start.
5. Compare the submitted WAV and captured signal with cross-correlation / marker detection. Report:
   - exact source start/end frame numbers;
   - exact captured start/end frame numbers, independently for L/R;
   - start latency;
   - effective captured tone duration;
   - missing/extra samples at the end;
   - amplitude, RMS, noise and obvious spurs during stable sections.
6. Repeat with at least 1 s, 2 s, 5 s and 10 s steady-tone duration. This distinguishes a fixed time loss from clock drift or a duration-proportional error.
7. Compare reasonable playback ALSA buffer configurations (for example `--buffer-time=50000`, `100000`, `200000`, and `400000` µs), recording the actual command, `aplay -v` output, exit status, stderr, elapsed time and relevant `/proc/asound/.../hw_params` while streams are open. Do not make persistent system/driver changes without permission.
8. Test whether a longer *in-stream* trailing silence (e.g. 0.5 s and 1.5 s) changes the captured end. Contrast this with extending the tone itself. If useful, compare `aplay` with a tiny direct ALSA/Python playback test that explicitly drains the PCM before close.
9. Check E-MU left/right noise and the 300 Hz spur independently with a quiet capture and a stable tone. Do not infer a hardware defect from the one summary alone.
10. Report evidence and a minimal reproduction. Do not modify PR #4. Do not merge experimental code. No need to upload large WAVs unless the precise numeric report cannot establish the finding.

### Acceptance criteria for a proposed fix

- The captured start/end markers and stable tone duration are explained by a reproducible model.
- A four-second analysis window can be proven to lie entirely inside the stable received tone, with margin at both ends.
- Direct-loopback repeat runs produce stable residual/THD+N results, and any remaining L/R noise asymmetry is separately quantified.
- Clock/coherence handling is addressed explicitly; do not call a non-credible harmonic bound a measured THD value.
- The fix has regression coverage using an appropriate simulated / recorded test and does not regress unrelated Gain Match behavior.

## 9. Local reproduction / files

Project root used by the user:
`/opt/MyApps/Adudio/ValveBench-git`

Diagnostic branch:
`diagnostics/d10-full-capture`

Latest diagnostic run directory:
`measurements/diagnostics/d10-emu-thd-tone-tail-06`

Useful commands (after checking current ALSA card numbering):

```bash
./run.sh cli cards

/usr/bin/python3 verification/diagnose_thd_full_capture.py \
  --blocks 4 \
  --tone-postroll 0.6 \
  --out-dir measurements/diagnostics/d10-emu-thd-tone-tail-06

/usr/bin/python3 verification/summarize_thd_archive.py \
  measurements/diagnostics/d10-emu-thd-tone-tail-06.zip \
  --step 0.1
```

The runner refuses to overwrite an existing output directory; use a new `--out-dir` for every run.

## 10. Explicit cautions for the next context / agent

- Do not ask the user to guess signal levels. The direct-loopback diagnostic defaults and current settings are already known.
- Do not repeat the incorrect "double settle" explanation.
- Do not claim the −24 ppm clock mismatch explains the missing fraction of a second.
- Do not interpret the current THD+N readings as verified THD. Current harmonic detections fail the credibility gate and the coherent-window check fails.
- Do not modify PR #4 during this investigation.
- Keep all outputs in the project directory, never in `~`.
- Use the actual attached hardware; do not settle the question by simulation alone.
