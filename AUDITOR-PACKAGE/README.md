# AUDITOR-PACKAGE — доказательный пакет для независимого аудита clock estimator ValveBench

**Назначение:** полный комплект исходных данных и отчётов для независимого
аудита оценки частоты (`dsp.analyze_tone`) и достоверности clock-измерений.
Задание аудитору — в [AUDIT-BRIEF.md](AUDIT-BRIEF.md) (отправлять практически
без изменений).

**Подготовил:** агент ValveBench по заданию координатора (п.6 ревизии v2.17).
Никаких новых аппаратных измерений; production-код не менялся; WAV скопированы
байт-в-байт, без перекодирования и нормализации.

## Проверяемые ревизии

| Что | Ветка | HEAD |
|---|---|---|
| Ветка результатов (отчёты, JSON, этот пакет) | `agent/v217-clock-estimator-stability-results` | `b3e5d52` |
| Кодовая ревизия, на которой выполнялся анализ | `coordinator/v217-clock-estimator-stability` | `02e9591d` |
| Репозиторий | `vovansrnd/ValveBench` | — |

⚠️ **Не ориентироваться на `main`:** он на ~152 коммита позади этих веток.
Для проверки актуальной реализации нужен код ревизии `02e9591d`.

## Состав пакета

```
AUDITOR-PACKAGE/
├── README.md                  ← этот файл
├── AUDIT-BRIEF.md             ← готовое задание независимому аудитору
├── CODE-REVISION.txt          ← происхождение и точный состав среза кода
├── MANIFEST.json              ← машинный манифест данных (SHA256, метаданные)
├── MANIFEST.md                ← человекочитаемая таблица + SHA256
├── MANIFEST.sha256            ← для `sha256sum -c` (данные пакета)
├── MANIFEST-CODE.sha256       ← для `sha256sum -c` (код, пути от code/)
├── code/                      ← ПОЛНЫЙ КОД ревизии 02e9591d (без .git/кэшей)
├── wav/                       ← 14 WAV: 7 стереозахватов clock + 7 поданных программ
├── json/                      ← исходный JSON v2.14 + результаты офлайна v2.16/v2.17
└── reports/                   ← отчёты v2.14 / v2.16 (исправленный) / v2.17
```

## Код (code/) — предмет аудита

`code/` — точное дерево ревизии **`02e9591d`** (`coordinator/v217-clock-estimator-stability`),
извлечённое `git archive` — только отслеживаемые файлы: без `.git`, без
`__pycache__`, без `*.pyc`, без WAV. Проверка подлинности:
`sha256sum -c MANIFEST-CODE.sha256` (из каталога `code/`). Единственное
отличие от полного дерева — удалён `code/measurements/` (99 МБ бинарных
артефактов ранних диагностик v2.1–v2.3, к аудиту не относятся); см.
`CODE-REVISION.txt`.

Файлы первоочередного просмотра (по заданию координатора):

| Файл в code/ | Предмет |
|---|---|
| `valvebench/dsp.py` | `analyze_tone`, оценка `f0`, спектральный центроид — **главный предмет аудита** |
| `valvebench/measure.py` | `Rig.run`, onset, `clock_check`, `capture_clock_ppm`, resampling |
| `valvebench/cli.py` | `split_clock_preflight`, повтор на другом уровне |
| `verification/v216_clock_offline_gate.py` | R-only onset и полный clock-gate |
| `verification/v217_clock_estimator_stability.py` | скользящий анализ ppm |
| `verification/test_split_clock_preflight.py` | логика clock-гейтов (FakeRig) |
| `verification/test_capture_timebase_resampling.py` | коррекция временной базы |

Проверки, которые аудитор может запустить офлайн из `code/` (данные уже в
пакете):

```bash
python3 -m py_compile valvebench/dsp.py valvebench/measure.py valvebench/cli.py
python3 verification/test_rig_run_regression.py          # 24/24
python3 verification/test_gainmatch_nonzero_loopback_correction.py   # 2/2
python3 - <<'PY'                                          # selftest 106/106
import sys; sys.path.insert(0, '.')
from valvebench.selftest import run_selftest, format_selftest
r = run_selftest(); print(format_selftest(r))
raise SystemExit(0 if r["passed"] == r["total"] else 1)
PY
python3 verification/test_ui_workflow_contracts.py        # 23/23
```

Для воспроизведения v2.16/v2.17 офлайн: сложить захваты из `wav/` и
`json/20261114-004425-results.json` в один каталог и передать его как
`--source-wav-dir` соответствующему скрипту.

### Захваты (обязательный минимум по заданию координатора)

| idx | Файл (wav/) | Уровень | Направление | Итог v2.14 |
|---|---|---|---|---|
| 1 | `20261114-004425-clock-01--72dB-capture.wav` | −72 dBFS | asc | FAIL (onset не найден) |
| 2 | `20261114-004425-clock-02--66dB-capture.wav` | −66 dBFS | asc | FAIL |
| 15 | `20261114-004425-clock-15--66dB-capture.wav` | −66 dBFS | desc | FAIL |
| 16 | `20261114-004425-clock-16--72dB-capture.wav` | −72 dBFS | desc | FAIL |
| 3 | `20261114-004425-clock-03--60dB-capture.wav` | −60 dBFS | asc | ok |
| 4 | `20261114-004425-clock-04--54dB-capture.wav` | −54 dBFS | asc | ok |
| 8 | `20261114-004425-clock-08--30dB-capture.wav` | −30 dBFS | asc | ok |

Все захваты: 192000 Гц × 1344000 кадров × 2 канала (7.0 с), IEEE float32.
Рядом — поданные программы (`*-program.wav`, 4.55 с) для проверки геометрии
`Rig._seg`/`Rig.run` и холодного старта (п.7 брифа).

### JSON и отчёты

| Файл | Содержимое |
|---|---|
| `json/20261114-004425-results.json` | исходный результат аппаратной серии v2.14 (16 clock-попыток + THD) |
| `json/v216-clock-offline-gate-results.json` | офлайн-проверка R-only onset и полного gate |
| `json/v217-clock-estimator-stability-results.json` | скользящая стабильность ppm |
| `reports/REPORT-v214-clock-diagnostic.md` | отчёт аппаратной серии |
| `reports/REPORT-v216-clock-offline-gate.md` | отчёт офлайн-gate (исправленная ревизия, источник `agent/v216-clock-offline-gate-results @ c320099`) |
| `reports/REPORT-v217-clock-estimator-stability.md` | отчёт стабильности оценщика |

## Верификация целостности

```bash
cd AUDITOR-PACKAGE
sha256sum -c MANIFEST.sha256
```

Полные SHA256 всех файлов — также в `MANIFEST.md`.

## Известные ограничения данных

* В WAV **нет** transport/XRUN-метаданных — офлайн-проверка числовых гейтов
  не подтверждает отсутствие ошибок передачи/захвата (см. бриф, п.9).
* Офлайн-пересчёт воспроизводит production-оценщик (Δ ≤ 0.004 ppm), что
  подтверждает воспроизводимость, но НЕ является независимой метрологической
  валидацией — оба результата получены одним и тем же оценщиком.
* Уровни −42…−30 dBFS проверены на прямом соединении D10→E-MU **без DUT**;
  перенос на высокоусилительный фонокорректор недопустим без отдельной
  проверки.

## Контекст проблемы (кратко)

Прямая линия D10→E-MU, 192 кГц, пилот 1 кГц. Production `clock_check()`
требует: SNR каждого канала ≥ 20 дБ, |ppm| ≤ 100, расхождение L/R ≤ 5 ppm,
отсутствия транспортных ошибок; временная база корректируется по среднему ppm.
На низких уровнях (−72/−66 dBFS) onset отказывает (порог поднимается шумом
более шумного канала L — v2.15), а при скольжении окна оценка ppm L плавает на
14–23 ppm при стабильном R и постоянном SNR (v2.17). Прохождение gate зависит
от положения окна. Главный вопрос аудита — корректность самого оценщика
частоты (`dsp.analyze_tone`, спектральный центроид) и допустимость его
результата для коррекции временной базы (см. AUDIT-BRIEF.md).
