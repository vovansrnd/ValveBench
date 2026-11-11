#!/usr/bin/env python3
"""Regression tests for Gain Match calibration with independent DAC/ADC PCMs."""
from __future__ import annotations

import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from valvebench import gainmatch


class GainMatchSplitPairTests(unittest.TestCase):
    def setUp(self):
        self.be = SimpleNamespace(hw="hw:7,0|hw:8,0", hw_out="hw:7,0", hw_in="hw:8,0",
                                  name="alsa")

    def test_pair_key_matches_window_pair_key_algorithm(self):
        with patch.object(gainmatch, "device_key_from_hw", side_effect=lambda hw: f"stable:{hw}"):
            actual = gainmatch.backend_device_key(self.be)
        raw = "stable:hw:7,0\0stable:hw:8,0"
        expected = "pair:" + hashlib.sha256(raw.encode()).hexdigest()[:24]
        self.assertEqual(actual, expected)

    def test_same_endpoint_keeps_legacy_device_key(self):
        be = SimpleNamespace(hw="hw:7,0", hw_out="hw:7,0", hw_in="hw:7,0", name="alsa")
        with patch.object(gainmatch, "device_key_from_hw", return_value="stable:card7") as key:
            self.assertEqual(gainmatch.backend_device_key(be), "stable:card7")
            self.assertEqual(key.call_count, 2)

    def test_split_mixer_fingerprint_reads_both_endpoints(self):
        with patch.object(gainmatch, "mixer_fingerprint_from_hw",
                          side_effect=lambda hw: {"hw:7,0": "dac-fp", "hw:8,0": "adc-fp"}[hw]) as fp:
            self.assertEqual(gainmatch.backend_mixer_fingerprint(self.be),
                             {"dac": "dac-fp", "adc": "adc-fp"})
            self.assertEqual([call.args[0] for call in fp.call_args_list], ["hw:7,0", "hw:8,0"])

    def test_split_mixer_fingerprint_fails_closed_if_either_snapshot_missing(self):
        with patch.object(gainmatch, "mixer_fingerprint_from_hw",
                          side_effect=lambda hw: "dac-fp" if hw == "hw:7,0" else None):
            self.assertIsNone(gainmatch.backend_mixer_fingerprint(self.be))

    def test_loopback_reference_validates_for_split_pair(self):
        key = "pair:0123456789abcdef01234567"
        mixer = {"dac": "dac-fp", "adc": "adc-fp"}
        ref = {
            "kind": "gain_match_loopback_reference", "device_key": key,
            "repeats": gainmatch.MIN_REPEATS_FOR_REFERENCE,
            "mixer_fingerprint": mixer, "fs": 96000,
            "generator_level_dbfs": -30.0, "riaa_compensated": False,
            "rows": [{"frequency_hz": f, "valid": True}
                     for f in gainmatch.SERIES_FREQUENCIES],
        }
        rig = SimpleNamespace(be=self.be, fs=96000)
        with patch.object(gainmatch, "backend_device_key", return_value=key), \
             patch.object(gainmatch, "backend_mixer_fingerprint", return_value=mixer):
            rows = gainmatch.validate_loopback_reference(rig, ref, -30.0)
        self.assertEqual(set(rows), {round(f, 6) for f in gainmatch.SERIES_FREQUENCIES})

    def test_loopback_reference_rejects_changed_adc_mixer(self):
        key = "pair:0123456789abcdef01234567"
        ref = {
            "kind": "gain_match_loopback_reference", "device_key": key,
            "repeats": gainmatch.MIN_REPEATS_FOR_REFERENCE,
            "mixer_fingerprint": {"dac": "dac-fp", "adc": "old-adc-fp"},
            "fs": 96000, "generator_level_dbfs": -30.0, "riaa_compensated": False,
            "rows": [{"frequency_hz": f, "valid": True}
                     for f in gainmatch.SERIES_FREQUENCIES],
        }
        rig = SimpleNamespace(be=self.be, fs=96000)
        with patch.object(gainmatch, "backend_device_key", return_value=key), \
             patch.object(gainmatch, "backend_mixer_fingerprint",
                          return_value={"dac": "dac-fp", "adc": "new-adc-fp"}):
            with self.assertRaisesRegex(ValueError, "микшер"):
                gainmatch.validate_loopback_reference(rig, ref, -30.0)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(GainMatchSplitPairTests))
    raise SystemExit(0 if result.wasSuccessful() else 1)
