# v2.26.1 — аппаратная проверка запаса C/R по уровню возбуждения (D10 → E-MU 0404)

## Цель

Продолжить v2.26.0 без изменения production-кода и порогов. Определить, проходит ли production Gain Match на реальной паре D10 playback → E-MU 0404 capture обе полярности известного межканального дисбаланса при более высоком, но всё ещё низком цифровом уровне возбуждения.

Основание для этапа:
- v2.26.0 при −30 dBFS правильно восстановил численные injected ΔG, но ослабленный L получил C/R ниже production-порога 30 dB.
- На 9994 Hz в inject-L был также один отдельный envelope-stability outlier на R.
- Координаторский разбор v2.26.0 приложен как V2260-COORDINATOR-REVIEW.md. В нём точно разведены C/R и SINAD; источник асимметрии не назначается.

## Пиннинг и роли

- Исполнять только точный SHA coordinator-ветки, указанный координатором в сообщении.
- Отдельный detached worktree, без переключения основной рабочей копии.
- Координатор написал harness и тесты; агент только выполняет проверки и публикует неизменённые результаты.
- Не исправлять код/тесты/критерии по ходу выполнения. При ошибке — остановиться, сохранить лог и вернуть координатору.
- Не менять production \`valvebench/\`, GUI, persistent config и существующие калибровки.

## Известный физический маршрут

SMSL D10 playback → E-MU 0404 USB analog capture. Это та же подтверждённая историческая цепь и та же пара v2.26.0. Не спрашивать пользователя повторно о соединении.

Свежая read-only инвентаризация обязательна:
- сопоставить D10 именно как Playback;
- E-MU 0404 USB именно как Capture;
- подтвердить общий 192000 Hz;
- номера \`hw:N,0\` получить заново, не переносить прошлые номера вслепую.
Если пара определяется неоднозначно — остановиться после inventory.

## Безопасность

- Существующие кабели оставить как есть; не отсоединять, не переставлять.
- DUT/фонокорректор не подключать.
- Не менять mixer, hardware gain, volume, routing, system sound level, sample rate или настройки карт.
- На обоих уровнях −24 и −18 dBFS цифровой тестовый синус остаётся с большим запасом до 0 dBFS. В injection только ослабляется один канал ровно в 2 раза; никакого положительного gain нет.
- Применяются только тестовые digital-zero guards: 1.5 s lead и 0.6 s tail, подтверждённые прежними D10 испытаниями. Это in-memory wrapper, production не меняется.
- Harness создаёт свежий \`Calibration()\`; пользовательский \`~/.config/valvebench/config.json\` не читает и не сохраняет.
- Никаких Setup-калибровок, passport, mixer writes, ALSA amixer writes или других процедур конфигурации.

## Зафиксированный план измерения

Fs=192000 Hz; частоты 94, 994, 9994 Hz; 3 повтора на частоту; blocks=2; settle=0.5 s; профиль \`mains_avoidance_50\`; D10 guard lead=1.5 s / tail=0.6 s.

Уровни исполняются строго по порядку:
1. −24 dBFS;
2. −18 dBFS.

На КАЖДОМ уровне заново и последовательно:
1. unattenuated loopback series → построить отдельный loopback-reference;
2. inject-R: ослабление только R на 6.020599913 dB → измерение с reference данного уровня;
3. inject-L: ослабление только L на 6.020599913 dB → измерение с тем же reference данного уровня.

Итого 2 отдельных loopback references и 6 измерительных серий. Нельзя использовать reference с другого уровня. Нельзя менять порядок и число повторов.

Отдельный clock preflight один раз до первой серии (штатная функция split_clock_preflight). Если он не проходит — прекратить аппаратные измерения.

## Неизменные критерии

Production thresholds не менять:
- C/R: минимум 30.0 dB для каждого измеренного канала/повтора;
- envelope stability: штатный предел production;
- ADC clip и fatal hardware events — недопустимы;
- повторяемость строки — \`repeat_stable=true\`;
- численный corrected ΔG для каждой частоты должен быть в пределах ±0.20 dB от ожидаемого:
  - inject-R: +6.020599913 dB;
  - inject-L: −6.020599913 dB.

В отчётах отдельно показывать:
- \`carrier_residual_db\` — C/R, фактический production gate;
- \`sinad_db\` — SINAD, отдельная метрика;
- \`noise_dbfs\`, \`level_dbfs\`, \`peak_dbfs\`, \`envelope.z_burst\`;
- validity reasons каждого повтора;
- corrected ΔG, loopback correction и repeat spread.

**Не называть C/R значением SINAD.** Не приписывать L/R асимметрию самой E-MU: физический источник не установлен, тестирует весь D10→E-MU маршрут.

## Итоговая логика

Для каждого уровня выставить отдельный результат:
- **PASS на уровне** — loopback valid/stable; обе injected серии valid/stable на всех трёх частотах; все шесть corrected ΔG в допуске; no fatal/clip; нет NaN/Inf.
- **FAIL на уровне** — не выполнен хотя бы один из перечисленных критериев. Вписать все точные причины; численное совпадение ΔG не отменяет invalid.
- Общий **PASS v2.26.1** — хотя бы один из двух зафиксированных уровней полностью прошёл; указать самый низкий (наиболее тихий) прошедший уровень.
- Общий **FAIL** — ни один уровень не прошёл.
- BLOCKED — аппаратный тест не состоялся из-за preflight, режима устройства или ошибки выполнения.

Если −24 FAIL, но −18 PASS — это содержательный результат: нижний проверенный уровень не обеспечивает требуемый C/R запас, а −18 обеспечивает. Не объявлять −24 «почти PASS» и не интерполировать порог между уровнями.

## Офлайн-проверки до железа

Работать из корня pinned worktree:

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

Обязательные условия:
- оба unittest-прогона OK;
- ProductionSimulatorIntegrationTests выполняется, не skipped, в обоих наборах;
- ValveBench selftest PASS.
Если что-либо не пройдено — аппаратный этап не выполнять.

## Inventory и аппаратный запуск

    ./run.sh cli devices | tee measurements/diagnostics/v2261-gainmatch-level-margin/devices-inventory.txt
    ./run.sh cli cards | tee measurements/diagnostics/v2261-gainmatch-level-margin/cards-inventory.txt

По инвентаризации указать текущие numeric индексы D10 Playback и E-MU Capture. Проверить ещё раз, что 192000 Hz объявлен общей частотой. Далее:

    D1=measurements/diagnostics/v2261-gainmatch-level-margin
    mkdir -p "$D1/results"
    PYTHONPATH=. python3 "$D1/v2261_gainmatch_level_margin.py" \
      --dac hw:D10_CARD,0 --adc hw:EMU_CARD,0 --fs 192000 \
      --out-dir "$D1/results/vovans-PC-run-01" \
      2>&1 | tee "$D1/results/vovans-PC-run-01-console.log"

Заменить D10_CARD / EMU_CARD на текущие фактические numeric индексы. Каталог \`results/vovans-PC-run-01\` должен отсутствовать перед запуском: его создаёт harness. Не создавать его заранее. Команда запускается из корня worktree; \`PYTHONPATH=.\` обязателен.

Запуск один раз. Не запускать nohup/фоновые watcher-повторы, если процесс можно дождаться в текущей SSH-сессии. Не повторять тест автоматически после FAIL. Для зависания действовать только по фактическому состоянию процесса и сохранить логи; не запускать второй экземпляр параллельно.

## Ожидаемые артефакты

Для каждого уровня:
- \`level_m24/loopback-series.json\`;
- \`level_m24/loopback-reference.json\`;
- \`level_m24/inject-R-series.json\`;
- \`level_m24/inject-L-series.json\`;
- аналогичные четыре файла в \`level_m18/\`.

Общие:
- \`clock-preflight.json\`;
- \`run-summary.json\` с per-level status, минимальным прошедшим уровнем, C/R/SINAD/peak headroom;
- \`captures/*.npz\` с submitted_to_backend и raw capture;
- \`SHA256SUMS.json\`;
- полный console log, inventory, execution manifest, agent report.

В отчёте привести таблицы по каждому уровню и каждой частоте. Указать min C/R per channel/series, min SINAD отдельно, max ADC peak, все validity reasons и точные corrected ΔG. Для invalid дополнительно показать repeat номер и envelope stability (trend_span/z_burst). SHA256SUMS пересчитать независимо.

## Публикация

- Создать \`agent/v2261-gainmatch-level-margin\` от pinned coordinator SHA.
- Коммитить только inventory, results, console log, manifest и report. Source/test/task/review files не менять.
- \`git diff --check\` чисто; SHA256 manifest подтверждён; production diff отсутствует.
- Обычный commit + fast-forward push; без force push, reset, rebase, clean.
- Вернуть commit SHA, status, offline totals, per-level result, exact artifact paths.

Любой неожиданно высокий peak/clip, fatal event или несоответствие physical endpoint mapping — остановка с сохранением имеющегося доказательства, без изменения gain/volume/mixer.
