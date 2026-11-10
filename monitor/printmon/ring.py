"""
Fixed-capacity time-series ring buffers.

Written by a source thread at sensor rate, read by the GUI thread at frame rate.
Reads copy only the requested time window, never the whole buffer, so render
cost scales with what is on screen rather than with how much is retained.
"""

import threading

import numpy as np


class RingBuffer:
    def __init__(self, capacity, columns, dtype=np.float32):
        self.capacity = int(capacity)
        self.columns = tuple(columns)
        self._index = {c: i for i, c in enumerate(self.columns)}
        self._t = np.zeros(self.capacity, dtype=np.int64)
        self._v = np.full((self.capacity, len(self.columns)), np.nan, dtype=dtype)
        self._n = 0
        self._lock = threading.Lock()

    def col(self, name):
        return self._index[name]

    @property
    def total(self):
        """Rows ever appended (keeps counting after the ring wraps)."""
        return self._n

    def __len__(self):
        return min(self._n, self.capacity)

    def append(self, t, values):
        with self._lock:
            i = self._n % self.capacity
            self._t[i] = t
            self._v[i] = values
            self._n += 1

    def clear(self):
        with self._lock:
            self._n = 0

    def last_t(self):
        with self._lock:
            if self._n == 0:
                return None
            return int(self._t[(self._n - 1) % self.capacity])

    def _tail(self, k):
        n = min(self._n, self.capacity)
        k = min(k, n)
        if k <= 0:
            return (np.empty(0, np.int64), np.empty((0, len(self.columns)), self._v.dtype))
        end = self._n % self.capacity
        start = end - k
        if start >= 0 and end > 0:
            return self._t[start:end].copy(), self._v[start:end].copy()
        if end == 0:
            return self._t[start:].copy(), self._v[start:].copy()
        return (np.concatenate((self._t[start:], self._t[:end])),
                np.concatenate((self._v[start:], self._v[:end])))

    def tail(self, k):
        """Last k rows, oldest first: (t[k], values[k, ncol]). Copies."""
        with self._lock:
            return self._tail(k)

    def _count_from(self, t, side):
        """Rows with t >= threshold (side='left') or t > threshold (side='right')."""
        n = min(self._n, self.capacity)
        if n == 0:
            return 0
        end = self._n % self.capacity
        segments = [(0, n)] if self._n <= self.capacity else [(end, self.capacity), (0, end)]
        k = 0
        for a, b in reversed(segments):  # newest segment first
            if b <= a:
                continue
            j = int(np.searchsorted(self._t[a:b], t, side=side))
            k += (b - a) - j
            if j > 0:
                break
        return k

    def window(self, t_min):
        """All rows with t >= t_min, oldest first. Assumes t is non-decreasing."""
        with self._lock:
            return self._tail(self._count_from(t_min, "left"))

    def between(self, t_min, t_max):
        """Rows with t_min <= t <= t_max, oldest first -- for looking back in history."""
        with self._lock:
            k = self._count_from(t_min, "left")
            later = self._count_from(t_max, "right")
            t, v = self._tail(k)
            keep = max(k - later, 0)
            return t[:keep], v[:keep]

    def first_t(self):
        with self._lock:
            n = min(self._n, self.capacity)
            if n == 0:
                return None
            return int(self._t[0 if self._n <= self.capacity else self._n % self.capacity])
