# v2.26.4 — execution manifest (offline forensic audit)

- Pinned coordinator SHA: edc088776794b0fef765994a66f2c09d89e83446
  ("docs: correct v2263 single-level manifest count")
- Result branch: agent/v2264-envelope-event-audit (создана от pinned SHA)
- Worktree: /home/vovans/.cache/valvebench-v2264-edc0887-run (detached;
  HEAD = edc0887…; tracked state чист)
- Host: vovans-PC; Ubuntu 26.04.1 LTS; kernel 7.0.0-43-generic
- Python 3.14.4, NumPy 2.3.5; valvebench 0.1.21-dev4

## Характер этапа

Исключительно offline-анализ сохранённых NPZ/JSON четырёх исторических
событий (v2261-r1 run-01, v2262 run-01, v2263-r1 run-02/run-03).
Ни одного вызова ALSA/backend/устройств; без inventory; без harness;
никаких записей/playback. Исходные NPZ/JSON не изменялись (чтение +
независимый пересчёт SHA256).

## Точная команда анализа (из корня pinned worktree)

    D4=measurements/diagnostics/v2264-envelope-event-audit
    PYTHONPATH=. python3 "$D4/audit_v2264_envelope_events.py" --out-dir "$D4/results" \
      2>&1 | tee "$D4/results/console.log"

- audit script: measurements/diagnostics/v2264-envelope-event-audit/
  audit_v2264_envelope_events.py (не патчи production: импортирует
  envelope_stability, Z_BURST_MAX, TREND_MAX, Rig из pinned valvebench/;
  Rig инстанцируется на SimBackend БЕЗ open() — только для f_of/_tone_an/
  _onset; ни одного обращения к аудиоустройствам).
- Два итерационных исправления САМОГО АУДИТ-СКРИПТА до финального прогона
  (KeyError: out["dir"]; отсутствовал trend_span в subwindow_detail) —
  не изменения production/исходников; финальный прогон один, полный.
- exit 0.

## Целостность входов

- INPUT-SHA256SUMS.json: все NPZ четырёх run-каталогов —
  **26 NPZ = 18 measurement captures + 8 preflight** (A=8, B=8, C=5,
  D=5); sha256 и размеры пересчитаны независимо, все совпали с их
  SHA256SUMS.json. Исправлено по ревизии координатора (было ошибочно
  22+8). JSON-инвентарь прочитанных файлов — INPUT-JSON-SHA256SUMS.json
  (v2.26.5).
- git diff --check: чисто; в diff нет valvebench/, harness v2260–v2263,
  тестов, исходных результатов/captures.
