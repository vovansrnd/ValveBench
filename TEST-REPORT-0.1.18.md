# ValveBench 0.1.18 — independent package verification

This report covers an independently reconstructed 0.1.18 candidate from the
uploaded 0.1.17 archive, applying the one-line UI layout restoration and the
version bump. It does not claim to reproduce the other agent's GUI run or
physical-device session.

- `python3 -m py_compile valvebench/*.py`: PASS.
- `python3 -m valvebench --version`: `ValveBench 0.1.18`.
- `python3 -m valvebench selftest`: **71/71 PASS**.
- Diff from 0.1.17: exactly `valvebench/ui.py` and `valvebench/__init__.py`.
- P0/P1 protected modules `pcm.py`, `steady.py`, and `io_health.py`: unchanged.

GTK interactive GUI and physical hardware were not independently tested in
this environment. The separate agent-reported GUI/hardware checks are reviewed
in the accompanying conversation, but their logs/archive were not attached
here for independent reproduction.
