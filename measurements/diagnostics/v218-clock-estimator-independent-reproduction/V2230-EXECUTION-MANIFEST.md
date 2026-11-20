# v2.23.0 — execution manifest (rerun, agent run on vovans-PC)

- Frozen source SHA: 0a18994ca96e5a04ec69323a9fef8625e735f1a8
  (branch coordinator/v2230-narrowband-pilot-validation-rerun; fix
  OFFBAND_OFFSET_HZ = 500.0; checkout --detach в изолированном worktree
  /home/vovans/.cache/valvebench-v2230-rerun-0a18994-run; SHA сверен
  test-ом "$(git rev-parse HEAD)", дерево чистое до прогона)
- Result branch: agent/v2230-coordinator-authored-validation-rerun
  (frozen SHA — предок: проверено merge-base --is-ancestor)
- Первый неудачный прогон сохранён отдельно на
  agent/v2230-coordinator-authored-validation (9328778) — не модифицировался
- Host: vovans-PC
- CPU: AMD Ryzen 5 3600 6-Core Processor, 12 threads
- OS/kernel (uname -a): Linux vovans-PC 7.0.0-43-generic #43-Ubuntu SMP
  PREEMPT_DYNAMIC Fri Oct 23 13:25:25 UTC 2026 x86_64 GNU/Linux
- Дистрибутив: Ubuntu 26.04.1 LTS (Resolute Raccoon)
- Python 3.14.4, NumPy 2.3.5, SciPy 1.16.3, Git 2.53.0
- PYTHONPYCACHEPREFIX: ~/.cache/valvebench-v2230-rerun-pycache (вне worktree)

## Точные команды запуска (из корня клона, по контракту)

```
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
export PYTHONPYCACHEPREFIX=$HOME/.cache/valvebench-v2230-rerun-pycache
/usr/bin/time -v python3 "$D/v2230_narrowband_occupancy.py" \
  > "$D/v2230-narrowband-occupancy-run.log" 2>&1
/usr/bin/time -v python3 "$D/v2230_pilot_leakage_bias.py" \
  > "$D/v2230-pilot-leakage-bias-run.log" 2>&1
/usr/bin/time -v python3 "$D/v2230_independent_psd_uncertainty.py" \
  > "$D/v2230-independent-psd-uncertainty-run.log" 2>&1
```

## Результаты

| Команда | Exit | Wall | Max RSS |
|---|---|---|---|
| python3 -m py_compile (4 файла) | 0 | — | — |
| python3 $D/v2230_narrowband_occupancy.py | 0 | 0:04.64 | 437656 КБ |
| python3 $D/v2230_pilot_leakage_bias.py | 0 | 5:32.36 | 167508 КБ |
| python3 $D/v2230_independent_psd_uncertainty.py | 0 | 4:06.69 | 205688 КБ |

## Валидация JSON (row counts, размеры, полные SHA-256)

| JSON | строк (ожидалось) | байт | SHA-256 (полный, 64 симв.) |
|---|---|---|---|
| v2230-narrowband-occupancy.json | 34/34 (per_case) | 128628 | 6bac69bf4c1326937853e8d5d18d8e05a47bd8ae729f0489331a2e054e2fedb7 |
| v2230-pilot-leakage-bias.json | 544/544 (per_trial) | 390116 | d17def2fb169b66ff6e01ed89532c2ea841725545112dbaf54ce494eabee38d0 |
| v2230-independent-psd-uncertainty.json | 384/384 (per_trial) | 249632 | 1960a799d0037a6a66e1d19df3a8d2165b1d04ff924746a15b999391bba8e265 |

SHA-256 вычислены sha256sum по опубликованным файлам в момент оформления
манифеста (файлы не менялись с момента валидации).

git diff --check: чисто. Production, frozen estimator и три harness не
редактировались. Hardware/ALSA/playback/реальный захват/DUT/WAV не
использовались. Опубликованные файлы: 8 из allowlist §9.
