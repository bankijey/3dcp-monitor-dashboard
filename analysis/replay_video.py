"""
Replay video of one print: the path builds up layer by layer in 3D, coloured
by the width track (measured where a scan profile was taken, interpolated in
time between, as in the dashboard's print map), with the usable scan sections
(travel within 5 deg of C, where the scanner sees the fresh bead) in red. The
pump signals run alongside on the same clock. Writes an H.264 MP4.

Run:  python analysis/replay_video.py <experiment_id> --seconds 40 --title "My print"
      python analysis/replay_video.py <experiment_id> --stills 0.05 0.5 1    # PNG frames only
      python analysis/replay_video.py <experiment_id> --fast                 # 960x540, ~15 s draft
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import to_rgba
from matplotlib.lines import Line2D
from matplotlib.ticker import MaxNLocator
from mpl_toolkits.mplot3d.art3d import Line3DCollection

_HERE = Path(__file__).resolve().parent
for _p in (_HERE.parent, _HERE):              # repo root, then analysis/
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import specimens as S
from water import M_BAR_GPS, continuous_ws_index

OUT = _HERE / "figures"
W_PX, H_PX, DPI = 1920, 1080, 120
INK, MUTED, GRID = "#0b0b0b", "#898781", "#e1e0d9"
BLUE, ORANGE, AQUA, RED = "#2a78d6", "#eb6834", "#1baf7a", "#d03b3b"
BEAD, CMAP = "#b4b0a5", "viridis"
LW = 2.6                                     # bead line width (pt)
# the 3D axes keep their box aspect by shrinking to a square-ish box, and the
# zoomed print reaches past it: nothing drawn in 3D may be clipped to that box
BAND = dict(s=70, color=RED, alpha=0.2, linewidths=0, depthshade=False, clip_on=False)
AZIM = (-74.0, -62.0, -50.0)                 # camera: start, end of replay, end of outro
ELEV = (32.0, 38.0)                          # replay, end of outro
BAND_MAX_SHARE = 0.5                         # above this the bands would hide the colours
CHAMBER_TAU_S = 40.0                         # mixing chamber as a stirred tank
WS_LINE = "#0f7a55"
XYZ = ["pos_x", "pos_y", "pos_z"]
TRACES = [("torque", "pump torque (%), 30 s mean", BLUE),
          ("P1", "pressure P1 (bar)", ORANGE),
          ("ws", "W/S leaving the mixer (stirred-tank model, kg/kg) · dots: doses", AQUA)]


# ------------------------------------------------------------------ data
def load(eid: str):
    dim = S.load_dim()
    return frames(S.build_specimen(eid, [S.load_run(dim[dim.experiment_id == eid].iloc[0].to_dict())]))


def motion_updates(sp: S.Specimen) -> pd.DataFrame | None:
    """The controller's position updates of every run, in time order."""
    parts = [r.motion if r.motion is not None else S.head_motion(r.scan) for r in sp.runs]
    parts = [m for m in parts if m is not None and not m.empty]
    return pd.concat(parts).sort_values("t") if parts else None


def reckon(t: np.ndarray, moves: pd.DataFrame) -> np.ndarray:
    """Head position (x, y, z rows) at times between controller updates. The logs
    repeat a position until the next update, 15 mm further on at print speed, so
    dead-reckon along the logged velocity, tied to the update after as well (cubic
    Hermite) so a corner is not overshot. Across a pause the last position holds."""
    m = moves.dropna(subset=["t", *XYZ])
    xyz = m[XYZ].to_numpy(float)
    new = np.r_[True, np.any(np.diff(xyz, axis=0) != 0, axis=1)]
    T, P = m.t.to_numpy(float)[new], xyz[new]
    V = np.nan_to_num(m[["vel_x", "vel_y", "vel_z"]].to_numpy(float)[new]) / 60     # mm/min -> mm/s
    if len(T) < 2:
        return np.tile(P[0] if len(T) else np.nan, (len(t), 1))
    gaps = np.diff(T)
    k = np.clip(np.searchsorted(T, t, side="right") - 1, 0, len(T) - 2)
    dt = gaps[k][:, None]
    f = np.clip((t[:, None] - T[k][:, None]) / np.where(dt > 0, dt, 1.0), 0, 1)
    out = ((2 * f ** 3 - 3 * f ** 2 + 1) * P[k] + (f ** 3 - 2 * f ** 2 + f) * dt * V[k]
           + (3 * f ** 2 - 2 * f ** 3) * P[k + 1] + (f ** 3 - f ** 2) * dt * V[k + 1])
    pause = gaps[k] > 3 * np.median(gaps)
    out[pause] = P[k][pause]
    return out


def frames(sp: S.Specimen):
    """Everything the replay draws for one specimen: (specimen, path, signals,
    sampled profiles with their width and area where measured, usable scan
    sections, doses)."""
    prof = S.profile_widths(sp, S.DEFAULT_METHOD, S.ALL_DIRECTIONS, per_layer=150)
    p = sp.pump[sp.pump.t.notna()].sort_values(["run", "elapsed"])
    # corners where C turns shift a little from layer to layer, so the layer
    # search finds no previous pass for a few samples there: a short unlabelled
    # stretch (<= 3 s) between samples of one layer belongs to that layer
    parts = []
    for _, q in p.groupby("run", sort=False):
        lay, el = q.layer, q.elapsed
        t_lab = el.where(lay.notna())
        fill = (lay.isna() & (lay.ffill() == lay.bfill())
                & (t_lab.bfill() - t_lab.ffill() <= 3.0))
        parts.append(q.assign(layer=lay.mask(fill, lay.ffill())))
    p = pd.concat(parts)
    printing = ((p.feed.fillna(0) > 0) & p.layer.notna()).to_numpy()
    p = p.assign(width=S.width_track(prof, p.elapsed.to_numpy(), printing)[0])
    path = p.loc[printing, ["elapsed", "pos_x", "pos_y", "pos_z", "layer", "width"]]
    # the log repeats a position until the controller's next update (~10 Hz): each
    # update once, on the row that first carried it, so a point has its own time
    xyz = path[["pos_x", "pos_y", "pos_z"]].to_numpy()
    path = path[np.r_[True, np.any(np.diff(xyz, axis=0) != 0, axis=1)]]
    sig = (p.assign(sec=np.floor(p.elapsed))
           .groupby("sec")[["torque", "P1", "P2", "T1", "width", "feed"]].mean().reset_index())
    sig["elapsed"] = sig.sec + 0.5
    # torque as 30 s means
    secs = np.arange(sig.sec.min(), sig.sec.max() + 1)
    y = (sig.set_index("sec").torque.reindex(secs).interpolate(limit=30)
         .rolling(30, center=True, min_periods=10).mean())
    sig["torque_s"] = y.reindex(sig.sec).to_numpy()
    # composition leaving the mixer: both feeds through the chamber's residence-
    # time distribution, as kg water per kg solids (750 g/s per second of screw)
    ws = pd.concat([continuous_ws_index(g.sort_values("elapsed")[["elapsed", "waterFlow", "mixer"]]
                                        .astype(float), tau_s=CHAMBER_TAU_S, feed_delay_s=0.0)
                    for _, g in p.groupby("run")])
    sig["ws"] = np.interp(sig.elapsed, ws.elapsed, ws.ws_cont / (M_BAR_GPS / 1000),
                          left=np.nan, right=np.nan)
    # water and solids of one dose do not enter in step, so the modelled chamber
    # jumps a little with every dose: a trailing 30 s mean (~2 dosing cycles)
    sig["ws"] = sig.ws.rolling(30, min_periods=10).mean()
    d = sp.doses
    doses = (pd.DataFrame({"elapsed": d.start_time,
                           "ws": d.water_L / (d.mixer_s * M_BAR_GPS / 1000)})[d.mixer_s > 0.5]
             if not d.empty else pd.DataFrame({"elapsed": [], "ws": []}))
    sc = sp.scans
    cols = ["elapsed", "pos_x", "pos_y", "pos_z", "layer"]
    usable = pd.DataFrame({c: pd.Series(dtype=float) for c in cols})     # no scan on this print
    if not sc.empty:
        usable = sc[sc.layer.notna() & (sc.feed.fillna(0) > 0) & sc.pos_x.notna()
                    & S.section_mask(sc, S.ALL_DIRECTIONS, S.direction_locked(sp))]
        moves = motion_updates(sp)
        if moves is not None:                # a profile sits between position updates
            usable = usable.assign(**dict(zip(XYZ, reckon(usable.t.to_numpy(float), moves).T)))
        usable = usable[cols].sort_values("elapsed")
    measured = prof.sort_values("elapsed")
    # a priming line printed on the way in (layer 1, off the part's footprint) is
    # not part of the object: keep layer-1 points within 15 mm of where layer 2 runs
    ref = path.loc[path.layer == 2, ["pos_x", "pos_y"]].to_numpy()
    if len(ref):
        from scipy.spatial import cKDTree
        tree = cKDTree(ref)
        d_path = tree.query(path[["pos_x", "pos_y"]].to_numpy())[0]
        path = path[(path.layer != 1) | (d_path < 15)]
        if len(usable):
            d_scan = tree.query(usable[["pos_x", "pos_y"]].to_numpy())[0]
            usable = usable[(usable.layer != 1) | (d_scan < 15)]
    return sp, path.reset_index(drop=True), sig, measured, usable, doses.dropna()


def joined(g: pd.DataFrame) -> np.ndarray:
    """Which consecutive path points belong to one bead: False at travel gaps.
    A pause in place (the head stops in X/Y while C turns at a corner) leaves a
    time gap but no jump: those stay joined."""
    a = g[["pos_x", "pos_y", "pos_z"]].to_numpy()
    dt = np.diff(g.elapsed.to_numpy())
    d = np.linalg.norm(np.diff(a, axis=0), axis=1)
    return (d < 60) & ((dt < 1.0) | (d < 10))


def segments(g: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Consecutive path points of one layer as line segments, broken at travel
    gaps, with the width at each segment's start."""
    a = g[["pos_x", "pos_y", "pos_z"]].to_numpy()
    if len(a) < 2:
        return np.empty((0, 2, 3)), np.empty(0)
    ok = joined(g)
    return np.stack([a[:-1], a[1:]], axis=1)[ok], g.width.to_numpy()[:-1][ok]


def usable_share(sp: S.Specimen, usable: pd.DataFrame) -> float:
    """Share of the profiles taken while printing that lie on a usable section."""
    sc = sp.scans
    if sc.empty:
        return 0.0
    moving = int((sc.layer.notna() & (sc.feed.fillna(0) > 0) & sc.pos_x.notna()).sum())
    return len(usable) / moving if moving else 0.0


def describe(eid: str) -> tuple[str, str]:
    """(title with the print date, date) from the experiment's catalogue row."""
    row = S.load_dim().set_index("experiment_id").loc[eid]
    when = f"{pd.Timestamp(row.experiment_date):%d %B %Y}".lstrip("0")
    return f"{str(row.board).replace('_', ' ').capitalize()} · {when}", when


# ------------------------------------------------------------------ figure
class Replay:
    def __init__(self, eid: str, seconds: float, scale: float = 1.0, title: str | None = None,
                 zoom: float = 1.28):
        self.scale = scale                       # < 1: smaller frames, same layout
        self.zoom = zoom                         # deep prints (large Y) need less
        self.sp, self.path, self.sig, self.w, self.usable, self.doses = load(eid)
        self.w = self.w[self.w.width.notna()][["elapsed", "width"]]
        self.t_end = float(self.path.elapsed.max())
        self.speed = self.t_end / seconds
        self.n_layers = int(self.path.layer.max())
        lo, hi = np.nanpercentile(self.path.width, [2, 98])
        self.norm = plt.Normalize(np.floor(lo), np.ceil(hi))
        self.layers = {int(k): g for k, g in self.path.groupby("layer")}
        self.bands = {int(k): g for k, g in self.usable.groupby("layer")}
        self.drawn: set[int] = set()
        # where C turns with the path the scanner follows it and nearly every
        # profile is usable: red bands everywhere would hide the width colours
        self.band_share = usable_share(self.sp, self.usable)
        if self.band_share > BAND_MAX_SHARE:
            self.bands = {}
        name, when = describe(eid)
        self._build(title or name, when)

    def colours(self, w: np.ndarray) -> np.ndarray:
        c = plt.get_cmap(CMAP)(self.norm(w))
        c[~np.isfinite(w)] = to_rgba(BEAD)
        return c

    def _build(self, title: str, when: str):
        fig = self.fig = plt.figure(figsize=(W_PX / DPI, H_PX / DPI), dpi=DPI * self.scale,
                                    facecolor="white")
        lh = self.sp.layer_h or 10
        feed = np.nanmedian(self.sp.pump.feed[self.sp.pump.feed > 0]) / 1000
        fig.text(0.035, 0.935, title, fontsize=24, weight="bold", color=INK)
        fig.text(0.035, 0.895,
                 f"3D concrete printing, {self.n_layers} layers of {round(lh, 1):g} mm at "
                 f"{feed:g} m/min. Replay at {self.speed:.0f}x speed.",
                 fontsize=13, color="#333")
        first = (Line2D([], [], ls="", marker="o", ms=12, mfc=RED, mec="none", alpha=0.35,
                        label="usable scan section: width measured here")
                 if self.bands else
                 Line2D([], [], ls="", marker="o", ms=12, mfc="none", mec="none",
                        label=f"C follows the path: {self.band_share:.0%} usable"))
        fig.legend(handles=[
            first,
            Line2D([], [], color=plt.get_cmap(CMAP)(0.6), lw=LW,
                   label="elsewhere: interpolated in time" if self.bands
                   else "width, measured + interpolated"),
            Line2D([], [], color=BEAD, lw=LW, label="before first / after last profile")],
            loc="upper left", bbox_to_anchor=(0.03, 0.875), ncol=3, frameon=False,
            fontsize=11, handlelength=1.6, columnspacing=1.4)

        ax = self.ax = fig.add_axes([-0.03, 0.16, 0.66, 0.72], projection="3d",
                                    computed_zorder=False)
        x, y, z = (self.path[c] for c in ("pos_x", "pos_y", "pos_z"))
        pad = 4 * lh                              # room around the print
        ax.set_xlim(x.min() - pad, x.max() + pad); ax.set_ylim(y.min() - pad, y.max() + pad)
        ax.set_zlim(0, z.max() + lh)
        ax.set_box_aspect((x.max() - x.min() + 2 * pad, y.max() - y.min() + 2 * pad,
                           z.max() + lh), zoom=self.zoom)
        for a in (ax.xaxis, ax.yaxis, ax.zaxis):
            a.set_pane_color((1, 1, 1, 0))
            a._axinfo["grid"].update(color=GRID, linewidth=0.6)
            a.set_tick_params(labelsize=8, colors=MUTED)
        ax.set_xlabel("X (mm)", fontsize=9, color=MUTED, labelpad=8)
        ax.set_ylabel("Y (mm)", fontsize=9, color=MUTED, labelpad=4)
        ax.set_zlabel("Z (mm)", fontsize=9, color=MUTED, labelpad=2)
        ax.yaxis.set_major_locator(MaxNLocator(3)); ax.zaxis.set_major_locator(MaxNLocator(3))
        # the current layer, above every finished one (painter's order = layer order)
        self.cur = Line3DCollection([], linewidths=LW, capstyle="round", zorder=2, clip_on=False)
        ax.add_collection3d(self.cur, autolim=False)
        self.cur_band = None
        self.nozzle, = ax.plot([], [], [], "o", ms=7, mfc=ORANGE, mec=INK, mew=0.8, zorder=3,
                               clip_on=False)   # clipped near the left edge otherwise

        cax = fig.add_axes([0.10, 0.135, 0.36, 0.014])
        cb = fig.colorbar(plt.cm.ScalarMappable(self.norm, CMAP), cax=cax,
                          orientation="horizontal")
        cb.set_label("bead width from the laser scan, measured + interpolated (mm)",
                     fontsize=10, color="#333")
        cb.ax.tick_params(labelsize=9, colors="#333")
        cb.outline.set_visible(False)

        self.status = fig.text(0.035, 0.045, "", fontsize=15, color=INK, family="monospace")
        bar = fig.add_axes([0.035, 0.03, 0.555, 0.006])
        bar.set_xlim(0, 1); bar.axis("off")
        bar.axhspan(0, 1, color=GRID)
        self.bar = bar.axhspan(0, 1, xmax=0, color=INK)

        # right column: signals on the print's own clock (minutes)
        rows = len(TRACES) + 1
        top, bottom, gap = 0.86, 0.10, 0.035
        h = (top - bottom - gap * (rows - 1)) / rows
        tmax = self.t_end / 60
        self.lines, self.cursors = [], []
        for i in range(rows):
            a = fig.add_axes([0.665, top - (i + 1) * h - i * gap, 0.30, h])
            a.set_xlim(0, tmax)
            for s in ("top", "right"):
                a.spines[s].set_visible(False)
            for s in ("left", "bottom"):
                a.spines[s].set_color(GRID)
            a.tick_params(labelsize=9, colors=MUTED)
            a.grid(axis="y", color=GRID, lw=0.6)
            if i < rows - 1:
                col, label, color = TRACES[i]
                if col == "torque":
                    lo, hi = np.nanpercentile(self.sig.torque_s, [0.5, 99.5])
                    a.set_ylim(max(0.0, lo - 2), hi + 2)
                    ln, = a.plot([], [], color=color, lw=1.4)
                    self.lines.append((ln, "torque_s"))
                elif col == "ws":
                    lo, hi = np.nanpercentile(self.sig.ws, [1, 99])    # the line; stray doses clip
                    pad = max(0.01, 0.6 * (hi - lo))
                    a.set_ylim(lo - pad, hi + pad)
                    self.dose_dots = a.scatter([], [], s=7, color=color, alpha=0.35,
                                               linewidths=0)
                    ln, = a.plot([], [], color=WS_LINE, lw=1.6)
                    self.lines.append((ln, "ws"))
                else:
                    a.set_ylim(0, np.nanpercentile(self.sig[col], 99.8) * 1.12)
                    ln, = a.plot([], [], color=color, lw=1.1)
                    self.lines.append((ln, col))
                a.tick_params(labelbottom=False)
            else:
                label = "bead width (mm): measured dots, interpolated line"
                a.set_ylim(self.norm.vmin - 2, self.norm.vmax + 2)
                a.set_xlabel("print time (min)", fontsize=10, color=MUTED)
                ln, = a.plot([], [], color=MUTED, lw=0.8)
                self.lines.append((ln, "width"))
                self.wdots = a.scatter([], [], c=[], s=5, cmap=CMAP, norm=self.norm, zorder=3)
            a.set_title(label, fontsize=11, color="#333", loc="left", pad=3)
            self.cursors.append(a.axvline(0, color=INK, lw=0.8, alpha=0.6))

    # -------------------------------------------------------------- frame
    def draw(self, t: float, azim: float, elev: float = ELEV[0]):
        ax, path = self.ax, self.path
        k = int(np.searchsorted(path.elapsed.to_numpy(), t, side="right")) - 1
        k = max(k, 0)
        layer = int(path.layer.iloc[k])
        for L in range(1, layer):                     # finished layers: drawn once
            if L in self.drawn:
                continue
            if L in self.layers:
                seg, w = segments(self.layers[L])
                ax.add_collection3d(Line3DCollection(seg, colors=self.colours(w), linewidths=LW,
                                                     capstyle="round", zorder=1 + L / 100,
                                                     clip_on=False),
                                    autolim=False)
            if L in self.bands:
                b = self.bands[L]
                ax.scatter(b.pos_x, b.pos_y, b.pos_z, zorder=1.005 + L / 100, **BAND)
            self.drawn.add(L)
        seg, w = segments(self.layers[layer][self.layers[layer].elapsed <= t])
        self.cur.set_segments(list(seg))
        self.cur.set_color(self.colours(w))
        if self.cur_band is not None:
            self.cur_band.remove()
            self.cur_band = None
        b = self.bands.get(layer)
        if b is not None and (b.elapsed <= t).any():
            b = b[b.elapsed <= t]
            self.cur_band = ax.scatter(b.pos_x, b.pos_y, b.pos_z, zorder=2.005, **BAND)
        g = path.iloc[k:k + 2]                        # on its way to the next update
        a = g[XYZ].to_numpy(float)
        f = (np.clip((t - g.elapsed.iloc[0]) / np.diff(g.elapsed)[0], 0, 1)
             if len(g) == 2 and joined(g)[0] and np.diff(g.elapsed)[0] > 0 else 0.0)
        hx, hy, hz = a[0] + f * (a[-1] - a[0])
        self.nozzle.set_data_3d([hx], [hy], [hz])
        ax.view_init(elev=elev, azim=azim)

        s = self.sig[self.sig.elapsed <= t]
        for ln, col in self.lines:
            ln.set_data(s.elapsed / 60, s[col])
        d = self.doses[self.doses.elapsed <= t]
        self.dose_dots.set_offsets(np.c_[d.elapsed / 60, d.ws])
        w = self.w[self.w.elapsed <= t]
        self.wdots.set_offsets(np.c_[w.elapsed / 60, w.width])
        self.wdots.set_array(w.width.to_numpy())
        for c in self.cursors:
            c.set_xdata([t / 60])
        m, sec = divmod(int(t), 60)
        self.status.set_text(f"{m:02d}:{sec:02d}   layer {layer:>2} of {self.n_layers}"
                             f"   nozzle Z {hz:3.0f} mm")
        self.bar.set_x(0); self.bar.set_width(t / self.t_end)

    def frame(self) -> np.ndarray:
        self.fig.canvas.draw()
        return np.asarray(self.fig.canvas.buffer_rgba())[..., :3]


# ------------------------------------------------------------------ main
def timeline(r: Replay, fps: int, seconds: float, hold: float = 1.0, outro: float = 5.0):
    """(print time, azimuth, elevation) per frame: hold the start, replay, slow turn
    at the end."""
    a0, a1, a2 = AZIM
    e0, e1 = ELEV
    n = int(seconds * fps)
    ts = [(0.0, a0, e0)] * int(hold * fps)
    ts += [(r.t_end * i / (n - 1), a0 + (a1 - a0) * i / (n - 1), e0) for i in range(n)]
    m = int(outro * fps)
    ease = [0.5 - 0.5 * np.cos(np.pi * i / (m - 1)) for i in range(m)]
    ts += [(r.t_end, a1 + (a2 - a1) * f, e0 + (e1 - e0) * f) for f in ease]
    return ts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("experiment_id")
    ap.add_argument("--seconds", type=float, default=48, help="length of the replay part")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--title", help="title instead of the one from the print's name")
    ap.add_argument("--zoom", type=float, default=1.28, help="3D view zoom")
    ap.add_argument("--stills", type=float, nargs="*",
                    help="write PNG frames at these fractions of the print instead")
    ap.add_argument("--fast", action="store_true",
                    help="draft for iterating: 960x540, 12 s replay at 20 fps, short outro")
    args = ap.parse_args()
    scale, hold, outro = (0.5, 0.5, 2.0) if args.fast else (1.0, 1.0, 5.0)
    if args.fast:
        args.seconds, args.fps = 12, 20
    r = Replay(args.experiment_id, args.seconds, scale, args.title, args.zoom)
    stem = args.experiment_id.lower() + "_replay" + ("_fast" if args.fast else "")
    OUT.mkdir(parents=True, exist_ok=True)
    if args.stills:
        for f in args.stills:
            r.draw(f * r.t_end, azim=AZIM[0] + (AZIM[1] - AZIM[0]) * f)
            p = (args.out or OUT) / f"{stem}_{int(f * 100):03d}.png"
            r.fig.savefig(p, dpi=r.fig.dpi)
            print(p)
        r.draw(r.t_end, azim=AZIM[2], elev=ELEV[1])
        r.fig.savefig((args.out or OUT) / f"{stem}_end.png", dpi=r.fig.dpi)
        return
    import imageio_ffmpeg
    out = args.out or OUT / f"{stem}.mp4"
    ts = timeline(r, args.fps, args.seconds, hold, outro)
    r.draw(0.0, AZIM[0])
    h_px, w_px = r.frame().shape[:2]
    w = imageio_ffmpeg.write_frames(str(out), (w_px, h_px), fps=args.fps, codec="libx264",
                                    quality=8, macro_block_size=2,
                                    output_params=["-movflags", "+faststart"])
    w.send(None)
    t0 = time.time()
    for i, (t, az, el) in enumerate(ts):
        r.draw(t, az, el)
        w.send(np.ascontiguousarray(r.frame()))
        if i % 150 == 0:
            print(f"frame {i}/{len(ts)}  {time.time() - t0:.0f} s", flush=True)
    w.close()
    print(out, f"{len(ts) / args.fps:.1f} s video, {time.time() - t0:.0f} s to render")


if __name__ == "__main__":
    main()
