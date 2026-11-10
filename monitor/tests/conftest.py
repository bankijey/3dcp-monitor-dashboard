"""Shared fixtures: a small synthetic recorded run in Node-RED's on-disk formats."""

import json

import numpy as np
import pytest

T0 = 1_747_995_514_000_000_000  # 2025-05-23, same era as the real runs
PUMP_HEADER = ("timestamp,pos,vel,start,setSpeed,waterValve,torque,waterFlow,waterTemp,"
               "P0,P1,P2,P3,T0,T1,waterPump,solenoidValve,mixer")


def _profile(n=320, width=34.0):
    x = np.linspace(-25, 28, n)
    z = np.where(np.abs(x) < width / 2, 111.0 - 0.002 * x ** 2, 80.0)
    z[:5] = 0  # scanner reports missing points as 0
    return x, z


@pytest.fixture
def synthetic_run(tmp_path):
    """2 s run: pump rows at 50 Hz, NC position changing at 10 Hz, scans at 40 Hz."""
    (tmp_path / "pump").mkdir()
    (tmp_path / "scans").mkdir()
    name = "2025-05-23__synthetic__F8000__S0__W0__run_1"
    vel = (8000.0, 0.0, 0.0, 0.0, 0.0)  # mm/min along X
    lines = [PUMP_HEADER]
    for i in range(100):
        t = T0 + i * 20_000_000
        k = i // 5  # position updates every 5 rows = 10 Hz
        pos = (500.0 + k * 13.33, 400.0, 35.0, 0.0, 0.0)
        lines.append('{},"{}","{}",true,50,38,{},{},21000,9000,3300,2700,32770,285,290,'
                     'false,false,true'.format(
                         t, json.dumps(list(pos)), json.dumps(list(vel)), 9000 + i, 12 + i % 3))
    (tmp_path / "pump" / (name + ".csv")).write_text("\n".join(lines) + "\n")

    x, z = _profile()
    with open(tmp_path / "scans" / (name + ".json"), "w") as fh:
        for i in range(80):
            t = T0 + 5_000_000 + i * 25_000_000
            fh.write(json.dumps({"time": t, "x": x.tolist(), "z": z.tolist(),
                                 "m0": [100] * len(x), "width": 34.0 + (i % 4) * 0.1,
                                 "X": 500, "Y": 400, "Z": 35, "C": 0}) + "\n")
    return tmp_path, name
