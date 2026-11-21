# v2.25.2 — execution contract: raw-domain characterization of the existing probe

## Objective

Distinguish a genuine coherent 15.38 Hz component from a brief broadband/low-frequency transient in the already existing 10-second WAV. v2.25.1's sliding fits were applied after whole-record forward/backward 8–20 Hz filtering; the first window may be affected by edge behaviour and the short transient.

v2.25.2 repeats the relevant measurements directly on the unfiltered raw samples, using 0.5-second non-overlapping blocks, overlapping 0.5-second windows (125 ms hop), and raw 2-second windows.

## Absolute restrictions

- Read only the existing /tmp/v2250-probe.wav.
- No ALSA, arecord, aplay, hardware, mixer, playback, recording, generated signals or device opens.
- Do not recreate the WAV if it is absent.
- Do not use another input file or synthetic replacement.
- Do not edit the analysis script, frozen estimator or production files.
- Do not copy or commit the WAV.

If the old file is missing/invalid, stop and publish BLOCKED evidence. Never recapture.

## Pinned source

Repository: vovansrnd/ValveBench

Task branch: coordinator/v2252-raw-probe-transient

Result branch: agent/v2252-raw-probe-transient

The exact pinned SHA is recorded in AGENT-TASK-v2252.md after the task is frozen.

Script: v2252_raw_probe_transient_analysis.py

Expected input: /tmp/v2250-probe.wav, 96 kHz stereo PCM24, 10 seconds. Verify, do not assume.

## Worktree and preflight

Use host lk and existing clone ~/ValveBench-compute/ValveBench, not the user's ThinkPad clone. Create an isolated detached worktree at exact pinned SHA. Check whether the proposed worktree path exists before creating; never overwrite an unknown directory. Put PYTHONPYCACHEPREFIX outside the worktree. Verify repository identity, HEAD and clean tracked state.

Before Python, do metadata-only checks on /tmp/v2250-probe.wav: test -f, stat, file, sha256sum. If absent, stop. If present, record owner, mode, bytes, full SHA-256 and WAV header.

## Compile and run

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

Do not edit or rerun the script after failure. Preserve traceback and stop. For a successful run, inspect log/JSON before publication.

## Required analysis

Use raw JSON values. Report:

1. Input bytes, exact SHA, WAV format, channels, rate, duration, clipping.
2. Full-band levels L/R.
3. For each of 20 non-overlapping 0.5 s blocks:
   - raw full-band RMS and peak;
   - raw periodogram integrated power/RMS over 8–20 Hz and 14–17 Hz;
   - dominant raw 8–20 Hz frequency and PSD;
   - fixed 15.380859375 Hz sinusoid fit with DC + linear trend, absolute-time phase, amplitude and fraction of detrended energy explained;
   - same fixed-frequency fit at 12 Hz as a control.
4. For overlapping 0.5 s windows at 125 ms hop:
   - raw 8–20 Hz power/peak;
   - fixed 15.380859375 Hz fit amplitude, phase and explained-energy fraction.
5. For raw 2-second windows at 1 s hop:
   - raw 8–20 Hz periodogram peak;
   - fixed 15.380859375 Hz fit amplitude/phase/energy reduction.
6. 968–1032 Hz L/R noise PSD median/mean/max (no pilot present).
7. Compare direct raw-domain results with v2.25.1 filtered-window results:
   - whether the initial event is confined to the first 0.5 s or persists longer;
   - whether it is spectrally concentrated around 15.4 Hz or broadband across 8–20 Hz;
   - whether the 15.38 Hz projection has phase continuity across consecutive windows;
   - whether the v2.25.1 0–2 s amplitude is plausibly influenced by filter/window boundary behaviour.

Important: a high fixed-frequency sine projection is not proof of a true sinusoidal component. Use energy reduction, phase continuity, raw spectral concentration and adjacent windows together. Do not infer physical source. Do not label it as mains hum, rumble, mechanical vibration or DUT noise.

## Required outputs

Successful run: exactly four files:
1. v2252-raw-probe-transient-analysis.json
2. v2252-raw-probe-transient-analysis-run.log
3. V2252-EXECUTION-MANIFEST.md
4. V2252-AGENT-REPORT.md

Blocked input: exactly:
1. v2252-input-check.log
2. V2252-EXECUTION-MANIFEST.md
3. V2252-AGENT-REPORT.md

Manifest must state pinned SHA, worktree HEAD/status, environment, exact WAV metadata/hash, exact commands, exit status, time/RSS, JSON byte size/hash, diff-check and explicit confirmation that no hardware/ALSA/playback/recording operation occurred.

Before push, confirm pinned SHA ancestry and safe result-branch state; stage only the allowlist; no reset/rebase/force push or unrelated cleanup. Raw WAV must never be committed.

The report must distinguish:
- measured raw-domain facts;
- comparison with v2.25.1's filtered estimates;
- interpretation and remaining ambiguity.

It must explicitly state that the physical source remains unknown and this task cannot authorize a new capture or certify the input path.

## Definition of done

A reproducible raw-domain analysis of the same existing WAV, or an honest BLOCKED result if it is missing. Absolutely no new capture or hardware access.
