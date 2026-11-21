# AGENT TASK — ValveBench v2.24.0
## Empirical coverage and residual-PSD validation of the frozen LSQ uncertainty estimate

**Role:** executor and reporter. The coordinator authored and froze the diagnostic script. Execute it unchanged, preserve the raw JSON/log, and write an evidence-based report. Do not redesign or repair the script.

## 1. Pinned inputs

Repository: https://github.com/vovansrnd/ValveBench

Task/contract branch: `coordinator/v2240-estimator-sigma-coverage`

Frozen source SHA (the only commit at which the test may be run):

`ea5656280f385b172d60bffe750ff31b76f7a016`

Result branch: `agent/v2240-estimator-sigma-coverage`

Test script:
- `measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2240_estimator_sigma_coverage.py`

Frozen estimator:
- `measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2188_estimators.py` (must not be edited)

Read this task and `V2240-EXECUTION.md` from the task branch before execution. Run the test script and estimator from the exact frozen SHA, not from the moving branch head.

## 2. Machine and execution environment

Use the established compute host `lk` and its existing clone:

`~/ValveBench-compute/ValveBench`

Do not use the user's ThinkPad clone. Create/use an isolated detached worktree at the pinned SHA, for example:

`~/.cache/valvebench-v2240-ea56562-run`

Before creating it, inspect whether it exists. Reuse it only after verifying repo identity, exact HEAD and status. Never delete or overwrite an unknown directory.

Set bytecode cache outside the worktree:

`export PYTHONPYCACHEPREFIX="$HOME/.cache/valvebench-v2240-pycache"`

## 3. Safety and scope — mandatory

This is an offline synthetic diagnostic:
- No ALSA enumeration, device opening, playback, recording, DUT, hardware, real capture or WAV.
- Do not change any file under `valvebench/`.
- Do not edit `v2188_estimators.py` or the v2.24 harness.
- Do not change production estimator, clock gate, pilot, thresholds, resampler or any production settings.
- Do not alter parameters, seeds, algorithms or output paths.
- If compile or execution fails, stop, preserve the full traceback/log, and report. Do not self-repair the script.
- No reset, clean, rebase, force push or overwrite of remote branches.

## 4. Preflight

1. Fetch task and result branches.
2. Verify the pinned SHA exists and create the isolated detached worktree at exactly that SHA.
3. Require clean tracked state before execution.
4. Verify the test script and frozen estimator exist at the pinned checkout.
5. Record exact HEAD, host, CPU, `uname -a`, `/etc/os-release`, Python, NumPy, SciPy and Git versions.
6. Compile:

```bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
python3 -m py_compile \
  "$D/v2188_estimators.py" \
  "$D/v2240_estimator_sigma_coverage.py"
```

Stop if compilation fails.

## 5. Execute once

Run sequentially; capture complete stdout/stderr and `/usr/bin/time -v`. Append `EXIT_STATUS=<code>` after each process and stop at the first nonzero code:

```bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction

/usr/bin/time -v python3 "$D/v2240_estimator_sigma_coverage.py" \
  > "$D/v2240-estimator-sigma-coverage-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2240-estimator-sigma-coverage-run.log"
test "$rc" -eq 0 || exit "$rc"

git diff --check
```

Do not run in parallel or rerun a completed test merely because the SSH connection was interrupted. First inspect the entire log, exit marker, JSON and source SHA.

## 6. Experimental design and expected population

The frozen script uses:
- `est_lsq` from v2.18.8 unchanged;
- 96 kHz, nominal 1000 Hz pilot, true frequency 999.9765 Hz (−23.5 ppm);
- pilot −72 dBFS and fixed total noise RMS −80 dBFS;
- durations 2/4/6 seconds;
- white Gaussian noise and the same one-pole 1400 Hz low-pass colored-noise family used in v2.23.0;
- 128 independent L/R noise pairs per duration/model, target L/R correlation 0.95;
- 256 channel rows per cell and **1536 per_trial rows total**.

Each row compares:
1. frozen estimator `estimator_sigma_ppm` from `est_lsq`;
2. oracle local-PSD sigma from the injected noise;
3. residual local-PSD sigma measured after subtracting the fitted sine and DC from the observed synthetic signal.

The PSD band is 2–32 Hz away from the true pilot, using one-sided Welch PSD and the unchanged CRLB expression:
`sigma_f = sqrt(3*N0/(pi^2*A^2*T^3))`.

These are diagnostic estimates. Do not assume the residual-PSD estimator is valid merely because it is closer to the oracle. Assess it from the measured rows.

## 7. Required analysis in V2240-AGENT-REPORT.md

Answer with numeric tables for all six duration × noise-model cells:

- independent pair count, channel rows, valid fit count and status counts;
- empirical signed error bias and sample standard deviation, with L/R separately;
- L/R error correlation;
- median/p90/max of `abs_error_over_estimator_sigma`;
- coverage of the existing estimator sigma at 1σ, 2σ and 3σ;
- corresponding median/p90 and 1σ/2σ/3σ coverage for oracle-PSD sigma;
- corresponding median/p90 and 1σ/2σ/3σ coverage for residual-PSD sigma;
- median estimator-sigma / oracle-PSD-sigma ratio;
- median residual-PSD-sigma / oracle-PSD-sigma ratio;
- local PSD values and sigma values sufficient to explain the white/colored difference.

Explicitly compare the v2.23 observation (estimator sigma strongly under-reported on colored noise) with the larger independent sample. State whether the residual-PSD estimate tracks the oracle and whether its empirical coverage is more credible than the existing sigma.

Important statistical handling:
- 128 independent *paired records* per cell, not 256 independent channel records. L/R channel rows are paired and correlated; do not claim 256 independent observations.
- Coverage is the fraction of channel rows with `abs(error) <= k*sigma`, k=1,2,3. Label that convention explicitly and also report L/R correlation.
- Use the script's `summary_by_cell` values as the canonical aggregate. Do not recompute the method silently or round before analysis.
- Do not claim a production fix or infer the physical origin of temporal correlation from these synthetic data.
- Clearly distinguish observation, interpretation and hypotheses.

## 8. Output validation

Expected JSON:
`v2240-estimator-sigma-coverage.json`

Validate:
- valid JSON and expected top-level `summary`, `per_trial`, `environment`;
- exactly 1536 `per_trial` rows;
- each of 6 cells has 128 independent trial indices and 256 channel rows;
- both L/R present for every trial;
- all required numeric result fields finite when a fit is valid;
- preserve every raw row;
- record byte size and full SHA-256.

The run log must include the full `/usr/bin/time -v` block and `EXIT_STATUS=0`. Record wall time and maximum RSS. Run `git diff --check`; no tracked source changes or unapproved files are permitted.

## 9. Manifest and report

Create:
- `V2240-EXECUTION-MANIFEST.md`
- `V2240-AGENT-REPORT.md`

Manifest must include:
- exact pinned SHA and source branch;
- result branch;
- exact worktree HEAD and clean pre-run state;
- host, CPU, full `uname -a`, full `NAME` and `VERSION` from `/etc/os-release`;
- Python, NumPy, SciPy and Git versions;
- exact py_compile command/exit code;
- full run command/exit code/wall time/maximum RSS;
- JSON rows, byte size and full 64-character SHA-256;
- `git diff --check`;
- confirmation: no production, estimator or harness edits; no hardware/ALSA/playback/real capture/DUT/WAV.

Report must answer section 7 from actual JSON values, include important limitations, and state that no production change was made. Do not write a generic “test passed” report.

## 10. Publication — four files only

Commit exactly:
1. `v2240-estimator-sigma-coverage.json`
2. `v2240-estimator-sigma-coverage-run.log`
3. `V2240-EXECUTION-MANIFEST.md`
4. `V2240-AGENT-REPORT.md`

Publish to `agent/v2240-estimator-sigma-coverage`.

Before publication:
- fetch result branch and confirm local head equals origin;
- confirm pinned SHA is an ancestor;
- confirm tracked source tree clean;
- stage only the four allowlisted files and inspect staged names/stat.

Commit and push normally. Never force push. If the remote branch moved or push is rejected, stop and report the exact condition.

After push, verify remote HEAD and return commit SHA, GitHub URL, report path and concise result summary.

## 11. Definition of done

The diagnostic ran once from the exact pinned SHA (or is documented as failed), complete raw rows and log are preserved, manifest/report are published, only the four allowlisted files were committed, and no production change was made.