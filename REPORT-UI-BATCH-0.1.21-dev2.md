# QA report — ValveBench 0.1.21-dev2

Base: `ValveBench-0.1.21-dev1-transfer.zip` (development source), itself based on fix4.2.

## Scope

This build clarifies and tightens the workflow already present in dev1:
- DUT batch: default response + THD/THD+N + noise, optional IMD; one package and one device passport export.
- Explicit physical handoff after noise so the operator can change the input termination to Anti-RIAA before driven measurements.
- Separate generator / oscilloscope page with sine, triangle, square, sawtooth, white noise and pink noise; view generated pre-DAC buffer or captured ADC input.
- Compact top controls and automatic generator-off when moving to measurement pages.
- Reduced redundant UI redraw requests and removed continuously changing frame counters from the status string.
- DUT note now remains the note captured at measurement start.

## Verification performed

- `python3 -m compileall -q valvebench verification` — PASS.
- `bash run.sh --version` — `ValveBench 0.1.21-dev2`.
- `bash run.sh selftest` — **106/106 PASS**.
- `python3 verification/test_ui_workflow_contracts.py` — **14/14 PASS**. This is a source-level contract check for the UI structure and guard conditions; it does not simulate GTK widgets.
- Generator waveform and noise tests validate finite samples, expected frame counts, band limits and level ceilings.
- Batch-export selftest validates that noise, sweep curves and THD data remain in one DUT passport with nested CSV curves.
- `store` tests cover JSON/CSV export, manifest hashes, safe device IDs, path confinement and unique session directories.

## Not verified in this environment

- PyGObject/GTK4 (`gi`) is unavailable, so no GTK window was launched. Therefore visual layout, actual widget interactions and the user's reported flicker cannot be confirmed here.
- No physical sound card / DUT was connected. No real analogue input/output level, noise, THD, frequency response or IMD result was taken.
- The automatic generator mute and batch handoff have source-level checks only. They still need a short visual/operational acceptance on the user's machine.

## Safety

- No installed application tree was modified.
- No process or GUI was launched on the user's desktop.
- No PAD topology or E-MU input impedance assumption is introduced by this build. `docs/MEASUREMENT-CALIBRATION.md` explicitly withdraws the earlier unsupported 91 kΩ / 10 kΩ proposal and treats the observed 4–5 kΩ as a preliminary estimate from response, not as a verified complex impedance.
