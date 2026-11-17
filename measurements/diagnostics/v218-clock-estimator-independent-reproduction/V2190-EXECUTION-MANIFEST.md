# v2.19.0 — execution manifest (agent run on vovans-PC)

- Branch HEAD used for execution: d292e730dc37fe64c3df06276ccce3d519a913f8
  (agent/v2190-coordinator-authored-validation, checkout --detach;
  harness и frozen v2188_estimators.py НЕ редактировались)
- Host: vovans-PC
- CPU: AMD Ryzen 5 3600 6-Core Processor, 12 threads
- OS/kernel: Linux 7.0.0-43-generic
- Python: Python 3.14.4
- NumPy: 2.3.5
- SciPy: 1.16.3

## Команды и результаты

| Команда | Exit | Wall | Max RSS |
|---|---|---|---|
| python3 -m py_compile (v2188_estimators.py, v2190_gate_lineqc_montecarlo.py, v2190_nearby_primary_fit_bias.py) | 0 | — | — |
| /usr/bin/time -v python3 measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2190_gate_lineqc_montecarlo.py > measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2190-gate-lineqc-run.log 2>&1 | 0 | 3:38.30 | 123344 КБ |
| /usr/bin/time -v python3 measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2190_nearby_primary_fit_bias.py > measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2190-primary-fit-bias-run.log 2>&1 | 0 | 3:15.63 | 131080 КБ |

Полные stdout/stderr + time -v: v2190-gate-lineqc-run.log,
v2190-primary-fit-bias-run.log. git diff --check: чисто.
JSON: v2190-gate-lineqc-montecarlo.json, v2190-nearby-primary-fit-bias.json
(per_trial не сокращались).
