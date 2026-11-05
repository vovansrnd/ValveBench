# ValveBench 0.1.17 — test report

- `python3 -m py_compile valvebench/*.py`: PASS.
- `python3 tools/check_page_scroll.py`: PASS.
- `./run.sh selftest`: **71/71 PASS**.
- GTK visual check: NOT RUN. The build environment has no `gi` / GTK4 / libadwaita Python bindings. The GTK4 widget tree is therefore syntax-checked and structurally inspected, but the actual scrollbar appearance/input behavior must be confirmed on the target Ubuntu desktop.

The change is limited to page layout. Measurement DSP, calibration, CLI and storage modules are unchanged.
