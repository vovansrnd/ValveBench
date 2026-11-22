# v2.26.4 — coordinator review (accepted findings + reproducibility corrections)

## 1. Проверка результата

Agent result commit: `752b105fa26c55b96af266341bd8bbbf3d66d124`  
Pinned source: `edc088776794b0fef765994a66f2c09d89e83446`  
Git compare: ровно 1 commit ahead, 0 behind. В commit входят только новые артефакты `measurements/diagnostics/v2264-envelope-event-audit/`; production, harness, tests и исторические NPZ/JSON не менялись.

Проверенный состав:
- audit script — 314 строк;
- четыре результата: report, execution manifest, console log, JSON, два CSV и checksum inventory;
- четыре event captures и controls восстановлены по manifest;
- CONTROL-MATRIX.csv содержит 324 строки данных;
- INPUT-SHA256SUMS.json содержит 26 NPZ entries: A=8, B=8, C=5, D=5; все 26 имеют `manifest_ok=true`.

## 2. Принятые технические результаты

По опубликованным outputs:
- 18 measurement captures обработаны; каждый дал 18 окон (9 сегментов × 2 канала) — всего 324;
- в CONTROL-MATRIX.csv все 324 строки имеют совпадающий `prod_z` vs `json_z` (по полю `z_match`);
- четыре исходных события воспроизведены на ADC capture с совпадением JSON;
- соответствующие `submitted_to_backend` окна имеют очень малые `z_burst` (≤0.0003);
- шесть окон имеют `z_burst ≥ 4.0`, включая L в inject-L (4.642863) и R в loopback (4.089429);
- измерение чувствительно к частоте, используемой для подоконной LS-проекции: B 5.0169→2.9778, D 5.2343→7.8979 при переходе с commanded на measured f0.

Исправленная классификация четырёх отказов: **4 на измерительном канале R; 3 в inject-L и 1 в inject-R**. Нельзя утверждать, что событие уникально для конфигурации inject-L или только для канала R: есть контролы с z≥4 на L и в loopback.

Формулировка «программа, переданная backend, чистая; измеренное capture отличается» корректна в пределах сохранённых массивов. Она не устанавливает физический источник и не доказывает, что причина является именно физическим transient: частотная/фазовая чувствительность метода ещё не исключена.

## 3. Недочёты воспроизводимости, которые надо исправить

### 3.1 Неверный состав NPZ в execution manifest

В manifest написано «22 capture + 8 preflight NPZ». По фактическому `INPUT-SHA256SUMS.json` и разложению по run dirs:
- A: 8 NPZ = 6 measurement + 2 preflight;
- B: 8 NPZ = 6 measurement + 2 preflight;
- C: 5 NPZ = 3 measurement + 2 preflight;
- D: 5 NPZ = 3 measurement + 2 preflight.

Итого **26 NPZ = 18 measurement + 8 preflight**, не 30 и не 22+8. Исправить отчёт/manifest.

### 3.2 Не перечислены SHA256 прочитанных JSON

`INPUT-SHA256SUMS.json` содержит только NPZ. Скрипт дополнительно читает run-level `SHA256SUMS.json` и series JSON из каждого run. В V2265 inventory надо включить также каждый уникальный JSON, реально прочитанный audit-script (отдельно путь, size, SHA256, source commit/blob SHA, если доступен). Не заявлять, что JSON SHA256 независимо вычислялись в v2264, если этого нет в артефакте.

### 3.3 CSV line endings

В опубликованном commit оба CSV фактически сохранены с CRLF (проверено по GitHub file content). Python `csv.writer` по умолчанию генерирует CRLF. Агент сообщает о чистом `git diff --check`, однако текущий tracked output имеет CRLF и script при повторном запуске воспроизводит CRLF. Исправить writer явно на `lineterminator="\\n"`, перегенерировать оба CSV и проверить `git diff --check` на чистой worktree с обычными настройками Git, без `core.whitespace cr-at-eol` обхода.

### 3.4 Fail-closed проверки аудита

В текущем скрипте список `mism` печатается, но несовпадения не добавляются в `out["errors"]`, поэтому exit code останется 0 даже при mismatch. Также скрипт не утверждает:
- что capture/submitted arrays имеют форму (N,2), Fs и metadata ожидаемые;
- что все входные массивы и извлечённые окна finite;
- что каждый срез имеет ровно 384000×2 samples;
- что реально обработано ровно ожидаемое число NPZ/окон;
- что отсутствуют дубликаты сегментов или пропуски.

Добавить явные проверки и ненулевой exit при любом нарушении. Не менять production-функцию.

## 4. Научная оговорка и следующий вопрос

Главный нерешённый вопрос: может ли почти идеальная синусоида с небольшим постоянным расхождением частоты ADC относительно commanded frequency давать высокий `z_burst` сама по себе, даже без дополнительного амплитудного disturbance?

Для события D и B reported measured f0 примерно на 0.25 Hz ниже 9994 Hz; для 94 Hz разница около 0.0024 Hz. Эти отклонения согласуются по порядку с clock offsets порядка −24 ppm из run preflight. При этом смена commanded→measured f0 меняет z существенно и неодинаково. Само по себе это не доказывает ни артефакт частотного рассогласования, ни реальный transient.

Следующий этап должен построить синтетический pure-tone null model на тех же временных окнах, частотах, фазах и sample rate, затем сравнить:
1. чистый идеальный синус с measured f0, проецируемый на commanded f;
2. тот же синус, проецируемый на measured f0;
3. идеальный синус на commanded f;
4. фактический ADC capture;
5. submitted_to_backend.

Только такое сравнение позволит понять, достаточен ли frequency-detuning/projection effect для возникновения z≥5, или фактический capture содержит дополнительное изменение амплитуды.

## 5. Граница выводов

Пока не менять `Z_BURST_MAX=5.0`, `TREND_MAX`, размер подокна, production алгоритм, GUI или harness. Не ослаблять invalid и не превращать предыдущие FAIL в PASS. Все дальнейшие вычисления — offline, без устройств и без DUT.
