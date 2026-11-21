# AGENT TASK — ValveBench v2.25.4

Read `measurements/diagnostics/v218-clock-estimator-independent-reproduction/V2254-EXECUTION.md` first and follow it exactly.

Task branch: `coordinator/v2254-pilotband-subband-distribution`

Pinned analyzer SHA: `33bb041312d77ddc04dbc36edb805d91f67556f5`

Analyzer: `measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2254_pilotband_subband_distribution.py`

Run the coordinator-authored analyzer on `lk`, using only the existing `/tmp/v2250-probe.wav` with the exact required SHA. It splits 968–1032 Hz into eight 8-Hz sub-bands and analyzes the full 10-second record plus nine overlapping 2-second windows.

**No ALSA, no device or hardware access, no recording, no playback, no cabling/configuration changes, no synthetic input. Do not recreate the WAV if missing or mismatched. Do not edit the script. Stop on any error and preserve diagnostics.**

Publish the exact success/blocked allowlist specified in `V2254-EXECUTION.md`, including JSON, full run log, manifest and report on success. Return commit SHA/URL and key L−R sub-band median differences.