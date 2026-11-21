# v2.25.0 — execution manifest (agent run on vovans-PC) — BLOCKED AT INVENTORY

**Статус: BLOCKED (inventory-only)** — захват не выполнен: текущее
подключение входа не удовлетворяет условию «known quiet analog
measurement path». По контракту (V2250-EXECUTION.md, «Hardware safety»
и «Hardware inventory») остановка после инвентаризации, без подмены
устройства, без изменения подключений и регуляторов.

- Pinned source SHA: ea9f6a7bcda3fc7e316d4a5f091a35325fc9fd9a
  (branch coordinator/v2250-real-noise-sigma-validation; worktree
  создан, harness и frozen estimator скомпилированы — но тест не
  запускался, т.к. захват заблокирован инвентаризацией)
- Result branch: agent/v2250-real-noise-sigma-validation
- Host: vovans-PC; CPU AMD Ryzen 5 3600, 12 threads
- OS/kernel (uname -a, дословно): Linux vovans-PC 7.0.0-43-generic #43-Ubuntu SMP PREEMPT_DYNAMIC Fri Oct 23 13:25:25 UTC 2026 x86_64 GNU/Linux
- Дистрибутив (из /etc/os-release, дословно): NAME="Ubuntu"; VERSION="26.04.1 LTS (Resolute Raccoon)"
- Python 3.14.4, NumPy 2.3.5, SciPy 1.16.3, Git 2.53.0

## Инвентаризация (полный вывод — v2250-hardware-inventory.log)

* `./run.sh cli cards`: card 0 HDMI (без capture), card 1 SMSL D10
  (playback only), **card 2 E-MU 0404 USB** — единственное capture-
  устройство; capture native S24_3LE, 2/4 канала, 44.1–192 кГц.
* Стабильный ALSA ID: **hw:CARD=USB,DEV=0** (E-MU 0404 | USB).
* `arecord --dump-hw-params` (S24_3LE): CHANNELS [2 4], RATE
  [44100 192000] — **native 96 кГц стерео поддерживается**; S16_LE
  не поддерживается устройством.
- Устройство свободно (fuser pcmC2D0c — пусто).
- Диск: 19 ГБ свободно (хватает для 384 с ≈ 221 МБ).

## Пробный захват 10 с (S24_3LE, 96000, стерео — устройство не менялось)

- RMS: L −71.0 дБFS, R −69.9 дБFS; peak −47.5 дБFS; клиппинга нет.
- **Block RMS (0.5 с, полная полоса): −84.9…−58.3 дБFS — разброс
  26.7 дБ**: на входе присутствует сильная НЧ-составляющая
  (**макс PSD на ~12 Гц**), источник которой не идентифицирован.
- После HP 300 Гц: −85.0…−80.7 дБFS (разброс 4.3 дБ) — выше 300 Гц
  пол тихий и стабильный (~−82 дБFS broadband), что вне всех полос
  анализа пилота (1 кГц ±32 Гц).

## Почему остановка

Контракт требует: «The input must already be a known quiet analog
measurement path» и «If the current path is unknown, not quiet,
carrying content... stop after inventory. Do not guess». Физическое
подключение входов E-MU агенту неизвестно (подключалось пользователем
ранее), полный полосовой сигнал не тихий (НЧ-составляющая ~12 Гц,
разброс 26.7 дБ) → capture заблокирован до подтверждения подключения
пользователем/координатором.

Примечание для координатора: НЧ-составляющая (12 Гц) лежит вне полос
анализа (пилот 1 кГц ±32 Гц; residual-PSD полоса 2–32 Гц от несущей),
а выше 300 Гц пол тих — если пользователь подтвердит, что текущее
подключение входов E-MU и есть ожидаемый измерительный путь (например,
idle-выход D10), повторный запуск возможен без изменений оборудования;
решение — за координатором/пользователем.

## Опубликованные файлы (blocked subset контракта §8)

- v2250-hardware-inventory.log (полная инвентаризация + анализ пробника)
- V2250-EXECUTION-MANIFEST.md (этот файл)
- V2250-AGENT-REPORT.md (blocked-отчёт)

## Подтверждения

- Production/valvebench, frozen estimator, coordinator harness — не
  редактировались (py_compile выполнен, но тест не запускался).
- Никакого playback, никаких изменений mixer/gain/cables/DUT, никаких
  reset/clean/rebase/force. Пробный захват 10 с — read-only с уже
  подключенного входа, сохранён вне Git (/tmp/v2250-probe.wav).
