"""
Recorder: full-rate, lossless storage of everything the acquisition services publish.

    python -m printmon record                         live, until Ctrl+C
    python -m printmon record --sim --run myrun --speed 20 --duration 60

Bronze layer -- raw, append-only, one row per frame as received:

    lake/bronze/process/origin=live/date=2026-09-22/<session>_00000.parquet
    lake/bronze/scan/origin=live/date=2026-09-22/<session>_00000.parquet
    lake/bronze/_sessions/<session>.json        what, when, from where, how much

Design choices
    Parquet + zstd      columnar, compressed, readable by DuckDB/Polars/Spark/pandas
    typed columns       queryable immediately; unknown keys kept in `extra` (JSON),
                        so schema drift upstream never loses data
    hive partitions     stream / origin / date -- replayed test data can never
                        mix with lab data by accident
    60 s file rotation  a crash loses at most the open file; files are written
                        as .tmp and renamed on close, so readers never see a
                        half-written file
    5 s row groups      buffered in memory, one row group per flush
"""

import argparse
import json
import os
import platform
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import __version__, config
from .schema import ACTUATOR_FIELDS, GANTRY_FIELDS, PUMP_FIELDS
from .sources import LiveSource, ReplaySource, Sink, find_run

TS = pa.timestamp("ns", tz="UTC")

PROCESS_SCHEMA = pa.schema(
    [("t", TS), ("recv_t", TS), ("session", pa.string()), ("src", pa.string()),
     ("seq", pa.int64())]
    + [(k, pa.float64()) for k in PUMP_FIELDS + GANTRY_FIELDS]
    + [(k, pa.bool_()) for k in ACTUATOR_FIELDS]
    + [("pos", pa.list_(pa.float64())), ("vel", pa.list_(pa.float64())),
       ("start", pa.bool_()), ("set_speed", pa.float64()), ("water_valve", pa.float64()),
       ("extra", pa.string())]
)

SCAN_SCHEMA = pa.schema([
    ("t", TS), ("recv_t", TS), ("session", pa.string()),
    ("profile_count", pa.int64()), ("dropped", pa.int32()), ("lost_profiles", pa.int32()),
    ("shutter_open", pa.float64()), ("shutter_close", pa.float64()),
    ("width", pa.float64()),
    ("x", pa.list_(pa.float32())), ("z", pa.list_(pa.float32())), ("m0", pa.list_(pa.uint32())),
    ("pos", pa.list_(pa.float64())),
    ("extra", pa.string()),
])

_LIST_COLUMNS = {"x": np.float32, "z": np.float32, "m0": np.uint32}


def _list_array(arrays, dtype, pa_type):
    """Build a list<...> column from numpy arrays without a per-element Python loop."""
    lengths = np.fromiter((0 if a is None else len(a) for a in arrays), dtype=np.int32,
                          count=len(arrays))
    offsets = np.zeros(len(arrays) + 1, dtype=np.int32)
    np.cumsum(lengths, out=offsets[1:])
    parts = [np.asarray(a, dtype=dtype) for a in arrays if a is not None and len(a)]
    values = np.concatenate(parts) if parts else np.empty(0, dtype=dtype)
    return pa.ListArray.from_arrays(pa.array(offsets), pa.array(values, type=pa_type.value_type))


class StreamWriter:
    """Buffers rows for one stream and writes rotating Parquet files."""

    def __init__(self, root, stream, schema, origin, session, rotate_s=60.0):
        self.root = Path(root)
        self.stream = stream
        self.schema = schema
        self.origin = origin
        self.session = session
        self.rotate_s = rotate_s
        self._cols = {n: [] for n in schema.names}
        self._lock = threading.Lock()
        self._writer = None
        self._tmp = None
        self._final = None
        self._opened_at = None
        self._part = 0
        self.rows_written = 0
        self.rows_buffered = 0
        self.files = []

    def append(self, row):
        with self._lock:
            for name, col in self._cols.items():
                col.append(row.get(name))
            self.rows_buffered += 1

    def _path_for(self, t_ns):
        day = datetime.fromtimestamp(t_ns / 1e9, tz=UTC).strftime("%Y-%m-%d")
        d = self.root / "bronze" / self.stream / "origin={}".format(self.origin) / \
            "date={}".format(day)
        d.mkdir(parents=True, exist_ok=True)
        return d / "{}_{:05d}.parquet".format(self.session, self._part)

    def _table(self, cols):
        arrays = []
        for field in self.schema:
            data = cols[field.name]
            if field.name in _LIST_COLUMNS:
                arrays.append(_list_array(data, _LIST_COLUMNS[field.name], field.type))
            else:
                arrays.append(pa.array(data, type=field.type))
        return pa.Table.from_arrays(arrays, schema=self.schema)

    def flush(self, rotate=False):
        with self._lock:
            cols, self._cols = self._cols, {n: [] for n in self.schema.names}
            n = self.rows_buffered
            self.rows_buffered = 0
        if n:
            table = self._table(cols)
            if self._writer is None:
                self._final = self._path_for(cols["t"][0])
                self._tmp = self._final.with_suffix(".parquet.tmp")
                self._writer = pq.ParquetWriter(self._tmp, self.schema, compression="zstd")
                self._opened_at = time.monotonic()
            self._writer.write_table(table, row_group_size=n)
            self.rows_written += n
        if self._writer is not None and (
                rotate or time.monotonic() - self._opened_at >= self.rotate_s):
            self._writer.close()
            os.replace(self._tmp, self._final)
            self.files.append(self._final)
            self._writer = None
            self._part += 1

    def close(self):
        self.flush(rotate=True)


class Recorder(Sink):
    def __init__(self, root, origin, session, meta=None, rotate_s=60.0):
        self.root = Path(root)
        self.origin = origin
        self.session = session
        self.meta = meta or {}
        self.process = StreamWriter(root, "process", PROCESS_SCHEMA, origin, session, rotate_s)
        self.scan = StreamWriter(root, "scan", SCAN_SCHEMA, origin, session, rotate_s)
        self.status = {}
        self.started = datetime.now(UTC)

    def on_process(self, rec):
        row = dict(rec)
        row["recv_t"] = time.time_ns()
        row["session"] = self.session
        row["pos"] = list(rec["pos"]) if rec["pos"] else None
        row["vel"] = list(rec["vel"]) if rec["vel"] else None
        row["extra"] = json.dumps(rec["extra"], separators=(",", ":")) if rec["extra"] else None
        self.process.append(row)

    def on_scan(self, rec):
        row = dict(rec)
        row["recv_t"] = time.time_ns()
        row["session"] = self.session
        row["pos"] = list(rec["pos"]) if rec["pos"] else None
        row["extra"] = json.dumps(rec["extra"], separators=(",", ":")) if rec["extra"] else None
        self.scan.append(row)

    def on_status(self, stream, text):
        if self.status.get(stream) != text:
            self.status[stream] = text
            print("[{}] {}".format(stream, text))

    def write_manifest(self, ended=None):
        d = self.root / "bronze" / "_sessions"
        d.mkdir(parents=True, exist_ok=True)
        doc = {
            "session": self.session,
            "origin": self.origin,
            "started_utc": self.started.isoformat(),
            "ended_utc": ended.isoformat() if ended else None,
            "host": socket.gethostname(),
            "platform": platform.platform(),
            "printmon_version": __version__,
            "streams": {
                w.stream: {"rows": w.rows_written,
                           "files": [str(p.relative_to(self.root)) for p in w.files]}
                for w in (self.process, self.scan)
            },
            **self.meta,
        }
        (d / "{}.json".format(self.session)).write_text(json.dumps(doc, indent=2))

    def flush(self, rotate=False):
        self.process.flush(rotate)
        self.scan.flush(rotate)

    def close(self):
        self.process.close()
        self.scan.close()
        self.write_manifest(ended=datetime.now(UTC))


def record(source_factory, recorder, duration=None, flush_s=5.0, report_s=10.0):
    source = source_factory(recorder)
    source.start()
    recorder.write_manifest()
    t0 = time.monotonic()
    next_flush = t0 + flush_s
    next_report = t0 + report_s
    last = (0, 0, t0)
    try:
        while True:
            time.sleep(0.2)
            now = time.monotonic()
            if now >= next_flush:
                recorder.flush()
                next_flush = now + flush_s
            if now >= next_report:
                p = recorder.process.rows_written + recorder.process.rows_buffered
                s = recorder.scan.rows_written + recorder.scan.rows_buffered
                dt = now - last[2]
                print("process {:>9} rows ({:6.0f}/s)   scan {:>8} rows ({:5.0f}/s)   "
                      "files {}".format(
                    p, (p - last[0]) / dt, s, (s - last[1]) / dt,
                    len(recorder.process.files) + len(recorder.scan.files)))
                last = (p, s, now)
                next_report = now + report_s
            if duration and now - t0 >= duration:
                break
            if not source.is_alive():
                break
    except KeyboardInterrupt:
        print("stopping...")
    finally:
        source.stop()
        recorder.close()
    return recorder


def main(argv=None):
    p = argparse.ArgumentParser(prog="printmon record", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sim", action="store_true", help="record a replayed run (origin=replay)")
    p.add_argument("--run", default="", help="replay: substring of the run name")
    p.add_argument("--speed", type=float, default=1.0, help="replay speed factor")
    p.add_argument("--data", default=None, help="recorded runs directory (default data/)")
    p.add_argument("--lake", default=None, help="storage root (default lake/)")
    p.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    p.add_argument("--rotate", type=float, default=60.0, help="seconds per Parquet file")
    p.add_argument("--label", default="", help="free-text label stored in the manifest")
    opts = p.parse_args(argv)

    root = Path(opts.lake or config.LAKE_DIR)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    meta = {"label": opts.label}
    if opts.sim:
        run = find_run(opts.run, opts.data)
        session = "{}_replay_{}".format(stamp, run.name.replace("__", "_"))
        meta.update({"replay_run": run.name, "replay_speed": opts.speed})

        def factory(sink):
            return ReplaySource(sink, run, speed=opts.speed, loop=False)
        origin = "replay"
    else:
        session = stamp
        meta.update({"inputs_url": config.INPUTS_URL, "scan_url": config.SCAN_URL})

        def factory(sink):
            return LiveSource(sink)
        origin = "live"

    rec = Recorder(root, origin, session, meta=meta, rotate_s=opts.rotate)
    print("recording session {} ({}) -> {}".format(session, origin, root / "bronze"))
    record(factory, rec, duration=opts.duration)
    print("done: process {} rows, scan {} rows, {} files".format(
        rec.process.rows_written, rec.scan.rows_written,
        len(rec.process.files) + len(rec.scan.files)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
