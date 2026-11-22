# v2.26.1 — agent technical report: BLOCKED (офлайн-гейт не пройден, железо не запускалось)

Краткий статус: **BLOCKED**. Обязательный офлайн-гейт не пройден:
unittest набор v2261 FAILED (1 failure, воспроизводимо 2/2).
По V2261-EXECUTION.md аппаратная часть на D10 → E-MU не выполнялась.

## Офлайн-итоги

| Проверка | Результат |
|---|---|
| py_compile (4 файла: v2260+v2261 harness/test) | OK |
| unittest D0 (11 тестов, incl. ProductionSimulatorIntegrationTests) | **OK** (2/2 прогонов) |
| unittest D1 (6 тестов) | **FAILED (failures=1)** (2/2 прогонов) |
| ./run.sh selftest | **PASS**, exit 0 |

## Точный failure

Тест: ProductionSimulatorIntegrationTests.
test_both_injection_signs_pass_at_both_levels
(файл test_v2261_gainmatch_level_margin.py, строка ~136).

    AssertionError: Lists differ: [...] != []
    ["94 Hz: corrected ΔG=+12.041 dB; expected +6.021 ±0.20 dB",
     "994 Hz: corrected ΔG=+12.041 dB; …",
     "9994 Hz: corrected ΔG=+12.041 dB; …"] : R attenuation at -18.0 dBFS

Упавший ассерт — inject-R на уровне **−18 dBFS**; предыдущие ассерты
(включая всю часть уровня −24 dBFS и R-инъекцию до этой строки) не
падали — failure единственный. SimBackend-интеграционный тест
выполнялся реально (не skipped).

## Причина (наблюдение агента; код и тесты не менялись)

В тестовом цикле по уровням state["channel"] не сбрасывается в None
перед loopback-серией каждого уровня. Последовательность:

1. level −24: state=None → loopback чистый (OK) → reference OK →
   inject-R OK → inject-L (state="L") OK.
2. level −18: state всё ещё "L" → loopback измеряется С активной
   L-инъекцией −6.0206 dB → reference наследует это ослабление →
   inject-R даёт delta_adc = +6.0206 − (−6.0206) = +12.041 dB →
   corrected ΔG = +12.041 dB на всех частотах — ровно наблюдаемое
   значение (2 × 6.020599913).

Механизм подтверждается кодом: guarded() применяет
inject_channel_attenuation при каждом play_record, а сброс
state["channel"]=None между уровнями в тесте отсутствует.

## Что НЕ делалось

- Аппаратный запуск v2261 (preflight, loopback, инъекции) — запрещён
  инструкцией при непройденном офлайн-гейте; ни один экземпляр
  не запускался, записей нет.
- Inventory endpoints — не выполнялся (аппаратный этап заблокирован
  до него).
- Никаких правок кода/тестов/критериев; повторный запуск unittest —
  только для подтверждения воспроизводимости (не rerun после правки).

## Рекомендация координатору

Исправление — сброс state["channel"]=None перед loopback каждого
уровня (или перенос сброса в начало итерации цикла). После ревизии
теста и нового pinned SHA готов исполнить v2.26.1 полностью по той же
процедуре (офлайн-гейты → inventory → аппаратные уровни −24/−18).
