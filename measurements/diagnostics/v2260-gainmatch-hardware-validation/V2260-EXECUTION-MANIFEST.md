# v2.26.0 — execution manifest

- Pinned coordinator SHA: d6728469a724cfb80298149d69538e202a8648dc
  ("Document v2.26.0 D10 to E-MU hardware validation protocol")
- Result branch: agent/v2260-gainmatch-hardware-validation (создана от
  pinned SHA; source/test/task files не изменялись)
- Worktree: /home/vovans/.cache/valvebench-v2260-d672846-run
  (отдельный detached worktree; HEAD = d672846…; tracked state чист)
- Host: vovans-PC; CPU AMD Ryzen 5 3600, 12 threads
- OS: Ubuntu 26.04.1 LTS; kernel 7.0.0-43-generic
- Python 3.14.4 (GCC 15.2.0), NumPy 2.3.5, SciPy 1.16.3
- valvebench_version: 0.1.21-dev4

## Офлайн-этапы (до железа, оба обязательных пройдены)

1. py_compile harness и теста: OK, без ошибок.
2. PYTHONPATH=. python3 -m unittest discover … -v: **11/11 OK**
   (ProductionSimulatorIntegrationTests выполнен реально, не skipped).
3. ./run.sh selftest: **PASS**, exit 0.

## Read-only inventory endpoints

- ./run.sh cli devices → devices-inventory.txt; ./run.sh cli cards →
  cards-inventory.txt (exit 0 оба).
- Сопоставление однозначно: SMSL D10 playback = **hw:1,0**;
  E-MU 0404 | USB capture = **hw:2,0** (единственный capture endpoint
  в системе); общий режим 192000 Hz объявлен обоими endpointами.
- mixer fingerprint (из harness): dac c9bb2776…63bba,
  adc 5ead4bff…b50 — ОДИНАКОВЫ во всех трёх сериях (reference, inject-R,
  inject-L); mixer и подключения между сериями агент не менял.

## Запуски (точные команды)

Два abort-прогона на подготовке окружения (оба завершились на setup,
ДО открытия аудиоустройств; физического воздействия не было; логи
сохранены в results/):

1. results/vovans-PC-run-01-aborted-setup-console.log — FileExistsError:
   агент ошибочно создал out-dir заранее; harness требует его
   отсутствия. Exit до устройств.
2. results/vovans-PC-run-01-aborted-no-PYTHONPATH-console.log —
   ModuleNotFoundError: valvebench; запуск без PYTHONPATH=. (ошибка
   окружения агента). Exit до устройств.

Финальный запуск (exit code = код python, без tee):

    cd /home/vovans/.cache/valvebench-v2260-d672846-run
    PYTHONPATH=. python3 measurements/diagnostics/v2260-gainmatch-hardware-validation/v2260_gainmatch_hardware_validation.py \
      --dac hw:1,0 --adc hw:2,0 --fs 192000 \
      --out-dir measurements/diagnostics/v2260-gainmatch-hardware-validation/results/vovans-PC-run-01 \
      > …/results/vovans-PC-run-01-console.log 2>&1

- out-dir не существовал перед запуском (создан harness-ом).
- **V2260_STATUS=FAIL, EXIT_CODE=2** (критерий inject-L validity не
  закрыт; детали в V2260-AGENT-REPORT.md).
- Длительность ~3 мин (00:56–00:59 UTC); результаты в results/vovans-PC-run-01/.

## Проверка артефактов

- SHA256SUMS.json: **11/11 файлов подтверждены** (пересчитано sha256
  и размер каждого: 5 captures/*.npz + 6 JSON).
- Записи: NaN=0, Inf=0 во всех 5 captures (проверено np.isnan/isinf
  по всем float-массивам submitted_to_backend и capture).
- git diff --check: чисто.

## Аттестация

- Кабели не отсоединялись и не переставлялись; DUT не подключался.
- ALSA mixer, громкость, gain, source, routing, частота — не менялись.
- Без Setup-калибровок, CLI calibrate, passport, записи калибровочных
  профилей; ~/.config/valvebench/config.json не читался/не сохранялся.
- Максимальный тестовый уровень −30 dBFS peak; только программное
  ослабление одного канала на 6.020599913 dB (0.5×), положительного
  gain не было.
- Код, тесты, критерии не изменялись; production не тронут.
- WAV-файлы в git не публиковались; captures — npz-массивы,
  требуемые заданием в result directory.
