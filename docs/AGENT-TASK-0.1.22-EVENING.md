# Agent task — first hardware acceptance of ValveBench 0.1.21-dev4

## Rules

- Work from the supplied **0.1.21-dev4** source tree, not an older archive.
- Do not modify or overwrite `/opt/MyApps/Adudio/ValveBench` (installed copy).
- Do not open test windows on the user's actual desktop beyond the normal manually launched app. Automated GUI tests must use isolated Xvfb + D-Bus + a private `XDG_RUNTIME_DIR` and `GDK_BACKEND=x11`.
- Do not change system mixer controls, sample-rate settings or saved user calibration silently. Record before/after state.
- Do not force a sample rate that is not advertised by both selected endpoints. Do not bypass a failed clock preflight.
- Keep command outputs, logs and results inside the dev project tree, under `measurements/validation-dev4/`.

## 1. Enumerate hardware

Run:

```bash
./run.sh cli cards
./run.sh cli devices
```

Record the exact current ALSA playback endpoint for D10 and capture endpoint for E-MU. USB `card:N` numbers can change after reconnect; do not hard-code an old number without re-enumerating.

Record for both endpoints:
- card ID/name, PCM device number;
- advertised Playback/Capture rates, formats and channels from `stream0`;
- `hw_params` while each stream is active, if it can be captured without disrupting the user's session.

## 2. GUI checks

Launch the dev4 tree manually. Confirm:

1. DAC dropdown selects D10; ADC dropdown selects E-MU independently.
2. Both selections survive app restart.
3. Fs choices are only common advertised rates. Do not expect 384 kHz to appear if ADC max is 192 kHz.
4. Switching either endpoint changes pair profile and does not inherit the old pair's voltage/FR/noise calibration.
5. Reverting to the original same-device pair restores its prior profile.
6. The compact HeaderBar selectors keep constant size/labels while stream warnings, XRUN and reconnect messages appear. Check allocation/layout during repeated XRUN or carrier interruption. Do not “fix” movement by enlarging controls.
7. No unrelated copy is activated when starting another dev tree.

## 3. Conservative direct-loopback validation

First make a known-safe direct cable loop from D10 output L/R to E-MU input L/R using the user's already understood connection/attenuation. Do not insert the phono preamp or Anti-RIAA for this check. If the safe input level/connection is uncertain, stop and ask the user before applying signal.

Set `DAC` and `ADC` in GUI to D10/E-MU. Then run (substitute actual endpoints):

```bash
./run.sh cli devices
./run.sh cli --dac hw:DAC_CARD,PCM --adc hw:ADC_CARD,PCM --rate 192000 clock
./run.sh cli --dac hw:DAC_CARD,PCM --adc hw:ADC_CARD,PCM --rate 192000 loopback --levels=-30,-40 --repeats 3 --silence-seconds 0 --report measurements/validation-dev4/direct-loopback.txt
```

If 192 kHz is not in the pair intersection, select a rate actually supported by both. The clock check uses a deliberately low 1 kHz, −72 dBFS tone. It should not be changed to a high level to “make it pass”.

Do not add the DUT until direct loopback is detected, levels are safe, and relative clock error is reported. If clock test says tone not found, investigate level, route and endpoint selection; do not interpret that as clock accuracy.

## 4. Measurement paths and compatibility

- Verify one low-level THD command over the split route, for example 997 Hz at −30 dBFS, after direct loopback validation.
- Verify a flat sweep and one noise capture.
- Verify Gain Match loopback reference creation and reuse with the same endpoint pair.
- Change only the ADC mixer and confirm that the reference is rejected when the mixer fingerprints are available. If fingerprints are unavailable, the report must say so clearly.
- Run `passport --quick`; confirm JSON records both `dac_device` and `adc_device`, independent PCM formats and sample rate.
- Test old config migration: an old config with only `device=hw:C,D` must reopen as DAC=ADC=that same endpoint, never as SIM.
- Test CLI legacy `--dev hw:C,D` and new `--dac ... --adc ...` paths.

## 5. Deliverables

Save inside `measurements/validation-dev4/`:
- `cards.txt`, `devices.txt`;
- clock check text + JSON if available;
- direct loopback report and CSV;
- one THD result, one sweep result, one noise result;
- GUI observations, actual PCM params and screenshots if useful;
- exact command lines and any errors.

Report separately:
- software behavior verified;
- actual hardware path verified;
- actual clock ppm measured;
- visual layout checked;
- not checked / inconclusive items.

## 6. Do not implement these in the hardware-acceptance pass

Do not add independent DAC/ADC sample rates, resampling, clock discipline or comparison matrix yet. The current precision DSP uses one common Fs. After the split route passes hardware acceptance, the next development stage can add raw ADC waveform export and import/compare DUT passports in a matrix (one DUT per column, metric per row, chart action per metric).
