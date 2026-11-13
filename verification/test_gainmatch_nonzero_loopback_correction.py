#!/usr/bin/env python3
"""Regression: Gain Match must subtract a NON-ZERO loopback correction.

The earlier test used a 1.0/1.0 loopback, so the correction was approximately
zero and did not prove the subtraction branch. Here the simulated interface
itself has 1.0/0.8 channel gain. The DUT adds another 0.5 factor to R. Expected:
  loopback delta_adc = 20log10(1/0.8) = +1.938 dB
  DUT delta_adc      = 20log10(1/0.4) = +7.959 dB
  corrected delta_G  = DUT - loopback = +6.021 dB
"""

from pathlib import Path
import math
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from verification.test_rig_run_regression import ScriptedHW, make_rig
from valvebench import gainmatch


def test_nonzero_loopback_correction_is_subtracted():
    hw = ScriptedHW(gain=(1.0, 0.8))
    rig = make_rig(hw, edge_protect=True)
    freqs = (987.0, 1123.0)

    loopback = gainmatch.measure_series(
        rig, frequencies=freqs, level_dbfs=-30.0, repeats=2, purpose="loopback"
    )
    expected_correction = 20.0 * math.log10(1.0 / 0.8)
    for row in loopback["rows"]:
        assert abs(row["delta_adc_db"] - expected_correction) < 0.15, (
            row["requested_frequency_hz"], row["delta_adc_db"], expected_correction
        )
        assert abs(row["delta_adc_db"]) > 1.0, (
            "test precondition failed: loopback correction must be non-zero"
        )

    reference = gainmatch.build_loopback_reference(loopback)

    # Keep the same backend identity; inserting the DUT adds 0.5 attenuation
    # to R on top of the existing 0.8 interface gain.
    hw.gain = (1.0, 0.4)
    dut = gainmatch.measure_series(
        rig, frequencies=freqs, level_dbfs=-30.0, repeats=2,
        purpose="dut", reference=reference
    )

    expected_dut_delta = 20.0 * math.log10(1.0 / 0.4)
    expected_corrected = 20.0 * math.log10(2.0)
    for row in dut["rows"]:
        assert abs(row["delta_adc_db"] - expected_dut_delta) < 0.15, (
            row["requested_frequency_hz"], row["delta_adc_db"], expected_dut_delta
        )
        assert abs(row["loopback_correction_db"] - expected_correction) < 0.15, (
            row["requested_frequency_hz"], row["loopback_correction_db"], expected_correction
        )
        assert abs(row["delta_gain_db"] - expected_corrected) < 0.15, (
            row["requested_frequency_hz"], row["delta_gain_db"], expected_corrected
        )

    print(
        "PASS non-zero loopback correction: "
        f"correction={expected_correction:.3f} dB, "
        f"DUT delta={expected_dut_delta:.3f} dB, "
        f"corrected={expected_corrected:.3f} dB"
    )


def test_analog_dead_r_detected():
    """Mirror of the L cable-disconnect test: R has only analog noise."""
    hw = ScriptedHW(dead_channel_noise=(1, -80.0))
    rig = make_rig(hw, edge_protect=True)
    from valvebench import measure as m
    k, a = rig.amp_tone(1000.0, -30.0)
    try:
        rig.run([([(k, a)], [(k, a)], None)])
    except m.MeasureError as exc:
        assert "канал R" in str(exc), f"wrong channel in diagnostic: {exc}"
        print("PASS analog-dead R: failure identifies channel R")
        return
    raise AssertionError("analog-dead R was not detected")


if __name__ == "__main__":
    test_nonzero_loopback_correction_is_subtracted()
    test_analog_dead_r_detected()
