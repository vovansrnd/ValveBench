# v2.26.1 — execution manifest (повтор r1)

- Pinned coordinator SHA: 11e6beb1078dc3b717bd1d1d338a23f471b8fe21
  ("v2.26.1: use fresh agent result branch for retry"); исправлен только
  тест (см. V2261-COORDINATOR-REVIEW.md); production/harness не менялись.
- Result branch: agent/v2261-gainmatch-level-margin-r1 (создана от pinned
  SHA). Прежняя ветка agent/v2261-gainmatch-level-margin с BLOCKED
  (679c583) не переписывалась.
- Worktree: /home/vovans/.cache/valvebench-v2261-r1-11e6beb-run
  (отдельный detached worktree; HEAD = 11e6beb…; tracked state чист)
- Host: vovans-PC; Ubuntu 26.04.1 LTS; kernel 7.0.0-43-generic
- Python 3.14.4, NumPy 2.3.5, SciPy 1.16.3; valvebench 0.1.21-dev4

## Офлайн-гейты (все пройдены)

1. py_compile 4 файлов (v2260+v2261 harness/test): OK.
2. unittest D0: **11/11 OK**; ProductionSimulatorIntegrationTests
   выполнен (не skipped).
3. unittest D1: **6/6 OK** (после исправления теста координатором);
   ProductionSimulatorIntegrationTests.test_both_injection_signs_pass_at_both_levels
   выполнен (не skipped), прошёл.
4. ./run.sh selftest: PASS, exit 0.

## Read-only inventory

- ./run.sh cli devices / cli cards → devices-inventory.txt,
  cards-inventory.txt (exit 0).
- D10 playback = hw:1,0; E-MU 0404 capture = hw:2,0 (единственный
  capture endpoint); 192000 Hz объявлен обоими — общий режим подтверждён.
- mixer fingerprint: доступен; dac/adc идентичны во всех сериях;
  mixer и подключения агент не менял.

## Аппаратный запуск (один прогон, без повторов)

    cd /home/vovans/.cache/valvebench-v2261-r1-11e6beb-run
    PYTHONPATH=. python3 measurements/diagnostics/v2261-gainmatch-level-margin/v2261_gainmatch_level_margin.py \
      --dac hw:1,0 --adc hw:2,0 --fs 192000 \
      --out-dir measurements/diagnostics/v2261-gainmatch-level-margin/results/vovans-PC-run-01 \
      > results/vovans-PC-run-01-console.log 2>&1

- out-dir не существовал перед запуском (создан harness-ом).
- **V2261_STATUS=PASS, EXIT_CODE=0.**
- Длительность ~4 мин; один экземпляр; дожидался в SSH-сессии
  (nohup-обёртка только для устойчивости к разрыву SSH — не повтор).

## Проверка артефактов

- SHA256SUMS.json: **18/18 файлов подтверждены** независимо
  (пересчитан sha256 и размер каждого).
- Записи: аудиомассивы submitted_to_backend и capture — **0 NaN,
  0 Inf** во всех 11 captures. Наблюдение (не fail): 2 NaN в
  скалярном метаполе generator_level_dbfs (shape=()) двух
  preflight-захватов — сериализация None→NaN в метаданных, аудио
  не затронуто.
- git diff --check: чисто; production/source/test/task diff отсутствует.

## Аттестация

- Кабели не отсоединялись/не переставлялись; DUT не подключался.
- ALSA mixer, громкость, gain, routing, sample rate — не менялись.
- Без Setup-калибровок, passport, mixer writes; config.json не
  читался/не сохранялся.
- Уровни −24 и −18 dBFS (оба с большим запасом до 0 dBFS); в инъекции
  только ослабление одного канала ровно 0.5×, положительного gain нет.
- Порядок уровней и серий по методике: preflight один раз → −24
  (loopback→reference→inject-R→inject-L) → −18 (аналогично, отдельный
  reference).
