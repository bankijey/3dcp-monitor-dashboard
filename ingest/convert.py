"""
Parse raw experiment files (extract step).

Pump CSV  -> typed Arrow table with the log's own column names, built in
             memory (files are at most ~45 MB); standardize.py then conforms
             it to fct_pump + fct_motion.
Scan JSON -> streamed straight to a conformed fct_scan Parquet file in bounded
             batches (files reach 1 GB, so they are never held in memory).

Format drift across 2023-2026 is handled per file:
  pump: header row missing in some files (layout inferred from the column
        count and value pattern), lower-case 'false', junk and repeated
        header rows, millisecond vs nanosecond timestamps; pos[] / vel[]
        arrays exploded into pos0..posN / vel0..velN.
  scan: 2023 batch envelopes {"sendtime", "scans": [...]}, flat records of
        several vintages, and 2024 closed-loop logs
        {"scan": {...}, "pump": {...}, "prev": {...}}.
Timestamps are normalised to nanoseconds. Sensor values stay raw: calibration
to physical units happens in calibrate.py (catalog stats) and downstream,
never in the stored Parquet.
"""
from __future__ import annotations

import csv
import itertools
import json
import math
import os
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .schema import (MODEL_VERSION, SCAN_COLUMNS, SCAN_SOURCE_NAMES, TS,
                     snake)

# ---------------------------------------------------------------- shared
TS_MIN = 946_684_800 * 10**9            # 2000-01-01, ns: below = junk clock
TS_MAX = 4_102_444_800 * 10**9          # 2100-01-01, ns


def _ts_scale(values) -> tuple[str, int]:
    """Guess a timestamp unit from its magnitude -> (unit, factor to ns)."""
    pos = sorted(v for v in values if v is not None and v > 0)
    if not pos:
        return "unknown", 1
    med = pos[len(pos) // 2]
    if med < 1e11:
        return "s", 10**9
    if med < 1e14:
        return "ms", 10**6
    if med < 1e17:
        return "us", 10**3
    return "ns", 1


def file_metadata(table: str, experiment_id: str, source_file: str) -> dict:
    """Key-value metadata stored in every Parquet file we write."""
    return {"model_version": MODEL_VERSION, "table": table,
            "experiment_id": experiment_id, "source_file": source_file}


def write_parquet(table: pa.Table, out_path: str | Path,
                  row_group_size: int | None = None,
                  metadata: dict | None = None) -> int:
    """Write atomically (temp file + rename) so a crash never leaves a
    half-written Parquet behind."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if metadata:
        table = table.replace_schema_metadata(
            {**(table.schema.metadata or {}), **metadata})
    tmp = out_path.with_name(out_path.name + ".tmp")
    pq.write_table(table, tmp, compression="zstd", compression_level=5,
                   use_dictionary=True, row_group_size=row_group_size)
    os.replace(tmp, out_path)
    return out_path.stat().st_size


# ---------------------------------------------------------------- pump CSV
ARRAY_COLUMNS = ("pos", "vel")
BOOL_COLUMNS = ("start", "waterPump", "solenoidValve", "mixer")
KNOWN_COLUMNS = {"timestamp", *ARRAY_COLUMNS, *BOOL_COLUMNS, "setSpeed",
                 "waterValve", "torque", "waterFlow", "waterTemp",
                 "P0", "P1", "P2", "P3", "T0", "T1"}

# Header layouts seen across the corpus, used when a file has no header row.
HEADER_TEMPLATES = {
    "2023-03": ["timestamp", "start", "setSpeed", "waterValve", "torque",
                "waterFlow", "waterTemp", "waterPump", "solenoidValve", "mixer"],
    "2023-05": ["timestamp", "pos", "start", "setSpeed", "waterValve", "torque",
                "waterFlow", "waterTemp", "waterPump", "solenoidValve", "mixer"],
    "2023-07a": ["timestamp", "pos", "start", "setSpeed", "waterValve", "torque",
                 "waterFlow", "P1", "P2", "T1", "waterPump"],
    "2023-07b": ["timestamp", "pos", "start", "setSpeed", "waterValve", "torque",
                 "waterFlow", "P1", "P2", "T1", "waterPump", "solenoidValve",
                 "mixer"],
    "2023-07c": ["timestamp", "pos", "vel", "start", "setSpeed", "waterValve",
                 "torque", "waterFlow", "P1", "P2", "T1", "waterPump",
                 "solenoidValve", "mixer"],
    "2024-02": ["timestamp", "pos", "vel", "start", "setSpeed", "waterValve",
                "torque", "waterFlow", "waterTemp", "P0", "P1", "P2", "P3",
                "T0", "T1", "waterPump", "solenoidValve", "mixer"],
}


def _to_number(v: str):
    v = v.strip()
    if not v:
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)                 # also accepts nan / inf / 1e5
    except ValueError:
        return None


def _to_int(v: str):
    x = _to_number(v)
    if isinstance(x, float):
        return int(x) if math.isfinite(x) else None
    return x


def _to_bool(v: str):
    s = v.strip().lower()
    if s in ("true", "1", "1.0"):
        return True
    if s in ("false", "0", "0.0"):
        return False
    return None


def _parse_list_cell(cell: str) -> list:
    """'[454.6,1160,...]' -> floats; a garbled element becomes None."""
    cell = (cell or "").strip()
    if cell[:1] == "[" and cell[-1:] == "]":
        cell = cell[1:-1]
    out = []
    for tok in cell.split(","):
        tok = tok.strip()
        if tok:
            try:
                out.append(float(tok))
            except ValueError:
                out.append(None)
    return out


def _fits(row: list[str], header: list[str]) -> bool:
    for v, col in zip(row, header):
        v = v.strip()
        if col in ARRAY_COLUMNS:
            if not (v.startswith("[") and v.endswith("]")):
                return False
        elif col in BOOL_COLUMNS:
            if v.lower() not in ("true", "false"):
                return False
        elif col == "timestamp":
            if not v.lstrip("-").isdigit():
                return False
        elif v and _to_number(v) is None:
            return False
    return True


def _infer_header(first: list[str]):
    for name, header in HEADER_TEMPLATES.items():
        if len(header) == len(first) and _fits(first, header):
            return header, name
    return None, None


def _numeric_or_text(raw: list[str]) -> pa.Array:
    nums = [_to_number(v) for v in raw]
    nonempty = sum(1 for v in raw if v.strip())
    bad = sum(1 for v, x in zip(raw, nums) if x is None and v.strip())
    if nonempty and bad > 0.05 * nonempty:          # genuinely a text column
        return pa.array([v if v.strip() else None for v in raw], type=pa.string())
    vals = [x for x in nums if x is not None]
    if vals and all(isinstance(x, int) for x in vals):
        try:
            return pa.array(nums, type=pa.int64())
        except (pa.ArrowInvalid, OverflowError):
            pass
    return pa.array([None if x is None else float(x) for x in nums],
                    type=pa.float64())


def convert_pump_csv(csv_path: str | Path) -> tuple[pa.Table, dict]:
    csv_path = Path(csv_path)
    with csv_path.open(newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise ValueError("empty file")

    first = [c.strip() for c in rows[0]]
    if any(c in KNOWN_COLUMNS for c in first):
        header, header_source, body = first, "file", rows[1:]
    else:
        header, name = _infer_header(first)
        if header is None:
            raise ValueError(f"no header row and no known layout with "
                             f"{len(first)} columns")
        header_source, body = f"inferred:{name}", rows

    # Logs get restarted mid-file (repeated header) and pick up blank or
    # truncated lines; drop those so they don't poison type coercion.
    ncols = len(header)
    body = [r for r in body
            if len(r) == ncols and any(c.strip() for c in r)
            and [c.strip() for c in r] != header]
    idx = {h: i for i, h in enumerate(header) if h}

    arrays: dict[str, pa.Array] = {}
    manifest: dict[str, Any] = {"rows": len(body), "header_source": header_source,
                                "ts_unit": None, "time_min": None, "time_max": None}

    if "timestamp" in idx:
        raw_ts = [_to_int(r[idx["timestamp"]]) for r in body]
        unit, fac = _ts_scale(raw_ts)
        ts = [None if v is None else v * fac for v in raw_ts]
        arrays["timestamp"] = pa.array(ts, type=pa.int64())
        valid = [v for v in ts if v is not None and TS_MIN <= v <= TS_MAX]
        manifest.update(ts_unit=unit,
                        time_min=min(valid) if valid else None,
                        time_max=max(valid) if valid else None)

    widths: dict[str, int] = {}
    for col in ARRAY_COLUMNS:
        if col not in idx:
            continue
        parsed = [_parse_list_cell(r[idx[col]]) for r in body]
        widths[col] = max((len(v) for v in parsed), default=0)
        for k in range(widths[col]):
            arrays[f"{col}{k}"] = pa.array(
                [v[k] if k < len(v) else None for v in parsed], type=pa.float64())

    for h, i in idx.items():
        if h == "timestamp" or h in ARRAY_COLUMNS:
            continue
        raw = [r[i] for r in body]
        if h in BOOL_COLUMNS:
            arrays[h] = pa.array([_to_bool(v) for v in raw], type=pa.bool_())
        else:
            arrays[h] = _numeric_or_text(raw)

    manifest.update(columns=list(arrays), array_widths=widths)
    return pa.table(arrays), manifest


# ---------------------------------------------------------------- scan JSON
SCAN_BATCH = 2000            # profiles per Parquet row group
SCHEMA_SAMPLE = 200          # records sampled for the fast-path schema

_ARROW = {"bool": pa.bool_(), "int": pa.int64(), "float": pa.float64(),
          "str": pa.string(), "list": pa.list_(pa.float32()),
          "json": pa.string()}
_NUMERIC_RANK = {"bool": 0, "int": 1, "float": 2}
_TIME_COLUMNS = ("event_time", "send_time")
_CORE_NAMES = {n for n, _, _ in SCAN_COLUMNS}
_PROFILE_COLUMNS = ("profile_x", "profile_z", "profile_m0", "profile_intensities")


def _core_category(typ: pa.DataType) -> str:
    if pa.types.is_list(typ):
        return "list"
    if pa.types.is_boolean(typ):
        return "bool"
    if pa.types.is_floating(typ):
        return "float"
    return "json"                         # text: stringify anything


_CORE_CAT = {n: ("time" if n in _TIME_COLUMNS else _core_category(t))
             for n, t, _ in SCAN_COLUMNS if n != "experiment_id"}


class _SchemaDrift(Exception):
    """A record doesn't fit the schema sampled from the start of the file."""


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flatten(v, f"{prefix}{k}_"))
        else:
            out[f"{prefix}{k}"] = v
    return out


def _records_of(obj, stats: dict) -> Iterator[dict]:
    """One parsed JSON line -> flat scan records (source key names)."""
    if isinstance(obj, dict) and isinstance(obj.get("scans"), list):
        stats["layouts"].add("envelope")                 # 2023 batch envelope
        send = obj.get("sendtime")
        for s in obj["scans"]:
            if isinstance(s, dict):
                r = dict(s)
                if send is not None:
                    r["sendtime"] = send
                yield r
    elif isinstance(obj, dict) and any(isinstance(v, dict) for v in obj.values()):
        stats["layouts"].add("nested")                   # 2024 closed-loop log
        r = _flatten(obj)
        if "x" not in r and "scan_x" in r:
            r["x"] = r.pop("scan_x")
        if "z" not in r and "scan_z" in r:
            r["z"] = r.pop("scan_z")
        if "time" not in r and "pump_timestamp" in r:
            r["time"] = r.pop("pump_timestamp")
        yield r
    elif isinstance(obj, dict):
        stats["layouts"].add("flat")
        yield obj
    elif isinstance(obj, list):
        stats["layouts"].add("array")
        for s in obj:
            if isinstance(s, dict):
                yield s


def _iter_pkl_records(path: Path, stats: dict) -> Iterator[dict]:
    """Pickled pandas scan frames (one row per profile: x, z, time), as kept
    for the 2023 arch campaign."""
    import pandas as pd
    df = pd.read_pickle(path)
    stats["layouts"].add("pkl")
    cols = list(df.columns)
    for row in df.itertuples(index=False):
        yield {c: (v.tolist() if hasattr(v, "tolist") else v)
               for c, v in zip(cols, row)}


def _iter_scan_records(path: Path, stats: dict) -> Iterator[dict]:
    """Yield flat scan records (source key names) from any known layout."""
    path = Path(path)
    if path.suffix.lower() == ".pkl":
        yield from _iter_pkl_records(path, stats)
        return
    with open(path, "rb") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                stats["bad_lines"] += 1
                continue
            yield from _records_of(obj, stats)


# ------------------------------------------------------------- time spans
# Cheap probes used to plan an ingest (recorded date, pump<->scan pairing)
# before any file is converted: first and last lines only.
_PROBE = 256 * 1024
NL = bytes([10])                                   # b"\n"


def _valid_span(values, head_n: int):
    """(first, last) ns from raw timestamps; head_n = how many came from the
    start of the file (the rest from the end)."""
    unit, fac = _ts_scale([v for v in values if v is not None])
    ns = [None if v is None else v * fac for v in values]
    ok = lambda v: v is not None and TS_MIN <= v <= TS_MAX
    head = [v for v in ns[:head_n] if ok(v)]
    tail = [v for v in ns[head_n:] if ok(v)]
    if not head and not tail:
        return None, None
    return (min(head) if head else min(tail)), (max(tail) if tail else max(head))


def pump_time_span(path: str | Path):
    """(first, last) valid timestamp in ns of a pump CSV, from its first and
    last 256 KB (the timestamp is the first column in every layout)."""
    path = Path(path)
    size = path.stat().st_size

    def first_fields(chunk: bytes):
        out = []
        for line in chunk.split(NL):
            f = line.split(b",", 1)[0].strip()
            out.append(_to_int(f.decode("ascii", "ignore")) if f[:1].isdigit() else None)
        return [v for v in out if v is not None]

    with open(path, "rb") as fh:
        head = first_fields(fh.read(_PROBE))
        fh.seek(max(0, size - _PROBE))
        tail = first_fields(fh.read()) if size > _PROBE else []
    t0, t1 = _valid_span(head + tail, len(head))
    if t0 is None and size > 2 * _PROBE:          # clock set only mid-file: full read
        with open(path, "rb") as fh:
            allv = first_fields(fh.read())
        t0, t1 = _valid_span(allv, len(allv))
    return t0, t1


def _edge_lines(path: Path, n_bytes: int = 4 * 1024 * 1024):
    """Complete lines at the start and at the end of a (possibly huge) file."""
    size = path.stat().st_size
    with open(path, "rb") as fh:
        first = fh.readline()
        fh.seek(max(0, size - n_bytes))
        tail = fh.read().split(NL)
    if size > n_bytes:
        tail = tail[1:]                            # first piece may be partial
    return first, [t for t in reversed(tail) if t.strip()]


def scan_time_span(path: str | Path):
    """(first, last) profile time in ns of a scan file (JSON: first and last
    line only; pkl: read whole, these files are small)."""
    path = Path(path)
    stats = {"bad_lines": 0, "layouts": set()}
    if path.suffix.lower() == ".pkl":
        times = [r.get("time") for r in _iter_pkl_records(path, stats)]
        return _valid_span(times, len(times))
    first, tail = _edge_lines(path)
    head_t, tail_t = [], []
    try:
        head_t = [r.get("time") for r in _records_of(json.loads(first), stats)]
    except ValueError:
        pass
    for line in tail:                              # last parseable line
        try:
            tail_t = [r.get("time") for r in _records_of(json.loads(line), stats)]
            break
        except ValueError:
            continue
    num = lambda xs: [x for x in xs if isinstance(x, (int, float))
                      and not isinstance(x, bool)]
    head_t, tail_t = num(head_t), num(tail_t)
    return _valid_span(head_t + tail_t, len(head_t))


def _category(v, thorough: bool):
    if v is None:
        return None
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        items = v if thorough else v[:3]
        ok = all(x is None or (isinstance(x, (int, float))
                               and not isinstance(x, bool)) for x in items)
        return "list" if ok else "json"
    return "json"


def _merge(a, b):
    if a is None:
        return b
    if b is None or a == b:
        return a
    if a in _NUMERIC_RANK and b in _NUMERIC_RANK:
        return a if _NUMERIC_RANK[a] >= _NUMERIC_RANK[b] else b
    return "json"                                   # incompatible -> text


def _is_time_key(k: str) -> bool:
    return k.endswith(("time", "timestamp"))


def _schema_of(records, thorough: bool) -> dict[str, str]:
    cats: dict[str, str | None] = {}
    for r in records:
        for k, v in r.items():
            cats[k] = _merge(cats.get(k), _category(v, thorough))
    # Scalars like head Y are logged as 1160 in one record and 1160.5 in the
    # next: store every non-time number as float64 so the sampled schema
    # doesn't drift and a column has one type across all files.
    return {k: ("float" if c == "int" and not _is_time_key(k) else c)
            for k, c in cats.items() if c is not None}


def _coerce(vals: list, cat: str) -> list:
    """Convert values to the column category; raises on a mismatch."""
    def as_int(v):
        if isinstance(v, (bool, int)):
            return int(v)
        raise TypeError(v)

    def as_bool(v):
        if isinstance(v, bool):
            return v
        raise TypeError(v)

    def as_str(v):
        if isinstance(v, str):
            return v
        raise TypeError(v)

    def as_list(v):
        if isinstance(v, list):
            return v
        raise TypeError(v)

    conv = {"float": float, "int": as_int, "bool": as_bool, "str": as_str,
            "list": as_list,
            "json": lambda v: v if isinstance(v, str) else json.dumps(v)}[cat]
    return [None if v is None else conv(v) for v in vals]


def _model_names(source_keys) -> dict[str, str]:
    """source key -> fct_scan column; unknown keys snake_cased (clashes get
    a suffix) and kept as extra columns."""
    used, names = set(_CORE_NAMES), {}
    for k in source_keys:
        name = SCAN_SOURCE_NAMES.get(k)
        if name is None:
            name = snake(k) or "field"
            while name in used:
                name += "_2"
            used.add(name)
        names[k] = name
    return names


def _scan_pass(path: Path, out_path: Path, experiment_id: str,
               strict: bool) -> dict:
    stats = {"bad_lines": 0, "layouts": set()}
    records = _iter_scan_records(path, stats)
    if strict:          # schema from the first records, then one streaming pass
        head = list(itertools.islice(records, SCHEMA_SAMPLE))
        src_cats = _schema_of(head, thorough=False)
        stream = itertools.chain(head, records)
    else:               # full pass for the schema, then a second pass to write
        fresh = lambda: _iter_scan_records(path, {"bad_lines": 0, "layouts": set()})
        src_cats = _schema_of(fresh(), thorough=True)
        head = list(itertools.islice(fresh(), SCHEMA_SAMPLE))
        stream = records

    names = _model_names(src_cats)
    by_model = {v: k for k, v in names.items()}
    extras = [k for k in src_cats if names[k] not in _CORE_NAMES]
    fields = [pa.field(n, t) for n, t, _ in SCAN_COLUMNS]
    fields += [pa.field(names[k], _ARROW[src_cats[k]]) for k in extras]
    schema = pa.schema(fields).with_metadata(
        file_metadata("fct_scan", experiment_id, path.name))
    known_keys = set(src_cats)

    tfac: dict[str, tuple[str, int]] = {}
    for col in _TIME_COLUMNS:
        k = by_model.get(col)
        if k is not None and src_cats.get(k) in ("int", "float"):
            tfac[col] = _ts_scale([r[k] for r in head
                                   if isinstance(r.get(k), (int, float))
                                   and not isinstance(r.get(k), bool)])

    rows = 0
    tmin = tmax = last_t = None
    gaps: list[int] = []
    lengths: set[int] = set()
    tmp = out_path.with_name(out_path.name + ".tmp")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    writer = None
    batch: list[dict] = []

    def flush():
        nonlocal writer, rows, tmin, tmax, last_t
        n = len(batch)
        cols = {"experiment_id": pa.repeat(pa.scalar(experiment_id, pa.string()), n)}
        for name, typ, _ in SCAN_COLUMNS[1:]:
            if name == "sample_seq":                      # position in the scan log
                cols[name] = pa.array(np.arange(rows, rows + n, dtype=np.int64))
                continue
            k = by_model.get(name)
            if k is None or (name in _TIME_COLUMNS and name not in tfac):
                cols[name] = pa.nulls(n, type=typ)
                continue
            vals = [r.get(k) for r in batch]
            if name in _TIME_COLUMNS:
                fac = tfac[name][1]
                ns = [None if v is None else int(v * fac) for v in vals]
                ns = [v if v is not None and TS_MIN <= v <= TS_MAX else None
                      for v in ns]
                if name == "event_time":
                    for v in ns:
                        if v is None:
                            continue
                        tmin = v if tmin is None or v < tmin else tmin
                        tmax = v if tmax is None or v > tmax else tmax
                        if last_t is not None and v > last_t:
                            gaps.append(v - last_t)
                        last_t = v
                cols[name] = pa.array(ns, type=pa.int64()).cast(TS)
                continue
            if name == "profile_x" and len(lengths) < 10:
                lengths.update(len(v) for v in vals if isinstance(v, list))
            cols[name] = pa.array(_coerce(vals, _CORE_CAT[name]), type=typ)
        for k in extras:
            cat = src_cats[k]
            cols[names[k]] = pa.array(_coerce([r.get(k) for r in batch], cat),
                                      type=_ARROW[cat])
        table = pa.table(cols, schema=schema)
        if writer is None:
            writer = pq.ParquetWriter(tmp, schema, compression="zstd",
                                      compression_level=5)
        writer.write_table(table)
        rows += n
        batch.clear()

    try:
        for r in stream:
            if strict and not r.keys() <= known_keys:
                raise _SchemaDrift(f"new key(s) {sorted(set(r) - known_keys)[:5]}")
            batch.append(r)
            if len(batch) >= SCAN_BATCH:
                flush()
        if batch:
            flush()
    except (TypeError, ValueError, pa.ArrowInvalid, pa.ArrowTypeError) as e:
        if writer is not None:
            writer.close()
            writer = None
        tmp.unlink(missing_ok=True)
        if strict:
            raise _SchemaDrift(str(e)[:200]) from e
        raise
    except BaseException:
        if writer is not None:
            writer.close()
            writer = None
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if writer is not None:
            writer.close()

    if rows:
        os.replace(tmp, out_path)
    else:
        tmp.unlink(missing_ok=True)
    return {
        "rows": rows,
        "columns": [f.name for f in fields],
        "scan_channels": [c for c in _PROFILE_COLUMNS if c in by_model],
        "extras": [names[k] for k in extras],
        "profile_points": ",".join(str(x) for x in sorted(lengths)) or None,
        "time_min": tmin, "time_max": tmax,
        "ts_unit": tfac.get("event_time", ("unknown", 1))[0],
        "rate_hz": float(1e9 / np.median(gaps)) if gaps else None,
        "layout": "+".join(sorted(stats["layouts"])) or None,
        "bad_lines": stats["bad_lines"],
        "schema_pass": "sampled" if strict else "full",
    }


def convert_scan_json(json_path: str | Path, out_path: str | Path,
                      experiment_id: str) -> dict:
    """Stream one scan JSON-lines file to a conformed fct_scan Parquet file;
    returns the manifest.

    Fast path: schema sampled from the first records, one streaming pass.
    If any later record doesn't fit (new key, changed type) the partial file
    is discarded and the file is redone with a full-file schema pass.
    """
    json_path, out_path = Path(json_path), Path(out_path)
    try:
        return _scan_pass(json_path, out_path, experiment_id, strict=True)
    except _SchemaDrift:
        return _scan_pass(json_path, out_path, experiment_id, strict=False)
