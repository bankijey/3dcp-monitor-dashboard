"""
Fan-out of frames from acquisition threads to any number of websocket clients.

The acquisition services produce frames on plain threads (the OPC UA and scanner
libraries are blocking). Every connected consumer -- Node-RED, the viewer, the
recorder -- must receive every frame independently, and no consumer may slow the
producer or steal frames from another.

    producer thread ──publish()──> ring (last `depth` frames, seq-numbered)
                                     │
                     asyncio clients each iterate frames() from their own cursor

A client that falls more than `depth` frames behind skips ahead and is told how
many frames it missed, rather than blocking the producer.
"""

import asyncio
import collections
import itertools
import threading


class Broadcaster:
    def __init__(self, depth=1024):
        self._ring = collections.deque(maxlen=depth)
        self._seq = 0
        self._lock = threading.Lock()
        self._loop = None
        self._fut = None

    def bind(self, loop):
        """Attach to the event loop the websocket clients run on. Call before publishing."""
        self._loop = loop
        self._fut = loop.create_future()

    @property
    def seq(self):
        with self._lock:
            return self._seq

    def publish(self, payload):
        """Thread-safe. Returns the sequence number assigned to the frame."""
        with self._lock:
            self._seq += 1
            self._ring.append(payload)
            seq = self._seq
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._wake)
            except RuntimeError:
                pass  # loop closed during shutdown
        return seq

    def _wake(self):
        fut = self._fut
        self._fut = self._loop.create_future()
        if not fut.done():
            fut.set_result(None)

    def since(self, seq):
        """Frames published after `seq`: (frames, missed, newest_seq)."""
        with self._lock:
            newest = self._seq
            n = len(self._ring)
            if seq >= newest or n == 0:
                return [], 0, newest
            oldest = newest - n + 1
            start = max(seq + 1, oldest)
            missed = start - (seq + 1)
            frames = list(itertools.islice(self._ring, start - oldest, n))
            return frames, missed, newest

    async def frames(self, from_start=False, idle_timeout=1.0):
        """Yield (frames, missed) batches forever. New subscribers start at the live edge."""
        if self._fut is None:
            raise RuntimeError("Broadcaster.bind(loop) must be called first")
        seq = 0 if from_start else self.seq
        while True:
            # Capture the wake-up future *before* checking the ring, so a frame
            # published in between cannot be missed.
            fut = self._fut
            frames, missed, newest = self.since(seq)
            if frames:
                seq = newest
                yield frames, missed
                continue
            try:
                await asyncio.wait_for(asyncio.shield(fut), idle_timeout)
            except TimeoutError:
                pass
