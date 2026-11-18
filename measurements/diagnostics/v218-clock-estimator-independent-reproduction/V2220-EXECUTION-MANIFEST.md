# V2220 execution manifest

- Task: `v2220`
- Exact checked-out HEAD: `5d5e9d1eaee6cc9e0b427e5f6e1a5d738b1a3a78`
- Source branch: `coordinator/v2220-clock-quality-validation`
- Result branch: `agent/v2220-coordinator-authored-validation`

## Environment

- Host: `vovans-PC`
- OS: `Linux-7.0.0-43-generic-x86_64-with-glibc2.43`
- Kernel: `7.0.0-43-generic`
- Machine: `x86_64`
- Processor: ``
- Python: `3.14.4 (main, Aug 20 2026, 10:41:58) [GCC 15.2.0]`
- NumPy: `2.3.5`
- SciPy: `1.16.3`

```text
Architecture:                            x86_64
CPU op-mode(s):                          32-bit, 64-bit
Address sizes:                           43 bits physical, 48 bits virtual
Byte Order:                              Little Endian
CPU(s):                                  12
On-line CPU(s) list:                     0-11
Vendor ID:                               AuthenticAMD
Model name:                              AMD Ryzen 5 3600 6-Core Processor
CPU family:                              23
Model:                                   113
Thread(s) per core:                      2
Core(s) per socket:                      6
Socket(s):                               1
Stepping:                                0
Frequency boost:                         enabled
CPU(s) scaling MHz:                      85%
CPU max MHz:                             4208,4512
CPU min MHz:                             557,7470
```

## Preflight

```text
python -m py_compile measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2220_clock_occupancy_diagnostics.py measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2220_lineqc_physical_width.py measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2220_nearby_duration_robustness.py measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2188_estimators.py
exit_code=0
```

## Execution

| Script | Exit | Wall time | Maximum RSS (kB) | Result JSON |
|---|---:|---|---:|---|
| `v2220_clock_occupancy_diagnostics.py` | 0 | mm:ss or m:ss): 0:03.85 | 260732 | `` |
| `v2220_lineqc_physical_width.py` | 0 | mm:ss or m:ss): 7:47.66 | 126888 | `` |
| `v2220_nearby_duration_robustness.py` | 0 | mm:ss or m:ss): 20:26.54 | 191716 | `` |

## Integrity checks

- `git diff --check`: PASS
- Frozen estimator modified: NO
- Coordinator-authored test scripts modified: NO
- Production files modified: NO
- Hardware, playback, DUT, audio capture or WAV operations: NONE
- Results retain all JSON `per_case` / `per_trial` rows.

## Files

- `v2220-clock-occupancy-diagnostics.json`
- `v2220-clock-occupancy-diagnostics-run.log`
- `v2220-lineqc-physical-width.json`
- `v2220-lineqc-physical-width-run.log`
- `v2220-nearby-duration-robustness.json`
- `v2220-nearby-duration-robustness-run.log`
- `V2220-EXECUTION-MANIFEST.md`
