"""
Follow-up probe. Two questions probe_rates.py raised:

1. The gantry subscription returned ServiceFaults and zero notifications.
   What is the actual StatusCode? (monkeypatches check_answer to print it)

2. Polling is not bound by the subscription floor -- inputs.py polls the pump at
   50 Hz even though its server revised every publishing interval to 100 ms.
   So: is that 50 Hz real information, or the same value read five times?

   This polls flat out and counts how many reads carry a NEW SourceTimestamp
   and how many carry a NEW value. Those are the rates that matter; the poll
   rate itself is just how fast you can ask.

Read it like this:
    unique_ts/s ~= poll/s     -> server is genuinely updating that fast
    unique_ts/s << poll/s     -> you are duplicating rows; the extra polls are
                                 storage cost with no information gain

Run:  python probe_effective_rate.py
"""

import time

from opcua import Client, ua
from opcua.client.ua_client import UASocketClient
from opcua.ua.ua_binary import nodeid_from_binary, struct_from_binary

from printmon import config

PUMP_URL = config.PUMP_OPCUA_URL
GANTRY_URL = config.GANTRY_OPCUA_URL

PUMP_NODES = config.nodes({
    "torque": "PUMP_TORQUE",
    "water_flow": "PUMP_WATER_FLOW",
    "water_temp": "PUMP_WATER_TEMP",
})
GANTRY_NODES = config.nodes({
    "P0": "GANTRY_P0",
    "P1": "GANTRY_P1",
    "P2": "GANTRY_P2",
    "T0": "GANTRY_T0",
})

MEASURE_SECONDS = 8.0


# --- 1. surface the real fault code ----------------------------------------

_orig_check = UASocketClient.check_answer


def check_answer(self, data, context):
    d = data.copy()
    typeid = nodeid_from_binary(d)
    if typeid == ua.FourByteNodeId(ua.ObjectIds.ServiceFault_Encoding_DefaultBinary):
        hdr = struct_from_binary(ua.ResponseHeader, d)
        print("  >> ServiceFault {}: {}".format(context.strip(), hdr.ServiceResult))
        return False
    return _orig_check(self, data, context)


UASocketClient.check_answer = check_answer


# --- 2. poll flat out and count what actually changes -----------------------

def poll_probe(url, nodemap, label):
    print("\n" + "=" * 78)
    print("{}  {}".format(label, url))
    print("=" * 78)

    client = Client(url)
    try:
        client.connect()
        client.load_type_definitions()
    except Exception as e:
        print("  could not connect: {}".format(e))
        return

    try:
        keys = list(nodemap.keys())
        nodeids = [client.get_node(nodemap[k]).nodeid for k in keys]

        polls = 0
        last_ts = {k: object() for k in keys}
        last_val = {k: object() for k in keys}
        uniq_ts = {k: 0 for k in keys}
        uniq_val = {k: 0 for k in keys}
        ts_present = {k: 0 for k in keys}

        t0 = time.perf_counter()
        while time.perf_counter() - t0 < MEASURE_SECONDS:
            try:
                results = client.uaclient.get_attributes(nodeids, ua.AttributeIds.Value)
            except Exception as e:
                print("  read failed: {}".format(e))
                break
            polls += 1
            for k, dv in zip(keys, results):
                ts = dv.SourceTimestamp
                if ts is not None:
                    ts_present[k] += 1
                if ts != last_ts[k]:
                    uniq_ts[k] += 1
                    last_ts[k] = ts
                v = dv.Value.Value
                if v != last_val[k]:
                    uniq_val[k] += 1
                    last_val[k] = v
        span = time.perf_counter() - t0

        print("  polled {} times in {:.1f}s = {:.1f} Hz "
              "({} nodes per request, {:.2f} ms per request)".format(
                  polls, span, polls / span, len(keys), span / polls * 1000))
        print()
        print("  {:<14} {:>10} {:>12} {:>12} {:>10}".format(
            "node", "poll Hz", "new ts Hz", "new val Hz", "has ts"))
        for k in keys:
            print("  {:<14} {:>10.1f} {:>12.1f} {:>12.1f} {:>9}%".format(
                k, polls / span, uniq_ts[k] / span, uniq_val[k] / span,
                int(100 * ts_present[k] / polls) if polls else 0))
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    poll_probe(GANTRY_URL, GANTRY_NODES, "GANTRY (Bosch Rexroth XM42)")
    poll_probe(PUMP_URL, PUMP_NODES, "PUMP (mtec duo-mix connect / ECC2100)")
    print("\nDone.")
