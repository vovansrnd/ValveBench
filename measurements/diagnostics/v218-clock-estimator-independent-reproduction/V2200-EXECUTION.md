# v2.20.0 — execution contract (coordinator-authored diagnostics)

## Frozen source

- Branch: `coordinator/v2200-full-clockpath-diagnostics`
- Exact test-source commit: `35c800b70bb270affc2d4964902660c8738ab064`
- The parent includes the completed v2.19.0 execution artifacts.
- Frozen estimator: `v2188_estimators.py` (do not edit).
- This package is synthetic/offline diagnostics only.

## Purpose

1. **Full production path.** `v2200_full_clock_path.py` calls the real
   `valvebench.measure.Rig.run()` and `clock_check()` through a deterministic
   synthetic capture backend. It exercises a clean capture, 40% and 50%
   front-loaded signal occupancy, a 25% tail dropout, a 500 ms head dropout,
   40/60 ms gaps, and a 200 ms / −6 dB sag. This closes the evidence gap left
   by v2.19.0, which called `_window_stability_reason()` directly.
2. **Line-QC false positives.** `v2200_lineqc_montecarlo.py` is the frozen
   v2.19.0 line-QC experiment with 100 focused white-noise trials per each of
   nine pilot/noise-level cells. It also retains the replay population,
   colored-noise controls, noise-only controls, and injected-spur sensitivity
   controls. The output adds per-cell Wilson 95% intervals.
3. **Nearby-line frequency bias.** `v2200_nearby_primary_fit_bias.py` uses the
   unchanged v2.18.8 estimator, offsets 0.25–1.50 Hz, line levels −10/−20/−30
   dBc, both phase modes, and 20 trials per cell (10 positive and 10 negative
   offsets). Each injected record has a paired pilot-only baseline. AM/FM
   controls remain separate. Results report induced frequency shift, absolute
   error, probe detections, and estimator statuses.

## Checkout and compile

From the existing ValveBench clone:

```bash
git fetch origin coordinator/v2200-full-clockpath-diagnostics
git checkout --detach 35c800b70bb270affc2d4964902660c8738ab064
test "$(git rev-parse HEAD)" = "35c800b70bb270affc2d4964902660c8738ab064"
git status --short
```

The checkout must be clean before execution. Do not change the source commit.

```bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
python3 -m py_compile \
  "$D/v2188_estimators.py" \
  "$D/v2200_full_clock_path.py" \
  "$D/v2200_lineqc_montecarlo.py" \
  "$D/v2200_nearby_primary_fit_bias.py"
```

If compilation fails, stop immediately and return the full traceback. Do not
repair or edit the scripts.

## Execute unchanged, in this order

Run each command exactly once. Capture the complete stdout/stderr and
`/usr/bin/time -v` output. Stop after the first nonzero exit code; retain the
failure log and traceback and do not start later tests.

```bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction

/usr/bin/time -v python3 "$D/v2200_full_clock_path.py" \
  > "$D/v2200-full-clock-path-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2200-full-clock-path-run.log"
test "$rc" -eq 0 || exit "$rc"

/usr/bin/time -v python3 "$D/v2200_lineqc_montecarlo.py" \
  > "$D/v2200-lineqc-montecarlo-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2200-lineqc-montecarlo-run.log"
test "$rc" -eq 0 || exit "$rc"

/usr/bin/time -v python3 "$D/v2200_nearby_primary_fit_bias.py" \
  > "$D/v2200-nearby-primary-fit-bias-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2200-nearby-primary-fit-bias-run.log"
test "$rc" -eq 0 || exit "$rc"

git diff --check
```

## Required artifacts

On a fully successful run, commit only these generated files:

- `v2200-full-clock-path.json`
- `v2200-full-clock-path-run.log`
- `v2200-lineqc-montecarlo.json`
- `v2200-lineqc-montecarlo-run.log`
- `v2200-nearby-primary-fit-bias.json`
- `v2200-nearby-primary-fit-bias-run.log`
- `V2200-EXECUTION-MANIFEST.md`

Keep all JSON `per_case` / `per_trial` arrays intact. Do not replace them
with summaries or screenshots.

The manifest must record the exact checked-out HEAD, host, CPU, OS/kernel,
Python, NumPy and SciPy versions, compile command/result, each exact run
command, exit code, wall time and max RSS, `git diff --check` result, output
file list, and confirmation that no source/test files were edited.

## Restrictions

- Do not edit any harness, estimator, production source, configuration,
  threshold, pilot frequency, clock gate, main, or PR #4.
- Do not run playback/capture on real audio devices and do not test a DUT.
- Do not create, copy, or upload WAV files.
- Do not add interpretation or code changes to the test commit. Return the
  untouched outputs and manifest for coordinator review.
