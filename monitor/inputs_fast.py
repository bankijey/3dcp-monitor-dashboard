"""
Drop-in replacement for inputs.py with the acquisition bottlenecks removed.

What changed, and why:

1. BATCHED READS. inputs.py issues one OPC UA Read request per node
   (13 sequential round-trips per cycle). This sends all nodes of a server in a
   single Read request -- 13 round-trips becomes 1 per server.

2. ACQUISITION DECOUPLED FROM THE WEBSOCKET. A background thread per server
   polls at full speed. Connected clients never trigger OPC traffic and cannot
   throttle acquisition.

3. FAN-OUT TO MANY CONSUMERS. Every frame is serialised once and broadcast.
   Two endpoints on the same port:
       ws://localhost:8001/       latest snapshot, at most LEGACY_MAX_HZ
                                  (Node-RED -- keeps its CSV/file nodes sane)
       ws://localhost:8001/full   every frame, for the recorder and viewer

4. SOURCE TIMESTAMPS KEPT. Per-node SourceTimestamp and StatusCode.
   Note: these servers stamp at *read* time, so *_ts is not a sample time.

5. MEASURED RATE PRINTED every RATE_REPORT_SECONDS per poller.

Payload keys from inputs.py are unchanged, so the existing Node-RED flow keeps
working. New keys are additive:
    <name>_ts      source timestamp, ns since epoch (None if server omits it)
    <name>_ok      False if the StatusCode was not Good
    pos / vel      5-axis actual position / velocity from the NC (10 Hz)
    src            which poller produced this frame: pump | gantry | nc
    pump_hz / gantry_hz / nc_hz    measured acquisition rate
    seq            increments once per frame

Run:  python inputs_fast.py
"""

import asyncio
import calendar
import json
import os
import threading
import time

import websockets
from opcua import Client, ua

from printmon import config
from printmon.broadcast import Broadcaster

# Equipment addresses and node ids come from .env (see .env.example); never hardcoded here.
PUMP_URL = config.PUMP_OPCUA_URL
GANTRY_URL = config.GANTRY_OPCUA_URL
WS_HOST, WS_PORT = config.INPUTS_WS_HOST, config.INPUTS_WS_PORT

# Rate cap for the legacy "/" endpoint that Node-RED connects to.
LEGACY_MAX_HZ = float(os.environ.get("INPUTS_LEGACY_HZ", 100))

# Minimum seconds between polls. 0.0 = as fast as the server will answer.
PUMP_MIN_PERIOD = 0.0
GANTRY_MIN_PERIOD = 0.0
NC_MIN_PERIOD = 0.0  # irrelevant in practice: the NC read itself blocks ~100 ms

RATE_REPORT_SECONDS = 2.0
RECONNECT_SECONDS = 3.0

PUMP_NODES = config.nodes({
    "torque": "PUMP_TORQUE",
    "water_flow": "PUMP_WATER_FLOW",
    "water_temp": "PUMP_WATER_TEMP",
    "solenoid_valve": "PUMP_SOLENOID_VALVE",
    "water_pump": "PUMP_WATER_PUMP",
    "mixer": "PUMP_MIXER",
})

GANTRY_NODES = config.nodes({
    "P0": "GANTRY_P0",
    "P1": "GANTRY_P1",
    "P2": "GANTRY_P2",
    "P3": "GANTRY_P3",
    "T0": "GANTRY_T0",
    "T1": "GANTRY_T1",
})

# The NC namespace is served by the CNC kernel gateway, and a Read
# against it blocks for ~100 ms regardless of how many nodes you ask for.
# Measured: 99.93 ms per request -- the hard 10 Hz wall.
#
# It MUST live in its own thread. Putting these nodes in GANTRY_NODES would drag
# that poller from ~1130 Hz down to 10 Hz. Add further NC values here: two NC
# nodes cost the same ~100 ms as one.
NC_NODES = config.nodes({
    "pos": "NC_POS",
    "vel": "NC_VEL",
    # Pump speed as commanded by the G-code S word -- the node Node-RED's "MCS: setSpeed"
    # subscribes to. Rides in the same ~100 ms NC read at no extra cost.
    "set_speed": "NC_SET_SPEED",
})


_snapshot = {}
_snapshot_lock = threading.Lock()
_seq = 0
_bus = Broadcaster(depth=4096)  # ~5 s of frames per client at full rate


def _to_ns(dt):
    """python-opcua hands back naive UTC datetimes."""
    if dt is None:
        return None
    return calendar.timegm(dt.utctimetuple()) * 1_000_000_000 + dt.microsecond * 1000


def _publish(values, src):
    global _seq
    # Serialise and publish under the lock so frames enter the ring in seq order
    # even though three pollers publish concurrently.
    with _snapshot_lock:
        _snapshot.update(values)
        _seq += 1
        _snapshot["seq"] = _seq
        _snapshot["src"] = src
        _snapshot["timestamp"] = time.time_ns()
        _bus.publish(json.dumps(_snapshot))


class Poller(threading.Thread):
    """Batched-read poller for one OPC UA server."""

    def __init__(self, name, url, nodemap, min_period, rate_key):
        threading.Thread.__init__(self, daemon=True)
        self.name_ = name
        self.url = url
        self.nodemap = nodemap
        self.min_period = min_period
        self.rate_key = rate_key
        self.client = None
        self.nodeids = None
        self.keys = None

    def _connect(self):
        self.client = Client(self.url)
        self.client.connect()
        self.client.load_type_definitions()
        self.keys = list(self.nodemap.keys())
        self.nodeids = [self.client.get_node(self.nodemap[k]).nodeid for k in self.keys]
        print("{} connected @ {} ({} nodes, 1 round-trip per cycle)".format(
            self.name_, self.url, len(self.keys)))

    def run(self):
        count = 0
        t_report = time.perf_counter()
        while True:
            if self.client is None:
                try:
                    self._connect()
                except Exception as e:
                    print("{}: connect failed ({}), retrying".format(self.name_, e))
                    self.client = None
                    time.sleep(RECONNECT_SECONDS)
                    continue

            t0 = time.perf_counter()
            try:
                # One Read request carrying every node on this server.
                results = self.client.uaclient.get_attributes(
                    self.nodeids, ua.AttributeIds.Value)
            except Exception as e:
                print("{}: read failed ({}), reconnecting".format(self.name_, e))
                try:
                    self.client.disconnect()
                except Exception:
                    pass
                self.client = None
                continue

            values = {}
            for key, dv in zip(self.keys, results, strict=True):
                # A bad node (e.g. unknown id) comes back as a bad status with no value
                # rather than failing the whole batch -- keep the rest of the frame.
                values[key] = dv.Value.Value if dv.Value is not None else None
                values[key + "_ts"] = _to_ns(dv.SourceTimestamp)
                values[key + "_ok"] = bool(dv.StatusCode.is_good())
            _publish(values, self.name_)

            count += 1
            now = time.perf_counter()
            elapsed = now - t_report
            if elapsed >= RATE_REPORT_SECONDS:
                hz = count / elapsed
                with _snapshot_lock:
                    _snapshot[self.rate_key] = round(hz, 2)
                print("{:<8} {:7.1f} Hz   ({:.2f} ms/cycle)".format(
                    self.name_, hz, 1000.0 / hz if hz else 0))
                count = 0
                t_report = now

            spare = self.min_period - (time.perf_counter() - t0)
            if spare > 0:
                time.sleep(spare)


async def handler(websocket):
    full = websocket.path.rstrip("/").endswith("full")
    who = "{} {}".format(websocket.remote_address, websocket.path)
    print("client connected: {} ({})".format(
        who, "every frame" if full else "<= {:g} Hz".format(LEGACY_MAX_HZ)))
    min_gap = 1.0 / LEGACY_MAX_HZ if LEGACY_MAX_HZ > 0 else 0.0
    last_sent = 0.0
    missed_total = 0
    try:
        async for frames, missed in _bus.frames():
            missed_total += missed
            if full:
                for payload in frames:
                    await websocket.send(payload)
            else:
                # Conflate: send only the newest snapshot, rate-limited.
                now = time.perf_counter()
                if now - last_sent >= min_gap:
                    last_sent = now
                    await websocket.send(frames[-1])
    except websockets.ConnectionClosed:
        pass
    finally:
        print("client disconnected: {}{}".format(
            who, " (missed {} frames)".format(missed_total) if full and missed_total else ""))


async def main():
    config.warn_if_unconfigured(PUMP_URL, GANTRY_URL)
    _bus.bind(asyncio.get_running_loop())
    Poller("pump", PUMP_URL, PUMP_NODES, PUMP_MIN_PERIOD, "pump_hz").start()
    Poller("gantry", GANTRY_URL, GANTRY_NODES, GANTRY_MIN_PERIOD, "gantry_hz").start()
    Poller("nc", GANTRY_URL, NC_NODES, NC_MIN_PERIOD, "nc_hz").start()
    # compression=None: localhost traffic, deflate would only burn CPU.
    async with websockets.serve(handler, WS_HOST, WS_PORT, compression=None):
        print("Websocket server running on ws://{}:{}  (/ = <= {:g} Hz, /full = every frame)"
              .format(WS_HOST, WS_PORT, LEGACY_MAX_HZ))
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
