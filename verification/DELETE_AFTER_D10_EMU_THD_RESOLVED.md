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


## 11. Review of the hardware-agent report and `timing_probe.py` (12 Nov 2026)

**Do not accept the report's sample-accurate timing conclusions or implement its proposed `Rig.run()` change yet.** Inspection of the committed probe found frame-index bookkeeping and detection problems. The physical observations (notably L/R output-noise asymmetry and a repeatable-looking residual) are useful leads, but the current code does not prove the stated 0.747–0.756 s latency or +0.425 s tone extension.

### 11.1 Search-window offsets are added incorrectly

`first_above()` returns an index relative to the array slice it receives. In `run_case()`:

- Start marker is searched in `e65[int(0.7*fs):int(2.2*fs)]`, but the returned local index is converted using `+ int(0.9*fs)`. Correct slice-origin conversion would start with `+ int(0.7*fs)`.
- Tone start is searched in `e997[int(0.7*fs):int(3.0*fs)]`, but uses `+ int(1.0*fs)` instead of `+ int(0.7*fs)`.
- End marker is searched in `e8[int(1.5*fs):int(submitted_frames*0.95)]`, but uses `+ int(2.0*fs)` instead of `+ int(1.5*fs)`.

For the first committed 2 s / 50 ms case, values printed into JSON are:
- start marker = 336511;
- tone first = 355711;
- end marker = 394111.

Removing only the excess slice-origin offsets gives:
- 336511 − 0.2·192000 = 298111;
- 355711 − 0.3·192000 = 298111;
- 394111 − 0.5·192000 = 298111.

All three detections map to exactly the same captured frame. That cannot represent the programmed start marker, tone start (210 ms later), and end marker (2.36 s after start marker) as distinct events. The result indicates false triggering / marker cross-detection and invalid frame labels, not sample-accurate detection.

The `latency_start_s` calculation also compares a capture-relative index with a programme-relative marker frame without accounting for (a) the 0.5 s `arecord` head start, (b) the 50 ms pad prepended before the programme, and (c) the erroneous +0.2 s slice offset. Thus the reported ~0.75 s is not a valid physical DAC latency measurement. The actual capture start time must be explicitly accounted for.

### 11.2 End-marker detection is not reliable

The probe uses an 8 kHz end marker while the source is a 997 Hz tone. The report itself notes that 8 kHz is too close to the tone's 8th harmonic (7976 Hz), and end-marker detection is poor. The committed JSON's end-marker coordinate also maps to the same frame as the start marker after correcting the slice origin. Do not use this end marker as evidence for the end of the tone. Replace it with a distinct, validated marker (for example a coded burst/chirp in a separate frequency band) and verify it is detected only at its known location.

### 11.3 Tone-duration threshold is not an independent marker measurement

The probe infers start/end from `last_above()` / `first_above()` on a 5 ms moving-demodulation envelope and a threshold derived from the minimum of candidate "quiet" windows. It must be validated against the marker locations and against raw waveform excerpts. In particular, the first threshold crossing may be leakage/transient at the 6.5 kHz start marker; the last crossing may reflect residual tone, an end transient, or threshold bias. Do not equate this with proof that every DAC sample is played.

The 400 ms buffer case already demonstrates channel-specific false detection: L reports 2.7457 s while R reports 2.425 s, and the report attributes this to an L spur. That is a failure of the detector for that case, not a measured duration difference.

### 11.4 Committed evidence conflicts with report text

The committed `measurements/diagnostics/timing-postroll.json` for `post_silence_s=0.6` reports:
- captured tone duration 2.425 s on both channels;
- `tone_last_frame` 821072 (L) / 821071 (R).

The baseline 50 ms buffer case in `timing-probe-192000.json` reports 2.425 s and `tone_last_frame` 821313 (both channels). These committed values do **not** show the report's claimed +1.066 s / +0.64 s extension after appending 0.6 s silence. The source report may reflect an uncommitted run, but the discrepancy must be reconciled by preserving the exact raw log/JSON and waveform from that run.

Also, the committed `timing-probe-192000.json` contains three 2 s cases (buffers 50/200/400 ms), not the stated 1/2/5/10 s duration series. The series must be committed as data or the report must identify the separate artifact that contains it.

### 11.5 Required correction before deciding on the production fix

Ask the hardware agent to:

1. Correct every sliced-array index conversion by adding the actual slice start; save both local and absolute indices in JSON so this cannot recur.
2. Record the 0.5 s capture pre-roll and 50 ms playback pre-pad separately; compute expected event positions in the capture timebase explicitly. Do not call a difference "DAC latency" until these offsets and the arecord start semantics are accounted for.
3. Replace the 8 kHz end marker with a robust, distinct coded marker. Test marker detection on the recorded raw signal and demonstrate there are no false detections in silence, the start marker, or the tone.
4. Save raw capture WAVE/PCM for at least one run and produce a small numeric excerpt / envelope table around each expected marker. The report must show separate, correctly ordered start-marker, tone-start, tone-end, and end-marker indices.
5. Repeat a short series (1, 2, 5, 10 s) and in-stream silence (0, 0.6, 1.5 s), with exact command lines and per-run output artifacts committed.
6. Explain the observed L/R residual / 300 Hz line separately; avoid assigning it to D10 firmware until E-MU-only and D10-only control measurements are documented.

Only after those points are resolved should the team choose between a robust marker/matched-filter onset locator, an improved onset detector, or a backend transport/drain change. A cross-correlation POC may be developed on the diagnostic branch, but must not be applied to production `Rig.run()` until it has regression tests for tone-only, multi-segment, clock-drift, click/noise, and DUT-response cases.

**Decision at this review:** no approval yet for the production `Rig.run()` cross-correlation fix. Request corrected sample-level evidence first. PR #4 remains untouched.


## 12. Review of agent v2 report and probe (12 Nov 2026)

The v2 work is a substantial correction. The v1 claims of +0.425 s tone extension, digital-silence substitution, and 0.75 s DAC latency are explicitly withdrawn by the agent and must remain withdrawn. The v2 data supports a new working explanation: the old `Rig.run()` onset detector can trigger on an early capture click / pre-tone residue, while the actual 997 Hz programme tone begins much later. The 1/2/5/10 s marker-B positions are close to their expected positions; trailing-silence tests show post-tone RMS around −79 to −81 dBFS. This is strong evidence against the earlier fixed truncation/substitution theory.

**However, do not implement the proposed production change yet. The v2 marker detector's own JSON is internally inconsistent and needs one more correction.**

### 12.1 Marker peak score contradicts the claimed correlation

In the v2 JSON files, `marker_A_scores` and `marker_B_scores` (the scores attached to the positions actually stored in `*_peaks_abs`) are approximately 0.46–0.49, while `marker_A_corr_max` / `marker_B_corr_max` are approximately 0.994–1.000.

Examples from `timing-v2-baseline-2s.json`:
- Marker A stored at frame 298015 with stored peak score 0.493, but `marker_A_corr_max` is 0.998.
- Marker B stored at frame 726182 with stored peak score 0.466, but `marker_B_corr_max` is 0.998.

The report quotes the near-1.0 `corr_max` as the marker confidence, while the stored detected event is associated with a score around 0.5. Those are not interchangeable. `fft_xcorr_peaks()` groups threshold crossings and then skips `0.1*fs` after each group; it may skip a stronger local maximum within that interval. The JSON also says the function writes all peaks, but this skip means it does not enumerate all local maxima.

Required correction:
- For each marker template, compute and retain the actual global maximum index and score, not just `cc.max()`.
- Enumerate local maxima with a principled minimum separation (at least template duration, with explicit rationale), and do not skip an interval before checking whether it contains a stronger maximum.
- Store every candidate's absolute frame and normalized correlation score.
- Select the event only if the selected peak's own score passes a stated threshold; report the selected peak score beside its selected frame.
- On the saved raw capture, print a compact candidate table around the expected A/B positions and show that the selected maximum is at the correct marker, not merely that some other location in the file has a high correlation.
- Explain the roughly 2:1 difference between stored peak scores and `corr_max`.

Until this is resolved, the claimed marker correlations of 0.996/0.998 and the sample-accurate anchor positions are not fully validated.

### 12.2 Tone-envelope first/last crossings are known to be unreliable

The v2 JSON itself shows `tone_first_abs` occurring before the expected tone start (for example, 2 s case: ~318k versus expected ~342k), and `tone_last_abs` extending far beyond the expected tone end, sometimes into the capture tail. The report acknowledges false triggering on marker A and does not use these values as final proof. Keep them explicitly labelled diagnostic-only; do not use `tone_last_abs` / `tone_last_tail_s_*` as measured tone duration.

The strongest end-of-tone evidence currently comes from the expected programme position plus the independently detected marker B, and from the post-tone RMS in the trailing-silence cases. That is promising, but it depends on validating marker B detection as above.

### 12.3 L/R control is not yet an E-MU-only isolation

`controls-emu-only.txt` says “E-MU-only (D10 idle)”, but the v2 report clarifies that D10 remained physically connected. Therefore it is an idle-D10 connected-path measurement, not an isolated E-MU-only control. The source of the L/R residual asymmetry remains unknown. Required next control: physically disconnect the D10-to-E-MU cable at the E-MU inputs, keep E-MU gain settings documented and unchanged, then repeat capture-only noise measurement. Do not attribute the asymmetry to D10 or E-MU until this is done.

### 12.4 Current decision and next agent task

The v2 evidence makes the stale-onset / pre-tone-window contamination explanation the leading hypothesis for the original 6–9% THD+N. The v1 fixed-tail-loss explanation is withdrawn. The v2 marker-B alignment and trailing-silence results are promising, but marker correlation bookkeeping still needs validation.

Ask the hardware agent for a narrow v2.1 correction:
1. Fix peak enumeration / score association as described in §12.1, using the already saved raw baseline capture first (no new hardware run until offline detection is validated).
2. Show selected A and B peak frames, their own normalized scores, and all competing local maxima; verify the correct ordering and frame separation against the known stimulus.
3. Recompute the 1/2/5/10 s and trailing-silence conclusions from the corrected detector. Preserve the existing v2 artifacts; add v2.1 outputs rather than overwriting them.
4. Only if offline validation passes, run one short physical repeat (2 s tone + 0.6 s trailing silence) to verify reproducibility.
5. Separately perform the physical E-MU-only control by disconnecting the D10 cable, with gain settings recorded. This is a distinct question and must not block the timing conclusion.

Do not change production `Rig.run()`, `measure.py`, `backend.py`, or PR #4 yet. After v2.1 marker validation, the coordinator can decide whether to implement a guarded marker/matched-filter locator on the diagnostic branch and build regression tests before touching production code.


## 13. Coordinator review of v2.4 and required v2.5 validation (12 Nov 2026)

Commit reviewed: `82cb9f4` (`diagnostics/d10-full-capture`). No production code or PR #4 changes are approved.

### 13.1 What v2.4 establishes — and what it does not

The corrected leading-silence experiment is promising: with 1.5 s of leading digital zeroes before the 4.55 s `Rig._seg` tone, four captures (A,A,B,B) show a 4.5413–4.5414 s interval above the script's −45 dBFS threshold. The final 0.3 s of the measured plateau remains at −36.8 dBFS. Adding 0.6 s of playback silence after the tone lowers the post-tone residual from about −74.2 dBFS to −81.3 dBFS. This is useful evidence for a leading-silence workaround and for a quiet trailing segment.

It is **not yet a sample-aligned proof of the mechanism**. The leading-silence script has no unique start/end markers, does not quantify the full pre-tone 1.5 s capture interval, and does not preserve source/PCM/command/stderr metadata alongside its summary in the repository. Its two A runs followed by two B runs are also not interleaved. Do not state that the D10 loses exactly 0.28 s at stream start and 0.2 s at stream end: the current measurements do not independently identify those two intervals.

The no-leading-silence A/B (4.0646 s plateau) was contaminated by the D10 replaying previous 997 Hz content. The script's first/last-threshold plateau detector therefore cannot distinguish the current programme from stale output. The agent correctly withdrew those A/B values as a valid loss measurement; do not use them to quantify current-stream truncation.

### 13.2 Independent code defects in the discarded v2.4 matrix

The matrix is invalid for additional reasons beyond the stale-output contamination:

- `tone_s` is never used to construct the programme. `prog` is regenerated by `rig._seg(...)` with its default 4.55 s tone for every row, including rows labelled 2.0 s.
- `buf` is only written into the result row. The single `AlsaCli` instance is created once with default `buffer_ms=100`; the per-row values 200 and 400 are never passed to a backend instance or otherwise applied.

Thus the matrix did not test the claimed tone-duration or buffer-time combinations. It must remain labelled invalid, and no conclusion about buffer-time independence may cite it.

### 13.3 A/B logging defects

In `verification/v24_ab_endofstream.py`, the manually written arecord command uses:
`int(len(arr)/FS + 0.1 + 0.45 + 1)`.
It omits `AlsaCli.play_record(tail=0.8)` and does not reproduce the backend's actual calculation:
`duration=(len(out)+2*pad_n)/fs + tail + 0.45`,
`record_seconds=ceil(duration)+1`.
For A (4.55 s payload) the backend captures 7 s; for B (5.15 s) it captures 8 s, while the saved command files say `-d 6`. The command log is therefore wrong. Also, the script does not save the actual stderr from either ALSA process; a successful return from `play_record` establishes that it did not raise, not the contents of both stderr streams.

The first A/B commands use playback `hw:2,0` and capture `hw:3,0`, whereas §2 lists the opposite card numbers. Card numbers can legitimately change, and the script does look devices up by USB VID:PID, so this is not itself evidence of a misroute. Every run must save the resolved mapping (USB ID -> ALSA card -> PCM) explicitly.

### 13.4 Required v2.5 task — marker-anchored AlsaCli test

Use the real `AlsaCli.play_record()`; no production changes, no PR #4 changes, no device/driver configuration changes.

1. Re-identify D10 and E-MU by USB VID:PID at run time. Save the resolved mapping and `/proc/asound/cardN/stream0` evidence. Never rely on the old card numbers.
2. Build one deterministic programme with exact frame positions:
   - configurable leading silence (control 0 s; treatment 1.5 s);
   - unique chirp A (6.5–7.5 kHz, 80 ms);
   - 150 ms gap;
   - 4.55 s, 997 Hz, −30 dBFS tone with the same 10 ms fades as `Rig._seg`;
   - unique chirp B (10.5–11.5 kHz, 80 ms);
   - configurable trailing silence (0 or 0.6 s).
   Use the same templates and validated v2.1 matched-filter detector. Record exact source frame indices for A, tone start/end, B and the total programme.
3. To reproduce the production sequence fairly, apply the same precondition before every trial: one identical 1 kHz clock-tone playback (matching the preceding clock preflight), followed by the same fixed idle interval. Do not allow one trial to inherit a 997 Hz programme from the previous trial. Keep the precondition identical for all conditions.
4. Run the 2×2 conditions (lead 0/1.5 s × trailing 0/0.6 s) in interleaved order (e.g. A-B-B-A, then repeat). At least two valid repeats per condition. If a condition fails marker validation, do not count it.
5. Detect both chirps independently in L/R and report each absolute frame, local/global score, expected frame and residual. Confirm B arrives after the expected tone end. Plot/emit the 997 Hz 1 ms demodulation envelope across the entire capture, including all leading silence and at least 0.3 s after B. Report the first stable tone frame, last stable tone frame and levels at −45/−55/−65 dBFS using a stated persistence rule. The pre-tone silence must be checked for stale 997 Hz content.
6. After each run, preserve the exact source array, PCM byte stream after the backend's standard 50 ms pads and channel mapping, complete capture, per-run JSON and command log. Log actual `fmt_out`, `fmt_in`, channels, `buffer_ms`, maps, USB mapping, capture length, calculated `record_seconds`, elapsed times, return codes and actual stderr. Do not manually reconstruct the arecord duration; instrument/observe the actual backend command. If actual stderr cannot be captured without altering production code, state that limitation instead of asserting its text.
7. Fix the test logger so its recorded arecord duration matches the exact backend implementation. Do not use the invalid v2.4 matrix as a buffer sweep. If buffer time is retested, instantiate a new backend with that buffer value and log the actual generated command.
8. As a diagnostic-only check, replay the resulting captures through the unchanged production `Rig._onset` and `Rig._tone_an`; report the exact full analysis blocks and verify the configured four-second window lies entirely inside received stable tone. Do not shorten windows silently.
9. Update the report: mark the matrix invalid for both code reasons above; distinguish the promising leading-silence workaround from a proven transport mechanism; retract the unsupported “0.28 s start + 0.2 s finish” split unless marker positions independently measure it.

### 13.5 Decision gate

A 1.5 s leading-silence workaround may be proposed for a production patch only after the marker-anchored 2×2 AlsaCli test repeats successfully, the pre-tone interval is demonstrably quiet, and the production four-second window is fully inside the received stable tone with margin. The 0.6 s trailing silence may be retained to make the post-stream state quiet, but it is not currently the fix for the missing/late programme content.

No production patch is approved yet. Keep the work on `diagnostics/d10-full-capture`. The existing onset detector and THD analysis remain unchanged. PR #4 remains untouched.


## 14. Coordinator review of v2.5 and required v2.6 (13 Nov 2026)

Reviewed commit `ced046b`, `verification/v25_marker_anchored.py`, all eight per-run JSON files, summary, device mapping, report appendix, and committed stderr files. No production changes or PR #4 changes are approved.

### 14.1 What is now supported

- The physical mapping is internally consistent for these runs: D10 USB `152a:85dd` = card 2 / `hw:2,0`; E-MU USB `041e:3f04` = card 3 / `hw:3,0`. The observed aplay/arecord commands target the corresponding endpoints.
- All four lead=1.5 s runs have strong A and B marker detections (scores about 0.993–0.9998); B follows A by the programmed interval within the expected small clock/start jitter.
- Before the tone, the measured residual is around −80 to −81.6 dBFS in those lead=1.5 s runs.
- In all four lead=1.5 s runs, the unchanged production onset plus 0.5 s settle selects a four-second / four-full-block window which is inside the tone interval derived from the valid A anchor and known programme frame positions. This is the strongest result in v2.5 and makes the 1.5 s leading-silence workaround promising.
- Lead=0 runs repeatedly fail to detect chirp A (scores about 0.009–0.013) while chirp B is detected strongly. The pre-tone 997-Hz demod value is high (about −36.7 dBFS), consistent with residual preflight content, but the present frequency analysis does not yet identify whether that content is the old 1 kHz preflight or the new 997 Hz test tone.

### 14.2 Important correction: 0.354–0.369 s is not a measured head truncation

For lead=0, the script calculates `start_offset_s` from the global maximum of marker A even when marker A's score is about 0.01 and the maximum is just noise. All derived `expected_abs`, `start_offset_s`, `marker_B_residual_ms`, `tone_first`, and `window_inside_tone` fields in those lead=0 JSON records are therefore invalid.

The report's claimed “D10 begins rendering at programme position 0.354–0.369 s” is also not established. It is obtained by subtracting the programme marker-B position from the absolute capture time of B while omitting the 50 ms backend pad and the capture-to-program start offset. Numerically it is approximately the normal program start offset (~0.304–0.313 s) plus the 50 ms pad. It is not a measurement of discarded source frames. The report's exact “first 0.354–0.369 s of stream was lost” conclusion is withdrawn pending v2.6.

The correct way to reconstruct the lead=0 timeline offline is to use the **valid marker B** as the anchor. Given the exact source-frame separation between B and A/tone, derive the expected A and tone frames backwards from B. Do not use the invalid global A noise maximum as a timing anchor. At the expected A frame, evaluate the normalized correlation score locally (not only the global maximum over the whole recording).

### 14.3 Frequency discrimination is currently inadequate for lead=0

Two aspects of the current analysis cannot distinguish the previous 1 kHz preflight tone from the 997 Hz test tone:

- `demod_env(..., 997, win_s=0.001)` uses a 1 ms window. A 3 Hz difference between 997 and 1000 Hz is far too small to reject the 1 kHz tone over such a short window.
- The chunk-FFT masks overlap: `991 < f < 999` and `996 < f < 1004` share bins 996–999 Hz. The FFT magnitude is also not normalized to a meaningful dBFS value.

Therefore `pre_tone_max_env997_db = -36.7` cannot by itself be identified as residual 1 kHz rather than current 997 Hz. Use sufficiently long windows and a frequency-selective method that explicitly separates both tones (e.g. joint least-squares sinusoid fitting at the measured 997-Hz and 1-kHz frequencies, with measured clock ppm applied; or a non-overlapping, suitably high-resolution spectral estimate). Report fitted amplitudes in dBFS per window and show the frequency-fit residual.

### 14.4 Other v2.5 script / artifact defects to correct

1. `PRE_TONE = 1.0` is unused. The preflight is created with `Rig._seg` for a Rig configured with four one-second blocks and 0.4 s settle; its actual programme length is 4.45 s, not 1.0 s. State and log the real preflight duration and frame count. Keep it identical across conditions.
2. Repeat labels are not per-condition repeats: `r{1 if idx < 4 else 2}` labels both occurrences in each A-B-B-A half identically. Use a per-condition counter.
3. WAV/PCM filenames are based on `cond`, not the unique run index. The repeated occurrences of a condition overwrite each other's `v25-stimulus-*.wav`, `v25-capture-*.wav`, and `v25-pcm-*.bin`. The stderr filenames are similarly reused by each new observer instance. Preserve every run under a unique run-indexed filename/directory.
4. The report says stderr and commands are saved in observer JSON. Confirm that each per-run JSON refers to that run's unique stderr file, not a shared filename later overwritten. Empty stderr is a valid result, but the saved logs must remain per-run.
5. The 1.5 s idle is added after `be.play_record(preflight)`. The real `play_record` itself waits for `arecord` to finish after aplay ends, so this introduces an additional 1.5 s beyond the production sequence. Log actual times from aplay start/end to the next aplay start; compare the test cadence with the real clock-check → THD sequence before assuming the workaround transfers unchanged.

### 14.5 Required v2.6 — offline reconstruction first, minimal repeat only if raw files were overwritten

Do not change production code or PR #4. Do not perform a new hardware run unless needed because repeated raw captures were overwritten.

1. From each lead=0 JSON, use the high-confidence marker B frame and the exact programme positions to derive:
   - expected marker A frame;
   - expected tone start and end frames;
   - expected analysis-window frame bounds.
   Use `offset_from_B = detected_B_abs - (marker_B_source_frame + PAD)` only as the capture/program alignment estimate; do not call it lost head. Cross-check this offset against the adjacent lead=1.5 run(s) and state the residual/jitter.
2. Compute the normalized correlation at the exact expected A frame (and a small justified search tolerance around it). Report the local score and the nearest competing peak. Confirm whether A really is absent at the programme-expected position.
3. With B as the anchor, analyze the lead=0 997-Hz tone interval. Separate actual 997-Hz signal from residual 1-kHz preflight using long-window joint sinusoidal least squares / equivalent non-overlapping method at measured frequencies. Evaluate the start, stable middle, final 1 s and post-tone interval. Report per-window amplitudes and residuals in dBFS; apply the measured clock ppm.
4. Re-evaluate the actual production window from the valid B-derived timeline. Report whether it intersects old preflight, stable 997-Hz signal, the terminal fade, or post-tone content. Do not use any lead=0 `window_inside_tone` flag from the existing JSON; it was derived from the failed A anchor.
5. For lead=1.5, retain the already-valid A/B timing and four-block-window result, but report exact per-run margins in frames/ms from window end to measured tone end.
6. Correct preflight duration metadata, repeat labels, unique artifact paths, and per-run stderr references. Produce a script that reproduces the full summary offline from the saved unique raw captures. Identify which repeated raw captures were overwritten by v2.5; do not imply eight raw captures exist if only four were retained.
7. If not all necessary lead=0 raw captures remain, perform only the missing lead=0/lead=1.5 marker-anchored runs with the exact production cadence (clock preflight then THD test; no extra 1.5 s idle unless that delay is also in the real application). Keep the preflight waveform and duration identical to the real clock check. Use interleaved conditions and unique output names.
8. Update the report: preserve the valid lead=1.5 finding; withdraw the unsupported 0.354–0.369 s head-loss claim; explicitly distinguish “marker A absent” from “0.36 s of source frames dropped”.

### 14.6 Decision gate

Current decision: **1.5 s leading silence is a promising workaround and has passed four marker-valid captures, but production approval is withheld until v2.6 confirms what happens to the 997-Hz signal and the real application cadence is reproduced.** The present evidence does not justify a claim that exactly 0.354–0.369 s of source frames are discarded.

If v2.6 confirms that a 1.5 s leading silence reliably clears stale preflight content and the full production analysis window remains inside stable 997-Hz tone using the actual app sequence, a minimal guarded production patch can be proposed:
- prepend the verified leading-silence preamble to the measurement playback programme (not blindly to unrelated backend playback);
- optionally append a verified 0.6 s zero tail for a quiet post-stream state;
- explicitly reject an analysis result when the requested number of full stable analysis blocks is unavailable; never silently shorten the window.

The L/R residual asymmetry is still a separate open issue. The D10 cable has not been physically disconnected from the E-MU inputs, so its source is not isolated. Do not delay the timing decision on that separate control, but do not attribute the asymmetry to either device.

## 15. Coordinator review of clock estimator audit and v2.18 reproduction (14 Nov 2026)

### 15.1 Revisions and evidence

- Auditor target: code revision `02e9591d807a73e4d1229a1377be1137c79df36c` (`coordinator/v217-clock-estimator-stability`).
- Agent reproduction: `0b6853be8dc7fc4e9d4d6f60472328ff3f97e232` (`agent/v218-clock-estimator-independent-reproduction`).
- v2.18 is diagnostic-only: five Python scripts and JSON/report artifacts were added; production files were not changed; no playback or hardware runs were performed.
- The full auditor report is now preserved at `measurements/diagnostics/v218-clock-estimator-independent-reproduction/AUDITOR-REPORT-v218.md`. The v2.18 agent did not receive this document and therefore reproduced the coordinator's summary rather than the auditor's exact 42-window/PSD/seed procedure.

### 15.2 Findings accepted at the architectural level

- Production clock estimation uses the seven-bin spectral centroid in `dsp.analyze_tone()`. Current `clock_check` requires both channel SNR >=20 dB, |L-R ppm| <=5 ppm and |ppm| <=100; `capture_clock_ppm()` then takes an unweighted mean of L/R.
- The auditor's conclusion that the centroid is statistically inefficient at low pilot levels is supported by v2.18's real-noise replay: at -72 dBFS the reported empirical standard deviations are 5.884 ppm for production centroid L, 1.152 ppm for centroid R, 0.739 ppm for LSQ L and 0.129 ppm for LSQ R. These are conditional on the particular noise slices and the current v2.18 script limitations below; do not treat them as a final population uncertainty estimate.
- L/R errors and noise can be correlated. R is cleaner in these recordings, but is not an independent clock reference. Do not infer that a stable R value independently proves the clock.
- The v2.17 claim of a missing ~185 ms tail is withdrawn. Whole-recording narrowband analysis in v2.18 finds the tone ending around 4.30 s in all seven captures; capture #1 begins around 2.225 s instead of ~1.85 s. This supports missing head content (~0.37-0.40 s), with the tail at the normal end. The physical mechanism is not established.
- Integer output-length rounding in `resample_capture_timebase()` is a real design limitation for phase-sensitive consumers. It is used by `sweep` and `noise_response`; acceptable residual ppm must be derived separately for H1 amplitude, coherence, phase and group delay.

### 15.3 v2.18 evidence defects to correct before accepting numerical calibration

1. Report says 80 MC trials per cell; script and JSON contain 50. Correct the report or rerun with an explicitly declared count.
2. The `real_noise` Monte-Carlo does not generate 50 independent real-noise realizations. It loads six fixed two-second quiet slices, then cycles them. Since each slice length equals the requested segment, the random start offset is always zero. The 50 results are repeated conditions with varying tone phase, not 50 independent noise samples. Treat the current sigma/max statistics as conditional demonstrations only. Re-run with a documented independent-noise method (independent quiet windows or PSD/cross-PSD-generated colored noise) and preserve every seed and per-trial estimate.
3. The MC path downsamples to 48 kHz before calling `est_lsq()`, which itself downsamples by four again. The actual LSQ fit is therefore on 12 kHz, not 48 kHz as several comments/report passages imply. Make one resampling stage explicit and consistent across methods.
4. `est_lsq()` initializes from the production centroid and searches only +/-0.05 Hz. It is not an independent initialization and cannot be assumed to cover the full +/-100 ppm clock range. Test acquisition from independent coarse estimates and at least +/-100 ppm, with spurs and low SNR.
5. `sigma_pred` is passed through the generic `stats()` function that subtracts `PPM_TRUE`, producing misleading JSON fields (`bias` about 23.5). Save uncertainty statistics separately: mean/median, spread, predicted-vs-empirical ratio, and empirical coverage of 1σ/2σ intervals.
6. The profile-curvature variance formula in `est_joint()` needs a derivation review. For Gaussian residuals with objective RSS, likelihood curvature may require a factor of two relative to the current `1/RSS''` expression. The observed predicted/empirical sigma ratio (~0.16/~0.23) is close to 1/sqrt(2); do not claim calibrated uncertainty until the derivation and coverage test agree.
7. `v218_tone_bounds.py` uses the observed anomalous onset for capture #1 as the expected programme start. Consequently its JSON reports `head_loss_ms=-25` and `tail_loss_ms=400`, opposite to the narrative. Separate observed onset from expected source-programme start (~1.85 s), regenerate boundary JSON and make all head/tail fields consistent with the exact source frame schedule.
8. The resampling diagnostic reports max-minus-min residual across a sampled ppm grid as a "step". Calculate the analytical 1/padded_frames quantization step and verify transition points directly.
9. Research scripts use absolute source paths under `/opt/MyApps/Adudio/ValveBench-v214-clock-diagnostic`; selected source WAVs are not in GitHub. Make input paths configurable and provide a reproducible source-data package/manifest with hashes, or explicitly state the data dependency and provide the archive to the auditor.

### 15.4 Current decisions — no production patch yet

- Do not retain the current unweighted mean of two centroid estimates as a trusted clock correction at -72/-66 dBFS.
- Do not change to hard-coded R-only yet. Treat the next production candidate as a per-channel LSQ estimator that returns frequency/ppm, uncertainty, tone presence, SNR and window validity. Choose the most informative valid channel (not a fixed L or R channel); preserve independent QC for both channels. A weighted joint estimate is deferred until a frequency-local cross-spectral covariance model is validated.
- The 20 dB SNR and 5 ppm spread gates are not accuracy guarantees. Replace/augment them with uncertainty-based consumer-specific acceptance criteria after those criteria are derived and tested. Do not adopt the auditor's 0.3/0.1 ppm values as final requirements without consumer analysis.
- Fix tone timing as a separate workstream: determine real stable tone bounds, require the entire estimator window plus margins to lie inside the received tone, and reject short/truncated windows. Do not silently shorten analysis.
- Keep the current pilot level and safety policy. v2.14/v2.18 low-level/level-sweep evidence is direct D10->E-MU loopback without a DUT. It does not authorize higher levels through a phono preamp.
- No claim is made yet about the physical cause of the first-open start loss. Any hardware reproduction must use the exact production sequence and marker-anchored timing; do not infer dropped source frames from a weak/missing marker alone.

### 15.5 Required next stage — v2.18.1 evidence repair (diagnostics only)

Create a separate agent branch from the coordinator review branch. Do not touch production modules and do not run playback.

1. Correct trial-count documentation to match the saved data; make the actual number and effective number of independent noise records explicit.
2. Rebuild the real-noise experiment using multiple independent noise windows or a documented colored-noise/cross-PSD generator. Preserve all seeds and per-trial raw estimates. Do not call repeated reuse of six fixed slices 50 independent trials.
3. Fix LSQ decimation so the actual fit rate is stated and identical across comparisons. Make initialization independent of the centroid and test a true +/-100 ppm search domain.
4. Re-derive profile-likelihood sigma mathematically; test against Monte Carlo coverage. Correct the factor if required. Store `sigma_pred` with its own schema, not `stats()` intended for ppm estimates.
5. Separate `observed_onset` from `expected_program_start/end`; regenerate `v218-tone-bounds.json`. Assert that the index-1 expected start is around 1.85 s and expected end around 4.30 s, and that the head/tail loss fields match those boundaries.
6. Recompute resampling quantization using exact integer-length transitions and record residuals after correction for 5–7 s captures.
7. Make every script accept a source-data directory argument. Provide a manifest for the seven WAVs (SHA256, sample rate, channels, frames, level). Make the WAV package available to the coordinator/auditor; do not commit huge files without checking their total size first.
8. Update the report so each conclusion is labelled as established, provisional or unverified. Keep the auditor's original document alongside the v2.18 files.

No production fixes, PRs or hardware tests in this stage. After v2.18.1 is independently reviewed, the coordinator will implement the production estimator and tests on a dedicated coordinator branch. Hardware agent will then run the agreed direct-loopback verification; no raised pilot level through DUT is authorized.

### 15.6 Decision gate

v2.18 establishes the direction (centroid is too noisy at low levels; LSQ is the leading candidate; capture #1 loses signal head, not tail) but does **not** yet establish a calibrated `sigma_pred`, a fully independent real-noise Monte-Carlo distribution, or the cold-start mechanism. No production release or clock-gate policy change is approved until v2.18.1 closes the evidence defects and the coordinator reviews the corrected artifacts.



## 16. Coordinator review of v2.18.1 — commit 8d8b040 (14 Nov 2026)

### 16.1 Repository status

- Agent branch: `agent/v2181-clock-estimator-evidence-repair`, HEAD `8d8b040969a9a872941ecfe502975f45419d4186`.
- Parent is exactly `753dfe1ef96729bbe8808e85314e4d37372b406c`, the coordinator review base.
- Compare confirms one commit ahead, zero behind, 32 changed files. Changes are under `measurements/diagnostics/v218-clock-estimator-independent-reproduction/`; the modified `v218_estimators_real.py` is also a diagnostic script. No production modules, main, or PR #4 were changed.
- No playback or hardware work is evidenced. The agent followed the diagnostic-only boundary.

### 16.2 Decision

**v2.18.1 is not accepted as a completed evidence repair.** The report is useful and several earlier mistakes were noticed, but the committed source does not implement the model described in the report, and some result JSON is inconsistent with the committed estimator. Do not merge or use its MC/pilot conclusions to set production clock policy. No production changes are authorized.

### 16.3 Blocking defect A — MC does not run the claimed white-correlated model

In `v2181_montecarlo.py`:

1. `run_batch()` calls `synth_pair_from_spectra()`. The `white_corr_pair()` function (the only implementation matching the report's “white correlated equivalent”, −80/−96 dBFS and rho≈0.97 description) is never called. The saved 500-trial summary is therefore not evidence from the claimed white model. It is generated from the quiet-window PSD/cross-PSD model.
2. In `synth_pair_from_spectra()`, the target spectrum grid is calculated as `FS / n`, with global `FS=192000`, although synthesis is called at `fs=48000` with `n=96000`. The interpolation grid is therefore 2 Hz/bin instead of the actual 0.5 Hz/bin. The source PSD is sampled at the wrong frequencies (factor four relative to the actual synthesized frequency).
3. The Fourier amplitude scale uses `FS * n` instead of `fs * n`. The synthesizer must use the actual synthesis rate consistently.
4. `normalize_pair_to_band()` calculates its target from the integrated PSD only over 900–1100 Hz, but divides by `np.var(nL)` / `np.var(nR)`, which are full-band time-domain variances. That sets total broadband variance to the narrow-band target; it does not normalize the noise power in the 900–1100 Hz band. Use a measured band-limited variance or correct per-bin PSD scaling; do not mix narrow-band power with full-band variance.
5. `verify_synthesis()` sets `bins = arange(...)` and then treats those integer bin indices as Hz, although `df_bins=fs_syn/n` is calculated separately and never applied. Its band labels are consequently wrong. Its PSD estimate `2/n²·|FFT|²` also lacks the required sampling-rate normalization; compare one-sided PSD using the correct frequency axis and units. Bands above the 24 kHz Nyquist limit of the 48 kHz synthesizer cannot be validated by that signal.
6. The saved verification itself does not show a match in the key 900–1100 Hz band: measured gamma²=0.784, synthesized gamma²=0.971; the report must not describe this as a successful match. The 2–8 kHz and higher-band coherence also differs materially.

The report's claimed 12× sigma difference being “explained” by +13.6 dB of D10 during-tone noise is not established. It compares a production aggregate in dBFS with a quiet-window spectral-density figure in dBFS/Hz-equivalent, without demonstrating integration over the same bandwidth and identical estimator bins. In addition, the current MC's band normalization and frequency-axis defects can change the noise level seen by the estimator. Recompute apples-to-apples noise power before attributing the sigma ratio to D10.

### 16.4 Blocking defect B — pilot LSQ errors use the wrong nominal frequency

- `v218_estimators_real.py:ppm_of()` is hard-coded as `(f / 1000 - 1)·1e6`.
- `v2181_lsq_pilots.py` calls `ppm_of(f_est)` for 997, 1000 and 1013 Hz. The error must be computed relative to the tested pilot: `(f_est / pilot - 1)·1e6 - PPM_TRUE`.
- Therefore the reported LSQ biases near −3000 ppm for 997 Hz and +13000 ppm for 1013 Hz are nominal-frequency offsets, not estimator bias. The claimed Dirichlet-side-lobe failure at 1013 Hz is unsupported.
- There is an additional reproducibility mismatch: the saved 997 Hz / −30 dBFS LSQ summary corresponds to an estimated frequency around 868 Hz, while the committed `est_lsq()` searches only within ±2 Hz of 997 Hz and refines within ±0.15 Hz. That result cannot be produced by the committed function. Treat the pilot JSON as stale or generated by a different code revision until a rerun proves otherwise.
- Rerun all pilot cells with per-trial raw `f_est`, correct pilot-relative ppm, and a record of the exact script hash. Report measured mains-line power integrated across the line's spectral lobe; do not compare a dBFS/Hz bin value directly with pilot dBFS.

### 16.5 Sigma prediction — analytic correction is plausible, calibration is not yet accepted

For Gaussian residuals with known unit variance after correct whitening, the derivation `var(f̂)=2/RSS''` is correct. The code's `var_f=step²/denom` together with the caller's `sqrt(2·var_f)` applies the factor of two. This supports the analytical correction to the v2.18 formula.

However:
- the current GLS whitens only the instantaneous 2×2 L/R covariance estimated from residuals; it does not whiten the temporal/frequency colour of the noise. A scalar per-sample covariance is not the full frequency-local cross-spectral model already identified as necessary;
- the aggregate coverage calculation compares every error with the mean predicted sigma, rather than each trial's own predicted sigma. Per-trial sigma values are saved, so compute coverage as `abs(error_i) <= sigma_pred_i` and similarly for 2σ;
- the underlying MC noise model is currently defective (16.3).

Thus the factor-two derivation is accepted conditionally; “sigma_pred calibrated” is not accepted as a real-noise or production result. At most it is a provisional result within a corrected, explicitly stated simulation model.

### 16.6 Tone bounds — useful observation, schedule still provisional

The re-analysis finds capture #1 crossing the chosen threshold near 2.225 s and ending near 4.300 s, while the six comparison captures begin near 1.85–1.86 s and end near 4.30 s. This supports the observed early-capture anomaly and the absence of a comparable tail loss.

But the script defines the expected start as the median of those same six “normal” received onsets, fixes the tone duration at 2.45 s, and hard-codes the per-capture production onsets. The assertions validate those chosen bounds; they do not independently reconstruct the source programme timeline from the actual playback code/frame schedule. Keep “about 370 ms later than the normal captures” as a provisional observed difference, not an independently proven count of dropped source frames. Keep the physical mechanism unresolved.

### 16.7 Resampling — analytical step accepted, interpolation claim limited

The integer-length spacing `1e6/N_padded` and values 0.947/0.801/0.694 ppm for the stated padded lengths are arithmetically correct. The rounded-resample test's residuals are consistent with the half-step bound.

The “fractional” comparison is a synthetic numerical demonstration using linear interpolation (`np.interp`), not a band-limited production resampler and not a validated production implementation. Its ±0.01 ppm result supports the possibility of removing length quantization, not yet a production patch. No change to `resample_capture_timebase()` is authorized.

### 16.8 Reproducibility

The manifest records hashes and WAV metadata, but the WAVs and AUDITOR-PACKAGE are not present in the pushed v2.18.1 branch. The report points to an absolute local path under `/opt/MyApps/Adudio/`; GitHub readers cannot retrieve that package from the branch. Preserve the archive at a known shared/downloadable location or provide the package to the coordinator. The manifest's source JSON path is likewise host-specific.

### 16.9 Required v2.18.2 — diagnostics only

Create a new agent branch from the updated coordinator review branch. Do not touch production, main, PR #4, clock policy, pilot frequency or hardware.

1. Make the main MC model match its description. Prefer two separately labelled experiments: (a) a simple white correlated proxy with explicit measured RMS per channel and a clearly defined Pearson correlation versus magnitude-squared coherence; (b) a coloured PSD/cross-PSD model only after fixing the synthesis frequency grid, sample-rate scaling, PSD normalization, band-power normalization and verification. Ensure the function actually called by `run_batch()` is the model named in the report.
2. Validate each generated model with independent per-channel RMS, Welch PSD, cross-PSD/coherence and an explicit 900–1100 Hz band-power check. Store the measured generated noise level and model parameters in the JSON. No claim about D10 during-tone noise unless compared in identical units and bandwidth.
3. Recompute the MC, preserve seeds and per-trial rows, and compute sigma coverage against each trial's own `sigma_pred`. State explicitly that results are model-conditional, not a measured hardware population.
4. Fix pilot-relative ppm and rerun all 997/1000/1013 cells. Save raw per-trial frequency estimates and the exact script hash; resolve the impossible 997/−30 artifact before drawing any conclusion.
5. Keep the factor-two derivation, but label sigma calibration as conditional on the noise/whitening assumptions. Do not describe zero-lag L/R covariance as full coloured-noise GLS.
6. For tone bounds, independently reconstruct the expected schedule from the exact source programme and backend timing/frame counts; retain the six normal captures as a cross-check, not as the source of the expected schedule.
7. Keep the resampling conclusion analytical/provisional; no production patch.
8. Make the WAV package accessible to the coordinator/auditor and ensure the report identifies exact source hashes, scripts, commands and output files.

Required final output: updated report with established/provisional/invalidated statuses, rerunnable scripts, per-trial data, corrected JSON, and a concise list of what changed. Commit and push the v2.18.2 diagnostic branch. No playback or hardware tests.

### 16.10 Current decision gate

v2.18.1 is **received, inspected, and rejected as final evidence**. It does not establish the 500-trial MC statistics for the claimed model, the 997/1013 LSQ pilot conclusions, or calibrated production sigma. It does provide useful artifacts and confirms the no-production-change boundary. Continue with v2.18.2 diagnostics; only after independent review may the coordinator design a production estimator. The current recommendation remains per-channel LSQ with explicit uncertainty and channel QC as the leading candidate, not an approved production change.


## 17. Coordinator review of v2.18.2 — commit 895c6e0 (15 Nov 2026)

### 17.1 Repository/scope status

- Agent branch: `agent/v2182-clock-estimator-evidence-repair`, HEAD `895c6e0a2383898fd0675de9cd19f6c45c25fef1`.
- Parent/base: `d312a23be367f22d8609d4f26a499e74f2ec4461`; GitHub compare shows one commit ahead, zero behind.
- Changes are diagnostic scripts, reports, JSON/CSV, generated tables and a .gitignore entry. No production module, main or PR #4 was changed. No playback or hardware test is evidenced.
- Coordinator review is saved at `measurements/diagnostics/v218-clock-estimator-independent-reproduction/COORDINATOR-REVIEW-v2182.md`.

### 17.2 Decision

**v2.18.2 is a substantial improvement, but is not accepted as final evidence.** The white/coloured generator separation and important spectral synthesis fixes are real. The source-code timeline for the 1.85 s tone start is materially better established. However, the Monte-Carlo and pilot scripts still import the old centroid-started LSQ, the actual fit rate is 12 kHz rather than 48 kHz, the coloured PSD verifier has a missing average, the 868 Hz explanation is not demonstrated, and several claims still overstate the evidence. No production changes or policy changes are approved.

### 17.3 Blocking defect A — v2.18.2 imports the old LSQ estimator

Both `v2182_montecarlo.py` and `v2182_pilots.py` import `est_lsq` from `v218_estimators_real.py`. That module in the v2.18.2 branch still has the v2.18 function:

- it initializes from `dsp.analyze_tone()` production centroid;
- searches only ±0.05 Hz around that centroid;
- unconditionally performs `resample_poly(seg, 1, 4)`.

The MC and pilot scripts pass 48 kHz input. Therefore the actual LSQ fit is **12 kHz**, not 48 kHz. It is still centroid-started, so it does not establish independent acquisition, ±100 ppm capture, or the claimed LSQ-vs-centroid advantage for an independent estimator.

The improved independent coarse-periodogram start and conditional single decimation exist in the v2.18.1 agent branch, but were not carried forward. Port that implementation, then fix its parabolic interpolation: require a valid concave local peak and bound the interpolation offset to the adjacent-bin interval. Log coarse peak bin, adjacent magnitudes, denominator, offset, final refinement interval and actual fit rate. Refuse/flag invalid or out-of-range interpolation rather than accepting a distant extrapolation.

This invalidates the v2.18.2 claims that LSQ is independently initialized, that its fit rate is 48 kHz, and that its pilot experiment measures independent LSQ robustness.

### 17.4 Blocking defect B — the 868 Hz result is not proven stale

The v2.18.1 independent-start estimator's coarse FFT parabolic interpolation checks only `denom != 0`; it does not require negative curvature and does not constrain the fractional-bin correction to ±0.5 bin. A selected coarse maximum at a search boundary, or a non-concave neighbouring profile, can therefore extrapolate far outside the discrete ±2 Hz interval. “The search is ±2 Hz, so 868 Hz is impossible” is not a valid argument for that function.

The v2.18.2 rerun at 997/−30 uses the old centroid-started ±0.05 Hz estimator and does not adjudicate the v2.18.1 result. Reproduce the exact v2.18.1 seed/case on the exact v2.18.1 code; save the selected FFT bin, three adjacent magnitudes, denominator, interpolation offset, final refinement grid and result. Do not call the previous JSON stale until this is resolved.

### 17.5 Coloured PSD verifier — generator improved, verifier accumulation bug

The coloured generator now uses the target grid `fs/n`, the actual 48 kHz sample rate and one-sided synthesis scale `S·fs·n/2`. The conditional complex L/R construction is materially improved and the main MC does call the selected noise model.

But `verify_colored()` accumulates 15 periodograms in `aLL/aRR/aLR` and compares those sums with a single target PSD without dividing by `n_check=15`. The saved PSD deviations are around +11.6…+12.4 dB, which is exactly the expected `10·log10(15)=11.761 dB` accumulation offset. This is a verifier bug, not a +12 dB model/unit mismatch.

After averaging, the saved deviations imply approximate L/R PSD differences to target of:
- 900–1100 Hz: +0.09 / +0.61 dB;
- 100–500 Hz: −0.13 / −0.13 dB;
- 500–2000 Hz: −0.07 / approximately 0.00 dB;
- 2–8 kHz: −0.13 / −0.13 dB;
- 8–24 kHz: −0.14 / −0.14 dB.

The gamma² ratio is invariant to this common division factor, so synthesized gamma² may still be close. However, the verifier JSON saves synthesized gamma² only; the report compares it to measured values not included in that verifier artifact. Fix by averaging all accumulated spectra by `n_check`, store target and generated PSD and gamma² side-by-side for exact frequency bands and averaging method, and add explicit per-channel RMS / band-power checks. The white verifier currently checks Pearson rho but not the asserted −80/−96 dBFS channel RMS; save and assert those values too.

### 17.6 MC records, interpretation and row counts

There are 20 batch CSVs per model, 75 data rows per CSV (25 trials × 3 levels): **1500 rows per model, 3000 rows total**. The report's statement “2000 rows total” is incorrect; regenerate this count from the files.

The batch data and summary statistics agree in the examined aggregates, but all LSQ fields inherit the centroid-started 12 kHz estimator defect in §17.3. Current results may describe the old local refinement conditional on centroid, not independent LSQ.

The white model is an ideal white correlated proxy (common component + independent additions, zero delay); its sigma_pred calibration is conditional on that model and whitening assumption only. The coloured model is generated from 129 idle-D10 quiet windows and does not include the during-tone D10 noise/asymmetry. Its jGLS sigma_pred remains badly under-dispersed (pred/actual roughly 0.045; per-trial coverage roughly 0.04 at 1σ and 0.06–0.08 at 2σ). Do not use those outputs for production acceptance.

### 17.7 Pilot test — ppm fixed, harmonic profile is a stress comb

The v2.18.2 pilot code correctly computes the error relative to the tested pilot. This fixes the v2.18.1 nominal-frequency arithmetic.

However, the test adds 40 mains harmonics (50 Hz through 2 kHz) at exactly the same amplitude for every line in a given trial. Only the 1 kHz line is cited as measured near −93 dBFS. Thus the −93 dBFS run is **not** a measured mains spectrum; it is a flat harmonic-comb stress model. Label it as such.

For a realistic test, estimate/integrate each line's power separately from source WAVs, preserve the measured harmonic vector, and rerun 997/1000/1013 with that profile. Keep flat-comb stress runs separate. Save raw per-trial frequency, seed and script hash. Given the LSQ implementation defect, no pilot conclusion about independent LSQ or pilot replacement is accepted. 1013 Hz may remain a candidate for further offline testing, but production must stay at 1 kHz pending evidence.

### 17.8 Schedule — arithmetic supported; distinguish the two 50 ms quantities

Production code inspection confirms the expected tone start at 1.85 s relative to nominal capture launch:
- `backend.play_record()` starts arecord, waits 0.30 s, then launches aplay;
- it adds a 50 ms leading zero pad and a separate 50 ms trailing pad to the playback stream;
- `Rig.run()`, when edge protection is enabled, adds a 1.5 s leading silence;
- `Rig._seg()` includes a separate 50 ms tail reserve after the segment's tone; with settle 0.4 s and 2.0 s of analysis blocks, the segment is 2.45 s.

Thus the tone onset is `0.30 + 0.05 backend-leading-pad + 1.50 edge-lead = 1.85 s`; the segment ends around 4.30 s. The program WAV check that the tone begins 1.50 s into the programme is consistent.

The script/report comments currently conflate the backend leading pad with the `_seg` tail reserve, and occasionally call the 50 ms a measured D10 latency. Correct these labels. Also, the 0.30 s is measured from process launch, not an explicit timestamp of arecord's first sample; keep the exact capture timeline / mechanism provisional.

The observed capture #1 onset ≈2.225 s and end ≈4.300 s versus nominal [1.850, 4.300] is a strong approximately 375 ms late-start anomaly. It does not prove the physical mechanism or exact number of source frames discarded. The production analysis window is still cross-checked using hard-coded old per-capture onset values; treat the +350 ms overrun as corroboration, not an independently reconstructed detector result.

### 17.9 Resampling — analytical quantum accepted, numeric multi-tone test invalid

The analytical `1e6/N_padded` quantum remains arithmetically correct. However, `v2182_resample_exact.py` synthesizes simultaneous 1, 10 and 20 kHz tones, then estimates each with a single-tone LSQ over the full multi-tone record. The residual includes cross-tone fit bias; this does not isolate resampler timebase error. Retest one tone at a time or use a properly isolated band-limited tone. Linear interpolation remains a demonstration only; no production patch is authorized.

### 17.10 Source-data availability

The WAV directory is ignored by Git and absent from the pushed tree. The report's local `wav/` directory is not accessible to the coordinator/auditor. Only the earlier manifest is available in the repository. Provide the capture and programme WAVs through an accessible shared archive/location with hashes and full format metadata (dtype/sample format, Fs, channels, frames).

### 17.11 Required v2.18.3 — diagnostics only

1. Port the independent-start LSQ from the v2.18.1 branch, fix bounded/concave parabolic interpolation, ensure conditional one-stage decimation, and log actual fit rate/coarse peak/refinement bounds.
2. Test deterministic ±100 ppm acquisition, low SNR, all three pilot frequencies and a strong line adjacent to/outside the search interval. Reproduce the exact v2.18.1 868 Hz case and explain its parabolic values.
3. Rerun both 500×3 MC models using the corrected estimator. Save new per-trial CSVs and summaries; state actual fit rate and per-model assumptions.
4. Fix coloured verification by averaging over `n_check`; store target and synthesized PSD/coherence per band. Add measured white-noise channel RMS checks. Correct total row count to 3000.
5. Rerun pilots using individually measured mains-line powers from the source WAVs; keep equal-level flat comb as a separate stress case.
6. Correct schedule comments to distinguish backend leading/trailing pads, `_seg` tail reserve, settle/analysis offset and edge-protect lead. Keep the 1.85/4.30 timing result provisional with the process-launch timestamp caveat.
7. Replace simultaneous multi-tone resample test with one-tone-at-a-time or band-isolated tests. Preserve analytical quantum only; no production resampler changes.
8. Provide source WAV package in an accessible shared location with complete hashes/format metadata. Generate all report tables from corrected outputs, and record exact commands/hashes/counts.

No production changes, main changes, PRs, playback, hardware or DUT measurements.

### 17.12 Current decision gate

v2.18.2 is **received and partially validated, but not accepted as final evidence**. Noise-model generation and timing-source analysis are materially improved; the colored PSD verifier has a missing averaging factor. The independent LSQ claim, 48 kHz fit-rate claim, pilot robustness and stale-868 explanation are not established because the old centroid-started 12 kHz estimator is still imported. Resample numeric residuals are not valid due to multi-tone fitting. No production design/patch or clock-gate policy change is authorized before independent review of v2.18.3.

The leading production direction remains per-channel LSQ with frequency/ppm, uncertainty, tone presence, SNR and window-validity QC for both channels; choose the most informative valid channel rather than hard-code R-only. Defer joint GLS until its frequency-local cross-spectral covariance model is validated.


## 18. Coordinator review of v2.18.3 — commit 92636b5 (15 Nov 2026)

### 18.1 Scope and data status

- Agent branch: `agent/v2183-clock-estimator-validation`, HEAD `92636b512088905363e8e78ac092d61217b41cd3`.
- Parent/base: `14097f5ad0d1cc77204695f5bf098051b3de09cf`.
- Changes are diagnostic only; production/main/PR #4 untouched. No playback or hardware test is evidenced.
- 20 × 75 rows per model = 1500 per model / 3000 total. Reviewed per-trial CSV chunks contain unique seeds and `fit_rate_lsq=48000`; MC results support ~3.3× lower LSQ σ than centroid within both synthetic models, with the limits below.
- Full review saved at `measurements/diagnostics/v218-clock-estimator-independent-reproduction/COORDINATOR-REVIEW-v2183.md`.

### 18.2 Decision

**v2.18.3 is a substantial improvement, but not accepted as a closed evidence package.** The independent LSQ implementation and fit-rate defect are fixed in the diagnostic code. However, the exact v2.18.1 868 Hz result was not reproduced with the version that generated it; the pilot profile isn't reproducible and may be 3 dB low; the white-noise model's absolute levels are wrong under the production dBFS convention; schedule comments still conflate two different 50 ms pads; and the old invalid multitone resample test remains.

No production implementation, main/PR changes, pilot-frequency changes or clock-gate policy changes are approved.

### 18.3 Independent LSQ — diagnostic candidate confirmed, acquisition boundary still needs a guard

`v2183_estimators.py` now coarse-starts from a Hann-window periodogram, not the production centroid. It decimates only if Fs >96 kHz, so 48 kHz input is fitted at 48 kHz. The RSS parabolic correction includes the `step` multiplier and is bounded; the spectral-peak interpolation requires positive curvature and is limited to ±0.5 bin. The formulae are dimensionally consistent.

The coarse-peak search uses `0 < k < len(X)-1`, however, instead of checking whether the selected bin is interior to the local `ib` acquisition interval. If the maximum is at `ib[0]` or `ib[-1]`, the adjacent global FFT bin outside the requested ±2 Hz interval is still used for interpolation. The function returns a frequency without an acquisition-validity flag. A large adjacent line can therefore pull the initial estimate to the search boundary, outside which the ±0.15 Hz RSS refinement cannot recover the true tone. Add boundary rejection/expansion/invalid status and an explicit strong-out-of-band-line test.

### 18.4 Blocking defect — the 868 Hz reproduction targets the wrong historical estimator

The problematic v2.18.1 JSON was generated by `v2181_lsq_pilots.py` on commit `8d8b040969a9a872941ecfe502975f45419d4186`, importing the independent coarse-periodogram estimator in that same revision. It searched ±2 Hz and used an **unbounded** periodogram parabolic interpolation.

The new `v2183_868_reproduction.py` says it copies `est_lsq` from `0b6853b`. That older commit contains the centroid-started estimator: it calls `dsp.analyze_tone()` and searches only ±0.05 Hz. It is not the function used by the v2.18.1 pilot script that generated the saved result.

The v2.18.1 JSON at pilot 997 / −30 reports `lsq.bias_ppm = −131878.593` using the old 1000 Hz reference. This implies a mean estimated frequency of approximately 868.098 Hz:
`1000 × (1 + (−131878.593 − 23.5) × 1e−6) ≈ 868.098 Hz`.

Therefore the new reproduction near 1000 Hz only tests the older centroid-started estimator and does not refute the v2.18.1 independent-start result. “868 Hz is impossible in any saved version” and “JSON stale” are **not established**. Rerun the exact 8d8b040 function with original pilot script, inputs and first 50 seeds, logging coarse bin, adjacent FFT magnitudes, curvature/denominator, unbounded offset, fine grid and returned f. Then compare with the bounded v2.18.3 function.

### 18.5 WHITE model — +3.01 dB level mismatch and no RMS verification

Production `dsp.rms_dbfs(rms)` converts RMS relative to a full-scale sine using `20·log10(rms·sqrt(2))`. The white generator sets noise standard deviation to `10^(dBFS/20)` using `dbfs_to_amp()`, without dividing by `sqrt(2)`. Under the production metric, generated Gaussian floors are therefore about −77/−93 dBFS, not the claimed −80/−96 dBFS.

To achieve the claimed production levels, set Gaussian standard deviation to `10^(dBFS/20)/sqrt(2)`, or relabel and report the actual levels. `verify_white()` currently checks only Pearson correlation; it does not calculate channel RMS or production `noise_dbfs`, despite the final agent message claiming separate RMS checks. Add those measurements/assertions and rerun/relabel the MC. The model remains a useful ideal white, zero-delay correlation proxy, but its absolute floor claim is currently false by 3.01 dB.

### 18.6 COLORED model — PSD verifier fixed, target/phase evidence still missing

The divide-by-`n_check` error is fixed and the saved PSD differences (about ±0.14 dB) are plausible. But the verifier JSON stores generated gamma² and PSD deviation only; it does not store the target gamma² values quoted in the report. It also checks only magnitude-squared coherence, not complex cross-spectral phase. Save target and generated auto-PSD, gamma² and complex CSD magnitude/phase per band using the same convention. Retain the idle-D10/E-MU-floor limitation; this is not full during-tone noise.

### 18.7 Pilot profile — hard-coded, 3 dB amplitude conversion issue

`v2183_pilots.py` correctly calculates ppm relative to each pilot, runs at 48 kHz and separates measured-profile and stress-comb cases. It nevertheless defines `MEASURED_PROFILE_DBHZ` as a hard-coded dictionary; it does not read source WAVs or accept `--source-dir`. The branch does not include a derivation script for the per-harmonic vector or the actual line frequencies.

The conversion `amp = sqrt(PSD × 23.4)` turns integrated one-sided Welch PSD into an RMS amplitude. The signal is then synthesized as `amp × sin()`, where `dbfs_to_amp()` uses peak sine amplitude. Unless the dictionary was already peak-normalized elsewhere (not documented), the correct peak amplitude is `sqrt(2 × P_line)`; current synthetic harmonics are about 3.01 dB too low. Derive/integrate each line's power and actual frequency from source WAVs, preserve source hashes, convert RMS to peak correctly, and rerun. Save deterministic seeds (avoid Python salted `hash()`) and hashes for both the pilot script and estimator module.

The result that 997 is better for the narrow-window centroid while 1013 is a good LSQ pilot is a provisional tradeoff. Do not change the production 1 kHz pilot on this test.

### 18.8 Schedule — 1.85/4.30 arithmetic correct, two 50 ms terms still conflated

The correct timeline is:
- start: 0.30 s backend wait + 0.05 s **backend leading pad** + 1.50 s edge lead = 1.85 s;
- tone end: 1.85 + 0.40 s settle + 2.00 s analysis + 0.05 s **Rig._seg tail reserve** = 4.30 s;
- backend also appends a separate 0.05 s trailing pad after the entire playback programme.

The final report still says the 0.05 s pad is the `_seg` tail reserve and not a leading element, although the 0.05 used in the start equation is the backend leading pad. These are two distinct 50 ms quantities. Correct script/report labels. The +375 ms capture-1 late onset remains a strong observation; the process-launch/first-captured-sample offset and physical cause remain unresolved. The +350 ms window overrun still uses hard-coded per-capture `prod_onset` values and is only a cross-check.

### 18.9 Resampling and source package

- The analytical `1e6/N_padded` quantum remains valid.
- The carried `v2182_resample_exact.py/json` still contains the simultaneous 1/10/20 kHz test measured with a single-tone estimator. Its numeric residuals remain contaminated by cross-tone fitting. The report omits them, but stale data remain. Replace with one tone at a time / band-isolated measurements or clearly mark/remove those numeric results.
- `v2183-wav-manifest.json` lists only seven capture WAVs and points to a host-specific absolute results JSON. It does not include all 7 programme WAVs/THD files claimed by the report. The `wav/` payload remains ignored by Git and inaccessible to coordinator/auditor from the branch. Provide an accessible archive/release asset plus full manifest (SHA256, dtype/sample format, Fs, channels, frames) for all required files.

### 18.10 Required v2.18.4 — diagnostics only

1. Reproduce the exact 8d8b040 v2.18.1 estimator and exact original 997/−30 case/seeds; log all coarse interpolation values. Do not claim stale until this is reproduced.
2. Add explicit coarse-search boundary detection/acquisition validity to v2.18.3 LSQ. Test a strong line just outside the coarse band and save status/trace per trial.
3. Correct WHITE model standard deviation for production dBFS convention or relabel actual floors; verify measured per-channel RMS/`rms_dbfs`/rho, then rerun MC.
4. Expand COLORED verifier to save target and generated auto-PSD, gamma² and complex CSD magnitude/phase per band.
5. Derive pilot line frequencies/powers from source WAVs with an executable extraction script, correct RMS-to-peak conversion, deterministic seeds and estimator/script hashes; rerun 997/1000/1013 and stress profiles.
6. Correct the separate backend leading pad, `_seg` tail reserve and backend trailing pad labels. Preserve timing observation/caveats.
7. Replace or invalidate the simultaneous-multitone resample numerical data; retain analytic quantization only.
8. Provide an accessible WAV/programme package and complete manifest. Regenerate all tables from corrected artifacts.

No production changes, main/PR changes, playback, hardware or DUT testing in v2.18.4.

### 18.11 Current decision gate

v2.18.3 is **partially accepted as diagnostic progress, not as final evidence**. The independent LSQ and 48 kHz fit-rate fixes are real; the MC comparison is valid within the two stated model constructions. The exact 868 Hz issue is still open because the reproduction uses the wrong historical estimator. Pilot-profile provenance and amplitude scaling, white-noise floor levels, schedule labels, resample numeric data and source-package availability remain unresolved.

The production direction should now be refined: per-channel LSQ with explicit acquisition-validity, uncertainty, tone-presence, SNR and window QC remains a candidate, but **must not be blindly preferred**. The stress tests show that a narrow-window centroid can outperform full-band LSQ under strong nearby lines. Keep centroid as a separately qualified fallback or add line-aware nuisance fitting; determine that selection policy in diagnostics before production changes.


## 19. Coordinator review of v2.18.4 — commit 4fc938e (15 Nov 2026)

### 19.1 Scope and decision

- Agent branch: `agent/v2184-clock-estimator-evidence-closure`, HEAD `4fc938e9b970655081bd82362336249fb32df461`.
- Parent/base: `24b5eeab02c11cdbf9d7ffb889bc61d0ed7e8f27`; one commit ahead, zero behind.
- The commit adds diagnostics only; compare shows no production-source changes. No playback, hardware or DUT test.
- Full coordinator review saved at `measurements/diagnostics/v218-clock-estimator-independent-reproduction/COORDINATOR-REVIEW-v2184.md`.

**v2.18.4 is partially accepted as diagnostic progress, not as a closed evidence package. No production approval.**

### 19.2 868 Hz — historical reproduction accepted

The reproduced function is the independent-start `est_lsq` at `8d8b040`, the version imported by `v2181_lsq_pilots.py`. The signal construction and seed formula for 997 Hz / −30 dBFS match the original case. The 100-trial reproduction gives 865.387–870.506 Hz, mean 868.085 Hz, sample σ 1.946 Hz; all trials are within 5 Hz of the failure cluster. Example trial 0: coarse bin 999 Hz, neighbours [1.4535, 189.8412, 379.6506], positive curvature +1.4216, unbounded shift −133.015 bins, final 865.8348 Hz. This closes the previous question about whether the saved v2.18.1 result came from the correct estimator.

### 19.3 New LSQ implementation — blocking defects remain

- `v2184_estimators.py` selects a maximum of `abs(rfft())`, but computes parabolic interpolation only when `curv = y[k-1] + y[k+1] - 2*y[k]` is positive. A local maximum has negative curvature; the sign test is reversed.
- `edge_peak` is only returned as metadata; the function still returns a frequency. `est_joint` discards the status, and pilot code still scores an edge result.
- RSS refinement has a fixed ±0.15 Hz grid but does not flag a minimum at either boundary.
- The demonstrated 997/1000 Hz, −30 dBFS case remains biased by −127 ppm with coarse status `ok`. No post-fit residual-line QC is implemented. Return explicit validity, residual metrics and failure states; fail closed in all callers.

### 19.4 White / colored Monte Carlo

White generator level correction is accepted: RMS verifier reports −79.99/−96.00 dBFS against −80/−96 declared under the production metric. The 40 CSV files are 20 batches per model × 75 trial rows each = 3000 rows; sampled rows show 48 kHz fit rate. White `sigma_pred` is conservative only within the white synthetic model (pred/actual ~1.40–1.46).

Colored MC supports LSQ σ ~3.17/3.34 ppm vs centroid ~10.47/10.67 ppm at −72 dBFS within this specific synthetic idle-noise construction. It correctly shows white `sigma_pred` is unusable for colored noise (pred/actual ~0.06); the known during-tone L/R asymmetry is not modeled.

The claimed full colored verification is **not accepted**: report's target `gamma² model` entries are blank; verifier outputs only synthesized gamma², band-averaged PSD deviations and mean phases, not complete target/generated complex CSD and gamma² comparisons. `v2184-verify-colored-full.json` is created by script but absent from the branch.

### 19.5 Pilot harmonic extraction — still under-integrates line power

The code now reads WAVs rather than using a static dictionary, but `nperseg=8192` at 192 kHz gives 23.4375 Hz resolution. The ±12 Hz integration contains typically one bin and does not capture the full Hann main lobe. A same-settings numerical check on pure tones recovered only ~0.577 of 1 kHz power (−2.39 dB in inferred peak amplitude) and ~0.651 of 50 Hz power (−1.86 dB). `sqrt(2P)` is correct only after accurate line-power integration.

Also: `S_LR` is calculated but unused; the pilot synthesis applies the same random line phase to L/R; only channel L is actually analysed; the JSON stores the pilot-script hash but not source WAV or estimator hashes. Rework extraction with calibrated injected-line tests; preserve full provenance and score both channels.

### 19.6 Schedule — 50 ms terms remain conflated

Production `backend.play_record()` explicitly builds playback data as `[pad, out, pad]`, with 50 ms zero pads at both ends, and separately waits 0.30 s before starting playback. The 50 ms term in the 1.85 s start equation is the **leading backend pre-pad**, not a measured D10 latency or a backend tail pad. The separate backend post-pad is omitted from v2.18.4's component accounting; `Rig._seg` has another independent 50 ms tail reserve. Correctly name all three, retain the 1.50 s edge lead and 0.40 s settle. The +375 ms capture-1 head-loss remains an observation with unknown physical cause.

### 19.7 Resampling and data package

The numerical resampling test now isolates one 1 kHz tone, resolving the previous multitone-fit contamination for that test. Fractional interpolation remains demonstration-only; 10/20 kHz impact values are analytical. No production resampler change.

The commit does not contain the claimed 271 MB WAV directory; `v2183-wav-manifest.json` and `v2184-verify-colored-full.json` are absent from the branch. Local ignored files and host-specific paths are not independently reproducible. Require an accessible complete archive, SHA256 and format metadata.

### 19.8 Required next iteration — diagnostics only

1. Fix coarse parabola sign/validity; add off-bin, flat, convex and concave tests and RSS-boundary status.
2. Implement post-fit residual line detection/quality gate for the −127 ppm out-of-band-line case; fail closed on invalid acquisition/fit in LSQ, pilots and joint GLS.
3. Rework pilot line-power extraction with calibrated integrated power; preserve WAV and estimator hashes, L/R complex cross-spectrum as needed, and score both channels.
4. Commit complete colored verification with target/generated auto-PSD, gamma² and complex CSD magnitude/phase.
5. Correct backend leading pad, backend trailing pad and `Rig._seg` tail reserve names and schedule model.
6. Publish all required WAVs with a complete manifest; preserve diagnostic-only scope.

No production/main/PR changes, pilot or clock-gate policy changes, playback, hardware or DUT work.



## 20. Coordinator review of v2.18.5 — commit 31c1f00 (16 Nov 2026)

### 20.1 Scope and decision

- Agent branch: `agent/v2185-line-aware-estimator-repair`, HEAD `31c1f001e133b3464f4739fff9fc32da5e8d44fa`.
- Parent/base: `65e44ea360744bbd7a01b41b6785f251f7ea7b44`; one commit ahead, zero behind.
- Compare contains diagnostic artifacts only. Production, main and PR #4 are untouched; no playback/hardware/DUT tests.
- Full review: `measurements/diagnostics/v218-clock-estimator-independent-reproduction/COORDINATOR-REVIEW-v2185.md`.

**v2.18.5 is partially accepted as diagnostic progress; it is NOT accepted as a working line-aware estimator or closed evidence. No production approval.**

### 20.2 Accepted progress

- Coarse spectral maximum interpolation now uses the correct concavity sign and bounded ±0.5-bin offset; RSS minimum interpolation exposes a `grid_edge` state.
- The exact historical 868 Hz case is rejected by the new edge guard in 100/100 trials; `est_joint` fails closed for that case.
- Schedule code now distinguishes backend pre-pad, backend post-pad and `Rig._seg` reserve.
- Manifest contains 32 unique entries and its total byte count agrees with the sum of entries.
- Pilot script now records both channels and deterministic L/R line phase; resampling remains a one-tone test.
- No production code changed.

### 20.3 Residual QC is fail-all, not calibrated

The residual detector compares the single largest FFT bin outside ±5 Hz from the fitted carrier with the median of all residual bins and flags it at a fixed 10 dB ratio. The parameter `qc_line_db` is not used; `line_amp_db` is not dBc; there is no multi-bin/persistence requirement.

The committed results report `line_contaminated` for 500/500 L and R trials at every level in both MC models, 100/100 both channels for all measured-profile pilots, and even for a pure tone with no noise. Unit tests explicitly expect these false positives and do not exercise a positive normal `status='ok'` path. 19/19 PASS therefore confirms current fail-all behavior, not a usable QC gate.

The known v2.18.4 −127 ppm case is not closed: residual search masks ±5 Hz, while its 997 Hz pilot / 1000 Hz interferer is separated by ~3 Hz. The exact in-band-coarse counterexample is not rerun with correct line attribution.

### 20.4 CSD conjugation error remains

The source `S_LR` is obtained with `scipy.signal.csd(L, R)`, whose documented convention is `conj(FFT(L))*FFT(R)`. For a generator `R = g L`, reproducing that target requires `g = S_LR/S_LL`. Current `noise_colored` uses `conj(S_LR)/S_LL`.

The verifier then accumulates `FL*conj(FR)`, the opposite cross-spectrum convention, and compares it with the un-conjugated SciPy target. A synthetic +0.37-rad L/R test confirms: target SciPy phase +0.37, current generated SciPy CSD phase −0.37, while the verifier's opposite-convention product is +0.37. Thus the saved phase match is an artifact of inconsistent conventions; the colored complex-phase fix is not accepted. Correct generator and verifier under one convention, add a known-phase test and rerun colored MC.

### 20.5 Pilot profile still under-integrates line power

The script still uses Welch `nperseg=8192` at 192 kHz (Δf=23.4375 Hz) and integrates only ±12 Hz, typically one bin rather than the full Hann main lobe. Independent pure-sine check with the same method recovers only 0.577 of 1 kHz power (−2.388 dB) and 0.652 of 50 Hz power (−1.861 dB). `sqrt(2P)` is correct only after accurate line-power recovery. The JSON stores a pilot-script hash, but no source WAV or estimator hashes. Both-channel scoring is progress; calibrated profile extraction is not closed.

### 20.6 MC and source-package limitations

The MC CSVs predate fail-closed joint semantics and do not include `status_joint_gls`. All channel rows are QC-rejected; the summary nevertheless calculates raw frequency-error statistics. Keep these as conditional raw-estimator accuracy only, not accepted measurements. Joint-GLS/`sigma_pred` evidence must be regenerated after correcting CSD and fail-closed flow.

The manifest's 32 names and byte sum are internally consistent, but actual WAV payload is only on the agent's local host; SHA256 values cannot be independently checked. No release asset should be published without user approval.

### 20.7 Required v2.18.6 — diagnostics only

1. Correct CSD convention end-to-end; known-phase test, full target/generated PSD, γ², complex CSD comparison, then colored MC rerun.
2. Replace residual max/median QC with calibrated local/noise-aware line detection that does not mask the 997/1000 nearby-interference case; positive clean `ok` controls and TP/FP/TN/FN are mandatory.
3. Reproduce the exact −127 ppm case and sweep nearby interferer offsets (including ±3 and ±5 Hz).
4. Calibrate pilot line-power recovery with injected on/off-bin tests and full main-lobe integration; record source WAV and estimator hashes.
5. Let joint estimator select/try both valid channels; exercise both accepted and rejected paths.
6. Regenerate MC/pilot summaries with status-separated accepted/rejected counts; do not score rejected estimates as accepted.
7. Keep WAV publication as a user decision; no blind upload.
8. No production/main/PR changes, pilot/clock-gate policy changes, playback, hardware or DUT work.



## 21. Coordinator review of v2.18.6 — commit fb12df6 (17 Nov 2026)

### 21.1 Scope and decision

- Agent branch: `agent/v2186-qc-calibration-csd-convention`, HEAD `fb12df62321f453a73db8d13dc2faf5824afd2df`.
- Parent/base: `7376cc18103a920e1cbbf32add31d7990d2e1dee`; one commit ahead, zero behind.
- Compare contains diagnostics only. Production/main/PR #4 are untouched; no playback, hardware or DUT work.
- Full review: `measurements/diagnostics/v218-clock-estimator-independent-reproduction/COORDINATOR-REVIEW-v2186.md`.

**v2.18.6 is partially accepted as diagnostic progress; not accepted as a production-ready estimator or closed evidence. No production approval.**

### 21.2 Accepted

- CSD synthesis now uses `S_LR/S_LL`; verifier uses `conj(FL)*FR`, consistently matching `scipy.signal.csd(L,R)`. The reported +0.37 → +0.3685 rad check supports the phase-sign correction.
- Anchored nominal fit ±0.15 Hz prevents coarse-peak teleportation; the 997/1000 and ±3/±5 Hz synthetic interference classes are now detected/attributed.
- Residual QC now uses a local floor, a ≥3-bin cluster and carrier-relative dBc.
- Pilot line power uses 32768-sample Welch segments, injected-line recovery calibration, side-band background subtraction and CSD phase. This corrects the old main-lobe truncation method; weak-line validation remains uncertain.
- Schedule names distinguish backend pre-pad, post-pad and `_seg` reserve.
- Fresh line-free colored MC and white MC both show 500/500 accepted at each tested level in the committed summaries.

### 21.3 Blocker — `ok_partial` still fits both channels

`est_joint` selects its starting frequency from whichever channel passes QC, but afterwards still computes residuals/covariance and the joint GLS objective from both original channels. The rejected channel is not removed. The current 1050 Hz partial-path test is far from the 1000 Hz carrier and only checks the returned status. Require true channel masking or return the valid single-channel estimate; test a ±3 Hz contaminant against a clean opposite channel.

### 21.4 Blocker — missing tone can be accepted

`snr_local_db` is computed but never used as an acceptance threshold. All 700 negatives in the confusion matrix contain a pilot. No-pilot/noise-only or dropout negatives exist. An independent replay of the committed LSQ/RSS/residual-QC logic on 200 noise-only records (2 s, 48 kHz, −80 dBFS Gaussian noise) returned `ok` on 55/200, `grid_edge` on 110/200 and `line_contaminated` on 35/200. Add an explicit tone-presence/minimum-SNR gate and tests for absent tone, partial tone and low-SNR captures.

### 21.5 Blocker — `sigma_pred` applies sqrt(2) twice

`est_joint` returns `var_f = 2/(RSS'' )` after its finite-difference curvature conversion, but the MC runner calculates `sqrt(2*jg[1])`. The additional factor inflates reported sigma by sqrt(2). The current white pred/actual ratio 1.462 would be about 1.034 after removing that extra factor; colored 0.511 would be about 0.361. Re-derive and apply variance-to-sigma conversion once, then refresh coverage tables. Do not use current `sigma_pred` claims.

### 21.6 Historical −127 ppm not exactly replayed

The 1700-case confusion suite uses 2 s at 48 kHz and a different noise/line construction from the original one-second 192 kHz → 48 kHz v2.18.1/v2.18.4 test. Its legacy script calls 8d8b040 and produces +1775.7 ppm, while the historical −127 ppm was reported by the v2.18.4 bounded-coarse ±0.15 Hz estimator. The 996.85 Hz lower-grid-end arithmetic is correct, but exact replay is not done. Re-run the exact v2.18.4 estimator and original signal/seed; keep the present legacy family as separate related cases.

### 21.7 Other evidence limits

- The TP/TN results are exact for the defined synthetic population only: no absent-tone negatives and no systematic below-carrier line sweep. The weakest injected positive in the confusion matrix is +12 dBc relative to the pilot; add calibrated dBc thresholds including −20…−45 dBc and actual line profiles.
- CSD phase sign is fixed, but the verifier does not save target gamma² or cross-spectrum magnitude errors on the same grid. The unit known-phase test divides CSD density by raw FFT-bin power and checks phase only; it does not validate generated amplitude. Use common PSD/CSD normalization and store target/generated full cross-spectral metrics.
- Pilot JSON records source WAV and pilot script hashes, but omits the estimator-module hash. Add it.
- The WAV archive remains local and has not been published; no publication requested.

### 21.8 Required next iteration — diagnostics only

1. Fix true channel exclusion in `ok_partial` and compare with the good-channel-only estimate.
2. Add a tone-presence/SNR gate, including no-pilot, dropout and low-SNR test sets.
3. Correct the extra sqrt(2) in `sigma_pred`; recalculate MC summaries and coverage.
4. Replay −127 ppm with the exact v2.18.4 estimator, original sample rate, duration, noise, phase and seed.
5. Add an interference amplitude sweep below carrier and around the −35 dBc threshold, with actual measured line profiles and TP/FP/TN/FN.
6. Complete the CSD verifier with common normalization, generated amplitude/PSD assertions and target/generated gamma²/CSD magnitude/phase.
7. Add estimator SHA256 to pilot provenance.

No production/main/PR changes, pilot-frequency or clock-gate changes, playback, hardware or DUT work.


## 22. Coordinator review of v2.18.7 — commit 12d64d9 (17 Nov 2026)

### 22.1 Scope and decision

- Agent branch: `agent/v2187-partial-channel-snr-uncertainty-repair`, HEAD `12d64d914c336d6db3aba865bcaf89fb70d4aa7b`.
- Base: `47657b350b2cd58aaf2c1209ea6248a34aa846d0`; compare 5 commits ahead, zero behind.
- The branch compare contains diagnostic material only under `measurements/diagnostics/v218-clock-estimator-independent-reproduction/`. No production source, main or PR #4 changes; no playback/hardware/DUT work.
- WAVs were transferred to the remote compute host for the heavy diagnostic runs; the WAV archive remains unpublished.
- Full review: `measurements/diagnostics/v218-clock-estimator-independent-reproduction/COORDINATOR-REVIEW-v2187.md`.

**v2.18.7 is partially accepted as diagnostic progress; NOT accepted as a production-ready estimator or fully closed evidence. No production approval.**

### 22.2 Accepted progress

- **True partial-channel isolation:** when one channel passes, `est_joint` returns that channel's own LSQ result and lists it in `channels_used`; the rejected channel no longer enters covariance, whitening or GLS. Paired L/R tests compare against the good-channel-only result.
- **Tone-presence gate is active:** no-pilot tests now reject rather than returning `ok`; 200 noise-only records at −80 dBFS give 0/200 `ok` (the v2.18.6 independent replay gave 55/200). Dropout, partial window, low-SNR and clipping tests are present.
- **Variance conversion corrected:** `variance_hz2` and `sigma_hz=sqrt(variance_hz2)` remove the extra sqrt(2). White MC prediction/actual ratios are 1.03, 0.987 and 1.003 at −72/−66/−60 dBFS, with near-nominal 1σ/2σ coverage.
- **CSD convention and amplitude check:** generator and verifier use `conj(FFT(L))*FFT(R)`; known phase +0.37→+0.3685 rad, amplitude ratio 1.025; target/generated S_LL, S_RR, S_LR and γ² arrays are saved.
- **Line-QC sweep extended:** 3090 cases with levels +20…−45 dBc, signed offsets ±1…±5 Hz, measured profiles and no-tone negatives. No false accepts were seen among 360 defined negatives; 1303 line detections had 1303 correct attributions.
- **Provenance improved:** pilots JSON contains SHA256 for the script, estimator module, WAV manifest and source WAVs. Weak-line extraction validation still has mean absolute error 1.21 dB / maximum 5.24 dB.

### 22.3 Blocker — numeric SNR threshold is not shown equivalent to production

The presence gate is real and catches the tested no-pilot records. But the new `snr_carrier_db` calculation is not the production `dsp.analyze_tone.local_snr_db` calculation to which the 20 dB constant belongs.

Production `clock_check` obtains its SNR from `dsp.analyze_tone`, whose local floor is the median local spectrum multiplied by the fundamental-lobe bin count (normally 11), using the production Block/BH spectrum. v2.18.7 estimates an integrated noise RMS from a Hann-windowed residual across approximately ±32 Hz, excluding ±2 Hz, and compares this with the fitted peak amplitude. Different bandwidth, window and normalization mean the same 20 dB value is not automatically equivalent.

Required: paired calculations on identical deterministic records and matched segments, with white/colored noise, hum/lines, pilot-level sweep, near-threshold cases, dropout, partial windows and no-tone cases. Save paired metric values, deltas and classification matrices. Either harmonize the metric or calibrate an explicitly separate threshold. Do not change production clock-gate policy or pilot frequency.

### 22.4 Historical −127 ppm — failure mechanism reproduced, original input not recovered

The v2.18.7 replay copies the identified v2.18.4 estimator code at ref `4fc938e` (not the unrelated 8d8b040 estimator). With a reconstructed 2 s @ 192 kHz signal, internally decimated 1/4, pilot 997 Hz at −60 dBFS, a 1000 Hz line at −30 dBFS and −96 dBFS noise, it produces 48/100 lower-end results (−126.95 ppm), 52/100 upper-end results (+173.95 ppm), and status `ok` in all 100. v2.18.7 rejects all 100.

This validates a v2.18.4 failure family and demonstrates that the new residual QC catches the chosen synthetic case. It is **not an exact replay of the historical test input**: the original inline script/input was not saved, and the current parameters were selected by searching for the −127 ppm fingerprint. Keep the report labelled “reconstructed synthetic replay”; do not call it exact historical reproduction unless the original input/provenance is recovered.

### 22.5 Remaining line-QC limitation and uncertainty boundary

The sweep has 1303 line-QC true positives (47.73% of 2730 injected-line positives), plus 48 unnamed/other refusals. Any-refusal recall is 49.49%; these are different metrics and must not be conflated. The ±1/±1.5 Hz exclusion remains a blind zone: strong lines there can bias accepted clock estimates by tens to >100 ppm (up to 118.5 ppm in the reported cells). This is an architectural limitation requiring a separate design task, not a minor reporting caveat.

White-noise `sigma_pred` is accepted only for the tested white synthetic model. Colored MC reports pred/actual ≈0.36 and poor 1σ/2σ coverage (about 0.27–0.33 / 0.526–0.554). Do not publish `sigma_pred` as calibrated for colored noise.

### 22.6 Required v2.18.8 — diagnostics only

1. Pair v2.18.7 `snr_carrier_db` with production `dsp.analyze_tone.local_snr_db` on identical records and matched segments. Calibrate/justify the threshold; do not change production policy.
2. Search available archived history for the original historical −127 input/script. If unavailable, keep the synthetic reconstruction label; never invent provenance.
3. Design and test a separate nearby-tone discriminator for the ±1/±1.5 Hz blind zone. Preserve the current exclusion band until a distinct design is reviewed.
4. Keep colored-noise uncertainty explicitly non-publishable until a temporally correlated-noise covariance model is validated.
5. Preserve diagnostic-only scope: no production/main/PR changes, pilot-frequency or clock-gate changes, playback, hardware or DUT work.



## 23. Coordinator review of v2.18.8 — commit 89569a1 (17 Nov 2026)

### 23.1 Scope and decision

- Agent branch: `agent/v2188-snr-calibration-nearby-tone-design`, HEAD `89569a1e4753d6c56cbb33e9ae19252929c9f170`.
- Base: `0fbf2e2284b61b07347d0570c347673cc89e536c`; compare 7 commits ahead, zero behind.
- The branch compare contains diagnostic files only. Production/main/PR #4 and pilot/clock-gate policy are untouched; no playback, hardware or DUT work.
- Full review: `measurements/diagnostics/v218-clock-estimator-independent-reproduction/COORDINATOR-REVIEW-v2188.md`.

**v2.18.8 is partially accepted as diagnostic progress. SNR calibration and nearby-tone discrimination are not closed; no production approval.**

### 23.2 Accepted progress

- The 760 paired synthetic records establish that the v2.18.7 SNR metric is not numerically interchangeable with production `local_snr_db` by copying the same 20 dB threshold.
- The original v2.18.4 inline script/input for −127 ppm was not found in available repository history; the v2.18.7 result remains correctly labelled synthetic reconstruction.
- The beat-envelope experiment was abandoned; the current nearby probe is a local spectral-peak search in the LSQ residual and does not alter status.
- The ±1.5 Hz exclusion remains unchanged.
- The branch contains diagnostic work only.

### 23.3 Blocker — SNR comparison does not reproduce standard production segmentation

The v2.18.8 comparison calls production `dsp.analyze_tone(x, FS=48000, f0, N=96000)`: one 2-second analysis block.

The standard `Rig` defaults in `valvebench/measure.py` are `fs=96000`, `df=1`, `blocks=2`; hence `self.n=96000` and `_tone_an` passes 96000 samples per block. The standard clock-check path supplies a two-block (2-second) window, therefore production computes its spectrum from two 1-second blocks. The v2.18.8 comparison uses a different FFT segmentation and frequency-bin spacing. Although both routines see the same total waveform duration, the paired production SNR is not the standard production clock-check result.

Consequently, the pooled regression diag = 1.192·prod − 15.09 and the derived diag≈8.8 dB “equivalent threshold” are not accepted as production calibration. Keep diagnostic 20 dB provisional; do not change production `CLOCK_MIN_SNR_DB=20`.

### 23.4 Nearby spectral probe — incomplete validation and stale documentation

The implementation is a residual spectral-peak probe, not the beat-envelope algorithm still described in stale docstrings. Its current validation:
- has 60 quiet controls and zero false fires in that sample;
- pools sync/free-phase populations;
- only injects positive offsets +0.5, +1.0 and +1.5 Hz;
- tests offsets aligned to the 0.5 Hz FFT grid, not off-bin interferers;
- lacks AM/FM sideband and other nonstationary-pilot controls.

The claim of ±Δ coverage is unsupported because the generator only iterates positive `delta`. The probe cannot by itself distinguish an independent oscillator from modulation sidebands. Keep it diagnostic-only; name it a nearby residual spectral-line probe until broader evidence supports a stronger claim. Do not change the ±1.5 Hz exclusion or turn the probe into a refusal status.

### 23.5 Required v2.18.9 — coordinator-authored test harness, agent execution only

The coordinator will create the scripts and exact test conditions:
1. SNR comparison at the standard 96 kHz, 2-second, two 1-second block production configuration, against v2.18.8 `est_lsq` on the exact same samples.
2. Nearby residual-line validation across positive and negative, on-bin and off-bin offsets, separate synchronous/free-phase classes, and AM/FM controls.
3. A clear output contract: raw per-trial JSON/CSV, per-class metrics and captured command/log provenance.

The agent is to execute the committed coordinator-authored harness on vovans-PC via the existing `ssh lk` clone, return raw results/logs, and report environment/runtime. It must not redesign the measurement method or silently rewrite the harness. Any proposed changes must be submitted separately for coordinator review.

No production/main/PR changes, no pilot-frequency or clock-gate changes, no playback, hardware or DUT work.


## 24. Coordinator review of v2.18.9 — execution commit 3998788 (17 Nov 2026)

### 24.1 Execution accepted

The coordinator-authored v2.18.9 harnesses were executed unchanged on vovans-PC at test HEAD `7eb4fb9dc3200dc59228b74b213cb342e2748bfb`. Result commit: `399878851f75af528c102a7d521cad9e2ab5d749`, whose sole parent is the test HEAD. The compare from this coordinator branch is 10 commits ahead / 0 behind; changed files are diagnostics only.

- `py_compile`: pass, Python 3.14.4 / NumPy 2.3.5 / SciPy 1.16.3.
- Paired SNR: exit 0, 2:25.27 wall, 794,596 KiB maximum RSS.
- Nearby sweep: exit 0, 8:33.66 wall, 127,004 KiB maximum RSS.
- `git diff --check`: clean.
- Full JSON, stdout/stderr and `/usr/bin/time -v` logs are committed with an execution manifest.

Full review: `measurements/diagnostics/v218-clock-estimator-independent-reproduction/COORDINATOR-REVIEW-v2189.md`.

### 24.2 Paired SNR — metrics differ even with matched segmentation

The corrected test pairs 448 identical 96 kHz, two-second records. Production sees two one-second blocks; LSQ sees the same complete two-second record. All pairs are finite. Diagnostic minus production SNR has mean −7.245 dB, median −3.723 dB, standard deviation 9.584 dB. The pooled regression remains descriptive only.

The definitions are not interchangeable: production local noise uses the median of an 11-bin neighbourhood on the 1 Hz production grid, while the LSQ residual SNR uses a wider ±32 Hz band (excluding ±2 Hz) on the 0.5 Hz grid. Different bandwidths and the LSQ residual's response to nonstationarity produce different values. Do not transfer thresholds by regression and do not change production `CLOCK_MIN_SNR_DB=20`.

The tested 20 dB LSQ floor separates the steady pilot+noise cases (lowest diagnostic SNR 23 dB) from all noise-only cases (all 40 report `tone_missing`), and rejects the half-window / 40%-occupancy cases. Retain 20 dB as a provisional LSQ residual-fit quality floor only; it is not a numerical equivalent of production SNR.

### 24.3 Do not misread the SNR threshold matrix as the full production gate

The v2.18.9 harness calls `dsp.analyze_tone` directly and does not execute
`Rig.run()` or `clock_check()`. The v2.19.0 matrix then called
`_window_stability_reason()` directly and measured: first/tail-quarter
dropouts reject; half-window dropout rejects; 40% occupancy (40% tone, 60%
quiet) **passes**; 40 ms gap passes; 60 ms gap rejects; 200 ms / −6 dB sag
passes.

The 40%-occupancy pass is a demonstrated blind spot in the median-referenced
guard. It does **not** establish that the complete production clock-check
path accepts that capture: onset anchoring, `dsp.analyze_tone`, and the
signal-validity gate were not exercised together. The v2.20.0 test is
specifically tasked with testing that full path.

### 24.4 Candidate line-QC false positives

13/252 steady-white, no-injected-line trials (5.2% of this sample) were reported as `line_contaminated`. The selected peaks span approximately 5.9–47.8 kHz. This is not yet a population false-positive estimate, but it warrants a larger seeded Monte Carlo before line-QC is used as a hard rejection criterion.

### 24.5 Nearby residual-line probe remains diagnostic

The probe detected 904/1440 injected lines (62.8%): 100% at −10/−20 dBc, 87.5% at −30 dBc, 26.4% at −35 dBc and none at −40 dBc. Quiet controls had no hits in this small sample; AM/FM controls fired because modulation produces legitimate sidebands.

Its selected residual peak is on a 0.5 Hz grid and is not sub-bin-refined. Across detections, absolute frequency error median was 0.0735 Hz, p90 0.2735 Hz and max 0.4765 Hz; amplitude error median −0.5 dB, p10 −4.4 dB and worst −12.9 dB. These fields are diagnostic peak metadata, not calibrated measurements. The probe cannot identify whether a component came from an independent oscillator or modulation.

### 24.6 Decisions and v2.19.0

1. Accept execution and preserve raw JSON/log artifacts.
2. Keep production `CLOCK_MIN_SNR_DB=20` unchanged.
3. Keep LSQ `snr_min_db=20` provisional, but remove any wording that calls it numerically equal to production SNR.
4. Reject the v2.18.8 8.8 dB “equivalent threshold”.
5. Keep nearby residual spectral-component detection diagnostic-only; no status change and no change to the ±1.5 Hz exclusion.
6. Next coordinator-authored validation: full-path upstream stability guard controls; larger line-QC false-positive Monte Carlo for line-free white/colored noise plus injected-spur sensitivity controls; and primary LSQ frequency bias with nearby components and AM/FM controls.

No production, main, PR #4, pilot-frequency or clock-gate code was changed. No physical hardware, playback or DUT test was run in v2.18.9.


## 25. Required v2.19.0 — coordinator-authored validation package (17 Nov 2026)

Branch: `agent/v2190-coordinator-authored-validation`, based directly on the updated coordinator review branch. The package is diagnostic-only and contains four files under the diagnostics directory:
- frozen `v2188_estimators.py`;
- `v2190_gate_lineqc_montecarlo.py`;
- `v2190_nearby_primary_fit_bias.py`;
- `V2190-EXECUTION.md`.

The coordinator-authored tests cover:

1. **Production stability guard:** same 96 kHz / 2 s window geometry; steady tone, first/tail-quarter dropouts, half-window dropout, 40% tone occupancy, 40 ms and 60 ms gaps, and a 200 ms −6 dB sag. The aim is to verify the actual median-referenced `Rig._window_stability_reason()`, especially where quiet frames form a majority. This is a candidate boundary check, not a presumed bug.
2. **Line-QC false-positive population:** exact seeded replay of the v2.18.9 252 steady-white cases; 270 additional low-level white-noise cases; 60 colored-noise cases; 60 no-pilot controls; and 48 injected discrete-spur sensitivity controls. Preserve every per-trial status and line result.
3. **Primary-frequency bias:** paired pilot-only baselines and 432 nearby-component trials covering both signs, nine offsets, four levels, and same-initial / independent phase; plus 72 AM/FM controls. Record the actual primary LSQ fitted frequency and its shift relative to the matched baseline, separately from nearby residual-peak metadata.

Agent contract: fetch the exact branch, run `py_compile` on the three Python files, and execute both committed scripts unchanged. Save only the two JSON outputs, full stdout/stderr + `time -v` logs, and environment manifest. On any preflight or runtime failure, stop and return the complete traceback; do not edit code or methodology.

No WAV copy, playback, hardware capture, DUT, production/main/PR #4, pilot-frequency or clock-gate changes.



## 26. v2.19.0 results accepted; v2.20.0 full-path validation prepared (17 Nov 2026)

### 26.1 v2.19.0 execution verified

The agent executed the coordinator-authored harness unchanged at source HEAD
`d292e730dc37fe64c3df06276ccce3d519a913f8`. Result commit
`7cfae8f0459eb5beef53cd562ac26b1497e12cde` is a direct child and adds only
the two JSON outputs, two complete run logs, and execution manifest.
Compilation and both scripts passed (exit 0); neither harness nor frozen
`v2188_estimators.py` was edited. Execution is accepted.

### 26.2 Direct stability-guard outcomes

At 96 kHz / 2 s, the real `Rig._window_stability_reason()` was invoked
directly on synthetic stereo windows:

- first-quarter dropout: reject;
- tail-quarter dropout: reject;
- half-window dropout: reject;
- 40% tone occupancy / 60% quiet: **pass**;
- 40 ms gap: pass; 60 ms gap: reject;
- 200 ms / −6 dB sag: pass.

This proves the 40%-occupancy vector bypasses the median guard. It does not
prove that the complete `Rig.run()` / `clock_check()` path accepts it:
onset anchoring and production tone analysis were not included in this
direct-function experiment. Earlier wording in §24.3 claiming that the
40%-occupancy vector would be rejected upstream is withdrawn.

### 26.3 Line-QC false-positive signal

- Seeded original white-noise replay: 13/252 steady-white, line-free records
  `line_contaminated` (5.16%; Wilson 95% approximately 3.0–8.6%).
- Focused low-level white-noise population: 27/270 (10.0%; Wilson 95%
  approximately 7.0–14.2%).
- Combined: 40/522 (7.66%), but the two populations have different
  pilot/noise-level mixes and must not be treated as one homogeneous rate.
- Colored-noise controls: 0/60 line-contaminated; 8/60 `snr_low`.
- Noise-only controls: 60/60 `tone_missing`.
- Injected-spur controls: 36/48 detected (75%) in this matrix.

These are seeded synthetic results, not a hardware false-positive rate.
No line-QC threshold or production state is changed.

### 26.4 Nearby-line primary-fit bias

The v2.19.0 sweep found the worst observed absolute induced shift of about
0.0649 Hz (about 65 ppm) for independent-phase components at ±0.30–0.35 Hz
and −10 dBc. The agent's 0.78 ppm example at +1.5 Hz / −30 dBc is one
favourable cell, not representative of the worst case. The extreme cells
contain only three trials per signed offset / phase / level, so this is
high-priority but preliminary. Nearby detection remains diagnostic metadata;
it does not change estimator status or guard the primary fitted frequency.

### 26.5 v2.20.0 coordinator-authored package

The package is on
`agent/v2200-coordinator-authored-validation`. It contains three
coordinator-authored scripts and the exact execution contract:

1. `v2200_full_clock_path.py`: synthetic backend, real `Rig.run()` and
   `clock_check()`, clean/40%/50% occupancy, 25% tail dropout, 500 ms head
   dropout, 40/60 ms gaps, and 200 ms / −6 dB sag.
2. `v2200_lineqc_montecarlo.py`: 100 focused seeded repetitions in each of
   nine white-noise pilot/noise cells, retaining the v2.19.0 replay,
   colored-noise, noise-only, and injected-spur controls; per-cell Wilson
   intervals are added.
3. `v2200_nearby_primary_fit_bias.py`: 0.25–1.50 Hz offsets, −10/−20/−30
   dBc, both phase modes, 20 repetitions per absolute-offset cell (10 per
   sign), paired pilot-only baselines, AM/FM controls, and separate
   positive/negative summaries.

The execution contract is `V2200-EXECUTION.md`. The agent's role is
execution-only: compile and run the scripts unchanged, return complete
JSON/logs/manifest, and stop on the first failure. No real hardware
playback/capture, DUT, WAV, production/main/PR #4, pilot-frequency,
clock-gate, or threshold changes are authorized.



## 27. v2.20.0 execution accepted; substantive review (18 Nov 2026)

### 27.1 Execution provenance

Agent result commit:
`43f1cb6cbbb973e5ba05260fe2dc832e2f38f896`, direct child of the pinned
execution-contract head `d5e29814dc6746a7fc196492539b4cb7959ff7ab`.
GitHub compare confirms exactly seven added result artifacts and one commit
ahead. Manifest identifies frozen test source
`8fcbe642132d957edc239f52e85edbe8e854b698`; `py_compile` ×4 and all three
runs exit 0; `git diff --check` clean. No harness, frozen estimator or
production source was edited.

Runtimes / maximum RSS from the manifest:
- Full production path: 1.08 s / 167,840 KB.
- Line-QC Monte Carlo: 6:58.17 / 124,956 KB.
- Nearby primary-fit bias: 8:03.01 / 131,988 KB.

Execution accepted; algorithmic acceptance remains separate.

### 27.2 Confirmed production clock-preflight false accept

The 40%-occupancy front-loaded vector (0.8 s tone followed by 1.2 s quiet
inside the 2 s measurement window) passes the real `Rig.run()` and
`clock_check()` path:
- `Rig.run()`: pass; `clock_check()`: returned; `ok=True`.
- `signal_ok=True`; local SNR 46.90 dB L / 46.81 dB R.
- Frequency estimates +0.087 ppm L / −0.040 ppm R.
- The analyzer emits inter-block phase coherence warnings (0.585 / 0.767),
  but this does not affect the signal-validity gate or overall result.

This is a demonstrated production-path false accept of an incomplete
measurement window. The median-referenced stability guard misses it, and
phase instability is warning-only for this gate. The next test must map the
acceptance boundary by duty fraction, time placement, block distribution,
onset shift and phase coherence before selecting a fix.

Other cases: clean PASS; 50% front occupancy reject; 25% tail dropout reject;
40 ms gap PASS; 60 ms gap reject; 200 ms / −6 dB sag PASS. The 500 ms head
dropout rejects, but onset shifts from the 0.137 s DAC delay to 1.030 s and
the selected window then contains about 0.85 s of post-program silence. This
is not an isolated test of a head dropout; it combines onset re-anchoring
with a too-short generated tail.

### 27.3 Frozen v2.18.8 LSQ line-QC false alarms

These results belong to the independent diagnostic LSQ estimator, not
production `clock_check()`:
- Focused line-free white noise: 86/900 `line_contaminated` (9.56%;
  Wilson 95% approximately 7.8–11.7%).
- Original replay: 13/252 (5.16%; Wilson 95% approximately 3.0–8.6%).
- 95/99 white-noise false flags were exactly three-bin clusters. However,
  all 42/48 injected-spur detections were also three-bin clusters. A naive
  four-bin minimum would remove most false flags but also the observed
  injected-spur hits; do not apply it.
- False peaks span 658.5–47,843.5 Hz, median 20,701.5 Hz; `line_dbc`
  median −33.1 dBc (range −34.9 to −27.6 dBc).
- Stable injected spurs detected: 42/48 (87.5%).

Keep the frozen `QC_CFG` unchanged. Test a two-block frequency-persistence
candidate against white/colored-noise controls and injected stable spurs.

### 27.4 Frozen v2.18.8 LSQ nearby-line bias

This is also independent-estimator evidence, not production
`clock_check()`:
- Total 1,512 records: 360 baselines and 1,080 nearby-line records.
- Nearby probe hits: 995/1,080 (92.1%).
- Worst induced shift: 67.03 ppm, independent phase, +0.40 Hz, −10 dBc.
  That cell has 20 trials, all 20 probe hits, and all 20 estimator statuses
  remain `ok`. The agent's quoted 61 ppm at +0.25 Hz is valid for that
  cell but is not the overall maximum.
- Other independent-phase −10 dBc maxima: +0.30 Hz 65.70 ppm;
  +0.35 Hz 65.42 ppm; +0.25 Hz 61.07 ppm; +0.50 Hz 58.06 ppm.
- AM triggered the probe 36/36; FM 34/36. The probe does not classify
  independent oscillators separately from modulation sidebands.

Keep nearby detection diagnostic-only pending a longer-window comparison.
No refusal state or threshold change is authorized.

### 27.5 v2.21.0 coordinator-authored follow-up

Branch: `agent/v2210-coordinator-authored-validation`.

- `v2210_clock_quality_boundary.py`: real production `Rig.run()` /
  `clock_check()` across front/centre/back occupancy, front duty fractions
  around 50%, and split/burst patterns; records onset, SNR, phase coherence,
  warnings and final status.
- `v2210_lineqc_two_block.py`: unchanged frozen v2.18.8 estimator; compares
  full-window line-QC with same-frequency hits in both 1 s blocks at
  0.5/1/2 Hz tolerances; 540 white, 60 colored, 48 stable-spur controls.
- `v2210_nearby_duration_comparison.py`: paired 2 s / 4 s windows, offsets
  0.25–1.00 Hz, −10/−20 dBc, both phase modes, 16 trials per absolute-offset
  cell (eight per sign).

Execution contract: `V2210-EXECUTION.md`. Agent remains execution-only.
No hardware, DUT, WAV, production/main/PR #4, pilot-frequency, clock-gate,
threshold or production-source changes are authorized.

Operational note from the agent: remote push authentication was configured
with `credential.helper=store` and `~/.git-credentials` mode 0600. Git's
`store` helper keeps credentials in plaintext; mode 0600 restricts local
file access but does not encrypt the token. Keep any token least-privileged
and rotate/revoke it when no longer needed.



## 28. v2.21.0 execution accepted; substantive review (18 Nov 2026)

### 28.1 Execution verified

Agent result `416a06be49d7a4a87217e05d672e4a299fe764b7` is a direct child of
the exact task-branch head `b676663921e2a0fc61e70f987b5fcf77d4c26f7c`.
GitHub compare confirms exactly seven output files. The manifest pins test
source `d42c6b4a3677c8f0521d41926b8782c6a4d23447`; `py_compile` ×4 and all
three scripts exit 0; `git diff --check` clean. No harness, frozen
estimator or production source was edited.

### 28.2 Production full-path occupancy false accept confirmed

21 cases: 12 `Rig.run()` passes and 9 rejects. Front-contiguous tone
occupancy 20–49% passes the real `Rig.run()` / `clock_check()` path with
`ok=True`; 50–90% rejects in this tested matrix. Tested 40% centre, rear,
split across blocks, two bursts and four bursts also pass. Tested 50% and
60% split cases reject.

The 20% case only barely meets local-SNR gate (20.29 / 20.32 dB) and
returns `ok=True`; the 40% centre/split cases are similarly near 20.3 dB.
Some other 40% placements produce much higher SNR. Warnings about onset
fallback, non-coherent frequency, or low inter-block phase coherence do not
affect the current overall clock-check result.

The median-reference mechanism explains the inversion: when quiet frames
are a majority, the median shifts into the quiet portion, hiding silence as
the normal level. The run-length threshold of 50 ms may also allow high-duty
pulse trains with many shorter gaps; that was not tested in v2.21.0.

This is a production-path false acceptance of a partial measurement window.
It is confirmed in the synthetic backend, not yet in physical hardware. The
next diagnostic step must test sub-50-ms pulse trains, per-block tone
presence, and legitimate continuous clock offsets before any fix is chosen.
Do not gate simply on the current raw phase-coherence value: real DAC/ADC
clock mismatch can also reduce it.

### 28.3 Two-block line-QC persistence candidate

Frozen independent LSQ v2.18.8 estimator:
- line-free white: 56/540 full-window flags (10.37%); 0/540 when requiring
  a line in both blocks within 0.5 Hz;
- colored noise: 1/60 full-window; 0/60 persistent;
- stable injected spurs: 31/48 full-window detections; 28/48 persistent;
- all 24 stable −30 dBc injected spurs persist; at −35 dBc, 7/24 full
  window and 4/24 persistent.

Important methodology qualification: same `floor_bins=161` was used at
0.5 Hz/bin for the 2 s FFT and 1 Hz/bin for each 1 s FFT. Thus physical
local-floor bandwidth changed from about ±40 Hz to ±80 Hz in block mode.
The apparent false-positive reduction needs a controlled repeat with
physical-width normalization (approximately 81 bins for the 1 Hz FFT).
No QC threshold changes.

### 28.4 Nearby-line duration result

Frozen independent LSQ v2.18.8 estimator:
- pilot-only absolute fit-error p90: 0.0659 ppm (2 s) → 0.0241 ppm (4 s);
- paired independent-phase injected-line records improved in 163/192
  cases, worsened in 29/192;
- paired same-initial-phase records improved in 128/192, worsened in 64/192.

Examples: independent phase, Δ=0.25 Hz / −10 dBc improved from median
47.63 to 13.40 ppm; Δ=0.40 Hz / −10 dBc from 50.65 to 5.36 ppm. In
same-initial-phase cases, Δ=0.25 Hz / −10 dBc worsened 12.63 to 20.05 ppm,
and Δ=0.75 Hz / −20 dBc worsened 0.046 to 2.51 ppm.

A 4 s window improves pilot-only precision and often reduces the nearby-line
bias, but it is not a universal fix. Keep production duration unchanged.

### 28.5 Coordinator review and v2.22.0 plan

Full review: `measurements/diagnostics/v218-clock-estimator-independent-reproduction/COORDINATOR-REVIEW-v221.md`.

The next coordinator-authored diagnostic package will:
1. Map full-path acceptance under periodic pulse trains with gaps shorter
   than 50 ms, instrument per-block SNR and levels, and test continuous
   tones with controlled offsets through ±100 ppm. Evaluate candidate
   occupancy metrics offline only.
2. Repeat two-block line-QC with physically normalized local-floor width;
   include off-bin, stable and half-window-only spurs.
3. Compare frozen LSQ durations 2/3/4/6 s on close-offset paired tests,
   retaining phase modes and reporting absolute ppm shifts.

No production/main/PR #4, pilot-frequency, clock-gate, threshold or frozen
estimator changes are authorized.



## 29. v2.22.0 coordinator-authored diagnostic package prepared (18 Nov 2026)

Pinned test-source commit:
`5d5e9d1eaee6cc9e0b427e5f6e1a5d738b1a3a78`.

Task branch:
`agent/v2220-coordinator-authored-validation`.
Execution contract:
`measurements/diagnostics/v218-clock-estimator-independent-reproduction/V2220-EXECUTION.md`.

Three coordinator-authored scripts:
1. `v2220_clock_occupancy_diagnostics.py`: synthetic full
   `Rig.run()` / `clock_check()` tests for contiguous/placed windows,
   periodic pulse trains with short gaps around the 50 ms run threshold,
   frequency offsets from −110 to +110 ppm, AM controls, per-block tone/SNR
   diagnostics, and 2 ms frame occupancy relative to upper quantiles.
2. `v2220_lineqc_physical_width.py`: unchanged frozen v2.18.8 estimator;
   compares line-QC persistence with legacy 161-bin and physically
   normalized 81-bin per-block local floors. Includes 900 white-noise
   controls, colored noise, stable on-/off-bin spurs, and first-/second-half
   only spurs.
3. `v2220_nearby_duration_robustness.py`: paired 2/3/4/6 s fits with
   consistent seeds, offsets 0.25/0.35/0.40/0.50/0.75 Hz, −10/−20 dBc,
   both phase modes, and eight positive plus eight negative trials per
   offset/phase/level cell.

The agent contract is execution-only. It may compile and run the exact
committed scripts, save the three complete JSON outputs, three full logs,
and manifest, then commit only those seven result files. It must stop on
the first failure. No hardware/playback/DUT/WAV, no production/main/PR #4,
no clock-gate/threshold changes and no estimator edits are authorized.
