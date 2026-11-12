#!/usr/bin/env python3
"""Regressions for non-coherent THD line detection above the local noise floor."""
from __future__ import annotations

import unittest
import numpy as np
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from valvebench.dsp import analyze_tone, COH_TOL_BINS


class NonCoherentThdFloorTests(unittest.TestCase):
    def setUp(self):
        self.fs = 48000
        self.n = 48000
        self.blocks = 8
        self.f0 = 997.4  # deliberately 0.4 FFT bin off-grid
        self.t = np.arange(self.n * self.blocks, dtype=np.float64) / self.fs
        self.carrier_amp = 10 ** (-20.0 / 20.0) * np.sqrt(2.0)
        self.rng = np.random.default_rng(20261112)

    def _noise(self):
        # Broadband noise is deliberately much higher than the distortion
        # product; the line detector must use its local spectral floor.
        return self.rng.normal(0.0, 10 ** (-55.0 / 20.0) / np.sqrt(2.0),
                               self.t.size)

    def _base(self):
        return self.carrier_amp * np.sin(2 * np.pi * self.f0 * self.t)

    def test_noise_only_is_reported_as_thd_upper_bound_not_distortion(self):
        result = analyze_tone(self._base() + self._noise(), self.fs,
                              self.f0, self.n, nharm=8, mains=0)
        self.assertGreater(abs(result["coh_err_bins"]), COH_TOL_BINS)
        self.assertEqual(result["line_mode"], "bh-power-floor-gated")
        self.assertTrue(result["thd_bound"])
        self.assertFalse(result["thd_partial"])
        self.assertFalse(any(h["credible"] for h in result["harmonics"]))
        self.assertAlmostEqual(result["thd_pct"], 0.0, places=10)
        self.assertGreater(result["thd_upper_pct"], result["thd_pct"])
        self.assertTrue(all(h["floor_dbc"] is not None
                            and h["margin_db"] is not None
                            for h in result["harmonics"]))

    def test_real_h2_is_detected_while_noise_floor_harmonics_remain_bounded(self):
        h2_amp = self.carrier_amp * 10 ** (-50.0 / 20.0)
        signal = (self._base()
                  + h2_amp * np.sin(2 * np.pi * (2 * self.f0) * self.t + 0.37)
                  + self._noise())
        result = analyze_tone(signal, self.fs, self.f0, self.n,
                              nharm=8, mains=0)
        harmonics = {h["h"]: h for h in result["harmonics"]}
        self.assertTrue(harmonics[2]["credible"], harmonics[2])
        self.assertAlmostEqual(harmonics[2]["dbc"], -50.0, delta=2.0)
        self.assertTrue(all(not harmonics[h]["credible"] for h in range(3, 9)))
        self.assertTrue(result["thd_partial"])
        self.assertFalse(result["thd_bound"])
        self.assertAlmostEqual(result["thd_db"], -50.0, delta=2.0)
        self.assertGreaterEqual(result["thd_upper_db"], result["thd_db"])

    def test_power_floor_scales_with_number_of_lobe_bins(self):
        result = analyze_tone(self._base() + self._noise(), self.fs,
                              self.f0, self.n, nharm=4, mains=0)
        for item in result["harmonics"]:
            self.assertIsNotNone(item["random_floor_dbc"])
            self.assertLessEqual(item["random_floor_dbc"], item["floor_dbc"] + 1e-9)
            self.assertFalse(item["credible"])

    def test_noncoherent_thdn_matches_known_broadband_noise(self):
        noise_rms = 10 ** (-55.0 / 20.0)
        noise = self.rng.normal(0.0, noise_rms, self.t.size)
        result = analyze_tone(self._base() + noise, self.fs, self.f0,
                              self.n, nharm=8, mains=0, band=(20.0, 20000.0))
        # White noise is limited to 20 kHz of the 24-kHz Nyquist band.
        expected_db = 20.0 * np.log10(noise_rms * np.sqrt(20000.0 / 24000.0) / 0.1)
        self.assertGreater(abs(result["coh_err_bins"]), COH_TOL_BINS)
        self.assertAlmostEqual(result["thdn_db"], expected_db, delta=1.5)
        self.assertAlmostEqual(result["noise_dbfs"], -55.0 + 10.0 * np.log10(20000.0 / 24000.0),
                               delta=1.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
