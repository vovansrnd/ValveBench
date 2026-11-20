# v2.23.0 — agent failure report (test A blocker)

## Итог: FAILED на первом тесте — отчёт только по факту блокера

Тест A (v2230_narrowband_occupancy.py, 34 ожидаемых case) завершился
**crash-ом с exit 1** на первом же case, до записи JSON. Тесты B
(v2230_pilot_leakage_bias.py, ожидается 544 per_trial) и C
(v2230_independent_psd_uncertainty.py, ожидается 384 per_trial) **не
запускались** — контракт требует остановки на первой ошибке. Технические
вопросы §6A/B/C в этой итерации не отвечаемы: данных нет.

## Точный блокер

- Файл: measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2230_narrowband_occupancy.py
- Закреплённый SHA: 2562f9a424a473bd6c355664b74af49f753016cf
- Точка падения: строка 147, функция metrics_for_channel:
  `_, noise_env = envelope_frames(x, fs, f + OFFBAND_OFFSET_HZ)`
- Причина: **NameError: name OFFBAND_OFFSET_HZ is not defined** —
  константа используется, но не определена в файле.
- Второе использование: строка 218 (JSON-поле
  `offband_demod_offset_hz: OFFBAND_OFFSET_HZ`) — упало бы следом.
- Полнота: grep по файлу — определения имени нет; краш безусловный,
  воспроизводится на любом прогоне, не зависит от окружения
  (Python 3.14.4 / numpy 2.3.5 / scipy 1.16.3 — preflight py_compile
  всех 4 файлов прошёл).
- Проверено на фиксе в более поздних коммитах ветки координатора
  (a19bfeb, 8672045, cb98684 после pinned SHA): файл не менялся —
  исправление не опубликовано.
- Контекст из §6A задания: офф-бендовский прокси описан как
  «off-band demodulated noise proxy at +500 Hz» — вероятно, предполагалось
  `OFFBAND_OFFSET_HZ = 500.0`, но фикс должен выполнить автор теста.

## Полная диагностика сохранена

- v2230-narrowband-occupancy-run.log — полный stdout/stderr + time -v
  (wall 0:02.17, RSS 180.9 МБ, EXIT_STATUS=1).
- Worktree исполнения: /home/vovans/.cache/valvebench-v2230-2562f9a-run
  (detached на 2562f9a, tracked-дерево чистое — ничего не редактировалось).

## Что нужно от координатора

Определить OFFBAND_OFFSET_HZ (по §6A — ожидаемо 500.0) в
v2230_narrowband_occupancy.py и выпустить новый immutable run target
(новый pinned SHA). После этого агент исполняет тот же контракт целиком
(A+B+C). Тесты агентом не исправлялись — по контракту и по поручению.

## Подтверждения

- production/valvebench, frozen estimator, три harness — не редактировались.
- Hardware/ALSA/playback/реальный захват/DUT/WAV — не использовались.
- git diff --check: чисто; published — только failed-run subset
  (лог теста A, манифест, этот отчёт).
