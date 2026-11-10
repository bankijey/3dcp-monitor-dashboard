"""
Near-real-time processing shared by the viewer (and, later, the silver layer).

    resample_profile   scanner points -> fixed x-grid heights (for 3D / stacking)
    PositionTracker    10 Hz NC position -> position estimate at any timestamp
    ProfileStore       time-indexed profiles: rewind, the 3D bead, profile at a cursor
    recent_layers      which build layers (by Z) to show, most recent first
"""

import threading

import numpy as np


def resample_profile(x, z, grid, max_gap=2.0):
    """
    Heights of one profile on a fixed x-grid.

    The scanner reports missing points as z == 0. Those are dropped, the rest is
    linearly interpolated onto `grid`, and grid points further than `max_gap` mm
    from any real measurement are NaN -- interpolation never invents a surface
    across an occlusion.
    """
    x = np.asarray(x, dtype=np.float64)
    z = np.asarray(z, dtype=np.float64)
    ok = np.isfinite(x) & np.isfinite(z) & (z != 0)
    out = np.full(len(grid), np.nan, dtype=np.float32)
    if ok.sum() < 2:
        return out
    xs, zs = x[ok], z[ok]
    order = np.argsort(xs, kind="stable")
    xs, zs = xs[order], zs[order]
    out[:] = np.interp(grid, xs, zs, left=np.nan, right=np.nan)
    # distance from each grid point to the nearest measured x
    idx = np.clip(np.searchsorted(xs, grid), 1, len(xs) - 1)
    nearest = np.minimum(np.abs(grid - xs[idx - 1]), np.abs(xs[idx] - grid))
    out[nearest > max_gap / 2.0] = np.nan
    return out


class PositionTracker:
    """
    Best estimate of tool position at an arbitrary timestamp.

    NC position arrives at 10 Hz (the NC namespace read wall). Between samples the tool
    keeps moving -- 13 mm per sample at F8000 -- so the estimate extrapolates
    from the last *new* sample along its reported velocity, for at most
    `max_extrapolate_s`. Replay rows repeat the same position until the NC
    updates it, so only a changed position counts as a new anchor.

    Velocity is in mm/min (NC convention; vel_scale converts to mm/s). Validated
    against recorded runs in tests/test_processing.py.
    """

    def __init__(self, max_extrapolate_s=0.15, vel_scale=1.0 / 60.0):
        self.max_extrapolate_s = max_extrapolate_s
        self.vel_scale = vel_scale
        self.extrapolate = True
        self._t = None
        self._pos = None
        self._vel = None
        self._lock = threading.Lock()

    def update(self, t, pos, vel=None):
        """Returns True if this was a new position sample."""
        if pos is None:
            return False
        with self._lock:
            if self._pos is not None and pos == self._pos:
                if vel is not None:
                    self._vel = vel
                return False
            self._t, self._pos, self._vel = t, pos, vel
            return True

    @property
    def pos(self):
        """Last raw NC position, all axes (X, Y, Z, C, ...), or None."""
        with self._lock:
            return self._pos

    def at(self, t):
        with self._lock:
            if self._pos is None:
                return None
            p = self._pos
            if not self.extrapolate or self._vel is None:
                return p[0], p[1], p[2]
            dt = min(max((t - self._t) / 1e9, 0.0), self.max_extrapolate_s)
            v = self._vel
            s = self.vel_scale * dt
            return p[0] + v[0] * s, p[1] + v[1] * s, p[2] + v[2] * s


class ProfileStore:
    """
    The last `capacity` profiles, indexed by time.

    Keeps the raw points (for the profile plot at any instant), the resampled
    heights (for the 3D bead) and the cumulative along-path distance (so the 3D
    bead is spaced by where the tool actually was, not by sample count).
    Capacity bounds how far back rewind can reach for profiles.
    """

    def __init__(self, capacity, grid, points=320):
        self.capacity = int(capacity)
        self.grid = np.asarray(grid, dtype=np.float32)
        self.points = int(points)
        self._t = np.zeros(self.capacity, dtype=np.int64)
        self._d = np.zeros(self.capacity, dtype=np.float64)
        self._h = np.full((self.capacity, len(self.grid)), np.nan, dtype=np.float32)
        self._x = np.full((self.capacity, self.points), np.nan, dtype=np.float32)
        self._z = np.full((self.capacity, self.points), np.nan, dtype=np.float32)
        self._n = 0
        self._dist = 0.0
        self._last_xy = None
        self._lock = threading.Lock()

    def __len__(self):
        return min(self._n, self.capacity)

    def push(self, t, x, z, heights, xy=None, min_step=0.05):
        """Append one profile. `xy` (estimated position) sets the along-path spacing."""
        m = min(len(x), self.points)
        with self._lock:
            step = min_step
            if xy is not None and self._last_xy is not None:
                step = max(float(np.hypot(xy[0] - self._last_xy[0],
                                          xy[1] - self._last_xy[1])), min_step)
            if xy is not None:
                self._last_xy = xy
            self._dist += step
            i = self._n % self.capacity
            self._t[i] = t
            self._d[i] = self._dist
            self._h[i] = heights
            self._x[i, :m] = x[:m]
            self._x[i, m:] = np.nan
            self._z[i, :m] = z[:m]
            self._z[i, m:] = np.nan
            self._n += 1

    def _order(self):
        """Ring indices, oldest first."""
        n = min(self._n, self.capacity)
        if self._n <= self.capacity:
            return np.arange(n)
        end = self._n % self.capacity
        return np.concatenate((np.arange(end, self.capacity), np.arange(0, end)))

    def time_range(self):
        with self._lock:
            if self._n == 0:
                return None
            order = self._order()
            return int(self._t[order[0]]), int(self._t[order[-1]])

    def at(self, t):
        """Raw (t, x, z) of the latest profile at or before t, or None."""
        with self._lock:
            if self._n == 0:
                return None
            order = self._order()
            k = int(np.searchsorted(self._t[order], t, side="right")) - 1
            if k < 0:
                return None
            i = order[k]
            return int(self._t[i]), self._x[i].copy(), self._z[i].copy()

    def window(self, t_min, t_max, max_rows=None, dense_tail=False):
        """
        (t, distance, heights) for profiles in [t_min, t_max], oldest first, decimated.

        dense_tail keeps every profile near t_max and thins progressively further back
        (geometric spacing) -- detail where the 3D camera looks, cost bounded by max_rows.
        """
        with self._lock:
            if self._n == 0:
                return None
            order = self._order()
            ts = self._t[order]
            a = int(np.searchsorted(ts, t_min, side="left"))
            b = int(np.searchsorted(ts, t_max, side="right"))
            if b - a < 2:
                return None
            sel = order[a:b]
            if max_rows and len(sel) > max_rows:
                if dense_tail:
                    back = np.unique(np.round(np.geomspace(1, len(sel), max_rows)).astype(int))
                    sel = sel[np.sort(len(sel) - back)]
                else:
                    sel = sel[np.linspace(0, len(sel) - 1, max_rows).astype(int)]
            return self._t[sel].copy(), self._d[sel].copy(), self._h[sel].copy()


def recent_layers(z, n=3, bucket=1.0, min_points=1):
    """
    Build layers present in `z` (oldest-first samples), most recent first.

    A layer is a Z value rounded to `bucket` mm. Recency is by each layer's last
    appearance, so travel moves and uneven layer heights need no special cases.
    Buckets with fewer than `min_points` samples are ignored -- they are Z moves
    between layers, not layers. Returns up to `n` layer heights.
    """
    z = np.asarray(z, dtype=np.float64)
    z = z[np.isfinite(z)]
    if len(z) == 0:
        return []
    keys = np.round(z / bucket) * bucket
    uniq, last_rev, counts = np.unique(keys[::-1], return_index=True, return_counts=True)
    last_seen = len(keys) - 1 - last_rev
    order = [i for i in np.argsort(-last_seen) if counts[i] >= min_points]
    return [float(uniq[i]) for i in order[:n]]
