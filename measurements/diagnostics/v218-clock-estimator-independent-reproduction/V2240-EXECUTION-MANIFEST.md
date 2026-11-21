# v2.24.0 — execution manifest (agent run on vovans-PC)

- Frozen source SHA (pinned, единственный допустимый): ea5656280f385b172d60bffe750ff31b76f7a016
  (branch coordinator/v2240-estimator-sigma-coverage; checkout --detach;
  SHA сверен test-ом "$(git rev-parse HEAD)" = ea56562…; tracked-дерево
  чистое до прогона)
- Isolated worktree: /home/vovans/.cache/valvebench-v2240-ea56562-run
  (создан заново; до создания проверено — не существовал)
- Result branch: agent/v2240-estimator-sigma-coverage
  (frozen SHA — предок result branch: проверено merge-base --is-ancestor)
- Host: vovans-PC
- CPU: AMD Ryzen 5 3600 6-Core Processor, 12 threads
- OS/kernel (uname -a, дословно): Linux vovans-PC 7.0.0-43-generic #43-Ubuntu SMP PREEMPT_DYNAMIC Fri Oct 23 13:25:25 UTC 2026 x86_64 GNU/Linux
- Дистрибутив (из /etc/os-release, дословно): NAME="Ubuntu"; VERSION="26.04.1 LTS (Resolute Raccoon)"
- Python 3.14.4, NumPy 2.3.5, SciPy 1.16.3, Git 2.53.0
- PYTHONPYCACHEPREFIX: /home/vovans/.cache/valvebench-v2240-pycache (вне worktree)

## Точная команда компиляции (preflight)

```
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
PYTHONPYCACHEPREFIX=/home/vovans/.cache/valvebench-v2240-pycache \
python3 -m py_compile \
  "$D/v2188_estimators.py" \
  "$D/v2240_estimator_sigma_coverage.py"
```
Exit code: 0.

## Точная команда запуска

```
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
/usr/bin/time -v python3 "$D/v2240_estimator_sigma_coverage.py" \
  > "$D/v2240-estimator-sigma-coverage-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2240-estimator-sigma-coverage-run.log"
```

## Результаты

| Команда | Exit | Wall | Max RSS |
|---|---|---|---|
| python3 -m py_compile (2 файла) | 0 | — | — |
| python3 $D/v2240_estimator_sigma_coverage.py | 0 | 17:23.22 | 228380 КБ |

## Валидация JSON

- per_trial строк: **1536** (ожидается 1536) ✓
- независимых trial-индексов: 128 на ячейку × 6 ячеек; 256 channel rows
  на ячейку; L и R присутствуют в каждом trial ✓
- все числовые поля валидных fit-строк конечны ✓
- байт: 1587951
- SHA-256 (полный, 64 симв.):
  757d843be0c9247260b5aefa0916d5f9842798470ee8d35859cd98445e5d80ba

git diff --check: чисто. Production, frozen estimator v2188_estimators.py
и harness v2240_estimator_sigma_coverage.py не редактировались.
Hardware/ALSA/playback/реальный захват/DUT/WAV не использовались.

## Опубликованные файлы (4 из контракта §10)

1. v2240-estimator-sigma-coverage.json
2. v2240-estimator-sigma-coverage-run.log
3. V2240-EXECUTION-MANIFEST.md
4. V2240-AGENT-REPORT.md
