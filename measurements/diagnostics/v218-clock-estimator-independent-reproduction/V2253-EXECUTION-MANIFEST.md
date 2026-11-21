# v2.25.3 — execution manifest (agent run on vovans-PC)

- Pinned source SHA: ae65d7caf41758c9275a3af5c0db68ff3f208dba
  (branch coordinator/v2253-pilotband-noise-characterization; checkout --detach
  в изолированном worktree /home/vovans/.cache/valvebench-v2253-ae65d7-run;
  SHA сверен, tracked-дерево чистое; анализатор не редактировался)
- Result branch: agent/v2253-pilotband-noise-characterization
- Host: vovans-PC; CPU AMD Ryzen 5 3600, 12 threads
- OS/kernel (uname -a, дословно): Linux vovans-PC 7.0.0-43-generic #43-Ubuntu SMP PREEMPT_DYNAMIC Fri Oct 23 13:25:25 UTC 2026 x86_64 GNU/Linux
- Дистрибутив (из /etc/os-release, дословно): NAME="Ubuntu"; VERSION="26.04.1 LTS (Resolute Raccoon)"
- Python 3.14.4, NumPy 2.3.5, SciPy 1.16.3, Git 2.53.0
- PYTHONPYCACHEPREFIX: /home/vovans/.cache/valvebench-v2253-pycache (вне worktree)

## Входной файл (метаданные; только test/stat/file/sha256sum — без ALSA)

- /tmp/v2250-probe.wav: 5 760 044 байта
- SHA-256: d1676fe55e822e703d89c34b1ee2f2fbb96143eb163540e18078081756282ca9
- RIFF WAVE PCM 24 бит, стерео, 96 кГц; 960 000 кадров = 10.0 с; не менялся

## Команды и результаты

| Команда | Exit | Wall | Max RSS |
|---|---|---|---|
| python3 -m py_compile v2253_pilotband_noise_characterization.py | 0 | — | — |
| /usr/bin/time -v python3 v2253_pilotband_noise_characterization.py --wav /tmp/v2250-probe.wav --output $D | 0 | 0:02.29 | 262676 КБ |

## Валидация JSON

- v2253-pilotband-noise-characterization.json: валидный JSON; 96 кГц
  стерео, 10.0 с ✓; source bytes/SHA-256 совпадают с preflight ✓;
  9 двухсекундных окон с 1-с hop ✓; все числовые поля конечны ✓
- Опубликованные файлы: v2253-pilotband-noise-characterization.json,
  v2253-pilotband-noise-characterization-run.log,
  V2253-EXECUTION-MANIFEST.md, V2253-AGENT-REPORT.md
