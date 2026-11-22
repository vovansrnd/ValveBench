# v2.26.5 — execution manifest (offline; две части)

- Pinned coordinator SHA: 357be1f3ca508e213e0ab59f3ee7f7afe76a36f5
  ("v2.26.5: include hashed clock preflight and run metadata"; на 3 коммита
  впереди 752b105, ancestry проверен)
- Result branch: agent/v2265-frequency-offset-model (создана от pinned SHA)
- Worktree: /home/vovans/.cache/valvebench-v2265-357be1f-run (detached;
  HEAD = 357be1f…; tracked state чист)
- Host: vovans-PC; Ubuntu 26.04.1 LTS; kernel 7.0.0-43-generic
- Python 3.14.4, NumPy 2.3.5; valvebench 0.1.21-dev4

## Offline-only

Ни одного вызова ALSA/backend/устройств, ни inventory, ни записей.
Исходные NPZ/JSON не изменялись (чтение + независимые SHA256).
Rig инстанцируется на SimBackend без open() (только f_of/_tone_an/_onset).

## Команды (из корня pinned worktree)

    D4=measurements/diagnostics/v2264-envelope-event-audit
    PYTHONPATH=. python3 "$D4/audit_v2264_envelope_events.py" --out-dir "$D4/results" \
      > "$D4/results/console.log" 2>&1     # exit 0 (fail-closed clean)
    D5=measurements/diagnostics/v2265-frequency-offset-model
    PYTHONPATH=. python3 "$D5/audit_v2265_frequency_offset_model.py" --out-dir "$D5/results" \
      > "$D5/results/console.log" 2>&1     # exit 0

py_compile обоих скриптов: OK. Два итерационных исправления v2264-скрипта
до финального прогона (глобальный JSON_READS; KeyError out[dir] из
v2.26.4) — правки только audit-скрипта, не production.

## Часть A (воспроизводимость v2264) — выполнено

- V2264-EXECUTION-MANIFEST.md: NPZ count исправлен на
  **26 NPZ = 18 measurement + 8 preflight** (A=8, B=8, C=5, D=5).
- audit_v2264_envelope_events.py: fail-closed (fs/stereo/keys/finiteness
  массивов и всех окон, окна ровно 384000×2, 9 окон в порядке, z-mismatch
  → errors → exit 1; totals-гейты: rows=324, z≥4=6, all_match, 26/26 NPZ);
  CSV lineterminator="\n"; used_files-параметр удалён;
  INPUT-JSON-SHA256SUMS.json: 30 уникальных JSON (4 SHA256SUMS + 18 series
  + 4 preflight + 4 run-summary), sha256+size.
- Перегон: **exit 0, rows=324, z≥4=6, all_z_match=True, npz 26/26**.
- Выводы v2264 не менялись (правки: манифест NPZ count + тех. fail-closed).

## Часть B (null-модель) — выполнено

- 324 окна (18 capture × 9 сегментов × 2 канала); все фактические
  z_actual_cmd совпали с JSON (fail-closed mismatch→exit 1).
- Модели: pure-tone LS-fit конвенция
  s(t)=amp·cos(2πf·t−phase), phase=atan2(c2,c1) по [1,cos,sin];
  synth_meas (measured f0, amp/phase окна) и synth_cmd (commanded f,
  согласованные amp/phase); без шума/гармоник/модуляции.
- Артефакты: FREQUENCY-OFFSET-MATRIX.csv (324), PURE-TONE-PHASE-SENSITIVITY.csv
  (6 окон × 16 фаз + 4 сдвига), EVENT-RESIDUALS.csv (6 окон),
  frequency-offset-model.json, INPUT-SHA256SUMS.json (26 NPZ + 41 JSON),
  console.log (полный).

## Аттестация

- Без изменений production/порогов/harness/тестов; исторические
  результаты не переписывались; git diff --check чисто (LF CSV).
- Доступ к оборудованию отсутствовал; config.json не читался.
