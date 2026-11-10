"""
Specimen data for the print dashboard, built from the unified corpus (data/)
or an uploaded pump CSV.

A specimen is one experiment, or several runs of the same print combined in
time order (real gaps between runs are kept). Everything is derived from the
fact tables with the same conform / calibrate code as the ingest, so the
dashboard and the corpus always agree.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from functools import lru_cache

from ingest import geometry as G
from ingest.calibrate import calibrate_pump, detect_era
from ingest.convert import convert_pump_csv
from ingest.standardize import to_fct_motion, to_fct_pump
from water import calculate_water

REPO = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("PRINT_DATA", REPO / "data"))   # the dataset folder
SENSORS = ["torque", "P0", "P1", "P2", "P3", "T0", "T1"]
MIN_MEASURED = 5             # profiles a layer needs before it gets a width
FEED_REF = 9000.0            # mm/min: feed-normalised width = width x feed / FEED_REF
FILTERS = ["None", "Savitzky–Golay", "Rolling median", "Low-pass (Butterworth)"]
SCAN_METHODS = G.METHODS              # [layer band, paper's fixed 4 mm offset]
DEFAULT_METHOD = SCAN_METHODS[1]      # finds both edges more often (64-95% vs 55-88%)
LEGACY_METHODS = {"Fixed 4 mm offset (L2−Δ)": G.METHODS[1],
                  "Half-height (sloped base)": G.METHODS[0]}


# ================================================================ corpus
def dim_path() -> Path:
    return DATA / "dim_experiment.parquet"


def load_dim() -> pd.DataFrame:
    p = dim_path()
    if not p.exists():
        return pd.DataFrame()
    d = pq.read_table(p).to_pandas()
    d["experiment_date"] = pd.to_datetime(d["experiment_date"]).dt.date
    return d


def _t(col) -> np.ndarray:
    """TIMESTAMP column -> seconds since epoch (float, NaN for NULL). float64
    keeps ~0.2 µs at today's epoch, plenty for 100 Hz data."""
    ns = pc.cast(pc.cast(col, pa.int64()), pa.float64(), safe=False)
    return np.array(ns.to_numpy(zero_copy_only=False), dtype=float) / 1e9


def _f(tbl: pa.Table, name: str) -> np.ndarray:
    return np.array(pc.cast(tbl[name], pa.float64()).to_numpy(zero_copy_only=False),
                    dtype=float)


def _b(tbl: pa.Table, name: str) -> np.ndarray:
    return np.array(pc.fill_null(tbl[name], False).to_numpy(zero_copy_only=False),
                    dtype=bool)


def pump_frame(tbl: pa.Table, era: str) -> pd.DataFrame:
    """fct_pump -> calibrated frame (torque %, P bar, T degC, flow L/min)."""
    cal = calibrate_pump(tbl, era)
    n = tbl.num_rows
    df = pd.DataFrame({"t": _t(tbl["event_time"]),
                       "sample_seq": tbl["sample_seq"].to_numpy()})
    for k in ("p0", "p1", "p2", "p3", "t0", "t1"):
        df[k.upper()] = cal.get(k, np.full(n, np.nan))
    df["torque"] = cal.get("torque", np.full(n, np.nan))
    df["waterFlow"] = _f(tbl, "water_flow_raw") * 20.4 / (2**16 - 1)
    # water temperature: degC floats in 2023; from 2024 integer counts of 1/1000 degC
    wt = _f(tbl, "water_temp_raw")
    df["waterT"] = wt / 1000.0 if era == "new" else wt
    df["waterValve"] = _f(tbl, "water_valve")
    df["mixer"] = _b(tbl, "mixer")
    df["waterPump"] = _b(tbl, "water_pump")
    return df


def motion_frame(tbl: pa.Table) -> pd.DataFrame:
    df = pd.DataFrame({"t": _t(tbl["event_time"]),
                       "sample_seq": tbl["sample_seq"].to_numpy()})
    for c in ("pos_x", "pos_y", "pos_z", "pos_c", "pos_b",
              "vel_x", "vel_y", "vel_z"):
        df[c] = _f(tbl, c)
    df["feed"] = np.hypot(df.vel_x, df.vel_y)                     # mm/min
    df["angle"] = np.degrees(np.arctan2(df.vel_y, df.vel_x))     # travel direction
    return df


def scan_index(path: Path) -> pd.DataFrame:
    """Light per-profile index (no arrays): time, position in the log, head."""
    cols = ["event_time", "sample_seq", "head_x", "head_y", "head_z", "head_c"]
    tbl = pq.read_table(path, columns=cols)
    df = pd.DataFrame({"t": _t(tbl["event_time"]),
                       "sample_seq": tbl["sample_seq"].to_numpy()})
    for c in cols[2:]:
        df[c] = _f(tbl, c)
    return df


@dataclass
class Run:
    key: str                          # experiment_id, or the upload's file name
    pump: pd.DataFrame | None
    motion: pd.DataFrame | None
    scan: pd.DataFrame | None
    scan_path: Path | None
    era: str
    orientation: str | None = None    # 'upright' / 'inverted' scan z


@lru_cache(maxsize=512)
def file_orientation(path: str) -> str | None:
    """Orientation read from a scan file's profiles, for corpus rows written
    before dim_experiment carried scan_orientation."""
    f = pq.ParquetFile(path)
    k = f.num_row_groups
    rgs = sorted(set(np.linspace(0, k - 1, min(k, 8)).astype(int)))
    t = f.read_row_groups(rgs, columns=["profile_x", "profile_z"])
    t = t.take(list(range(0, t.num_rows, max(1, t.num_rows // 2000))))
    xs, zs = t.column(0).to_pylist(), t.column(1).to_pylist()
    return G.detect_orientation(
        (x, z) for x, z in zip(xs, zs) if x is not None and z is not None)[0]


def load_run(row: dict) -> Run:
    """One corpus experiment (a dim_experiment row as dict)."""
    era = row.get("sensor_era") if isinstance(row.get("sensor_era"), str) else "none"
    get = lambda c: DATA / row[c] if isinstance(row.get(c), str) and row[c] else None
    pp, mp, sp = get("pump_path"), get("motion_path"), get("scan_path")
    orient = row.get("scan_orientation")
    if not isinstance(orient, str) and sp is not None and "scan_orientation" not in row:
        orient = file_orientation(str(sp))
    return Run(key=row["experiment_id"],
               pump=pump_frame(pq.read_table(pp), era) if pp else None,
               motion=motion_frame(pq.read_table(mp)) if mp else None,
               scan=scan_index(sp) if sp else None, scan_path=sp, era=era,
               orientation=orient if isinstance(orient, str) else None)


def upload_run(data: bytes, name: str, era_choice: str = "Auto") -> Run:
    """An uploaded pump CSV, conformed exactly like the ingest does."""
    fd, tmp = tempfile.mkstemp(suffix=".csv")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        raw, man = convert_pump_csv(tmp)
    finally:
        os.unlink(tmp)
    fct_pump, _ = to_fct_pump(raw, name)
    _, fct_motion = to_fct_motion(raw, name)
    era = (detect_era(man["columns"], fct_pump) if era_choice == "Auto"
           else {"New era": "new", "Old era": "old"}[era_choice])
    return Run(key=name, pump=pump_frame(fct_pump, era),
               motion=motion_frame(fct_motion) if fct_motion is not None else None,
               scan=None, scan_path=None, era=era)


# ================================================================ specimen
def _asof(left: pd.DataFrame, right: pd.DataFrame | None, cols: list[str]):
    """Attach the last `right` reading at or before each left row (by t)."""
    out = left.copy()
    for c in cols:
        out[c] = np.nan
    if right is None or right.empty or out.empty:
        return out
    lv = out.loc[out.t.notna(), ["t"]].reset_index().sort_values("t")
    rv = right[["t"] + cols].dropna(subset=["t"]).sort_values("t")
    if lv.empty or rv.empty:
        return out
    m = pd.merge_asof(lv, rv, on="t", direction="backward")
    out.loc[m["index"].to_numpy(), cols] = m[cols].to_numpy()
    return out


def head_motion(scan: pd.DataFrame | None) -> pd.DataFrame | None:
    """Motion rebuilt from the scanner's own head positions, for experiments
    without a pump log (scan only). The scan log repeats the last known head
    position between controller updates (~10 Hz under ~40 Hz profiles), so
    only real updates are kept, as for fct_motion; where the head stops, a
    zero-speed sample closes the move so the last speed doesn't carry on."""
    if scan is None or scan.empty or scan.head_x.isna().all():
        return None
    m = scan[["t", "sample_seq", "head_x", "head_y", "head_z", "head_c"]].dropna(
        subset=["t", "head_x", "head_y"]).sort_values("t").reset_index(drop=True)
    m = m.rename(columns={f"head_{a}": f"pos_{a}" for a in "xyzc"})
    pos = m[["pos_x", "pos_y", "pos_z"]].to_numpy()
    moved = np.r_[True, np.any(np.diff(pos, axis=0) != 0, axis=1)]
    m = m[moved].reset_index(drop=True)
    if len(m) < 3:
        return None
    t = m.t.to_numpy()
    dt = np.diff(t)
    typical = float(np.median(dt[dt > 0])) if (dt > 0).any() else 0.1
    vel = {a: np.r_[0.0, np.diff(m[f"pos_{a}"].to_numpy()) / np.where(dt > 0, dt, np.nan)] * 60.0
           for a in "xyz"}
    for a in "xyz":                        # speed over the interval ending at each update
        m[f"vel_{a}"] = vel[a]
    long_gap = np.r_[dt > 3 * typical, False]          # head stood still after this update
    if long_gap.any():
        stops = m[long_gap].copy()
        stops["t"] = stops.t + 2 * typical
        for a in "xyz":
            stops[f"vel_{a}"] = 0.0
        m = pd.concat([m, stops]).sort_values("t").reset_index(drop=True)
    m["pos_b"] = np.nan
    m["feed"] = np.hypot(m.vel_x, m.vel_y)
    m["angle"] = np.degrees(np.arctan2(m.vel_y, m.vel_x))
    return m


@dataclass
class Specimen:
    name: str
    runs: list[Run]
    pump: pd.DataFrame            # calibrated + elapsed, run, pos_*, feed, angle, layer
    scans: pd.DataFrame           # profile index + elapsed, run, path, pos_*, angle, layer
    doses: pd.DataFrame
    layer_h: float | None         # typical Z_i - Z_(i-1)
    t_first: float | None
    layer_heights: dict           # {layer: h} for layers >= 2 (layer 1 sits on the bed)
    c_rotates: bool | None        # C turns with the path (None: no C axis)
    orientation: dict             # scan file path -> 'upright' / 'inverted' / None


def build_specimen(name: str, runs: list[Run]) -> Specimen:
    starts = [x for r in runs for x in
              ([r.pump.t.min()] if r.pump is not None else [])
              + ([r.scan.t.min()] if r.scan is not None else [])
              if np.isfinite(x)]
    t_first = min(starts) if starts else None
    pumps, motions, scans, doses = [], [], [], []
    for i, r in enumerate(runs):
        mo = r.motion if r.motion is not None else head_motion(r.scan)
        if mo is not None:
            motions.append(mo.assign(run=i))
        if r.pump is not None:
            p = r.pump.assign(run=i, elapsed=r.pump.t - (t_first or 0.0))
            pumps.append(p)
            pv = p[p.t.notna()].reset_index(drop=True)
            if len(pv) > 10:
                w = calculate_water(pv)
                if not w.empty:
                    doses.append(w.assign(run=i))
        if r.scan is not None:
            scans.append(r.scan.assign(run=i, path=str(r.scan_path),
                                       elapsed=r.scan.t - (t_first or 0.0)))
    cat = lambda xs: (pd.concat(xs, ignore_index=True).sort_values(["t", "run"])
                      .reset_index(drop=True) if xs else pd.DataFrame())
    motion, pump, scan = cat(motions), cat(pumps), cat(scans)
    h_typ, h_layer, c_rot = None, {}, None
    if not motion.empty:                    # layers: Z_i - Z_(i-1) along the path
        mv = motion.feed.fillna(0) > 0
        pth = motion[mv]
        _, layers, h_typ, h_layer = G.path_layers(pth.t, pth.pos_x, pth.pos_y, pth.pos_z)
        motion["layer"] = np.nan
        motion.loc[pth.index, "layer"] = layers
        c_rot = G.c_axis_rotates(motion.pos_c, mv)
    mcols = ["pos_x", "pos_y", "pos_z", "pos_c", "feed", "angle", "layer"]
    mo = motion if not motion.empty else None
    if not pump.empty:
        pump = _asof(pump, mo, mcols)
    if not scan.empty:
        scan = _asof(scan, mo, mcols)
        for a in ("x", "y", "z"):                 # no motion log: use the scanner's
            scan[f"pos_{a}"] = scan[f"pos_{a}"].fillna(scan[f"head_{a}"])
    dose = (pd.concat(doses, ignore_index=True).assign(specimen=name)
            if doses else pd.DataFrame())
    orient = {str(r.scan_path): r.orientation for r in runs if r.scan_path is not None}
    return Specimen(name=name, runs=runs, pump=pump, scans=scan, doses=dose,
                    layer_h=h_typ, t_first=t_first, layer_heights=h_layer,
                    c_rotates=c_rot, orientation=orient)


def layer_height(sp: Specimen, layer) -> float | None:
    """Band height of a layer: None for layer 1 (it sits on the substrate)."""
    if layer is None or not np.isfinite(layer) or int(layer) <= 1:
        return None
    return sp.layer_heights.get(int(layer), sp.layer_h)


# ================================================================ geometry
def profile_geometry(x, z, orientation: str | None, h: float | None,
                     method: str = SCAN_METHODS[0]) -> dict | None:
    """Raw profile -> bead geometry (see ingest.geometry). The width comes
    from `method`; the area always from the layer band, and only for a full
    profile (both flanks seen down to layer i-1), whichever method. Adds the
    prepared arrays for plotting: x/z as the method used them, x_fix/z_fix
    (dropouts inside the line filled, upright, ends not seen = NaN)."""
    method = LEGACY_METHODS.get(method, method)
    xn, zn = G.prepare(x, z, orientation)
    band = G.bead(xn, zn, h)
    if method == SCAN_METHODS[1]:
        xf, zf = G.prepare(x, z, orientation, ends="hold")
        zr = np.asarray(z, float)
        g = G.bead_fixed_offset(xf, zf, np.isfinite(zr) & (zr != 0))
        if g is not None:
            g.update(area=band["area"] if band else np.nan,
                     full=bool(band and band["full"]))
    else:
        xf, zf, g = xn, zn, band
    if g is not None:
        g.update(x=xf, z=zf, x_fix=xn, z_fix=zn)
    return g


def read_profiles(path: str, seqs) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """Raw (x, z) arrays for the given profiles of one scan file."""
    seqs = [int(s) for s in seqs]
    if not seqs:
        return {}
    tbl = pq.read_table(path, columns=["sample_seq", "profile_x", "profile_z"],
                        filters=[("sample_seq", "in", seqs)])
    out = {}
    for s, x, z in zip(tbl["sample_seq"].to_pylist(), tbl["profile_x"].to_pylist(),
                       tbl["profile_z"].to_pylist()):
        if x is not None and z is not None:
            out[int(s)] = (np.array(x, float), np.array(z, float))
    return out


# ---------------------------------------------------------------- sections
# Which profiles count as a cross-section of the fresh bead. The scanner
# trails the nozzle when the head travels within +-5 deg of -C (the C axis
# counts the other way round from the travel angle; +X without a C axis): the
# other way it leads and sees the layer below, and across it the line runs
# along the bead. This alignment is always applied. When C is fixed (or absent)
# it pins the travel direction, so the direction filter is locked; when C turns
# with the path, nearly every profile is a cross-section and the travel
# direction is free. X / Y windows are optional.
ALL_DIRECTIONS = {"angle": [-180, 180], "x": None, "y": None}


def direction_locked(sp: Specimen) -> bool:
    return not sp.c_rotates


def fixed_c(sp: Specimen) -> float:
    """C the scanner was held at (0 without a C axis)."""
    c = sp.scans.pos_c.dropna() if "pos_c" in sp.scans else pd.Series(dtype=float)
    return float(c.median()) if len(c) else 0.0


def section_mask(s: pd.DataFrame, sec: dict, locked: bool = False) -> pd.Series:
    c = s.pos_c if "pos_c" in s else np.nan
    m = pd.Series(G.scanner_aligned(s.angle, c), index=s.index) & s.angle.notna()
    lo, hi = sec.get("angle") or (-180, 180)
    if not locked and (lo, hi) != (-180, 180):
        m &= s.angle.between(lo, hi)
    if sec.get("x"):
        m &= s.pos_x.between(*sec["x"])
    if sec.get("y"):
        m &= s.pos_y.between(*sec["y"])
    return m


def describe_section(sp: Specimen, sec: dict) -> str:
    if direction_locked(sp):
        d = f"travel {(-fixed_c(sp)) % 360:.0f}±{G.ALIGN_TOL_DEG:.0f}° (scanner fixed at C {fixed_c(sp):.0f})"
    else:
        lo, hi = sec.get("angle") or (-180, 180)
        d = "travel ∥ C" + ("" if (lo, hi) == (-180, 180) else f", {lo:+.0f}…{hi:+.0f}°")
    parts = [d]
    if sec.get("x"):
        parts.append(f"X {sec['x'][0]:.0f}–{sec['x'][1]:.0f}")
    if sec.get("y"):
        parts.append(f"Y {sec['y'][0]:.0f}–{sec['y'][1]:.0f}")
    return ", ".join(parts)


def _even(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Up to n rows evenly spaced in time."""
    if len(df) <= n:
        return df
    targets = np.linspace(df.elapsed.min(), df.elapsed.max(), n)
    idx = np.unique(np.searchsorted(df.elapsed.to_numpy(), targets).clip(0, len(df) - 1))
    if len(idx) < n:                    # time gaps (pauses, joined runs): by count
        idx = np.unique(np.linspace(0, len(df) - 1, n).round().astype(int))
    return df.iloc[idx]


def profile_widths(sp: Specimen, method: str, section: dict,
                   per_layer: int = 150) -> pd.DataFrame:
    """One row per sampled profile (up to `per_layer` per layer, taken inside
    the section while the head was moving): the measured width and area
    (both edges seen), plus each side's edge or reach for the estimates."""
    cols = ["layer", "path", "sample_seq", "width", "area", "edges", "n_edges",
            "edge_lo", "edge_hi", "reach_lo", "reach_hi",
            "pos_x", "pos_y", "pos_z", "feed", "elapsed", "full"]
    s = sp.scans
    if s.empty or "layer" not in s or s.layer.isna().all():
        return add_estimates(pd.DataFrame(columns=cols))
    s = s[s.layer.notna() & (s.feed.fillna(0) > 0)
          & section_mask(s, section, direction_locked(sp))]
    if s.empty:
        return add_estimates(pd.DataFrame(columns=cols))
    s = s.sort_values("elapsed")
    picked = pd.concat([_even(g, per_layer) for _, g in s.groupby("layer")])
    rows = []
    for path, g in picked.groupby("path"):
        prof = read_profiles(path, g.sample_seq)
        orient = sp.orientation.get(path)
        for r in g.itertuples(index=False):
            seq, layer = int(r.sample_seq), r.layer
            if seq not in prof:
                continue
            where = (r.pos_x, r.pos_y, r.pos_z, r.feed, r.elapsed)
            geo = profile_geometry(*prof[seq], orient, layer_height(sp, layer), method)
            if geo is None:
                rows.append((layer, path, seq, np.nan, np.nan, False, 0,
                             np.nan, np.nan, np.nan, np.nan, *where, False))
                continue
            ok, full = geo["complete"], bool(geo.get("full"))
            rows.append((layer, path, seq, geo["width"] if ok else np.nan,
                         geo["area"] if full else np.nan, bool(geo.get("edges_seen")),
                         geo["n_edges"], geo["edge_lo"], geo["edge_hi"],
                         geo["reach_lo"], geo["reach_hi"], *where, full))
    return add_estimates(pd.DataFrame(rows, columns=cols))


def add_estimates(df: pd.DataFrame) -> pd.DataFrame:
    """Width estimates for profiles that miss an edge, kept apart from the
    measured width.

    width_est  one edge seen: twice the distance from that edge to the bead
               centre, the centre being the median over that scan file's
               fully measured profiles (needs MIN_MEASURED of them). Biased
               low where the bead drifts off-centre (that is why an edge
               leaves the line), so it is never mixed into `width`. Never
               narrower than what the laser saw standing: the mirrored edge
               cannot lie inside the visible bead.
    width_lb   the bead runs off the line, with no edge seen or with one edge
               and a mirrored edge that would lie inside the visible bead: it
               spans at least what was seen, so this is only a lower bound
               (width >= it)."""
    df = df.copy()
    for c in ("centre", "width_est", "width_lb", "width_fn", "h_eff", "vflow", "vflow_w"):
        df[c] = np.nan
    if df.empty:
        return df
    # volume flow out = cross-section x head speed. The area is trusted only
    # on full profiles; widths exist far more often, so each scan file gets a
    # fitted effective bead height h_eff = median(area / width) over its full,
    # measured profiles (needs MIN_MEASURED), and area ~ h_eff x width.
    both = df[df.area.notna() & df.width.notna()]
    fit = (both.area / both.width).groupby(both.path).agg(["median", "size"])
    df["h_eff"] = df.path.map(fit["median"].where(fit["size"] >= MIN_MEASURED)).astype(float)
    df["vflow"] = df.area * df.feed * 1e-6                      # mm^3/min -> L/min
    df["vflow_w"] = df.h_eff * df.width * df.feed * 1e-6
    # feed-normalised width: at constant pump output the cross-section is
    # flow / head speed, so width x feed / FEED_REF is the width this bead
    # would have had at the reference feed (per profile, its own feed)
    df["width_fn"] = df.width * df.feed / FEED_REF
    full = df[df.width.notna() & df.edge_lo.notna() & df.edge_hi.notna()]
    mid = (full.edge_lo + full.edge_hi) / 2
    stats = mid.groupby(full.path).agg(["median", "size"])
    centre = stats["median"].where(stats["size"] >= MIN_MEASURED)
    df["centre"] = df.path.map(centre).astype(float)
    one = df.width.isna() & (df.n_edges == 1)
    est = np.where(df.edge_lo.notna(), 2 * (df.centre - df.edge_lo),
                   2 * (df.edge_hi - df.centre))
    span = df.reach_hi - df.reach_lo
    possible = est >= span                      # False where the centre is unknown
    df["width_est"] = np.where(one & possible & (est >= G.MIN_BEAD_WIDTH), est, np.nan)
    off_line = ((df.n_edges == 0) | (one & ~possible & (span >= G.MIN_BEAD_WIDTH)))
    df["width_lb"] = np.where(df.width.isna() & df.reach_lo.notna() & off_line, span, np.nan)
    return df


def summarise_widths(df: pd.DataFrame) -> pd.DataFrame:
    """Per layer. `width` / `area`: median of the measured profiles, only
    when at least MIN_MEASURED were measured. `width_est` and `width_lb`:
    the one-edge estimate and the no-edge lower bound, same rule."""
    cols = ["layer", "width", "area", "profiles", "measured", "edges_seen",
            "width_est", "one_edge", "width_lb", "no_edge", "width_fn", "full",
            "h_eff", "vflow", "vflow_w"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    g = df.groupby("layer")
    n, ne, nl = g.width.count(), g.width_est.count(), g.width_lb.count()
    nf = g.area.count()
    return pd.DataFrame({
        "width": g.width.median().where(n >= MIN_MEASURED),
        "area": g.area.median().where(nf >= MIN_MEASURED), "full": nf,
        "h_eff": g.h_eff.median(),
        "vflow": g.vflow.median().where(nf >= MIN_MEASURED),
        "vflow_w": g.vflow_w.median().where(g.vflow_w.count() >= MIN_MEASURED),
        "profiles": g.size(), "measured": n, "edges_seen": g.edges.sum(),
        "width_est": g.width_est.median().where(ne >= MIN_MEASURED), "one_edge": ne,
        "width_lb": g.width_lb.median().where(nl >= MIN_MEASURED), "no_edge": nl,
        "width_fn": g.width_fn.median().where(n >= MIN_MEASURED),
    }).reset_index()


def layer_widths(sp: Specimen, method: str, section: dict,
                 per_layer: int = 150) -> pd.DataFrame:
    return summarise_widths(profile_widths(sp, method, section, per_layer))


def width_track(prof: pd.DataFrame, elapsed, printing) -> tuple[np.ndarray, np.ndarray]:
    """Width at every time point while printing: linear in time between the
    profiles with a measured width (exact where one was taken), NaN outside
    the first-last measurement. -> (width, gap_s), gap_s = time to the
    nearest measured profile (0 = measured, large = long interpolation). A
    model on the continuous pump signals is meant to replace the
    interpolation later."""
    e = np.asarray(elapsed, float)
    w = np.full(len(e), np.nan)
    gap = np.full(len(e), np.nan)
    m = prof[prof.width.notna() & prof.elapsed.notna()].sort_values("elapsed")
    if len(m) < 2:
        return w, gap
    te, tw = m.elapsed.to_numpy(), m.width.to_numpy()
    ok = np.asarray(printing, bool) & np.isfinite(e) & (e >= te[0]) & (e <= te[-1])
    w[ok] = np.interp(e[ok], te, tw)
    j = np.searchsorted(te, e[ok]).clip(1, len(te) - 1)
    gap[ok] = np.minimum(np.abs(e[ok] - te[j - 1]), np.abs(te[j] - e[ok]))
    return w, gap


def bead_centres(df: pd.DataFrame) -> dict:
    """Scan file -> bead centre (x, mm) from a profile_widths table."""
    if df.empty:
        return {}
    c = df.dropna(subset=["centre"]).groupby("path").centre.first()
    return {k: float(v) for k, v in c.items()}


def view_profiles(sp: Specimen, layer, section: dict, n: int = 20):
    """Profiles of one layer inside a section, at most n evenly spaced in
    time. -> (rows, layer start in elapsed seconds)."""
    s = sp.scans
    if s.empty:
        return s, None
    s = s[s.layer == layer].sort_values("elapsed")
    if s.empty:
        return s, None
    start = float(s.elapsed.min())
    s = s[(s.feed.fillna(0) > 0) & section_mask(s, section, direction_locked(sp))]
    return _even(s, n), start


def layers_in_section(sp: Specimen, section: dict) -> list[int]:
    """Layers that hold at least one usable profile inside the section."""
    s = sp.scans
    if s.empty or "layer" not in s:
        return []
    s = s[s.layer.notna() & (s.feed.fillna(0) > 0)
          & section_mask(s, section, direction_locked(sp))]
    return sorted(int(v) for v in s.layer.unique())


# ================================================================ signals
def sample_rate(t) -> float:
    """Median sampling rate (Hz) of a time column in seconds."""
    d = np.diff(np.asarray(t, float))
    d = d[np.isfinite(d) & (d > 0)]
    return float(1.0 / np.median(d)) if d.size else 1.0


def smooth(t, y, kind: str, window_s: float, order: int = 2) -> np.ndarray:
    """Filter one run of a signal. The window is in seconds so it means the
    same at 10 Hz and 100 Hz; NaN gaps are bridged for the filter and put
    back afterwards."""
    from scipy.signal import butter, filtfilt, savgol_filter

    y = np.asarray(y, float)
    ok = np.isfinite(y)
    if kind == FILTERS[0] or ok.sum() < 5:
        return y
    fs = sample_rate(t)
    n = max(3, int(round(window_s * fs)))
    yf = pd.Series(y).interpolate(limit_direction="both").to_numpy()
    if kind == FILTERS[1]:
        n = min(n | 1, len(yf) - (1 - len(yf) % 2))      # odd, <= length
        if n < 3:
            return y
        out = savgol_filter(yf, n, min(order, n - 1))
    elif kind == FILTERS[2]:
        out = pd.Series(yf).rolling(n, center=True, min_periods=1).median().to_numpy().copy()
    else:                                               # cut-off = 1 / window
        wn = min(0.99, (1.0 / window_s) / (fs / 2.0))
        b, a = butter(min(order, 4), wn)
        if len(yf) <= 3 * max(len(a), len(b)):
            return y
        out = filtfilt(b, a, yf)
    out[~ok] = np.nan
    return out


def minmax_pick(y, max_points: int) -> np.ndarray:
    """Row positions to plot at most ~max_points: per bucket the min and the
    max of y, so spikes survive the decimation."""
    y = np.asarray(y, float)
    n = len(y)
    if n <= max_points:
        return np.arange(n)
    nb = max(1, max_points // 2)
    size = int(np.ceil(n / nb))
    a = np.concatenate([y, np.full(nb * size - n, np.nan)]).reshape(nb, size)
    nan = np.isnan(a)
    lo = np.where(nan, np.inf, a).argmin(axis=1)
    hi = np.where(nan, -np.inf, a).argmax(axis=1)
    start = np.arange(nb) * size
    idx = np.unique(np.concatenate([start + lo, start + hi]))
    return idx[idx < n]
