# v2.19.0 — execution contract (coordinator-authored)

## Scope

Run the two committed coordinator-authored diagnostic harnesses on vovans-PC using the existing remote clone reached through `ssh lk`.

This package is synthetic/software-only:
- no ALSA device opening;
- no playback or capture;
- no DUT connection;
- no WAV transfer;
- no production, main, PR #4, pilot-frequency or clock-gate edits.

The frozen estimator file `v2188_estimators.py` is a pinned dependency. Do not replace it with a current or locally modified implementation.

## Exact preflight

From the ValveBench repository root:

```bash
git fetch origin agent/v2190-coordinator-authored-validation
git checkout --detach origin/agent/v2190-coordinator-authored-validation
git rev-parse HEAD
python3 -m py_compile \
  measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2188_estimators.py \
  measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2190_gate_lineqc_montecarlo.py \
  measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2190_nearby_primary_fit_bias.py
```

Record the full HEAD. Expected test source commit is the current head of this branch at execution; do not silently substitute a different ref. If compile fails, stop and send the full traceback. Do not edit the harnesses.

## Run commands

Run from repository root. Preserve complete stdout and stderr and collect `/usr/bin/time -v` output for each process.

```bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
/usr/bin/time -v python3 "$D/v2190_gate_lineqc_montecarlo.py" \
  > "$D/v2190-gate-lineqc-run.log" 2>&1
echo "EXIT_STATUS=$?" >> "$D/v2190-gate-lineqc-run.log"

/usr/bin/time -v python3 "$D/v2190_nearby_primary_fit_bias.py" \
  > "$D/v2190-primary-fit-bias-run.log" 2>&1
echo "EXIT_STATUS=$?" >> "$D/v2190-primary-fit-bias-run.log"
```

Do not run the second script if the first fails due to an import, assertion, memory, or runtime error. Preserve the failing log and traceback; stop and report it.

## Result contract

Commit only these generated files:
- `v2190-gate-lineqc-montecarlo.json`
- `v2190-nearby-primary-fit-bias.json`
- `v2190-gate-lineqc-run.log`
- `v2190-primary-fit-bias-run.log`
- `V2190-EXECUTION-MANIFEST.md`

The manifest must include:
- exact branch HEAD used for execution;
- host name, CPU, OS/kernel;
- Python, NumPy and SciPy versions;
- exact commands and individual exit codes;
- wall time and maximum RSS from each `time -v` section;
- `git diff --check` result;
- confirmation that neither harness nor frozen estimator was edited.

Do not reformat or summarize away the JSON `per_trial` arrays. They are the primary evidence.

## Restrictions

Do not modify scripts, the frozen estimator, configuration thresholds, production source, main, PR #4, pilot frequency, or clock gate. Do not add a new refusal state. Do not perform hardware playback/capture or DUT testing. Do not copy WAV files.

If any harness needs correction, stop and return the traceback plus the exact command and HEAD. The coordinator will patch the harness and issue a new immutable run target.
