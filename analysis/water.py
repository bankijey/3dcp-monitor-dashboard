"""
Water dosing and the water-to-solids ratio (W/S) shown by the dashboard and the
replay video. W/S is computed for display only: this code does not interpret it.

Input: the calibrated pump frame, with `elapsed` [s], `waterFlow` [L/min],
`waterValve` and the boolean `mixer`.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

if not hasattr(np, "trapezoid"):  # numpy < 2
    np.trapezoid = np.trapz

FLOW_THRESHOLD_LPM = 0.25   # a dosing pulse is a period with waterFlow above this
M_BAR_GPS = 750.0           # solids fed per second of screw running [g/s]
CHAMBER_TAU_S = 40.0        # mixing chamber as a stirred tank: mean residence time [s]


def _periods(mask: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True-runs of `mask` as (first_idx, last_idx), inclusive."""
    m = np.asarray(mask, dtype=bool)
    if not m.any():
        return []
    edges = np.diff(m.astype(np.int8))
    starts = np.flatnonzero(edges == 1) + 1
    ends = np.flatnonzero(edges == -1)
    if m[0]:
        starts = np.r_[0, starts]
    if m[-1]:
        ends = np.r_[ends, len(m) - 1]
    return list(zip(starts, ends))


def _overlap_seconds(t0: float, t1: float, periods, elapsed) -> float:
    """Total seconds of [t0, t1] covered by the given index-periods."""
    total = 0.0
    for i0, i1 in periods:
        lo, hi = max(elapsed[i0], t0), min(elapsed[i1], t1)
        total += max(0.0, hi - lo)
    return total


def calculate_water(df: pd.DataFrame, flow_threshold: float = FLOW_THRESHOLD_LPM) -> pd.DataFrame:
    """One row per dosing pulse: start and end [s], water [L], the seconds of
    screw running inside the pulse, and the water valve setting."""
    elapsed = df["elapsed"].to_numpy(float)
    flow = df["waterFlow"].to_numpy(float)
    mixer_periods = _periods(df["mixer"].to_numpy(bool))
    rows = []
    for i0, i1 in _periods(flow > flow_threshold):
        sl = slice(i0, i1 + 1)
        t_start, t_end = elapsed[i0], elapsed[i1]
        rows.append({
            "start_time": t_start,
            "end_time": t_end,
            "water_L": float(np.trapezoid(flow[sl] / 60.0, x=elapsed[sl])),
            "mixer_s": _overlap_seconds(t_start, t_end, mixer_periods, elapsed),
            "valve_mode": (float(df["waterValve"].iloc[sl].mode().iloc[0])
                           if "waterValve" in df else np.nan),
        })
    return pd.DataFrame(rows, columns=["start_time", "end_time", "water_L", "mixer_s", "valve_mode"])


def continuous_ws_index(
    df: pd.DataFrame,
    tau_s: float = CHAMBER_TAU_S,
    feed_delay_s: float = 0.0,
    grid_s: float = 0.2,
) -> pd.DataFrame:
    """Water [L/s] per unit solids feed leaving the mixing chamber, following
    Versteege, Wolfs and Salet (2025), Automation in Construction 177, 106327:

        W/S(t)  proportional to  (E * m_w)(t) / (E * m_s)(t),   E(t) = (1/tau) exp(-t/tau)

    m_w is the measured water flow and m_s the screw's on/off signal. Divide by
    the solids rate to get kg water per kg solids.
    """
    t = df["elapsed"].to_numpy(float)
    grid = np.arange(t[0], t[-1], grid_s)
    m_w = np.interp(grid, t, df["waterFlow"].to_numpy(float) / 60.0)  # L/s
    feed = np.interp(grid, t, df["mixer"].to_numpy(float)) > 0.5

    m_s = feed.astype(float)
    n0 = int(round(feed_delay_s / grid_s))
    if n0 > 0:
        for i0, i1 in _periods(feed):
            m_s[i0:min(i0 + n0, i1 + 1)] = 0.0

    kernel_t = np.arange(0.0, 5.0 * tau_s, grid_s)
    E = np.exp(-kernel_t / tau_s)
    E /= E.sum()
    conv_w = np.convolve(m_w, E)[: len(grid)]   # causal
    conv_s = np.convolve(m_s, E)[: len(grid)]
    ws = np.full_like(conv_w, np.nan)
    ok = conv_s > 1e-6
    ws[ok] = conv_w[ok] / conv_s[ok]
    return pd.DataFrame({"elapsed": grid, "ws_cont": ws})
