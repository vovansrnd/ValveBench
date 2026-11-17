# v2.21.0 — execution contract (coordinator-authored diagnostics)

## Frozen source

- Branch: `coordinator/v2210-clock-quality-diagnostics`
- Exact test-source commit: `d42c6b4a3677c8f0521d41926b8782c6a4d23447`
- Frozen estimator: `v2188_estimators.py` — do not edit.
- Synthetic/offline diagnostics only; no hardware or DUT testing.

## Purpose

1. **Production clock-quality boundary.** `v2210_clock_quality_boundary.py`
   calls the real `Rig.run()` and `clock_check()` with contiguous front,
   centre, and rear pilot occupancy; duty fractions around 50%; and split /
   burst patterns. It records onset displacement, analysis status, local SNR,
   inter-block phase coherence and warnings. It characterizes the 40%-occupancy
   full-path PASS found in v2.20.0.
2. **Candidate two-block line persistence.** `v2210_lineqc_two_block.py`
   runs the frozen v2.18.8 LSQ estimator on 540 line-free white-noise samples,
   60 colored-noise samples and 48 stable injected-spur controls. It rebuilds
   the exact fitted residual, applies the unchanged `residual_line_qc`
   separately to both one-second halves, and compares the existing full-window
   decision with a candidate requirement that both blocks report a line at a
   consistent frequency (0.5 / 1 / 2 Hz). This is an offline diagnostic
   proposal only; no thresholds are changed.
3. **Nearby-line duration comparison.** `v2210_nearby_duration_comparison.py`
   compares 2 s and 4 s records on paired pilot/noise seeds, offsets
   0.25–1.00 Hz, −10/−20 dBc, both phase modes, and 16 trials per absolute
   offset/phase/level cell (eight positive and eight negative offsets).
   All fits use the frozen v2.18.8 estimator. It tests whether the larger time
   aperture reduces primary-frequency bias.

## Checkout and compile

First fetch the branch and read this file. Then, in the existing ValveBench clone:

```bash
git fetch origin coordinator/v2210-clock-quality-diagnostics
git checkout --detach d42c6b4a3677c8f0521d41926b8782c6a4d23447
test "$(git rev-parse HEAD)" = "d42c6b4a3677c8f0521d41926b8782c6a4d23447"
git status --short
```

The working tree must be clean before execution.

```bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
python3 -m py_compile \
  "$D/v2188_estimators.py" \
  "$D/v2210_clock_quality_boundary.py" \
  "$D/v2210_lineqc_two_block.py" \
  "$D/v2210_nearby_duration_comparison.py"
```

If compilation fails, stop and return the complete traceback. Do not repair
or edit any script.

## Execute unchanged, in this order

Run each script exactly once. Save complete stdout/stderr and
`/usr/bin/time -v`. Stop at the first nonzero exit code, preserve its full
log/traceback, and do not launch later scripts.

```bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction

/usr/bin/time -v python3 "$D/v2210_clock_quality_boundary.py" \
  > "$D/v2210-clock-quality-boundary-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2210-clock-quality-boundary-run.log"
test "$rc" -eq 0 || exit "$rc"

/usr/bin/time -v python3 "$D/v2210_lineqc_two_block.py" \
  > "$D/v2210-lineqc-two-block-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2210-lineqc-two-block-run.log"
test "$rc" -eq 0 || exit "$rc"

/usr/bin/time -v python3 "$D/v2210_nearby_duration_comparison.py" \
  > "$D/v2210-nearby-duration-comparison-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2210-nearby-duration-comparison-run.log"
test "$rc" -eq 0 || exit "$rc"

git diff --check
```

## Required artifacts

Commit only these seven generated files:

- `v2210-clock-quality-boundary.json`
- `v2210-clock-quality-boundary-run.log`
- `v2210-lineqc-two-block.json`
- `v2210-lineqc-two-block-run.log`
- `v2210-nearby-duration-comparison.json`
- `v2210-nearby-duration-comparison-run.log`
- `V2210-EXECUTION-MANIFEST.md`

Preserve every `per_case` and `per_trial` row. Do not replace raw JSON
with summaries, screenshots or plots.

The manifest must record exact checked-out HEAD, host, CPU, OS/kernel, Python,
NumPy, SciPy, compile command/result, run commands, exit codes, wall times,
maximum RSS, diff-check result and output file list. Confirm that the estimator,
harnesses and production files were not edited.

## Restrictions

- Do not edit the scripts, frozen estimator, production source, main, PR #4,
  configuration, thresholds, pilot frequency or clock gate.
- Do not test hardware, playback, audio capture or DUT.
- Do not generate, copy, upload or modify WAV files.
- No algorithm or production fix is authorized by this task. Return the raw
  results for coordinator review only.
