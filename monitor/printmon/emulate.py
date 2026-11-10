"""
Emulate the acquisition services from a recorded run -- no PLC, pump or scanner needed.

    python -m printmon emulate --run myrun                     alternate ports 18001 / 18764
    python -m printmon emulate --run myrun --lab-ports         the real ports 8001 / 8764

This runs the *real* websocket code of inputs_fast.py and scan_fast.py; only the
hardware threads are replaced by a replay of recorded frames, re-stamped with the
current time. So anything that talks to the services -- the viewer, the recorder,
Node-RED -- can be developed and tested off-machine against the exact protocol it
will meet in the lab. Like the real scanner service, profiles only flow after a
client sends "start_scan" (use --autostart-scan to emulate Node-RED having done so).

Every emulated frame carries "emulated": true. The recorder stores unknown keys in
`extra`, so emulated data stays identifiable in the lake forever.

Point the viewer/recorder at the alternate ports with:
    set PRINTMON_INPUTS_URL=ws://localhost:18001/full
    set PRINTMON_SCAN_URL=ws://localhost:18764/full
"""

import argparse
import asyncio
import json
import sys
import time

import websockets

from .sources import ReplaySource, Sink, find_run


class _ServiceFeeder(Sink):
    """Turns replayed records back into live-format frames on the real service buses."""

    def __init__(self, inputs_fast, scan_fast):
        self.inputs = inputs_fast
        self.scan = scan_fast
        self._last_pos = None
        self._profile_count = 0

    def on_process(self, rec):
        pump = {k: rec[k] for k in ("torque", "water_flow", "water_temp", "water_pump",
                                    "solenoid_valve", "mixer")}
        gantry = {k: rec[k] for k in ("P0", "P1", "P2", "P3", "T0", "T1")}
        self.inputs._publish(dict(pump, emulated=True), "pump")
        self.inputs._publish(dict(gantry, emulated=True), "gantry")
        if rec["pos"] is not None and rec["pos"] != self._last_pos:
            self._last_pos = rec["pos"]
            self.inputs._publish({"pos": list(rec["pos"]), "vel": list(rec["vel"] or ()),
                                  "emulated": True}, "nc")

    def on_scan(self, rec):
        if not self.scan._running.is_set():
            return  # the real service only streams between start_scan and stop_scan
        self._profile_count += 1
        frame = {
            "time": time.time_ns(),
            "profile_count": self._profile_count,
            "dropped": 0,
            "lost_profiles": 0,
            "m0": rec["m0"].tolist() if rec["m0"] is not None else [],
            "width": rec["width"],
            "x": rec["x"].tolist(),
            "z": rec["z"].tolist(),
            "emulated": True,
        }
        self.scan._bus.publish(json.dumps(frame))

    def on_status(self, stream, text):
        print("[{}] {}".format(stream, text))


async def _serve(opts):
    import inputs_fast  # the real service modules; importing them starts nothing
    import scan_fast

    loop = asyncio.get_running_loop()
    inputs_fast._bus.bind(loop)
    scan_fast._bus.bind(loop)
    if opts.autostart_scan:
        scan_fast._running.set()

    run = find_run(opts.run, opts.data)
    # Frames are re-stamped with the current time: inputs_fast._publish stamps process
    # frames itself, and _ServiceFeeder.on_scan stamps profiles.
    src = ReplaySource(_ServiceFeeder(inputs_fast, scan_fast), run, speed=opts.speed,
                       loop=True, start_s=opts.start)
    src.start()

    p_in, p_scan = (8001, 8764) if opts.lab_ports else (18001, 18764)
    async with websockets.serve(inputs_fast.handler, "localhost", p_in, compression=None), \
            websockets.serve(scan_fast.ws_server, "localhost", p_scan, compression=None):
        print("=" * 72)
        print(" EMULATED acquisition services -- replaying {} -- NOT THE MACHINE".format(
            run.name))
        print("   inputs  ws://localhost:{0}/  and  ws://localhost:{0}/full".format(p_in))
        print("   scan    ws://localhost:{}/full   (streams after start_scan{})".format(
            p_scan, "; auto-started" if opts.autostart_scan else ""))
        print("=" * 72)
        try:
            if opts.duration:
                await asyncio.sleep(opts.duration)
            else:
                await asyncio.Future()
        finally:
            src.stop()


def main(argv=None):
    p = argparse.ArgumentParser(prog="printmon emulate", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", default="", help="substring of the recorded run name")
    p.add_argument("--data", default=None, help="recorded runs directory (default data/)")
    p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--start", type=float, default=0.0, help="skip the first N seconds")
    p.add_argument("--lab-ports", action="store_true",
                   help="serve on the real ports 8001/8764 (stop the real services first)")
    p.add_argument("--autostart-scan", action="store_true",
                   help="stream profiles without waiting for a start_scan command")
    p.add_argument("--duration", type=float, default=None, help="exit after N seconds")
    opts = p.parse_args(argv)
    try:
        asyncio.run(_serve(opts))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
