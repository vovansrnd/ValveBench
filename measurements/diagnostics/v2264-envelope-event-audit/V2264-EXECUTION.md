# v2.26.4 — offline forensic audit четырёх envelope-событий Gain Match

## Назначение и неизменяемые границы

Это **только offline-аудит уже сохранённых файлов** v2261-r1, v2262 и v2263-r1. Новых воспроизведений/записей не выполнять.

- Coordinator pin передаётся отдельным сообщением; работать только на точном SHA из сообщения, в отдельной detached worktree.
- Результатная ветка: `agent/v2264-envelope-event-audit`.
- Вся работа — чтение архива и вычисления на уже имеющихся NPZ/JSON.
- **Запрещено** вызывать команды ALSA/backend, `./run.sh cli devices/cards`, запускать harness, делать playback/capture, открывать аудиоустройства, менять mixer/volume/gain/routing/rate/cables/config/calibration. Никакого DUT.
- Не менять `valvebench/`, Gain Match production threshold/algorithm, v2260/v2261/v2262/v2263 harness или их tests. Не менять исходные NPZ/JSON и их SHA256SUMS.
- Не retry/повторно измерять. Не менять и не скрывать результаты.
- Сохранить полный console log анализа, скрипт, JSON/CSV summary и SHA256 входных архивов. Обычный commit + fast-forward push, без force push/rebase/reset.

## Вопросы аудита

1. Подтвердить реальными массивами NPZ все четыре события из таблицы V2264-COORDINATOR-REVIEW.md.
2. Независимо воспроизвести точный расчёт production `envelope_stability()` и временную привязку каждой измерительной выборки из `Rig.run()`.
3. Определить, является ли excursion видимым уже в цифровом `submitted_to_backend` или появляется только в аналоговом ADC `capture`.
4. Проверить контрольные измерения: остальные repeats той же частоты, L/R, loopback, противоположная inject-конфигурация, другие частоты и другие сессии.
5. Проверить устойчивость вывода к частоте демодуляции: commanded `actual_f` (как в production) против измеренной `analysis["f0"]`. Не менять production и не предлагать новый порог на основании этой проверки.
6. Выявить близкие к порогу значения `z_burst` (например, ≥4.0) во всех исследуемых R/L контрольных repeats, чтобы оценка не ограничивалась четырьмя уже известными отказами.
7. По результатам сказать только то, что действительно следует из сохранённых файлов. Не назначать физический источник без доказательств.

## Четыре исходных события и архивы

Все пути относительно корня репозитория на pinned SHA.

| Событие | Серия / измеряемый канал | JSON |
|---|---|---|
| A — v2261-r1 | −18, `inject-R`, канал R, 94 Hz, rep2 | `measurements/diagnostics/v2261-gainmatch-level-margin/results/vovans-PC-run-01/level_m18/inject-R-series.json` |
| B — v2262 run-01 | −18, `inject-L`, канал R, 9994 Hz, rep1 | `measurements/diagnostics/v2262-gainmatch-repeatability/results/vovans-PC-run-01/level_m18/inject-L-series.json` |
| C — v2263 run-02 | −20, `inject-L`, канал R, 94 Hz, rep1 | `measurements/diagnostics/v2263-operating-level-qualification/results/vovans-PC-run-02/level_m20/inject-L-series.json` |
| D — v2263 run-03 | −20, `inject-L`, канал R, 9994 Hz, rep3 | `measurements/diagnostics/v2263-operating-level-qualification/results/vovans-PC-run-03/level_m20/inject-L-series.json` |

Capture NPZ для событий:
- A: `.../v2261-gainmatch-level-margin/results/vovans-PC-run-01/captures/m18-inject_R-R-07.npz`
- B: `.../v2262-gainmatch-repeatability/results/vovans-PC-run-01/captures/m18-inject_L-L-08.npz`
- C: `.../v2263-operating-level-qualification/results/vovans-PC-run-02/captures/m20-inject_L-L-05.npz`
- D: `.../v2263-operating-level-qualification/results/vovans-PC-run-03/captures/m20-inject_L-L-05.npz`

Для controls найди соответствующие capture файлы в тех же run-каталогах и используй JSON manifests, чтобы не перепутать: `*-loopback-none-*.npz`, capture противоположной инъекции (`*-inject_L-*.npz` или `*-inject_R-*.npz`) и при наличии другие уровни того же v2261/v2262 run. Не предполагай имена без сверки SHA256SUMS.

## Проверка целостности входов

Для каждого используемого NPZ:
- прочитать соответствующий `SHA256SUMS.json` в том же run-каталоге;
- локально пересчитать SHA-256 и size bytes;
- зафиксировать совпадение manifest, hash, size, имя и полный относительный путь;
- не изменять оригинал.

У исходных JSON повторов зафиксировать `validity_reasons`, `envelope.z_burst`, `trend_span`, `subwindows`, C/R `carrier_residual_db`, SINAD `sinad_db`, `noise_dbfs`, `level_dbfs`, `peak_dbfs`, `clipped`. C/R и SINAD держать раздельно.

## Точное восстановление окон Rig.run

Пользуйся source из этого же pinned worktree. Не переписывай алгоритм «по памяти».

Для используемых серий:
- Fs = 192000 Hz, df = 1 Hz, n = Fs/df = 192000;
- blocks = 2; settle = 0.5 s; tail allowance = 0.05 s;
- длина одного program segment из `Rig._seg`: `settle_n + blocks*n + int(0.05*fs)` = 489600 samples;
- частотный порядок каждого прохода: 94, 994, 9994 Hz; всего 3 repeats × 3 частоты;
- порядок окон: rep1 94/994/9994, rep2 94/994/9994, rep3 94/994/9994;
- для ADC `capture` восстановить начало тем же `Rig._onset(rec, tone_hz=94.0)`, как делает `Rig.run`; затем для segment index `i=0..8`: `pos = d + i*489600`, `a = pos + 96000`, `b = a + 384000`, окно `capture[a:b]`;
- для исходного `submitted_to_backend` учесть сохранённый цифровой lead guard из NPZ (обычно 1.5 s = 288000 samples): `a_in = lead_n + i*489600 + 96000`, `b_in = a_in + 384000`. Используй metadata `lead_seconds`, `fs`, не hardcode если фактические значения отличаются.
- Проверь размер каждого среза — ровно 384000×2, отсутствие NaN/Inf, и подтвердить, что захват/окно не выходит за границы массива.
- Окно для каждого события выбирается по индексу repeat/frequency из JSON, а не по визуальному предположению.

Важно: `Rig.run` находит один общий onset для начала последовательного воспроизведения; сегменты затем раскладываются по их фиксированной длине. Использовать этот путь один-в-один. Записать полученный onset и sample indices в отчёт.

## Повторный расчёт detector

Импортировать `envelope_stability` и `Rig` из pinned production source, без локальных патчей.

Для каждого окна L и R:
1. Вызвать `envelope_stability(window[:, channel_index], fs, commanded_actual_f)`.
2. Отдельно получить `analysis = rig._tone_an(window, channel_index, k, nharm=9)` и сохранить `analysis["f0"]`; повторить envelope только как диагностическую чувствительность с этой measured frequency.
3. Записать все 20 LS-amplitudes каждого 0.1-s subwindow. Сам массив amplitudes можно получить отдельной инструментированной копией расчёта, но числовые `z_burst` и `trend_span` сверять с импортированной production-функцией.
4. Сравнить recomputed production `z_burst`/`trend_span` со значениями исходных JSON (ожидается численное совпадение с разумной машинной точностью). При несовпадении сначала выяснить временной срез/параметр/источник, не менять production.
5. Для каждого события указать subwindow index 0–19 с максимальным положительным/отрицательным отклонением, sample range и время относительно начала измерительного окна; амплитуду выражать также в процентах от средней амплитуды.

## Контрольная матрица

Для каждого из четырёх events:
- тот же run + та же частота + тот же измеряемый R канал в остальных repeats;
- L-канал того же окна;
- соответствующая частота R в loopback;
- соответствующая частота R в противоположной inject-конфигурации;
- та же частота и R в других sessions/levels, если такая запись есть.

Для каждого контроля записать `z_burst`, `trend_span`, mean amplitude, max positive/negative residual %, C/R, SINAD, noise, peak, validity. Покажи всю матрицу, не только значения, удобные для гипотезы.

Отдельно по всем доступным сериям выбранных исторических run-ов собрать распределение `z_burst` для обоих каналов, частот, repeats, injection modes и loopback. Отметить все значения ≥4.0. Не выводить вероятности и статистическую «значимость» на выборке такого размера.

## Сравнение submitted_to_backend и capture

Для того же самого сегмента и канала вычислить subwindow LS-amplitudes на `submitted_to_backend` и на ADC capture. Учесть, что входной массив уже содержит lead/tail guards, а capture имеет фактический аппаратный latency и восстанавливается через onset.

Смысл сравнения:
- excursion присутствует и в submitted → исследовать генератор/формирование программы или ошибку цифровой временной привязки;
- submitted стабилен, capture показывает excursion → событие возникло после массива, переданного backend (DAC/аналоговый тракт/ADC/захват/синхронизация); **не делать вывод, какой компонент виноват**;
- mismatch только в алгоритме временного выравнивания/частоты fit → изложить и воспроизвести, не менять production.

Не считать submitted_to_backend эквивалентом измеренного аналогового выхода DAC.

## Результат

Создать `measurements/diagnostics/v2264-envelope-event-audit/`:
- `V2264-AGENT-REPORT.md` — итог с таблицами всех четырёх событий, provenance, raw-array results, controls, caveats;
- воспроизводимый offline script (например, `audit_v2264_envelope_events.py`);
- JSON machine-readable output и компактный CSV/TSV с 20 subwindow amplitudes и контрольной матрицей;
- `results/console.log` — полный лог;
- `results/INPUT-SHA256SUMS.json` — SHA256/size всех исходных NPZ и JSON, которые реально читались;
- если добавлен тест скрипта — тест и его полный лог.

Отдельно в V2264-AGENT-REPORT.md исправить классификацию: **четыре события на R; 3 inject-L, 1 inject-R**, с точной таблицей. Не повторять ошибочное «все четыре в inject-L». Не перезаписывать исторические commits/результаты. Не изменять V2263 результаты.

Вывод должен включать один из вариантов по каждому событию: воспроизведено из сырого capture; не воспроизведено (с причиной); или недостаточно данных. И отдельно общий вывод: есть ли подтверждение общего R-channel pattern, где именно появляется excursion, чувствителен ли он к commanded/measured f0, или источник остаётся неразличим.

## Гейты и публикация

Это offline-only задача. Перед анализом убедись, что HEAD равен exact coordinator pin и worktree чиста. Проверь:
- в diff нет `valvebench/`, v2260/v2261/v2262/v2263 harness/tests, оригинальных результатов, исходных captures;
- нет вызовов backend/ALSA в audit script;
- входные NPZ не изменены;
- `git diff --check` чист;
- анализ может быть воспроизведён на том же pinned tree командой из корня проекта.

Добавлять только новые audit artifacts в V2264 directory. Обычный commit и FF push в `agent/v2264-envelope-event-audit`.

В отчёте вернуть: commit SHA, pinned SHA, exact команда анализа, перечень hash-verified inputs, event/control matrix, совпадение пересчитанного detector с JSON, найденные подокна, все caveats и вывод без причинных домыслов.
