# v2.26.2 — execution manifest (три независимые сессии)

- Pinned coordinator SHA: 564a4af239c7c78f7661099656cc80fc06efc9b1
  ("v2.26.1 r1: level margin run — PASS, lowest passing level -24 dBFS")
  — как указано координатором для v2.26.2; production/harness/тесты не
  менялись.
- Result branch: agent/v2262-gainmatch-repeatability (создана от pinned
  SHA). Ветки agent/v2261-gainmatch-level-margin-r1 и BLOCKED-ветка не
  переписывались.
- Worktree: /home/vovans/.cache/valvebench-v2262-564a4af-run
  (отдельный detached worktree; HEAD = 564a4af…; tracked state чист)
- Host: vovans-PC; Ubuntu 26.04.1 LTS; kernel 7.0.0-43-generic
- Python 3.14.4, NumPy 2.3.5, SciPy 1.16.3; valvebench 0.1.21-dev4

## Офлайн-гейты (все пройдены до железа)

1. py_compile 4 файлов: OK.
2. unittest D0: 11/11 OK; unittest D1: 6/6 OK
   (оба ProductionSimulatorIntegrationTests выполнены, не skipped —
   logs results/unittest-D0.log, unittest-D1.log).
3. ./run.sh selftest: PASS.

## Read-only inventory

- devices-inventory.txt / cards-inventory.txt (exit 0):
  D10 playback = hw:1,0; E-MU 0404 capture = hw:2,0; 192000 Hz общий.
- mixer_fingerprint доступен во всех трёх сессиях; mixer/кабели агент
  не менял между сессиями.

## Аппаратное исполнение — ровно три последовательные сессии

Единая форма каждой команды (по методике; set +e вокруг harness, код из
PIPESTATUS[0]; out-dir не создавался заранее):

    cd /home/vovans/.cache/valvebench-v2262-564a4af-run
    PYTHONPATH=. python3 measurements/diagnostics/v2261-gainmatch-level-margin/v2261_gainmatch_level_margin.py \
      --dac hw:1,0 --adc hw:2,0 --fs 192000 \
      --out-dir measurements/diagnostics/v2262-gainmatch-repeatability/results/vovans-PC-run-NN \
      > results/vovans-PC-run-NN-console.log 2>&1

- run-01: завершён штатно; V2261_STATUS=PASS, EXIT_CODE=0.
- run-02: запущен только после проверки run-01 summary; PASS/0.
- run-03: запущен только после проверки run-02 summary; PASS/0
  (overall PASS harness-а по правилу «хотя бы один уровень», но m24
  FAIL — см. отчёт; QC-FAIL штатно не прерывал план сессий).
- Параллельных запусков не было; сессии не повторялись; между сессиями
  mixer/кабели/настройки не менялись.

## Проверка артефактов (независимая)

- SHA256SUMS.json каждого запуска: **18/18 ok ×3** (npz=8, json=10 в
  каждом; пересчитаны sha256 и размеры).
- Аудиомассивы (submitted_to_backend, capture) во всех 24 captures:
  **0 NaN / 0 Inf**. Метаполе generator_level_dbfs: 2 NaN на preflight
  npz в каждой сессии (harness намеренно пишет np.nan, уровень ещё не
  задан — учтено отдельно, аудио не затронуто).
- Max |sample| по аудио: 0.125893 (≈ −18.0 dBFS peak, m18 loopback) —
  клипа нет.
- git diff --check: чисто; production/source/test diff отсутствует.

## Аттестация

- Кабели не отсоединялись/не переставлялись; DUT не подключался.
- ALSA mixer, громкость, gain, routing, sample rate не менялись; без
  Setup-калибровок/passport/mixer writes; config.json не читался.
- В инъекциях только программное ослабление одного канала 0.5×; без
  положительного gain.
