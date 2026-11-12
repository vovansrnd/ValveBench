#!/usr/bin/env python3
"""Regression tests for Rig.run() onset/settle alignment."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from valvebench.measure import Rig
from valvebench import gainmatch
from valvebench.backend import SimBackend, SimChannel
from valvebench.calib import Calibration


class DelayedStartBackend:
    """Model a capture stream that starts before playback, as ALSA does."""

    def __init__(self, fs: int):
        self.fs = fs
        self.notes: list[str] = []

    def play_record(self, out: np.ndarray) -> np.ndarray:
        lead = np.zeros((int(round(0.30 * self.fs)), 2), dtype=np.float64)
        tail = np.zeros((int(round(0.50 * self.fs)), 2), dtype=np.float64)
        return np.vstack((lead, out, tail))


class RigOnsetAlignmentTests(unittest.TestCase):
    def test_first_settle_is_not_added_twice_after_tone_onset(self):
        fs = 48000
        rig = Rig(
            DelayedStartBackend(fs),
            fs=fs,
            blocks=2,
            settle=0.5,
        )
        k, amplitude = rig.amp_tone(997.0, -30.0)

        windows, warnings = rig.run([
            ([(k, amplitude)], [(k, amplitude)], None),
        ])

        self.assertEqual(len(windows), 1)
        window = windows[0]
        self.assertEqual(window.shape, (2 * fs, 2))

        # The selected two-second analysis window must contain the full tone.
        # Before the fix, Rig.run() detected the tone onset and then added the
        # 0.5 s settle a second time: the last ~0.45 s became silence.
        chunk = int(0.1 * fs)
        for channel in (0, 1):
            first_rms = float(np.sqrt(np.mean(window[:chunk, channel] ** 2)))
            last_rms = float(np.sqrt(np.mean(window[-chunk:, channel] ** 2)))
            self.assertGreater(first_rms, 1e-3)
            self.assertGreater(
                last_rms / first_rms,
                0.90,
                f"channel {channel}: analysis window contains a silent tail",
            )



    def test_gainmatch_simulated_loopback_stays_valid_and_repeatable(self):
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
                window, channel, k, nharm=nharm,
                coherent_line_floors=coherent_line_floors,
            )
            if int(channel) == 0:
                result["level_dbfs"] += interface_freq_error.get(
                    round(float(rig.f_of(k)), 6), 0.0
                )
            return result

        rig._tone_an = wrapped
        series = gainmatch.measure_series(
            rig, level_dbfs=-24.0, repeats=3, purpose="loopback"
        )
        bad = [
            row for row in series["rows"]
            if not row["valid"] or row.get("repeat_stable") is not True
        ]
        self.assertFalse(bad, f"invalid simulated Gain Match rows after onset alignment: {bad!r}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
