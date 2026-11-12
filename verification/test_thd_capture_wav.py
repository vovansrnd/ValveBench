#!/usr/bin/env python3
"""Preservation tests for the exact THD analysis capture."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
from scipy.io import wavfile

from valvebench.measure import MeasureError, thd_point
from valvebench.cli import main as cli_main


class FakeRig:
    def __init__(self, capture):
        self.capture = np.asarray(capture, dtype=np.float64)
        self.fs = 48000
        self.blocks = 2
        self.n = 48000
        self.cal = {"band_hi": 20000.0}

    def amp_tone(self, frequency, level_dbfs):
        return 123, 0.1

    def f_of(self, k):
        return 997.0

    def run(self, segments):
        return [self.capture.copy()], []

    def _tone_an(self, window, channel, k, nharm):
        return {
            "level_dbfs": -20.0,
            "warnings": [],
            "thd_pct": 0.1,
            "thd_db": -60.0,
            "thdn_pct": 0.2,
            "thdn_db": -53.98,
            "noise_dbfs": -80.0,
            "noise_dbc": -60.0,
            "band_hi": 20000.0,
            "thd_bound": False,
            "thd_partial": False,
        }

    def _levels_info(self, generator_level, measured_level, channel):
        return {"out_v": None, "dut_in_v": None, "gain_db": None}


class ThdCaptureWavTests(unittest.TestCase):
    def test_saves_exact_stereo_window_used_by_thd_analysis(self):
        rng = np.random.default_rng(42)
        capture = rng.normal(0.0, 0.02, size=(self_fs := 96000 * 2, 2))
        rig = FakeRig(capture)
        rig.fs = 96000
        rig.n = 96000

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "raw" / "thd-capture.wav"
            result = thd_point(rig, 997.0, -12.0, capture_wav=target)
            sample_rate, saved = wavfile.read(target)
            self.assertEqual(sample_rate, rig.fs)
            self.assertEqual(saved.dtype, np.float32)
            np.testing.assert_array_equal(saved, capture.astype(np.float32))
            self.assertEqual(result["capture_wav"], str(target.resolve()))
            self.assertEqual(result["capture_format"], "IEEE float32 WAV")
            self.assertEqual(result["capture_sample_rate_hz"], 96000)
            self.assertEqual(tuple(saved.shape), (self_fs, 2))

    def test_refuses_to_overwrite_existing_capture(self):
        capture = np.zeros((96000, 2), dtype=np.float64)
        rig = FakeRig(capture)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "existing.wav"
            original = b"keep-this-file"
            target.write_bytes(original)
            with self.assertRaisesRegex(MeasureError, "не перезаписывается|отказ от перезаписи"):
                thd_point(rig, capture_wav=target)
            self.assertEqual(target.read_bytes(), original)

    def test_without_wav_option_does_not_create_capture_file(self):
        rig = FakeRig(np.zeros((96000, 2), dtype=np.float64))
        result = thd_point(rig)
        self.assertNotIn("capture_wav", result)

    def test_cli_wav_option_reaches_thd_capture_method(self):
        rig = FakeRig(np.zeros((96000, 2), dtype=np.float64))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cli-capture.wav"
            output = StringIO()
            with patch("valvebench.cli.make_rig", return_value=rig), \\
                 patch("valvebench.cli.m.text_thd", return_value="fake THD report"), \\
                 redirect_stdout(output):
                code = cli_main(["--sim", "thd", "--wav", str(target)])
            self.assertEqual(code, 0, output.getvalue())
            self.assertTrue(target.is_file())
            self.assertIn("fake THD report", output.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
