# v2.26.1 — execution manifest (BLOCKED до железа: офлайн-гейт не пройден)

- Pinned coordinator SHA: 0a74ef2b69c5e9790506f183f51db9c82b088f7b
  ("Make v2.26.1 harness resolve prior-stage helpers")
- Result branch: agent/v2261-gainmatch-level-margin (создана от pinned SHA)
- Worktree: /home/vovans/.cache/valvebench-v2261-0a74ef2-run
  (отдельный detached worktree; HEAD = 0a74ef2b…; tracked state чист)
- Host: vovans-PC; Ubuntu 26.04.1 LTS; kernel 7.0.0-43-generic
- Python 3.14.4, NumPy 2.3.5, SciPy 1.16.3; valvebench 0.1.21-dev4

## Офлайн-этапы

1. py_compile всех 4 файлов (v2260 harness/test + v2261 harness/test):
   OK.
2. unittest D0 (pattern test_v226*.py, PYTHONPATH=.:$D0:$D1):
   **11/11 OK** — оба прогона (run1 в сессии, run2 в файле
   results/unittest-D0-run2.log). ProductionSimulatorIntegrationTests
   выполнен, не skipped.
3. unittest D1 (тот же паттерн): **FAILED (failures=1), воспроизводимо
   2/2** (run1 в сессии; run2 — results/unittest-D1-run2.log):
   ProductionSimulatorIntegrationTests.test_both_injection_signs_pass_at_both_levels
   — assertEqual(harness.assess_series(inj_r, INJECT_DB), []) при
   level=-18.0 dBFS упал: corrected ΔG=+12.041 dB на всех трёх
   частотах (expected +6.021 ±0.20). Часть уровня −24 dBFS упавших
   ассертов не имеет (failure единственный, на −18 R-инъекции).
4. ./run.sh selftest: **PASS**, exit 0 (results/selftest.log).

По правилам V2261-EXECUTION.md («оба unittest-прогона OK… если
что-либо не пройдено — аппаратный этап не выполнять») аппаратная
часть НЕ запускалась: ни preflight, ни inventory, ни записи.
Inventory не выполнялся, так как аппаратный этап заблокирован до него.

## Диагностика падения (наблюдение агента, правок не вносилось)

В test_both_injection_signs_pass_at_both_levels состояние инъекции
state["channel"] не сбрасывается в None перед loopback-серией
КАЖДОГО уровня: на втором уровне (−18 dBFS) loopback-серия
измеряется с ещё активной L-инъекцией первого уровня, reference
наследует ослабление −6.0206 dB, поэтому corrected ΔG inject-R
= +12.041 dB ≈ 2×6.020599913 на всех частотах. Точный механизм:
после первой итерации цикла state["channel"]="L"; перед loopback
второго уровня сброса нет; add_d10_edge_guards + inject применяются
в guarded() при каждом play_record.

## Аттестация

- Код, тесты, критерии не изменялись; production не тронут.
- Ни одного доступа к аудиоустройствам: без ALSA, записей, playback,
  mixer, инъекций на железе.
- Кабели/mixer/конфигурация не менялись; config.json не читался.
- Все артефакты — текстовые логи офлайн-прогонов.
