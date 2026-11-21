# v2.25.3 — execution manifest (исправлен по ревизии координатора v2.25.3)

- Pinned analyzer/source SHA: ae65d7caf41758c9275a3af5c0db68ff3f208dba
  (анализатор НЕ перезапускался; JSON и лог байт-в-байт из result
  commit 79de22ba2ace66d6e888aeead098bf1e8a880fab)
- Result branch: agent/v2253-report-corrections
  (создан координатором от coordinator/v2253-report-corrections)
- Host: vovans-PC; CPU AMD Ryzen 5 3600, 12 threads
- OS/kernel (uname -a, дословно): Linux vovans-PC 7.0.0-43-generic #43-Ubuntu SMP PREEMPT_DYNAMIC Fri Oct 23 13:25:25 UTC 2026 x86_64 GNU/Linux
- Дистрибутив (из /etc/os-release, дословно): NAME="Ubuntu"; VERSION="26.04.1 LTS (Resolute Raccoon)"
- Python 3.14.4, NumPy 2.3.5, SciPy 1.16.3, Git 2.53.0

## Входной WAV (метаданные при оформлении исправления; metadata-only проверка без ALSA)

- /tmp/v2250-probe.wav: присутствует; 5 760 044 байта;
  owner vovans, mode 0644; SHA-256
  d1676fe55e822e703d89c34b1ee2f2fbb96143eb163540e18078081756282ca9;
  RIFF WAVE PCM 24 бит, стерео, 96 кГц (заголовок из v2.25.0).

## Опубликованный JSON (result commit 79de22b)

- v2253-pilotband-noise-characterization.json: **51 397 байт**,
  **SHA-256 b763095d987cd314cf4e32f4953d229114622c9edc1cf5ed8681f1535843c4cd**
  (получено из git-объекта коммита 79de22b; не пересчитывалось и не
  менялось)

## Исправление документации (этот commit)

- Изменены ТОЛЬКО: V2253-AGENT-REPORT.md, V2253-EXECUTION-MANIFEST.md
- git diff --check: чисто
- Анализатор, JSON, лог, production — байт-в-байт без изменений;
  анализатор НЕ перезапускался

## Аттестация

- Без ALSA/arecord/aplay/mixer/device opens/перечисления устройств.
- Без записи, playback, синтетических сигналов, нового захвата.
- Без взаимодействия с оборудованием или изменения его конфигурации.
- Без изменений production, frozen estimator, анализатора.
- Доступ к оборудованию отсутствовал; использован только metadata-only
  доступ к существующему /tmp/v2250-probe.wav (stat/файл/SHA — без
  открытия аудиоустройства).
