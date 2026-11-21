# v2.26.0 — аппаратная сквозная проверка Gain Match на D10 → E-MU 0404

## Назначение

Проверить на реальной паре SMSL D10 playback → E-MU 0404 USB analog capture, что production-реализация Gain Match:

1. строит частотно-зависимый loopback-эталон разницы каналов;
2. применяет его к последующему измерению;
3. восстанавливает известную искусственно заданную межканальную разницу с правильным знаком.

Это проверка измерительного алгоритма и реального DAC→ADC тракта. Это **не** измерение фонокорректора и не основание менять production-пороги/алгоритмы.

## Пиннинг и роли

- Исполнять только coordinator-ветку coordinator/v2260-gainmatch-hardware-validation на точном SHA, указанном координатором.
- Работать в отдельном detached worktree, не переключать ветки в основном checkout агента.
- Код, методику, harness и тесты написал координатор. **Агент только запускает их на железе, сохраняет фактические результаты и публикует артефакты.**
- Не исправлять код, тесты, критерии и не запускать повтор после самостоятельной правки. При ошибке — остановиться и вернуть полный лог.
- Production-код valvebench/, пользовательскую конфигурацию, GTK/ALSA mixer, физические подключения и внешний DUT не изменять.

## Известная физическая цепь

Постоянная историческая пара — **SMSL D10 playback → E-MU 0404 USB analog capture**. Она подтверждена прежней физической серией REPORT-TIMING-D10-EMU-v2.md в ветке diagnostics/d10-full-capture. Не спрашивать пользователя повторно, что подключено.

Перед тестом только read-only определить текущие numeric ALSA PCM endpoints командами:

    ./run.sh cli devices
    ./run.sh cli cards

По названиям и направлениям однозначно сопоставить D10 Playback и E-MU Capture. Номера карт могут измениться после USB reconnect; не переносить старые hw:N,0 автоматически. Если endpoint-пара неоднозначна — остановиться после инвентаризации.

## Безопасность и границы воздействия

- Использовать существующее соединение D10→E-MU. Кабели не отсоединять и не переставлять.
- Не подключать фонокорректор или другой DUT.
- Не менять ALSA mixer, аппаратные ручки, системную громкость, gain, source, routing или настройки частоты.
- Максимальный тестовый уровень — **−30 dBFS peak**. В двух injected-прогонах программно только ослабляется один канал на 6.020599913 dB (коэффициент амплитуды 0.5); положительного цифрового gain нет.
- Harness создаёт свежий Calibration() с относительными dB; пользовательский ~/.config/valvebench/config.json не читает и не сохраняет. Не запускать Setup-калибровки, CLI calibrate, passport или процедуры записи калибровочного профиля.
- D10 edge-особенность компенсируется только тестовым wrapper: 1.5 s цифрового нуля до сигнала и 0.6 s после него. Эти интервалы основаны на прежних аппаратных опытах v2.5–v2.10. Production-файлы не меняются.
- Частота зафиксирована на **192000 Hz**. Если общий режим 192 kHz не объявлен, STOP/BLOCKED; не выбирать другую Fs самостоятельно.

## Тестовый контракт

Harness вызывает действующие production-компоненты Rig, split_clock_preflight, gainmatch.measure_series и build_loopback_reference. Он временно оборачивает backend.play_record только в памяти своего процесса: добавляет D10 guard silence, сохраняет массив для воспроизведения и фактическую запись E-MU, а затем (в двух сериях) ослабляет один логический канал.

Параметры: 94, 994 и 9994 Hz (профиль mains_avoidance_50), −30 dBFS, 2 когерентных блока, settle 0.5 s, 3 повтора на частоту.

Порядок обязателен:

1. **Clock preflight** реальной пары D10→E-MU. Если не прошёл — никаких следующих измерений.
2. **Loopback reference** без цифрового дисбаланса. Три частоты × три повтора. Все строки должны быть valid и repeat_stable=true; затем штатно построить reference.
3. **R attenuated**: ослабить только R на 6.020599913 dB, измерить purpose=dut с тем же reference. Ожидаемый corrected ΔG = GL−GR = **+6.0206 dB**.
4. **L attenuated**: ослабить только L на те же 6.020599913 dB, измерить purpose=dut с тем же reference. Ожидаемый corrected ΔG = GL−GR = **−6.0206 dB**.

Reference один и тот же для обоих injected-прогонов. Порядок не переставлять.

## Критерии приёмки

Полный PASS только если одновременно:

- clock preflight ok=true;
- все 3 строки reference valid, каждая repeat_stable=true;
- все 3 строки inject-R и все 3 строки inject-L valid и repeat-stable;
- на каждой частоте inject-R.delta_gain_db отличается от +6.020599913 dB не более чем на **0.20 dB**;
- на каждой частоте inject-L.delta_gain_db отличается от −6.020599913 dB не более чем на **0.20 dB**;
- events.fatal пуст, нет XRUN/overrun/underrun/ADC clip, в записях нет NaN/Inf;
- SHA-256 manifest корректен, source SHA зафиксирован.

Нельзя принимать результат только по общему среднему: проверяются все шесть injected значений отдельно. Любой незакрытый критерий — FAIL/BLOCKED с точной причиной; допуск не ослаблять.

Если mixer fingerprint недоступен, не пытаться его исправить. Отразить limitation в отчёте; подтвердить, что агент не менял mixer или подключения между сериями.

## Подготовка в закреплённом worktree

    set -euo pipefail
    D=measurements/diagnostics/v2260-gainmatch-hardware-validation
    python3 -m py_compile "$D/v2260_gainmatch_hardware_validation.py" "$D/test_v2260_gainmatch_hardware_validation.py"
    PYTHONPATH=. python3 -m unittest discover -s "$D" -p 'test_v2260_gainmatch_hardware_validation.py' -v
    ./run.sh selftest
    ./run.sh cli devices | tee "$D/devices-inventory.txt"
    ./run.sh cli cards | tee "$D/cards-inventory.txt"

Оба тестовых уровня обязательны до железа: helper/unit tests должны завершиться без ошибок; production SimBackend integration test внутри test-файла должен реально выполниться (не быть skipped); штатный selftest ValveBench должен завершиться PASS. Если SimBackend integration skipped или любая проверка не прошла — аппаратный запуск не выполнять, вернуть лог координатору.

После read-only проверки endpoint-пары:

    mkdir -p "$D/results"
    python3 "$D/v2260_gainmatch_hardware_validation.py" \
      --dac hw:D10_CARD,0 --adc hw:EMU_CARD,0 \
      --fs 192000 \
      --out-dir "$D/results/vovans-PC-run-01" \
      2>&1 | tee "$D/results/vovans-PC-run-01-console.log"

Заменить D10_CARD и EMU_CARD фактическими numeric индексами из inventory. Перед запуском убедиться, что out-dir ещё не существует. Команды выполнять из корня detached worktree.

## Артефакты

Harness сохраняет:

- run-summary.json — статус, конфигурация, preflight, значения и итоговые критерии;
- clock-preflight.json;
- loopback-series.json и loopback-reference.json;
- inject-R-series.json и inject-L-series.json;
- captures/*.npz — массивы, переданные backend, и фактические записи E-MU;
- SHA256SUMS.json — размеры и SHA-256 файлов.

Дополнительно сохранить полный console log, точные команды и exit codes, git status --short, HEAD SHA и SHA256 source/test/task-файлов. В V2260-AGENT-REPORT.md дать по каждой частоте: raw delta_adc_db, reference correction, corrected delta_gain_db, repeat spread, valid/repeat_stable и отклонение от ожидаемых ±6.0206 dB.

## Публикация результата

После теста, не изменяя source/test/task files:

- создать ветку agent/v2260-gainmatch-hardware-validation от pinned coordinator SHA;
- добавить только inventory, result directory, console log, execution manifest и agent report;
- проверить git diff --check, SHA256 manifest и отсутствие любых изменений production/source/test/task;
- commit и push обычным fast-forward способом; без force push/reset/rebase/clean;
- вернуть commit SHA, итоговый статус, сводку тестов и точные пути артефактов.

Если тест заблокирован до создания JSON, сохранить inventory, лог и отчёт с точной причиной. Не выполнять физические проверки по изменённому коду без нового задания координатора.
