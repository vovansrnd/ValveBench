#!/usr/bin/env python3
"""Regression tests for correcting independent DAC/ADC sample-timebases."""
from __future__ import annotations

import unittest
import numpy as np
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from valvebench.measure import (
    MeasureError,
    capture_clock_ppm,
    resample_capture_timebase,
)


def peak_frequency(x, fs, oversample=8):
    x = np.asarray(x, dtype=np.float64)
    x = x - np.mean(x)
    window = np.hanning(x.size)
    nfft = int(x.size * oversample)
    spectrum = np.abs(np.fft.rfft(x * window, n=nfft))
    k = int(np.argmax(spectrum))
    return k * fs / nfft


class CaptureTimebaseTests(unittest.TestCase):
    def test_positive_and_negative_clock_error_are_corrected(self):
        fs = 96000
        seconds = 6
        n = fs * seconds
        t = np.arange(n, dtype=np.float64) / fs
        for ppm in (-25.6, 25.6, -99.0, 99.0):
            with self.subTest(ppm=ppm):
                ratio = 1.0 + ppm * 1e-6
                raw = np.column_stack((
                    np.sin(2 * np.pi * 997.0 * ratio * t),
                    np.sin(2 * np.pi * 19000.0 * ratio * t),
                ))
                corrected = resample_capture_timebase(raw, ppm, fs)
                self.assertEqual(corrected.ndim, 2)
                self.assertEqual(corrected.shape[1], 2)
                self.assertAlmostEqual(
                    corrected.shape[0] / raw.shape[0], ratio, delta=2e-6
                )
                f997 = peak_frequency(corrected[:fs * 4, 0], fs)
                f19k = peak_frequency(corrected[:fs * 4, 1], fs)
                self.assertLess(abs(f997 - 997.0), 0.06)
                self.assertLess(abs(f19k - 19000.0), 0.06)

    def test_zero_correction_preserves_samples_and_shape(self):
        rng = np.random.default_rng(2401)
        raw = rng.normal(size=(32768, 2))
        corrected = resample_capture_timebase(raw, 0.0, 96000)
        np.testing.assert_array_equal(corrected, raw)

    def test_rejects_invalid_shape_and_out_of_gate_ratio(self):
        with self.assertRaises(MeasureError):
            resample_capture_timebase(np.zeros(1000), 25.0, 96000)
        with self.assertRaises(MeasureError):
            resample_capture_timebase(np.zeros((1000, 2)), 101.0, 96000)

    def test_clock_ppm_uses_mean_of_valid_l_r_estimates(self):
        class FakeRig:
            clock_result = {
                "ok": True,
                "ch": {"L": {"ppm": 25.0}, "R": {"ppm": 26.0}},
            }
        self.assertAlmostEqual(capture_clock_ppm(FakeRig()), 25.5)

    def test_shared_clock_needs_no_correction(self):
        class SharedClockRig:
            pass
        self.assertEqual(capture_clock_ppm(SharedClockRig()), 0.0)

    def test_invalid_clock_result_is_not_resampled(self):
        class InvalidRig:
            clock_result = {
                "ok": False,
                "ch": {"L": {"ppm": 25.0}, "R": {"ppm": 25.0}},
            }
        with self.assertRaises(MeasureError):
            capture_clock_ppm(InvalidRig())


if __name__ == "__main__":
    unittest.main(verbosity=2)
