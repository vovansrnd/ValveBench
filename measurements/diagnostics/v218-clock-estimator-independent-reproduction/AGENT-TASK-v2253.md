# AGENT TASK — ValveBench v2.25.3
## Time-resolved characterization of the 968–1032 Hz L/R noise asymmetry

**Role:** run the coordinator-authored offline analyzer on the old probe WAV and report. This is not a hardware task.

## 1. Repository refs

Repository: vovansrnd/ValveBench

Task branch: coordinator/v2253-pilotband-noise-characterization

Result branch: agent/v2253-pilotband-noise-characterization

Pinned SHA (only permitted analyzer/contract version):
ae65d7caf41758c9275a3af5c0db68ff3f208dba

Script:
measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2253_pilotband_noise_characterization.py

Execution contract:
measurements/diagnostics/v218-clock-estimator-independent-reproduction/V2253-EXECUTION.md

Read the execution contract first. Use the exact pinned SHA only.

## 2. Background

v2.25.1/v2.25.2 characterized the existing 10-second E-MU 0404 probe and found:
- 968–1032 Hz median PSD: L −124.47 vs R −141.09 dBFS²/Hz (16.62 dB difference);
- mean PSD: L −106.02 vs R −123.03 dBFS²/Hz;
- max PSD: L −92.20 vs R −109.21 dBFS²/Hz.

The mean is about 18 dB above the median in both channels. We need to know whether the L/R median difference is broadband and persistent, or whether a few time-localized/narrowband components dominate these summaries.

## 3. Absolute restrictions

- Read only /tmp/v2250-probe.wav.
- NO ALSA, arecord, aplay, mixer tools, hardware calls, device opens, playback, recording or generated signals.
- Do not recreate the WAV if it is absent.
- Do not use another file or synthetic substitute.
- Do not change cabling, controls, DUT, hardware settings or project configuration.
- Do not edit the coordinator-authored analyzer or any production file.
- Do not commit the WAV.

If the existing WAV is absent/invalid, stop and publish a BLOCKED report. No new capture.

## 4. Execution environment

Use lk and the existing clone ~/ValveBench-compute/ValveBench. Do not use the user's ThinkPad clone.

Create an isolated detached worktree at exact pinned SHA PINNED_SHA_TO_BE_FILLED. Check whether the worktree path already exists; never overwrite unknown directories. Put PYTHONPYCACHEPREFIX outside the worktree. Verify repository identity, exact HEAD and clean tracked state.

Before Python, perform metadata-only checks on the existing WAV: test -f, stat, file, sha256sum. Record exact bytes, owner/mode, format, sample rate, channel count, frames, duration and full SHA-256. No ALSA commands.

## 5. Compile and execute once

Compile:

~~~bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
export PYTHONPYCACHEPREFIX="$HOME/.cache/valvebench-v2253-pycache"
python3 -m py_compile "$D/v2253_pilotband_noise_characterization.py"
~~~

Run:

~~~bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
/usr/bin/time -v python3 "$D/v2253_pilotband_noise_characterization.py" \
  --wav /tmp/v2250-probe.wav --output "$D" \
  > "$D/v2253-pilotband-noise-characterization-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2253-pilotband-noise-characterization-run.log"
test "$rc" -eq 0 || exit "$rc"
~~~

Stop on any error; preserve complete log/traceback. Do not repair the script and do not rerun after failure.

## 6. Validation and report

Validate JSON structure, finite numeric data, 96 kHz stereo, >=9s duration, exact source bytes/SHA, expected 9 two-second windows at 1s hop, and all per-window fields. Record JSON bytes and full SHA-256. Run git diff --check. No source or production changes.

The technical report must answer the questions in V2253-EXECUTION.md using actual JSON:
- Does the L/R median PSD asymmetry persist across all/most windows?
- Is the mean/median gap dominated by specific narrow peaks? List frequencies, levels, prominence and which windows/channels contain them.
- Are any peaks shared/coherent in L/R?
- Is the L channel floor elevated throughout the band, or only in sub-bands?
- How does the peak/median and mean/median relationship evolve through time?
- What can be concluded about this single capture's pilot-adjacent noise and what remains unknown?

Do not assign a physical source. Overlapping windows are not independent. State clearly that this one unknown-path capture cannot establish the source or justify hardware/production changes.

## 7. Publication allowlist

Success: exactly four files:
1. v2253-pilotband-noise-characterization.json
2. v2253-pilotband-noise-characterization-run.log
3. V2253-EXECUTION-MANIFEST.md
4. V2253-AGENT-REPORT.md

Blocked: only:
1. v2253-input-check.log
2. V2253-EXECUTION-MANIFEST.md
3. V2253-AGENT-REPORT.md

Do not commit WAV or unrelated files. Verify result branch base/ancestry, stage only allowlist, inspect staged names, push normally, and verify remote HEAD. No force push, reset, rebase or unrelated cleanup. If branch moved or push rejected, stop and report.

## 8. Return to coordinator

Return result commit SHA/URL, exact changed file list, JSON bytes/SHA if created, core per-window findings and explicit no-hardware/no-recording confirmation.
