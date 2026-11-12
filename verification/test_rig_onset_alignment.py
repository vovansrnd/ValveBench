#!/usr/bin/env python3
"""Regression tests for tone-onset alignment and its Gain Match caller."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from valvebench import gainmatch
from valvebench.backend import SimBackend, SimChannel
from valvebench.calib import Calibration
from valvebench.measure import Rig


class DelayedStartBackend:
    """Capture backend with a fixed transport start delay and post-roll."""

    def __init__(self, fs: int, delay_s: float = 0.30, tail_s: float = 0.50):
        self.fs = int(fs)
        self.delay_s = float(delay_s)
        self.tail_s = float(tail_s)
        self.notes: list[str] = []

    def play_record(self, out: np.ndarray) -> np.ndarray:
        lead = np.zeros((int(round(self.delay_s * self.fs)), 2), dtype=np.float64)
        tail = np.zeros((int(round(self.tail_s * self.fs)), 2), dtype=np.float64)
        return np.vstack((lead, np.asarray(out, dtype=np.float64), tail))


class RigOnsetAlignmentTests(unittest.TestCase):
    def test_first_analysis_window_contains_full_tone_not_settle_plus_tone(self):
        fs = 48000
        for frequency in (100.0, 997.0, 10000.0):
            with self.subTest(frequency=frequency):
                rig = Rig(
                    DelayedStartBackend(fs),
                    fs=fs,
                    blocks=2,
                    settle=0.5,
                )
                k, amplitude = rig.amp_tone(frequency, -24.0)
                windows, _warnings = rig.run([
                    ([(k, amplitude)], [(k, amplitude)], None),
                ])

                self.assertEqual(len(windows), 1)
                window = windows[0]
                self.assertEqual(window.shape, (2 * fs, 2))

                # Compare the first and last 100 ms at the known carrier.
                # A duplicated 0.5 s settle shifts the window into the final
                # 0.5 s of silence and drives the last-window amplitude to zero.
                n = int(0.1 * fs)
                t = np.arange(n, dtype=np.float64) / fs
                levels = []
                for channel in (0, 1):
                    channel_levels = []
                    for segment in (window[:n, channel], window[-n:, channel]):
                        basis = np.column_stack((
                            np.sin(2 * np.pi * frequency * t),
                            np.cos(2 * np.pi * frequency * t),
                            np.ones(n),
                        ))
                        coef, *_ = np.linalg.lstsq(basis, segment, rcond=None)
                        channel_levels.append(float(np.hypot(coef[0], coef[1])))
                    levels.append(channel_levels)
                for channel, (first, last) in enumerate(levels):
                    self.assertGreater(first, 1e-3, f"channel {channel}: first window is silent")
                    self.assertGreater(
                        last / first,
                        0.90,
                        f"channel {channel}: final 100 ms lost tone "
                        f"(first={first:.6g}, last={last:.6g})",
                    )

    def test_simulated_gainmatch_loopback_remains_valid_and_repeatable(self):
        backend = SimBackend(
            SimChannel(gain=0.97, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130),
            SimChannel(gain=1.03, noise_dbfs=-125, hum50_dbfs=-130, hum100_dbfs=-130),
            seed=17,
        )
        rig = Rig(backend, Calibration(), fs=48000, blocks=2, settle=0.15)
        rig.open()

        interface_freq_error = {100.0: -0.25, 1000.0: 0.10, 10000.0: 0.35}
        original = rig._tone_an

        def wrapped(window, channel, k, nharm=9, coherent_line_floors=None):
            result = original(
                window,
                channel,
                k,
                nharm=nharm,
                coherent_line_floors=coherent_line_floors,
            )
            if int(channel) == 0:
                f_meas = round(float(rig.f_of(k)), 6)
                result["level_dbfs"] += interface_freq_error.get(f_meas, 0.0)
            return result

        rig._tone_an = wrapped
        series = gainmatch.measure_series(
            rig,
            level_dbfs=-24.0,
            repeats=3,
            purpose="loopback",
        )
        invalid = [
            {
                "frequency_hz": row["frequency_hz"],
                "valid": row["valid"],
                "repeat_stable": row["repeat_stable"],
                "L_reasons": row["channels"]["L"]["validity_reasons"],
                "R_reasons": row["channels"]["R"]["validity_reasons"],
            }
            for row in series["rows"]
            if not row["valid"] or row.get("repeat_stable") is not True
        ]
        self.assertFalse(invalid, f"simulated Gain Match rows became invalid: {invalid!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
