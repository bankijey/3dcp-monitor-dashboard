"""
The acquisition services' websocket behaviour, exercised with fake frames.

These import the real inputs_fast.py / scan_fast.py modules but never call their
main(): no PLC or scanner is touched. They pin down the multi-consumer contract
Node-RED, the viewer and the recorder rely on.
"""

import asyncio
import json

import pytest

websockets = pytest.importorskip("websockets")


async def _serve(module, handler):
    module._bus.bind(asyncio.get_running_loop())
    server = await websockets.serve(handler, "127.0.0.1", 0, compression=None)
    port = server.sockets[0].getsockname()[1]
    return server, "ws://127.0.0.1:{}".format(port)


async def _drain(ws, seconds):
    got = []
    try:
        while True:
            got.append(json.loads(await asyncio.wait_for(ws.recv(), seconds)))
    except TimeoutError:
        return got


def test_inputs_full_endpoint_gets_every_frame_and_legacy_is_conflated():
    inputs_fast = pytest.importorskip("inputs_fast")  # needs python-opcua installed

    async def main():
        server, url = await _serve(inputs_fast, inputs_fast.handler)
        async with websockets.connect(url + "/full") as full, \
                websockets.connect(url + "/full") as full2, \
                websockets.connect(url + "/") as legacy:
            await asyncio.sleep(0.1)
            for i in range(500):
                inputs_fast._publish({"torque": i}, "pump")
                if i % 50 == 0:
                    await asyncio.sleep(0.01)  # ~ 1 s of frames in bursts
            a, b, c = await asyncio.gather(_drain(full, 0.5), _drain(full2, 0.5),
                                           _drain(legacy, 0.5))
        server.close()
        return a, b, c

    a, b, c = asyncio.run(main())
    assert [f["torque"] for f in a] == list(range(500))  # every frame, in order
    assert [f["torque"] for f in b] == list(range(500))  # ...for every /full client
    assert all(f["src"] == "pump" for f in a)
    assert 0 < len(c) < 100  # Node-RED endpoint is rate-limited, not a firehose
    assert c[-1]["seq"] <= a[-1]["seq"]


def test_scan_passive_listener_cannot_stop_the_scan():
    scan_fast = pytest.importorskip("scan_fast")  # needs pyllt + LLT.dll

    async def main():
        server, url = await _serve(scan_fast, scan_fast.ws_server)
        owner = await websockets.connect(url + "/")
        viewer = await websockets.connect(url + "/full")
        await owner.send("start_scan")
        await asyncio.sleep(0.1)
        assert scan_fast._running.is_set()
        for i in range(50):
            scan_fast._bus.publish(json.dumps({"time": i, "x": [], "z": []}))
        got_owner, got_viewer = await asyncio.gather(_drain(owner, 0.3), _drain(viewer, 0.3))
        await viewer.close()  # a passive client leaving must not stop acquisition
        await asyncio.sleep(0.1)
        still_running = scan_fast._running.is_set()
        await owner.close()  # the controlling client leaving must stop it
        await asyncio.sleep(0.1)
        stopped = not scan_fast._running.is_set()
        server.close()
        return got_owner, got_viewer, still_running, stopped

    got_owner, got_viewer, still_running, stopped = asyncio.run(main())
    assert len(got_owner) == 50 and len(got_viewer) == 50  # no frame stealing
    assert still_running and stopped
