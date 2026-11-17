# v2.20.0 — execution manifest (agent run on vovans-PC)

- Checked-out HEAD: 8fcbe642132d957edc239f52e85edbe8e854b698
  (coordinator/v2200-full-clockpath-diagnostics, checkout --detach;
  SHA сверён test-ом, git status чист)
- Host: vovans-PC
- CPU: AMD Ryzen 5 3600 6-Core Processor, 12 threads
- OS/kernel: Linux 7.0.0-43-generic
- Python: Python 3.14.4
- NumPy: 2.3.5
- SciPy: 1.16.3

## Команды и результаты (по порядку контракта)

| Команда | Exit | Wall | Max RSS |
|---|---|---|---|
| python3 -m py_compile (v2188_estimators.py, v2200_full_clock_path.py, v2200_lineqc_montecarlo.py, v2200_nearby_primary_fit_bias.py) | 0 | — | — |
| /usr/bin/time -v python3 measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2200_full_clock_path.py > measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2200-full-clock-path-run.log 2>&1 | 0 | 0:01.08 | 167840 КБ |
| /usr/bin/time -v python3 measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2200_lineqc_montecarlo.py > measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2200-lineqc-montecarlo-run.log 2>&1 | 0 | 6:58.17 | 124956 КБ |
| /usr/bin/time -v python3 measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2200_nearby_primary_fit_bias.py > measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2200-nearby-primary-fit-bias-run.log 2>&1 | 0 | 8:03.01 | 131988 КБ |

Полные stdout/stderr + time -v: v2200-full-clock-path-run.log,
v2200-lineqc-montecarlo-run.log, v2200-nearby-primary-fit-bias-run.log.
git diff --check: чисто. JSON: v2200-full-clock-path.json,
v2200-lineqc-montecarlo.json, v2200-nearby-primary-fit-bias.json
(per_case/per_trial не сокращались). Harness, estimator, production
исходники не редактировались.
