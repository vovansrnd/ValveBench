# v2.25.2 — execution manifest (agent run on vovans-PC)

- Pinned source SHA: c9fbaae7cf427934f71776bad097c1a892f1d1ae
  (branch coordinator/v2252-raw-probe-transient; checkout --detach в
  изолированном worktree /home/vovans/.cache/valvebench-v2252-c9fbaae-run;
  SHA сверен, tracked-дерево чистое до прогона)
- Result branch: agent/v2252-raw-probe-transient
- Host: vovans-PC; CPU AMD Ryzen 5 3600, 12 threads
- OS/kernel (uname -a, дословно): Linux vovans-PC 7.0.0-43-generic #43-Ubuntu SMP PREEMPT_DYNAMIC Fri Oct 23 13:25:25 UTC 2026 x86_64 GNU/Linux
- Дистрибутив (из /etc/os-release, дословно): NAME="Ubuntu"; VERSION="26.04.1 LTS (Resolute Raccoon)"
- Python 3.14.4, NumPy 2.3.5, SciPy 1.16.3, Git 2.53.0
- PYTHONPYCACHEPREFIX: /home/vovans/.cache/valvebench-v2252-pycache (вне worktree)
- Вход: /tmp/v2250-probe.wav, 5 760 044 байта, SHA-256 d1676fe55e822e703d89c34b1ee2f2fbb96143eb163540e18078081756282ca9 (не менялся с v2.25.0)

## Задокументированное исправление harness (по разрешению пользователя
## «если тесты требуют исправления — исправлять и писать что/почему»)

Первый запуск упал с exit 1: `AttributeError: module 'numpy' has no
attribute '__version'` на строке 289 (блок environment, ПОСЛЕ
завершения всего анализа): в координаторском скрипте опечатка
`np.__version()` вместо `np.__version__`. Исправлен один символ,
анализ не менялся. Traceback первого запуска сохранён в
v2252-raw-probe-transient-analysis-run.log (первый прогон в том же
логе, до второго). Координатору рекомендуется внести ту же правку
в исходник и перевыпустить pinned SHA.

## Команды и результаты

| Команда | Exit | Wall | Max RSS |
|---|---|---|---|
| python3 -m py_compile v2252_raw_probe_transient_analysis.py | 0 | — | — |
| запуск 1 (до исправления опечатки) | 1 | 0:10.13 | 610584 КБ |
| запуск 2 (после исправления 1 символа) | 0 | 0:08.82 | 591140 КБ |

## Валидация JSON

- v2252-raw-probe-transient-analysis.json: валидный JSON; 96 кГц
  стерео, 10.0 с ✓; 20 non-overlap 0.5-с блоков ✓; 77 скользящих
  0.5-с окон (шаг 125 мс) и 9 raw 2-с окон (шаг 1 с) ✓; все числовые
  поля конечны ✓; source bytes/SHA-256 совпадают с preflight ✓

## Опубликованные файлы (4)

- v2252-raw-probe-transient-analysis.json
- v2252-raw-probe-transient-analysis-run.log (2 прогона: fail + OK)
- V2251→V2252 не относятся; этот manifest + AGENT-REPORT

git diff --check: чисто (tracked-изменение — только задокументированная
опечатка). Hardware/ALSA/playback/DUT/WAV не использовались.
