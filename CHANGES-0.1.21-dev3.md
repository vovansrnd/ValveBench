# ValveBench 0.1.21-dev3

## Fixed-layout status indicators

- HeaderBar ADC and GEN toggle buttons keep invariant labels (`АЦП`, `ГЕН`) and no longer switch the `suggested-action` CSS class when toggled. GTK's checked state remains the active-state indicator; the tooltip explains the current state.
- Header status has a fixed width (13 characters), single-line ellipsis and a full-text tooltip. Long error/status strings can no longer claim a wider HeaderBar allocation than short success strings.
- The persistent right-hand health label is single-line and bounded. Its complete diagnostic message remains available as a tooltip, preventing XRUN/transport event strings from changing the meter column's requested height.
- Per-page measurement status labels are single-line/ellipsized with full diagnostic text in tooltips. Full error details remain in the page result text.

This patch addresses the source-level causes of layout reflow in the status/header path. It does not claim to verify the visual result: GTK4/libadwaita is unavailable in the build environment, so a desktop acceptance run remains necessary.

## Explicitly not part of this patch

- Independent DAC and ADC device/sample-rate selection. Current `Window.dev`, `Window.rate`, `AlsaCli.hw`, and `Rig.fs` still represent a duplex endpoint and a shared sample rate.
- Cross-device clock-domain handling/resampling.
- DUT comparison matrix / import of multiple measurement passports.
- Raw ADC waveform-capture export.

These are specified in `docs/AGENT-TASK-0.1.22-EVENING.md` for the next development session.
