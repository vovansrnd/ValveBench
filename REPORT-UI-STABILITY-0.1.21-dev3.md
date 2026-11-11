# ValveBench 0.1.21-dev3 — status/layout stability report

## Changes

- HeaderBar ADC/GEN toggles now keep constant visible labels (`АЦП`, `ГЕН`). The code no longer appends a status dot or toggles `suggested-action` on these controls. ToggleButton's built-in active state remains the state indicator.
- Header status label now has a fixed 104 px slot / 13-character width, is single-line and ellipsized. Full status text is in the tooltip.
- Persistent MeterStrip health text is single-line, bounded and ellipsized. The complete status/error/event string remains in the tooltip instead of changing the health label's wrapped height.
- Page measurement status labels are single-line/ellipsized and expose full text through tooltips. Detailed failure content remains in the result area.
- Added source-contract checks for all these invariants.

## Findings in dev2 source

The status path contained several layout-dependent properties:

1. `Window.status` had a minimum width of 12 characters but a maximum of 18; the visible text changes between short success, running and error messages.
2. ADC button text changed between `АЦП` and `АЦП ●`, and `suggested-action` was added/removed while the stream toggled.
3. GEN button text changed between `ГЕН` and `ГЕН ●`, and `suggested-action` was added/removed while generation toggled. Theme CSS can have different padding/border metrics for suggested-action.
4. `MeterStrip.health` wrapped long transport-event diagnostics to a variable number of lines while health text changes dynamically.

These are concrete code-level sources capable of changing widget requisitions. Whether they exactly explain the user's observed rapid jumps still needs GUI confirmation. ToastOverlay is an overlay and should not allocate width to the underlying layout; the next agent must measure allocations rather than assume ToastOverlay is the root cause.

## Checks

- `python3 -m compileall -q valvebench verification` — PASS.
- `bash run.sh --version` — `ValveBench 0.1.21-dev3`.
- `bash run.sh selftest` — 106/106 PASS.
- `python3 verification/test_ui_workflow_contracts.py` — 18/18 PASS.
- No physical audio device or DUT was used.
- GTK4/PyGObject GUI was not launched in this environment. No visual/Wayland acceptance is claimed.

## Not implemented in dev3

- Independent DAC and ADC endpoint selection or separate sample rates.
- Asynchronous clock-domain compensation for independent USB devices.
- Raw oscilloscope sample ZIP export.
- DUT import/comparison matrix.

These are specified in `docs/AGENT-TASK-0.1.22-EVENING.md`. Square-wave guidance is in `docs/SQUARE-WAVE-MEASUREMENT.md`.

## Safety

- Installed `/opt/MyApps/Adudio/ValveBench` was not modified.
- No GUI or process was launched on the user's desktop.
