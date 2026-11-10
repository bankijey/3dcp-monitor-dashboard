"""
Data sources. Each runs on its own thread and pushes normalised records to a sink.

    LiveSource    subscribes to the acquisition services' full-rate websockets
    ReplaySource  replays a recorded run from data/ with its original timing

Both deliver identical record types (see schema.py), so a consumer -- the viewer,
the recorder -- works unchanged against the lab or against a simulation.
"""

import asyncio
import csv
import heapq
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import websockets

from . import config
from .schema import peek_scan_time, process_from_csv, process_from_live, scan_from_frame


class Sink:
    """Receiver interface. Called on the source thread -- keep implementations cheap."""

    def on_process(self, rec):
        pass

    def on_scan(self, rec):
        pass

    def on_status(self, stream, text):
        pass


class Source(threading.Thread):
    origin = "?"

    def __init__(self, sink):
        super().__init__(daemon=True)
        self.sink = sink
        self._stop_evt = threading.Event()

    def stop(self, timeout=3.0):
        self._stop_evt.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout)

    @property
    def stopped(self):
        return self._stop_evt.is_set()


# --- live ---------------------------------------------------------------------

class LiveSource(Source):
    origin = "live"

    def __init__(self, sink, inputs_url=None, scan_url=None):
        super().__init__(sink)
        self.urls = {
            "inputs": inputs_url or config.INPUTS_URL,
            "scan": scan_url or config.SCAN_URL,
        }

    @property
    def label(self):
        return "Live"

    def run(self):
        asyncio.run(self._main())

    async def _main(self):
        tasks = [
            asyncio.create_task(self._consume("inputs", self._on_inputs)),
            asyncio.create_task(self._consume("scan", self._on_scan)),
        ]
        while not self.stopped:
            await asyncio.sleep(0.2)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _on_inputs(self, frame):
        self.sink.on_process(process_from_live(frame))

    def _on_scan(self, frame):
        if "x" in frame and "time" in frame:
            self.sink.on_scan(scan_from_frame(frame))

    async def _consume(self, stream, handle):
        url = self.urls[stream]
        backoff = 1.0
        while not self.stopped:
            try:
                async with websockets.connect(url, max_size=None, compression=None,
                                              open_timeout=3, ping_interval=20) as ws:
                    self.sink.on_status(stream, "connected {}".format(url))
                    backoff = 1.0
                    async for msg in ws:
                        try:
                            frame = json.loads(msg)
                        except ValueError:
                            continue  # e.g. an error string from the service
                        if isinstance(frame, dict):
                            handle(frame)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 -- any failure means reconnect
                self.sink.on_status(stream, "waiting for {} ({})".format(
                    url, type(e).__name__))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)


# --- replay -------------------------------------------------------------------

@dataclass(frozen=True)
class Run:
    name: str
    pump: Path = None
    scan: Path = None

    @property
    def streams(self):
        return [s for s, p in (("process", self.pump), ("scan", self.scan)) if p]


def discover_runs(data_dir=None):
    """Recorded runs: data/pump/<name>.csv paired with data/scans/<name>.json."""
    data_dir = Path(data_dir or config.DATA_DIR)
    pumps = {p.stem: p for p in (data_dir / "pump").glob("*.csv")}
    scans = {p.stem: p for p in (data_dir / "scans").glob("*.json")}
    return [Run(n, pumps.get(n), scans.get(n)) for n in sorted(set(pumps) | set(scans))]


def find_run(query, data_dir=None):
    runs = discover_runs(data_dir)
    if not runs:
        raise FileNotFoundError("no recorded runs under {}".format(data_dir or config.DATA_DIR))
    if not query:
        return runs[0]
    matches = [r for r in runs if query in r.name]
    if not matches:
        raise FileNotFoundError("no run matching '{}'. Available: {}".format(
            query, ", ".join(r.name for r in runs)))
    return matches[0]


def _last_line(path, chunk=1 << 16):
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - chunk))
        lines = fh.read().splitlines()
    for line in reversed(lines):
        if line.strip():
            return line.decode("utf-8", "replace")
    return None


def run_time_bounds(run):
    """(first_t, last_t) across a run's files, reading only their ends."""
    bounds = []
    if run.pump:
        with open(run.pump, newline="") as fh:
            rdr = csv.reader(fh)
            header = [h.strip() for h in next(rdr)]
            first = next(rdr, None)
        last = _last_line(run.pump)
        ti = header.index("timestamp")
        if first:
            bounds.append(int(first[ti]))
        if last:
            bounds.append(int(next(csv.reader([last]))[ti]))
    if run.scan:
        with open(run.scan) as fh:
            first = fh.readline()
        last = _last_line(run.scan)
        if first.strip():
            bounds.append(peek_scan_time(first))
        if last:
            bounds.append(peek_scan_time(last))
    return (min(bounds), max(bounds)) if bounds else (None, None)


class ReplaySource(Source):
    """
    Replays a recorded run with its original inter-arrival timing, scaled by `speed`.

    The pump CSV and scan NDJSON are merged by timestamp and streamed from disk --
    runs are up to several hundred MB, so nothing is loaded up front and scan
    lines are only fully parsed when emitted. When looping, timestamps are
    shifted so time keeps increasing across passes.
    """

    origin = "replay"

    def __init__(self, sink, run, speed=1.0, loop=True, start_s=0.0):
        super().__init__(sink)
        self.run_info = run
        self.loop = loop
        self.start_s = float(start_s)
        self._speed = float(speed)
        self._paused = False
        self._lock = threading.Lock()
        self.position_s = 0.0
        self.duration_s = None
        self.passes = 0

    @property
    def label(self):
        return "Simulation: {}".format(self.run_info.name)

    @property
    def speed(self):
        with self._lock:
            return self._speed

    @speed.setter
    def speed(self, value):
        with self._lock:
            self._speed = max(float(value), 0.01)

    def set_paused(self, paused):
        with self._lock:
            self._paused = bool(paused)

    def _events(self):
        gens = []
        if self.run_info.pump:
            gens.append(self._pump_events(self.run_info.pump))
        if self.run_info.scan:
            gens.append(self._scan_events(self.run_info.scan))
        return heapq.merge(*gens, key=lambda e: e[0])

    @staticmethod
    def _pump_events(path):
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    yield int(row["timestamp"]), 0, row
                except (KeyError, TypeError, ValueError):
                    continue

    @staticmethod
    def _scan_events(path):
        with open(path) as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    yield peek_scan_time(line), 1, line
                except (ValueError, KeyError):
                    continue

    def run(self):
        first, last = run_time_bounds(self.run_info)
        if first is None:
            self.sink.on_status("replay", "{}: no data".format(self.run_info.name))
            return
        self.duration_s = (last - first) / 1e9
        shift = 0
        while not self.stopped:
            self.sink.on_status("replay", "playing {} ({:.0f} s, pass {})".format(
                self.run_info.name, self.duration_s, self.passes + 1))
            last_t = self._play_once(first, shift)
            self.passes += 1
            if not self.loop or self.stopped or last_t is None:
                break
            shift += (last_t - first) + 1_000_000_000  # 1 s gap between passes
        self.sink.on_status("replay", "finished {}".format(self.run_info.name))

    def _play_once(self, first, shift):
        start_t = first + int(self.start_s * 1e9)
        anchor_t = None
        anchor_wall = None
        cur_speed = None
        last_t = None
        for t, kind, payload in self._events():
            if self.stopped:
                return None
            if t < start_t:
                continue
            # Pace: wait until this event's due time, re-anchoring on speed/pause changes.
            while True:
                with self._lock:
                    speed, paused = self._speed, self._paused
                if paused:
                    anchor_t = None
                    time.sleep(0.05)
                    if self.stopped:
                        return None
                    continue
                if anchor_t is None or speed != cur_speed:
                    anchor_t, anchor_wall, cur_speed = t, time.perf_counter(), speed
                delay = anchor_wall + (t - anchor_t) / 1e9 / speed - time.perf_counter()
                if delay <= 0:
                    break
                time.sleep(min(delay, 0.05))
                if self.stopped:
                    return None
            try:
                if kind == 0:
                    rec = process_from_csv(payload)
                    rec["t"] += shift
                    self.sink.on_process(rec)
                else:
                    rec = scan_from_frame(json.loads(payload))
                    rec["t"] += shift
                    self.sink.on_scan(rec)
            except (ValueError, KeyError, TypeError):
                continue  # a corrupt line in a recorded file must not stop the replay
            last_t = t
            self.position_s = (t - first) / 1e9
        return last_t
