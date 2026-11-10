"""
Ingest pipeline: raw pump CSV + scan JSON/pkl -> the star schema in schema.py.

  plan       probe every source file's time span (first/last lines only),
             date each experiment by its recorded time, pair pump and scan
             files by name or, failing that, by time overlap
  extract    parse each source file, tolerating 2023-2026 format drift  (convert)
  transform  conform to the model: standard names and types, NULL for missing
             channels and axes, pump log normalised into fct_pump + fct_motion
                                                                     (standardize)
  load       one Parquet file per experiment and fact table, the experiment
             dimension and data dictionary as Parquet, and (when it is not
             locked by another program) the DuckDB convenience catalog

Output (data folder, default ./data):
  dim_experiment.parquet                            one row per experiment (source of truth)
  data_dictionary.parquet                           every table and column, with units
  pump/experiment_date=YYYY-MM-DD/<id>.parquet      fct_pump
  motion/experiment_date=YYYY-MM-DD/<id>.parquet    fct_motion
  scan/experiment_date=YYYY-MM-DD/<id>.parquet      fct_scan
  catalog.duckdb                                    dim_experiment + fct_* views + data_dictionary
  ingest_report.json                                per-experiment rows, time, errors

Experiment identity
  * Dated by the RECORDED time (Europe/Berlin calendar day of the first valid
    sample). The pump logger names files with its session start date, so 1 in
    3 file names is a day or two early; the ID keeps the file stem with its
    leading date corrected. The raw files are never renamed.
  * A pump log and a scan file pair when their names match, or else when the
    scan's time span lies >= 50% inside the pump log (different names or
    dates for the same print). JSON scans win over pkl copies of the same scan.
  * Byte-identical copies (e.g. '... - Copy.csv') are skipped and reported.

Run:
    python -m ingest.run_ingest --pump-dir C:/.../pumpTests \\
        --scan-dir C:/.../scans --scan-dir C:/.../more_scans
Options:
    --out DIR        data folder (default: data)
    --workers N      parallel worker processes (default: CPU cores - 2, max 8)
    --incremental    skip experiments whose source files are unchanged
    --rebuild        start from scratch (deletes pump/, motion/, scan/)
    --only IDS       comma list of experiment IDs to ingest (debugging)
    --dry-run        show the plan (dates, pairing), then stop
    --catalog-only   only rebuild catalog.duckdb from the Parquet outputs
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytz

from .calibrate import detect_era, experiment_stats
from .convert import (convert_pump_csv, convert_scan_json, file_metadata,
                      pump_time_span, scan_time_span, write_parquet)
from . import geometry, lab_samples, run_type
from .filename_parser import parse_experiment_filename, shape_family
from .schema import (DATASETS, DIM_EXPERIMENT, DIM_INSERT_COLUMNS, FACT_GRAIN,
                     MODEL_VERSION,
                     FACT_VIEWS)
from .standardize import (median_rate_hz, motion_update_rate_hz, to_fct_motion,
                          to_fct_pump)

LOCAL_TZ = pytz.timezone("Europe/Berlin")
PAIR_MIN_OVERLAP = 0.5
_DATE_START = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_AXIS_COL = re.compile(r"^(pos|vel)\d+$")
PATH_COLUMNS = ("pump_path", "motion_path", "scan_path")
DIM_FILE, DICT_FILE = "dim_experiment.parquet", "data_dictionary.parquet"


# ================================================================== plan
def _md5(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _drop_identical(files: list[Path], root: Path):
    """Skip byte-identical copies; keep the shallowest, shortest-named file."""
    by_size = defaultdict(list)
    for p in files:
        by_size[p.stat().st_size].append(p)
    drop, skipped = set(), []
    for same_size in by_size.values():
        if len(same_size) < 2:
            continue
        by_hash = defaultdict(list)
        for p in same_size:
            by_hash[_md5(p)].append(p)
        for group in by_hash.values():
            group.sort(key=lambda p: (len(p.relative_to(root).parts),
                                      len(p.name), p.name.lower()))
            for p in group[1:]:
                drop.add(p)
                skipped.append((str(p), f"identical copy of {group[0].name}"))
    return [p for p in files if p not in drop], skipped


def _source_stem(p: Path) -> str:
    stem = p.stem
    return stem[:-5] if stem.endswith("_scan") else stem      # pkl: <run>_scan.pkl


def _source_id(p: Path, root: Path, taken: set) -> str:
    """ID from the file name alone: folder-prefixed when the name has no date,
    folder-suffixed when the stem is already used (e.g. Archive/)."""
    rel = p.relative_to(root)
    folder = "__".join(rel.parent.parts)
    stem = _source_stem(p)
    sid = f"{folder}__{stem}" if folder and not _DATE_START.match(stem) else stem
    if sid in taken and folder:
        sid = f"{stem}__{folder}"
    base, n = sid, 2
    while sid in taken:
        sid, n = f"{base}__{n}", n + 1
    return sid


def _sources(dirs, patterns, kind):
    out, skipped = [], []
    for root in dirs or []:
        root = Path(root)
        files = sorted({p for pat in patterns for p in root.rglob(pat) if p.is_file()})
        skipped += [(str(p), "empty file") for p in files if p.stat().st_size == 0]
        files = [p for p in files if p.stat().st_size > 0]
        files, dups = _drop_identical(files, root)
        skipped += dups
        files.sort(key=lambda p: (len(p.relative_to(root).parts), str(p).lower()))
        taken: set[str] = set()
        for p in files:
            sid = _source_id(p, root, taken)
            taken.add(sid)
            out.append({"kind": kind, "path": p, "rel": p.relative_to(root).as_posix(),
                        "sid": sid, "fmt": p.suffix.lower().lstrip(".")})
    return out, skipped


def _overlap(scan, pump) -> float:
    """Share of the scan's time span that lies inside the pump log."""
    s0, s1, p0, p1 = scan["t0"], scan["t1"], pump["t0"], pump["t1"]
    if None in (s0, s1, p0, p1):
        return 0.0
    if s1 <= s0:
        return float(p0 <= s0 <= p1)
    return max(0, min(s1, p1) - max(s0, p0)) / (s1 - s0)


def _local_date(ns: int | None) -> date | None:
    return datetime.fromtimestamp(ns / 1e9, tz=LOCAL_TZ).date() if ns else None


def plan(pump_dirs, scan_dirs, workers: int = 8):
    """-> ({experiment_id: spec}, skipped files, summary counts)."""
    pumps, sk1 = _sources(pump_dirs, ["*.csv"], "pump")
    scans, sk2 = _sources(scan_dirs, ["*.json", "*_scan.pkl"], "scan")
    skipped = sk1 + sk2

    def probe(src):
        fn = pump_time_span if src["kind"] == "pump" else scan_time_span
        try:
            src["t0"], src["t1"] = fn(src["path"])
        except Exception:
            src["t0"] = src["t1"] = None
        return src

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(probe, pumps + scans))

    # --- pair: a name match unless the time says otherwise, else by time
    by_sid = {p["sid"]: p for p in pumps}
    claims = defaultdict(list)                   # pump sid -> [(score, scan, how, ov)]
    for s in scans:
        named = by_sid.get(s["sid"])
        cands = [(p, _overlap(s, p)) for p in pumps]
        best = max(cands, key=lambda c: c[1]) if cands else (None, 0.0)
        named_ov = _overlap(s, named) if named else 0.0
        if named and (named_ov >= PAIR_MIN_OVERLAP or best[1] < PAIR_MIN_OVERLAP):
            pick, how, ov = named, "name", named_ov
        elif best[0] is not None and best[1] >= PAIR_MIN_OVERLAP:
            pick, how, ov = best[0], "time", best[1]
        else:
            pick = None
        if pick is not None:
            score = (s["fmt"] == "json", ov)     # original JSON beats a pkl copy
            claims[pick["sid"]].append((score, s, how, ov))
    pair = {}                                     # pump sid -> (scan, how, ov)
    paired_scans = set()
    for psid, cl in claims.items():
        cl.sort(key=lambda c: c[0], reverse=True)
        _, s, how, ov = cl[0]
        pair[psid] = (s, how, ov)
        paired_scans.add(id(s))
        for _, other, _, _ in cl[1:]:
            # a pkl copy of the winning scan is a duplicate; any other scan
            # stays as its own (scan-only) experiment rather than being lost
            if other["fmt"] == "pkl" and _overlap(other, s) >= PAIR_MIN_OVERLAP:
                skipped.append((str(other["path"]),
                                f"duplicate of scan {s['path'].name} (same print)"))
                paired_scans.add(id(other))

    # --- experiments, dated by their recorded time
    entries = []
    for p in pumps:
        s, how, ov = pair.get(p["sid"], (None, None, None))
        entries.append((p, s, how))
    for s in scans:
        if id(s) not in paired_scans:
            entries.append((None, s, None))
    exps, taken = {}, set()
    renamed = 0
    for p, s, how in sorted(entries, key=lambda e: (e[0] or e[1])["sid"]):
        anchor = p or s
        t0 = (p or {}).get("t0") or (s or {}).get("t0")
        recorded = _local_date(t0)
        sid = anchor["sid"]
        m = _DATE_START.match(sid)
        name_date = date.fromisoformat(m[1]) if m else None
        eid = sid
        if recorded and m and recorded != name_date:
            eid = recorded.isoformat() + sid[10:]
            renamed += 1
        elif recorded and not m:
            eid = f"{recorded.isoformat()}__{sid}"
        base, n = eid, 2
        while eid in taken:
            eid, n = f"{base}__{n}", n + 1
        taken.add(eid)
        spec = {"name_stem": anchor["sid"], "pairing": how,
                "recorded_date": recorded.isoformat() if recorded else None}
        if p:
            spec.update(pump=p["path"], pump_rel=p["rel"])
        if s:
            spec.update(scan=s["path"], scan_rel=s["rel"])
        exps[eid] = spec

    counts = {"experiments": len(exps),
              "pump+scan": sum("pump" in e and "scan" in e for e in exps.values()),
              "paired by time": sum(e["pairing"] == "time" for e in exps.values()),
              "pump only": sum("pump" in e and "scan" not in e for e in exps.values()),
              "scan only": sum("scan" in e and "pump" not in e for e in exps.values()),
              "renamed to recorded date": renamed}
    return exps, skipped, counts


# ================================================================== worker
def _rel(path: Path, data_dir: Path) -> str:
    try:
        return Path(os.path.relpath(path, data_dir)).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _utc(ns: int | None):
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc) if ns else None


def scan_quality(path: Path, max_profiles: int = 2000) -> dict:
    """Orientation and emptiness from a sample of a scan file's profiles
    (evenly spread row groups), plus the C spread of the scanner head."""
    f = pq.ParquetFile(path)
    k = f.num_row_groups
    rgs = sorted({round(i * (k - 1) / 7) for i in range(8)}) if k > 1 else [0]
    t = f.read_row_groups(rgs, columns=["profile_x", "profile_z", "head_c"])
    step = max(1, t.num_rows // max_profiles)
    s = t.take(list(range(0, t.num_rows, step)))         # thin before to_pylist
    pairs = list(zip(s.column("profile_x").to_pylist(), s.column("profile_z").to_pylist()))
    empty = sum(1 for x, z in pairs if x is None or z is None or not any(z))
    orient, share, votes = geometry.detect_orientation(
        (x, z) for x, z in pairs if x is not None and z is not None)
    head_c = [c for c in t.column("head_c").to_pylist() if c is not None]
    return dict(scan_orientation=orient, scan_orientation_share=share,
                scan_orientation_votes=votes,
                scan_orientation_source="profiles" if orient else None,
                scan_empty_share=empty / len(pairs) if pairs else None,
                _head_c=head_c)


def motion_c_rotates(fct_motion) -> bool | None:
    if fct_motion is None or not fct_motion.num_rows:
        return None
    m = fct_motion.select(["pos_c", "vel_x", "vel_y"]).to_pandas()
    moving = (m.vel_x.fillna(0) ** 2 + m.vel_y.fillna(0) ** 2) > 0
    return geometry.c_axis_rotates(m.pos_c.to_numpy(dtype=float), moving.to_numpy())


def classify_run(fct_pump, fct_motion, era, heads, duration_s, has_pump) -> dict:
    sig = run_type.signature(fct_pump, fct_motion, era, heads)
    rt, why = run_type.classify(sig, (duration_s or 0) / 60, has_pump)
    return dict(run_type=rt, run_type_reason=why, run_type_source="signals",
                print_minutes=round(sig["print_minutes"], 2),
                printed_layers=int(sig["printed_layers"]))


def backfill_run_type(row: dict, data_dir: str) -> dict:
    """run_type columns for an existing dim row, from its Parquet files."""
    d = Path(data_dir)
    get = lambda c: pq.read_table(d / row[c]) if row.get(c) else None
    try:
        fp, fm = get("pump_path"), get("motion_path")
        heads = (pq.read_table(d / row["scan_path"],
                               columns=["event_time", "head_x", "head_y", "head_z"])
                 if fp is None and row.get("scan_path") else None)
        return dict(experiment_id=row["experiment_id"],
                    **classify_run(fp, fm, row.get("sensor_era"), heads,
                                   row.get("duration_s"), fp is not None))
    except Exception as e:
        return dict(experiment_id=row["experiment_id"], run_type=None,
                    run_type_reason=f"error: {type(e).__name__}: {e}")


def process_experiment(eid: str, spec: dict, data_dir: str) -> dict:
    """Extract + transform + write the Parquet files of one experiment and
    return its dim_experiment row. Never raises: failures go to ingest_error."""
    t_start = time.time()
    data_dir = Path(data_dir)
    rec = dict.fromkeys(DIM_INSERT_COLUMNS)
    meta = parse_experiment_filename(Path(spec["name_stem"] + ".csv"))
    notes: list[str] = []
    rec.update(
        experiment_id=eid, board=meta.board, run=meta.run,
        feed_nominal=meta.feed, set_speed_nominal=meta.set_speed,
        w_nominal=meta.width, name_parse_ok=meta.parse_ok,
        name_warnings=";".join(meta.parse_warnings) or None,
        name_extra=";".join(f"{k}={v}" for k, v in meta.extra.items()) or None,
        source_name_date=meta.exp_date.isoformat() if meta.exp_date else None,
        experiment_date=spec.get("recorded_date")
        or (meta.exp_date.isoformat() if meta.exp_date else None),
        pairing=spec.get("pairing"),
        shape=shape_family(meta.board), shape_source="name",
    )
    if (rec["source_name_date"] and rec["experiment_date"]
            and rec["source_name_date"] != rec["experiment_date"]):
        notes.append(f"recorded {rec['experiment_date']}, file name says "
                     f"{rec['source_name_date']}")
    if spec.get("pairing") == "time":
        notes.append(f"scan paired by time: {Path(spec['scan_rel']).name}")
    errors: list[str] = []
    spans: list[tuple[int, int]] = []
    pump_tables = None
    p_span = s_span = None
    staging = None

    if "pump" in spec:
        src = Path(spec["pump"])
        rec.update(source_pump_file=spec["pump_rel"], source_pump_bytes=src.stat().st_size)
        try:
            raw, man = convert_pump_csv(src)
            fct_pump, warns = to_fct_pump(raw, eid)
            motion_full, fct_motion = to_fct_motion(raw, eid)
            del raw
            notes += warns
            era = detect_era(man["columns"], fct_pump)
            rec.update(experiment_stats(fct_pump, motion_full, era))
            del motion_full
            widths = man["array_widths"]
            rec.update(
                pump_rows=fct_pump.num_rows,
                motion_rows=fct_motion.num_rows if fct_motion is not None else None,
                pump_header=man["header_source"],
                source_time_unit=man["ts_unit"],
                pump_channels=",".join(
                    [c for c in man["columns"] if not _AXIS_COL.match(c)]
                    + [f"{k}[{w}]" for k, w in widths.items()]),
                n_pos_axes=widths.get("pos", 0), n_vel_axes=widths.get("vel", 0),
                pump_rate_hz=median_rate_hz(fct_pump["event_time"]),
                motion_rate_hz=motion_update_rate_hz(fct_motion),
            )
            pump_tables = (fct_pump, fct_motion)
            rec["c_axis_rotates"] = motion_c_rotates(fct_motion)
            if man["time_min"] is not None:
                p_span = (man["time_min"], man["time_max"])
                spans.append(p_span)
        except Exception as e:
            errors.append(f"pump: {type(e).__name__}: {e}")

    if "scan" in spec:
        src = Path(spec["scan"])
        rec.update(source_scan_file=spec["scan_rel"], source_scan_bytes=src.stat().st_size)
        staging = data_dir / "_staging" / f"{eid}.scan.parquet"
        try:
            sman = convert_scan_json(src, staging, eid)
            rec.update(scan_rows=sman["rows"], scan_layout=sman["layout"],
                       scan_channels=",".join(sman["scan_channels"]) or None,
                       profile_points=sman["profile_points"],
                       scan_rate_hz=sman["rate_hz"])
            rec["source_time_unit"] = rec["source_time_unit"] or sman["ts_unit"]
            if sman["bad_lines"]:
                notes.append(f"scan: {sman['bad_lines']} unreadable lines skipped")
            if sman["extras"]:
                notes.append("scan extra columns: " + ",".join(sman["extras"]))
            if sman["time_min"] is not None:
                s_span = (sman["time_min"], sman["time_max"])
                spans.append(s_span)
            if not sman["rows"]:
                notes.append("scan file has no profiles")
                staging = None
            else:
                q = scan_quality(staging)
                head_c = q.pop("_head_c")
                rec.update(q)
                if rec["c_axis_rotates"] is None and head_c:     # scan only
                    rec["c_axis_rotates"] = geometry.c_axis_rotates(
                        head_c, [True] * len(head_c))
        except Exception as e:
            errors.append(f"scan: {type(e).__name__}: {e}")
            staging = None

    if spans:
        t0, t1 = min(s[0] for s in spans), max(s[1] for s in spans)
        rec.update(start_time=_utc(t0), end_time=_utc(t1), duration_s=(t1 - t0) / 1e9)
        if rec["experiment_date"] is None:
            rec["experiment_date"] = _local_date(t0).isoformat()
            notes.append("date taken from data")
    if p_span and s_span:
        span = s_span[1] - s_span[0]
        inter = max(0, min(s_span[1], p_span[1]) - max(s_span[0], p_span[0]))
        rec["scan_pump_overlap"] = (inter / span if span > 0
                                    else float(p_span[0] <= s_span[0] <= p_span[1]))

    try:                                        # real print or not, from the signals
        heads = None
        if pump_tables is None and staging is not None and staging.exists():
            heads = pq.read_table(staging, columns=["event_time", "head_x", "head_y", "head_z"])
        fp, fm = pump_tables if pump_tables is not None else (None, None)
        rec.update(classify_run(fp, fm, rec.get("sensor_era"), heads,
                                rec.get("duration_s"), pump_tables is not None))
    except Exception as e:
        notes.append(f"run type not determined: {type(e).__name__}: {e}")

    part = f"experiment_date={rec['experiment_date'] or 'unknown'}"
    try:
        if pump_tables is not None:
            fct_pump, fct_motion = pump_tables
            dest = data_dir / "pump" / part / f"{eid}.parquet"
            write_parquet(fct_pump, dest, row_group_size=100_000,
                          metadata=file_metadata("fct_pump", eid, spec["pump_rel"]))
            rec["pump_path"] = _rel(dest, data_dir)
            if fct_motion is not None and fct_motion.num_rows:
                dest = data_dir / "motion" / part / f"{eid}.parquet"
                write_parquet(fct_motion, dest, row_group_size=100_000,
                              metadata=file_metadata("fct_motion", eid, spec["pump_rel"]))
                rec["motion_path"] = _rel(dest, data_dir)
        if staging is not None and staging.exists():
            dest = data_dir / "scan" / part / f"{eid}.parquet"
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, dest)
            rec["scan_path"] = _rel(dest, data_dir)
    except Exception as e:
        errors.append(f"write: {type(e).__name__}: {e}")
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)

    rec["ingest_notes"] = "; ".join(notes) or None
    rec["ingest_error"] = "; ".join(errors) or None
    rec["model_version"] = MODEL_VERSION
    rec["_seconds"] = round(time.time() - t_start, 1)
    return rec


# ================================================================== load
_ARROW_DIM = {"VARCHAR": pa.string(), "DATE": pa.date32(), "BIGINT": pa.int64(),
              "DOUBLE": pa.float64(), "BOOLEAN": pa.bool_(),
              "TIMESTAMPTZ": pa.timestamp("us", tz="UTC")}


def dim_schema() -> pa.Schema:
    return pa.schema([pa.field(n, _ARROW_DIM[t.split()[0]]) for n, t, _ in DIM_EXPERIMENT])


def _as_date(v):
    return date.fromisoformat(v) if isinstance(v, str) else v


def write_dim(rows: list[dict], data_dir: Path) -> None:
    now = datetime.now(timezone.utc)
    cols = {}
    for f in dim_schema():
        vals = [r.get(f.name) for r in rows]
        if f.name in ("experiment_date", "source_name_date"):
            vals = [_as_date(v) for v in vals]
        if f.name == "ingested_at":
            vals = [v or now for v in vals]
        cols[f.name] = pa.array(vals, type=f.type)
    table = pa.table(cols, schema=dim_schema()).sort_by("experiment_id")
    write_parquet(table, data_dir / DIM_FILE)


def read_dim(data_dir: Path) -> list[dict]:
    p = data_dir / DIM_FILE
    return pq.read_table(p).to_pylist() if p.exists() else []


def write_dictionary(data_dir: Path) -> None:
    rows = [("dim_experiment", n, t.split()[0], d) for n, t, d in DIM_EXPERIMENT]
    for ds, cols in DATASETS.items():
        rows += [(FACT_VIEWS[ds], n, str(t), d) for n, t, d in cols]
        rows.append((FACT_VIEWS[ds], "experiment_date", "date32",
                     "Partition key, same as dim_experiment.experiment_date"))
    rows += [(lab_samples.LAB_VIEW, n, str(t), d) for n, t, _, d in lab_samples.LAB_SAMPLE]
    table = pa.table({k: [r[i] for r in rows] for i, k in enumerate(
        ("table_name", "column_name", "data_type", "description"))})
    write_parquet(table, data_dir / DICT_FILE)


def write_lab(lab_dirs, data_dir: Path) -> None:
    """Lab samples (spread, density, air, timed weighings) -> lab_samples.parquet."""
    import pandas as pd
    dim = pd.DataFrame(read_dim(data_dir))
    tbl, notes = lab_samples.build(lab_dirs, dim)
    for n in notes:
        print(f"  lab: {n}", flush=True)
    if tbl is None:
        return
    write_parquet(tbl, data_dir / lab_samples.LAB_FILE)
    weighed = sum(v is not None for v in tbl["mass_kg"].to_pylist())
    print(f"lab samples: {tbl.num_rows} from {len(set(tbl['experiment_id'].to_pylist()))} "
          f"experiments ({weighed} weighed)", flush=True)


def _q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def build_catalog(data_dir: Path) -> None:
    """(Re)build catalog.duckdb from the Parquet outputs: the dimension table,
    fct_* views over the datasets and the data dictionary as comments."""
    con = duckdb.connect(str(data_dir / "catalog.duckdb"))
    try:
        for t in ("experiments", "dim_experiment"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
        ddl = ",\n    ".join(f"{n} {t}" for n, t, _ in DIM_EXPERIMENT)
        con.execute(f"CREATE TABLE dim_experiment (\n    {ddl}\n)")
        cols = ", ".join(DIM_INSERT_COLUMNS + ["ingested_at"])
        con.execute(f"INSERT INTO dim_experiment ({cols}) SELECT {cols} FROM "
                    f"read_parquet({_q((data_dir / DIM_FILE).resolve().as_posix())})")
        for ds, view in FACT_VIEWS.items():
            folder = data_dir / ds
            if not (folder.exists() and any(folder.rglob("*.parquet"))):
                con.execute(f"DROP VIEW IF EXISTS {view}")
                continue
            glob = folder.resolve().as_posix() + "/**/*.parquet"
            con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet("
                        f"{_q(glob)}, hive_partitioning = true, union_by_name = true)")
            con.execute(f"COMMENT ON VIEW {view} IS {_q(FACT_GRAIN[ds])}")
            for name, _, desc in DATASETS[ds]:
                con.execute(f"COMMENT ON COLUMN {view}.{name} IS {_q(desc)}")
            con.execute(f"COMMENT ON COLUMN {view}.experiment_date IS "
                        f"{_q('Partition key, same as dim_experiment.experiment_date')}")
        con.execute("COMMENT ON TABLE dim_experiment IS " + _q(
            "One row per experiment: recorded date, provenance, pairing, time span, "
            "sampling rates and calibrated observed ranges"))
        for name, _, desc in DIM_EXPERIMENT:
            con.execute(f"COMMENT ON COLUMN dim_experiment.{name} IS {_q(desc)}")
        lab = data_dir / lab_samples.LAB_FILE
        if lab.exists():
            v = lab_samples.LAB_VIEW
            con.execute(f"CREATE OR REPLACE VIEW {v} AS SELECT * FROM read_parquet("
                        f"{_q(lab.resolve().as_posix())})")
            con.execute(f"COMMENT ON VIEW {v} IS {_q(lab_samples.LAB_GRAIN)}")
            for name, _, _, desc in lab_samples.LAB_SAMPLE:
                con.execute(f"COMMENT ON COLUMN {v}.{name} IS {_q(desc)}")
        con.execute("""
            CREATE OR REPLACE VIEW print_catalog AS
            SELECT * FROM dim_experiment
            WHERE run_type IS DISTINCT FROM 'not a print'""")
        con.execute("COMMENT ON VIEW print_catalog IS " + _q(
            "dim_experiment without the experiments that are not real prints "
            "(dry runs, levelling, pump tests); unsure rows stay until decided"))
        con.execute("""
            CREATE OR REPLACE VIEW data_dictionary AS
            SELECT table_name, column_name, data_type, comment AS description
            FROM duckdb_columns()
            WHERE table_name IN ('dim_experiment', 'fct_pump', 'fct_motion', 'fct_scan',
                                 'fct_lab_sample')
            ORDER BY table_name, column_index""")
    finally:
        con.close()


def try_build_catalog(data_dir: Path) -> bool:
    try:
        build_catalog(data_dir)
        return True
    except duckdb.IOException as e:
        holder = re.search(r"open in\s*(.+?)\)?$", str(e).strip())
        print("WARNING catalog.duckdb not updated: it is open in another program"
              + (f" ({holder[1].strip()})" if holder else "")
              + ". The Parquet outputs are complete; close that connection (or "
                "open it read-only) and run with --catalog-only.", flush=True)
        return False


# ================================================================== main
def inherit_orientation(rows: list[dict]) -> None:
    """Scans that showed no bead (levelling, flat-plate scans) take the
    orientation of the nearest-in-time scan of the same recorded day: the
    scanner settings are a per-session thing."""
    known = [r for r in rows if r.get("scan_path")
             and r.get("scan_orientation_source") == "profiles"]
    for r in rows:
        if not r.get("scan_path") or r.get("scan_orientation_source") == "profiles":
            continue
        r["scan_orientation"] = r["scan_orientation_source"] = None
        same = [k for k in known if str(k.get("experiment_date")) == str(r.get("experiment_date"))
                and k.get("start_time") and r.get("start_time")]
        if not same:
            continue
        t = r["start_time"]
        best = min(same, key=lambda k: abs((k["start_time"] - t).total_seconds()))
        r["scan_orientation"] = best["scan_orientation"]
        r["scan_orientation_source"] = "same day"
        note = f"scan orientation from {best['experiment_id']} (same day, no bead seen)"
        r["ingest_notes"] = "; ".join(x for x in (r.get("ingest_notes"), note) if x)


def _interleave(order: list, workers: int) -> list:
    """Largest jobs first, but spaced out so the biggest pump logs (parsed in
    memory) don't all run at the same time."""
    heavy, light = order[:workers * 2], order[workers * 2:]
    out = []
    for h in heavy:
        out.append(h)
        out += light[:workers - 1]
        light = light[workers - 1:]
    return out + light


def _source_bytes(spec: dict) -> int:
    return sum(Path(spec[k]).stat().st_size for k in ("pump", "scan") if k in spec)


def _fmt_row(i: int, n: int, rec: dict, secs) -> str:
    def num(k):
        v = rec.get(k)
        return f"{v:>9,}" if v else "        -"
    line = (f"[{i:>3}/{n}] {rec['experiment_id'][:52]:52s} pump{num('pump_rows')} "
            f"motion{num('motion_rows')} scan{num('scan_rows')} {secs:>6}s")
    if rec.get("ingest_error"):
        line += f"   ERROR {rec['ingest_error'][:120]}"
    return line


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m ingest.run_ingest",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("--pump-dir", type=Path, action="append")
    ap.add_argument("--scan-dir", type=Path, action="append")
    ap.add_argument("--out", type=Path, default=Path("data"))
    ap.add_argument("--workers", type=int,
                    default=max(1, min(8, (os.cpu_count() or 2) - 2)))
    ap.add_argument("--incremental", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--catalog-only", action="store_true")
    ap.add_argument("--lab-dir", type=Path, action="append",
                    help="folder(s) with lab sample sheets and mass files (xlsx / csv)")
    ap.add_argument("--lab-only", action="store_true",
                    help="only (re)build lab_samples.parquet from --lab-dir, then the catalog")
    ap.add_argument("--backfill-run-type", action="store_true",
                    help="(re)compute run_type for every row from the Parquet files "
                         "already written, apply run_type_overrides.csv, rewrite the "
                         "dimension (no source files needed)")
    args = ap.parse_args(argv)
    data_dir = args.out.resolve()

    if args.lab_only:
        if not args.lab_dir:
            ap.error("--lab-only needs --lab-dir")
        write_lab(args.lab_dir, data_dir)
        write_dictionary(data_dir)
        if try_build_catalog(data_dir):
            print(f"catalog rebuilt: {data_dir / 'catalog.duckdb'}")
        return
    if args.backfill_run_type:
        rows = read_dim(data_dir)
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            res = {r["experiment_id"]: r for r in
                   ex.map(backfill_run_type, rows, [str(data_dir)] * len(rows))}
        for r in rows:
            r.update(res.get(r["experiment_id"], {}))
            r.update(shape=shape_family(r.get("board")), shape_source="name")
            if r.get("run_type"):
                r["model_version"] = MODEL_VERSION
        n_manual = run_type.apply_overrides(rows)
        n_shape = run_type.apply_shape_overrides(rows)
        print(f"shape: {n_shape} set by hand", flush=True)
        write_dim(rows, data_dir)
        write_dictionary(data_dir)
        counts = {k: sum(r.get("run_type") == k for r in rows) for k in run_type.RUN_TYPES}
        print("run_type: " + ", ".join(f"{v} {k}" for k, v in counts.items())
              + f" ({n_manual} set by hand)")
        if try_build_catalog(data_dir):
            print(f"catalog rebuilt: {data_dir / 'catalog.duckdb'}")
        return

    if args.catalog_only:
        if try_build_catalog(data_dir):
            print(f"catalog rebuilt: {data_dir / 'catalog.duckdb'}")
        return
    if not (args.pump_dir or args.scan_dir):
        ap.error("give --pump-dir and/or --scan-dir")

    t_run = time.time()
    exps, skipped, counts = plan(args.pump_dir, args.scan_dir, args.workers)
    print("plan: " + ", ".join(f"{v} {k}" for k, v in counts.items())
          + f"; {len(skipped)} files skipped", flush=True)
    for path, why in skipped:
        print(f"  skipped {Path(path).name}: {why}", flush=True)
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        exps = {k: v for k, v in exps.items() if k in wanted}
    if args.dry_run:
        for eid, spec in sorted(exps.items()):
            if spec.get("pairing") == "time" or not eid.startswith(spec["name_stem"][:10]):
                print(f"  {eid:56s} <- {spec['name_stem'][:40]}"
                      + (f"  + scan {Path(spec['scan_rel']).name} (by time)"
                         if spec.get("pairing") == "time" else ""))
        return

    if args.rebuild:
        for ds in DATASETS:
            shutil.rmtree(data_dir / ds, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    prev = {} if args.rebuild else {r["experiment_id"]: r for r in read_dim(data_dir)}

    todo = dict(exps)
    if args.incremental:
        def unchanged(eid, spec):
            p = prev.get(eid)
            if not p or p["ingest_error"] or p.get("model_version") != MODEL_VERSION:
                return False
            same = (p["source_pump_bytes"] == (Path(spec["pump"]).stat().st_size
                                               if "pump" in spec else None)
                    and p["source_scan_bytes"] == (Path(spec["scan"]).stat().st_size
                                                   if "scan" in spec else None)
                    and p.get("source_scan_file") == spec.get("scan_rel")
                    and p.get("pairing") == spec.get("pairing"))
            return same and all((data_dir / p[c]).exists()
                                for c in PATH_COLUMNS if p[c])
        todo = {k: v for k, v in exps.items() if not unchanged(k, v)}
        print(f"incremental: {len(exps) - len(todo)} unchanged, "
              f"{len(todo)} to ingest", flush=True)

    order = _interleave(sorted(todo, key=lambda k: -_source_bytes(todo[k])),
                        args.workers)
    rows = {k: v for k, v in prev.items() if k in exps and k not in todo}
    report, failed_pool, done = [], [], 0

    def finish(rec: dict) -> None:
        nonlocal done
        done += 1
        secs = rec.pop("_seconds", None)
        old = prev.get(rec["experiment_id"], {})
        for c in PATH_COLUMNS:                     # file moved or stream gone
            if old.get(c) and old[c] != rec.get(c):
                (data_dir / old[c]).unlink(missing_ok=True)
        rows[rec["experiment_id"]] = rec
        report.append({"experiment_id": rec["experiment_id"], "seconds": secs,
                       "pump_rows": rec.get("pump_rows"),
                       "motion_rows": rec.get("motion_rows"),
                       "scan_rows": rec.get("scan_rows"),
                       "notes": rec.get("ingest_notes"),
                       "error": rec.get("ingest_error")})
        print(_fmt_row(done, len(order), rec, secs), flush=True)

    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_experiment, eid, todo[eid], str(data_dir)): eid
                for eid in order}
        for fut in as_completed(futs):
            try:
                finish(fut.result())
            except BrokenProcessPool:
                failed_pool.append(futs[fut])
    for eid in failed_pool:                     # a worker died (e.g. out of memory)
        finish(process_experiment(eid, todo[eid], str(data_dir)))
    shutil.rmtree(data_dir / "_staging", ignore_errors=True)

    if not args.only:                           # experiments that no longer exist
        for eid, old in prev.items():
            if eid not in exps:
                for c in PATH_COLUMNS:
                    if old.get(c):
                        (data_dir / old[c]).unlink(missing_ok=True)
    else:
        rows.update({k: v for k, v in prev.items() if k not in rows})

    final = list(rows.values())
    inherit_orientation(final)
    run_type.apply_overrides(final)
    run_type.apply_shape_overrides(final)
    write_dim(final, data_dir)
    write_dictionary(data_dir)
    if args.lab_dir:
        write_lab(args.lab_dir, data_dir)
    catalog_ok = try_build_catalog(data_dir)

    referenced = {r[c] for r in final for c in PATH_COLUMNS if r.get(c)}
    sizes, orphans = {}, []
    for ds in DATASETS:
        files = list((data_dir / ds).rglob("*.parquet")) if (data_dir / ds).exists() else []
        sizes[ds] = sum(f.stat().st_size for f in files)
        orphans += [_rel(f, data_dir) for f in files if _rel(f, data_dir) not in referenced]
    (data_dir / "ingest_report.json").write_text(json.dumps({
        "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(time.time() - t_run, 1), "workers": args.workers,
        "plan": counts, "catalog_updated": catalog_ok,
        "skipped_files": [{"file": p, "reason": r} for p, r in skipped],
        "orphan_files": orphans, "experiments": report}, indent=1, default=str))

    n_err = sum(1 for r in final if r.get("ingest_error"))
    tot = lambda k: sum(r.get(k) or 0 for r in final)
    print(f"\ndim_experiment: {len(final)} experiments, {n_err} with errors | rows: "
          f"pump {tot('pump_rows'):,}  motion {tot('motion_rows'):,}  "
          f"scan {tot('scan_rows'):,}")
    print("parquet on disk: " + "  ".join(f"{ds} {b / 1e9:.2f} GB"
                                          for ds, b in sizes.items()))
    if orphans:
        print(f"WARNING {len(orphans)} Parquet files not referenced by "
              f"dim_experiment (see ingest_report.json)")
    print(f"done in {time.time() - t_run:.0f}s; report: {data_dir / 'ingest_report.json'}")


if __name__ == "__main__":
    main()
