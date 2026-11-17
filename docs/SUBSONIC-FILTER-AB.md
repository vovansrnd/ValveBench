# Subsonic filter A/B test with ValveBench

This procedure measures the *incremental* response of an output subsonic filter. It compares the same phono stage in BYPASS and FILTERED positions. The difference cancels the common RIAA target and, to first order, the unchanged DAC/ADC response.

## Capture

Use the normal phono-stage measurement path: DAC → Anti-RIAA input network → DUT → the same safe output attenuator/ADC input. Keep the exact same cables, input network, attenuator, card pair, mixer settings, level and sample rate for every capture. Only change the filter switch position.

Check the currently enumerated devices first; ALSA card numbers may change after USB reconnect:

```bash
./run.sh cli cards
./run.sh cli devices
mkdir -p measurements/subsonic
```

Capture the bypass state:

```bash
./run.sh cli --rate 96000 noise-response \
  --kind riaa --seconds 60 --level -36 --lo 2 --hi 1000 \
  --csv measurements/subsonic/bypass.csv
```

Select one filter position and repeat with a different output filename, for example:

```bash
./run.sh cli --rate 96000 noise-response \
  --kind riaa --seconds 60 --level -36 --lo 2 --hi 1000 \
  --csv measurements/subsonic/filter-10Hz.csv

./run.sh cli --rate 96000 noise-response \
  --kind riaa --seconds 60 --level -36 --lo 2 --hi 1000 \
  --csv measurements/subsonic/filter-15Hz.csv
```

The selected DAC/ADC pair comes from ValveBench configuration. When using separate devices, add `--dac hw:X,0 --adc hw:Y,0` before `noise-response`, using the current values shown by `devices`.

Use the same `--iec` setting in every capture if IEC correction is part of the test. The `riaa` noise mode shapes the test signal for a phono stage; the analyzer compares the two measured curves, so the common RIAA target cancels.

At 96 kHz the current Welch segment cap gives a frequency-bin spacing of about 0.73 Hz; at 48 kHz it is about 0.37 Hz. The 2 Hz lower limit therefore gives useful resolution around a 10–20 Hz corner. Keep an eye on the ValveBench warnings and the reported coherence. Do not use a capture marked as overloaded.

## Text analysis

```bash
python tools/analyze_subsonic_filter.py \
  measurements/subsonic/bypass.csv \
  measurements/subsonic/filter-10Hz.csv \
  measurements/subsonic/filter-15Hz.csv \
  --json-out measurements/subsonic/report.json
```

The report includes:

- measured attenuation at selected frequencies for L/R;
- measured and fitted −3 dB corner frequencies;
- RMS error of a first-order RC high-pass model;
- coherence-gated point counts and L/R mismatch.

Only points with coherence at or above 0.80 are used for the fit. The default model-fit band is 2–200 Hz. A large model residual means that a single RC pole does not describe the measured network well.

## Interpretation limits

- This is an A/B response comparison, not an absolute calibrated measurement. It is reliable only while the measurement chain and all settings except the filter position remain unchanged.
- Each CSV curve is normalized near 1 kHz. The analysis intentionally removes any 1 kHz level difference. Check insertion loss separately with Gain Match, especially when testing shunt-resistor options that load the SRPP.
- The DUT's actual input and output loads are part of the measured result. The result is specific to the amplifier input impedance and the ADC/attenuator connected during the test.
- The script analyzes ValveBench CSV exports; it does not control the audio hardware or switch the filter.
