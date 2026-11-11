# Square-wave / transient capture notes

A DAC-generated square wave is a useful *diagnostic stimulus*, not a substitute for the calibrated frequency-response, THD, noise and IMD tests.

## Recommended initial experiment

1. Characterize the source first: DAC output -> the same ADC input path, same cables and levels. Save this as a loopback reference.
2. Use a 1 kHz band-limited square wave at a conservative level (start around -20 dBFS peak for loopback; lower for a DUT if its gain could cause ADC overload). Do not use 0 dBFS.
3. For a phono preamp, the stimulus path must be documented. With a physical inverse-RIAA network between DAC and DUT, a square wave from the DAC is inverse-RIAA-shaped before the DUT. Its output can approach a square only to the extent that the complete inverse-RIAA + DUT chain matches the intended response. Without Anti-RIAA, do not expect a square at the DUT output.
4. Repeat loopback and DUT capture at identical DAC/ADC rates, gain, loading and cabling. Compare DUT trace against the source loopback, not against an ideal mathematical square.
5. Record both channels and preserve raw samples. A PNG alone is insufficient for timing, overshoot, settling and spectral calculations.

## Why 1 kHz

A 1 kHz square contains odd harmonics at 1, 3, 5, ... kHz. Harmonics through 19 kHz sample the conventional audio band. This makes 1 kHz a practical first visual/transient check. 100 Hz emphasizes low-frequency response and can reveal LF tilt/rumble behavior; 10 kHz is not a good general-purpose square-wave choice for a 20 kHz audio band because only its fundamental and possibly one odd harmonic lie in-band.

## DAC and ADC rates

A sampled square must be band-limited. The generator must not make an ideal discontinuity with aliased harmonics. The current generator uses a finite Fourier series below Nyquist, but has a 99-harmonic cap. Raising DAC Fs above 192 kHz can extend the realizable analog edge if the DAC reconstruction filter and output stage pass the added bandwidth. It does not magically create more DUT information: the ADC analog bandwidth and ADC sampling rate are the measurement ceiling. For example, a 192 kHz ADC has a 96 kHz Nyquist limit and 5.208 us sample spacing. Edge timing much finer than a few samples cannot be stated confidently from that capture.

Use the highest clean DAC rate supported by the actual DAC when studying its generated edge, but separately state the ADC rate. For useful audio-band (up to ~20 kHz) comparison, 192 kHz ADC is adequate for visualization; precise sub-10-us edge/rise-time claims need a faster ADC and calibrated analogue bandwidth.

## Required raw export for future implementation

Export one ZIP with:
- `capture.csv`: `time_s, adc_l, adc_r` in normalized ADC full-scale samples; no screenshot-only data.
- optional `dac_reference.csv`: generated samples and DAC sample-time base.
- `metadata.json`: DUT id, test id, date/time, DAC/ADC device stable identities, ALSA PCM, sample rates, sample formats/channels, mixer/gain state, generator waveform/frequency/peak level, Anti-RIAA presence/identity, source/load/cable notes, calibration profile IDs, capture length, trigger/index, software version, and warnings/XRUN/event intervals.
- `MANIFEST.json`: sizes and SHA-256 hashes.

Analysis candidates: measured fundamental and odd-harmonic amplitudes, duty cycle, high/low plateau mean, rise/fall 10-90%, overshoot/undershoot relative to settled plateau, ringing frequency/decay, and settling time. Metrics must be marked unavailable when the ADC bandwidth/sample rate cannot resolve them. For DUT judgement, compare to a matching loopback capture acquired with the same converter settings.
