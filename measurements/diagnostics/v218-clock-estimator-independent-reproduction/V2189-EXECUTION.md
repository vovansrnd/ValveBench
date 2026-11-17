# v2.18.9 — execution package (coordinator-authored)

This branch contains the coordinator-authored test harness and a frozen copy
of the v2.18.8 diagnostic estimator. The implementation and test conditions
are already defined here.

## Required actions on vovans-PC

1. Fetch this branch in the existing clone via the established `ssh lk`
   workflow. Do not rsync WAVs. The tests use deterministic synthetic data plus the compact WAV-derived line-profile JSON already included in this branch; its source WAV hashes remain in the JSON.
2. Run from the repository root:

```bash
python3 measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2189_paired_snr_clockcheck.py
python3 measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2189_nearby_offbin_sweep.py
```

3. Capture complete stdout/stderr and runtime for each command.
4. Commit only the generated JSON outputs and a short execution manifest
   containing host CPU, OS, Python, NumPy, SciPy, branch HEAD and command
   exit status.

## Do not

- Do not edit either Python harness or the frozen `v2188_estimators.py`.
- Do not change thresholds, line exclusions, production source, main,
  PR #4, pilot frequency or clock gate.
- Do not run playback, physical hardware or DUT tests.
- Do not reinterpret or regenerate the expected metrics. If a script fails,
  return the full traceback/log and stop; the coordinator will patch it.

## Output contract

- `v2189-paired-snr-clockcheck.json`: paired metrics for identical samples,
  analyzed with the production-standard two one-second block layout and
  the diagnostic LSQ estimator.
- `v2189-nearby-offbin.json`: positive and negative offsets, off-bin cases,
  separate phase modes, quiet controls, and AM/FM sideband controls.

The SNR threshold remains diagnostic-only. The nearby probe remains an
informational residual spectral-component detector; it is not a refusal gate.
