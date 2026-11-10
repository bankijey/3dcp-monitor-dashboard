"""
Rate probe: what does the OPC UA server actually grant, vs. what we ask for?

The gantry delivers a rock-steady 10 Hz whether the axes move or not. A deadband
filter would go quiet when stationary, so this is a fixed cadence -- most likely
the server revising our requested publishing interval (or its own sampling
interval) up to 100 ms.

For each requested interval this reports:
    requested vs. RevisedPublishingInterval   (what the subscription runs at)
    RevisedSamplingInterval                   (how often the server samples)
    measured notification rate + inter-arrival spread

Read it like this:
    revised == requested, measured == requested   -> no server limit, ask faster
    revised > requested                           -> server floor, this is the ceiling
    revised == requested but measured slower      -> the PLC task updating the
                                                     variable is the limit, not OPC UA

Run:  python probe_rates.py
"""

import time

from opcua import Client, ua
from opcua.client.ua_client import UaClient
from opcua.common.subscription import Subscription

from printmon import config

GANTRY_URL = config.GANTRY_OPCUA_URL
PUMP_URL = config.PUMP_OPCUA_URL

# One representative fast-changing node per server.
GANTRY_NODE = config.node("GANTRY_P0")
PUMP_NODE = config.node("PUMP_TORQUE")

# 0 asks the server for the fastest it is willing to support.
REQUESTED_INTERVALS = [0, 5, 10, 20, 25, 50, 100]
MEASURE_SECONDS = 6.0


# --- capture the revised values python-opcua otherwise discards -------------

_revised = {}

_orig_ready = Subscription.ready_callback
_orig_cmi = UaClient.create_monitored_items


def _ready_callback(self, response):
    try:
        _revised["publishing"] = response.Parameters.RevisedPublishingInterval
    except Exception:
        pass
    return _orig_ready(self, response)


def _create_monitored_items(self, params):
    results = _orig_cmi(self, params)
    try:
        _revised["sampling"] = results[0].RevisedSamplingInterval
        _revised["queue"] = results[0].RevisedQueueSize
    except Exception:
        pass
    return results


Subscription.ready_callback = _ready_callback
UaClient.create_monitored_items = _create_monitored_items


class RateHandler(object):
    def __init__(self):
        self.times = []

    def datachange_notification(self, node, val, data):
        self.times.append(time.perf_counter())

    def event_notification(self, event):
        pass

    def status_change_notification(self, status):
        pass


def _stats(times):
    if len(times) < 2:
        return None
    gaps = [(b - a) * 1000.0 for a, b in zip(times, times[1:])]
    gaps.sort()
    span = times[-1] - times[0]
    return {
        "n": len(times),
        "hz": (len(times) - 1) / span if span > 0 else 0.0,
        "min_ms": gaps[0],
        "med_ms": gaps[len(gaps) // 2],
        "max_ms": gaps[-1],
    }


def probe(url, nodeid, label):
    print("\n" + "=" * 78)
    print("{}  {}".format(label, url))
    print("node: {}".format(nodeid))
    print("=" * 78)

    client = Client(url)
    try:
        client.connect()
        client.load_type_definitions()
    except Exception as e:
        print("  could not connect: {}".format(e))
        return

    print("{:>10} {:>10} {:>10} {:>9} {:>8} {:>8} {:>8} {:>8}".format(
        "req(ms)", "revPub(ms)", "revSmp(ms)", "notifs", "meas Hz",
        "min ms", "med ms", "max ms"))

    try:
        node = client.get_node(nodeid)
        for requested in REQUESTED_INTERVALS:
            _revised.clear()
            handler = RateHandler()
            sub = None
            try:
                sub = client.create_subscription(requested, handler)
                sub.subscribe_data_change(node)
                time.sleep(MEASURE_SECONDS)
                s = _stats(handler.times)
                print("{:>10} {:>10} {:>10} {:>9} {:>8} {:>8} {:>8} {:>8}".format(
                    requested,
                    _revised.get("publishing", "?"),
                    _revised.get("sampling", "?"),
                    s["n"] if s else 0,
                    "{:.2f}".format(s["hz"]) if s else "-",
                    "{:.1f}".format(s["min_ms"]) if s else "-",
                    "{:.1f}".format(s["med_ms"]) if s else "-",
                    "{:.1f}".format(s["max_ms"]) if s else "-"))
            except Exception as e:
                print("{:>10}  failed: {}".format(requested, e))
            finally:
                if sub is not None:
                    try:
                        sub.delete()
                    except Exception:
                        pass

        # How long does a plain batched read actually take?
        print("\n  batched read timing (all-in-one-roundtrip):")
        nodeids = [node.nodeid]
        t0 = time.perf_counter()
        for _ in range(50):
            client.uaclient.get_attributes(nodeids, ua.AttributeIds.Value)
        dt = (time.perf_counter() - t0) / 50
        print("    1 node : {:.2f} ms per read  ({:.0f} Hz ceiling)".format(
            dt * 1000, 1.0 / dt))

    finally:
        try:
            client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    probe(GANTRY_URL, GANTRY_NODE, "GANTRY (Bosch Rexroth XM42)")
    probe(PUMP_URL, PUMP_NODE, "PUMP (mtec duo-mix connect / ECC2100)")
    print("\nDone.")
