# v2.21.0 — execution manifest (agent run on vovans-PC)

- Exact checked-out HEAD (pinned test source): d42c6b4a3677c8f0521d41926b8782c6a4d23447
  (checkout --detach, SHA сверён, git status чист;
  harness, frozen v2188_estimators.py и production НЕ редактировались)
- Host: vovans-PC
- CPU: AMD Ryzen 5 3600 6-Core Processor, 12 threads
- OS/kernel: Linux 7.0.0-43-generic
- Python: Python 3.14.4
- NumPy: 2.3.5
- SciPy: 1.16.3

## Команды и результаты (строго по порядку контракта)

| Команда | Exit | Wall | Max RSS |
|---|---|---|---|
| python3 -m py_compile (v2188_estimators.py, v2210_clock_quality_boundary.py, v2210_lineqc_two_block.py, v2210_nearby_duration_comparison.py) | 0 | — | — |
| /usr/bin/time -v python3 measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2210_clock_quality_boundary.py > measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2210-clock-quality-boundary-run.log 2>&1 | 0 | 0:02.14 | 167852 КБ |
| /usr/bin/time -v python3 measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2210_lineqc_two_block.py > measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2210-lineqc-two-block-run.log 2>&1 | 0 | 3:56.42 | 125892 КБ |
| /usr/bin/time -v python3 measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2210_nearby_duration_comparison.py > measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2210-nearby-duration-comparison-run.log 2>&1 | 0 | 9:31.30 | 162128 КБ |

Полные stdout/stderr + time -v: v2210-clock-quality-boundary-run.log,
v2210-lineqc-two-block-run.log, v2210-nearby-duration-comparison-run.log.
git diff --check: чисто. JSON: v2210-clock-quality-boundary.json,
v2210-lineqc-two-block.json, v2210-nearby-duration-comparison.json
(per_case/per_trial целиком). Подтверждение: estimator, harness и
production файлы не редактировались.
