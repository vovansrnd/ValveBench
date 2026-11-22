# v2.26.1 r1 — coordinator review

## Verdict

**PASS at −24 dBFS.** The physical D10 playback → E-MU 0404 capture run completed,
the required offline gates passed, and the run summary reports
`any_level_passed=true` with `lowest_passing_test_level_dbfs=-24.0`.

The −18 dBFS level is correctly classified **FAIL**, not "almost PASS": one
repeat at R / 94 Hz was rejected by the production `unstable_carrier` gate.

## Verified results

Pinned hardware run: `564a4af239c7c78f7661099656cc80fc06efc9b1`.
Harness HEAD recorded in `run-summary.json`:
`11e6beb1078dc3b717bd1d1d338a23f471b8fe21`.

The run summary confirms:
- D10 playback `hw:1,0`, E-MU capture `hw:2,0`, 192000 Hz;
- clock preflight `ok=true`, L/R frequency mismatch about 0.18 ppm;
- `m24.passed=true`;
- `m18.passed=false`, because of one invalid repeat;
- no fatal events and no clipping in the recorded series.

At −24 dBFS all six corrected ΔG rows pass:
- inject-R: +6.021726 / +6.020339 / +6.021627 dB;
- inject-L: −6.023569 / −6.021001 / −6.020719 dB.

Maximum absolute error against the 6.0205999 dB target is about 0.0030 dB.

## Important correction: worst-repeat C/R margin

The agent report's phrase "C/R oslablened L 31.3–32.6 dB" uses the
channel-level summary field `channels.L.carrier_residual_db`. It is **not**
the lowest individual-repeat C/R.

Production validity is evaluated for each repeat. Reading
`level_m24/inject-L-series.json` down to `channels.L.repeats[*].carrier_residual_db`
shows:

- 94 Hz: minimum 32.382115 dB;
- 994 Hz: minimum 32.092408 dB;
- 9994 Hz: minimum **30.736754 dB** (repeat 2).

Therefore the true worst individual-repeat C/R in the attenuated L channel is
**30.736754 dB**, with only **0.736754 dB** above the 30 dB production limit.
The repeat is valid and the overall −24 dBFS level remains PASS, but this is a
narrower margin than the report's 1.3–2.6 dB implication. Do not use the
channel-level summary as a substitute for the minimum across individual repeats.

This small margin justifies a separate repeatability check before treating
−24 dBFS as a robust operating recommendation.

## −18 dBFS outlier

The invalid repeat is precisely R / 94 Hz / repeat 2:
- reason: `unstable_carrier`;
- `z_burst=5.81231586`;
- `trend_span=0.0000602491 dB`;
- C/R = 56.683577 dB;
- SINAD = 56.6480717 dB;
- corrected ΔG for the row = +6.021301 dB.

This is not a C/R failure or a numerical ΔG failure. The row is invalid because
the envelope stability gate failed. The other two R repeats at 94 Hz are valid.
The higher drive level remains FAIL under the unchanged production criteria.

## Capture metadata NaNs

The two preflight captures contain NaN only in the scalar NPZ metadata field
`generator_level_dbfs`. This is not an automatic `None → NaN` serializer
conversion: the pinned harness explicitly writes `np.nan` when
`state["level_dbfs"] is None` during clock preflight. The audio arrays
`submitted_to_backend` and `capture` are reported as finite (0 NaN / 0 Inf
across all 8 capture files).

This does not invalidate the audio measurement, but future harness revisions
should preferably represent "not applicable" with an explicit applicability
flag rather than a NaN sentinel. Do not rewrite this completed run's evidence.

The agent reports 18/18 manifest entries independently verified. The committed
SHA256 manifest contains 18 file entries: 8 capture NPZ files plus 10 JSON
files. The agent report says "all 11 captures"; that count does not match the
committed manifest, which contains **8** capture NPZs. The finite-array claim
should therefore be read as applying to those 8 files. The binary capture
payloads were not recomputed by this coordinator review.

## Next step

Run v2.26.2 as a planned three-session repeatability check using the exact
v2.26.1 hardware harness and unchanged production gates. Primary question:
does −24 dBFS pass all three independent sessions, and what is the minimum
individual-repeat C/R margin? Also record whether the isolated −18 dBFS
envelope outlier recurs. See `V2262-EXECUTION.md`.
