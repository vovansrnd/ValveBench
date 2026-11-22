# v2.26.3 — execution manifest (повтор r1)

- Pinned coordinator SHA: 74d71b3101d49f2b19ba70f3b8794af5106d8d7f
  ("v2.26.3: review blocked import attempt and coordinator repair") —
  исправлен только тест (импорт v2263 + identity-проверка); harness,
  production, пороги не менялись.
- Result branch: agent/v2263-operating-level-qualification-r1 (создана
  от pinned SHA). BLOCKED-ветка agent/v2263-operating-level-qualification
  (5e18ba1) не переписывалась.
- Worktree: /home/vovans/.cache/valvebench-v2263-r1-74d71b3-run
  (detached; HEAD = 74d71b3…; tracked state чист)
- Host: vovans-PC; Ubuntu 26.04.1 LTS; kernel 7.0.0-43-generic
- Python 3.14.4, NumPy 2.3.5, SciPy 1.16.3; valvebench 0.1.21-dev4

## Офлайн-гейты (все пройдены до железа)

1. py_compile 6 файлов (D0+D1+D3): OK.
2. unittest D0: 11/11 OK (integration не skipped).
3. unittest D1: 6/6 OK (integration не skipped).
4. unittest D3: **7/7 OK**, включая новый
   test_this_suite_imports_its_own_v2263_harness (identity через
   harness.__file__) и integration test_both_injection_signs_pass_at_candidate_level
   против v2263-модуля на −20 dBFS (не skipped). Проверка агента:
   v2263_gainmatch_operating_level.__file__ указывает на v2263 файл,
   LEVELS_DBFS = (−20.0,).
5. ./run.sh selftest: PASS, exit 0.

## Read-only inventory

- devices-inventory.txt / cards-inventory.txt (exit 0 оба):
  D10 playback = hw:1,0; E-MU 0404 capture = hw:2,0; 192000 Hz общий.
- mixer_fingerprint доступен во всех трёх сессиях; mixer/кабели агент
  не менял между сессиями.

## Аппаратное исполнение — ровно три последовательные сессии на −20 dBFS

Единая форма команды (по методике; out-dir не создавался заранее):

    cd /home/vovans/.cache/valvebench-v2263-r1-74d71b3-run
    PYTHONPATH=. python3 measurements/diagnostics/v2263-operating-level-qualification/v2263_gainmatch_operating_level.py \
      --dac hw:1,0 --adc hw:2,0 --fs 192000 \
      --out-dir measurements/diagnostics/v2263-operating-level-qualification/results/vovans-PC-run-NN \
      > results/vovans-PC-run-NN-console.log 2>&1

- run-01: штатно завершён; STATUS=PASS, EXIT_CODE=0.
- run-02: запущен после проверки run-01; STATUS=FAIL, EXIT_CODE=2 —
  штатный QC-FAIL (1 invalid repeat, envelope), fatal/clip/XRUN нет →
  по методике продолжено.
- run-03: запущен после проверки run-02; STATUS=FAIL, EXIT_CODE=2 —
  штатный QC-FAIL (1 invalid repeat, envelope), fatal нет.
- Параллельных запусков нет; сессии не повторялись; mixer/кабели между
  сессиями не менялись.

## Проверка артефактов (независимая)

- SHA256SUMS.json каждой сессии: **11/11 ok ×3** (пересчитаны sha256 и
  размеры). Замечание к инструкции: v2263-harness с ОДНИМ уровнем
  сохраняет 11 файлов (5 NPZ + 6 JSON) на сессию, а не 18 (8+10) — 18
  соответствуют двухуровневому v2261; несоответствие не является
  недостачей: все присутствующие файлы верифицированы.
- Аудиомассивы submitted_to_backend и capture во всех 15 captures:
  **0 NaN / 0 Inf**. Метаполе generator_level_dbfs preflight-NPZ:
  NaN-sentinel (2 на сессию), как ожидаемо инструкцией.
- Max |sample|: 0.100000 (−20.0 dBFS peak, m20 loopback) — клипа нет.
- git diff --check: чисто; production/source/test diff отсутствует.

## Аттестация

- Кабели не отсоединялись/не переставлялись; DUT не подключался.
- ALSA mixer, громкость, gain, routing, sample rate не менялись; без
  Setup-калибровок/passport/mixer writes; config.json не читался.
- В инъекциях только программное ослабление одного канала 0.5×.
