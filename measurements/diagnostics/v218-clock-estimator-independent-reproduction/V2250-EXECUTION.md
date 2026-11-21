# v2.25.0 — execution contract: real captured input noise

## Objective

Validate the frozen v2.18.8 est_lsq uncertainty estimate and the v2.24 residual-PSD candidate against **real analog input-noise samples**. No production code is changed.

The capture is noise-only. The 1 kHz pilot is added digitally after capture so its true frequency is known in the captured sample-timebase. This isolates estimator/noise uncertainty. It does not test analog pilot transfer, DAC/ADC clock mismatch, or sampling jitter.

## Hardware safety

- No playback or generated signal is sent to hardware at any point.
- Do not change hardware controls, mixer levels, sample-rate settings, cables, DUT bias/supply, or input termination.
- Do not unplug, short, rewire, or move any analog connection.
- Do not use a microphone or record speech/music/private content. The input must already be a known quiet analog measurement path (for example, an operating DUT output with no test signal).
- If the existing analog path is unknown, carrying content, disconnected, or not safely identifiable, stop after inventory. Do not guess, rewire, or substitute another device.
- Do not terminate or kill another process to free an audio device. If busy, stop and report.
- Capture directly from hardware at native 96 kHz, stereo. Do not use plughw, software resampling, or format conversion.
- No production application settings or project files may be modified.

## Hardware inventory and capture

1. From the normal project checkout on lk, run ./run.sh cli cards first. Also record arecord -l, arecord -L, and uname -a.
2. Identify the currently connected, known quiet two-channel analog input path. Record the stable ALSA card ID and device number, device name, analog path/DUT state as observed, and whether the path was already connected. Do not infer the DUT identity.
3. Check native support with arecord --dump-hw-params for that exact hardware device. Require 96 kHz, 2 channels, and a supported PCM format. Prefer S32_LE; S24_3LE/S24_LE or S16_LE is acceptable if it is what the device natively supports.
4. Check available disk space. Capture **384 seconds** continuously, with no playback, to a WAV under the existing project clone's artifacts/v2250/ directory. Use the stable ALSA card ID, not an ephemeral card index. Example (replace only the device/format with the discovered native values):

   ~~~bash
   arecord -D "hw:CARD=STABLE_ID,DEV=0" -t wav -f S32_LE \
     -r 96000 -c 2 -d 384 \
     "$HOME/ValveBench-compute/ValveBench/artifacts/v2250/real-input-noise.wav"
   ~~~

5. Verify WAV sample rate, channel count, sample format, duration, and peak/clipping. Compute file size and full SHA-256. Preserve the raw WAV on vovans-PC under artifacts/v2250/; do not add it to Git.
6. If the required direct 96 kHz stereo capture cannot be made without altering hardware or using resampling, stop and publish an inventory-only blocked report. Do not substitute a synthetic file.

## Analysis harness

The coordinator-authored script is:

v2250_real_capture_sigma_validation.py

It must be executed unchanged from pinned source SHA ea9f6a7bcda3fc7e316d4a5f091a35325fc9fd9a.

It requires:
- 96 kHz, stereo WAV;
- at least 384 seconds;
- no clipped samples;
- 64 non-overlapping six-second noise blocks.

For each block it uses 2 s, 4 s, and 6 s prefixes. In each window it digitally injects a 1 kHz pilot with true offset −23.5 ppm at two target broadband SNRs (+8 dB and +16 dB), using the same pilot amplitude in L/R. It compares:
- unchanged frozen est_lsq sigma;
- oracle local-PSD sigma from the captured noise samples;
- residual local-PSD sigma after fitting/subtracting the pilot.

The script separates:
- all_fit: every row with a usable fit, including non-OK statuses;
- status_ok_only: rows whose frozen estimator status is exactly ok.

This distinction is mandatory. A line_contaminated, snr_low, grid_edge, or skipped-headroom row must not be silently counted as a production-accepted estimate.

The same six-second source block is reused for its 2 s and 4 s prefixes, and the same noise windows are used for +8/+16 dB. Results across duration/SNR cells are therefore paired, not independent. Within each row pair, L/R are also correlated; never count channels as independent trials.

## Run

From an isolated worktree at the pinned SHA:

~~~bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
export PYTHONPYCACHEPREFIX="$HOME/.cache/valvebench-v2250-pycache"

python3 -m py_compile \
  "$D/v2188_estimators.py" \
  "$D/v2250_real_capture_sigma_validation.py"
~~~

Then run once:

~~~bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
CAPTURE="$HOME/ValveBench-compute/ValveBench/artifacts/v2250/real-input-noise.wav"

/usr/bin/time -v python3 "$D/v2250_real_capture_sigma_validation.py" \
  --wav "$CAPTURE" --output "$D" \
  > "$D/v2250-real-noise-sigma-validation-run.log" 2>&1
rc=$?
echo "EXIT_STATUS=$rc" >> "$D/v2250-real-noise-sigma-validation-run.log"
test "$rc" -eq 0 || exit "$rc"
~~~

Do not rerun a completed test merely because SSH was interrupted. Inspect the complete log, exit marker, JSON, capture hash, and source SHA first.

## Required report

For all six duration × target-SNR cells, report:
- 64 independent non-overlapping source windows, 128 channel rows, fit counts and status counts;
- L/R noise RMS and actual SNR distributions;
- signed bias and sample standard deviation separately for L/R;
- L/R error correlation;
- median/p90/max absolute error and absolute-error/sigma ratios;
- 1σ/2σ/3σ coverage, separately for all_fit and status_ok_only;
- median estimator/oracle/residual sigma and residual/oracle ratios;
- local PSD values and any narrowband line/hum contamination;
- clipping/headroom and skipped rows.

Explicitly state:
- pilot injection is digital and occurs after ADC capture;
- this is a test of the estimator on real captured noise, not of analog signal transfer or clock accuracy;
- the test uses one existing hardware path and does not establish general validity for all DUTs/interfaces;
- no production change was made.

## Publication

Success: commit exactly four files to agent/v2250-real-noise-sigma-validation:
1. v2250-real-noise-sigma-validation.json
2. v2250-real-noise-sigma-validation-run.log
3. V2250-EXECUTION-MANIFEST.md
4. V2250-AGENT-REPORT.md

Do not commit the raw WAV. Record its absolute path on vovans-PC, bytes, full SHA-256, device/path description, exact native capture command and format in the manifest.

If hardware inventory blocks capture, publish only:
- V2250-EXECUTION-MANIFEST.md
- V2250-AGENT-REPORT.md
- v2250-hardware-inventory.log

Do not fabricate JSON or a successful run.

Only allowlisted output files may be staged. No force push, reset, rebase, source edits, production changes, or cleanup of unrelated files. If the result branch moved or push is rejected, stop and report exact state.
