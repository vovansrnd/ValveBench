# v2.25.1 — execution contract: read-only characterization of the existing probe

## Objective

Characterize only the already existing 10-second file /tmp/v2250-probe.wav on vovans-PC. The inventory report observed a variable 8–20 Hz component near 12 Hz and a comparatively stable band above 300 Hz. This task measures its frequency, amplitude, L/R relation, temporal stability, harmonics, and noise close to the 1 kHz pilot band.

This does NOT identify the physical source, certify the input path, or authorize another capture.

## Absolute restrictions

- Do NOT run arecord, aplay, ./run.sh cli cards, ALSA enumeration, mixer tools, or any hardware command.
- Do NOT access/open an audio device.
- Do NOT record, playback, or generate any signal.
- Do NOT change controls, cabling, connections, DUT state, sample rate, or configuration.
- Do NOT recreate /tmp/v2250-probe.wav if it is absent.
- Do NOT copy or commit the WAV into Git.
- Do NOT edit the frozen estimator or any production file.
- Run only the coordinator-authored analysis script unchanged.

If the existing WAV is absent, unreadable, the wrong format, or fails validation, stop and publish a BLOCKED report. No alternative source file and no recapture.

## Pinned source

Repository: vovansrnd/ValveBench

Task branch: coordinator/v2251-probe-characterization

Result branch: agent/v2251-probe-characterization

Pinned SHA (script and execution contract):

8e530f8f4447f288ffa73cdcd5544ff70c610935

Analysis script:
measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2251_probe_characterization.py

Input:
 /tmp/v2250-probe.wav — reported by the v2.25.0 inventory as 10 seconds, S24_3LE, 96 kHz, stereo. Treat this only as an expected description; verify actual file metadata and hash.

## Worktree and preflight

Use lk and the existing clone ~/ValveBench-compute/ValveBench. Do not use the user's ThinkPad clone.

Create a detached isolated worktree at the exact pinned SHA. Check whether the proposed worktree path exists first; never delete or overwrite an unknown directory. Set PYTHONPYCACHEPREFIX outside the worktree. Verify repository identity, exact HEAD and clean tracked state.

Before running Python:
1. Check only file metadata for /tmp/v2250-probe.wav (test -f, stat, file, and sha256sum). Do not access ALSA.
2. If missing: stop immediately and report BLOCKED. Do not recreate it.
3. If present: record exact path, bytes, SHA-256, owner/mode, sample format, sample rate, channels, frame count, and duration as verified from the WAV header.

## Compile and run

Compile:

~~~bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
export PYTHONPYCACHEPREFIX="$HOME/.cache/valvebench-v2251-pycache"
python3 -m py_compile "$D/v2251_probe_characterization.py"
~~~

Run exactly once:

~~~bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
/usr/bin/time -v python3 "$D/v2251_probe_characterization.py" \
  --wav /tmp/v2250-probe.wav --output "$D" \
  > "$D/v2251-probe-characterization-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2251-probe-characterization-run.log"
test "$rc" -eq 0 || exit "$rc"
~~~

If compile or execution fails, preserve the full error and stop. Do not edit the script or rerun it.

## Required analysis

Use the JSON as the source of truth. Report:
- exact input metadata, bytes and full SHA-256;
- channel peaks, clipping counts, DC means, full-band RMS and sine-equivalent level (keep true RMS distinct from sine-equivalent dBFS);
- 8–20 Hz band RMS and dominant peak per channel, joint peak frequency, refined sinusoidal frequency/amplitude/phase per channel;
- L/R correlation in the 8–20 Hz filtered band and coherence at the common low-frequency peak;
- harmonic-band peaks around multiples of the common low-frequency peak (descriptive only);
- per 0.5-second block levels for full-band, 8–20 Hz, above 300 Hz, and 968–1032 Hz, L/R separately;
- median/mean/max PSD in 968–1032 Hz from the captured signal (no pilot is present);
- any time variation, frequency drift, or channel asymmetry supported by the block and spectral data.

Clearly distinguish:
1. verified properties of the WAV;
2. spectral observations;
3. hypotheses that cannot be resolved from the waveform.

Do NOT call the 12 Hz component mains hum, rumble, mechanical vibration, DUT noise, or a particular source unless the waveform itself proves only a narrower descriptive property (e.g. a harmonic family). Even harmonic structure does not identify the physical source.

The report must explicitly state that this characterization cannot establish that the analog path is known/quiet or approve a 384-second capture.

## Outputs and publication

Successful analysis: commit exactly:
1. v2251-probe-characterization.json
2. v2251-probe-characterization-run.log
3. V2251-EXECUTION-MANIFEST.md
4. V2251-AGENT-REPORT.md

Blocked/missing/invalid input: commit only:
1. v2251-probe-characterization-run.log (if a command produced one; otherwise v2251-probe-input-check.log)
2. V2251-EXECUTION-MANIFEST.md
3. V2251-AGENT-REPORT.md

Never include /tmp/v2250-probe.wav in Git.

Manifest must include pinned SHA, exact worktree HEAD/status, host and software versions, source WAV path/metadata/hash if present, exact command, exit status, wall time, max RSS, output JSON byte size/hash if generated, and a statement that no ALSA/hardware/playback/capture command was issued in this task.

Before publication, confirm the result branch is based on the pinned source history, stage only the allowlisted files, inspect staged names, and push normally. No force push, reset, rebase, or unrelated cleanup. If the remote branch moved or push is rejected, stop and report the exact condition.

## Definition of done

Either a validated read-only characterization of the existing file, or an honest BLOCKED report if that file is no longer present/valid. No new recording under any circumstances.
