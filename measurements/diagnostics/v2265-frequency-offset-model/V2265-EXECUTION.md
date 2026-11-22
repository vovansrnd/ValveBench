# v2.26.5 — frequency-offset null model и fail-closed воспроизводимость v2264

## Цель

Продолжить строго offline. Проверить, способен ли один только небольшой frequency offset между commanded и measured ADC tone вызвать высокие значения production `envelope_stability().z_burst` при идеально чистой синусоиде. Одновременно исправить выявленные coordinator review пробелы воспроизводимости v2264-артефактов.

Pinned coordinator SHA этого задания передаётся в сообщении координатора. Не использовать старые pins. Результатная ветка: `agent/v2265-frequency-offset-model`.

## Абсолютные ограничения

- Detached worktree на exact pinned SHA, новый отдельный result branch. Проверить чистый tracked state.
- **Offline-only. Никакого ALSA/backend/device access.** Запрещены `./run.sh cli cards/devices`, playback, capture, harness, открытие PCM, hardware tests и любые новые записи.
- Не менять соединения, mixer, volume, gain, routing, sample rate, cables или конфигурацию.
- Не подключать DUT.
- Не менять `valvebench/`, production `envelope_stability`, `Z_BURST_MAX`, `TREND_MAX`, длительность production subwindow, harness/tests v2260–v2263 и их исторические результаты.
- Не удалять, не перезаписывать и не нормализовать старые captures/series JSON или прежние commit. На новой ветке разрешены только исправления audit-артефактов v2264 и новые v2265 artifacts.
- Не выбирать или подгонять модель по желаемому ответу. Если чистая синусоидальная модель не объясняет событие, прямо это написать.
- Не использовать retry/повторные hardware measurements. Все логи и выходы — в текущей проектной директории.

## Часть A — исправить воспроизводимость v2264

Работать с существующим `measurements/diagnostics/v2264-envelope-event-audit/audit_v2264_envelope_events.py` и его outputs, не изменяя production.

1. Исправить V2264-EXECUTION-MANIFEST.md: **26 NPZ = 18 measurement captures + 8 preflight captures** (A=8, B=8, C=5, D=5). Не писать 22+8.
2. Расширить `INPUT-SHA256SUMS.json` или сделать соседний `INPUT-JSON-SHA256SUMS.json`: SHA256 + size всех уникальных JSON, которые audit script реально читает. Минимум — 4 run-level `SHA256SUMS.json` и все series JSON из 4 исследуемых run directories. Указать pinned commit, относительный путь, Git blob SHA если доступен, размер и SHA256 фактических байтов. Отдельно считать NPZ и JSON; не делать общих заявлений без полного списка.
3. В audit script добавить fail-closed проверки:
   - все NPZ имеют ожидаемый sample rate 192000 Hz, stereo (N,2), необходимые keys;
   - submitted/capture arrays и все извлечённые окна конечны (no NaN/Inf);
   - каждый analysis window ровно 384000×2;
   - каждый NPZ содержит все 9 ожидаемых окон в ожидаемом порядке, все capture paths присутствуют, нет дублей/пропусков;
   - всего 18 measurement captures × 9 сегментов × 2 канала = 324 rows; отдельно явно подтвердить 26 NPZ hash checks;
   - каждый z mismatch, json missing, metadata inconsistency, input hash mismatch попадает в errors и приводит к non-zero exit.
4. `used_files` либо задействовать по назначению, либо удалить. Не оставлять параметр, который создаёт видимость ограниченной выборки, если скрипт обрабатывает все файлы из manifest.
5. В обоих CSV writer задать `lineterminator="\\n"`. Перегенерировать CSV и проверить, что в tracked CSV нет CRLF. `git diff --check` должен пройти с обычной конфигурацией Git; не включать `core.whitespace cr-at-eol` ради маскировки.
6. Перезапустить исправленный v2264 audit из pinned worktree без устройств. Проверить JSON parse, точные 324 строки и шесть z≥4.0 значений, совпадение всех `z_match`, SHA всех NPZ/JSON.
7. Не изменять выводы v2264, кроме коррекции NPZ/JSON counts/hash inventory и технических fail-closed/CSV исправлений.

## Часть B — pure-tone frequency-offset null model

Сделать воспроизводимый offline script в `measurements/diagnostics/v2265-frequency-offset-model/`, который читает уже проверенные исходные captures и использует source production из pinned tree без патчей.

### B1. Данные

Использовать все 324 ADC measurement windows из v2264 (18 captures ×9 segments ×2 channels), не только четыре известных invalid. Использовать v2264 event/card data и CONTROL-MATRIX для сопоставления. Не анализировать preflight NPZ как measurement window, но их JSON использовать как clock metadata.

Для каждого окна получить (read-only из существующих JSON):
- run, capture, level, injection/loopback mode, frequency, repeat, channel;
- соответствующие run-level `clock-preflight.json` и `run-summary.json`, чтобы сопоставить measured f0 с clock preflight и acquisition metadata; включить все прочитанные JSON в SHA256 input inventory;
- commanded actual_f (generator frequency exactly as passed into production analysis);
- measured `analysis["f0"]` и frequency offset (Hz и ppm);
- production z/trend с commanded f;
- diagnostic z/trend с measured f0;
- амплитуду и фазу fundamental-fit, C/R, SINAD, noise_dbfs, peak, clip, validity.

### B2. Построение pure-tone модели

Для каждого фактического ADC window построить synthetic signal length ровно 384000 samples:
- чистая фундаментальная синусоида с той же sample rate, оценённой measured f0, амплитудой и фазой этого окна;
- чистая синусоида с commanded actual_f и той же амплитудой/согласованной фазой;
- не добавлять шум, гармоники, случайные компоненты или envelope modulation в базовую pure-tone модель.

Фундаментальные amplitude/phase извлечь явно через least-squares fit на [1, cos(2πf0t), sin(2πf0t)] либо через проверенный эквивалентный метод. Зафиксировать формулу и конвенцию фазы в коде/отчёте. Не использовать частоту из JSON округлением, если production `analysis["f0"]` даёт более точную оценку.

На каждой модели рассчитать:
1. `envelope_stability(synthetic_measured_tone, FS, commanded_actual_f)`;
2. `envelope_stability(synthetic_measured_tone, FS, measured_f0)`;
3. `envelope_stability(synthetic_commanded_tone, FS, commanded_actual_f)`.

Для фактического capture сохранить исходные production z/trend и measured-f0 z/trend. Не менять production source.

### B3. Проверка фазовой/оконной чувствительности

Для всех четырёх исходных событий и всех 6 окон с original z≥4.0:
- построить pure-tone модели с одинаковой measured frequency/amplitude, но разными начальными фазами (не менее 16 равномерных фаз 0..2π);
- пересчитать `z_burst` при commanded f;
- оценить диапазон min/median/max z и долю фаз, где z≥5;
- отдельно проверить зависимость от начала 100-ms окон: сдвиги сегментации 0, 25, 50, 75 ms на непрерывной synthetic pure-tone sequence (не circular roll, не добавлять искусственный wrap transient).
Это диагностическая модель, не изменение production окна.

### B4. Decomposition actual capture

Для четырёх events и шести окон z≥4:
- сравнить actual ADC window и его pure-tone reconstruction при measured f0;
- сохранить residual = actual − fitted fundamental;
- измерить RMS residual и max abs residual relative to fundamental;
- выполнить envelope analysis на pure-tone model, actual capture и, отдельно, residual-added control с фиксированным фундаментом;
- определить, объясняет ли frequency detuning большую часть z, или actual capture содержит дополнительное component beyond pure tone model.
Не называть residual «физическим шумом устройства»: в нём могут быть гармоники, noise, distortion, fitting error, timing и ADC artifacts.

### B5. Выводы

Классифицировать каждое событие по результату:
- **detuning sufficient** — pure-tone model сама пересекает production threshold и близко воспроизводит actual z;
- **detuning contributes, not sufficient** — pure-tone model даёт z>control, но ниже actual/threshold;
- **not explained by detuning** — pure-tone model остаётся низкой, actual capture сохраняет high z;
- **inconclusive** — недостаточно точности/модель неустойчива.

Не вводить вероятности, p-values или статистическую значимость. Не предлагать новый production threshold и не менять алгоритм.

## Требуемые артефакты

### В V2264 directory (только воспроизводимость исправления)
- исправленный audit script;
- исправленные V2264-EXECUTION-MANIFEST.md;
- актуальный NPZ/JSON checksum inventory;
- перегенерированные LF CSV;
- `results/console.log` с полным новым offline run;
- `results/audit-results.json` с fail-closed audit status/counts.

### В V2265 directory
- `V2265-AGENT-REPORT.md`;
- `V2265-EXECUTION-MANIFEST.md`;
- воспроизводимый `audit_v2265_frequency_offset_model.py`;
- полный `results/console.log`;
- `results/frequency-offset-model.json`;
- `results/FREQUENCY-OFFSET-MATRIX.csv` (324 rows);
- `results/PURE-TONE-PHASE-SENSITIVITY.csv`;
- `results/EVENT-RESIDUALS.csv`;
- `results/INPUT-SHA256SUMS.json` со списком всех прочитанных NPZ и JSON inputs;
- при необходимости небольшой unit-test pure tone generation и его лог.

## Ожидаемые доказательства в отчёте

1. Exact pinned SHA, result commit SHA, dirty/clean state, точные команды.
2. 26 NPZ + точный набор JSON input hashes; все совпадают.
3. Валидация v2264: 324/324 windows, shape/finiteness/count checks, zero JSON mismatch, exactly six z≥4.0 values.
4. Сравнение actual / pure-tone measured-f0 / pure-tone commanded-f / synthetic commanded-f по всем 324 окнам.
5. Полная фазовая/сдвиговая чувствительность по событиям и high-z controls.
6. Residual decomposition с осторожной интерпретацией.
7. Однозначный вывод, объясняет ли measured-vs-commanded frequency offset четыре отказа или нет.
8. Список ограничений: synthetic pure tone не является физической моделью DAC/ADC, не определяет источник и не заменяет аппаратный эксперимент.

## Гейты перед commit

- py_compile оба audit scripts;
- запустить V2264 offline audit и V2265 offline model;
- parse всех generated JSON;
- проверить ожидаемые row counts и все numeric values finite;
- проверить NPZ/JSON hashes;
- `git diff --check` чисто на default config;
- diff содержит только v2264 audit cleanup и новые v2265 artifacts;
- diff не содержит `valvebench/`, production thresholds, harness/tests v2260–v2263, старые source NPZ/JSON или hardware configs;
- grep audit scripts: никаких вызовов ALSA/backend, play_record, device open, harness run или shell commands для устройств.

Затем commit и fast-forward push в `agent/v2265-frequency-offset-model`, без force push/rebase/reset. В отчёте явно отделить результаты реальных capture от synthetic model.
