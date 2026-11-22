# v2.26.3 — execution manifest (BLOCKED до железа: офлайн-гейт не пройден)

- Pinned coordinator SHA: 96da4c6f34a4e28def58d2dca942a5a52c93b439
  ("v2.26.3: retain proven simulator seed for repeatable integration gate")
- Result branch: agent/v2263-operating-level-qualification (создана от
  pinned SHA)
- Worktree: /home/vovans/.cache/valvebench-v2263-96da4c6-run
  (отдельный detached worktree; HEAD = 96da4c6…; tracked state чист)
- Host: vovans-PC; Ubuntu 26.04.1 LTS; kernel 7.0.0-43-generic
- Python 3.14.4, NumPy 2.3.5, SciPy 1.16.3; valvebench 0.1.21-dev4

## Офлайн-гейты

1. py_compile всех 6 файлов (D0+D1+D3 harness/test): OK.
2. unittest D0: **11/11 OK** (exit 0; integration выполнен, не skipped).
3. unittest D1: **6/6 OK** (exit 0; integration выполнен, не skipped).
4. unittest D3: **FAILED (failures=1), exit 1** —
   test_single_candidate_level_and_production_gates_are_pinned;
   ProductionSimulatorIntegrationTests D3 формально ok, но см. ниже.
5. ./run.sh selftest: PASS, exit 0 (для полноты; гейт D3 уже не пройден).

По правилам V2263-EXECUTION.md («Все suites и selftest должны пройти.
Иначе аппаратный этап запрещён», «При offline failure к устройствам не
обращаться») аппаратная часть НЕ запускалась: ни inventory, ни
preflight, ни записей.

## Диагностика падения D3 (наблюдение агента, правок не вносилось)

test_v2263_gainmatch_operating_level.py строка 10:

    import v2261_gainmatch_level_margin as harness

Тестовый файл импортирует V2261-модуль вместо
v2263_gainmatch_operating_level. Следствия:

- harness.LEVELS_DBFS в тестах = (−24.0, −18.0) из v2261, а тест-контракт
  требует (−20.0,) → FAIL
  test_single_candidate_level_and_production_gates_are_pinned
  (AssertionError: Tuples differ: (-24.0, -18.0) != (-20.0,)).
- При этом сам v2263_gainmatch_operating_level.py строка 31 корректно
  определяет LEVELS_DBFS = (-20.0,) — harness не проверялся этим
  набором вовсе: ВСЕ 6 тестов D3 (включая
  ProductionSimulatorIntegrationTests.test_both_injection_signs_pass_at_candidate_level,
  который "ok") исполнялись против v2261-модуля, т.е. интеграционный
  гейт v2263 фактически не валидировал v2263-harness.

Вероятное исправление координатора: строка 10 тест-файла →
`import v2263_gainmatch_operating_level as harness` (и проверка, что
остальные ассерты contract-теста соответствуют v2263-константам).
После ревизии теста и нового pinned SHA готов исполнить v2.26.3
полностью.

## Аттестация

- Код, тесты, критерии не изменялись; production не тронут.
- Ни одного обращения к аудиоустройствам: без ALSA, записей, playback,
  mixer; кабели/настройки не менялись.
- Артефакты: три unittest-лога + selftest.log (текстовые).
