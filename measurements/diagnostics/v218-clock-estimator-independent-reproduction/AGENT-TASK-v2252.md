# AGENT TASK — ValveBench v2.25.2
## Raw-domain analysis: is the 15.38 Hz observation a real coherent component?

**Role:** run the coordinator-authored offline analyzer on the existing probe WAV and prepare an evidence-based report. No hardware access is permitted.

## 1. Branches and pinned source

Repository: vovansrnd/ValveBench

Task branch: coordinator/v2252-raw-probe-transient

Result branch: agent/v2252-raw-probe-transient

Pinned SHA (script + execution contract):
c9fbaae7cf427934f71776bad097c1a892f1d1ae

Script:
measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2252_raw_probe_transient_analysis.py

Execution contract:
measurements/diagnostics/v218-clock-estimator-independent-reproduction/V2252-EXECUTION.md

Read the contract before execution. Use only the exact pinned SHA.

## 2. Background

The v2.25.0 10-second E-MU 0404 probe had a large first 0.5-second full-band transient. v2.25.1 applied a zero-phase 8–20 Hz filter to the entire record, then fitted 2-second windows. It reported a 15.38 Hz peak and a large sine projection in 0–2 seconds, falling by roughly 39 dB in 1–3 seconds.

This does not yet prove a coherent 15.38 Hz tone. The first raw 0.5-second transient, spectral leakage, bandpass edge behaviour, or filter ringing could influence the 2-second result.

v2.25.2 tests the same existing raw samples directly, without using the filtered signal for its principal analysis.

## 3. Absolute safety restrictions

- Read only /tmp/v2250-probe.wav.
- No arecord, aplay, ALSA enumeration, mixer tools, device opens, hardware, playback, recording, or generated signals.
- Do not recreate the WAV if absent.
- Do not substitute another file or synthetic data.
- Do not alter controls, cabling, DUT or project configuration.
- Do not edit this script, frozen estimator, or production.
- Do not commit the source WAV.

If the file is absent or invalid, stop and publish a BLOCKED result. No recapture under any circumstances.

## 4. Execution host and worktree

Use lk and existing clone ~/ValveBench-compute/ValveBench. Do not use the user's ThinkPad clone.

Create a new isolated detached worktree at the exact pinned SHA. Inspect whether the proposed worktree path exists before creating it; never delete or overwrite an unknown directory. Set PYTHONPYCACHEPREFIX outside the worktree. Record repo identity, exact HEAD, tracked/untracked status and environment.

Before Python, check the existing WAV using only:
- test -f
- stat
- file
- sha256sum

If missing, stop immediately. If present, record owner/mode/bytes/full SHA and inspect WAV header. No ALSA commands.

## 5. Compile and run once

Compile:

~~~bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
export PYTHONPYCACHEPREFIX="$HOME/.cache/valvebench-v2252-pycache"
python3 -m py_compile "$D/v2252_raw_probe_transient_analysis.py"
~~~

Run once:

~~~bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
/usr/bin/time -v python3 "$D/v2252_raw_probe_transient_analysis.py" \
  --wav /tmp/v2250-probe.wav --output "$D" \
  > "$D/v2252-raw-probe-transient-analysis-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2252-raw-probe-transient-analysis-run.log"
test "$rc" -eq 0 || exit "$rc"
~~~

Do not edit or rerun the harness on failure. Preserve full diagnostics and stop.

## 6. Required validation

Successful JSON must:
- parse;
- identify 96 kHz stereo, at least 9 seconds;
- contain 20 non-overlapping 0.5 s blocks for the 10 s probe;
- contain 0.5 s windows at 125 ms hop and raw 2 s windows at 1 s hop;
- contain finite numeric fields;
- preserve exact source WAV byte count and SHA-256 from preflight.

Record JSON bytes and full SHA-256. Run git diff --check. No source/production modifications.

## 7. Required technical report

Use raw JSON values. For every major finding, show its evidence and distinguish observation from interpretation.

Address:
1. Does the raw first 0.5 s contain a narrow peak around 15.38 Hz, or does it contain broader 8–20 Hz energy?
2. How quickly does raw 8–20 Hz band power fall across successive 0.5 s blocks?
3. Does a fixed 15.380859375 Hz sinusoid explain a meaningful fraction of the linearly detrended raw energy in each block/window?
4. Is the absolute-time fitted phase consistent across adjacent windows while amplitude is present? If it is not, do not describe it as a stable sinusoid.
5. Does the 12 Hz fixed-frequency control explain less/more energy than the 15.38 Hz model?
6. Do raw 2-second windows reproduce the v2.25.1 filtered-window finding?
7. Is the reported 0–2 s filtered amplitude potentially affected by the whole-record filter's beginning-edge response or by the first 0.5 s transient?
8. How do L/R results compare?
9. Reconfirm the 968–1032 Hz noise PSD L/R median/mean/max.

Important:
- Zero-padding interpolates the plotted spectrum; it does not improve the true frequency resolution of a 0.5 s block.
- A large fixed-frequency projection on a nonstationary transient is not proof of a physical sinusoid.
- Do not infer or name the physical source.
- Do not say the 15.38 Hz component is a decaying physical oscillator unless raw data show narrowband concentration, coherent phase and a credible decay envelope.
- The source remains unidentified; this task cannot certify the input path or authorize a longer capture.

The report must explicitly say:
- only the pre-existing WAV was read;
- no hardware/ALSA/playback/recording command was issued;
- no new capture occurred;
- the physical signal path remains unidentified;
- no production or estimator code changed.

## 8. Publication

Success: commit exactly:
1. v2252-raw-probe-transient-analysis.json
2. v2252-raw-probe-transient-analysis-run.log
3. V2252-EXECUTION-MANIFEST.md
4. V2252-AGENT-REPORT.md

Blocked input: commit only:
1. v2252-input-check.log
2. V2252-EXECUTION-MANIFEST.md
3. V2252-AGENT-REPORT.md

Do not commit the WAV or unrelated files. Stage only the allowlist. Verify the pinned SHA is an ancestor and result branch is safely based on the task branch. Push normally, without force. No reset/rebase/clean of unrelated files. If the remote branch moved or push is rejected, stop and report exact state.

## 9. Return to coordinator

Return:
- commit SHA and URL;
- exact changed-file list;
- JSON bytes and SHA if generated;
- key evidence: 8–20 Hz band power by initial blocks, tone-fit energy reduction, phase continuity, raw 2-second results, L/R comparison;
- explicit no-hardware/no-capture confirmation.

Definition of done: raw-domain evidence from the same existing probe, or an honest BLOCKED result. No new recording.
