# AGENT TASK — ValveBench v2.25.0
## Real hardware input-noise validation of frequency-estimator uncertainty

**Role:** execute the coordinator-authored capture/analysis package on lk, with access to the physical audio inputs. Preserve evidence and report. Do not redesign the harness or make production changes.

## 1. Repository and pinned source

Repository: https://github.com/vovansrnd/ValveBench

Task branch: coordinator/v2250-real-noise-sigma-validation

Result branch: agent/v2250-real-noise-sigma-validation

Pinned source SHA (the only permitted script/estimator checkout):

ea9f6a7bcda3fc7e316d4a5f091a35325fc9fd9a

Coordinator-authored harness:
measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2250_real_capture_sigma_validation.py

Frozen estimator:
measurements/diagnostics/v218-clock-estimator-independent-reproduction/v2188_estimators.py

Read this task and V2250-EXECUTION.md from the task branch before doing anything. Read-only hardware inventory comes first. The script and frozen estimator must be executed from the exact pinned SHA.

## 2. Machine

Use the established host lk and existing clone:

~/ValveBench-compute/ValveBench

Do not use the user's ThinkPad clone. Create an isolated detached worktree at the pinned SHA. Check whether the proposed worktree path exists before creating it; never delete or overwrite an unknown directory. Set PYTHONPYCACHEPREFIX outside the worktree.

## 3. Scope and safety — mandatory

This test deliberately uses physical capture but **NO PLAYBACK**.

Forbidden:
- playback or sending a generated signal to any audio output;
- changing mixer/gain controls, device rates, analog cabling, input termination, DUT bias/supply or operating point;
- unplugging, shorting, rewiring, or moving any connection;
- changing project configuration or any file under valvebench/;
- editing the coordinator script or frozen estimator;
- terminating other processes to release a device;
- committing raw WAV files or unrelated files.

Do not record microphone input, speech, music, or private content. The only permissible capture is a known quiet analog measurement path already connected before this task (for example, an operating DUT output with no test signal). Do not guess the DUT identity or input wiring.

**Stop after inventory** if the current path is unknown, not quiet, carrying content, disconnected, or cannot be captured natively at 96 kHz stereo without hardware changes. Also stop if the device is busy. Do not substitute a different device, plughw, or software resampling. Publish the blocked inventory report as specified.

## 4. Hardware inventory — before capture

From the normal project clone on lk:
1. Run ./run.sh cli cards first.
2. Record the complete output of arecord -l, arecord -L, uname -a, and /etc/os-release.
3. Identify only an already-connected two-channel analog input path known to be quiet. Record the stable ALSA card ID and device number, advertised device name, existing analog connection/DUT operating state as observed, and whether the connection was already present. Do not infer details.
4. Use arecord --dump-hw-params against that exact device. Require native 96000 Hz, 2 channels, and a supported PCM format. Prefer S32_LE; use S24_3LE/S24_LE or S16_LE only if that is the device's native supported format.
5. Verify available disk space. Capture only if the path meets every condition in V2250-EXECUTION.md.

Do not use ephemeral ALSA card numbers as the persistent identifier. Use the stable card ID in the capture command.

## 5. Capture

Create the output directory under the existing project clone:

~/ValveBench-compute/ValveBench/artifacts/v2250/

Capture 384 seconds continuously with no playback to:

artifacts/v2250/real-input-noise.wav

Use a direct hardware device (hw:CARD=<stable-id>,DEV=<n>) and native format. Example only; replace the stable ID/device/format with the exact values verified above:

~~~bash
arecord -D "hw:CARD=STABLE_ID,DEV=0" -t wav -f S32_LE \
  -r 96000 -c 2 -d 384 \
  "$HOME/ValveBench-compute/ValveBench/artifacts/v2250/real-input-noise.wav"
~~~

Do not use plughw or a rate/format conversion. Do not change device or DUT controls. If any other process owns the device, stop and report rather than killing it.

Verify the capture: WAV sample rate, stereo channel count, format, duration, peak and clipping. Compute byte size and full SHA-256. Keep the raw WAV on vovans-PC outside the Git worktree; do not push it. Keep it until the coordinator explicitly releases it.

## 6. Run coordinator harness unchanged

Use the isolated worktree at pinned SHA ea9f6a7bcda3fc7e316d4a5f091a35325fc9fd9a. Verify exact HEAD and clean tracked tree. Compile frozen estimator and harness:

~~~bash
D=measurements/diagnostics/v218-clock-estimator-independent-reproduction
export PYTHONPYCACHEPREFIX="$HOME/.cache/valvebench-v2250-pycache"
python3 -m py_compile \
  "$D/v2188_estimators.py" \
  "$D/v2250_real_capture_sigma_validation.py"
~~~

Run once:

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

Stop on compile/runtime failure. Preserve the complete log and traceback. Do not fix or rerun the harness. Inspect the existing log and artifacts before considering any rerun.

## 7. Output validation and analysis

Successful harness output must contain:
- exactly 768 per_trial rows;
- six cells: 2/4/6 seconds × target broadband SNR +8/+16 dB;
- 64 non-overlapping six-second source blocks per cell;
- 128 channel rows per cell, both L and R for every trial;
- finite numeric values for every available fit;
- explicit status counts, including skipped_headroom;
- JSON size and full SHA-256.

The report must use the JSON summary_by_cell values as canonical. Do not silently recompute or change the statistical method.

For every cell, report all_fit and status_ok_only separately:
- pair/channel/fit counts and status counts;
- L/R noise RMS and actual SNR;
- signed bias and sample standard deviation for L/R;
- L/R error correlation;
- median/p90/max abs error and abs_error/sigma;
- coverage at 1σ/2σ/3σ for estimator, oracle-PSD and residual-PSD sigma;
- median estimator/oracle/residual sigma and residual/oracle ratio;
- local PSD values and any narrowband line/hum contamination;
- clipping/headroom and skipped rows.

Important interpretation constraints:
- The pilot is injected digitally after the ADC capture. The test validates estimation on real captured noise, but does not validate analog pilot transfer, DAC/ADC clock mismatch or sampling jitter.
- The same captured six-second source block is used for 2s/4s prefixes and for both SNR settings. These cells are paired, not independent.
- L/R rows are paired and correlated. Never count the two channels as independent trials.
- all_fit includes usable fits with non-OK statuses. status_ok_only includes only status exactly ok. A line_contaminated, snr_low, grid_edge or skipped-headroom row is not a production-accepted result.
- Do not claim general validity beyond this one observed hardware path.
- Do not propose or implement a production sigma replacement in this task.

## 8. Publication

On success, commit exactly these four output files to agent/v2250-real-noise-sigma-validation:
1. v2250-real-noise-sigma-validation.json
2. v2250-real-noise-sigma-validation-run.log
3. V2250-EXECUTION-MANIFEST.md
4. V2250-AGENT-REPORT.md

The manifest must include:
- exact pinned SHA and worktree HEAD;
- host, CPU, full uname and os-release;
- Python/NumPy/SciPy/Git versions;
- full hardware inventory outputs or paths to preserved full logs;
- stable ALSA card ID/device, device name, existing analog path/DUT state as observed;
- exact native capture command, format, sample rate, channels, duration;
- raw capture absolute path, bytes and full SHA-256;
- exact compile/run commands, exit codes, time and max RSS;
- JSON byte size and full SHA-256;
- git diff --check;
- explicit no-playback and no-hardware-control-change confirmation;
- confirmation that production and frozen sources were not edited.

If capture is blocked at inventory, commit only:
- V2250-EXECUTION-MANIFEST.md
- V2250-AGENT-REPORT.md
- v2250-hardware-inventory.log

Do not create a successful JSON or claim a completed experiment in the blocked case.

Before push, verify only the allowlisted files are staged. Confirm the pinned SHA is an ancestor, result branch ancestry is safe, and remote branch has not moved. Push normally; never force. After push verify remote HEAD and return commit SHA, URL, report link and concise outcome.

## 9. Definition of done

Either:
- a successful, validated real-noise analysis with raw capture preserved on vovans-PC and four allowlisted output files published; or
- an honest inventory-only blocked report with the exact blocker.

No playback, no hardware changes, no production changes, no estimator changes, and no fabricated data.
