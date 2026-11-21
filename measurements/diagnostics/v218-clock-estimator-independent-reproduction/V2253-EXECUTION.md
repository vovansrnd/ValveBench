# v2.25.3 — execution contract: time-resolved pilot-band noise characterization

## Objective

Characterize the L/R noise asymmetry measured in v2.25.1/v2.25.2 in the 968–1032 Hz band. Determine whether the L channel's higher PSD median/mean is a stable broadband floor, a small number of narrow spectral components, or a time-localized event.

Use only the already existing /tmp/v2250-probe.wav. The physical connection remains unknown. This task cannot identify the source, certify the path, or authorize another capture.

## Absolute restrictions

- Read only /tmp/v2250-probe.wav.
- No ALSA enumeration, arecord, aplay, mixer inspection, hardware calls, device opens, playback, recording, or generated signals.
- Do not recreate the WAV if missing; do not substitute another input.
- Do not alter cables, controls, DUT, device settings, or configuration.
- Do not edit the analyzer or production code.
- Do not commit the WAV.

If the old file is missing/invalid, stop and publish BLOCKED evidence. Absolutely no new capture.

## Pinned source

Repository: vovansrnd/ValveBench

Task branch: coordinator/v2253-pilotband-noise-characterization

Result branch: agent/v2253-pilotband-noise-characterization

Pinned SHA is recorded in AGENT-TASK-v2253.md after freezing.

Script:
v2253_pilotband_noise_characterization.py

Input:
 /tmp/v2250-probe.wav (expected 96 kHz stereo, 10 s; verify actual header and hash).

## Worktree and preflight

Use lk and existing clone ~/ValveBench-compute/ValveBench, not the user's ThinkPad clone.

Create a detached isolated worktree at the exact pinned SHA. Check if the target worktree path exists before creating; never overwrite unknown directories. Set PYTHONPYCACHEPREFIX outside the worktree. Verify repository identity, HEAD and clean tracked state.

Before Python, use only test -f, stat, file and sha256sum on the existing WAV. If absent, stop. If present, record owner, mode, byte size, full SHA-256, sample rate, channels, sample format, frame count and duration.

## Compile and run once

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

If compile/runtime fails, preserve the full diagnostic and stop. Do not repair the harness or rerun after failure. Inspect log/artifacts before publication.

## Analysis requirements

Use JSON values as canonical. Report:

### Full 10-second record
- L/R PSD median, mean, p90 and max in 968–1032 Hz.
- L/R mean-to-median and p90-to-median ratios.
- Integrated noise power/RMS over the band.
- Frequency of the maximum bin.
- Count of bins at least +6 dB and +10 dB above the band median.
- Prominent narrow peaks from the full-record Hann periodogram: frequency, PSD, local baseline and prominence.
- L/R magnitude-squared coherence at each prominent peak frequency.

### Time-resolved windows
There are nine overlapping 2-second windows at 1-second hop. For each:
- L/R median, mean, p90, max PSD and band integrated noise.
- Peak frequency and prominence.
- Number of bins above median +6/+10 dB.
- Difference L minus R for median and mean PSD.
- Any narrow peak that persists across consecutive windows, moves in frequency, or appears only in one channel/window.

These windows overlap; do not call them independent observations.

### Interpretation
Answer:
1. Is the 16.6 dB L/R median difference stable across most time windows, or driven by the initial transient?
2. Is the mean/median gap caused by a few narrow peaks? Identify their frequencies and time presence.
3. Are L/R prominent components coherent at matching frequencies?
4. Is L's higher floor broadband across the entire 968–1032 Hz band or concentrated in particular sub-bands?
5. What does this imply for the pilot SNR as observed in this single unknown-path capture, and what cannot be inferred?

Do not infer the physical origin or call a component mains/USB/DUT noise without evidence. Explicitly state that only one unknown-path 10-second file was analyzed and no hardware conclusion or new capture authorization follows.

## Outputs and publication

Successful: commit exactly:
1. v2253-pilotband-noise-characterization.json
2. v2253-pilotband-noise-characterization-run.log
3. V2253-EXECUTION-MANIFEST.md
4. V2253-AGENT-REPORT.md

Blocked: commit only:
1. v2253-input-check.log
2. V2253-EXECUTION-MANIFEST.md
3. V2253-AGENT-REPORT.md

Manifest must include pinned SHA, worktree status, host/software versions, WAV metadata/full SHA, exact compile/run commands, exit status, wall time, maximum RSS, JSON bytes/full SHA, git diff --check, and explicit no-hardware/no-ALSA/no-recording/no-playback confirmation.

Do not include the WAV in Git. Stage only allowlisted files. Verify pinned SHA ancestry and safe branch relationship. Push normally; no reset, rebase or force push. If the branch moved or push rejected, stop and report exact state.

## Definition of done

A read-only, reproducible time-resolved spectral characterization of the existing 10-second probe, or an honest BLOCKED result. No new capture or hardware access.
