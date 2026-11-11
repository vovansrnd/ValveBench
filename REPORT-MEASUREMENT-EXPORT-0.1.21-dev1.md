# QA report — ValveBench 0.1.21-dev1

Base: `ValveBench-0.1.20-dev3-reviewfix4.2`.

## Scope

This development build adds a first portable DUT measurement export workflow, explicit ADC-side PAD calibration, and safeguards against mislabelling a result with a different DUT ID.

- UI has DUT ID and note fields plus `СОХРАНИТЬ ЗАМЕР`.
- Each export is an immutable session in `<application-root>/measurements/devices/<ID>/<timestamp>-<kind>/`.
- `session.json` is the authoritative record and includes result arrays, measurement conditions, audio-device identity, PCM settings and calibration snapshot.
- `metrics.csv` and `curves.csv` are convenience exports; `MANIFEST.json` records byte sizes and SHA-256 for the four session payload files.
- The default path is resolved relative to `store.py`'s application tree, not the current working directory.
- A new measurement run clears the prior exportable result. DUT ID is snapshotted at measurement start; a changed ID blocks saving to prevent accidental cross-device attribution.
- ADC PAD is a separate parameter (`adc_att_db`) from DAC→DUT attenuation (`att_db`). Voltage calculations refer readings back to the DUT side. A frequency-response calibration is rejected after PAD configuration changes.

## Verification performed

- `python3 -m compileall -q valvebench` — PASS.
- `python3 -m valvebench --version` — `ValveBench 0.1.21-dev1`.
- `python3 -m valvebench selftest` — **103/103 PASS, 0 FAIL**.
- DUT export checks include valid/unsafe IDs, JSON sanitation, complete numerical curve export, blank missing curve samples, no-overwrite session naming, manifest hashes and symlink escape rejection.
- SimBackend sweep integration export retains 435 frequency points and FR/THD data in the saved JSON/CSV.
- ADC PAD calculations, loopback calibration correction, profile scoping and stale FR profile rejection are covered by selftests.
- Archive was extracted to a clean directory; `./run.sh --version` and all 103 selftests were repeated there.
- From `/tmp` (an unrelated working directory), a smoke export landed inside the extracted application tree, not `/tmp/measurements`; the smoke session was removed afterwards.
- Archive SHA-256 sidecar verified; patch `--dry-run` and actual application to a clean fix4.2 tree passed. The patched source tree matched the release tree (apart from an empty `measurements/` directory created by the path smoke test).

## Not verified in this environment

- GTK4/PyGObject is unavailable (`ModuleNotFoundError: gi`), so the GUI was not launched and its save button was not visually clicked here.
- No physical ALSA interface was attached; E-MU/ASUS operation, analogue calibration and the physical resistor PAD were not tested.
- `run.sh`'s GTK GUI launch, real audio E2E and real-device measurements remain a user-side hardware acceptance step.
- This export does not yet save raw PCM/WAV for every measurement kind, import sessions, or compare devices A/B. It preserves the numerical arrays and metrics already returned by each analysis function.

## Safety / scope

The existing installed copy at `/opt/MyApps/Adudio/ValveBench` was not modified. No test GUI was opened on the user's Wayland desktop. This archive is a development build, not an instruction to replace the installed copy without backup.
