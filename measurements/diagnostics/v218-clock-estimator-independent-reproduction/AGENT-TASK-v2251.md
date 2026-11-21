# AGENT TASK — ValveBench v2.25.1
## Read-only characterization of the existing E-MU 0404 10-second probe

**Role:** execute the coordinator-authored offline analysis script on lk and publish evidence. No hardware access is permitted in this task.

## 1. Source and branches

Repository: vovansrnd/ValveBench

Task branch: coordinator/v2251-probe-characterization

Result branch: agent/v2251-probe-characterization

Pinned source SHA (only permitted script/contract version):
1dcacb6e645bb866e8ead6eea2b9b115c96ab83b

Script:
measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2251_probe_characterization.py

Execution contract:
measurements/diagnostics/v218-clock-estimator-independent-reproduction/V2251-EXECUTION.md

Read the contract before execution. Use only the exact pinned SHA.

## 2. Context

The v2.25.0 inventory on vovans-PC found the only capture input to be E-MU 0404 USB, native 96 kHz stereo S24_3LE. A 10-second probe was saved at /tmp/v2250-probe.wav. The agent correctly stopped the intended 384-second capture because the analog path was not known and a prominent low-frequency component near 12 Hz caused block RMS variation.

This task exists only to understand that already captured 10-second WAV. It does not authorize the agent to identify/reconfigure the analog path or perform any new capture.

## 3. Absolute safety rules

- No arecord, aplay, ALSA enumeration, mixer inspection, or hardware calls.
- No recording, playback, generated test signal, device open, or hardware access.
- No cable changes, mixer/gain/rate changes, or DUT changes.
- Do not recreate the WAV if it is absent.
- Do not use another WAV or any synthetic replacement.
- Do not edit the coordinator script, frozen estimator, or production files.
- Do not run any additional experiment on hardware.
- Do not commit the raw WAV.

If /tmp/v2250-probe.wav is absent or invalid, stop and publish a BLOCKED report. No new capture.

## 4. Execution

Use host lk and clone ~/ValveBench-compute/ValveBench. Never use the user's ThinkPad clone.

Create an isolated detached worktree at exact SHA 1dcacb6e645bb866e8ead6eea2b9b115c96ab83b. Check whether the target worktree path exists; do not overwrite unknown directories. Set PYTHONPYCACHEPREFIX outside the worktree. Verify repository identity, HEAD and clean tracked state.

First perform only metadata checks on the existing file:
- test -f /tmp/v2250-probe.wav
- stat and file
- sha256sum

Record owner, mode, bytes and full SHA-256. Do not run any ALSA commands.

If present, verify the WAV header and compile:

~~~bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
export PYTHONPYCACHEPREFIX="$HOME/.cache/valvebench-v2251-pycache"
python3 -m py_compile "$D/v2251_probe_characterization.py"
~~~

Then execute the analyzer exactly once:

~~~bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
/usr/bin/time -v python3 "$D/v2251_probe_characterization.py" \
  --wav /tmp/v2250-probe.wav --output "$D" \
  > "$D/v2251-probe-characterization-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2251-probe-characterization-run.log"
test "$rc" -eq 0 || exit "$rc"
~~~

Do not change the script if it fails. Preserve traceback and stop. Do not rerun a successful run merely because SSH disconnects; inspect artifacts and log first.

## 5. Validation

For successful execution, validate:
- valid JSON;
- 2 channels, 96 kHz, duration 9 seconds or longer;
- block count consistent with 0.5-second blocks;
- all numeric fields finite;
- source file bytes and SHA-256 in JSON match preflight metadata;
- git diff --check;
- no source/production changes.

The raw WAV remains outside Git and is never included in result artifacts.

## 6. Report requirements

Create V2251-AGENT-REPORT.md and V2251-EXECUTION-MANIFEST.md.

The report must use actual JSON values and clearly separate observations from interpretations. Include:
- source metadata and file hash;
- full-band true RMS and sine-equivalent dBFS separately, peak and clipping;
- DC offset;
- 8–20 Hz filtered level and dominant peak per channel;
- refined low-frequency sinusoidal fit frequency, amplitude and phase per channel;
- 8–20 Hz L/R correlation and coherence at the common peak;
- harmonic-band peaks (descriptive only; do not infer source identity);
- 0.5-second block level ranges and tables for full-band, 8–20 Hz, above 300 Hz and 968–1032 Hz, L/R separately;
- mean/median/max PSD in 968–1032 Hz;
- time variation and L/R asymmetry supported by evidence;
- any script limitations or invalid/ambiguous measurements.

Explain whether the observed 12 Hz feature behaves more like a stable narrowband component or a varying/broadband low-frequency component. If frequency/amplitude vary by block, quantify it. Do not guess its physical source.

Do not call it mains hum, mechanical rumble, DUT noise or any named source without evidence. Even a harmonic series does not prove the source.

Explicitly state:
- only the already existing 10-second file was read;
- no hardware, ALSA, playback or capture command was issued for this task;
- the analysis cannot certify that the analog connection is known/quiet;
- the 384-second measurement remains blocked pending the user's own identification/confirmation of the physical signal path;
- no production code or estimator was changed.

If file missing/invalid, create a concise BLOCKED report and manifest with exact evidence. No fake JSON.

## 7. Publication allowlist

On success, commit exactly:
1. v2251-probe-characterization.json
2. v2251-probe-characterization-run.log
3. V2251-EXECUTION-MANIFEST.md
4. V2251-AGENT-REPORT.md

On blocked input, commit only:
1. v2251-probe-input-check.log (or run log if an execution actually started)
2. V2251-EXECUTION-MANIFEST.md
3. V2251-AGENT-REPORT.md

Only allowlisted files may be staged. Do not commit the source WAV. Verify result branch ancestry and current remote head before committing. Push normally. No force push, reset, rebase, or cleanup of unrelated files. If branch moved or push is rejected, stop and report.

## 8. Return to coordinator

Return:
- result commit SHA and GitHub URL;
- exact changed file list;
- JSON size and SHA-256 if produced;
- short key findings: low-frequency frequency/amplitude stability, L/R coherence, 1 kHz-adjacent PSD, and block variation;
- explicit confirmation that no hardware or ALSA operations were performed and no recording was made.

## Definition of done

A reproducible offline characterization of the existing probe, or an honest BLOCKED result if the old file is gone. Absolutely no new capture.
