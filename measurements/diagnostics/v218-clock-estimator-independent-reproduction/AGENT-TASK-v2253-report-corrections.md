# AGENT TASK — v2.25.3 report/manifest correction

## Goal

Correct the v2.25.3 report and execution manifest using the coordinator review. This is a **documentation-only** follow-up to result commit `79de22ba2ace66d6e888aeead098bf1e8a880fab`.

Read first:
- [COORDINATOR-REVIEW-v2253.md](COORDINATOR-REVIEW-v2253.md)

## Repository and refs

Repository: `vovansrnd/ValveBench`

Coordinator task branch: `coordinator/v2253-report-corrections`

Pinned result commit: `79de22ba2ace66d6e888aeead098bf1e8a880fab`

Result branch: `agent/v2253-report-corrections` (created from the coordinator task branch).

The original analyzer remains pinned at `ae65d7caf41758c9275a3af5c0db68ff3f208dba`. Do not edit or rerun it.

## Allowed changes

Update only these two existing files:
1. `measurements/diagnostics/v218-clock-estimator-independent-reproduction/V2253-AGENT-REPORT.md`
2. `measurements/diagnostics/v218-clock-estimator-independent-reproduction/V2253-EXECUTION-MANIFEST.md`

No other file may be changed or added. Preserve JSON and run log byte-for-byte.

## Required report fixes

Follow every numbered correction in `COORDINATOR-REVIEW-v2253.md`. In particular:

- Replace the incorrect claim that per-window “mean/median” values are 16.2–17.3 dB. Those values are L−R median asymmetry. Give the actual within-channel mean/median ranges and, preferably, the 9-row table in the review.
- Describe the persistent 1000.488 Hz peak in all nine 2-second windows; describe the 984.375 Hz and 1019.531 Hz peaks detected in both channels from 2–4 s through 8–10 s. State the 1.46484375 Hz Welch bin spacing for these windows.
- Correct the detector threshold: `PEAK_MIN_PROMINENCE_DB=6.0`; the script returns up to 20 peaks per channel. Do not label the full top-20 set as “>25 dB”.
- Correct the 1007.9 Hz R-channel entry from the JSON: −133.1573 dBFS²/Hz, prominence 27.7841 dB.
- Keep 14 common / 6 L-only / 6 R-only only when explicitly referring to the full returned top-20 frequency lists.
- Explain that coherence uses the nearest 1.46484375 Hz grid bin; some candidate frequencies map to the same bin. High coherence supports L/R correlation at those bins, but does not prove common physical source, upstream injection, or signal-chain location.
- State that v2.25.3 did not split 968–1032 Hz into sub-bands, so the claim “raised broadband floor across the entire band” is not established. Mark sub-band distribution unresolved.
- Preserve the one-capture/unknown-path limitation and the fact that no production conclusions or hardware action follow.

## Required manifest completion

- Obtain the exact JSON artifact byte count and SHA-256 for the committed JSON at `79de22b`. Use the repository object or a verified checkout; do not alter it.
- Record the `git diff --check` result for the documentation correction commit.
- Add an explicit, truthful attestation: no ALSA/device access, no hardware interaction or configuration changes, no recording, no playback.
- Include original WAV owner/mode only if `/tmp/v2250-probe.wav` is still present and can be checked with metadata-only commands. If unavailable, state that it was unavailable at correction time; do not recreate or recapture.
- Do not claim the analyzer was rerun. It was not.

## Restrictions

- No ALSA, arecord, aplay, mixer/device calls, hardware enumeration, device opens, recording, playback, synthetic signals or new capture.
- Do not access any WAV other than the original `/tmp/v2250-probe.wav`; do not recreate it if missing.
- Do not edit the analyzer, any production code, JSON, or run log.
- Do not rewrite history, force-push, reset, or rebase.
- Work in the existing `lk` clone/worktree; do not use the user's ThinkPad clone.

## Validation and publication

1. Verify repository, branch ancestry, and exact parent/result commit before editing.
2. Edit only the two allowed documents.
3. Run `git diff --check`.
4. Inspect the final diff and confirm exactly the two allowed paths changed.
5. Commit a new documentation-only commit on `agent/v2253-report-corrections` and push normally.
6. Return commit SHA/URL, changed paths, JSON bytes/SHA, diff-check result, and explicit no-hardware/no-capture confirmation.

If any condition cannot be met safely, stop and report the exact blocker. Do not self-repair unrelated problems.
