import csv
from itertools import pairwise

import numpy as np
import pytest

from printmon import config
from printmon.processing import (
    PositionTracker,
    ProfileStore,
    recent_layers,
    resample_profile,
)
from printmon.schema import process_from_csv


def test_resample_drops_missing_points_and_does_not_bridge_gaps():
    x = np.array([-10, -9, -8, -7, 5, 6, 7], dtype=float)
    z = np.array([100, 101, 0, 102, 110, 111, 112], dtype=float)  # one missing, one gap
    grid = np.arange(-10, 8, 1.0)
    h = resample_profile(x, z, grid, max_gap=2.0)
    assert h[0] == pytest.approx(100)
    assert h[2] == pytest.approx(101.5)  # interpolated across the single missing point
    assert np.isnan(h[grid.tolist().index(0.0)])  # 12 mm occlusion is not invented
    assert h[-1] == pytest.approx(112)


def test_resample_all_missing():
    assert np.isnan(resample_profile([1, 2], [0, 0], np.arange(3.0))).all()


def test_tracker_extrapolates_along_velocity_in_mm_per_min():
    tr = PositionTracker(max_extrapolate_s=0.15)
    tr.update(0, (100.0, 0.0, 0.0, 0, 0), (6000.0, 0.0, 0.0, 0, 0))  # 100 mm/s
    assert tr.at(50_000_000)[0] == pytest.approx(105.0)  # +50 ms
    assert tr.at(10_000_000_000)[0] == pytest.approx(115.0)  # capped at 150 ms
    assert tr.update(1, (100.0, 0.0, 0.0, 0, 0)) is False  # repeated value is not a new anchor
    tr.extrapolate = False
    assert tr.at(50_000_000)[0] == pytest.approx(100.0)


def test_profile_store_time_lookup_window_and_spacing():
    ps = ProfileStore(4, np.arange(3.0), points=5)
    for i in range(6):  # wraps: keeps t = 20..50
        ps.push(i * 10, np.arange(5.0), np.full(5, float(i)), np.full(3, i, np.float32),
                xy=(i * 2.0, 0.0))
    assert ps.time_range() == (20, 50)
    t, _, z = ps.at(34)  # latest profile at or before t
    assert t == 30 and z[0] == 3.0
    assert ps.at(5) is None  # older than retained history
    tt, d, h = ps.window(20, 40)
    assert tt.tolist() == [20, 30, 40] and h[:, 0].tolist() == [2, 3, 4]
    assert np.allclose(np.diff(d), 2.0)  # spaced by path distance, not sample count
    assert len(ps.window(0, 100, max_rows=2)[0]) == 2
    tail = ps.window(0, 100, max_rows=3, dense_tail=True)[0]
    assert tail[-1] == 50 and tail.tolist() == sorted(tail.tolist())  # newest always kept


def test_recent_layers_orders_by_last_appearance():
    z = [35, 35, 55, 55, 75, 497, 75, 55.3]  # travel move at 497, revisit of layer 55
    assert recent_layers(z, n=3) == [55.0, 75.0, 497.0]
    assert recent_layers([], n=3) == []
    assert recent_layers([35.0, 35.2, np.nan]) == [35.0]


def test_recent_layers_ignores_z_moves_between_layers():
    # 10 mm layers; a lift from 105 to 115 passes through 108/111/113 for a sample each
    z = [95] * 50 + [105] * 50 + [108, 111, 113] + [115] * 20
    assert recent_layers(z, n=3, min_points=15) == [115.0, 105.0, 95.0]
    assert recent_layers(z, n=3) == [115.0, 113.0, 111.0]  # without the guard: slivers


RUN = config.DATA_DIR / "pump" / "recorded_run.csv"


@pytest.mark.skipif(not RUN.exists(), reason="recorded lab data not available")
def test_velocity_units_on_recorded_run():
    """
    The dead-reckoning assumption, checked against a real run: extrapolating the
    10 Hz NC position with vel in mm/min must beat holding the last sample by a
    wide margin. Measured on this run: 1.2 mm vs 14 mm median error.
    """
    anchors, last = [], None
    with open(RUN, newline="") as fh:
        for row in csv.DictReader(fh):
            r = process_from_csv(row)
            if r["pos"] and r["pos"] != last:
                anchors.append((r["t"], np.array(r["pos"][:3]), np.array(r["vel"][:3])))
                last = r["pos"]
    hold, dead = [], []
    for (t0, p0, v0), (t1, p1, _) in pairwise(anchors):
        dt = (t1 - t0) / 1e9
        if 0 < dt < 0.5 and np.linalg.norm(v0) > 60:
            hold.append(np.linalg.norm(p1 - p0))
            dead.append(np.linalg.norm(p0 + v0 / 60.0 * dt - p1))
    assert len(dead) > 1000
    assert np.median(dead) < 2.0
    assert np.median(dead) * 5 < np.median(hold)
