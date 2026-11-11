# QA report — ValveBench 0.1.21-dev4

## Scope

Основа: предоставленный `ValveBench-0.1.21-dev3.tar.gz` (dev3). Установленная копия не изменялась.

Цель: завершить реальный backend для независимого выбора PCM playback (DAC) и capture (ADC), не заявляя поддержку независимых Fs там, где измерительное ядро работает на одной временной сетке.

## Реализовано

1. `AlsaCli` принимает `hw_out` и `hw_in`; playback-команда получает только endpoint ЦАП, capture-команда — endpoint АЦП.
2. Формат/число каналов для playback и capture выбираются независимо.
3. `common_pcm_rates()` вычисляет пересечение поддерживаемых частот пары в пределах текущей сетки проекта (до 192 кГц). Когда `stream0` не предоставляет данные, список является кандидатом для фактической проверки при открытии ALSA. Несовместимость не маскируется успешной подстановкой иной частоты.
4. В GUI pair selectors реально передаются backend; pair-key использует стабильные идентификаторы двух физических endpoints. Старый профиль одной карты остаётся совместимым.
5. Перед coherent-тестами с разными endpoints выполняется 1 кГц clock preflight при −72 dBFS. Расхождение более 100 ppm останавливает измерение. Коррекции/ресэмплинга дрейфа пока нет.
6. Gain Match reference использует общий алгоритм идентификации пары на стороне UI и DSP. Mixer fingerprint проверяет playback и capture отдельно.
7. CLI `--dac`/`--adc` поддерживается основными измерительными командами; `--dev` не удалён. CLI также выполняет clock preflight.
8. DUT/measurement passport записывает стороны ЦАП и АЦП раздельно; сравнение выявляет изменение одной стороны и предупреждает при смешении старого формата паспорта с новым.
9. Ring buffer теперь выделяется на фактическом Fs после открытия backend.

## Tests run

| Проверка | Результат |
|---|---:|
| `./run.sh selftest` | 106/106 PASS |
| `test_dual_endpoint_backend` | 7/7 PASS |
| `test_split_clock_preflight` | 6/6 PASS |
| `test_gainmatch_split_pair` | 6/6 PASS |
| `test_dual_endpoint_passports_cli` | 8/8 PASS |
| Итого независимые unittest | 27/27 PASS |
| `test_ui_workflow_contracts.py` | 23/23 PASS |
| `compileall` | PASS |
| version / CLI help | PASS |

All tests above are software, simulation, or source-contract checks. They do not establish hardware performance or visual correctness.

## Explicit limitations

- GTK4/PyGObject is absent in the build environment; no GUI was launched here and no Wayland visual acceptance was performed.
- D10 and E-MU were not connected. Actual ALSA PCM endpoints, supported formats, common rates, clock drift and mixer fingerprints must be tested on the user's machine.
- The shared measurement Fs is still required. If D10 supports 384 kHz but ADC supports only 192 kHz, the current precision pipeline is limited to the common mode (typically at most 192 kHz). There is no independent DAC=384 kHz / ADC=192 kHz mode yet.
- Clock preflight measures relative frequency error; it does not synchronize sample clocks or resample. It may reject a route if the −72 dBFS tone is not detectable through the actual connected path; do not bypass that gate without understanding the cause.
- ALSA `stream0` is card-scoped capability information, not an absolute guarantee for every PCM subdevice. The final exact PCM open is authoritative and may still report an unsupported rate/format.

## Expected first hardware acceptance

See `AGENT-TASK-0.1.22-HARDWARE-ACCEPTANCE.md`. Start with device enumeration and a known-safe direct loopback at conservative digital levels. Do not connect a DUT into the clock-check path until the direct route and levels are understood.
