"""Conversions must match the lab's calibration notebook."""

import numpy as np
import pytest

from printmon.units import DEFAULT_SELECTION, SIGNALS, feed_from_vel

# raw count -> expected engineering value, computed by hand from the notebook formulas
CASES = {
    "torque": (32767.5, 50.0),                     # 100 * v / (2**16 - 1)
    "T0": (285, 28.5),                             # v / 10
    "T1": (290, 29.0),
    "P0": (9261, 1.25 * 40 * (9261 - 6000) / 24000),
    "P1": (3339, 40 * 3339 / 30000),
    "P2": (2737, 40 * 2737 / 30000),
    "P3": (30000, 40.0),
    "water_flow": (65535, 20.4),                   # v * 20.4 / (2**16 - 1)
    "water_temp": (21237, 21.237),                 # v / 1000
}


@pytest.mark.parametrize("key", sorted(CASES))
def test_engineering_conversion_matches_notebook(key):
    raw, expected = CASES[key]
    sig = SIGNALS[key]
    assert sig.convert(np.array([raw], float), True)[0] == pytest.approx(expected)
    assert sig.convert(np.array([raw], float), False)[0] == raw  # raw mode is untouched


def test_registry_covers_the_requested_sensors():
    for key in ("set_speed", "feed", "P0", "P1", "P2", "P3", "T0", "T1", "water_temp",
                "water_flow", "mixer"):
        assert key in SIGNALS
    assert set(DEFAULT_SELECTION) <= set(SIGNALS)
    assert SIGNALS["mixer"].boolean


def test_feed_is_resolved_xy_velocity():
    assert feed_from_vel((6000.0, 8000.0, -500.0, 0, 0)) == pytest.approx(10000.0)
