"""
Print-path and laser-scan geometry, shared by the ingest (data-quality
attributes in dim_experiment) and the dashboards (bead width per profile).

Profiles
    One scan profile is the laser line across the bead: x (mm) and z (mm) per
    point; z == 0 marks a dropout (no return).

Orientation (a data-quality attribute, detected per experiment)
    upright   z grows upward: the scanner's settings were loaded, so the
              bead top is the largest value.
    inverted  z is the distance from the scanner (settings not loaded), so
              the bead top is the smallest value.
    The setting changed from session to session (not with the date), so it is
    read from the profiles themselves: the bead top is flat and sits under the
    nozzle (centre of the line), and the bead's flanks fall away from it.

Layers
    Layer height is Z_i - Z_(i-1): the head's Z now minus its Z on the
    previous pass over the same XY spot. This works for stepped layers and for
    prints whose Z ramps continuously along the path. A spot with nothing
    printed below it is layer 1 (its base is the substrate).

Scanner alignment
    The scanner trails the nozzle, and so sees the bead just printed, when
    the head travels in the direction C points (+X when there is no C axis).
    Travelling the other way it leads and sees the layer below (one layer
    lower); travelling across, the line runs along the bead.

Bead (layer-band method)
    The top is the centre of the line (convex or concave alike). Layer i
    occupies the band [top - h_i, top]; the top of layer i-1 is 0. Width is
    measured at half the band height, walking outward from the centre.
    Layer 1 uses the substrate seen at both ends of the line as its base.
"""
from __future__ import annotations

import numpy as np

CENTRE = slice(80, 240)            # middle half of the 320-point line
ORIENT_MIN_VOTES = 30              # decisive profiles needed to call it
ORIENT_SHARE = 0.7                 # majority needed among decisive profiles
C_ROTATION_MIN_DEG = 5.0           # C spread above this = C turns while printing
ALIGN_TOL_DEG = 5.0                # travel within this of -C sees the fresh bead
MIN_BEAD_WIDTH = 5.0               # narrower = not a bead (bumps on a flat surface)
MIN_FIRST_LAYER = 3.0              # layer 1 must stand this high above the substrate
BASE_TOL = 0.1                     # flank counts as down at the base within 10% of h
MAX_CROWN = 1.5                    # mm the line's very centre may stand above the wider centre
CROWN_LIMIT = 4.0                  # taller than this is a bead on a wider layer, not a crown
FLAT_TOL = 1.0                     # mm around the wider centre's median that counts as its top
FLAT_SHARE = 0.4                   # share of the wider centre that must lie there
NOTCH_MM = 4.0                     # a cut narrower than this is not where the bead ends
NOTCH_DEPTH = 1.5                  # mm: shallower dips are the bead's own surface
SPIKE_JUMP = 2.0                   # mm over two samples (~0.4 mm): a near-vertical side
MAX_SPIKE_MM = 12.0                # wider than this is not something standing on the bead
FOOT_TOL = 1.5                     # mm above a spike's foot that still counts as back on it
MIN_LINE_RETURNS = 0.5             # share of the line that must hold real returns
MIN_BEAD_RETURNS = 0.75            # share of the bead's own span that must hold them
METHODS =["Layer band (Zᵢ − Zᵢ₋₁)", "Fixed 4 mm offset (paper)"]


# ================================================================ profiles
def prepare(x, z, orientation: str | None, ends: str = "nan"):
    """Raw profile -> (x, z_up): z_up grows upward whatever the file's
    orientation. Dropouts inside the line are interpolated; dropouts at
    either end are NaN (nothing seen there) or, with ends="hold", hold the
    last seen value (the original fix_z behaviour, kept for the paper method).
    """
    x = np.asarray(x, float)
    z = np.asarray(z, float)
    i = np.arange(len(z))
    bad = ~np.isfinite(z) | (z == 0)
    good = np.flatnonzero(~bad)
    if len(good) < 2:
        return x, np.full(len(z), np.nan)
    zf = np.interp(i, good, z[good])
    if ends == "nan":
        zf[: good[0]] = np.nan
        zf[good[-1] + 1:] = np.nan
    # x is only trustworthy where z is: at a dropout the scanner reports a
    # placeholder x, often out of sequence, so it is rebuilt from the valid
    # neighbours (what the original fix_z did by zeroing it first)
    xg = np.flatnonzero(~bad & np.isfinite(x))
    xf = np.interp(i, xg, x[xg]) if len(xg) >= 2 else x
    return xf, (-zf if orientation == "inverted" else zf)


# ============================================================= orientation
def orientation_vote(x, z) -> int:
    """+1 upright, -1 inverted, 0 undecided, for one raw profile. Decisive
    only when the line shows a bead: >= 5 mm of relief, the centre near one
    extreme, and more points on that (flat) side than on the other."""
    x = np.asarray(x, float)
    z = np.asarray(z, float)
    m = (z != 0) & np.isfinite(z) & np.isfinite(x)
    if m.sum() < 60:
        return 0
    x, z = x[m], z[m]
    lo, hi = np.percentile(z, [2, 98])
    relief = hi - lo
    if relief < 5:
        return 0
    c = z[np.abs(x - np.median(x)) < 5]
    if len(c) < 10:
        return 0
    rank = (np.median(c) - lo) / relief
    near_hi, near_lo = np.mean(z > hi - 1.0), np.mean(z < lo + 1.0)
    if rank > 0.8 and near_hi > near_lo:
        return 1
    if rank < 0.2 and near_lo > near_hi:
        return -1
    return 0


def detect_orientation(profiles) -> tuple[str | None, float | None, int]:
    """profiles: iterable of raw (x, z). -> (orientation or None, share of
    decisive profiles agreeing, number of decisive profiles)."""
    v = np.array([orientation_vote(x, z) for x, z in profiles], dtype=int)
    up, inv = int((v == 1).sum()), int((v == -1).sum())
    n = up + inv
    if n == 0:
        return None, None, 0
    share = up / n
    agree = max(share, 1 - share)
    if n < ORIENT_MIN_VOTES:
        return None, agree, n
    if share >= ORIENT_SHARE:
        return "upright", agree, n
    if share <= 1 - ORIENT_SHARE:
        return "inverted", agree, n
    return None, agree, n


# ================================================================== C axis
def c_spread(c) -> float | None:
    """Circular spread (deg) of C angles: 360 minus the largest gap between
    them, so 359.9 and 0.1 count as 0.2 apart. None without a C axis."""
    c = np.asarray(c, float)
    c = c[np.isfinite(c)]
    if c.size == 0:
        return None
    a = np.sort(np.mod(c, 360.0))
    gaps = np.diff(np.r_[a, a[0] + 360.0])
    return float(360.0 - gaps.max())


def scanner_aligned(angle, pos_c, tol: float = ALIGN_TOL_DEG):
    """Travel direction within `tol` deg of the direction the scanner trails.
    The C axis counts the opposite way round from the travel angle (atan2 of
    the velocity): on the 23 May 2025 arch, where C followed the path, angle + C
    is 0 for 96 % of the profiles and angle - C is spread over the circle. So
    the scanner trails when angle = -C (C = 0 without a C axis; a fixed C of
    181 means travel at 179)."""
    c = np.nan_to_num(np.asarray(pos_c, float), nan=0.0)
    rel = (np.asarray(angle, float) + c + 180.0) % 360.0 - 180.0
    return np.abs(rel) <= tol


def c_axis_rotates(pos_c, moving) -> bool | None:
    """True when C turns while printing; False when it holds still; None
    when there is no C axis (3-axis machine)."""
    pos_c = np.asarray(pos_c, float)
    s = c_spread(pos_c[np.asarray(moving, bool)])
    return None if s is None else bool(s > C_ROTATION_MIN_DEG)


# ================================================================== layers
def previous_pass(t, x, y, z, radius: float = 5.0, step: float = 2.0,
                  min_drop: float = 1.0, min_dt: float = 2.0, max_gap_s: float = 5.0):
    """For each path sample (time-ordered printing moves): the head's Z on the
    most recent earlier pass over the same XY spot that lies at least
    `min_drop` lower. -> (h = Z_i - Z_(i-1), layer number). Layer is 1 where
    nothing lies below, else 1 + the layer of that pass. The path is
    densified to `step` mm so fast moves cannot step over a spot."""
    from scipy.spatial import cKDTree

    t, x, y, z = (np.asarray(a, float) for a in (t, x, y, z))
    n = len(t)
    h = np.full(n, np.nan)
    layer = np.full(n, np.nan)
    ok = np.isfinite(t) & np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
    idx = np.flatnonzero(ok)
    if idx.size < 2:
        layer[idx] = 1
        return h, layer
    tt, xx, yy, zz = t[idx], x[idx], y[idx], z[idx]
    seg = np.hypot(np.diff(xx), np.diff(yy))
    k = np.clip(np.ceil(seg / step), 1, 500).astype(int)
    k[np.diff(tt) > max_gap_s] = 1                    # no fake path across gaps
    src = np.repeat(np.arange(len(seg)), k)
    f = (np.arange(k.sum()) - np.repeat(np.cumsum(k) - k, k)) / np.repeat(k, k)
    X = np.r_[xx[src] + f * (xx[src + 1] - xx[src]), xx[-1]]
    Y = np.r_[yy[src] + f * (yy[src + 1] - yy[src]), yy[-1]]
    Z = np.r_[zz[src] + f * (zz[src + 1] - zz[src]), zz[-1]]
    T = np.r_[tt[src] + f * (tt[src + 1] - tt[src]), tt[-1]]
    S = np.r_[src, len(tt) - 1]                        # sample each point came from
    near = cKDTree(np.c_[X, Y]).query_ball_point(np.c_[xx, yy], radius)
    hh = np.full(len(tt), np.nan)
    ll = np.ones(len(tt))
    for i in range(len(tt)):                           # time order: earlier layers known
        cand = np.asarray(near[i], dtype=int)
        if cand.size:
            cand = cand[(Z[cand] < zz[i] - min_drop) & (T[cand] < tt[i] - min_dt)]
        if cand.size:
            j = cand[np.argmax(T[cand])]
            hh[i] = zz[i] - Z[j]
            ll[i] = ll[S[j]] + 1
    h[idx], layer[idx] = hh, ll
    return h, layer


def path_layers(t, x, y, z, tolerance: float = 0.5):
    """Layer number and layer height for every printing sample of a path.

    Two passes of previous_pass: the first (1 mm minimum drop, every 4th
    sample) finds the typical layer height; the second only accepts a pass
    at least half a layer lower, so the short Z moves at a layer change are
    not mistaken for a layer. Samples well above the print (travel) get no
    layer. Per-layer height = median of its samples; layers more than
    `tolerance` mm from the typical height take the mean of the nearest
    regular layers on each side (correct_outliers, both directions).

    -> (h per sample, layer per sample, typical h or None,
        {layer: h} for layers >= 2)."""
    import pandas as pd

    t, x, y, z = (np.asarray(a, float) for a in (t, x, y, z))
    n = len(t)
    if n < 10:
        return np.full(n, np.nan), np.ones(n), None, {}
    h1, _ = previous_pass(t[::4], x[::4], y[::4], z[::4])
    hv = h1[np.isfinite(h1)]
    if hv.size < 10:                                   # nothing stacked: one layer
        return np.full(n, np.nan), np.ones(n), None, {}
    h_typ = float(np.median(hv))
    h, layer = previous_pass(t, x, y, z, min_drop=0.5 * h_typ)
    first = layer == 1
    z1 = np.nanpercentile(z[first], 25) if first.any() else np.nanmin(z)
    off = (h > 1.5 * h_typ) | (first & (z > z1 + 1.5 * h_typ))
    h[off] = np.nan
    layer[off] = np.nan
    # consistency: a sample far from its layer's typical Z (a stray link in
    # the chain) moves to the layer nearest in Z, or gets none
    med = pd.Series(z).groupby(layer).median()
    if len(med):
        lz = med.reindex(layer).to_numpy()
        stray = np.isfinite(layer) & (np.abs(z - lz) > 0.75 * h_typ)
        if stray.any():
            lv, mz = med.index.to_numpy(), med.to_numpy()
            k = np.abs(z[stray, None] - mz[None, :]).argmin(axis=1)
            close = np.abs(z[stray] - mz[k]) <= 0.5 * h_typ
            layer[stray] = np.where(close, lv[k], np.nan)
            h[np.flatnonzero(stray)[~close]] = np.nan
    per = pd.Series(h).groupby(layer).median().drop(1.0, errors="ignore")
    if per.empty:
        return h, layer, h_typ, {}
    bad = (per - h_typ).abs() > tolerance
    kept = per.mask(bad)
    fill = pd.concat([kept.ffill(), kept.bfill()], axis=1).mean(axis=1)
    per = per.where(~bad, fill.fillna(h_typ))
    return h, layer, h_typ, {int(k): float(v) for k, v in per.items()}


def correct_outliers(values, threshold: float):
    """Values above `threshold` -> mean of the nearest valid value on each
    side (or the one that exists). Same result as the notebook version,
    vectorised; NaN stays NaN."""
    import pandas as pd

    s = pd.Series(np.asarray(values, float))
    bad = s > threshold
    if not bad.any():
        return s.to_numpy()
    kept = s.mask(bad)
    left, right = kept.ffill(), kept.bfill()
    fill = pd.concat([left, right], axis=1).mean(axis=1)   # mean, or the one present
    return s.where(~bad, fill.where(fill.notna(), s)).to_numpy()


# ==================================================================== bead
def _crossing(x, fil, split, start, step):
    """Walk from `start` in direction `step` while fil >= split; return the
    interpolated x where it drops below, or None if the data ends first."""
    i = start
    n = len(fil)
    while 0 <= i + step < n and np.isfinite(fil[i + step]) and fil[i + step] >= split:
        i += step
    j = i + step
    if not (0 <= j < n) or not np.isfinite(fil[j]):
        return None, i
    a, b = fil[i], fil[j]
    w = (a - split) / (a - b) if a != b else 0.0
    return x[i] + w * (x[j] - x[i]), i


def bead(x, z_up, h: float | None) -> dict | None:
    """Layer-band geometry of one prepared profile (z up). h = layer height
    Z_i - Z_(i-1), or None for layer 1 (base = substrate at both ends).

    -> rel (z relative to the top of layer i-1, i.e. the band base), fil
    (rel clipped to the band), top, split, width, area, xl/xr (width ends)
    and complete (both edges found inside the seen part of the line).
    None when the line holds no usable centre."""
    x = np.asarray(x, float)
    z = np.asarray(z_up, float)
    cz = z[CENTRE]
    cz = cz[np.isfinite(cz)]
    if cz.size < 20:
        return None
    # Top of the bead: median + spread of the line's centre, spikes above cut.
    # MAD instead of std, so flanks inside the window (narrow beads) don't
    # lift it.
    med = float(np.median(cz))
    thresh = med + 1.4826 * float(np.median(np.abs(cz - med)))
    corr = z.copy()
    corr[corr > thresh] = np.nan
    seen = np.isfinite(z)
    corr = np.where(seen, _fill(corr), np.nan)
    if h is None:                                      # layer 1: substrate line
        v = np.flatnonzero(seen)
        if v.size < 20:
            return None
        e0, e1 = np.nanmean(z[v[:5]]), np.nanmean(z[v[-5:]])
        base = np.interp(np.arange(len(z)), [v[0], v[-1]], [e0, e1])
        rel = corr - base
        fil = np.clip(rel, 0, None)
        top = float(np.nanmedian(thresh - base[CENTRE]))
        split = top / 2.0
    else:
        base = np.full(len(z), thresh - h)
        rel = corr - base
        fil = np.clip(rel, 0, h)
        top = float(np.nanmedian(fil[CENTRE]))
        split = top - h / 2.0
    out = dict(rel=rel, fil=fil, base=base, top=top, split=split, h=h,
               width=np.nan, area=np.nan, xl=np.nan, xr=np.nan,
               complete=False, edges_seen=False, full=False, n_edges=0,
               edge_lo=np.nan, edge_hi=np.nan, reach_lo=np.nan, reach_hi=np.nan,
               y0=(h if h else top))                  # display: 0 at the bead top
    if not np.isfinite(split) or split <= 0 or (h is None and top < MIN_FIRST_LAYER):
        return out
    mid = len(z) // 2
    above = np.flatnonzero(np.nan_to_num(fil[CENTRE], nan=-1) >= split) + CENTRE.start
    if above.size == 0:
        return out
    start = int(above[np.argmin(np.abs(above - mid))])
    xa, ia = _crossing(x, fil, split, start, -1)
    xb, ib = _crossing(x, fil, split, start, +1)
    # per side: the edge if found, else how far the bead reaches (line end)
    sides = sorted([(xa, x[ia]), (xb, x[ib])], key=lambda s: s[1])
    out.update(edge_lo=np.nan if sides[0][0] is None else sides[0][0],
               edge_hi=np.nan if sides[1][0] is None else sides[1][0],
               reach_lo=sides[0][1], reach_hi=sides[1][1],
               n_edges=int(xa is not None) + int(xb is not None))
    if (xa is not None and xb is not None and abs(xb - xa) >= MIN_BEAD_WIDTH
            and _stands_up(x, fil, min(xa, xb), max(xa, xb), split, top)):
        out.update(xl=min(xa, xb), xr=max(xa, xb), width=abs(xb - xa),
                   complete=True, edges_seen=True)
        # full profile: both flanks seen all the way down to the band base;
        # only then is the area the bead's whole cross-section
        tol = BASE_TOL * (h if h else top)
        ja, jb = _to_base(rel, ia - 1, -1, tol), _to_base(rel, ib + 1, +1, tol)
        if ja is not None and jb is not None:
            seg = slice(min(ja, jb), max(ja, jb) + 1)
            out.update(full=True, area=float(abs(np.trapezoid(fil[seg], x[seg]))))
    return out


def _to_base(rel, i, step, tol):
    """Walk outward from index i until the profile is down at the band base
    (rel <= tol); None if the seen line ends first."""
    n = len(rel)
    while 0 <= i < n and np.isfinite(rel[i]):
        if rel[i] <= tol:
            return i
        i += step
    return None


def _stands_up(x, fil, xl, xr, split, top) -> bool:
    """A bead stands up on both sides of its centre: a quarter of the width
    in from each edge it is well above the width level. A tilted plate (one
    long ramp) fails this on its low side."""
    ok = np.isfinite(fil)
    if ok.sum() < 2:
        return False
    order = np.argsort(x[ok])
    xs, fs = x[ok][order], fil[ok][order]
    w = xr - xl
    need = split + 0.5 * (top - split)
    return bool(np.interp(xl + 0.25 * w, xs, fs) >= need
                and np.interp(xr - 0.25 * w, xs, fs) >= need)


def _notches_closed(x, fil) -> np.ndarray:
    """`fil` with narrow cuts into the surface filled at the level of their
    rims: narrower than NOTCH_MM and deeper than NOTCH_DEPTH. Everything else
    is left exactly as measured."""
    from scipy.ndimage import grey_closing

    dx = np.abs(np.diff(x))
    dx = dx[np.isfinite(dx) & (dx > 0)]
    if dx.size == 0:
        return np.asarray(fil, float)
    size = max(3, int(round(NOTCH_MM / float(np.median(dx)))) | 1)
    rims = grey_closing(_fill(fil), size=size, mode="nearest")
    return np.where(rims - fil > NOTCH_DEPTH, rims, fil)


def _bridged(x, fil) -> np.ndarray:
    """One pass along the samples: a near-vertical rise whose surface comes
    back to the level of its foot within MAX_SPIKE_MM is bridged at that
    level. A foot on the bed (a narrow bead on a flat bed) does not count."""
    out = np.array(fil, float)
    step = fil[2:] - fil[:-2]                  # over 2 samples: a side can take two
    done = -1
    for a in np.flatnonzero(step > SPIKE_JUMP):
        if a <= done or fil[a] < MIN_FIRST_LAYER:
            continue
        back = np.flatnonzero(fil[a + 2:] <= fil[a] + FOOT_TOL)
        if back.size == 0:
            continue
        e = a + 2 + int(back[0])
        if abs(x[e] - x[a]) <= MAX_SPIKE_MM:
            out[a + 1:e] = np.nan
            done = e
    return _fill(out)


def _despiked(x, fil) -> np.ndarray:
    """`fil` with anything standing on the bead bridged over at the level of
    its feet, from both sides so the order of the samples does not matter.
    Notches are closed first: a cut through a real bead must not leave two
    short pieces that look like spikes."""
    closed = _notches_closed(x, fil)
    return _bridged(x[::-1], _bridged(x, closed)[::-1])[::-1]


def _bead_top(fil) -> float:
    """Median of the middle 40 points, unless they stand on something: above
    the wider centre by more than MAX_CROWN and at most CROWN_LIMIT (taller
    is a narrow bead on a wider layer), with that centre flat enough to be
    the bead's real top (steep flanks in the window would not be) and raised
    above the lowest point (a narrow bead on a flat bed has the bed there,
    and the middle 40 points are its real top)."""
    top = float(np.nanmedian(fil[140:180]))
    wide = fil[CENTRE]
    wide = wide[np.isfinite(wide)]
    if wide.size < 20:
        return top
    level = float(np.median(wide))
    if (MAX_CROWN < top - level <= CROWN_LIMIT and level >= MIN_FIRST_LAYER
            and np.mean(np.abs(wide - level) <= FLAT_TOL) >= FLAT_SHARE):
        return level
    return top


def _joined(x, fil, split) -> np.ndarray:
    """Indices at or above `split` joined to the bead under the middle of the
    line: a cut up to NOTCH_MM wide does not end the bead, anything beyond a
    wider gap is not this bead (the next wall, a stray return)."""
    idx = np.flatnonzero(np.nan_to_num(fil, nan=-1) >= split)
    if idx.size < 2:
        return idx
    apart = np.abs(np.diff(x[idx])) > NOTCH_MM
    runs = np.cumsum(np.r_[False, apart])
    return idx[runs == runs[np.argmin(np.abs(idx - len(fil) // 2))]]


def bead_fixed_offset(x, z_up, real=None) -> dict | None:
    """The paper's method: height below the lowest point seen, top = median
    of the line's centre, width where the bead stands within 4 mm of the top.
    Expects ends="hold" profiles (as the paper did). `real` (bool per point)
    tells which points are real returns, not dropouts filled in.

    A clean profile comes out as the paper measured it. The guards:
    a spike or blob does not set the top (_despiked, _bead_top); only the
    stretch joined to the bead counts, and a notch does not end it (_joined);
    a side with no real return beyond the bead has no edge, it gives how far
    the bead reaches and the width is only a lower bound; with too many
    missing points (`sparse`) the profile gives no width at all. A gap at the
    edge alone is no reason to drop it: a near-vertical flank shadows the
    laser, and the crossing across that gap agrees with the neighbours."""
    x = np.asarray(x, float)
    z = np.asarray(z_up, float)
    if not np.isfinite(z).any():
        return None
    real = np.isfinite(z) if real is None else np.asarray(real, bool)
    fil = z - np.nanmin(z)
    top = _bead_top(_despiked(x, fil))
    split = top - 4.0
    idx = _joined(x, fil, split)
    out = dict(rel=fil, fil=fil, base=np.full(len(z), np.nanmin(z)), top=top,
               split=split, h=None, width=np.nan, area=np.nan,
               xl=np.nan, xr=np.nan, complete=False, edges_seen=False, full=False,
               n_edges=0, edge_lo=np.nan, edge_hi=np.nan, reach_lo=np.nan,
               reach_hi=np.nan, y0=top, sparse=False)
    if len(idx) < 2:
        return out
    a, b = int(idx[0]), int(idx[-1])
    kept = idx[real[idx]]                      # the bead's points that are real
    if (real.mean() < MIN_LINE_RETURNS or real[a:b + 1].mean() < MIN_BEAD_RETURNS
            or kept.size < 2):
        out["sparse"] = True
        return out
    a_seen, b_seen = bool(real[:a].any()), bool(real[b + 1:].any())
    sides = sorted([(x[a] if a_seen else x[kept[0]], a_seen),
                    (x[b] if b_seen else x[kept[-1]], b_seen)])
    out.update(reach_lo=sides[0][0], reach_hi=sides[1][0],
               edge_lo=sides[0][0] if sides[0][1] else np.nan,
               edge_hi=sides[1][0] if sides[1][1] else np.nan,
               n_edges=int(a_seen) + int(b_seen))
    w = sides[1][0] - sides[0][0]
    if a_seen and b_seen and w >= MIN_BEAD_WIDTH:
        out.update(xl=sides[0][0], xr=sides[1][0], width=w, complete=True,
                   edges_seen=True)
    return out


def _fill(a):
    """Linear interpolation over NaN (ends held)."""
    a = np.asarray(a, float)
    ok = np.isfinite(a)
    if ok.sum() < 2:
        return a
    i = np.arange(len(a))
    return np.interp(i, i[ok], a[ok])
