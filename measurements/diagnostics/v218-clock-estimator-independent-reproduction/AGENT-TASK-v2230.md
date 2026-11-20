# AGENT TASK — ValveBench v2.23.0
## Offline execution of coordinator-authored pilot-validity diagnostics

**Role:** executor and reporter. The coordinator authored and froze the tests. Execute them unchanged, preserve all raw output, publish results, and return an evidence-based report. Do not redesign or repair tests yourself.

## 1. Pinned inputs

Repository: `https://github.com/vovansrnd/ValveBench.git`

Task/contract branch: `coordinator/v2230-narrowband-pilot-validation`

Frozen test-source SHA (the only commit at which tests may be run):

`2562f9a424a473bd6c355664b74af49f753016cf`

Result branch:

`agent/v2230-coordinator-authored-validation`

Read the current task and execution contract from the task branch first:

- `measurements/diagnostics/v218-clock-estimator-independent-reproduction/AGENT-TASK-v2230.md`
- `measurements/diagnostics/v218-clock-estimator-independent-reproduction/V2230-EXECUTION.md`

Then execute the three scripts from the exact frozen SHA. The task branch may contain later task-document-only commits; do not run tests from a moving branch head.

## 2. Machine and execution environment

Use the established compute host `lk` (the remote machine reached by the existing SSH alias), in its existing clone:

`~/ValveBench-compute/ValveBench`

Do not use the user's ThinkPad clone for the computational run. Do not make changes to the user's working tree. Work in a detached, isolated Git worktree at the pinned SHA. Use a new worktree path such as:

`~/.cache/valvebench-v2230-2562f9a-run`

Before creating it, inspect whether it already exists. If it does, verify its repository identity, HEAD and worktree status; never delete or overwrite an unknown directory. Reuse valid previous output only after checking the complete log, exit marker, JSON integrity, row count and source SHA. Never rerun a completed test merely because the SSH connection was interrupted.

Set a Python bytecode cache outside the worktree, for example:

`export PYTHONPYCACHEPREFIX="$HOME/.cache/valvebench-v2230-pycache"`

This avoids untracked `__pycache__` files contaminating the publication check.

## 3. Safety and scope — mandatory

These are offline synthetic diagnostics. They do not require or permit hardware access.

- No ALSA enumeration, device opening, playback, recording, DUT connection, real capture, or WAV creation/copying.
- Do not alter any file under `valvebench/`.
- Do not alter the frozen estimator `v2188_estimators.py`.
- Do not edit any of the three v2.23 test scripts, their parameters, thresholds, seeds, algorithms, output paths or execution order.
- Do not change the production pilot frequency, clock gate, SNR threshold, estimator, line-QC, resampler or any production threshold.
- Do not add ad-hoc fixes if a test fails. Stop at the first failure, preserve the complete traceback/log and report the blocker.
- Do not use `git reset`, `git clean`, rebase, force push, or overwrite a remote branch.
- Do not publish WAV/audio files or unrelated files.

If you discover a suspected defect in a coordinator-authored test, do not silently correct it. Preserve the evidence and report the exact file, line, failure and why it prevents a valid conclusion.

## 4. Preflight

From the existing clone on `lk`:

1. Fetch the task and result branches from origin.
2. Verify the frozen SHA exists.
3. Create/use the isolated detached worktree at the exact frozen SHA.
4. Require a clean tracked worktree before running.
5. Verify that the four source files below exist and are exactly from the pinned checkout:
   - `v2188_estimators.py`
   - `v2230_narrowband_occupancy.py`
   - `v2230_pilot_leakage_bias.py`
   - `v2230_independent_psd_uncertainty.py`
6. Record host, CPU model, OS/kernel, Python, NumPy, SciPy, Git version and exact HEAD.
7. Run the exact compile check:

```bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
python3 -m py_compile \
  "$D/v2188_estimators.py" \
  "$D/v2230_narrowband_occupancy.py" \
  "$D/v2230_pilot_leakage_bias.py" \
  "$D/v2230_independent_psd_uncertainty.py"
```

If preflight or compilation fails, stop. Return the complete output and do not edit anything.

## 5. Run exactly these tests, once, in this order

Use `/usr/bin/time -v` and capture complete stdout/stderr. After each process, append an explicit `EXIT_STATUS=<code>` line to its log. Stop on the first nonzero exit code; do not launch later tests.

```bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction

/usr/bin/time -v python3 "$D/v2230_narrowband_occupancy.py" \
  > "$D/v2230-narrowband-occupancy-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2230-narrowband-occupancy-run.log"
test "$rc" -eq 0 || exit "$rc"

/usr/bin/time -v python3 "$D/v2230_pilot_leakage_bias.py" \
  > "$D/v2230-pilot-leakage-bias-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2230-pilot-leakage-bias-run.log"
test "$rc" -eq 0 || exit "$rc"

/usr/bin/time -v python3 "$D/v2230_independent_psd_uncertainty.py" \
  > "$D/v2230-independent-psd-uncertainty-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2230-independent-psd-uncertainty-run.log"
test "$rc" -eq 0 || exit "$rc"

git diff --check
```

Do not run tests in parallel. Do not change the scripts to make them faster. If resource limits or runtime become an issue, stop cleanly and report the exact process and resource evidence.

## 6. Expected test populations and technical questions

These are expected row counts, not pass/fail quality thresholds.

### A. Narrowband occupancy — 34 cases

Expected JSON: `v2230-narrowband-occupancy.json`, top-level `per_case` with 34 rows.

Test content:
- 10 ms narrowband demodulation frames, 2 ms hop;
- separate L/R results and independent first/second 1 s blocks;
- realistic broadband noise at −80 dBFS sine-equivalent RMS, correlated L/R noise;
- measured base pilot offset −23.5 ppm (999.9765 Hz) and persistent 1000 Hz leakage (L −109 dBFS, R −125 dBFS);
- pilot levels −66 and −72 dBFS;
- steady windows, 150/350/800 ms tail gaps, front partial windows, split activity, periodic gaps 20–100 ms, AM, offsets through ±100 ppm, and noise-only control;
- per-channel q95 reference, low-envelope maximum run, off-band demodulated noise proxy at +500 Hz, and candidate pilot/off-band margins.

Report:
- production `Rig.run()` and `clock_check()` decisions by case;
- whether the global-reference occupancy and maximum-run candidates separate continuous, gapped and noise-only cases;
- the number of noise-only false passes if occupancy is considered alone;
- how the 10/20 dB pilot/off-band margin candidates change those classifications;
- AM control behavior and any false failures;
- L/R differences and effects of the −23.5 ppm base offset.

Important: occupancy ≥0.97 and maximum gap ≤50 ms are experimental candidate values only. The q95 reference is data-derived; never treat occupancy alone as proof of pilot presence, especially in a noise-only case. The off-band metric is a diagnostic proxy, not an approved production threshold.

### B. 1000 Hz leakage and alternative pilots

Expected JSON: `v2230-pilot-leakage-bias.json`, top-level `per_trial` with 544 rows.

Test content:
- frozen v2.18.8 `est_lsq`, untouched;
- pilots 1000, 997 and 1013 Hz, each at a −23.5 ppm frequency offset;
- durations 2/4/6 s for 1000 Hz and 2/6 s for 997/1013 Hz;
- paired no-leak baseline and fixed 1000 Hz leakage at observed levels L −109 dBFS / R −125 dBFS, plus −100 and −90 dBFS stress controls;
- independent relative phase trials;
- −80 dBFS broadband/coloured noise and a low-level 60 Hz harmonic comb;
- AM and FM controls kept in separate populations.

Report:
- for each pilot × duration × channel × leakage level: median, p90 and maximum absolute induced frequency shift in ppm; include signed behavior if relevant;
- compare the actual observed L/R leakage pair with the stress cases;
- say whether 997 or 1013 Hz materially avoids the effect at each duration;
- distinguish absolute error from the paired induced shift;
- report status counts, nearby-probe hits and line-QC hits;
- report AM/FM behavior separately and explain whether a naive “nearby hit = reject” rule would reject legitimate modulation.

A detected line/nearby component is not itself a clock failure. Do not recommend moving the production pilot based on a single synthetic sweep.

### C. Independent local-PSD uncertainty

Expected JSON: `v2230-independent-psd-uncertainty.json`, top-level `per_trial` with 384 rows.

Test content:
- 32 independently seeded, non-overlapping noise records for each duration × noise-color cell;
- 2/4/6 s records, white and gently coloured noise;
- −72 dBFS pilot, −80 dBFS broadband noise;
- correlated L/R noise (target correlation 0.95);
- local Welch PSD near the carrier, paired with frozen LSQ estimator errors;
- CRLB formula `sigma_f = sqrt(3*N0/(pi^2*A^2*T^3))`, using one-sided local PSD and peak pilot amplitude.

Report by duration × noise color:
- row and valid-fit counts;
- median, p90 and maximum absolute error / CRLB sigma;
- signed bias and standard deviation in ppm;
- estimator-status counts;
- L/R error correlation and how it changes with duration/color;
- any differences between local-PSD CRLB and the estimator's own residual-curvature sigma, if the raw rows support a meaningful comparison.

Do not call 32 trials a production calibration or infer independent L/R errors. This is a first independent-noise validation sample.

## 7. Validate all outputs before publication

For each completed JSON:
- parse it successfully;
- verify the expected top-level schema and exact row count (34, 544, 384);
- check required fields are present;
- verify all numerical result fields intended to be finite actually are finite;
- preserve every raw `per_case` / `per_trial` record; do not reduce JSON to summary-only form;
- compute byte size and SHA-256.

For each completed log:
- ensure it contains the full `/usr/bin/time -v` block;
- ensure `EXIT_STATUS=0`;
- record elapsed wall time and maximum RSS.

Run `git diff --check`. Confirm no tracked source file changed, no production file changed, and only expected generated output files are untracked. A failed or partial run must be clearly marked as failed/partial; never fabricate missing JSONs or successful exit markers.

## 8. Manifest and report

Create these two Markdown files in the diagnostics directory.

### `V2230-EXECUTION-MANIFEST.md`

Must contain:
- exact frozen source SHA and source branch;
- result branch;
- host, CPU, OS/kernel, Python, NumPy, SciPy and Git versions;
- compile command and exit code;
- all three full run commands, actual exit codes, wall times and maximum RSS;
- JSON row counts, file sizes and SHA-256 hashes;
- `git diff --check` result;
- confirmation that production files, frozen estimator and test scripts were not edited;
- explicit confirmation: no hardware, ALSA, playback, real capture, DUT or WAV use;
- list of all published result/report files.

### `V2230-AGENT-REPORT.md`

Write a concise technical report for the coordinator, not a generic statement that the scripts passed. It must:
- summarize actual outcomes for tests A, B and C;
- answer the specific questions in section 6 with exact numeric evidence;
- distinguish measured results from hypotheses and candidate thresholds;
- identify any invalid, ambiguous, surprising or contradictory outcomes;
- state limitations and whether any conclusion needs a follow-up experiment;
- make no production-code or threshold change recommendations unless supported by the evidence; explicitly state that no changes were made.

If a test fails, write a failure report covering the completed tests and the exact failure; do not claim the remaining tests passed.

## 9. Publication

Only these eight files may be committed from the run:

1. `v2230-narrowband-occupancy.json`
2. `v2230-narrowband-occupancy-run.log`
3. `v2230-pilot-leakage-bias.json`
4. `v2230-pilot-leakage-bias-run.log`
5. `v2230-independent-psd-uncertainty.json`
6. `v2230-independent-psd-uncertainty-run.log`
7. `V2230-EXECUTION-MANIFEST.md`
8. `V2230-AGENT-REPORT.md`

Publish on `agent/v2230-coordinator-authored-validation`.

Before switching to that branch:
- fetch it and confirm its local head equals origin; if it differs, stop and report—do not reset/rebase/force;
- confirm the frozen SHA is an ancestor of the result branch;
- ensure all generated results are untracked and the tracked tree is clean.

Switch/track the result branch without force operations, stage the eight files by exact path, review `git diff --cached --stat` and `git diff --cached --name-only`, commit and push normally. If push is rejected because the remote moved, stop and report; never force-push.

After push, verify the remote result branch head and list the resulting commit SHA. Return the GitHub commit URL, report path, and a short summary in your final reply to the user.

## 10. Definition of done

Done means:
- the three frozen tests either completed successfully or the first failure is completely documented;
- all valid raw JSON rows and complete logs are preserved;
- manifest and technical report exist;
- only allowlisted files were published;
- result branch is updated without force;
- exact commit SHA and factual summary are returned.

Do not ask the user to run commands or collect data. This task is intended to be executed end-to-end by the agent on `lk`.
