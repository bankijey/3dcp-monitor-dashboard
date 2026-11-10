"""
Conform a parsed pump log to the data model (schema.py).

convert.py turns a source file into typed Arrow with the source's own column
names. This step maps it onto the model:
  * renames to the standard lower snake_case names
  * casts every column to its conformed type
  * adds each channel / axis the log did not have as NULL
  * normalises the log into two fact tables: fct_pump (pump sensors) and
    fct_motion (position / velocity, which come from the motion controller)

The pump logger re-writes the last known position on every sample, so ~4 of
5 logged rows repeat a stale reading (the controller updates at ~10 Hz, the
logger samples at ~50 Hz). fct_motion keeps only rows that carry a new
reading. Nothing is lost: an as-of join (last reading at or before a time)
returns exactly the value the logger wrote for every pump row.
"""
from __future__ import annotations

import re

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from .convert import TS_MAX, TS_MIN
from .schema import (AXES, KEY_COLUMNS, MOTION_COLUMNS, PUMP_COLUMNS,
                     PUMP_SOURCE_NAMES, STR, TS, arrow_schema, snake)

_AXIS_COL = re.compile(r"^(pos|vel)(\d+)$")
_NKEY = len(KEY_COLUMNS)


def _key(experiment_id: str, n: int) -> pa.Array:
    return pa.repeat(pa.scalar(experiment_id, STR), n)


def _keys(raw: pa.Table, experiment_id: str) -> dict:
    """experiment_id, event_time, sample_seq (row position in the source log)."""
    n = raw.num_rows
    return {"experiment_id": _key(experiment_id, n), "event_time": event_time(raw),
            "sample_seq": pa.array(np.arange(n, dtype=np.int64))}


def event_time(raw: pa.Table) -> pa.Array:
    """int64 ns -> TIMESTAMP(ns, UTC); an unset clock (outside 2000-2100) -> NULL."""
    n = raw.num_rows
    if "timestamp" not in raw.column_names:
        return pa.nulls(n, type=TS)
    ts = raw["timestamp"]
    ok = pc.and_(pc.greater_equal(ts, TS_MIN), pc.less_equal(ts, TS_MAX))
    ts = pc.if_else(ok, ts, pa.scalar(None, pa.int64()))
    return pc.cast(ts, TS)


def _cast(col, typ, warnings: list, name: str):
    try:
        return pc.cast(col, typ)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError):
        warnings.append(f"{name}: not castable to {typ}, stored as NULL")
        return pa.nulls(len(col), type=typ)


def to_fct_pump(raw: pa.Table, experiment_id: str) -> tuple[pa.Table, list]:
    n = raw.num_rows
    warnings: list[str] = []
    src = {PUMP_SOURCE_NAMES[c]: c for c in raw.column_names
           if c in PUMP_SOURCE_NAMES}
    cols = _keys(raw, experiment_id)
    for name, typ, _ in PUMP_COLUMNS[_NKEY:]:
        cols[name] = (_cast(raw[src[name]], typ, warnings, name) if name in src
                      else pa.nulls(n, type=typ))
    fields = list(arrow_schema(PUMP_COLUMNS))
    # future channels the model doesn't know yet: kept, snake_cased, at the end
    known = set(PUMP_SOURCE_NAMES) | {"timestamp"}
    for c in raw.column_names:
        if c not in known and not _AXIS_COL.match(c):
            name = snake(c)
            if name not in cols:
                cols[name] = raw[c]
                fields.append(pa.field(name, raw[c].type))
                warnings.append(f"extra channel kept: {c} -> {name}")
    return pa.table(cols, schema=pa.schema(fields)), warnings


def _update_mask(table: pa.Table, columns) -> np.ndarray:
    """Rows to keep: the first row of every run of identical readings
    (NaN-safe: NULL -> NULL is 'unchanged'), plus the first row of each run
    that has a usable clock, so a time-based as-of join still finds the run
    when its first row has no timestamp."""
    n = table.num_rows
    changed = np.zeros(n, dtype=bool)
    if n == 0:
        return changed
    changed[0] = True
    for c in columns:
        a = np.array(pc.cast(table[c], pa.float64())
                     .to_numpy(zero_copy_only=False), dtype=float)
        prev, cur = a[:-1], a[1:]
        same = (prev == cur) | (np.isnan(prev) & np.isnan(cur))
        changed[1:] |= ~same
    keep = changed.copy()
    timed = np.flatnonzero(~np.asarray(pc.is_null(table["event_time"])
                                       .to_numpy(zero_copy_only=False), dtype=bool))
    if timed.size:
        run = np.cumsum(changed) - 1                       # run id of every row
        _, first = np.unique(run[timed], return_index=True)
        keep[timed[first]] = True
    return keep


def to_fct_motion(raw: pa.Table, experiment_id: str):
    """-> (motion at pump grain, motion updates only) or (None, None) when
    the log has no position / velocity."""
    axis_cols = {c: m for c in raw.column_names if (m := _AXIS_COL.match(c))}
    if not axis_cols:
        return None, None
    n = raw.num_rows
    cols = _keys(raw, experiment_id)
    for kind in ("pos", "vel"):
        for k, a in enumerate(AXES):
            src = f"{kind}{k}"
            cols[f"{kind}_{a}"] = (pc.cast(raw[src], pa.float64())
                                   if src in raw.column_names
                                   else pa.nulls(n, type=pa.float64()))
    full = pa.table(cols, schema=arrow_schema(MOTION_COLUMNS))
    value_cols = [name for name, _, _ in MOTION_COLUMNS[_NKEY:]]
    return full, full.filter(pa.array(_update_mask(full, value_cols)))


def median_rate_hz(times, max_gap_s: float | None = None) -> float | None:
    """Median sampling rate from event times (ignores NULLs, repeats and,
    optionally, gaps longer than max_gap_s such as stops)."""
    t = np.array(pc.cast(times, pa.int64()).to_numpy(zero_copy_only=False),
                 dtype=float) if not isinstance(times, np.ndarray) else times
    t = t[np.isfinite(t)]
    if t.size < 2:
        return None
    d = np.diff(t)
    d = d[d > 0]
    if max_gap_s is not None:
        d = d[d <= max_gap_s * 1e9]
    return float(1e9 / np.median(d)) if d.size else None


def motion_update_rate_hz(motion: pa.Table) -> float | None:
    """Median rate of real position updates while the head is moving."""
    if motion is None or motion.num_rows < 2:
        return None
    v = np.zeros(motion.num_rows)
    has_vel = False
    for a in ("x", "y", "z"):
        x = np.array(pc.cast(motion[f"vel_{a}"], pa.float64())
                     .to_numpy(zero_copy_only=False), dtype=float)
        has_vel |= bool(np.isfinite(x).any())
        v += np.abs(np.nan_to_num(x))
    # 2023-05..07 logs have position but no velocity: use every update
    moving = motion.filter(pa.array(v > 0)) if has_vel else motion
    return median_rate_hz(moving["event_time"], max_gap_s=1.0)
