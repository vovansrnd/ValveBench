# v2.26.4 — coordinator review: повторяющиеся envelope-события в Gain Match

## Проверка v2.26.3 r1

Результатный commit: `346fb908faa54d213cca321af5bf6ecf96d065e3`  
Pinned source: `74d71b3101d49f2b19ba70f3b8794af5106d8d7f`  
Ветка: `agent/v2263-operating-level-qualification-r1`

Сравнение GitHub: ровно 1 commit ahead, 0 behind. В result commit нет изменений production, v2263 harness или тестов. Обновлены отчёт и execution manifest, добавлены inventory, offline logs, три консольных лога и три набора аппаратных результатов.

По опубликованным логам и первичным JSON:
- py_compile — 6 файлов OK;
- D0 — 11/11, D1 — 6/6, D3 — 7/7; D3 identity test и SimBackend integration присутствуют в исправленном наборе;
- selftest PASS;
- fresh inventory соответствует SMSL D10 playback `hw:1,0` → E-MU 0404 capture `hw:2,0`, 192 kHz;
- три аппаратные сессии на −20 dBFS: run-01 PASS, run-02 FAIL, run-03 FAIL;
- штатные отказы — по одному `unstable_carrier` repeat, fatal/XRUN/clip не заявлены;
- каждый v2263 SHA256SUMS содержит 11 записей: 5 NPZ + 6 JSON. Это соответствует одноступенчатому v2263 harness. Старое указание «18 файлов (8 NPZ + 10 JSON)» в V2263-EXECUTION.md было перенесено из двухуровневого v2261 формата и ошибочно.

Я сверил ключевые per-repeat JSON, классификацию причин и количества manifest entries. Бинарные NPZ в этой проверке независимо не скачивались и не пересчитывались координатором; проверка их hash/size остаётся ответственностью локального audit-runner в следующем этапе.

## Исправление исторической классификации четырёх событий

В `V2263-AGENT-REPORT.md` содержится неверное утверждение, что все четыре исторических события произошли в конфигурации `inject-L`. Первичные JSON показывают следующую картину:

| Источник | Инъекция | Канал измерения | Частота | repeat | z_burst | trend_span | C/R, dB | SINAD, dB |
|---|---|---|---:|---:|---:|---:|---:|---:|
| v2261-r1 | **inject-R** | R | 94 Hz | 2 | 5.812316 | 0.000060249 | 56.683577 | 56.648072 |
| v2262 run-01 | inject-L | R | 9994 Hz | 1 | 5.016931 | 0.000084101 | 62.501376 | 62.499260 |
| v2263 run-02 | inject-L | R | 94 Hz | 1 | 5.740411 | 0.000034487 | 60.377700 | 60.341073 |
| v2263 run-03 | inject-L | R | 9994 Hz | 3 | 5.234266 | 0.000023502 | 60.309688 | 60.308275 |

Точные первичные источники:
- v2261-r1: `measurements/diagnostics/v2261-gainmatch-level-margin/results/vovans-PC-run-01/level_m18/inject-R-series.json`, ref `564a4af239c7c78f7661099656cc80fc06efc9b1`;
- v2262: `measurements/diagnostics/v2262-gainmatch-repeatability/results/vovans-PC-run-01/level_m18/inject-L-series.json`, ref `906268a745715ce8827b6bfc30d1565ba0889093`;
- v2263 run-02: `measurements/diagnostics/v2263-operating-level-qualification/results/vovans-PC-run-02/level_m20/inject-L-series.json`;
- v2263 run-03: `measurements/diagnostics/v2263-operating-level-qualification/results/vovans-PC-run-03/level_m20/inject-L-series.json`;
  последние два — ref `346fb908faa54d213cca321af5bf6ecf96d065e3`.

Итого: **4/4 события на измерительном канале R, но только 3/4 в inject-L и 1/4 в inject-R**. Частоты — 94 Hz дважды и 9994 Hz дважды; на 994 Hz события не зарегистрированы. События встречались на −18 и −20 dBFS. Поэтому утверждение об исключительной привязке к `inject-L`/режиму ослабления L не подтверждается. Общий признак «канал R» заслуживает проверки, но четыре события — малая выборка и не доказывают неисправность R-канала или причинную связь.

Во всех четырёх случаях C/R выше 30 dB с большим запасом. Это не C/R gate failure. SINAD показывается отдельно и не подменяет production C/R.

## Что делает production detector

В pinned `valvebench/gainmatch.py`:
- `SUBWINDOW_S = 0.1` s;
- `TREND_MAX = 0.005`;
- `Z_BURST_MAX = 5.0`;
- `envelope_stability()` получает 0.1-секундные неперекрывающиеся подокна, извлекает LS-амплитуду несущей из базиса [1, cos, sin], аппроксимирует последовательность амплитуд линейным трендом и считает максимальное отклонение остатка от медианы в единицах robust MAD с нижним пределом `mean_amp * 1e-6`;
- `measure_series()` применяет функцию отдельно к каждому измеренному каналу и repeat. Invalid выставляется при `trend_span > 0.005` или `z_burst > 5.0`.

Из JSON видно, что эти четыре invalid вызваны именно z_burst; C/R gate не задействован. Это пока не устанавливает, что всплеск физический: требуется сверить рассчитанные 20 подокон с сохранёнными PCM capture и воспроизвести временное выравнивание `Rig.run()`.

## Вывод координатора

1. v2.26.3 честно не прошёл критерий −20 dBFS (только 1/3 сессий PASS). Не переклассифицировать FAIL по высокому C/R или правильному ΔG.
2. Численное ядро Gain Match выглядит точным: все 18 строк в пределах около 0.0018 dB от цели; attenuated-L C/R минимум 34.2089 dB, запас около 4.21 dB.
3. Исторический «inject-L only» паттерн — ошибка отчёта. Корректно говорить: четыре события на R; конфигурации 3×inject-L и 1×inject-R.
4. Порог `Z_BURST_MAX`, методика и production-код не менять. Следующий этап — только read-only offline forensic audit сохранённых NPZ. Никаких новых аппаратных запусков, перестановок или присоединения DUT.
5. До аудита PCM нельзя заключать, что причина — D10, E-MU, ALSA/driver, генератор, временное выравнивание или алгоритм. Физический источник не установлен.
