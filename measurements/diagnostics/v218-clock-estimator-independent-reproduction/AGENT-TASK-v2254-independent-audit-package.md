# AGENT TASK — independent audit package for v2.25.3–v2.25.4

## Purpose
Prepare one self-contained offline evidence archive for the independent auditor. The auditor has **no GitHub/Git access and no hardware**. The user will download this ZIP and provide it directly to the auditor.

This is a packaging task only. Do not perform new analysis, alter source/results, or access hardware.

## Required evidence
Collect the exact coordinator-authored analyzer sources and execution evidence for:
- v2.25.3 pilot-band noise characterization (pinned analyzer SHA `ae65d7caf41758c9275a3af5c0db68ff3f208dba`);
- v2.25.4 eight-sub-band distribution (pinned analyzer SHA `a10e88f8c47751a2b12f504372ba8ee4005a8968`).

Include, preserving original relative paths where practical:
1. Both analyzer source files at the exact pinned revisions.
2. Both raw JSON result files.
3. Both complete run logs, including command, exit status and resource measurement sections.
4. Both execution manifests.
5. Both final agent reports, including the corrected v2.25.3 and v2.25.4 reports.
6. Both coordinator review documents and the execution contracts/task documents that define the pinned runs.
7. A new `README-FOR-AUDITOR.md` describing the project purpose, test question, exact source WAV identity, what each stage measures, known limitations, and the audit questions below.
8. A `SHA256SUMS` covering every file in the archive except the checksum file itself.

Do not include the WAV itself: it is a 5.76 MB temporary recording of an unknown physical input path and is not part of this audit request. Clearly state that the auditor cannot independently reproduce the capture and must not infer its source.

## Audit questions for README
Ask the auditor to independently assess:
- whether each analyzer implements the stated PSD/sub-band/window calculations correctly;
- whether the JSON values and report tables/conclusions are internally consistent with the source and logs;
- whether the v2.25.3 peak detection/coherence interpretation is technically justified and appropriately qualified;
- whether v2.25.4's eight-sub-band result supports the stated claim that the L/R median PSD offset is present across 968–1032 Hz in this one recording;
- whether any statistical language overstates what can be inferred from 9 overlapping windows and 16-bin sub-band estimates;
- whether the corrected reports still contain factual, mathematical, methodological, provenance, or interpretation errors;
- what additional controlled measurements would be required before attributing the approximately 17 dB asymmetry to any part of the physical audio chain.

The auditor must distinguish code correctness, numerical consistency, and physical interpretation. No hardware diagnosis is possible from this archive.

## Packaging and publication
- Work on `lk`, in the existing ValveBench clone.
- Create a dedicated directory under `measurements/diagnostics/v218-clock-estimator-independent-reproduction/audit-v2253-v2254/`.
- Build a ZIP containing the complete package, plus a sidecar `.sha256` for the ZIP.
- Keep the ZIP and sidecar in the repository directory above and commit/push them to a new agent branch `agent/v2254-independent-audit-package`, based on this task branch.
- The auditor must be able to open the ZIP without Git, network access, or external files.
- Verify archive listing, extract it to a fresh temporary directory, verify every SHA256SUMS entry, and confirm README and all required evidence files are present.
- Record exact archive size, ZIP SHA-256, sidecar content, commit SHA, and changed paths in a short publication report.

## Strict prohibitions
- No ALSA, hardware enumeration, device access, capture, playback, cable changes, or recreation/access of the temporary WAV.
- No analyzer execution and no changes to analyzer source, JSON, logs, manifests, or reports.
- No production code/configuration changes.
- Do not silently substitute different revisions. If any required pinned source or artifact cannot be found, stop and report BLOCKED with exact missing path/ref; do not guess.
- No force-push, reset, rebase, or unrelated file changes.

## Completion
Return the ZIP download URL, sidecar URL, SHA-256, commit SHA, exact changed paths, and the verification results. Stop after publication; wait for coordinator review.
