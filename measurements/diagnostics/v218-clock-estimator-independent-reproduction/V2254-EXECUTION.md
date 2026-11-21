# v2.25.4 — execution contract: pilot-band sub-band distribution

## Objective

Resolve the v2.25.3 open question: is the 968–1032 Hz L/R PSD median asymmetry distributed across the whole band, or concentrated in selected frequency sub-bands?

Use only the one existing v2.25.0 probe WAV:
`/tmp/v2250-probe.wav`

Expected source SHA-256:
`d1676fe55e822e703d89c34b1ee2f2fbb96143eb163540e18078081756282ca9`

This is an offline analysis-only task. The physical input connection is unknown.

## Repository and pin

Repository: `vovansrnd/ValveBench`

Task branch: `coordinator/v2254-pilotband-subband-distribution`

Result branch: `agent/v2254-pilotband-subband-distribution`

Pinned analyzer/source commit SHA (checkout this exact commit):
`a10e88f8c47751a2b12f504372ba8ee4005a8968`

Analyzer:
`measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2254_pilotband_subband_distribution.py`

The analyzer verifies the full source WAV SHA itself and stops if it differs. Do not change or rerun with another source.

## Absolute restrictions

- Read only `/tmp/v2250-probe.wav`.
- No ALSA enumeration, arecord, aplay, mixer inspection, device opens, hardware calls, recording, playback, generated signals, or configuration/cabling changes.
- Do not recreate the WAV if absent. Do not substitute another capture or synthetic input.
- Do not edit the analyzer, any production file, or any existing v2.25.3 artifact.
- Do not commit the WAV.

If the WAV is missing or its SHA differs, stop and publish a BLOCKED result with metadata-only evidence. No new capture.

## Worktree and preflight

Use `lk` and the existing clone `~/ValveBench-compute/ValveBench`; do not use the user's ThinkPad clone.

Create a new isolated detached worktree at the exact pinned SHA. Check whether the target path exists; never overwrite an existing unknown directory. Set `PYTHONPYCACHEPREFIX` outside the worktree. Verify repository identity, exact HEAD and clean tracked state.

Before running Python, use only metadata-only commands (`test -f`, `stat`, `file`, `sha256sum`) on the existing WAV. Record exact path, owner/mode, bytes, format, sample rate, channels, frames, duration and SHA-256. Do not call audio devices.

## Compile and run once

From the detached worktree:

~~~bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
export PYTHONPYCACHEPREFIX="$HOME/.cache/valvebench-v2254-pycache"
python3 -m py_compile "$D/v2254_pilotband_subband_distribution.py"
~~~

Then run exactly once:

~~~bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
/usr/bin/time -v python3 "$D/v2254_pilotband_subband_distribution.py" \
  --wav /tmp/v2250-probe.wav --output "$D" \
  > "$D/v2254-pilotband-subband-distribution-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2254-pilotband-subband-distribution-run.log"
test "$rc" -eq 0 || exit "$rc"
~~~

If compile or runtime fails, preserve the full diagnostic and stop. Do not repair the analyzer or rerun it.

## Analysis requirements

The analyzer uses:
- half-open frequency intervals [low, high) so adjacent sub-bands do not double-count edge bins;
- bin-width integration (sum of PSD-bin densities multiplied by the uniform frequency step);
- the full-record 10-second Hann periodogram (0.1 Hz spacing);
- eight contiguous 8 Hz sub-bands: 968–976, 976–984, …, 1024–1032 Hz;
- nine overlapping 2-second Hann-periodogram windows at 1-second hop (0.5 Hz spacing).

For each sub-band and channel, report median, p10, p25, p75, p90, mean, max/frequency, bins above median +6/+10 dB, and integrated band RMS. Compare L−R median, p25 and p75 differences.

Answer from the JSON:
1. Does the L−R median PSD offset remain similar in all eight sub-bands, or is it localized?
2. Are there contiguous regions with materially different channel offsets?
3. Does the sub-band pattern persist across the nine overlapping time windows?
4. Do peaks/outlier bins appear to distort only mean/max while robust percentiles remain offset, or does the underlying median also change by sub-band?
5. What can be concluded for this single 10-second unknown-path recording, and what remains unknown?

Do not call the windows independent. Do not assign a physical source or infer the input topology.

## Outputs

Success, exactly four files:
1. `v2254-pilotband-subband-distribution.json`
2. `v2254-pilotband-subband-distribution-run.log`
3. `V2254-EXECUTION-MANIFEST.md`
4. `V2254-AGENT-REPORT.md`

Blocked, exactly three files:
1. `v2254-input-check.log`
2. `V2254-EXECUTION-MANIFEST.md`
3. `V2254-AGENT-REPORT.md`

Manifest must include:
- pinned SHA and worktree/repo status;
- host and software versions;
- full WAV metadata and SHA;
- exact compile/run commands, exit, wall time, max RSS;
- output JSON exact byte size and SHA-256 if generated;
- `git diff --check` result;
- explicit statement: no ALSA/device access, no hardware interaction, no configuration/cabling changes, no recording, no playback, no synthetic signal.

Do not claim any prohibited action was performed.

## Publication

Verify the result branch starts from the task branch's latest commit, and the pinned analyzer commit is an ancestor. Stage only the exact allowlist. Inspect staged names. Commit and push normally. No force push, reset, rebase, unrelated cleanup or history rewrite. Verify the remote HEAD and list exact changed paths.

If any preflight, execution, ancestry or push condition fails, stop and report the exact blocker. Do not self-repair.

## Definition of done

A reproducible read-only sub-band characterization of the original verified probe WAV, or an honest BLOCKED result. No new capture and no hardware access.
