# v2.22.0 — execution contract (coordinator-authored diagnostics)

## Frozen source

- Branch: `coordinator/v2220-clock-quality-validation`
- Exact test-source commit: `5d5e9d1eaee6cc9e0b427e5f6e1a5d738b1a3a78`
- Frozen estimator: `v2188_estimators.py` — do not edit.
- This package is synthetic/offline diagnostics only.

## Purpose

1. **Production clock occupancy / block diagnostics.**
   `v2220_clock_occupancy_diagnostics.py` calls the real production
   `Rig.run()` and `clock_check()` through an offline synthetic capture
   backend. It includes contiguous and placed signal windows, periodic
   pulse trains with on/off gaps around the production 50 ms run threshold,
   controlled frequency offsets through ±110 ppm, and continuous AM controls.
   It records the production result, two one-second block-level tone/SNR
   estimates, 2 ms frame occupancy relative to the 90th/95th percentile, and
   candidate diagnostics. Candidate metrics are reports only; they do not
   change or replace production logic.
2. **Line-QC local-floor physical-width correction.**
   `v2220_lineqc_physical_width.py` runs frozen v2.18.8 `est_lsq` on 900
   seeded line-free white-noise records, 60 colored-noise records, stable
   on-bin/off-bin spurs, and first-half-/second-half-only spurs. It compares
   the current full-window `floor_bins=161` with per-block QC using both
   legacy 161 bins and physically normalized 81 bins (approximately ±40 Hz
   at the one-second FFT's 1 Hz/bin resolution). It preserves every raw row.
3. **Nearby-line duration robustness.**
   `v2220_nearby_duration_robustness.py` compares paired 2/3/4/6 s records
   with identical per-trial pilot/noise seeds and explicit phase modes.
   Conditions: offsets 0.25/0.35/0.40/0.50/0.75 Hz, levels −10/−20 dBc,
   8 positive and 8 negative trials per absolute-offset/phase/level cell.
   All fits use the unchanged frozen estimator; results report absolute
   induced shifts and paired improvements/worsenings, not just ratios.

## Checkout and compile

First fetch the branch and read this file. Then, in the existing ValveBench clone:

```bash
git fetch origin \
  +refs/heads/coordinator/v2220-clock-quality-validation:refs/remotes/origin/coordinator/v2220-clock-quality-validation \
  +refs/heads/agent/v2220-coordinator-authored-validation:refs/remotes/origin/agent/v2220-coordinator-authored-validation
git checkout --detach 5d5e9d1eaee6cc9e0b427e5f6e1a5d738b1a3a78
test "$(git rev-parse HEAD)" = "5d5e9d1eaee6cc9e0b427e5f6e1a5d738b1a3a78"
git status --short
```

The working tree must be clean before execution.

```bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
python3 -m py_compile \
  "$D/v2188_estimators.py" \
  "$D/v2220_clock_occupancy_diagnostics.py" \
  "$D/v2220_lineqc_physical_width.py" \
  "$D/v2220_nearby_duration_robustness.py"
```

If compilation fails, stop and return the complete traceback. Do not repair
or edit any script.

## Execute unchanged, in this order

Run each script exactly once. Save complete stdout/stderr and
`/usr/bin/time -v`. Stop after the first nonzero exit code, preserve its
full log/traceback, and do not launch later scripts.

```bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction

/usr/bin/time -v python3 "$D/v2220_clock_occupancy_diagnostics.py" \
  > "$D/v2220-clock-occupancy-diagnostics-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2220-clock-occupancy-diagnostics-run.log"
test "$rc" -eq 0 || exit "$rc"

/usr/bin/time -v python3 "$D/v2220_lineqc_physical_width.py" \
  > "$D/v2220-lineqc-physical-width-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2220-lineqc-physical-width-run.log"
test "$rc" -eq 0 || exit "$rc"

/usr/bin/time -v python3 "$D/v2220_nearby_duration_robustness.py" \
  > "$D/v2220-nearby-duration-robustness-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2220-nearby-duration-robustness-run.log"
test "$rc" -eq 0 || exit "$rc"

git diff --check
```

## Required artifacts

Commit only these seven generated files:

- `v2220-clock-occupancy-diagnostics.json`
- `v2220-clock-occupancy-diagnostics-run.log`
- `v2220-lineqc-physical-width.json`
- `v2220-lineqc-physical-width-run.log`
- `v2220-nearby-duration-robustness.json`
- `v2220-nearby-duration-robustness-run.log`
- `V2220-EXECUTION-MANIFEST.md`

Preserve every `per_case` and `per_trial` row. Do not replace raw JSON
with summaries, screenshots or plots.

The manifest must record exact checked-out HEAD, host, CPU, OS/kernel, Python,
NumPy, SciPy, compile command/result, run commands, exit codes, wall times,
maximum RSS, diff-check result and output file list. Confirm that the estimator,
harnesses and production files were not edited.

## Commit results to the task branch

The scripts are run detached at the pinned test-source commit. After all
three runs pass, do not commit on detached HEAD and do not push a detached
child.

The seven generated output files remain untracked when switching back to
the task branch. Reattach to the task branch and verify it descends from
the pinned test source:

```bash
if git show-ref --verify --quiet refs/heads/agent/v2220-coordinator-authored-validation; then
  git switch agent/v2220-coordinator-authored-validation
  test "$(git rev-parse HEAD)" = "$(git rev-parse origin/agent/v2220-coordinator-authored-validation)" || {
    echo "Local task branch differs from fetched origin branch; stop without reset/rebase."
    exit 1
  }
else
  git switch --track -c agent/v2220-coordinator-authored-validation origin/agent/v2220-coordinator-authored-validation
fi
git merge-base --is-ancestor 5d5e9d1eaee6cc9e0b427e5f6e1a5d738b1a3a78 HEAD
git status --short
```

Then stage only the seven filenames listed above, commit them, and push:

```bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
git add \
  "$D/v2220-clock-occupancy-diagnostics.json" \
  "$D/v2220-clock-occupancy-diagnostics-run.log" \
  "$D/v2220-lineqc-physical-width.json" \
  "$D/v2220-lineqc-physical-width-run.log" \
  "$D/v2220-nearby-duration-robustness.json" \
  "$D/v2220-nearby-duration-robustness-run.log" \
  "$D/V2220-EXECUTION-MANIFEST.md"
git diff --cached --name-only
git diff --cached --check
git commit -m "v2.22.0: execute coordinator-authored offline diagnostics"
git push origin agent/v2220-coordinator-authored-validation
```

If the task branch is unavailable, the ancestry check fails, or the push is
not fast-forward, stop and report the exact local HEAD, remote branch HEAD,
and commit parent. Do not reset, rebase, cherry-pick, force-push or rewrite
either branch without a separate coordinator instruction.

## Restrictions

- Do not edit the scripts, frozen estimator, production source, main, PR #4,
  configuration, thresholds, pilot frequency or clock gate.
- Do not test hardware, playback, audio capture or DUT.
- Do not generate, copy, upload or modify WAV files.
- No algorithm or production fix is authorized by this task. Return the raw
  results for coordinator review only.
