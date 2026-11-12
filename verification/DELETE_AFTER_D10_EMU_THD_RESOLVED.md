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
