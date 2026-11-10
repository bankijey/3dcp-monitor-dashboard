"""
Calibration to physical units and per-experiment summary statistics.

fct_pump stores raw sensor values. These physical-unit values only feed
dim_experiment's *_min / *_max columns, so experiments from different eras
are directly comparable in a filter (p1_max > 20 means 20 bar in every year).

Calibration follows the notebook: process_pump (new era) and
process_old_pump (old era). The era is decided by the channels the source
log carries and, where that is ambiguous, by the value domain:

  new   P0 present, or P1/T1 logged as ADC counts      2024-02-11 onwards
  old   P1/P2/T1 logged as volt-like floats, no P0      2023-07 .. 2024-02-12
  none  no pressure / temperature                       2023-03 .. 2023-07

Three logs from the 2024-02-11/12 sensor upgrade already carry the new ADC
counts (T1 ~200 = 20 degC /10) but not yet the P0 column; old-era values are
T1 ~5 (x5 = 25 degC) and P1 < 5 (x4 = bar), so T1 > 50 or P1 > 10 can only be
new-era counts.

Dead or saturated channels log a sentinel (32768 and a few counts above);
those samples are excluded. P0 is a 4-20 mA transmitter (6000 counts = 4 mA
= 0 bar, 1500 counts/mA): a disconnected loop reads ~0 counts, which the
formula would turn into -12.5 bar, so P0 below the NAMUR NE43 fault limit
(3.6 mA = 5400 counts) is excluded too. Torque and feed are the same in every
era: torque % = 100*raw/65535; feed = planar head speed sqrt(vx^2+vy^2) in
mm/min (only where velocity is logged, 2023-07 onwards).
"""
from __future__ import annotations

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from .schema import SENTINEL

P0_OPEN_LOOP = 5400          # counts; < 3.6 mA = transmitter fault (NAMUR NE43)
RANGE_CHANNELS = ("p0", "p1", "p2", "p3", "t0", "t1", "torque", "feed")


def _live_median(table: pa.Table, col: str):
    a = _f64(table, col)
    a = a[np.isfinite(a) & (a < SENTINEL)]
    return float(np.median(a)) if a.size else None


def detect_era(source_columns, fct_pump: pa.Table | None = None) -> str:
    cols = set(source_columns)
    if "P0" in cols:
        return "new"
    if not cols & {"P1", "P2", "T1"}:
        return "none"
    if fct_pump is not None:                  # new ADC counts without a P0 column
        t1, p1 = _live_median(fct_pump, "t1_raw"), _live_median(fct_pump, "p1_raw")
        if (t1 is not None and t1 > 50) or (p1 is not None and p1 > 10):
            return "new"
    return "old"


def _f64(table: pa.Table, col: str) -> np.ndarray:
    # always a writable copy: pyarrow hands back read-only zero-copy views
    return np.array(pc.cast(table[col], pa.float64())
                    .to_numpy(zero_copy_only=False), dtype=float, copy=True)


def calibrate_pump(fct_pump: pa.Table, era: str) -> dict[str, np.ndarray]:
    """Physical-unit arrays: p0-p3 (bar), t0/t1 (degC), torque (%)."""
    out: dict[str, np.ndarray] = {}
    for ch in ("p0", "p1", "p2", "p3", "t0", "t1"):
        a = _f64(fct_pump, f"{ch}_raw")
        if not np.isfinite(a).any():
            continue
        a[a >= SENTINEL] = np.nan
        if ch == "p0":
            a[a < P0_OPEN_LOOP] = np.nan
        if era == "new":
            if ch.startswith("t"):
                a = a / 10
            elif ch == "p0":
                a = 1.25 * 40 * (a - 6000) / 24000
            else:
                a = 40 * a / 30000
        elif era == "old":
            if ch == "t1":
                a = a * 5
            elif ch == "p1":
                a = a * 4
            elif ch == "p2":
                a = a * 40000 / 2**16
            else:
                continue            # P0/P3/T0 don't exist in the old era
        else:
            continue
        out[ch] = a
    t = _f64(fct_pump, "torque_raw")
    if np.isfinite(t).any():
        out["torque"] = 100 * t / (2**16 - 1)
    return out


def _minmax(a: np.ndarray):
    a = a[np.isfinite(a)]
    return (float(a.min()), float(a.max())) if a.size else (None, None)


def experiment_stats(fct_pump: pa.Table, motion: pa.Table | None, era: str) -> dict:
    """dim_experiment range columns for one experiment. `motion` must be the
    table at pump grain (before removing repeats) so means are time-weighted."""
    s: dict = {"sensor_era": era}
    cal = calibrate_pump(fct_pump, era)
    if motion is not None:
        vx, vy = _f64(motion, "vel_x"), _f64(motion, "vel_y")
        if np.isfinite(vx).any() or np.isfinite(vy).any():
            cal["feed"] = np.hypot(vx, vy)
        for axis, arr in (("x", vx), ("y", vy)):
            s[f"vel_{axis}_min"], s[f"vel_{axis}_max"] = _minmax(arr)
            fin = arr[np.isfinite(arr)]
            s[f"vel_{axis}_mean"] = float(fin.mean()) if fin.size else None
    for ch in RANGE_CHANNELS:
        s[f"{ch}_min"], s[f"{ch}_max"] = _minmax(cal[ch]) if ch in cal else (None, None)
    for col in ("set_speed", "water_valve"):
        s[f"{col}_min"], s[f"{col}_max"] = _minmax(_f64(fct_pump, col))
    return s
