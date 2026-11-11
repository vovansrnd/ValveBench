#!/usr/bin/env python3
"""CLI and passport metadata regressions for independent DAC/ADC endpoints."""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from valvebench import cli, passport


class DualEndpointPassportCliTests(unittest.TestCase):
    def test_cli_resolves_split_pair_and_legacy_override(self):
        cfg = {"device": "hw:2,0", "dac_device": "hw:3,0", "adc_device": "hw:2,0"}
        self.assertEqual(cli.selected_audio_pair(SimpleNamespace(), cfg), ("hw:3,0", "hw:2,0"))
        self.assertEqual(cli.selected_audio_pair(SimpleNamespace(dev="hw:4,0"), cfg),
                         ("hw:4,0", "hw:4,0"))

    def test_validate_pair_rate_rejects_non_intersection(self):
        with patch.object(cli, "common_pcm_rates", return_value=(48000, 96000)):
            cli.validate_pair_rate("hw:3,0", "hw:2,0", 96000)
            with self.assertRaisesRegex(ValueError, "не входит"):
                cli.validate_pair_rate("hw:3,0", "hw:2,0", 192000)

    def test_validate_pair_rate_rejects_no_known_overlap(self):
        with patch.object(cli, "common_pcm_rates", return_value=()):
            with self.assertRaisesRegex(ValueError, "нет общей"):
                cli.validate_pair_rate("hw:3,0", "hw:2,0", 96000)

    def test_split_clock_preflight_is_required_and_fails_closed(self):
        rig = object()
        with patch.object(cli.m, "clock_check", return_value={"ok": True}) as check:
            self.assertEqual(cli.split_clock_preflight(rig, "hw:3,0", "hw:2,0"), {"ok": True})
            check.assert_called_once()
        with patch.object(cli.m, "clock_check", return_value={"ok": True}) as check:
            self.assertIsNone(cli.split_clock_preflight(rig, "hw:2,0", "hw:2,0"))
            check.assert_not_called()
        with patch.object(cli.m, "clock_check", return_value={"ok": False, "warnings": [], "ch": {}}):
            with self.assertRaises(cli.m.MeasureError):
                cli.split_clock_preflight(rig, "hw:3,0", "hw:2,0")

    def test_run_passport_exposes_split_pair_at_top_level(self):
        be = SimpleNamespace(name="alsa", hw="hw:3,0|hw:2,0", hw_out="hw:3,0", hw_in="hw:2,0")
        fake_rig = SimpleNamespace(be=be, fs=96000)
        with patch.object(passport, "run_rate", return_value={"io_config": passport.io_config_for_backend(be)}):
            result = passport.run_passport(lambda _fs: fake_rig, rates=(96000,), label="test")
        self.assertEqual(result["dac_device"], "hw:3,0")
        self.assertEqual(result["adc_device"], "hw:2,0")

    def test_passport_io_config_preserves_both_endpoints(self):
        be = SimpleNamespace(name="alsa", hw="hw:3,0|hw:2,0", hw_out="hw:3,0", hw_in="hw:2,0",
                             fmt_in="S16_LE", fmt_out="S24_3LE", ch_in=2, ch_out=2)
        io = passport.io_config_for_backend(be)
        self.assertEqual(io["dac_device"], "hw:3,0")
        self.assertEqual(io["adc_device"], "hw:2,0")
        self.assertEqual(io["capture_format"], "S16_LE")
        self.assertEqual(io["playback_format"], "S24_3LE")

    def test_passport_comparison_detects_only_dac_or_adc_change(self):
        a = {"device": "hw:3,0|hw:2,0", "dac_device": "hw:3,0", "adc_device": "hw:2,0"}
        b = {"device": "hw:3,0|hw:4,0", "dac_device": "hw:3,0", "adc_device": "hw:4,0"}
        mismatches = passport.passport_mismatches(a, b)
        self.assertEqual(len(mismatches), 1)
        self.assertIn("АЦП", mismatches[0])

    def test_passport_comparison_flags_legacy_missing_split_route(self):
        legacy = {"device": "hw:2,0"}
        split = {"device": "hw:3,0|hw:2,0", "dac_device": "hw:3,0", "adc_device": "hw:2,0"}
        mismatches = passport.passport_mismatches(legacy, split)
        self.assertTrue(any("нет раздельной идентификации" in item for item in mismatches))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DualEndpointPassportCliTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
