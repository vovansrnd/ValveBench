# v2.23.0 — execution manifest (rerun, agent run on vovans-PC)

- Frozen source SHA: 0a18994ca96e5a04ec69323a9fef8625e735f1a8
  (branch coordinator/v2230-narrowband-pilot-validation-rerun; fix
  OFFBAND_OFFSET_HZ = 500.0; checkout --detach в изолированном worktree
  /home/vovans/.cache/valvebench-v2230-rerun-0a18994-run; SHA сверен, дерево чистое)
- Result branch: agent/v2230-coordinator-authored-validation-rerun
  (frozen SHA — предок: проверено merge-base)
- Первый неудачный прогон сохранён отдельно на
  agent/v2230-coordinator-authored-validation (9328778) — не модифицировался
- Host: vovans-PC, AMD Ryzen 5 3600, 12 threads
- OS/kernel: Linux 6.x
- Python 3.14.4, NumPy 2.3.5, SciPy 1.16.3, Git 2.53.0
- PYTHONPYCACHEPREFIX: ~/.cache/valvebench-v2230-rerun-pycache (вне worktree)

## Команды и результаты

| Команда | Exit | Wall | Max RSS |
|---|---|---|---|
| python3 -m py_compile (4 файла) | 0 | — | — |
| python3 $D/v2230_narrowband_occupancy.py | 0 | 0:04.64 | 437656 КБ |
| python3 $D/v2230_pilot_leakage_bias.py | 0 | 5:32.36 | 167508 КБ |
| python3 $D/v2230_independent_psd_uncertainty.py | 0 | 4:06.69 | 205688 КБ |

Все три с /usr/bin/time -v, полные stdout/stderr в соответствующих
*-run.log; EXIT_STATUS=0 в каждом.

## Валидация JSON

| JSON | строк (ожидалось) | байт | SHA-256 (первые 16) | inf/non-finite |
|---|---|---|---|---|
| v2230-narrowband-occupancy.json | 34/34 (per_case) | 128628 | 6bac69bf4c132693 | 0 |
| v2230-pilot-leakage-bias.json | 544/544 (per_trial) | 390116 | d17def2fb169b66f | 0 |
| v2230-independent-psd-uncertainty.json | 384/384 (per_trial) | 249632 | 1960a799d0037a6a | 0 |

git diff --check: чисто. Production, frozen estimator и три harness не
редактировались. Hardware/ALSA/playback/реальный захват/DUT/WAV не
использовались. Опубликованные файлы: 8 из allowlist §9.
