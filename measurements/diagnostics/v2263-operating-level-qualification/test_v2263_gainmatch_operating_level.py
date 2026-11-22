#!/usr/bin/env python3
"""Offline tests for v2.26.3 operating-level harness."""
from __future__ import annotations

import unittest

import numpy as np

import v2261_gainmatch_level_margin as harness


def valid_series(delta: float | None = None) -> dict:
    rows = []
    for f in harness.FREQUENCIES:
        rows.append({
            "frequency_hz": f,
            "delta_adc_db": 0.0 if delta is None else delta,
            "loopback_correction_db": 0.0 if delta is not None else None,
            "delta_gain_db": delta,
            "delta_output_spread_db": 0.01,
            "valid": True,
            "repeat_stable": True,
            "channels": {
                ch: {
                    "valid": True,
                    "repeats": [{
                        "valid": True, "repeat": i, "carrier_residual_db": 44.0,
                        "sinad_db": 43.9, "peak_dbfs": -40.0,
                        "validity_reasons": [], "envelope": {"z_burst": 1.0}
                    } for i in (1, 2, 3)]
                } for ch in ("L", "R")
            }
        })
    return {"events": {"fatal": []}, "rows": rows,
            "mixer_fingerprint": {"dac": "d", "adc": "a"}}


class LevelContractTests(unittest.TestCase):
    def test_single_candidate_level_and_production_gates_are_pinned(self):
        self.assertEqual(harness.LEVELS_DBFS, (-20.0,))
        self.assertEqual(harness.FREQUENCIES, (94.0, 994.0, 9994.0))
        self.assertEqual(harness.REPEATS, 3)
        self.assertEqual(harness.MIN_CARRIER_RESIDUAL_DB, 30.0)
        self.assertEqual(harness.RESULT_TOLERANCE_DB, 0.20)

    def test_series_metrics_keep_cr_sinad_and_headroom_separate(self):
        result = valid_series(None)
        metrics = harness._series_metrics(result)
        self.assertEqual(metrics["min_carrier_residual_db"], 44.0)
        self.assertEqual(metrics["min_sinad_db"], 43.9)
        self.assertEqual(metrics["max_peak_dbfs"], -40.0)
        self.assertEqual(metrics["min_peak_headroom_db"], 40.0)
        self.assertEqual(metrics["invalid_repeats"], [])

    def test_level_summary_passes_only_with_both_injection_directions_valid(self):
        loop = valid_series(None)
        inj_r = valid_series(harness.INJECT_DB)
        inj_l = valid_series(-harness.INJECT_DB)
        failures_loop = harness.assess_series(loop, expected_delta_db=None)
        failures_r = harness.assess_series(inj_r, expected_delta_db=harness.INJECT_DB)
        failures_l = harness.assess_series(inj_l, expected_delta_db=-harness.INJECT_DB)
        summary = harness._summarize_level(
            loop, inj_r, inj_l, failures_loop, failures_r, failures_l)
        self.assertTrue(summary["passed"])
        self.assertTrue(summary["loopback"]["mixer_fingerprint_available"])
        self.assertEqual(len(summary["inject_R"]["rows"]), 3)
        self.assertEqual(len(summary["inject_L"]["rows"]), 3)

    def test_level_summary_fails_if_one_polarity_fails(self):
        loop = valid_series(None)
        inj_r = valid_series(harness.INJECT_DB)
        inj_l = valid_series(-harness.INJECT_DB)
        inj_l["rows"][1]["valid"] = False
        inj_l["rows"][1]["validity_reasons"] = ["low_carrier_residual"]
        failures_loop = harness.assess_series(loop, expected_delta_db=None)
        failures_r = harness.assess_series(inj_r, expected_delta_db=harness.INJECT_DB)
        failures_l = harness.assess_series(inj_l, expected_delta_db=-harness.INJECT_DB)
        summary = harness._summarize_level(
            loop, inj_r, inj_l, failures_loop, failures_r, failures_l)
        self.assertFalse(summary["passed"])
        self.assertTrue(failures_l)

    def test_level_summary_fails_if_numeric_delta_is_wrong(self):
        loop = valid_series(None)
        inj_r = valid_series(harness.INJECT_DB + 0.21)
        inj_l = valid_series(-harness.INJECT_DB)
        failures_loop = harness.assess_series(loop, expected_delta_db=None)
        failures_r = harness.assess_series(inj_r, expected_delta_db=harness.INJECT_DB)
        failures_l = harness.assess_series(inj_l, expected_delta_db=-harness.INJECT_DB)
        summary = harness._summarize_level(
            loop, inj_r, inj_l, failures_loop, failures_r, failures_l)
        self.assertFalse(summary["passed"])
        self.assertTrue(failures_r)


@unittest.skipUnless(__import__("importlib").util.find_spec("valvebench"),
                     "run from ValveBench checkout to test production integration")
class ProductionSimulatorIntegrationTests(unittest.TestCase):
    def test_both_injection_signs_pass_at_candidate_level(self):
        from valvebench.backend import SimBackend, SimChannel
        from valvebench.calib import Calibration
        from valvebench.gainmatch import build_loopback_reference, measure_series
        from valvebench.measure import Rig

        fs = 48000
        backend = SimBackend(
            SimChannel(gain=0.65, a2=5e-4, a3=1e-3, riaa=False,
                       noise_dbfs=-115, hum50_dbfs=-125, hum100_dbfs=-130),
            SimChannel(gain=0.52, a2=8e-4, a3=7e-4, riaa=False,
                       noise_dbfs=-115, hum50_dbfs=-125, hum100_dbfs=-130),
            seed=2263,
        )
        backend.open(fs)
        rig = Rig(backend, Calibration(), fs=fs, df=1.0,
                  blocks=harness.BLOCKS, settle=harness.SETTLE_SECONDS)
        state = {"channel": None}
        applied_channels = []
        original = backend.play_record

        def guarded(signal, tail=0.8):
            applied_channels.append(state["channel"])
            prepared = harness.inject_channel_attenuation(
                signal, state["channel"], harness.INJECT_DB)
            prepared = harness.add_d10_edge_guards(prepared, fs=fs)
            return original(prepared, tail=tail)

        backend.play_record = guarded
        for level in harness.LEVELS_DBFS:
            # Every level gets a genuinely unattenuated loopback reference;
            # do not inherit the previous level's final L injection.
            state["channel"] = None
            loopback_call_start = len(applied_channels)
            kwargs = dict(frequencies=harness.FREQUENCIES,
                          level_dbfs=level, repeats=harness.REPEATS,
                          frequency_profile=harness.FREQUENCY_PROFILE)
            loop = measure_series(rig, purpose="loopback", **kwargs)
            loopback_channels = applied_channels[loopback_call_start:]
            self.assertTrue(loopback_channels, "loopback made no backend play_record calls")
            self.assertTrue(
                all(channel is None for channel in loopback_channels),
                f"loopback at {level} dBFS must be unattenuated; "
                f"observed channel states={loopback_channels}")
            self.assertEqual(harness.assess_series(loop, None), [])
            reference = build_loopback_reference(loop)

            state["channel"] = "R"
            inj_r = measure_series(rig, purpose="dut", reference=reference, **kwargs)
            self.assertEqual(
                harness.assess_series(inj_r, harness.INJECT_DB), [],
                f"R attenuation at candidate level {level} dBFS")
            state["channel"] = "L"
            inj_l = measure_series(rig, purpose="dut", reference=reference, **kwargs)
            self.assertEqual(
                harness.assess_series(inj_l, -harness.INJECT_DB), [],
                f"L attenuation at candidate level {level} dBFS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
