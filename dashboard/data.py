"""
Data behind the print dashboard (dashboard/server.py): the catalogue for the
sidebar, one specimen for the replay (the same frames as the replay video) and
the width-control page (the valve step test of 16 May 2025, the print behind
the NDT-CE 2025 paper, and the arch printed after it the same day).
"""
from __future__ import annotations

import hashlib
import json
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import savgol_filter

_REPO = Path(__file__).resolve().parent.parent
for _p in (_REPO, _REPO / "analysis"):        # repo root, then analysis/
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import replay_video as R
import specimens as S



def published() -> dict:
    """The page's settings: dashboard.json next to the parquet files if there is
    one, else next to this file (the public repository keeps it there, so names
    and DOIs can change while the published data cannot). All keys optional: "board" and "arch" (the step test and its arch, for
    the Width control page), "title", "doi", "code_doi", "notes" (the footer's
    method notes), "prints" ({id: {"title", "mortar"}}) and "default" (the id of
    the print the page opens on)."""
    for p in (S.DATA / "dashboard.json", Path(__file__).with_name("dashboard.json")):
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return {}


_PUB = published()
BOARD, ARCH = _PUB.get("board"), _PUB.get("arch")      # the step test and its arch, when published
ROI = (600.0, 1800.0)                         # the paper's section along X (mm)
LAYERS = (1, 2, 3)                            # the paper's aggregated layers
SECTION_LAYERS = tuple(range(1, 8))           # every layer of a wall, for the cross-section view
FLOW = {42: 8.4, 40: 8.0, 38: 7.6}            # valve % -> water L/min, as in the paper
BAND = S.SCAN_METHODS[0]                      # the layer band: the paper's width
SECTION = S.DEFAULT_METHOD                    # the cross-section view: 4 mm below the top, needs
                                              # only the upper flank, so upper layers have widths
SG_S = 12.0                                   # torque filter: 600 samples at ~50 Hz
X_BIN = 10.0                                  # mm, for the along-the-wall lines


FLOW_TABLE = [{"mix": "W205", "water": 20.5, "density": 1.99, "diameter": 12.10},
              {"mix": "W225", "water": 22.5, "density": 1.94, "diameter": 12.93},
              {"mix": "W245", "water": 24.5, "density": 1.89, "diameter": 14.23}]

WIDTH_BIN_S = 15                               # s of scan profiles behind one error bar
MIN_BIN_PROFILES = 3                           # fewer than this is no spread to show
WHISKER_REACH = 1.5                            # box heights a whisker may reach beyond the box
UPLOADS: dict[str, bytes] = {}                # digest -> pump CSV bytes, this session

# the footer's method notes, unless dashboard.json brings its own
NOTES = [
    "Width: the bead on each laser profile, between the two edges found 4 mm below its top, "
    "one value per profile when both edges are seen. A box holds the middle half of the "
    f"profiles in {WIDTH_BIN_S} s, the whiskers reach {WHISKER_REACH:g} box heights beyond it.",
    "Nothing in the charts is interpolated; the colour of the 3D bead between scan sections is.",
    "W/S: water metered per dose over the dry mortar fed while the mixer ran, kg per kg, shown "
    "as logged. It is displayed only; any interpretation is up to the reader.",
]


def num(a, nd: int = 3) -> list:
    """Array -> JSON list: rounded, None for anything not finite."""
    a = np.asarray(a, float)
    return np.where(np.isfinite(a), np.round(a, nd), None).tolist()


def version() -> float:
    p = S.dim_path()
    return p.stat().st_mtime if p.exists() else 0.0


@lru_cache(maxsize=4)
def _dim(ver: float) -> pd.DataFrame:
    return S.load_dim()


def dim() -> pd.DataFrame:
    return _dim(version())


# ================================================================ catalogue
def catalogue() -> dict:
    d = dim().sort_values("start_time", na_position="last")
    local = pd.to_datetime(d.start_time, utc=True).dt.tz_convert("Europe/Berlin")
    day = pd.to_datetime(d.experiment_date)
    named = pd.to_datetime(d.source_name_date)
    rows = pd.DataFrame({
        "id": d.experiment_id,
        "day": day.dt.strftime("%Y-%m-%d"),
        "board": d.board.fillna("?"),
        "run": d.run,
        "start": local.dt.strftime("%H:%M").fillna("–"),
        "min": (d.duration_s / 60).round(1),
        "kind": d.run_type.fillna("unsure"),
        "pump": d.pump_path.notna(),
        "scan": d.scan_path.notna(),
        "named": named.dt.strftime("%d.%m.%Y"),
        "renamed": (named != day) & named.notna(),
    }).astype(object).where(lambda f: f.notna(), None)
    return {"rows": rows.to_dict("records"), "control": {"board": BOARD, "arch": ARCH}}


# ================================================================ specimens
def add_upload(data: bytes) -> str:
    digest = hashlib.md5(data).hexdigest()
    UPLOADS[digest] = data
    return digest


@lru_cache(maxsize=64)
def _run(key: tuple, ver: float):
    if key[0] == "corpus":
        d = _dim(ver)
        return S.load_run(d[d.experiment_id == key[1]].iloc[0].to_dict())
    return S.upload_run(UPLOADS[key[1]], key[2], key[3])


@lru_cache(maxsize=32)
def specimen(name: str, keys: tuple, ver: float) -> S.Specimen:
    return S.build_specimen(name, [_run(k, ver) for k in keys])


def corpus_specimen(eid: str) -> S.Specimen:
    return specimen(eid, (("corpus", eid),), version())


# ================================================================ replay
def _box(v: np.ndarray) -> list[float]:
    """Lower whisker, lower quartile, median, upper quartile, upper whisker. A whisker
    ends at the furthest value within WHISKER_REACH box heights of the box (Tukey)."""
    q1, median, q3 = np.percentile(v, [25, 50, 75])
    reach = WHISKER_REACH * (q3 - q1)
    return [min(v[v >= q1 - reach].min(), q1), q1, median, q3, max(v[v <= q3 + reach].max(), q3)]


def width_bars(prof: pd.DataFrame) -> dict:
    """The profiles' widths (both edges seen) as one box and whiskers per WIDTH_BIN_S.
    t is the end of the interval: the replay draws the box across the interval and
    quotes its value only once all of it was measured."""
    out = {"bin_s": WIDTH_BIN_S, "t": [], "w": [], "lo": [], "hi": [], "wlo": [], "whi": [], "n": []}
    m = prof[prof.width.notna() & prof.elapsed.notna()]
    if m.empty:
        return out
    bins = np.floor(m.elapsed.to_numpy(float) / WIDTH_BIN_S)
    kept = [(b, v.to_numpy(float)) for b, v in m.groupby(bins).width if len(v) >= MIN_BIN_PROFILES]
    if kept:
        wlo, lo, w, hi, whi = np.array([_box(v) for _, v in kept]).T
        out.update(t=num([(b + 1) * WIDTH_BIN_S for b, _ in kept], 1), w=num(w, 2), lo=num(lo, 2),
                   hi=num(hi, 2), wlo=num(wlo, 2), whi=num(whi, 2), n=[len(v) for _, v in kept])
    return out


def describe(eid: str) -> tuple[str, str]:
    """(title with the print date, date) of a corpus print: dashboard.json's title
    when it names one, else from the experiment id."""
    pub = _PUB.get("prints", {}).get(eid, {})
    if pub.get("title"):
        row = dim().set_index("experiment_id").loc[eid]
        when = f"{pd.Timestamp(row.experiment_date):%d %B %Y}".lstrip("0")
        return f"{pub['title']} · {when}", when
    return R.describe(eid)


def record(sp: S.Specimen, prof: pd.DataFrame, path: pd.DataFrame, when: str, eid: str | None) -> dict:
    """The print's record card, what a batch sheet would carry: date, printing
    time, layers, head speed, valve range, mortar, and how much the scanner saw."""
    p = sp.pump
    moving = p[(p.feed.fillna(0) > 0) & p.layer.notna()] if "feed" in p and "layer" in p else p.iloc[0:0]
    valve = moving.waterValve.dropna() if "waterValve" in moving else pd.Series(dtype=float)
    pub = _PUB.get("prints", {}).get(eid, {}) if eid else {}
    n = len(prof)
    return {"date": when, "printing_s": round(float(path.elapsed.max() - path.elapsed.min()), 1),
            "layers": int(path.layer.max()), "layer_h": float(sp.layer_h or 10),
            "feed_mm_min": round(float(np.nanmedian(moving.feed)), 0) if len(moving) else None,
            "valve": [round(float(valve.min()), 1), round(float(valve.max()), 1)] if len(valve) else None,
            "mortar": pub.get("mortar"), "profiles": n,
            "width_share": round(float(prof.width.notna().mean()), 3) if n else None}


@lru_cache(maxsize=16)
def _frames(name: str, keys: tuple, ver: float) -> tuple:
    """The replay video's frames for one specimen: (specimen, path, signals,
    sampled profiles, usable scan sections, doses)."""
    sp = specimen(name, keys, ver)
    if sp.pump.empty or "pos_x" not in sp.pump or sp.pump.pos_x.notna().sum() < 2:
        raise ValueError("This print has no head positions, so there is no path to replay.")
    if "layer" not in sp.pump or sp.pump.layer.isna().all():
        raise ValueError("No layers were found on this run: the head never printed a path.")
    out = R.frames(sp)
    if len(out[1]) < 2:
        raise ValueError("No printed path on this run.")
    return out


def corpus_id(keys: tuple) -> str | None:
    """The experiment id when the specimen is one corpus print, else None."""
    return keys[0][1] if len(keys) == 1 and keys[0][0] == "corpus" else None


@lru_cache(maxsize=16)
def _replay(name: str, keys: tuple, ver: float) -> dict:
    sp, path, sig, prof, usable, doses = _frames(name, keys, ver)
    share = R.usable_share(sp, usable)
    eid = corpus_id(keys)
    title, when = describe(eid) if eid else (name, "")
    w = path.width.to_numpy(float)
    lo, hi = (np.nanpercentile(w, [2, 98]) if np.isfinite(w).any() else (np.nan, np.nan))
    feed = sp.pump.feed[sp.pump.feed > 0]
    layer_t = path.elapsed[path.layer.diff().fillna(1) != 0]     # layers repeat on a multi-wall print
    bands = 0 < share <= R.BAND_MAX_SHARE
    if not bands:                             # the page draws no bands then: 90 000 points saved
        usable = usable.iloc[0:0]
    return {
        "name": name, "title": title, "when": when,
        "layers": int(path.layer.max()), "layer_h": float(sp.layer_h or 10),
        "feed": float(np.nanmedian(feed) / 1000) if len(feed) else None,
        "t_end": float(path.elapsed.max()), "t_first": float(path.elapsed.min()),
        "range": num([np.floor(lo), np.ceil(hi)], 0),
        "chamber_tau_s": float(R.CHAMBER_TAU_S),
        "band_share": round(share, 3), "bands": bands,
        "layer_t": num(layer_t.to_numpy(), 1),
        "path": {"t": num(path.elapsed, 2), "x": num(path.pos_x, 1), "y": num(path.pos_y, 1),
                 "z": num(path.pos_z, 1), "layer": num(path.layer, 0), "w": num(w, 2),
                 "breaks": np.flatnonzero(~R.joined(path)).tolist()},
        "sig": {"t": num(sig.elapsed, 1), "torque": num(sig.torque_s, 2), "P1": num(sig.P1, 2),
                "P2": num(sig.P2, 2), "T1": num(sig.T1, 2), "ws": num(sig.ws, 4),
                "feed": num(sig.feed / 1000, 2)},
        "doses": {"t": num(doses.elapsed, 1), "ws": num(doses.ws, 4)},
        "width_bars": width_bars(prof),
        "record": record(sp, prof, path, when, eid),
        "usable": {"t": num(usable.elapsed, 2), "x": num(usable.pos_x, 1),
                   "y": num(usable.pos_y, 1), "z": num(usable.pos_z, 1)},
    }


def replay(name: str, keys: tuple) -> dict:
    return _replay(name, keys, version())


# ================================================================ site
def has_step_test() -> bool:
    """The Width control page needs the step test and its arch in the catalogue."""
    d = dim()
    ids = set(d.experiment_id) if not d.empty else set()
    return bool(BOARD and ARCH) and {BOARD, ARCH} <= ids


def site() -> dict:
    """What every page shows: title, the two DOIs, the method notes, the prints'
    names and whether the Width control page has its data."""
    return {"title": _PUB.get("title", "3DCP print dashboard"), "data_doi": _PUB.get("doi"),
            "code_doi": _PUB.get("code_doi"), "notes": _PUB.get("notes", NOTES),
            "prints": _PUB.get("prints", {}), "default_print": _PUB.get("default"),
            "width_control": has_step_test()}


# ================================================================ width control
def walls(p: pd.DataFrame) -> list[dict]:
    """The print's walls: blocks of printing over a minute long, one per valve
    setting, with the pumping into a container between them."""
    q = p[p.feed.fillna(0) > 0]
    el = q.elapsed.to_numpy()
    cut = np.r_[0, np.flatnonzero(np.diff(el) > 60) + 1, len(el)]
    out = []
    for a, b in zip(cut[:-1], cut[1:]):
        if el[b - 1] - el[a] < 60:
            continue
        valve = int(q.waterValve.iloc[a:b].median())
        out.append({"valve": valve, "flow": FLOW.get(valve), "t0": float(el[a]),
                    "t1": float(el[b - 1])})
    return out


def wall_rows(p: pd.DataFrame, prof: pd.DataFrame, ws: list[dict]) -> list[dict]:
    """Width, area and sensors per wall and layer, inside the paper's section."""
    rows = []
    for w in ws:
        sens = p[p.elapsed.between(w["t0"], w["t1"]) & p.pos_x.between(*ROI) & (p.feed > 0)]
        prf = prof[prof.elapsed.between(w["t0"], w["t1"]) & prof.pos_x.between(*ROI)]
        for layer in sorted(int(v) for v in sens.layer.dropna().unique()):
            s, f = sens[sens.layer == layer], prf[prf.layer == layer]
            if len(s) < 50:                  # the last, unfinished lap
                continue
            rows.append({"valve": w["valve"], "flow": w["flow"], "layer": layer,
                         "width": f.width.mean(), "width_sd": f.width.std(),
                         "n": int(f.width.notna().sum()), "profiles": len(f),
                         "area": f.area.mean(), "torque": s.torque_f.mean(),
                         "P0": s.P0.mean(), "P1": s.P1.mean(), "P2": s.P2.mean(),
                         "T0": s.T0.mean()})
    return rows


def along(prof: pd.DataFrame, ws: list[dict]) -> dict:
    """Mean width and area along X, per layer and wall."""
    out = {}
    edges = np.arange(ROI[0], ROI[1] + X_BIN, X_BIN)
    for layer in SECTION_LAYERS:
        out[layer] = []
        for w in ws:
            f = prof[prof.elapsed.between(w["t0"], w["t1"]) & (prof.layer == layer)
                     & prof.pos_x.between(*ROI)]
            b = (f.groupby(pd.cut(f.pos_x, edges, labels=edges[:-1] + X_BIN / 2), observed=True)
                 [["width", "area"]].mean())
            out[layer].append({"flow": w["flow"], "x": num(b.index.astype(float), 0),
                               "width": num(b.width, 2), "area": num(b.area, 1)})
    return out


@lru_cache(maxsize=4)
def _board_prof(ver: float, method: str = BAND) -> pd.DataFrame:
    """Every profile of the step test, by one width method. About 30 s to read, so
    it is kept on disk until the corpus changes."""
    tag = "" if method == BAND else "offset_"
    keep = S.DATA / "dashboard_cache" / f"board_profiles_{tag}{int(ver)}.parquet"
    if keep.exists():
        return pd.read_parquet(keep)
    prof = S.profile_widths(corpus_specimen(BOARD), method, S.ALL_DIRECTIONS, per_layer=100_000)
    keep.parent.mkdir(exist_ok=True)
    prof.to_parquet(keep)
    return prof


def arch_check(step: list[dict]) -> dict | None:
    """The arch printed after the step test at 41 %: does its width land where
    the steps say? Volume per time at one valve setting is the same whatever
    the head speed, so the step-test width at 41 % scales by feed."""
    ids = set(dim().experiment_id)
    if ARCH not in ids:
        return None
    sp = corpus_specimen(ARCH)
    prof = S.profile_widths(sp, BAND, S.ALL_DIRECTIONS, per_layer=150)
    m = prof[prof.width.notna() & (prof.layer >= 2)]
    p = sp.pump[sp.pump.t.notna()]
    printing = p[(p.feed.fillna(0) > 0) & p.layer.notna()]
    by = (m.groupby("layer").agg(width=("width", "median"), feed=("feed", "median"),
                                 n=("width", "size")).reset_index())
    ref = {r["valve"]: r for r in step if r["layer"] == 2}
    w41 = float(np.mean([ref[42]["width"], ref[40]["width"]]))
    feed_board = 10500.0
    pred = w41 * feed_board / by.feed.to_numpy()
    return {"layer": num(by.layer, 0), "width": num(by.width, 2), "feed": num(by.feed, 0),
            "predicted": num(pred, 2), "valve": float(printing.waterValve.median()),
            "torque": float(printing.torque.mean()), "layers": int(by.layer.max()),
            "width_median": float(m.width.median()), "predicted_median": float(np.median(pred)),
            "w41_at_board_feed": w41, "board_feed": feed_board,
            "feed_median": float(by.feed.median())}


@lru_cache(maxsize=2)
def _width_control(ver: float) -> dict:
    sp = corpus_specimen(BOARD)
    prof = _board_prof(ver)
    p = sp.pump[sp.pump.t.notna()].sort_values("elapsed").copy()
    p["torque_f"] = S.smooth(p.t.to_numpy(), p.torque.to_numpy(), S.FILTERS[1], SG_S, 2)
    ws = walls(p)
    rows = wall_rows(p, prof, ws)
    sec = (p.assign(sec=np.floor(p.elapsed)).groupby("sec")
           [["torque_f", "P0", "P2", "T0", "waterValve"]].mean().reset_index())
    fit = prof[prof.width.notna() & prof.area.notna() & prof.pos_x.between(*ROI)]
    pairs = {}
    for layer in (2, 3):                      # layer 1 sits on the bed: its area is another band
        f = fit[fit.layer == layer]
        pairs[layer] = {"r": float(f.width.corr(f.area)), "n": len(f)}
    f = fit[fit.layer == 2]
    keep = f.iloc[:: max(1, len(f) // 900)]
    flow_of = np.select([keep.elapsed.between(w["t0"], w["t1"]) for w in ws],
                        [w["flow"] for w in ws], default=np.nan)
    path = p[p.layer.notna() & (p.feed.fillna(0) > 0)]
    path = path.iloc[:: 10]
    flow_path = np.select([path.elapsed.between(w["t0"], w["t1"]) for w in ws],
                          [w["flow"] for w in ws], default=np.nan)
    wide = {r["valve"]: r for r in rows if r["layer"] == 2}
    fix = sorted(wide)
    gain = (wide[fix[-1]]["width"] - wide[fix[0]]["width"]) / (fix[-1] - fix[0])
    return {
        "eid": BOARD, "walls": ws, "rows": [{k: (num([v], 3)[0] if isinstance(v, float) else v)
                                             for k, v in r.items()} for r in rows],
        "flow_table": FLOW_TABLE, "roi": list(ROI),
        "series": {"t": num(sec.sec, 0), "torque": num(sec.torque_f, 2), "P0": num(sec.P0, 2),
                   "P2": num(sec.P2, 2), "T0": num(sec.T0, 2), "valve": num(sec.waterValve, 1)},
        "along": along(_board_prof(ver, SECTION), ws),
        "scatter": {"width": num(keep.width, 2), "area": num(keep.area, 1),
                    "flow": num(flow_of, 1)},
        "fit": pairs, "gain_mm_per_pct": float(gain),
        "plan": {"x": num(path.pos_x, 1), "y": num(path.pos_y, 1), "flow": num(flow_path, 1)},
        "feed_mm_min": float(np.nanmedian(p.feed[p.feed > 0])),
        "arch": arch_check(rows),
    }


def width_control() -> dict:
    return _width_control(version())


def scan_profiles(layer: int, x: float) -> dict:
    """The bead's cross-section at X in one layer, one per wall: the profile
    nearest to X with both edges seen, centred on the bead, as height relative
    to the bead's top (the width is read 4 mm below it)."""
    ver = version()
    sp = corpus_specimen(BOARD)
    prof = _board_prof(ver, SECTION)
    p = sp.pump[sp.pump.t.notna()]
    out = []
    for w in walls(p):
        f = prof[prof.elapsed.between(w["t0"], w["t1"]) & (prof.layer == layer)
                 & prof.width.notna()]
        if f.empty:
            continue
        r = f.iloc[int(np.argmin(np.abs(f.pos_x.to_numpy() - x)))]
        raw = S.read_profiles(r.path, [int(r.sample_seq)]).get(int(r.sample_seq))
        if raw is None:
            continue
        g = S.profile_geometry(*raw, sp.orientation.get(r.path), S.layer_height(sp, layer), SECTION)
        if g is None or not np.isfinite(g["xl"]):
            continue
        mid = (g["xl"] + g["xr"]) / 2
        xs, zs = g["x"] - mid, g["fil"] - g["top"]
        keep = np.isfinite(zs) & np.isfinite(g["z_fix"]) & (np.abs(xs) < 30)      # not the held line ends
        out.append({"flow": w["flow"], "x": num(xs[keep], 2), "z": num(zs[keep], 2),
                    "width": float(r.width), "area": None if pd.isna(r.area) else float(r.area),
                    "at_x": float(r.pos_x), "y": float(r.pos_y)})
    return {"layer": layer, "x": x, "profiles": out}
