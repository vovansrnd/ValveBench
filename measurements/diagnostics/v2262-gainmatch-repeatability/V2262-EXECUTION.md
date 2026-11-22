# v2.26.2 — повторяемость Gain Match на подтверждённом уровне −24 dBFS

## Цель

Подтвердить, что результат v2.26.1 r1 воспроизводим в нескольких независимых
аппаратных сессиях. Главный результат v2.26.1 — PASS на −24 dBFS, но
проверка первичных repeat-значений выявила худший C/R ослабленного L
**30.736754 dB**, то есть запас всего **0.736754 dB** над неизменным
production-порогом 30 dB. В отчёте r1 ошибочно приведён channel-level summary
31.3 dB как нижняя граница C/R; в этом этапе обязательно анализировать
каждый repeat отдельно.

Вторичный вопрос: повторится ли единичный `unstable_carrier` на R / 94 Hz /
repeat 2 при −18 dBFS (v2.26.1 r1: `z_burst=5.8123`) или это был единичный
эпизод.

## Пиннинг и неизменяемые исходники

- Исполнять строго pinned SHA:
  `564a4af239c7c78f7661099656cc80fc06efc9b1`.
- Отдельный detached worktree; не использовать рабочую копию.
- Тот же v2.26.1 production harness и тесты, без изменений.
- Результатная ветка: `agent/v2262-gainmatch-repeatability`, создать от
  pinned SHA.
- Ветка v2261 r1 и BLOCKED-ветка первой попытки не переписываются.
- Не изменять `valvebench/`, harness, тесты, production thresholds,
  калибровки или конфигурацию.
- Если появляется ошибка исходников/offline gate, остановиться до железа.

## Физический маршрут и безопасность

Подтверждённая цепь: SMSL D10 playback → E-MU 0404 USB analog capture.
Кабели оставить как есть; DUT/фонокорректор не подключать.
Никаких изменений mixer, hardware gain, volume, routing, системного уровня,
частоты дискретизации или настроек карт. Не выполнять Setup-калибровки,
passport, mixer writes или ALSA amixer writes.

Сделать свежий read-only inventory. Сопоставить именно D10 Playback и E-MU
Capture и заново проверить общий режим 192000 Hz. Не переносить ALSA индексы
из предыдущего запуска вслепую.

## Офлайн-гейты

Из корня pinned worktree:

    set -euo pipefail
    D0=measurements/diagnostics/v2260-gainmatch-hardware-validation
    D1=measurements/diagnostics/v2261-gainmatch-level-margin

    python3 -m py_compile \
      "$D0/v2260_gainmatch_hardware_validation.py" \
      "$D0/test_v2260_gainmatch_hardware_validation.py" \
      "$D1/v2261_gainmatch_level_margin.py" \
      "$D1/test_v2261_gainmatch_level_margin.py"

    PYTHONPATH=.:$D0:$D1 python3 -m unittest discover -s "$D0" -p 'test_v226*.py' -v
    PYTHONPATH=.:$D0:$D1 python3 -m unittest discover -s "$D1" -p 'test_v226*.py' -v
    ./run.sh selftest

Оба набора должны пройти; ProductionSimulatorIntegrationTests обязан
выполниться, не skipped. Selftest должен PASS. При любом offline failure
аппаратный этап запрещён.

## План — три независимые сессии

Выполнить **ровно три** последовательных аппаратных запуска тем же
v2.26.1 harness. Каждый запуск самостоятельно выполняет:
1. clock preflight;
2. −24 dBFS: loopback → отдельный reference → inject-R → inject-L;
3. −18 dBFS: новый loopback → отдельный reference → inject-R → inject-L.

Таким образом, для каждого запуска создаётся собственный набор references
обоих уровней; никаких reference между запусками или уровнями не переносить.
Сессии выполнять по очереди, не параллельно. Не создавать out-dir заранее:
его создаёт harness. Команды запускать из корня worktree.

Сначала:

    D2=measurements/diagnostics/v2262-gainmatch-repeatability
    mkdir -p "$D2/results"
    ./run.sh cli devices | tee "$D2/devices-inventory.txt"
    ./run.sh cli cards | tee "$D2/cards-inventory.txt"

По свежей инвентаризации подтвердить playback/capture и общий 192000 Hz.
Затем выполнить три команды последовательно, подставив фактические текущие
индексы в `hw:D10_CARD,0` и `hw:EMU_CARD,0`:

    PYTHONPATH=. python3 measurements/diagnostics/v2261-gainmatch-level-margin/v2261_gainmatch_level_margin.py \
      --dac hw:D10_CARD,0 --adc hw:EMU_CARD,0 --fs 192000 \
      --out-dir "$D2/results/vovans-PC-run-01" \
      2>&1 | tee "$D2/results/vovans-PC-run-01-console.log"

    PYTHONPATH=. python3 measurements/diagnostics/v2261-gainmatch-level-margin/v2261_gainmatch_level_margin.py \
      --dac hw:D10_CARD,0 --adc hw:EMU_CARD,0 --fs 192000 \
      --out-dir "$D2/results/vovans-PC-run-02" \
      2>&1 | tee "$D2/results/vovans-PC-run-02-console.log"

    PYTHONPATH=. python3 measurements/diagnostics/v2261-gainmatch-level-margin/v2261_gainmatch_level_margin.py \
      --dac hw:D10_CARD,0 --adc hw:EMU_CARD,0 --fs 192000 \
      --out-dir "$D2/results/vovans-PC-run-03" \
      2>&1 | tee "$D2/results/vovans-PC-run-03-console.log"

Каждый процесс должен быть завершён до следующего. Не использовать параллельный
запуск и не повторять уже завершённую сессию. При fatal hardware event, XRUN,
clip, пропадании устройства или неожиданном изменении mixer fingerprint —
остановить серию, сохранить доказательства и не запускать следующий экземпляр.
Если сессия завершилась штатно, но C/R/envelope/ΔG не прошёл QC, это результат
измерения, а не основание для скрытого retry; продолжить остальные заранее
запланированные сессии, если нет аппаратного стоп-события.

## Критерии и агрегация

Производственные критерии не менять:
- C/R каждого канала и каждого repeat ≥30.0 dB;
- production envelope stability gate;
- все строки valid и repeat_stable;
- corrected ΔG каждой частоты в пределах ±0.20 dB от цели;
- нет ADC clip и fatal hardware events;
- все audio sample arrays конечны.

**Главный v2.26.2 PASS:** уровень −24 dBFS полностью PASS во всех трёх
независимых сессиях. Если хотя бы в одной сессии −24 не проходит хотя бы один
штатный критерий — общий результат FAIL. Никаких послаблений или усреднения
через невалидную сессию.

Уровень −18 dBFS — вторичный диагностический результат. Его PASS/FAIL
фиксировать отдельно для каждой сессии; он не заменяет и не отменяет основной
критерий повторяемости −24 dBFS.

В отчёте обязательно извлекать C/R из
`rows[*].channels[*].repeats[*].carrier_residual_db`, а не только из
`rows[*].channels[*].carrier_residual_db` (channel-level summary).
Привести худший individual-repeat C/R:
- отдельно для inject-R / ослабленного R;
- отдельно для inject-L / ослабленного L;
- по каждой частоте и по каждой сессии;
- общий минимум и запас над 30 dB.

C/R и SINAD показывать раздельно. Для каждого invalid repeat указывать
частоту, канал, номер повтора, `validity_reasons`, `z_burst`,
`trend_span`, C/R, SINAD и peak. По −18 отдельно проследить R/94 Hz
envelope outlier.

Также дать таблицу corrected ΔG, correction и repeat spread по трём частотам
в каждом запуске. Сравнить loopback corrections между независимыми сессиями.

## Capture metadata и NaN

В текущем pinned harness два preflight NPZ намеренно получают
`generator_level_dbfs=np.nan`, когда уровень ещё не задан на clock preflight.
Это скалярное поле метаданных, не аудиоданные. Учитывать его отдельно и
не представлять как NaN в submitted/capture arrays. Не менять harness в этом
этапе. Все значения в аудиомассивах `submitted_to_backend` и `capture`
должны быть finite (0 NaN / 0 Inf).

В каждом запуске проверить SHA256 manifest независимо: все перечисленные
файлы и размеры, включая все captures и JSON. Приложить список путей и итог
проверки.

## Артефакты и публикация

Для каждого из трёх запусков сохранить полный каталог harness:
`run-summary.json`, `clock-preflight.json`, обе папки уровней с series и
references, `captures/*.npz`, `SHA256SUMS.json`, плюс отдельный полный
console log. Сохранить inventory, офлайн-логи, execution manifest и полный
agent report в `measurements/diagnostics/v2262-gainmatch-repeatability/`.

Публиковать только результаты/логи/manifest/report в новой ветке
`agent/v2262-gainmatch-repeatability`. Source, tests и pinned v2261 task
files не менять. Обычный commit и fast-forward push, без force push/reset/rebase.
Перед коммитом `git diff --check`; подтвердить отсутствие production diff.

В отчёте вернуть:
- commit SHA и точный pinned SHA;
- py_compile, D0, D1 и selftest totals;
- inventory mapping и clock preflight каждого запуска;
- PASS/FAIL по −24 и −18 в каждом запуске;
- worst per-repeat C/R и margin over 30 dB;
- invalid envelope events;
- corrected ΔG;
- peak/clip/fatal/XRUN;
- число manifest entries и подтверждение SHA256 каждого запуска;
- точные пути всех артефактов.

Никаких изменений физической схемы подключения или аппаратных настроек.
