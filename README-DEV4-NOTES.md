# ValveBench 0.1.21-dev4 — handoff

- Main change: independent ALSA playback/DAC and capture/ADC endpoints with one common measurement Fs and fail-closed clock preflight for split physical devices.
- Pair-bound calibration and Gain Match reference; separate DAC/ADC identity in measurement passports.
- Full details: `CHANGES-0.1.21-dev4.md`, `REPORT-DUAL-AUDIO-0.1.21-dev4.md`.
- First hardware acceptance: `AGENT-TASK-0.1.22-HARDWARE-ACCEPTANCE.md`.
- No GTK desktop or physical D10/E-MU test was performed during build.
