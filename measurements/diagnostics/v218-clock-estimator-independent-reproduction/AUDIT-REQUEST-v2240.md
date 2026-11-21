# Independent audit request — v2.24.0 uncertainty calibration

## Scope

Audit only the v2.24.0 synthetic experiment and its conclusions. No hardware access is required or available to the auditor. Do not edit code or production files.

Repository: vovansrnd/ValveBench

Execution commit:
6ff2c8b1f646bb91fd11f94f6e628d0fb6bb91be

Relevant files:
- v2240_estimator_sigma_coverage.py on coordinator/v2240-estimator-sigma-coverage at pinned source ea5656280f385b172d60bffe750ff31b76f7a016;
- frozen v2188_estimators.py at the same pinned source;
- v2240-estimator-sigma-coverage.json, V2240-AGENT-REPORT.md, V2240-EXECUTION-MANIFEST.md and run log on agent/v2240-estimator-sigma-coverage.

## Questions requiring independent answers

1. **Residual-PSD circularity / evidential strength**
   - Oracle PSD is estimated from the injected noise array.
   - Residual PSD is estimated from the same noise realization after adding a digitally generated pilot, fitting that pilot, and subtracting the fit.
   - The reported median residual/oracle sigma ratio is 0.99989–1.00001.
   - Is this agreement an independent validation of the residual-PSD estimator, or is it largely expected because both PSDs come from the same realization and the fitted sinusoid occupies a narrow spectral region?
   - What evidence is still missing before calling residual-PSD operationally credible?

2. **Coverage population correctness**
   - Inspect summarize_cell in the harness. It defines valid rows using fit_error_ppm != None, then computes coverage without filtering estimator_status.
   - The report states that two line_contaminated rows in 2s colored were excluded from coverage, while it also says valid_fit_rows=256 and the rows had valid fits.
   - Verify exactly which rows enter each reported coverage number. State whether the report is factually wrong and whether all-fit and status-ok-only coverage must be reported separately.

3. **Statistical independence and confidence**
   - There are 128 independent noise pairs per cell and two correlated L/R channel records per pair (reported r≈0.94–0.97).
   - Evaluate the effective sample size and uncertainty of the 1σ/2σ/3σ coverage estimates. Do not treat 256 channel rows as independent.
   - In particular, assess the low 2s-white oracle coverage (1σ≈0.578, 2σ≈0.926) versus nominal Gaussian coverage.

4. **PSD estimator and CRLB assumptions**
   - Assess the one-sided Welch PSD with nperseg=65536, 50% overlap, median across offsets 2–32 Hz around the pilot.
   - Assess use of the local-PSD CRLB formula for the one-pole 1400 Hz low-pass colored-noise family.
   - Identify possible bias from spectral leakage, finite Welch degrees of freedom, local spectral slope, narrowband components, or using a single local median as N0.

5. **Noise generator and pairing**
   - Check whether make_noise_pair really produces the stated total RMS and L/R correlation after filtering/scaling.
   - Check whether the six cells and the per-trial rows are described without overstating independence.
   - Check the estimator sigma calculation and residual fitting logic for mismatches.

6. **Conclusion**
   - Separate verified findings, report corrections, unresolved limitations, and the smallest next experiment needed before considering any production change.
   - Do not recommend a production patch based on the synthetic result alone.

## Required auditor output

Return a concise but technically complete audit with:
- confirmed issues and exact code/data evidence;
- corrected interpretation of coverage and residual/oracle agreement;
- any calculation errors;
- specific additional tests required;
- explicit statement that no production change is justified yet.

Do not modify the repository. The coordinator will apply any accepted corrections and design the next experiment.
