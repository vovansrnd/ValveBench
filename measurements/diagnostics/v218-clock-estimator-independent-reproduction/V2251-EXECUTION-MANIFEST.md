# v2.25.1 — execution manifest (agent run on vovans-PC)

- Pinned source SHA: a16675d060930d7a523ea2573fad5816a82aaaac
  (branch coordinator/v2251-probe-characterization; checkout --detach
  в изолированном worktree /home/vovans/.cache/valvebench-v2251-a16675-run;
  SHA сверен, tracked-дерево чистое; анализатор не редактировался)
- Result branch: agent/v2251-probe-characterization
- Host: vovans-PC; CPU AMD Ryzen 5 3600, 12 threads
- OS/kernel (uname -a, дословно): Linux vovans-PC 7.0.0-43-generic #43-Ubuntu SMP PREEMPT_DYNAMIC Fri Oct 23 13:25:25 UTC 2026 x86_64 GNU/Linux
- Дистрибутив (из /etc/os-release, дословно): NAME="Ubuntu"; VERSION="26.04.1 LTS (Resolute Raccoon)"
- Python 3.14.4, NumPy 2.3.5, SciPy 1.16.3, Git 2.53.0
- PYTHONPYCACHEPREFIX: /home/vovans/.cache/valvebench-v2251-pycache (вне worktree)

## Входной файл (метаданные до запуска; только test/stat/file/sha256sum — без ALSA)

- /tmp/v2250-probe.wav: существует, 5 760 044 байта
- SHA-256: d1676fe55e822e703d89c34b1ee2f2fbb96143eb163540e18078081756282ca9
- Владелец vovans:1000, режим 0644
- file: RIFF little-endian, WAVE, Microsoft PCM, 24 bit, stereo 96000 Hz
- WAV-заголовок (проверено анализатором): 960000 кадров, 2 канала,
  10.0 с, dtype signed_int_32 (S24_3LE распакован в int32)
- Файл не менялся с момента захвата (mtime 2026-11-21 15:11 = время
  захвата v2.25.0)

## Команды и результаты

| Команда | Exit | Wall | Max RSS |
|---|---|---|---|
| python3 -m py_compile v2251_probe_characterization.py | 0 | — | — |
| /usr/bin/time -v python3 v2251_probe_characterization.py --wav /tmp/v2250-probe.wav --output $D | 0 | 0:04.16 | 347828 КБ |

Полный stdout/stderr + time -v: v2251-probe-characterization-run.log.
git diff --check: чисто. Никаких ALSA-команд, записи, playback,
изменений оборудования. Raw WAV в Git не включён.

## Валидация JSON

- v2251-probe-characterization.json: валидный JSON; channels 2,
  fs 96000, длительность 10.0 с ✓; block_count 20 (0.5-с блоки) ✓;
  все числовые поля конечны ✓; source bytes/SHA-256 в JSON совпадают
  с preflight ✓
- Опубликованные файлы: v2251-probe-characterization.json,
  v2251-probe-characterization-run.log, V2251-EXECUTION-MANIFEST.md,
  V2251-AGENT-REPORT.md
