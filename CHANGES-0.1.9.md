# ValveBench 0.1.9

## Исправления анализа

- Tone fit: coarse FFT-peak detection followed by a bounded refinement inside the central Hann lobe. The previous broad `nominal ±0.5 Hz` scalar search could settle on a neighbouring correlation maximum spaced approximately `1/T` apart.
- Steady residual: DC and H1…Hn are removed independently in each window using an orthogonal least-squares projection. The residual metric no longer relies on a narrow fixed notch around H1.
- Each output channel's measured frequency is used for its own steady interval. The same measured frequency is projected onto both ADC channels to quantify coherent cross-channel tone leakage.
- Both ADC channels are analysed and both appear in the top-window list.
- The measured fundamental level is checked again in the long recording. A main tone outside the requested target tolerance marks the series `INVALID`; a successful short calibration alone is insufficient.
- `steady.json` stores device paths, PCM formats, target, calibration trials, measured L/R tone map, validity and analysis summaries.

## Multi-interface ALSA

- `cli devices` lists playback-only and capture-only PCM endpoints separately, plus full-duplex endpoints.
- `cli steady --play-dev hw:C,D --rec-dev hw:C,D` supports different DAC and ADC devices.
- Playback and capture formats are negotiated independently by opening each candidate format through ALSA. Supported candidates: S24_3LE, S24_LE, S32_LE, S16_LE.
- The GUI continues to list full-duplex endpoints and now gives each selected device its advertised common rates. ALSA `stream0` rates are hints; actual PCM open remains authoritative.

## GUI / spectrum

- The large spectrum now repeats dBFS/bin tick labels on the right and reserves space for them.

## Measurement passport

- Passport schema is 2. Each rate stores the actual backend, PCM paths, formats and channel counts where available.
- Strict comparison rejects different devices, mappings, tone/level/repeat settings and per-rate PCM I/O formats. Old passports remain readable and comparisons warn when one side lacks the per-rate PCM details.

## Selectively carried from 0.1.8

- symmetric PSD smoothing;
- `unstable` levelscan classification for high-repeat-spread points;
- loopback series validity `OK / INVALID / INCOMPLETE` and XRUN invalidation;
- THD page default frequency 997 Hz;
- simulator modulation/jitter parameters used by the new diagnostics self-test.

## Verification in build environment

- `./run.sh selftest`: 36/36 PASS.
- `python3 -m compileall -q valvebench`: PASS.
- CLI help, quick simulator passport, JSON save, passport comparison: PASS.
- `./run.sh debug --no-selftest`: confirms GTK introspection and ALSA utilities/hardware are not installed in the build container. Therefore no physical interface or GTK window test is claimed.
