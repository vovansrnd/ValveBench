# ValveBench 0.1.21-dev3 — handoff notes

Start with:
- `REPORT-UI-STABILITY-0.1.21-dev3.md` — exact UI status/layout changes and verification limits.
- `docs/AGENT-TASK-0.1.22-EVENING.md` — evening tasks: separate DAC/ADC endpoints, raw oscilloscope capture export, DUT comparison matrix, GUI reflow investigation.
- `docs/SQUARE-WAVE-MEASUREMENT.md` — square-wave method and what data to preserve.

Run from this directory:
```bash
bash run.sh --version
bash run.sh selftest
python3 verification/test_ui_workflow_contracts.py
```

No GTK visual acceptance or real-hardware test is claimed for dev3.
