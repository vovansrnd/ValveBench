# v2.23.0 execution contract — coordinator-authored synthetic diagnostics

## Frozen source and scope

- Source branch: `coordinator/v2230-narrowband-pilot-validation`
- Frozen test-source commit: `2562f9a424a473bd6c355664b74af49f753016cf`
- Task/contract branch: `coordinator/v2230-narrowband-pilot-validation`
- Result branch: `agent/v2230-coordinator-authored-validation`
- Frozen estimator: `v2188_estimators.py` (must not be edited)
- Production module: imported for offline `Rig.run()` / `clock_check()` exercise only
- No hardware, ALSA, playback, DUT, real capture, WAV input/output, or production edits

## Questions under test

1. Does a 10 ms narrowband demodulation envelope expose pilot-off intervals hidden by realistic broadband noise? The occupancy synthetic backend includes the observed −23.5 ppm baseline (999.9765 Hz) against the fixed 1000 Hz leakage line. Report each channel and each 1 s block separately, including occupancy relative to one global per-channel q95 reference and the longest low-envelope run. The off-band demodulation proxy is offset by +500 Hz (five exact cycles in each 10 ms frame), to avoid the pilot leakage caused by the former +40 Hz offset. Report pilot/off-band margin and its 10/20 dB candidate classifications. The 0.97 occupancy and 50 ms maximum-run values are candidates only.
2. What frequency-estimation bias is induced by the observed 1000 Hz leakage line at -109 dBFS (L) / -125 dBFS (R), with stress levels, for pilot candidates 1000, 997 and 1013 Hz? The result rows record channel-specific leakage amplitude, including −109 dBFS on L and −125 dBFS on R for the observed-level case. Compare each trial with a paired no-leak baseline. Keep AM/FM controls separate. A nearby/line detector hit is never an automatic rejection.
3. Does the local-PSD CRLB estimate track frequency error on independent, non-overlapping noise realizations? Report L/R error correlation; do not infer that the channels are statistically independent.

## Execute unchanged, in order

From the exact pinned source commit:

```bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
python3 -m py_compile "$D/v2188_estimators.py" \
  "$D/v2230_narrowband_occupancy.py" \
  "$D/v2230_pilot_leakage_bias.py" \
  "$D/v2230_independent_psd_uncertainty.py"

/usr/bin/time -v python3 "$D/v2230_narrowband_occupancy.py" \
  > "$D/v2230-narrowband-occupancy-run.log" 2>&1
rc=$?; echo "EXIT_STATUS=$rc" >> "$D/v2230-narrowband-occupancy-run.log"
test "$rc" -eq 0 || exit "$rc"

/usr/bin/time -v python3 "$D/v2230_pilot_leakage_bias.py" \
  > "$D/v2230-pilot-leakage-bias-run.log" 2>&1
rc=$?; echo "EXIT_STATUS=$rc" >> "$D/v2230-pilot-leakage-bias-run.log"
test "$rc" -eq 0 || exit "$rc"

/usr/bin/time -v python3 "$D/v2230_independent_psd_uncertainty.py" \
  > "$D/v2230-independent-psd-uncertainty-run.log" 2>&1
rc=$?; echo "EXIT_STATUS=$rc" >> "$D/v2230-independent-psd-uncertainty-run.log"
test "$rc" -eq 0 || exit "$rc"

git diff --check
```

Stop on the first nonzero exit status. Preserve the entire log and traceback. Do not edit tests, estimator or production code to get a passing run.

## Required publication allowlist

Commit only these eight generated artifacts:

- `v2230-narrowband-occupancy.json`
- `v2230-narrowband-occupancy-run.log`
- `v2230-pilot-leakage-bias.json`
- `v2230-pilot-leakage-bias-run.log`
- `v2230-independent-psd-uncertainty.json`
- `v2230-independent-psd-uncertainty-run.log`
- `V2230-EXECUTION-MANIFEST.md`
- `V2230-AGENT-REPORT.md`

Retain all `per_case` and `per_trial` rows. The manifest must include exact source SHA, host/CPU/OS/kernel, Python/NumPy/SciPy versions, compile result, full commands, exit codes, wall times, maximum RSS, JSON row counts/byte sizes/SHA-256, diff-check result, and the allowlisted output set.

## Publication safeguards

- Run in a detached isolated worktree at the pinned source commit.
- Result branch must descend from that exact source commit.
- Never force-push, reset, rebase, or modify the user's working branch.
- Refuse publication if any tracked file changes, if any file under `valvebench/` changes, or if an unapproved untracked file exists.
- Copy only the eight allowlisted result/report artifacts to the result branch.
- The diagnostics are evidence only. They do not authorize a change to production thresholds, pilot frequency, clock gate, estimator, or resampler.
