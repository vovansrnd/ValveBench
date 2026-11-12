#!/usr/bin/env python3
"""Safe-level clock preflight tests; simulation only, no hardware claims."""
from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from valvebench.backend import SimBackend, SimChannel
from valvebench.calib import Calibration
from valvebench.measure import Rig, MeasureError, clock_check


class SplitClockPreflightTests(unittest.TestCase):
    def _rig(self):
        backend = SimBackend(
            SimChannel(gain=0.65, a2=5e-4, a3=3e-3, riaa=True,
                       noise_dbfs=-105, hum50_dbfs=-105, hum100_dbfs=-112),
            SimChannel(gain=0.65, a2=2e-3, a3=1e-3, riaa=True,
                       noise_dbfs=-103, hum50_dbfs=-92, hum100_dbfs=-108),
        )
        rig = Rig(backend, Calibration(), fs=96000)
        rig.open()
        return rig

    def test_default_clock_probe_is_low_level_and_detectable_in_sim(self):
        result = clock_check(self._rig())
        self.assertTrue(result["ok"], result["warnings"])
        self.assertEqual(result["test_level_dbfs"], -72.0)
        self.assertLess(result["ch"]["L"]["level_dbfs"], -70.0)
        self.assertLess(result["ch"]["R"]["level_dbfs"], -70.0)
        self.assertLess(abs(result["ch"]["L"]["ppm"]), 1.0)
        self.assertLess(abs(result["ch"]["R"]["ppm"]), 1.0)
        self.assertTrue(result["clock_ok"])
        self.assertTrue(result["signal_ok"])
        self.assertTrue(result["coherent"])

    def test_calibration_profiles_are_isolated_per_dac_adc_pair(self):
        cal = Calibration()
        cal.activate_profile("pair:D10:EMU", "hw:1,0|hw:2,0")
        cal["out_fs_vrms"] = [2.0, 2.1]
        cal.activate_profile("pair:D10:ASUS", "hw:1,0|hw:3,0")
        self.assertEqual(cal["out_fs_vrms"], [None, None])
        cal["out_fs_vrms"] = [1.0, 1.1]
        cal.activate_profile("pair:D10:EMU", "hw:1,0|hw:2,0")
        self.assertEqual(cal["out_fs_vrms"], [2.0, 2.1])
        restored = Calibration(cal.serialize())
        restored.activate_profile("pair:D10:ASUS", "hw:1,0|hw:3,0")
        self.assertEqual(restored["out_fs_vrms"], [1.0, 1.1])

    def test_clock_probe_rejects_relative_drift_over_100_ppm(self):
        class FakeRig:
            fs = 96000
            def __init__(self, ppm):
                self.ppm = ppm
            def amp_tone(self, _f, _level):
                return 1000, 10 ** (-72.0 / 20.0)
            def run(self, _segs):
                return [[object()]], []
            def f_of(self, _k):
                return 1000.0
            def _tone_an(self, _win, _ch, _k, nharm=2):
                return {"f0": 1000.0 * (1.0 + self.ppm / 1e6),
                        "level_dbfs": -72.0, "noise_dbfs": -105.0,
                        "coh_err_bins": self.ppm / 1000.0, "warnings": []}
        within_gate_but_not_coherent = clock_check(FakeRig(50.0))
        self.assertTrue(within_gate_but_not_coherent["ok"])
        self.assertTrue(within_gate_but_not_coherent["clock_ok"])
        self.assertTrue(within_gate_but_not_coherent["signal_ok"])
        self.assertFalse(within_gate_but_not_coherent["coherent"])
        rejected = clock_check(FakeRig(150.0))
        self.assertFalse(rejected["ok"])
        self.assertFalse(rejected["clock_ok"])
        self.assertTrue(any("расхождение тактов" in w for w in rejected["warnings"]))

    def test_legacy_single_device_config_migrates_to_both_endpoints(self):
        legacy = Calibration({"device": "hw:2,0", "fs": 192000,
                              "out_fs_vrms": [1.0, 1.1], "in_fs_vrms": [0.2, 0.21]})
        self.assertEqual(legacy["dac_device"], "hw:2,0")
        self.assertEqual(legacy["adc_device"], "hw:2,0")
        self.assertEqual(legacy["fs"], 192000)
        self.assertEqual(legacy["out_fs_vrms"], [1.0, 1.1])
        self.assertEqual(legacy["in_fs_vrms"], [0.2, 0.21])

    def test_partial_split_config_falls_back_missing_side_to_legacy(self):
        cfg = Calibration({"device": "hw:2,0", "dac_device": "hw:7,0"})
        self.assertEqual(cfg["dac_device"], "hw:7,0")
        self.assertEqual(cfg["adc_device"], "hw:2,0")

    def test_clock_probe_rejects_accidentally_high_level(self):
        with self.assertRaises(MeasureError):
            clock_check(self._rig(), level_dbfs=-12.0)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(SplitClockPreflightTests))
    raise SystemExit(0 if result.wasSuccessful() else 1)
