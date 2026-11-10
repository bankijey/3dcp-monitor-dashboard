"""
Is an experiment a real print? Read from its own signals, not its name.

    material flowing   pump torque >= 8 % while the head moves (a dry or idle
                       pump reads ~3 % or ~0; nothing in the corpus falls in
                       between)
    printing           the head moving with material flowing for >= 2 min
    stacked            >= 2 layers (previous-pass layers, see geometry)

    print        material flowing, printing, stacked
    not a print  no material while moving (dry run, levelling, scan), material
                 but no printing motion (pump / flush test), or a log < 1 min
    unsure       everything in between (short prints of small objects,
                 dead torque channel with line pressure, no position log,
                 scan only): decided by hand in run_type_overrides.csv

Decisions in run_type_overrides.csv (experiment_id, run_type, note) win over
the signals and survive every rebuild.
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from . import geometry
from .calibrate import calibrate_pump

TORQUE_MATERIAL = 8.0         # % while moving: material in the line
TORQUE_DRY = 6.0              # below: no material
P_MATERIAL_BAR = 2.0          # line pressure that betrays material when torque reads ~0
MIN_PRINT_MIN = 2.0
MIN_LAYERS = 2
OVERRIDES = Path(__file__).resolve().parent / "run_type_overrides.csv"
SHAPE_OVERRIDES = Path(__file__).resolve().parent / "shape_overrides.csv"
RUN_TYPES = ("print", "not a print", "unsure")


def _seconds(col) -> np.ndarray:
    ns = pc.cast(pc.cast(col, pa.int64()), pa.float64(), safe=False)
    return np.array(ns.to_numpy(zero_copy_only=False), dtype=float) / 1e9


def _f(tbl: pa.Table, name: str) -> np.ndarray:
    if name not in tbl.column_names:
        return np.full(tbl.num_rows, np.nan)
    return np.array(pc.cast(tbl[name], pa.float64()).to_numpy(zero_copy_only=False), dtype=float)


def _path_from_heads(scan: pa.Table):
    """(t, x, y, z, feed) from a scan log's head positions, real updates only
    (the log repeats the last position between controller updates)."""
    t = _seconds(scan["event_time"])
    x, y, z = (_f(scan, f"head_{a}") for a in "xyz")
    ok = np.isfinite(t) & np.isfinite(x) & np.isfinite(y)
    t, x, y, z = t[ok], x[ok], y[ok], z[ok]
    if len(t) < 3:
        return None
    o = np.argsort(t)
    t, x, y, z = t[o], x[o], y[o], z[o]
    moved = np.r_[True, (np.diff(x) != 0) | (np.diff(y) != 0) | (np.diff(z) != 0)]
    t, x, y, z = t[moved], x[moved], y[moved], z[moved]
    if len(t) < 3:
        return None
    dt = np.diff(t)
    feed = np.r_[0.0, np.hypot(np.diff(x), np.diff(y)) / np.where(dt > 0, dt, np.nan) * 60]
    return t, x, y, z, np.nan_to_num(feed)


def signature(fct_pump: pa.Table | None, fct_motion: pa.Table | None, era: str | None,
              scan_heads: pa.Table | None = None) -> dict:
    """Minutes moving, minutes printing with material, torque / pressure
    while moving, and the highest layer reached."""
    out = dict(moving_min=0.0, print_minutes=0.0, torque_p50_moving=np.nan,
               torque_p90=np.nan, p1_p50_moving=np.nan, printed_layers=0,
               has_motion=fct_motion is not None and fct_motion.num_rows > 0)
    path = None
    if fct_motion is not None and fct_motion.num_rows:
        mt = _seconds(fct_motion["event_time"])
        mx, my, mz = (_f(fct_motion, f"pos_{a}") for a in "xyz")
        mf = np.hypot(np.nan_to_num(_f(fct_motion, "vel_x")), np.nan_to_num(_f(fct_motion, "vel_y")))
        ok = np.isfinite(mt)
        o = np.argsort(mt[ok])
        path = tuple(a[ok][o] for a in (mt, mx, my, mz, mf))
    elif scan_heads is not None and scan_heads.num_rows:
        path = _path_from_heads(scan_heads)
        out["has_motion"] = path is not None
    if fct_pump is not None and fct_pump.num_rows:
        t = _seconds(fct_pump["event_time"])
        cal = calibrate_pump(fct_pump, era or "none")
        tq = np.asarray(cal.get("torque", np.full(len(t), np.nan)), float)
        p1 = np.asarray(cal.get("p1", np.full(len(t), np.nan)), float)
        ok = np.isfinite(t)
        t, tq, p1 = t[ok], tq[ok], p1[ok]
        o = np.argsort(t)
        t, tq, p1 = t[o], tq[o], p1[o]
        if len(t) > 1:
            dt = np.clip(np.diff(t, append=t[-1]), 0, 1.0)
            if path is not None and len(path[0]):
                j = np.searchsorted(path[0], t, side="right") - 1
                feed = np.where(j >= 0, path[4][np.clip(j, 0, None)], 0.0)
            else:
                feed = np.zeros(len(t))
            mv = feed > 0
            out["moving_min"] = float(dt[mv].sum() / 60)
            out["print_minutes"] = float(dt[mv & (tq > TORQUE_MATERIAL)].sum() / 60)
            if np.isfinite(tq).any():
                out["torque_p90"] = float(np.nanpercentile(tq, 90))
            if mv.any() and np.isfinite(tq[mv]).any():
                out["torque_p50_moving"] = float(np.nanmedian(tq[mv]))
            if mv.any() and np.isfinite(p1[mv]).any():
                out["p1_p50_moving"] = float(np.nanmedian(p1[mv]))
    elif path is not None:
        dt = np.clip(np.diff(path[0], append=path[0][-1]), 0, 1.0)
        out["moving_min"] = float(dt[path[4] > 0].sum() / 60)
    if path is not None and len(path[0]) >= 10:
        mv = path[4] > 0
        if mv.sum() >= 10:
            _, layer, _, _ = geometry.path_layers(*(a[mv] for a in path[:4]))
            if np.isfinite(layer).any():
                out["printed_layers"] = int(np.nanmax(layer))
    return out


def classify(sig: dict, duration_min: float, has_pump: bool) -> tuple[str, str]:
    """-> (run_type, reason)."""
    pm, layers = sig["print_minutes"], sig["printed_layers"]
    if duration_min < 1:
        if pm > 0.1:
            return "unsure", "short log (< 1 min) but it printed with material"
        return "not a print", "log shorter than 1 min"
    if not has_pump:
        if layers >= 3 and sig["moving_min"] >= 2:
            return "unsure", "scan only (no pump log): head moved and stacked layers"
        return "not a print", "scan only, no printing motion or stacking"
    if not sig["has_motion"]:
        if (sig["torque_p90"] or 0) >= TORQUE_MATERIAL and duration_min >= 2:
            return "unsure", "no position log, but material was pumped"
        return "not a print", "no position log and no material"
    if sig["moving_min"] < 0.5:
        if np.nan_to_num(sig["torque_p90"]) >= TORQUE_MATERIAL:
            return "not a print", "material pumped but the head did not move (pump / flush test)"
        return "not a print", "head did not move, no material"
    tq = sig["torque_p50_moving"]
    if not np.isfinite(tq) or tq < TORQUE_DRY:
        if (not np.isfinite(tq) or tq < 1) and np.nan_to_num(sig["p1_p50_moving"]) > P_MATERIAL_BAR:
            return "unsure", "torque reads ~0 but pressure says material was flowing"
        return "not a print", "head moved without material (dry run / levelling / scan)"
    if pm >= MIN_PRINT_MIN and layers >= MIN_LAYERS:
        return "print", "material flowing while printing, stacked layers"
    why = []
    if pm < MIN_PRINT_MIN:
        why.append(f"only {pm:.1f} min printing with material")
    if layers < MIN_LAYERS:
        why.append(f"{layers} layer(s)")
    return "unsure", ", ".join(why)


def load_overrides(path: Path = OVERRIDES) -> dict[str, tuple[str, str]]:
    """experiment_id -> (run_type, note) for rows with a decision."""
    if not path.exists():
        return {}
    out = {}
    with path.open(newline="", encoding="utf-8") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
        for row in csv.DictReader(lines):
            rt = (row.get("run_type") or "").strip().lower()
            if rt in RUN_TYPES:
                out[row["experiment_id"].strip()] = (rt, (row.get("note") or "").strip())
    return out


def apply_overrides(rows: list[dict]) -> int:
    ov = load_overrides()
    n = 0
    for r in rows:
        if r["experiment_id"] in ov:
            rt, note = ov[r["experiment_id"]]
            r.update(run_type=rt, run_type_source="manual",
                     run_type_reason=("set by hand" + (f": {note}" if note else "")))
            n += 1
    return n


def apply_shape_overrides(rows: list[dict], path: Path = SHAPE_OVERRIDES) -> int:
    """shape from shape_overrides.csv (experiment_id, shape[, note]) wins over
    the name; rows without a shape are ignored."""
    if not path.exists():
        return 0
    with path.open(newline="", encoding="utf-8") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
        ov = {r["experiment_id"].strip(): r["shape"].strip().lower()
              for r in csv.DictReader(lines) if (r.get("shape") or "").strip()}
    n = 0
    for r in rows:
        if r["experiment_id"] in ov:
            r.update(shape=ov[r["experiment_id"]], shape_source="manual")
            n += 1
    return n

