import asyncio
import threading

from printmon.broadcast import Broadcaster


def _collect(bc, n_clients, n_frames, depth_ok=True):
    async def main():
        bc.bind(asyncio.get_running_loop())
        received = [[] for _ in range(n_clients)]
        missed = [0] * n_clients

        async def client(i):
            async for frames, m in bc.frames():
                received[i].extend(frames)
                missed[i] += m
                if received[i] and received[i][-1] == n_frames - 1:
                    return

        tasks = [asyncio.create_task(client(i)) for i in range(n_clients)]
        await asyncio.sleep(0.05)  # clients subscribe at the live edge first

        def produce():
            for k in range(n_frames):
                bc.publish(k)

        threading.Thread(target=produce).start()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
        return received, missed

    return asyncio.run(main())


def test_every_client_receives_every_frame_in_order():
    received, missed = _collect(Broadcaster(depth=100_000), n_clients=3, n_frames=20_000)
    for frames, m in zip(received, missed, strict=True):
        assert frames == list(range(20_000))
        assert m == 0


def test_slow_client_skips_ahead_and_reports_missed():
    bc = Broadcaster(depth=10)
    for k in range(25):
        bc.publish(k)
    frames, missed, newest = bc.since(0)
    assert frames == list(range(15, 25))  # only the last `depth` survive
    assert missed == 15
    assert newest == 25


def test_since_live_edge_is_empty():
    bc = Broadcaster(depth=4)
    bc.publish("a")
    assert bc.since(bc.seq) == ([], 0, 1)
