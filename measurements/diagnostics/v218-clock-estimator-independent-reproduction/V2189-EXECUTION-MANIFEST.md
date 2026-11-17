# v2.18.9 — execution manifest (agent run on vovans-PC)

- Branch HEAD at run: 7eb4fb9dc3200dc59228b74b213cb342e2748bfb (7eb4fb9, coordinator-authored)
- Host: vovans-PC — AMD Ryzen 5 3600 6-Core Processor, 12 threads
- OS: Linux 7.0.0-43-generic
- Python: Python 3.14.4
- NumPy: 2.3.5
- SciPy: 1.16.3

| Команда | Exit | Время (wall) |
|---|---|---|
| python3 -m py_compile (3 файла) | 0 | — |
| python3 v2189_paired_snr_clockcheck.py | 0 | см. лог |
| python3 v2189_nearby_offbin_sweep.py | 0 | 8:33.66 |

Полные логи: v2189-snr-run.log, v2189-nearby-run.log (stdout+stderr +
/usr/bin/time -v). git diff --check: чисто. Изменения harness/estimator:
нет (координаторский комплект исполнен как есть).
