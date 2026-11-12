#!/usr/bin/env python3
"""Tests for strict cross-DUT session comparisons."""
from __future__ import annotations

import csv
import json
import math
import tempfile
import unittest
from pathlib import Path
import sys
from contextlib import redirect_stdout
from io import StringIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np

from valvebench.dutcompare import ComparisonError, compare_sessions, save_comparison
from valvebench.cli import main as cli_main


def make_envelope(device_id, session_id, *, note="", rate=96000, mixer="mixer-A",
                  thd_level=-12.0, state="complete"):
    frequency = np.geomspace(20.0, 20000.0, 100).tolist()
    left_response = np.linspace(-0.4, 0.3, 100).tolist()
    right_response = np.linspace(-0.2, 0.5, 100).tolist()
    left_riaa = np.linspace(-0.5, 0.4, 100).tolist()
    right_riaa = np.linspace(-0.3, 0.6, 100).tolist()
    tests = {
        "noise": {
            "kind": "noise", "ch": {
                "L": {"unw_dbfs": -100.0, "a_dbfs": -103.0, "c_dbfs": -101.0, "hum": []},
                "R": {"unw_dbfs": -90.0, "a_dbfs": -93.0, "c_dbfs": -91.0, "hum": []},
            },
        },
        "response": {
            "kind": "sweep", "mode": "riaa", "calibration_note": "same calibrated path",
            "ch": {
                "L": {"freq": frequency, "fr_norm_db": left_response,
                      "riaa_dev_db": left_riaa, "gain_1k_db": 20.0},
                "R": {"freq": frequency, "fr_norm_db": right_response,
                      "riaa_dev_db": right_riaa, "gain_1k_db": 22.0},
            },
        },
        "thd": {
            "kind": "thd", "ch": {
                "L": {"level_dbfs": -20.0, "thd_pct": 0.2, "thd_db": -53.98,
                      "thd_upper_pct": 0.25, "thd_upper_db": -52.04,
                      "thdn_pct": 0.5, "thdn_db": -46.02,
                      "noise_dbfs": -66.0, "thd_bound": False, "thd_partial": False,
                      "harmonics": [{"h": 2, "dbc": -53.98, "raw_dbc": -53.0,
                                     "floor_dbc": -70.0, "margin_db": 16.0, "credible": True}]},
                "R": {"level_dbfs": -19.0, "thd_pct": 0.4, "thd_db": -47.96,
                      "thd_upper_pct": 0.45, "thd_upper_db": -46.94,
                      "thdn_pct": 0.6, "thdn_db": -44.44,
                      "noise_dbfs": -64.0, "thd_bound": True, "thd_partial": False,
                      "harmonics": [{"h": 2, "dbc": -47.96, "raw_dbc": -46.94,
                                     "floor_dbc": -62.0, "margin_db": 15.0, "credible": False}]},
            },
        },
        "imd": {
            "kind": "imd", "ch": {
                "L": {"imd_pct": 0.1, "imd_db": -60.0, "d2_pct": 0.08, "d3_pct": 0.06,
                      "products": [{"label": "fв−fн", "dbc": -61.0}]},
                "R": {"imd_pct": 0.2, "imd_db": -53.98, "d2_pct": 0.16, "d3_pct": 0.12,
                      "products": [{"label": "fв−fн", "dbc": -55.0}]},
            },
        },
    }
    result = {
        "kind": "dut_batch", "schema": 1, "state": state,
        "test_order": ["noise", "response", "thd", "imd"],
        "parameters": {
            "noise": {"seconds": 8.0},
            "response": {"f_lo": 20.0, "f_hi": 20000.0, "dur": 8.0, "mode": "riaa", "iec": True},
            "thd": {"frequency_hz": 997.0, "level_dbfs": thd_level, "route": "both"},
            "imd": {"f1_hz": 19000.0, "f2_hz": 20000.0, "mode": "ccif", "level_dbfs": -12.0},
        },
        "tests": tests,
    }
    context = {
        "software": {"name": "ValveBench", "version": "0.1.21-dev4"},
        "device": {"stable_id": "pair:DAC-ADC", "dac_alsa_pcm": "hw:3,0", "alsa_pcm": "hw:2,0"},
        "audio_io": {
            "dac_device": "hw:3,0", "adc_device": "hw:2,0",
            "device_pair_key": "pair:DAC-ADC", "sample_rate_hz": rate,
            "playback_format": "S24_3LE", "capture_format": "S24_3LE",
            "playback_channels": 2, "capture_channels": 2,
            "alsa_mixer_fingerprint": mixer,
        },
        "calibration": {
            "out_fs_vrms": [2.0, 2.0], "in_fs_vrms": [0.2, 0.2],
            "dut_to_adc_pad_db": 12.0, "mains_hz": 50, "analysis_band_hi_hz": 20000,
        },
    }
    return {
        "format": "valvebench-device-measurement", "schema": 1,
        "device_id": device_id, "session_id": session_id, "note": note,
        "measurement_context": context, "result": result,
    }


class DutComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _write(self, name, envelope):
        path = self.root / name
        path.mkdir()
        with (path / "session.json").open("w", encoding="utf-8") as fh:
            json.dump(envelope, fh)
        return str(path)

    def test_comparison_keeps_lr_and_adds_power_correct_aggregate(self):
        first = self._write("dut-590", make_envelope("590", "session-A", note="tube set A"))
        second = self._write("dut-591", make_envelope("591", "session-B", note="tube set B"))
        result = compare_sessions([first, second])
        self.assertEqual(result["device_order"], ["590", "591"])
        noise = result["devices"][0]["metrics"]["noise"]["unw_dbfs"]
        self.assertEqual(noise["L"], -100.0)
        self.assertEqual(noise["R"], -90.0)
        expected = 10.0 * math.log10((10 ** (-100.0 / 10) + 10 ** (-90.0 / 10)) / 2.0)
        self.assertAlmostEqual(noise["aggregate"], expected, places=8)
        thd = result["devices"][0]["metrics"]["thd"]["thd_pct"]
        self.assertEqual(thd["L"], 0.2)
        self.assertEqual(thd["R"], 0.4)
        self.assertAlmostEqual(thd["aggregate"], math.sqrt((0.2**2 + 0.4**2) / 2), places=10)
        self.assertEqual(result["devices"][0]["metrics"]["thd"]["thd_status"]["R"], "bound")
        h2 = result["devices"][0]["metrics"]["thd"]["H2_dbc"]
        self.assertEqual(h2["L"], -53.98)
        self.assertEqual(h2["R"], -47.96)
        self.assertEqual(result["devices"][0]["metrics"]["thd"]["H2_status"]["R"], "bound")

    def test_response_curves_keep_lr_and_generate_aggregate(self):
        path = self._write("dut-590", make_envelope("590", "session-A"))
        result = compare_sessions([path, self._write("dut-591", make_envelope("591", "session-B"))])
        curve = result["devices"][0]["curves"]["response"]
        self.assertEqual(len(curve["frequency_hz"]), 100)  # full sweep grid for graph overlays
        self.assertEqual(len(curve["frequency_hz"]), len(curve["L_db"]))
        self.assertEqual(len(curve["frequency_hz"]), len(curve["R_db"]))
        self.assertEqual(len(curve["frequency_hz"]), len(curve["aggregate_db"]))
        self.assertLessEqual(len(curve["sampled_frequency_hz"]), 22)
        self.assertAlmostEqual(curve["sampled_aggregate_db"][0],
                               (curve["L_db"][0] + curve["R_db"][0]) / 2.0, places=10)
        gain = result["devices"][0]["metrics"]["response"]["gain_1k_db"]
        self.assertEqual(gain["L"], 20.0)
        self.assertEqual(gain["R"], 22.0)
        self.assertAlmostEqual(gain["aggregate"], 20.0 * math.log10((10 + 10**1.1) / 2), places=8)

    def test_different_measurement_chain_is_rejected(self):
        first = self._write("dut-590", make_envelope("590", "session-A"))
        second = make_envelope("591", "session-B", mixer="mixer-B")
        path = self._write("dut-591", second)
        with self.assertRaisesRegex(ComparisonError, "mixer_fingerprint"):
            compare_sessions([first, path])

    def test_different_test_parameters_are_rejected(self):
        first = self._write("dut-590", make_envelope("590", "session-A"))
        second = make_envelope("591", "session-B", thd_level=-6.0)
        path = self._write("dut-591", second)
        with self.assertRaisesRegex(ComparisonError, "Параметры теста THD"):
            compare_sessions([first, path])

    def test_single_channel_thd_does_not_create_fake_two_channel_aggregate(self):
        first_data = make_envelope("590", "session-A")
        second_data = make_envelope("591", "session-B")
        for envelope in (first_data, second_data):
            envelope["result"]["parameters"]["thd"]["route"] = "L"
            envelope["result"]["tests"]["thd"]["ch"].pop("R")
        first = self._write("dut-590", first_data)
        second = self._write("dut-591", second_data)
        result = compare_sessions([first, second])
        metric = result["devices"][0]["metrics"]["thd"]["thd_pct"]
        self.assertEqual(metric["L"], 0.2)
        self.assertIsNone(metric["R"])
        self.assertIsNone(metric["aggregate"])

    def test_same_dut_can_be_compared_across_two_saved_revisions(self):
        first = self._write("before", make_envelope("590", "before-session", note="before"))
        second = self._write("after", make_envelope("590", "after-session", note="after"))
        result = compare_sessions([first, second])
        self.assertEqual(result["device_order"], ["590", "590"])
        self.assertEqual([x["note"] for x in result["devices"]], ["before", "after"])

    def test_partial_package_is_rejected(self):
        first = self._write("dut-590", make_envelope("590", "session-A", state="partial"))
        second = self._write("dut-591", make_envelope("591", "session-B"))
        with self.assertRaisesRegex(ComparisonError, "завершённые пакеты"):
            compare_sessions([first, second])

    def test_cli_dut_compare_runs_without_opening_audio_devices(self):
        first = self._write("dut-590", make_envelope("590", "session-A"))
        second = self._write("dut-591", make_envelope("591", "session-B"))
        output_dir = self.root / "cli-comparison"
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = cli_main(["dut-compare", first, second, "--out-dir", str(output_dir)])
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertIn("СРАВНЕНИЕ DUT", stdout.getvalue())
        self.assertTrue(any(output_dir.iterdir()))

    def test_comparison_export_writes_json_metrics_and_lr_curves(self):
        first = self._write("dut-590", make_envelope("590", "session-A"))
        second = self._write("dut-591", make_envelope("591", "session-B"))
        comparison = compare_sessions([first, second])
        output = Path(save_comparison(comparison, self.root / "comparison-output"))
        self.assertTrue((output / "comparison.json").is_file())
        self.assertTrue((output / "metrics.csv").is_file())
        self.assertTrue((output / "curves.csv").is_file())
        with (output / "metrics.csv").open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        noise = next(row for row in rows
                     if row["device_id"] == "590" and row["test"] == "noise"
                     and row["metric"] == "unw_dbfs")
        self.assertEqual(noise["L"], "-100.0")
        self.assertEqual(noise["R"], "-90.0")
        self.assertNotEqual(noise["aggregate"], "")
        with (output / "curves.csv").open(encoding="utf-8", newline="") as fh:
            curve_rows = list(csv.DictReader(fh))
        self.assertTrue(any(row["device_id"] == "590" and row["L_db"] and row["R_db"]
                            and row["aggregate_db"] for row in curve_rows))
        with (output / "comparison.json").open(encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["kind"], "dut_comparison")


if __name__ == "__main__":
    unittest.main(verbosity=2)
