# ValveBench 0.1.13 — Gain Match frequency series

## Added

- Added a repeatable two-channel gain measurement series at 100 Hz, 1 kHz and 10 kHz to the existing Gain Match page.
- Frequencies are measured in interleaved repeat passes; the production tone analyzer estimates the captured frequency, so small independent DAC/ADC clock offsets are handled rather than forcing an exact nominal-frequency fit.
- Results include L/R input levels, residual/SNR, repeat spread, clipping/validity flags and calibrated channel gains when DAC and ADC voltage calibration is complete.
- If voltage calibration is incomplete, the report explicitly labels L−R as an ADC-level difference and leaves absolute G and ΔG blank.
- The saved result records the per-channel one-point calibration values. The report warns that no frequency-dependent DAC/ADC loopback correction is applied; results at 100 Hz and 10 kHz therefore still include the measurement interface's frequency response.
- Added DUT/model, tube/position and notes fields. Each completed series is automatically saved as JSON and CSV under `./measurements/gain-match/` relative to the project working directory.

## Validation

- `python3 -m py_compile valvebench/gainmatch.py valvebench/store.py valvebench/ui.py valvebench/selftest.py`
- `python3 -m valvebench selftest`: 61/61 PASS, including known 1.5× vs 1.0× synthetic gain at all three frequencies, uncalibrated labelling, and JSON/CSV metadata persistence.
- GTK runtime validation was not possible in the build container because PyGObject (`gi`) is not installed. GUI code compiles, but the new controls need a visual smoke test on the user's Ubuntu desktop.
