#!/usr/bin/env python3
"""Offline tests for v2.26.0 harness helpers and production simulator path."""
import importlib.util
from pathlib import Path
import unittest

import numpy as np

SCRIPT = Path(__file__).with_name("v2260_gainmatch_hardware_validation.py")
spec = importlib.util.spec_from_file_location("v2260_harness", SCRIPT)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


class GuardAndInjectionTests(unittest.TestCase):
    def test_edge_guards_are_exact_silence_and_preserve_signal(self):
        x = np.ones((100, 2), dtype=np.float64)
        y = harness.add_d10_edge_guards(x, fs=100, lead_seconds=1.5, tail_seconds=0.6)
        self.assertEqual(y.shape, (310, 2))
        np.testing.assert_array_equal(y[:150], 0.0)
        np.testing.assert_array_equal(y[150:250], x)
        np.testing.assert_array_equal(y[250:], 0.0)

    def test_r_attenuation_is_exact_and_does_not_touch_l(self):
        x = np.ones((20, 2), dtype=np.float64)
        y = harness.inject_channel_attenuation(x, "R", harness.INJECT_DB)
        np.testing.assert_allclose(y[:, 0], 1.0, atol=0.0)
        np.testing.assert_allclose(y[:, 1], 0.5, rtol=1e-14, atol=0.0)

    def test_l_attenuation_is_exact_and_does_not_touch_r(self):
        x = np.ones((20, 2), dtype=np.float64)
        y = harness.inject_channel_attenuation(x, "L", harness.INJECT_DB)
        np.testing.assert_allclose(y[:, 0], 0.5, rtol=1e-14, atol=0.0)
        np.testing.assert_allclose(y[:, 1], 1.0, atol=0.0)

    def test_no_attenuation_is_copy(self):
        x = np.arange(20, dtype=np.float64).reshape(10, 2)
        y = harness.inject_channel_attenuation(x, None)
        np.testing.assert_array_equal(y, x)
        self.assertIsNot(y, x)

    def test_rejects_non_stereo_and_nan(self):
        with self.assertRaises(ValueError):
            harness.add_d10_edge_guards(np.ones(10))
        x = np.ones((5, 2))
        x[0, 0] = np.nan
        with self.assertRaises(ValueError):
            harness.inject_channel_attenuation(x, "R")


class AcceptanceGateTests(unittest.TestCase):
    @staticmethod
    def result(delta):
        return {"events": {"fatal": []}, "rows": [
            {"frequency_hz": f, "valid": True, "repeat_stable": True,
             "delta_gain_db": delta, "validity_reasons": []}
            for f in harness.FREQUENCIES]}

    def test_positive_six_db_expected_for_r_attenuation(self):
        self.assertEqual(
            harness.assess_series(self.result(harness.INJECT_DB), harness.INJECT_DB), [])

    def test_negative_six_db_expected_for_l_attenuation(self):
        self.assertEqual(
            harness.assess_series(self.result(-harness.INJECT_DB), -harness.INJECT_DB), [])

    def test_out_of_tolerance_fails(self):
        failures = harness.assess_series(self.result(5.0), harness.INJECT_DB)
        self.assertEqual(len(failures), 3)

    def test_invalid_or_unstable_rows_fail(self):
        result = self.result(harness.INJECT_DB)
        result["rows"][0]["valid"] = False
        result["rows"][1]["repeat_stable"] = None
        self.assertEqual(len(harness.assess_series(result, harness.INJECT_DB)), 2)

    def test_fatal_hardware_event_fails(self):
        result = self.result(harness.INJECT_DB)
        result["events"]["fatal"] = ["XRUN"]
        self.assertEqual(len(harness.assess_series(result, harness.INJECT_DB)), 1)


@unittest.skipUnless(importlib.util.find_spec("valvebench"),
                     "run from the ValveBench checkout to test production integration")
class ProductionSimulatorIntegrationTests(unittest.TestCase):
    def test_gainmatch_loopback_reference_recovers_both_injected_signs(self):
        # Production analyzer + SimBackend; no ALSA or physical hardware.
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
            seed=2260,
        )
        backend.open(fs)
        rig = Rig(backend, Calibration(), fs=fs, df=1.0, blocks=2, settle=0.5)
        state = {"channel": None}
        original = backend.play_record

        def guarded(signal, tail=0.8):
            signal = harness.inject_channel_attenuation(signal, state["channel"])
            signal = harness.add_d10_edge_guards(signal, fs=fs)
            return original(signal, tail=tail)

        backend.play_record = guarded
        kwargs = dict(frequencies=harness.FREQUENCIES, level_dbfs=-30.0,
                      repeats=3, frequency_profile="mains_avoidance_50")
        loop = measure_series(rig, purpose="loopback", **kwargs)
        self.assertEqual(harness.assess_series(loop, None), [])
        reference = build_loopback_reference(loop)

        state["channel"] = "R"
        right = measure_series(rig, purpose="dut", reference=reference, **kwargs)
        self.assertEqual(harness.assess_series(right, harness.INJECT_DB), [])

        state["channel"] = "L"
        left = measure_series(rig, purpose="dut", reference=reference, **kwargs)
        self.assertEqual(harness.assess_series(left, -harness.INJECT_DB), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
