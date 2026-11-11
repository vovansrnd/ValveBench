#!/usr/bin/env python3
"""Hardware-free tests for independent ALSA playback/capture endpoints."""
from __future__ import annotations

import unittest
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from valvebench import backend


class DualEndpointBackendTests(unittest.TestCase):
    def test_aplay_and_arecord_use_their_own_endpoint(self):
        be = backend.AlsaCli(hw_out="hw:7,2", hw_in="hw:8,1")
        be.open(96000)
        play = be._cmd("aplay", "p")
        capture = be._cmd("arecord", "c")
        self.assertEqual(play[play.index("-D") + 1], "hw:7,2")
        self.assertEqual(capture[capture.index("-D") + 1], "hw:8,1")
        self.assertEqual(be.hw_out, "hw:7,2")
        self.assertEqual(be.hw_in, "hw:8,1")
        self.assertNotEqual(be.hw, be.hw_in)

    def test_playback_and_capture_formats_are_negotiated_independently(self):
        fake_streams = {
            7: {"Playback": [backend.AltSet("S24_3LE", 4, (96000,))], "Capture": []},
            8: {"Playback": [], "Capture": [backend.AltSet("S16_LE", 2, (96000,))]},
        }
        be = backend.AlsaCli(hw_out="hw:7,2", hw_in="hw:8,1")
        with patch.object(backend, "parse_stream", side_effect=lambda card: fake_streams[card]):
            be.open(96000)
        self.assertEqual((be.fmt_out, be.ch_out), ("S24_3LE", 4))
        self.assertEqual((be.fmt_in, be.ch_in), ("S16_LE", 2))
        self.assertEqual(be._cmd("aplay", "p")[be._cmd("aplay", "p").index("-f") + 1], "S24_3LE")
        self.assertEqual(be._cmd("arecord", "c")[be._cmd("arecord", "c").index("-f") + 1], "S16_LE")

    def test_ring_buffer_duration_tracks_opened_sample_rate(self):
        be = backend.AlsaCli(hw_out="hw:7,0", hw_in="hw:8,0")
        be.open(192000)
        self.assertEqual(be._ring_capacity, 12 * 192000)
        self.assertEqual(be._ring.shape, (12 * 192000, 2))
        self.assertEqual(be._ring_count, 0)
        self.assertEqual(be._ring_total, 0)

    def test_legacy_single_endpoint_argument_still_maps_both_sides(self):
        be = backend.AlsaCli("hw:3,0")
        be.open(48000)
        self.assertEqual(be.hw_out, "hw:3,0")
        self.assertEqual(be.hw_in, "hw:3,0")
        self.assertEqual(be._cmd("aplay", "p")[be._cmd("aplay", "p").index("-D") + 1], "hw:3,0")
        self.assertEqual(be._cmd("arecord", "c")[be._cmd("arecord", "c").index("-D") + 1], "hw:3,0")

    def test_common_rates_intersect_both_endpoints(self):
        def rates(hw, direction):
            if hw == "hw:7,0":
                return (48000, 96000, 192000) if direction == "Playback" else ()
            if hw == "hw:8,0":
                return (48000, 88200, 96000) if direction == "Capture" else ()
            if hw == "hw:9,0":
                return (44100, 88200) if direction == "Capture" else ()
            return ()
        with patch.object(backend, "supported_pcm_rates", side_effect=rates):
            self.assertEqual(backend.common_pcm_rates("hw:7,0", "hw:8,0"), (48000, 96000))
            self.assertEqual(backend.common_pcm_rates("hw:7,0", "hw:9,0"), ())

    def test_missing_stream_descriptor_falls_back_to_known_side_for_open_probe(self):
        def rates(hw, direction):
            if hw == "hw:7,0" and direction == "Playback":
                return (48000, 96000)
            return ()
        with patch.object(backend, "supported_pcm_rates", side_effect=rates):
            self.assertEqual(backend.common_pcm_rates("hw:7,0", "hw:8,0"), (48000, 96000))
            self.assertEqual(backend.common_pcm_rates("hw:8,0", "hw:9,0"),
                             (44100, 48000, 88200, 96000, 176400, 192000))

    def test_simulator_is_only_a_full_duplex_pair(self):
        self.assertEqual(backend.common_pcm_rates("sim", "sim"),
                         (44100, 48000, 88200, 96000, 176400, 192000))
        self.assertEqual(backend.common_pcm_rates("sim", "hw:8,0"), ())
        self.assertEqual(backend.common_pcm_rates("hw:8,0", "sim"), ())


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DualEndpointBackendTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
