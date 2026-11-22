# v2.26.3 — квалификация кандидата Gain Match −20 dBFS
 
## Предыдущая попытка — BLOCKED до аппаратного этапа

Первая попытка на SHA `96da4c6f34a4e28def58d2dca942a5a52c93b439`
остановлена корректно до inventory и любых обращений к аудиоустройствам.
В тестовом файле D3 ошибочно импортировался модуль
`v2261_gainmatch_level_margin`, поэтому большая часть D3 suite, включая
интеграционный тест, фактически проверяла не v2263 harness.

В текущей ревизии исправлен только
`test_v2263_gainmatch_operating_level.py`:
- импортируется `v2263_gainmatch_operating_level`;
- отдельный контрактный тест сравнивает `harness.__file__` с путём к
  `v2263_gainmatch_operating_level.py`, чтобы исключить импорт не того этапа;
- production harness, production package, уровни, measurement plan и QC
  thresholds не менялись.

**Повторное исполнение разрешено только на новом полном pinned SHA,
переданном координатором.** Не использовать старый SHA `96da4c6...`.
Сначала заново выполнить все offline gates. D3 suite теперь содержит 7 тестов;
обязательны 7/7 OK, включая ProductionSimulatorIntegrationTests, который
должен реально выполниться, не skipped. Затем только допускается hardware.

Результаты повторной попытки публиковать в новой ветке
`agent/v2263-operating-level-qualification-r1`; предыдущую BLOCKED-ветку
не переписывать.

## Цель

v2.26.2 показал, что −24 dBFS не является надёжно проходящим уровнем:
полный PASS был только в 2 из 3 независимых сессий. В третьей один repeat
L / 9994 Hz получил C/R=29.6066 dB (<30.0), при стабильном carrier и
поднявшемся остаточном шуме.

−18 dBFS дал больший C/R запас, но за четыре аппаратные сессии (v2261 r1
и v2262) было только два полных PASS; в двух других встречались единичные
unstable_carrier события на R на разных частотах. Их природа не установлена.

Проверить один промежуточный кандидат **−20 dBFS** в трёх независимых
аппаратных сессиях. Не предполагать заранее, что он пройдёт.

## Пиннинг и scope

- Исполнять точный pinned SHA, переданный координатором вместе с заданием.
- Новая отдельная detached worktree.
- Результатная ветка: agent/v2263-operating-level-qualification-r1,
  созданная от pinned SHA. Предыдущую BLOCKED-ветку не переписывать.
- Harness: measurements/diagnostics/v2263-operating-level-qualification/v2263_gainmatch_operating_level.py
- Tests: measurements/diagnostics/v2263-operating-level-qualification/test_v2263_gainmatch_operating_level.py
- В harness зафиксирован один уровень: −20.0 dBFS.
- D3 suite должен пройти 7/7; тест identity обязан подтвердить, что импортирован
  именно файл v2263_gainmatch_operating_level.py.
- Не менять harness, tests, production valvebench/, QC thresholds,
  calibration, UI, hardware routing или ALSA mixer.
- Не менять исходники v2260/v2261. При offline failure к устройствам не обращаться.

## Физический маршрут и безопасность

SMSL D10 playback → E-MU 0404 USB analog capture. Кабели оставить как есть.
DUT/фонокорректор не подключать. Не менять mixer, hardware gain, volume,
routing, системный уровень, sample rate, карты, настройки или persistent config.
Не выполнять Setup-калибровки, passport, mixer writes или amixer writes.

Перед запуском сделать свежий read-only inventory. Заново определить D10
Playback и E-MU Capture и проверить общий режим 192000 Hz. Не переносить
старые ALSA индексы без проверки.

## Офлайн-гейты

Из корня pinned worktree:

    set -euo pipefail
    D0=measurements/diagnostics/v2260-gainmatch-hardware-validation
    D1=measurements/diagnostics/v2261-gainmatch-level-margin
    D3=measurements/diagnostics/v2263-operating-level-qualification

    python3 -m py_compile \
      "$D0/v2260_gainmatch_hardware_validation.py" \
      "$D0/test_v2260_gainmatch_hardware_validation.py" \
      "$D1/v2261_gainmatch_level_margin.py" \
      "$D1/test_v2261_gainmatch_level_margin.py" \
      "$D3/v2263_gainmatch_operating_level.py" \
      "$D3/test_v2263_gainmatch_operating_level.py"

    PYTHONPATH=.:$D0:$D1:$D3 python3 -m unittest discover -s "$D0" -p 'test_v226*.py' -v
    PYTHONPATH=.:$D0:$D1:$D3 python3 -m unittest discover -s "$D1" -p 'test_v226*.py' -v
    PYTHONPATH=.:$D0:$D1:$D3 python3 -m unittest discover -s "$D3" -p 'test_v226*.py' -v
    ./run.sh selftest

В каждом suite ProductionSimulatorIntegrationTests должен реально выполниться,
не skipped. Все suites и selftest должны пройти. Иначе аппаратный этап запрещён.

## План измерения

Только −20 dBFS; Fs=192000 Hz; frequencies=94/994/9994 Hz; 3 repeats;
blocks=2; settle=0.5 s; mains_avoidance_50; attenuation ровно 6.020599913 dB
(0.5×).

Одна сессия:
1. clock preflight;
2. unattenuated loopback series → собственный reference;
3. inject-R series;
4. inject-L series.

Выполнить **ровно три независимые сессии** последовательно. У каждой
сессии собственный preflight и свой loopback reference. Не переносить
reference между сессиями. Не запускать параллельно, не повторять сессию.

Сначала:

    D3=measurements/diagnostics/v2263-operating-level-qualification
    mkdir -p "$D3/results"
    ./run.sh cli devices | tee "$D3/devices-inventory.txt"
    ./run.sh cli cards | tee "$D3/cards-inventory.txt"

После проверки endpoint mapping и общего 192 kHz выполнить три команды
последовательно, подставив свежие индексы:

    PYTHONPATH=. python3 "$D3/v2263_gainmatch_operating_level.py" \
      --dac hw:D10_CARD,0 --adc hw:EMU_CARD,0 --fs 192000 \
      --out-dir "$D3/results/vovans-PC-run-01" \
      2>&1 | tee "$D3/results/vovans-PC-run-01-console.log"

    PYTHONPATH=. python3 "$D3/v2263_gainmatch_operating_level.py" \
      --dac hw:D10_CARD,0 --adc hw:EMU_CARD,0 --fs 192000 \
      --out-dir "$D3/results/vovans-PC-run-02" \
      2>&1 | tee "$D3/results/vovans-PC-run-02-console.log"

    PYTHONPATH=. python3 "$D3/v2263_gainmatch_operating_level.py" \
      --dac hw:D10_CARD,0 --adc hw:EMU_CARD,0 --fs 192000 \
      --out-dir "$D3/results/vovans-PC-run-03" \
      2>&1 | tee "$D3/results/vovans-PC-run-03-console.log"

Каталог каждого out-dir до команды должен отсутствовать — его создаёт harness.
Каждый запуск завершить и проверить summary до следующего.

Exit 2 используется и при штатном QC FAIL, поэтому не позволяй set -e
автоматически завершить весь план. Для каждой команды используй set +e,
сохрани PIPESTATUS[0], затем set -e и прочитай run-summary.json.
Если level status=FAIL, но все серии записаны штатно, нет fatal/clip/XRUN/
device error и summary не BLOCKED_OR_FAIL — продолжай следующий run.
При BLOCKED, fatal event, clip, XRUN, потере устройства или изменении
mixer fingerprint — остановись.

## Неизменные критерии production

- C/R carrier_residual_db ≥30.0 dB для каждого канала и каждого repeat;
- штатный envelope stability gate;
- все строки valid и repeat_stable;
- corrected ΔG каждой частоты в пределах ±0.20 dB от цели
  (+6.020599913 inject-R, −6.020599913 inject-L);
- no clipping, no fatal hardware events / XRUN;
- все аудиомассивы submitted_to_backend и capture finite.

**Главный PASS v2.26.3:** −20 dBFS полностью PASS во всех трёх сессиях.
Хотя бы одна полная сессия FAIL означает общий FAIL. Не усреднять невалидные
повторы и не компенсировать один FAIL другими успешными сессиями.
QC-критерии и пороги не менять.

## Per-repeat отчёт

Для каждого run и каждой частоты показать corrected ΔG, loopback correction,
repeat spread, row validity и repeat_stable.

C/R извлекать из rows[*].channels[*].repeats[*].carrier_residual_db,
отдельно для L/R, особенно для ослабленного L. Для каждой частоты указать
minimum individual-repeat C/R и margin over 30 dB; дать общий минимум
по сессии и трём сессиям. Не подменять per-repeat минимум channel-level
summary.

C/R и SINAD показывать раздельно. Для invalid repeat привести frequency,
channel, repeat number, validity_reasons, z_burst, trend_span, C/R, SINAD,
noise_dbfs, level_dbfs и peak_dbfs.

Сравнить loopback corrections между сессиями. Указать preflight ppm,
mixer fingerprint identity, peak, clipped, fatal events, XRUN/backend notes
и статус каждой сессии.

## NaN и манифесты

В preflight NPZ поле generator_level_dbfs ожидаемо содержит NaN sentinel,
потому что уровень ещё не задан. Учитывать только как metadata.
Аудиомассивы submitted_to_backend и capture должны иметь 0 NaN / 0 Inf.

Для каждого run независимо проверить SHA256 и размеры всех файлов по
SHA256SUMS.json; ожидаются 18 файлов: 8 NPZ + 10 JSON.

## Публикация

В measurements/diagnostics/v2263-operating-level-qualification/ сохранить:
- оба inventory;
- полные offline logs;
- три полных run-каталога с series/reference/captures/SHA256SUMS;
- три console logs;
- execution manifest и полный agent report.

Публиковать только результаты/logs/manifests/report в
agent/v2263-operating-level-qualification. Source/tests/task files не менять.
git diff --check чисто; production diff отсутствует; обычный commit и
fast-forward push, без force push/reset/rebase.

В отчёте вернуть commit SHA, pinned SHA, offline totals, fresh inventory,
preflight каждого run, PASS/FAIL, per-repeat C/R minima и margins, ΔG,
invalid reasons, peak/clip/fatal/XRUN, counts и SHA256 status, точные пути.

Никаких физических перестановок или изменений аудио-настроек.
