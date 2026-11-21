# v2.24.0 execution contract — estimator sigma empirical coverage

## Frozen source and scope

- Source branch: `coordinator/v2240-estimator-sigma-coverage`
- Frozen source SHA: `b63e60b8ec800342f9e244c885b4b89098277746`
- Result branch: `agent/v2240-estimator-sigma-coverage`
- Harness: `measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2240_estimator_sigma_coverage.py`
- Frozen estimator: `measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2188_estimators.py` (unchanged)
- Offline synthetic only. No hardware, ALSA, playback, DUT, capture, WAV or production edits.

## Run order

Compile both Python files, then run the v2.24 harness exactly once with `/usr/bin/time -v`. Capture full output, append `EXIT_STATUS=<code>`, and stop on nonzero. Full exact commands and preflight are in `AGENT-TASK-v2240.md`.

## Expected data

- 128 independent paired L/R records per duration/model cell.
- Durations: 2, 4, 6 seconds.
- Noise models: white and one-pole low-pass colored (fc=1400 Hz).
- 6 cells, 256 channel rows each, 1536 `per_trial` rows total.
- Compare frozen estimator sigma, oracle injected-noise local-PSD sigma, and post-fit-residual local-PSD sigma.
- Report 1σ/2σ/3σ coverage and L/R covariance; 256 channel rows are 128 correlated pairs, not 256 independent trials.

## Publication allowlist

- `v2240-estimator-sigma-coverage.json`
- `v2240-estimator-sigma-coverage-run.log`
- `V2240-EXECUTION-MANIFEST.md`
- `V2240-AGENT-REPORT.md`

No other files may be committed. No test, estimator or production file may be changed. No force push, reset, clean or rebase.