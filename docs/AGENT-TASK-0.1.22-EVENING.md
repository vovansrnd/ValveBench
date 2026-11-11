# Agent handoff — ValveBench next development session (based on 0.1.21-dev3)

## Current baseline

Use `ValveBench-0.1.21-dev3` supplied with this task. It is based on accepted `0.1.21-dev2` and includes the status/header layout stabilization patch. Do not start from old 0.1.20 or 0.1.21-dev1 sources.

## P0 — prove/fix HeaderBar and status layout jitter

The user reports rapid layout jumps when carrier/stream-drop/XRUN diagnostics produce longer status text. The likely source-level causes found in dev2:

- `Window.status` allowed 12..18 characters (`set_width_chars(12)`, `set_max_width_chars(18)`) while its contents vary (`Измерение…`, `Ошибка измерения`, `Готово · …`).
- `Window.start_live/stop_live` changed ADC label (`АЦП` vs `АЦП ●`) and toggled `suggested-action` on the same fixed-size header button.
- `LivePage.sync_generator_button` changed GEN label (`ГЕН` vs `ГЕН ●`) and toggled `suggested-action`, whose theme padding/border may alter intrinsic size.
- `MeterStrip.health` allowed wrapped text of variable height and variable text length; `refresh_health` emits dynamic event explanations.

Dev3 changes these to fixed labels/style, bounded one-line status, ellipsis and tooltips. Verify in actual GTK4/libadwaita on the user's Wayland desktop. Test repeated conditions while running live capture:
1. steady carrier / stable stream;
2. deliberate generator mute / carrier loss;
3. short ADC overrun / DAC underrun or simulated XRUN;
4. device disconnect/reconnect;
5. rapid alternating health states.

Record a desktop video/screenshot and compare window/header/control allocations. Do not resize the buttons larger. Header control positions and content minimum width must remain stable. If the user still sees jumps, instrument GTK allocations (`notify::width`, `notify::height`, size-allocate/layout snapshots where supported) for HeaderBar, status label, `dev`, `rate`, `live_btn`, `generator_btn`, meter health label and ToastOverlay. Do not guess at toast as cause: ToastOverlay is an overlay; prove which widget allocation changes. Avoid global pkill and never allow test UI to escape to the user's desktop.

## P1 — separate DAC and ADC selection (major architecture task)

Current implementation is duplex-only:
- `Window.__init__`: `self.cards = [sim] + list_duplex_devices()`, a single `self.dev`, a single `self.rate`.
- `backend.list_pcm_devices()` already enumerates separate PCM directions; `list_duplex_devices()` intersects playback/capture and therefore hides separate devices.
- `AlsaCli.__init__(hw=...)` holds a single `self.hw`; `_cmd()` always uses it for both aplay and arecord.
- `Rig.fs` is one shared rate used for playback generation and ADC capture/analysis.
- Calibration and mixer identity currently scope both sides to one combined device.

Implement two explicit endpoint selectors:
- DAC/playback device + PCM and sample rate;
- ADC/capture device + PCM and sample rate.

Extend backend to `hw_out` and `hw_in` with backward-compatible `hw` alias during migration. `_cmd(aplay)` must use output PCM; `_cmd(arecord)` input PCM. Separate formats, channel counts and channel maps. Persist stable physical identity and ALSA PCM for each side; never persist volatile card numbers as identity. Use exact supported rates from the selected endpoint; do not silently fall back to a wrong rate. Device hotplug must invalidate/re-resolve profiles.

Critical clock-domain distinction:
- Generator/scope-only mode must support e.g. DAC at 384 kHz and ADC at 192 kHz, each with its own true timebase. Do not label DAC buffer points with ADC Fs or vice versa.
- The existing measurement suite assumes one `Rig.fs` and coherent shared rate. Do NOT simply change `Rig.fs` to one side's rate and proceed. For driven measurements, initially require a common supported sample rate and a successful DAC↔ADC clock check; clearly show/record the measured ppm. If separate clocks remain, either implement validated asynchronous sample-rate/timebase correction or reject precision measurements with a clear reason. Never pretend independent USB clocks are synchronized.
- Scope capture uses ADC's rate. Generated preview uses DAC's rate. For waveform frequency/phase comparison, estimate source frequency from capture or apply calibrated clock ratio; do not compare arrays by sample index without alignment.

Test with simulated endpoint pairs and mocks, plus two real USB devices when available. Verify no regression for current same-card E-MU duplex configuration.

## P2 — raw waveform snapshot/export

Add a `СОХРАНИТЬ ОСЦИЛЛОГРАММУ` action on Generator/Scope page. Capture a stable ADC window (not an interpolated graph trace) and write a portable ZIP with raw CSV samples, optional generated DAC reference samples, metadata JSON and manifest SHA-256. The required fields and analysis ideas are in `docs/SQUARE-WAVE-MEASUREMENT.md`.

Do not export only pixels, rounded display values or decimated plot data as the measurement source. Any display decimation must be separate from the raw capture array. Preserve L and R, exact Fs, sample count, device stable IDs, generator level/frequency/waveform, trigger position, DAC/ADC settings, calibration IDs, routing, Anti-RIAA presence and any XRUN/sample-gap intervals. Do not call a capture valid if it crossed a transport event.

## P3 — multi-device comparison matrix (RMAA-inspired, not a clone)

Add Library/Measurements view to import exported DUT session ZIP/JSON packages and compare selected devices in a metric matrix:
- columns = DUT identifiers (e.g. 590, 591, 592);
- rows = Gain L/R and ΔG, response deviation, THD, THD+N, noise A/C and ASD, IMD products, crosstalk, optional transient metrics;
- each row has a chart action.

Chart semantics:
- one DUT selected: show L and R separately;
- multiple DUTs selected: show one stereo summary per DUT (compute mean channel response in linear amplitude/power space, not by naively averaging dB), plus optional L/R spread/band.
- Preserve exact per-DUT numeric results in table cells; never replace them with a chart-only summary.
- Validate test comparability before overlay: sample rates, generator levels/frequencies, RIAA/IEC profile, input load, Anti-RIAA, DAC/ADC identities, calibration profiles, mixer settings and software/schema versions. Mark mismatch and prevent misleading overlays or require an explicit acknowledgement.
- Keep original source packages immutable. Imports must not overwrite or silently merge records with the same DUT ID. Give every session a globally unique ID and retain measurement timestamps.

## P4 — square-wave method and evaluation

Read `docs/SQUARE-WAVE-MEASUREMENT.md`. Initial standard visual check: 1 kHz band-limited square, conservative peak level (start -20 dBFS for source loopback, lower as needed after measured DUT gain), save matching loopback and DUT captures through the same cables/ADC settings. Through physical Anti-RIAA the square spectrum is shaped before the DUT; document this, and don't expect a square if Anti-RIAA isn't present. 100 Hz is a secondary LF check; 10 kHz is not a good general-purpose 20 kHz-band transient test.

The current `make_generator_signal()` uses a finite Fourier series, capped at 99 harmonics and <0.45 Fs. That is an anti-aliased band-limited square, not an ideal edge. Evaluate whether configurable harmonic ceiling / band limit is worthwhile only with the DAC/ADC bandwidth and filters documented.

## Constraints and hygiene

- No changes to user's installed copy `/opt/MyApps/Adudio/ValveBench` without explicit permission.
- No UI tests on the real desktop unless user explicitly agrees. Use isolated Xvfb, private DBus and private XDG_RUNTIME_DIR for automation.
- Do not use global `pkill`; address only child PIDs/process groups created by the test.
- Do not claim a physical/audio test passed without hardware.
- Retain all valid data when one batch subtest fails. Avoid overwriting previous DUT sessions.
- Update changelog, tests and a report. Separate source-level tests from GTK GUI acceptance and real-hardware acceptance.
