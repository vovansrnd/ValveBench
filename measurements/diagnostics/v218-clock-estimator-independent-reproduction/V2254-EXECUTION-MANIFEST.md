# v2.25.4 — execution manifest

- Pinned analyzer/source SHA: a10e88f8c47751a2b12f504372ba8ee4005a8968
  ("v2.25.4: use consistent half-open sub-bands and bin-width integration").
  Ревизия контракта: координатор заменил pinned SHA 33bb041… на
  a10e88f… (коммиты d2e0474, 8a6427d); исполнение — по финальному
  контракту на a10e88f.
- Result branch: agent/v2254-pilotband-subband-distribution
  (tip до публикации 8a6427dd1f92e6102b8539aff0acb9c0ddb495d3 =
  tip координаторской task-ветки; a10e88f — предок, проверено
  git merge-base --is-ancestor).
- Worktree: /home/vovans/.cache/valvebench-v2254-a10e88f-run
  (изолированный, создан от exact pinned SHA; tracked state чистый
  до и после прогона; результат — untracked JSON+лог, коммитятся
  только они и два документа).
- Host: vovans-PC; CPU AMD Ryzen 5 3600, 12 threads
- OS/kernel (uname -a, дословно): Linux vovans-PC 7.0.0-43-generic #43-Ubuntu SMP PREEMPT_DYNAMIC Fri Oct 23 13:25:25 UTC 2026 x86_64 GNU/Linux
- Дистрибутив (из /etc/os-release): Ubuntu 26.04.1 LTS (Resolute Raccoon)
- Python 3.14.4, NumPy 2.3.5, SciPy 1.16.3, Git 2.53.0

## Входной WAV (metadata-only: test -f, stat, file, sha256sum)

- /tmp/v2250-probe.wav: присутствует; 5 760 044 байта;
  owner vovans, mode 0644; SHA-256
  d1676fe55e822e703d89c34b1ee2f2fbb96143eb163540e18078081756282ca9;
  RIFF WAVE PCM 24 бит, стерео, 96 кГц.
- Анализатор самостоятельно проверил полный SHA источника и совпал
  (первая строка лога); расхождения нет.

## Команды (из финального контракта V2254-EXECUTION.md, дословно)

~~~bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
export PYTHONPYCACHEPREFIX="$HOME/.cache/valvebench-v2254-pycache"
python3 -m py_compile "$D/v2254_pilotband_subband_distribution.py"
/usr/bin/time -v python3 "$D/v2254_pilotband_subband_distribution.py" \
  --wav /tmp/v2250-probe.wav --output "$D" \
  > "$D/v2254-pilotband-subband-distribution-run.log" 2>&1
~~~

- py_compile: без ошибок.
- Прогон: ровно один; exit status 0.
- /usr/bin/time -v: wall 0:02.28; user 2.13 s; sys 0.14 s;
  max RSS 259 204 kB (~253 MiB).
- EXIT_STATUS=0 записан в конец лога.

## Выходной JSON

- v2254-pilotband-subband-distribution.json: **173 789 байт**,
  **SHA-256 381a412f48ef6d12ed4c289d6e5d77cda20bb642d8e922920bce2f18aef6835e**
- Валидация структуры: full_record.subbands = 8 подполос;
  windows = 9; в каждом окне 8 подполос; каждое окно 2.0 с;
  source sha256 в JSON совпадает с SHA WAV.
- git diff --check: чисто.

## Аттестация

- Без ALSA/arecord/aplay/mixer/device opens/перечисления устройств.
- Без записи, playback, синтетических сигналов, нового захвата.
- Без взаимодействия с оборудованием и без изменения его
  конфигурации/кабельных соединений.
- Без изменений production, frozen estimator, анализатора; WAV не
  коммитился; доступ к оборудованию отсутствовал (metadata-only
  доступ к существующему /tmp/v2250-probe.wav).
