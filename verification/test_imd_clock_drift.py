#!/usr/bin/env python3
"""Regression: IMD must not cancel tones when DAC and ADC clocks drift."""
from __future__ import annotations

import unittest
import numpy as np
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from valvebench.dsp import analyze_imd


class ImdClockDriftTests(unittest.TestCase):
    def test_ccif_product_survives_25ppm_inter_block_phase_rotation(self):
        fs = 96000
        n = fs
        blocks = 4
        f1, f2 = 19000.0, 20000.0
        ppm = 25.6
        ratio = 1.0 + ppm / 1e6
        t = np.arange(n * blocks, dtype=np.float64) / fs
        # 19/20 kHz fundamentals and a -66.02 dBc difference product.
        x = (
            0.3 * np.sin(2 * np.pi * f1 * ratio * t)
            + 0.3 * np.sin(2 * np.pi * f2 * ratio * t)
            + 0.0003 * np.sin(2 * np.pi * (f2 - f1) * ratio * t)
        )
        result = analyze_imd(x, fs, f1, f2, n, mode="ccif")
        self.assertGreater(result["d2_pct"], 0.0)
        self.assertAlmostEqual(result["imd_db"], -66.02, delta=1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
