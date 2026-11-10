"""Replay -> recorder -> Parquet -> DuckDB, end to end on a synthetic run."""

import time
from itertools import pairwise

import pyarrow.dataset as ds
import pytest

from printmon.recorder import Recorder, record
from printmon.sources import ReplaySource, Sink, discover_runs, find_run, run_time_bounds


class Collect(Sink):
    def __init__(self):
        self.process, self.scan = [], []

    def on_process(self, rec):
        self.process.append(rec)

    def on_scan(self, rec):
        self.scan.append(rec)


def test_discover_and_bounds(synthetic_run):
    data, name = synthetic_run
    runs = discover_runs(data)
    assert [r.name for r in runs] == [name]
    assert runs[0].streams == ["process", "scan"]
    first, last = run_time_bounds(runs[0])
    assert (last - first) / 1e9 == pytest.approx(1.98, abs=0.01)
    with pytest.raises(FileNotFoundError):
        find_run("nope", data)


def test_replay_merges_streams_in_time_order_and_keeps_timing(synthetic_run):
    data, _ = synthetic_run
    sink = Collect()
    src = ReplaySource(sink, find_run("synthetic", data), speed=4.0, loop=False)
    t0 = time.perf_counter()
    src.start()
    src.join(10)
    wall = time.perf_counter() - t0
    assert len(sink.process) == 100 and len(sink.scan) == 80
    merged = sorted([r["t"] for r in sink.process] + [r["t"] for r in sink.scan])
    assert merged[0] == sink.process[0]["t"]
    assert wall == pytest.approx(1.98 / 4.0, abs=0.25)  # original timing, scaled


def test_replay_loop_keeps_time_monotonic(synthetic_run):
    data, _ = synthetic_run
    sink = Collect()
    src = ReplaySource(sink, find_run("synthetic", data), speed=50.0, loop=True)
    src.start()
    deadline = time.time() + 10
    while src.passes < 2 and time.time() < deadline:
        time.sleep(0.02)
    src.stop()
    ts = [r["t"] for r in sink.process]
    assert src.passes >= 2 and len(ts) >= 200
    assert all(b > a for a, b in pairwise(ts))


def test_recorder_writes_complete_queryable_parquet(synthetic_run, tmp_path):
    data, _ = synthetic_run
    lake = tmp_path / "lake"
    run = find_run("synthetic", data)
    # Rotation is checked at flush time: a 1 s replay with 0.2 s flushes and 0.25 s files
    # guarantees several rotations.
    rec = Recorder(lake, "replay", "testsession", meta={"replay_run": run.name}, rotate_s=0.25)
    record(lambda sink: ReplaySource(sink, run, speed=2.0, loop=False), rec,
           flush_s=0.2, report_s=60)

    files = list((lake / "bronze").rglob("*.parquet"))
    assert files and not list((lake / "bronze").rglob("*.tmp"))  # all files closed
    assert len(rec.process.files) > 1  # rotation happened
    assert (lake / "bronze" / "_sessions" / "testsession.json").exists()

    proc = ds.dataset(lake / "bronze" / "process", format="parquet",
                      partitioning="hive").to_table()
    scan = ds.dataset(lake / "bronze" / "scan", format="parquet", partitioning="hive").to_table()
    assert proc.num_rows == 100 and scan.num_rows == 80
    assert set(proc.column("origin").to_pylist()) == {"replay"}
    row = scan.slice(0, 1).to_pylist()[0]
    assert len(row["x"]) == 320 and len(row["m0"]) == 320 and row["width"] == pytest.approx(34.0)

    duckdb = pytest.importorskip("duckdb")
    n, span = duckdb.sql(
        "SELECT count(*), epoch(max(t)) - epoch(min(t)) FROM read_parquet('{}', "
        "hive_partitioning = true)".format(
            (lake / "bronze" / "process" / "**" / "*.parquet").as_posix())).fetchone()
    assert n == 100 and span == pytest.approx(1.98, abs=0.01)
