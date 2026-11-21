# AGENT TASK — Build an offline auditor ZIP for ValveBench v2.24.0

## Purpose

The independent auditor has NO GitHub/Git access. The coordinator will download one ZIP and upload it to the auditor. Your job is to assemble and publish that ZIP. Do not ask the auditor to follow Git links, clone a repository, or fetch separate files.

This is a packaging task only. Do not execute tests, access audio hardware, make recordings, or edit source/production files.

## Source of truth

Repository: vovansrnd/ValveBench

Frozen test source commit:
ea5656280f385b172d60bffe750ff31b76f7a016

Execution result commit:
6ff2c8b1f646bb91fd11f94f6e628d0fb6bb91be

Task branch:
coordinator/v2240-auditor-bundle

Result branch:
agent/v2240-auditor-bundle

Use exact files from the refs/commits above, not moving branch heads and not local uncommitted copies.

## Required ZIP layout

Create:

measurements/diagnostics/v218-clock-estimator-independent-reproduction/V2240-AUDIT-BUNDLE.zip

The ZIP must contain this self-contained folder structure:

~~~text
V2240-AUDIT-BUNDLE/
  README-FOR-AUDITOR.md
  AUDIT-REQUEST-v2240.md
  source/
    v2240_estimator_sigma_coverage.py
    v2188_estimators.py
  results/
    v2240-estimator-sigma-coverage.json
    v2240-estimator-sigma-coverage-run.log
    V2240-AGENT-REPORT.md
    V2240-EXECUTION-MANIFEST.md
  SHA256SUMS.txt
~~~

Source files:
- source/v2240_estimator_sigma_coverage.py from frozen commit ea5656280f385b172d60bffe750ff31b76f7a016.
- source/v2188_estimators.py from the same frozen commit.

Results:
- four files from execution commit 6ff2c8b1f646bb91fd11f94f6e628d0fb6bb91be.

Audit request:
- Exact contents of AUDIT-REQUEST-v2240.md from coordinator/v2250-real-noise-sigma-validation at commit bc0078d1f4a207a67c75b9f4c894618a274fbe7e.

## README-FOR-AUDITOR.md content

Write in Russian. Explain:
- The auditor has received a complete offline package and does not need Git or internet access.
- Audit scope is v2.24.0 synthetic uncertainty calibration.
- The frozen estimator and harness are coordinator-authored source snapshots; their provenance SHAs are listed in this README.
- JSON contains raw per-trial rows; run log, report and execution manifest are included.
- Please inspect actual code/data and independently verify claims; do not rely only on the report.
- The key questions are fully described in AUDIT-REQUEST-v2240.md.
- Auditor has no hardware access; this task requires no hardware.
- Do not modify source, produce patches or change production. Return a written technical audit with findings, corrected interpretation, unresolved limitations and smallest next experiment.
- Distinguish verified facts, report errors, hypotheses and recommendations.
- In particular, check the two line_contaminated rows and coverage population; evaluate whether residual-PSD/oracle agreement is circular; assess paired L/R effective sample size and Welch/CRLB assumptions.
- Include the frozen source SHA and result commit SHA above.

## SHA256SUMS and validation

1. Compute SHA-256 for each of the eight payload files (README, audit request, two source files, two result data files, report, manifest). Include each entry in SHA256SUMS.txt.
2. Also record the ZIP SHA-256 in a sibling text file:
   V2240-AUDIT-BUNDLE.zip.sha256
3. Verify:
   - unzip -t succeeds;
   - extraction into a new empty directory succeeds;
   - every checksum validates;
   - JSON parses and has 1536 per_trial rows;
   - exact source/result provenance recorded in README;
   - no files are missing or extra.
4. Ensure the archive does not include the v2.25 probe WAV, credentials, unrelated project files, local paths or any private/user data.

## Git publication

Create/fetch result branch agent/v2240-auditor-bundle from the exact task/source branch history without rewriting history. Stage and commit only:
1. V2240-AUDIT-BUNDLE.zip
2. V2240-AUDIT-BUNDLE.zip.sha256

No source, production, test, or documentation files outside the ZIP may be committed. Do not force push, reset, or rebase. Verify the published branch head and files after push.

## Return to coordinator

Return:
- commit SHA and GitHub commit URL;
- exact downloadable browser URL for the ZIP (not a Git clone URL or ordinary file page);
- exact downloadable URL for the .sha256 sidecar;
- archive byte size and full SHA-256;
- ZIP member listing;
- validation outcomes.

Do not tell the coordinator to pass GitHub URLs to the auditor. The coordinator will download the ZIP and upload that one file to the auditor.
