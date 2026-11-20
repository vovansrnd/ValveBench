# v2.23.0 — execution manifest (agent run on vovans-PC) — FAILED AT TEST A

**Статус: FAILED/PARTIAL** — первый тест (A, narrowband occupancy) завершился
crash-ом до записи JSON; тесты B и C не запускались (stop-at-first-failure
по контракту). Отсутствующий v2230-narrowband-occupancy.json НЕ фабриковался.

- Frozen source SHA (единственный допустимый): 2562f9a424a473bd6c355664b74af49f753016cf
  (branch coordinator/v2230-narrowband-pilot-validation, checkout --detach
  в изолированном worktree /home/vovans/.cache/valvebench-v2230-2562f9a-run;
  в поздних коммитах a19bfeb/8672045/cb98684 тестовый файл НЕ менялся)
- Result branch: agent/v2230-coordinator-authored-validation
  (frozen SHA — предок result branch: проверено merge-base)
- Host: vovans-PC, CPU AMD Ryzen 5 3600 6-Core, 12 threads
- OS/kernel: Linux 6.x (см. uname в логе), Python 3.14.4, NumPy 2.3.5, SciPy 1.16.3, Git 2.53.0
- PYTHONPYCACHEPREFIX: ~/.cache/valvebench-v2230-pycache (вне worktree)

## Команды

| Команда | Exit | Wall | Max RSS |
|---|---|---|---|
| python3 -m py_compile (4 файла) | 0 | — | — |
| /usr/bin/time -v python3 $D/v2230_narrowband_occupancy.py | **1** | 0:02.17 | 180896 КБ |
| v2230_pilot_leakage_bias.py | не запускался | — | — |
| v2230_independent_psd_uncertainty.py | не запускался | — | — |

## Блокер (точная диагностика)

`v2230_narrowband_occupancy.py`, NameError на строке 147
(metrics_for_channel): имя **OFFBAND_OFFSET_HZ не определено**.
Используется дважды: строка 147 (`envelope_frames(x, fs, f+OFFBAND_OFFSET_HZ)`)
и строка 218 (JSON-поле offband_demod_offset_hz). Определения в файле НЕТ.
По §6A задания офф-бендовский прокси ожидается «at +500 Hz», но значение
константы должен зафиксировать автор теста. Краш безусловный (не зависит
от окружения): падает на первом же case до записи JSON.

git diff --check: чисто; tracked-файлы не менялись; production, frozen
estimator и harness не редактировались; hardware/ALSA/playback/DUT/WAV
не использовались.

## Опубликованные файлы (failed-run subset контракта §9)

- v2230-narrowband-occupancy-run.log (полный traceback + time -v, EXIT_STATUS=1)
- V2230-EXECUTION-MANIFEST.md (этот файл)
- V2230-AGENT-REPORT.md (failure-отчёт)
