"""
Live monitor for the 3D concrete printer.

    python -m printmon view                  live, from inputs_fast.py + scan_fast.py
    python -m printmon view --sim            simulation: replay a recorded run
    python -m printmon view --sim --run lines --speed 2

Acquisition and rendering are decoupled. Sources write every sample into ring
buffers at full rate on their own thread; the GUI reads only what is on screen at
the display rate (capped at 30 FPS by default). Nothing here can slow acquisition.

The viewer is a passive listener: it never sends commands. Node-RED still owns
start/stop and all process control.

Tabs
    Monitor         scanner profile (with the C axis), bead width, and one plot + gauge
                    per selected sensor, raw or engineering units
    Bead & layers   GPU 3D bead over the time window, and bead width over the build
                    plate for the current and two previous layers, coloured against a
                    target width

Pause freezes the view (and the replay, in simulation). While paused, drag or wheel
on any time plot -- or use the history bar -- to look back. Everything on screen then
shows the state at the right edge of the view. How far back depends on buffer sizes:
~10 min of sensor traces, ~2 min of full profiles at 143 Hz.
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from . import config
from .processing import PositionTracker, ProfileStore, recent_layers, resample_profile
from .ring import RingBuffer
from .sources import LiveSource, ReplaySource, Sink, discover_runs
from .units import DEFAULT_SELECTION, SIGNALS, feed_from_vel

PROFILE_GRID = np.linspace(-32.0, 32.0, 257)  # mm across the bead; scanner sees ~-25..29
SPEEDS = ("0.25x", "0.5x", "1x", "2x", "5x", "10x")
PALETTE = ("#4cc9f0", "#f72585", "#ffd166", "#06d6a0", "#b388ff", "#ff8c42", "#90e0ef",
           "#ef476f", "#c9f27b", "#f4a261", "#a0c4ff", "#e5989b", "#9bf6ff")
SIGNAL_COLOR = {k: PALETTE[i % len(PALETTE)] for i, k in enumerate(SIGNALS)}
WIDTH_COLOR = "#ffffff"
POINT_COLS = ("X", "Y", "Z", "C", "width")
NS = 1_000_000_000

# Ring capacities: ~10 min of history at the measured rates (gantry I/O ~650 Hz).
_CAPACITY = {"pump": 1 << 17, "gantry": 1 << 19, "nc": 1 << 15}


def _fmt(v):
    if v is None or not np.isfinite(v):
        return "--"
    return "{:.4g}".format(v)


class ViewerSink(Sink):
    """Receives records on the source thread; everything the GUI reads lives here."""

    def __init__(self):
        self.signals = {k: RingBuffer(_CAPACITY[s.stream], ("v",)) for k, s in SIGNALS.items()}
        self.width = RingBuffer(1 << 17, ("width",))
        self.points = RingBuffer(1 << 18, POINT_COLS)  # where each width was measured
        self.tracker = PositionTracker()
        self.profiles = ProfileStore(1 << 14, PROFILE_GRID)  # ~115 s at 143 Hz
        self.counts = {"pump": 0, "gantry": 0, "nc": 0, "scan": 0}
        self.dropped = 0
        self.status = {}
        self.t_first = None
        self.t_latest = None
        self.emulated = False  # frames from `printmon emulate`, not the machine

    def _touch(self, t):
        if self.t_first is None:
            self.t_first = t
        if self.t_latest is None or t > self.t_latest:
            self.t_latest = t

    def _put(self, key, t, v):
        if v is not None:
            self.signals[key].append(t, [float(v)])
            return True
        return False

    def on_process(self, rec):
        t, src = rec["t"], rec["src"]
        if rec["extra"].get("emulated"):
            self.emulated = True
        if src in (None, "pump"):
            got = [self._put(k, t, rec[k]) for k in ("torque", "water_flow", "water_temp",
                                                     "mixer")]
            self.counts["pump"] += any(got)
        if src in (None, "gantry"):
            got = [self._put(k, t, rec[k]) for k in ("P0", "P1", "P2", "P3", "T0", "T1")]
            self.counts["gantry"] += any(got)
        if src in (None, "nc"):
            self._put("set_speed", t, rec["set_speed"])
            pos, vel = rec["pos"], rec["vel"]
            if pos is not None and self.tracker.update(t, pos, vel):
                self.counts["nc"] += 1
                if vel:
                    self._put("feed", t, feed_from_vel(vel))
                if len(pos) > 3:
                    self._put("C", t, pos[3])
        self._touch(t)

    def on_scan(self, rec):
        t, w = rec["t"], rec["width"]
        if rec["extra"].get("emulated"):
            self.emulated = True
        self.width.append(t, [np.nan if w is None else w])
        xyz = self.tracker.at(t) or rec["pos"]
        heights = resample_profile(rec["x"], rec["z"], PROFILE_GRID)
        self.profiles.push(t, rec["x"], rec["z"], heights, xyz[:2] if xyz else None)
        if xyz is not None and w is not None:
            # XY is dead-reckoned (that is where it helps); Z is the raw NC value, because
            # extrapolating Z through a layer-change lift invents layers that do not exist.
            pos = self.tracker.pos
            z = pos[2] if pos is not None else xyz[2]
            c = pos[3] if pos is not None and len(pos) > 3 else np.nan
            self.points.append(t, [xyz[0], xyz[1], z, c, w])
        if rec["dropped"]:
            self.dropped += rec["dropped"]
        self.counts["scan"] += 1
        self._touch(t)

    def on_status(self, stream, text):
        self.status[stream] = text


# --- plot helpers -------------------------------------------------------------------

class YScaler:
    """
    Y-range control that does not relayout the axis every frame.

    pyqtgraph autorange recomputes ticks on every data update, and axis painting
    was measured at ~60% of frame time. Here the range expands immediately when
    data escapes it, and contracts at most every `settle_s` seconds.
    """

    def __init__(self, plot, settle_s=0.5, pad=0.08):
        self.plot = plot
        self.settle_s = settle_s
        self.pad = pad
        self.lo = self.hi = None
        self._last = 0.0
        plot.enableAutoRange(axis="y", enable=False)

    def reset(self):
        self.lo = self.hi = None

    def feed(self, rng, now):
        if rng is None:
            return
        lo, hi = rng
        if hi - lo < 1e-9:
            lo, hi = lo - 0.5, hi + 0.5
        escaped = self.lo is None or lo < self.lo or hi > self.hi
        if escaped or now - self._last >= self.settle_s:
            span = hi - lo
            new = (lo - span * self.pad, hi + span * self.pad)
            shift = max(abs(new[0] - self.lo), abs(new[1] - self.hi)) if self.lo is not None else 0
            if escaped or self.lo is None or shift > span * 0.05:
                self.lo, self.hi = new
                self.plot.setYRange(*new, padding=0)
            self._last = now


class GaugeItem(pg.GraphicsWidget):
    """Arc gauge beside each plot: current value, unit, and the signal's sample rate."""

    WIDTH = 150

    def __init__(self, color):
        super().__init__()
        self.setMinimumWidth(self.WIDTH)
        self.setMaximumWidth(self.WIDTH)
        self.color = QtGui.QColor(color)
        self._state = None

    def set_state(self, label, value, lo, hi, unit, hz, boolean=False):
        state = (label, None if value is None else round(float(value), 6), lo, hi, unit,
                 None if hz is None else round(hz), boolean)
        if state != self._state:
            self._state = state
            self.update()

    def paint(self, p, *_):
        if self._state is None:
            return
        label, value, lo, hi, unit, hz, boolean = self._state
        r = self.rect()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        small = QtGui.QFont()
        small.setPointSizeF(7.5)
        big = QtGui.QFont()
        big.setPointSizeF(10.5)
        big.setBold(True)
        grey = QtGui.QColor(150, 150, 150)
        left = QtCore.Qt.AlignmentFlag.AlignLeft
        center = QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignVCenter

        p.setFont(small)
        p.setPen(self.color)
        p.drawText(QtCore.QRectF(r.left() + 6, r.top() + 2, r.width() - 8, 14), left, label)
        p.setPen(grey)
        p.drawText(QtCore.QRectF(r.left() + 6, r.bottom() - 15, r.width() - 8, 14), left,
                   "{} Hz".format(hz) if hz is not None else "")

        dia = min(r.width() - 24, r.height() - 36, 72)
        cx, cy = r.center().x(), r.top() + 18 + dia / 2
        if boolean:
            on = bool(value)
            rad = max(min(dia, 40) / 2, 6)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor("#06d6a0") if on else QtGui.QColor(70, 70, 70))
            p.drawEllipse(QtCore.QPointF(cx, cy), rad, rad)
            p.setFont(big)
            p.setPen(QtGui.QColor(230, 230, 230))
            p.drawText(QtCore.QRectF(r.left(), cy + rad, r.width(), 18), center,
                       "--" if value is None else ("ON" if on else "OFF"))
            return
        if dia >= 24:
            arc = QtCore.QRectF(cx - dia / 2, cy - dia / 2, dia, dia)
            pen = QtGui.QPen(QtGui.QColor(60, 60, 60), max(4.0, dia * 0.1))
            pen.setCapStyle(QtCore.Qt.PenCapStyle.FlatCap)
            p.setPen(pen)
            p.drawArc(arc, 225 * 16, -270 * 16)
            if value is not None and hi is not None and lo is not None and hi > lo:
                frac = float(np.clip((value - lo) / (hi - lo), 0.0, 1.0))
                pen.setColor(self.color)
                p.setPen(pen)
                p.drawArc(arc, 225 * 16, int(-270 * 16 * frac))
        p.setFont(big)
        p.setPen(QtGui.QColor(235, 235, 235))
        p.drawText(QtCore.QRectF(r.left(), cy - 11, r.width(), 18), center, _fmt(value))
        p.setFont(small)
        p.setPen(grey)
        p.drawText(QtCore.QRectF(r.left(), cy + 7, r.width(), 14), center, unit)


class SeriesRow:
    """One sensor: its plot, its gauge, and where its data lives."""

    def __init__(self, plot, gauge, key, color):
        self.plot = plot
        self.gauge = gauge
        self.key = key  # a SIGNALS key, or "width"
        self.curve = plot.plot(pen=pg.mkPen(color, width=1), connect="finite")
        self.curve.setDownsampling(auto=True, method="peak")
        self.curve.setClipToView(True)
        self.scaler = YScaler(plot)
        self.hz = None

    def ring(self, sink):
        return sink.width if self.key == "width" else sink.signals[self.key]

    def update(self, sink, t0, t_min, t_max, follow, engineering, now):
        ring = self.ring(sink)
        t, v = ring.window(t_min) if follow else ring.between(t_min, t_max)
        sig = SIGNALS.get(self.key)
        vals = v[:, 0] if len(v) else np.empty(0, np.float32)
        if sig is not None and len(vals):
            vals = sig.convert(vals.astype(np.float64), engineering)
        self.curve.setData((t - t0) / 1e9, vals)
        finite = vals[np.isfinite(vals)] if len(vals) else vals
        if len(finite):
            self.scaler.feed((float(finite.min()), float(finite.max())), now)
        current = float(vals[-1]) if len(vals) and np.isfinite(vals[-1]) else None
        if sig is not None:
            label, unit, boolean = sig.label, sig.unit(engineering), sig.boolean
        else:
            label, unit, boolean = "width", "mm", False
        self.gauge.set_state(label, current, self.scaler.lo, self.scaler.hi, unit, self.hz,
                             boolean)


def width_colormap():
    """Blue = narrow, green = on target, red = wide."""
    return pg.ColorMap([0.0, 0.5, 1.0], [(50, 110, 255), (40, 220, 110), (255, 70, 70)])


class LayerMap:
    """X/Y scatter of bead width for one layer, coloured against the target."""

    def __init__(self, plot, role):
        self.plot = plot
        self.role = role
        plot.setAspectLocked(True)
        plot.setLabel("bottom", "X (mm)")
        plot.setLabel("left", "Y (mm)")
        plot.showGrid(x=True, y=True, alpha=0.15)
        self.scatter = pg.ScatterPlotItem(size=5, pen=None)
        plot.addItem(self.scatter)
        self.marker = pg.ScatterPlotItem(size=13, pen=pg.mkPen("w", width=1.5),
                                         brush=pg.mkBrush(255, 0, 84))
        plot.addItem(self.marker)
        self.follow = True
        self._key = None
        plot.getViewBox().sigRangeChangedManually.connect(self._manual)

    def _manual(self, *_):
        self.follow = False

    def show(self, z, x, y, w, brushes_for, bbox):
        key = (z, len(x), brushes_for.version)
        if key == self._key:
            return
        self._key = key
        if z is None:
            self.plot.setTitle("{}: no data".format(self.role), size="9pt")
            self.scatter.setData([], [])
            return
        self.plot.setTitle("{}  -  layer Z = {:g} mm  ({} pts)".format(self.role, z, len(x)),
                           size="9pt")
        self.scatter.setData(x, y, brush=brushes_for(w))
        if self.follow and bbox is not None:
            m = 40.0
            self.plot.setRange(xRange=(bbox[0] - m, bbox[1] + m),
                               yRange=(bbox[2] - m, bbox[3] + m), padding=0)


class BrushLUT:
    """64 pre-built brushes: colouring 10k points is a list index, not 10k mkBrush calls."""

    def __init__(self):
        self.cmap = width_colormap()
        self.version = 0
        self.set_range(35.0, 5.0)

    def set_range(self, target, tol):
        self.lo, self.hi = target - tol, target + tol
        cols = self.cmap.map(np.linspace(0, 1, 64), mode="byte")
        self.brushes = [pg.mkBrush(*c) for c in cols]
        self.version += 1

    def __call__(self, w):
        idx = np.clip(((np.asarray(w) - self.lo) / (self.hi - self.lo) * 63).round(), 0, 63)
        b = self.brushes
        return [b[int(i)] for i in idx]


# --- window -------------------------------------------------------------------------

class MonitorWindow(QtWidgets.QMainWindow):
    def __init__(self, opts):
        super().__init__()
        self.opts = opts
        self.sink = None
        self.source = None
        self.runs = discover_runs(opts.data)
        # Remembers sensor selection, units and target between sessions. PRINTMON_SETTINGS
        # points it at an ini file instead (tests use a temporary one).
        ini = os.environ.get("PRINTMON_SETTINGS")
        self.settings = (QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat) if ini
                         else QtCore.QSettings("BAM", "printmon"))
        self.paused = False
        self._frames = 0
        self._fps = 0.0
        self._last_counts = None
        self._last_totals = {}
        self._last_rate_t = time.perf_counter()
        self.rates = {}
        self.rows = []
        self._rows_sink = None
        self._stats_t = 0.0
        self._cam_set = False
        self._t_ref = None
        self._scroll_guard = False
        self._view_key = None
        self.brushes = BrushLUT()

        self.setWindowTitle("printmon")
        self.resize(1600, 980)
        self._build_toolbars()
        self._build_banner()
        self._build_tabs()
        self.status_label = QtWidgets.QLabel()
        self.statusBar().addWidget(self.status_label, 1)

        self.fast_timer = QtCore.QTimer(self)
        # Windows' default coarse timer rounds a 16 ms interval up to ~32 ms.
        self.fast_timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self.fast_timer.timeout.connect(self._refresh_fast)
        self.fast_timer.start(int(1000 / max(opts.fps, 1)))
        self.slow_timer = QtCore.QTimer(self)
        self.slow_timer.timeout.connect(self._refresh_slow)
        self.slow_timer.start(150)
        self.rate_timer = QtCore.QTimer(self)
        self.rate_timer.timeout.connect(self._refresh_rates)
        self.rate_timer.start(1000)

        self.start()

    # --- layout -----------------------------------------------------------------

    def _build_toolbars(self):
        tb = self.addToolBar("Source")
        tb.setMovable(False)
        tb.addWidget(QtWidgets.QLabel(" Source "))
        self.source_box = QtWidgets.QComboBox()
        self.source_box.addItems(["Live", "Simulation"])
        self.source_box.setCurrentIndex(1 if self.opts.sim else 0)
        self.source_box.currentIndexChanged.connect(self._on_source_changed)
        tb.addWidget(self.source_box)

        tb.addWidget(QtWidgets.QLabel("  Run "))
        self.run_box = QtWidgets.QComboBox()
        self.run_box.setMinimumWidth(330)
        for r in self.runs:
            self.run_box.addItem("{}  [{}]".format(r.name, "+".join(r.streams)))
        if self.opts.run:
            for i, r in enumerate(self.runs):
                if self.opts.run in r.name:
                    self.run_box.setCurrentIndex(i)
                    break
        self.run_box.currentIndexChanged.connect(lambda _: self._restart_if_sim())
        tb.addWidget(self.run_box)

        tb.addWidget(QtWidgets.QLabel("  Speed "))
        self.speed_box = QtWidgets.QComboBox()
        self.speed_box.addItems(SPEEDS)
        want = "{:g}x".format(self.opts.speed)
        self.speed_box.setCurrentIndex(SPEEDS.index(want) if want in SPEEDS else 2)
        self.speed_box.currentIndexChanged.connect(self._on_speed_changed)
        tb.addWidget(self.speed_box)

        self.loop_chk = QtWidgets.QCheckBox("Loop")
        self.loop_chk.setChecked(not self.opts.no_loop)
        tb.addWidget(self.loop_chk)

        tb.addSeparator()
        self.pause_btn = QtWidgets.QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setToolTip("Freeze the view to look back through the buffered history.\n"
                                  "In simulation this also pauses the replay.")
        self.pause_btn.toggled.connect(self.set_paused)
        tb.addWidget(self.pause_btn)
        self.start_btn = QtWidgets.QPushButton("Stop")
        self.start_btn.clicked.connect(self._on_start_stop)
        tb.addWidget(self.start_btn)

        tb.addSeparator()
        tb.addWidget(QtWidgets.QLabel(" Window "))
        self.window_spin = QtWidgets.QSpinBox()
        self.window_spin.setRange(2, 600)
        self.window_spin.setValue(self.opts.window)
        self.window_spin.setSuffix(" s")
        tb.addWidget(self.window_spin)

        self.dr_chk = QtWidgets.QCheckBox("Dead-reckon position")
        self.dr_chk.setToolTip(
            "Extrapolate the 10 Hz NC position along its velocity between samples.\n"
            "Validated on recorded runs: median error 1.1-1.6 mm vs 12-17.5 mm without.")
        self.dr_chk.setChecked(True)
        self.dr_chk.toggled.connect(self._on_dead_reckon)
        tb.addWidget(self.dr_chk)

        self.addToolBarBreak()
        tb2 = self.addToolBar("Display")
        tb2.setMovable(False)
        self.sensor_btn = QtWidgets.QToolButton()
        self.sensor_btn.setText(" Sensors ")
        self.sensor_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(self.sensor_btn)
        saved = self._setting("sensors", ",".join(DEFAULT_SELECTION)).split(",")
        self.sensor_actions = {}
        for key, sig in SIGNALS.items():
            act = menu.addAction(sig.label)
            act.setCheckable(True)
            act.setChecked(key in saved)
            act.toggled.connect(self._on_sensors_changed)
            self.sensor_actions[key] = act
        self.sensor_btn.setMenu(menu)
        tb2.addWidget(self.sensor_btn)

        self.units_chk = QtWidgets.QCheckBox("Engineering units")
        self.units_chk.setToolTip("Convert raw controller counts using the calibration notebook "
                                  "calibration (process_pump). Storage always keeps raw values.")
        self.units_chk.setChecked(self._setting("engineering", "true") == "true")
        self.units_chk.toggled.connect(self._on_units_changed)
        tb2.addWidget(self.units_chk)

        tb2.addSeparator()
        tb2.addWidget(QtWidgets.QLabel(" Target width "))
        self.target_spin = QtWidgets.QDoubleSpinBox()
        self.target_spin.setRange(5.0, 150.0)
        self.target_spin.setDecimals(1)
        self.target_spin.setSuffix(" mm")
        self.target_spin.setValue(float(self._setting("target", self.opts.target_width)))
        self.target_spin.valueChanged.connect(self._on_target_changed)
        tb2.addWidget(self.target_spin)
        tb2.addWidget(QtWidgets.QLabel(" ± "))
        self.tol_spin = QtWidgets.QDoubleSpinBox()
        self.tol_spin.setRange(0.5, 50.0)
        self.tol_spin.setDecimals(1)
        self.tol_spin.setSuffix(" mm")
        self.tol_spin.setValue(float(self._setting("tolerance", self.opts.width_tol)))
        self.tol_spin.valueChanged.connect(self._on_target_changed)
        tb2.addWidget(self.tol_spin)
        self._sync_controls()

    def _setting(self, key, default):
        try:
            v = self.settings.value(key, default)
            return str(v) if v is not None else str(default)
        except Exception:  # noqa: BLE001 -- settings are a convenience, never fatal
            return str(default)

    def _store(self, key, value):
        try:
            self.settings.setValue(key, value)
        except Exception:  # noqa: BLE001
            pass

    def _build_banner(self):
        self.banner = QtWidgets.QLabel()
        self.banner.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.banner.setStyleSheet(
            "background:#ff8c00; color:black; font-weight:bold; font-size:14px; padding:4px;")
        self.banner.hide()

    def _build_tabs(self):
        central = QtWidgets.QWidget()
        vbox = QtWidgets.QVBoxLayout(central)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)
        vbox.addWidget(self.banner)
        self.tabs = QtWidgets.QTabWidget()
        vbox.addWidget(self.tabs)
        self.setCentralWidget(central)
        self.tabs.addTab(self._build_monitor_tab(), "Monitor")
        self.tabs.addTab(self._build_bead_tab(), "Bead && layers")  # && = literal &

    def _build_monitor_tab(self):
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)

        left = pg.GraphicsLayoutWidget()
        self.profile_plot = left.addPlot(row=0, col=0, title="Laser profile")
        self.profile_plot.setLabel("bottom", "x (mm)")
        self.profile_plot.setLabel("left", "z, height (mm)")
        self.profile_plot.setXRange(-35, 35, padding=0)
        for xv in (-20, -10, 0, 10, 20):
            self.profile_plot.addLine(x=xv, pen=pg.mkPen((255, 255, 255, 28)))
        self.profile_curve = self.profile_plot.plot(
            pen=pg.mkPen("#4cc9f0", width=2), symbol="o", symbolSize=3,
            symbolBrush="#4cc9f0", symbolPen=None, connect="finite")
        self.profile_y = YScaler(self.profile_plot, settle_s=1.0, pad=0.05)
        # C axis (rotary) annotation, pinned to the plot's top-right corner.
        self.c_text = pg.TextItem(anchor=(1, 0), color="#ffd166")
        self.c_text.setParentItem(self.profile_plot.getViewBox())
        font = QtGui.QFont()
        font.setPointSize(15)
        font.setBold(True)
        self.c_text.setFont(font)
        self.profile_plot.getViewBox().sigResized.connect(self._place_c_text)
        self.stats_label = pg.LabelItem(justify="left")
        left.addItem(self.stats_label, row=1, col=0)
        left.ci.layout.setRowStretchFactor(0, 5)
        split.addWidget(left)

        right = QtWidgets.QWidget()
        rbox = QtWidgets.QVBoxLayout(right)
        rbox.setContentsMargins(0, 0, 0, 0)
        rbox.setSpacing(2)
        self.series_view = pg.GraphicsLayoutWidget()
        rbox.addWidget(self.series_view, 1)
        self.history_bar = QtWidgets.QScrollBar(QtCore.Qt.Orientation.Horizontal)
        self.history_bar.setToolTip("Paused: scroll back through buffered history")
        self.history_bar.valueChanged.connect(self._on_history_scroll)
        self.history_bar.hide()
        rbox.addWidget(self.history_bar)
        split.addWidget(right)
        split.setSizes([520, 1080])
        self._build_series()
        return split

    def _place_c_text(self, *_):
        r = self.profile_plot.getViewBox().rect()
        self.c_text.setPos(r.right() - 6, r.top() + 4)

    def _selected(self):
        return [k for k, a in self.sensor_actions.items() if a.isChecked()]

    def _build_series(self):
        """(Re)create one plot + gauge per selected sensor. Width is always first."""
        old = getattr(self, "master", None)
        keep = old.viewRange()[0] if old is not None and self.paused else None
        self.series_view.clear()
        self.rows = []
        keys = ["width"] + self._selected()
        eng = self.units_chk.isChecked()
        self.master = None
        for i, key in enumerate(keys):
            p = self.series_view.addPlot(row=i, col=0)
            sig = SIGNALS.get(key)
            title = "width (mm)" if sig is None else "{} ({})".format(sig.label, sig.unit(eng))
            p.setLabel("left", title)
            p.getAxis("left").setWidth(64)
            p.getAxis("left").enableAutoSIPrefix(False)
            p.setMouseEnabled(x=self.paused, y=False)
            p.hideButtons()
            if self.master is None:
                self.master = p
                p.getViewBox().sigXRangeChanged.connect(self._on_view_changed)
            else:
                p.setXLink(self.master)
            if i < len(keys) - 1:
                p.hideAxis("bottom")
            else:
                p.setLabel("bottom", "elapsed (s)")
            gauge = GaugeItem(WIDTH_COLOR if sig is None else SIGNAL_COLOR[key])
            self.series_view.addItem(gauge, row=i, col=1)
            row = SeriesRow(p, gauge, key, WIDTH_COLOR if sig is None else SIGNAL_COLOR[key])
            self.rows.append(row)
        self.target_line = self.rows[0].plot.addLine(
            y=self.target_spin.value(), pen=pg.mkPen("#40dc6e", style=QtCore.Qt.PenStyle.DashLine))
        self._rows_sink = self.sink
        self._view_key = None
        if keep is not None:  # a rebuild while paused keeps the rewound view
            self.master.setXRange(*keep, padding=0)

    def _build_bead_tab(self):
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.gl = None
        if self.opts.no_3d:
            split.addWidget(QtWidgets.QLabel("3D view disabled (--no-3d)"))
        else:
            try:
                import pyqtgraph.opengl as gl

                w = gl.GLViewWidget()
                w.setBackgroundColor("k")
                w.setCameraPosition(pos=QtGui.QVector3D(0, -120, 10), distance=360,
                                    elevation=26, azimuth=-62)
                self.grid3d = gl.GLGridItem()
                self.grid3d.setSpacing(10, 10)
                w.addItem(self.grid3d)
                self.surface = gl.GLSurfacePlotItem(shader=None, smooth=False,
                                                    computeNormals=False, glOptions="opaque")
                w.addItem(self.surface)
                # Elapsed-time labels are 2D overlays positioned by projecting their 3D anchor.
                # pyqtgraph's GLTextItem paints through a QPainter mid-render and does not
                # show on Qt 6.11 (verified in isolation), so it is not used.
                self.time_labels = []
                for _ in range(9):
                    lbl = QtWidgets.QLabel(w)
                    lbl.setStyleSheet("color: rgba(255,255,255,220); background: transparent;"
                                      "font-size: 11px; font-weight: bold;")
                    lbl.hide()
                    self.time_labels.append(lbl)
                self.gl = w
                self.lut3d = pg.colormap.get("viridis").getLookupTable(
                    0.0, 1.0, 256, alpha=True).astype(np.float32) / 255.0
                split.addWidget(w)
            except Exception as e:  # noqa: BLE001 -- no GL context (e.g. remote desktop)
                split.addWidget(QtWidgets.QLabel("3D view unavailable: {}".format(e)))

        maps = pg.GraphicsLayoutWidget()
        self.layer_maps = [
            LayerMap(maps.addPlot(row=0, col=0, rowspan=2), "Current layer"),
            LayerMap(maps.addPlot(row=0, col=1), "Previous layer (-1)"),
            LayerMap(maps.addPlot(row=1, col=1), "Layer -2"),
        ]
        self.width_bar = pg.ColorBarItem(values=(self.brushes.lo, self.brushes.hi),
                                         colorMap=self.brushes.cmap, interactive=False,
                                         label="width (mm)", width=14)
        maps.addItem(self.width_bar, row=0, col=2, rowspan=2)
        lay = maps.ci.layout
        lay.setColumnStretchFactor(0, 6)
        lay.setColumnStretchFactor(1, 4)
        split.addWidget(maps)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 6)
        split.setSizes([380, 570])
        self._on_target_changed()
        return split

    # --- source control ---------------------------------------------------------

    @property
    def is_sim(self):
        return self.source_box.currentIndex() == 1

    def _speed(self):
        return float(self.speed_box.currentText().rstrip("x"))

    def _sync_controls(self):
        for w in (self.run_box, self.speed_box, self.loop_chk):
            w.setEnabled(self.is_sim)

    def start(self):
        self.stop()
        self.sink = ViewerSink()
        self.sink.tracker.extrapolate = self.dr_chk.isChecked()
        self._last_counts = None
        self._last_totals = {}
        self._cam_set = False
        for lm in getattr(self, "layer_maps", []):
            lm.follow = True
        self.pause_btn.setChecked(False)
        if self.is_sim:
            if not self.runs:
                self.banner.setText("SIMULATION: no recorded runs found under {}".format(
                    self.opts.data or config.DATA_DIR))
                self.banner.show()
                return
            run = self.runs[max(self.run_box.currentIndex(), 0)]
            self.source = ReplaySource(self.sink, run, speed=self._speed(),
                                       loop=self.loop_chk.isChecked(), start_s=self.opts.start)
            self.banner.setText("SIMULATION -- replaying recorded run {}  --  NOT LIVE DATA".format(
                run.name))
            self.banner.show()
            self.setWindowTitle("printmon -- SIMULATION")
        else:
            self.source = LiveSource(self.sink)
            self.banner.hide()
            self.setWindowTitle("printmon -- LIVE")
        self.source.start()
        self.start_btn.setText("Stop")

    def stop(self):
        if self.source is not None:
            self.source.stop()
            self.source = None
        self.start_btn.setText("Start")

    def _on_start_stop(self):
        if self.source is not None:
            self.stop()
        else:
            self.start()

    def _on_source_changed(self, _):
        self._sync_controls()
        self.start()

    def _restart_if_sim(self):
        if self.is_sim:
            self.start()

    def _on_speed_changed(self, _):
        if isinstance(self.source, ReplaySource):
            self.source.speed = self._speed()

    def _on_dead_reckon(self, on):
        if self.sink is not None:
            self.sink.tracker.extrapolate = on

    def _on_sensors_changed(self, *_):
        self._store("sensors", ",".join(self._selected()))
        self._build_series()

    def _on_units_changed(self, on):
        self._store("engineering", "true" if on else "false")
        self._build_series()

    def _on_target_changed(self, *_):
        target, tol = self.target_spin.value(), self.tol_spin.value()
        self._store("target", target)
        self._store("tolerance", tol)
        self.brushes.set_range(target, tol)
        if hasattr(self, "width_bar"):
            self.width_bar.setLevels((target - tol, target + tol))
        if getattr(self, "rows", None):
            self.target_line.setValue(target)

    # --- pause / rewind ---------------------------------------------------------

    def set_paused(self, paused):
        self.paused = bool(paused)
        self.pause_btn.setText("Resume" if paused else "Pause")
        if isinstance(self.source, ReplaySource):
            self.source.set_paused(paused)
        for row in self.rows:
            row.plot.setMouseEnabled(x=self.paused, y=False)
        self.history_bar.setVisible(self.paused)
        self._view_key = None
        if self.paused:
            self._sync_history_bar()

    def _history_span(self):
        """(oldest, newest) time every visible trace still has in its buffer."""
        s = self.sink
        if s is None or s.t_latest is None:
            return None
        firsts = [r.ring(s).first_t() for r in self.rows]
        firsts = [f for f in firsts if f is not None]
        return (max(firsts) if firsts else s.t_first), s.t_latest

    def _sync_history_bar(self):
        span = self._history_span()
        if span is None or self.sink.t_first is None:
            return
        oldest, newest = span
        t0 = self.sink.t_first
        (xmin, xmax) = self.master.viewRange()[0]
        width = xmax - xmin
        self._scroll_guard = True
        lo = int((oldest - t0) / 1e8)
        self.history_bar.setRange(lo, max(int((newest - t0) / 1e8 - width * 10), lo))
        self.history_bar.setPageStep(max(int(width * 10), 1))
        self.history_bar.setValue(int(xmin * 10))
        self._scroll_guard = False

    def _on_history_scroll(self, value):
        if self._scroll_guard or not self.paused:
            return
        xmin, xmax = self.master.viewRange()[0]
        width = xmax - xmin
        self.master.setXRange(value / 10.0, value / 10.0 + width, padding=0)

    def _on_view_changed(self, *_):
        if self.paused and not self._scroll_guard:
            self._view_key = None
            self._sync_history_bar()

    # --- refresh ----------------------------------------------------------------

    def _refresh_fast(self):
        self._frames += 1
        s = self.sink
        if s is None or s.t_latest is None:
            return
        if self._rows_sink is not s:
            self._build_series()
        t0 = s.t_first
        now = time.perf_counter()
        window = self.window_spin.value()
        if self.paused:
            xmin, xmax = self.master.viewRange()[0]
            t_min, t_max = t0 + int(xmin * NS), t0 + int(xmax * NS)
            self._t_ref = min(t_max, s.t_latest)
            key = (round(xmin, 3), round(xmax, 3), self.units_chk.isChecked())
            redraw = key != self._view_key
            self._view_key = key
        else:
            t_max = s.t_latest
            t_min = t_max - window * NS
            self._t_ref = t_max
            self.master.setXRange((t_min - t0) / 1e9, (t_max - t0) / 1e9, padding=0)
            redraw = True
        if self.tabs.currentIndex() != 0:
            return
        eng = self.units_chk.isChecked()
        if redraw:
            for row in self.rows:
                row.update(s, t0, t_min, t_max, not self.paused, eng, now)
        self._refresh_profile(s, now)

    def _value_at(self, ring, t, lookback_s=10):
        tt, vv = ring.between(t - int(lookback_s * NS), t)
        return float(vv[-1, 0]) if len(tt) else None

    def _refresh_profile(self, s, now):
        t_ref = self._t_ref
        got = s.profiles.at(t_ref)
        if got is not None:
            _, x, z = got
            zz = np.where(z != 0, z, np.nan)
            self.profile_curve.setData(x, zz)
            if np.isfinite(zz).any():
                self.profile_y.feed((float(np.nanmin(zz)), float(np.nanmax(zz))), now)
        c = self._value_at(s.signals["C"], t_ref)
        self.c_text.setText("C = {} °".format(_fmt(c)) if c is not None else "C = --")
        self._place_c_text()
        if now - self._stats_t < 0.2:
            return
        self._stats_t = now
        _, wv = s.width.between(t_ref - 2 * NS, t_ref)
        wv = wv[:, 0][np.isfinite(wv[:, 0])] if len(wv) else np.empty(0)
        w_now = float(wv[-1]) if len(wv) else None
        tp, pv = s.points.between(t_ref - 2 * NS, t_ref)
        pos = pv[-1] if len(tp) else None
        parts = [
            "<b>width</b> {} mm".format(_fmt(w_now)),
            "2 s mean {:.2f} / sd {:.2f} mm".format(wv.mean(), wv.std()) if len(wv) else "",
            "<b>pos</b> X {:.1f} Y {:.1f} Z {:.1f}".format(*pos[:3]) if pos is not None else "",
            "<span style='color:#ff8c00'><b>PAUSED</b> t = {:.1f} s</span>".format(
                (t_ref - s.t_first) / 1e9) if self.paused else "",
        ]
        self.stats_label.setText("&nbsp;&nbsp;".join(p for p in parts if p), size="10pt")

    def _refresh_slow(self):
        s = self.sink
        if s is None or s.t_latest is None or self._t_ref is None:
            return
        if self.tabs.currentIndex() == 1:
            if self.gl is not None:
                self._refresh_3d(s)
            # Layer maps change slowly: ~3 Hz, and only when new points arrived.
            self._slow_tick = getattr(self, "_slow_tick", 0) + 1
            key = (s.points.total, self._t_ref if self.paused else None, self.brushes.version)
            if self._slow_tick % 2 == 0 and key != getattr(self, "_layers_key", None):
                self._layers_key = key
                self._refresh_layers(s)

    def _refresh_3d(self, s):
        t_ref = self._t_ref
        window = self.window_spin.value()
        got = s.profiles.window(t_ref - window * NS, t_ref, max_rows=self.opts.max_rows,
                                dense_tail=True)
        if got is None:
            return
        tt, d, h = got
        h = h[:, ::2]  # 0.5 mm across the bead is finer than the screen resolves
        if not np.isfinite(h).any():
            self.surface.setVisible(False)  # nothing in laser range (e.g. gantry parked)
            return
        self.surface.setVisible(True)
        floor = np.nanpercentile(h, 2)
        h = np.where(np.isfinite(h), h, floor) - floor
        top = max(float(np.nanpercentile(h, 99.5)), 1e-3)
        y = d - d[-1]
        z = h.T  # (grid, rows)
        # Flat (grid*rows, 4), row-major -- the order GLSurfacePlotItem flattens vertices in.
        idx = (np.clip(z / top, 0, 1) * 255).astype(np.intp).ravel()
        self.surface.setData(x=PROFILE_GRID[::2], y=y, z=z, colors=self.lut3d[idx])
        length = float(-y[0])
        if not self._cam_set and len(y) >= 60:
            near = min(length, 500.0)
            self.gl.setCameraPosition(pos=QtGui.QVector3D(0, -near * 0.2, 10),
                                      distance=near * 1.35 + 100, elevation=24, azimuth=-62)
            self._cam_set = True
        self.grid3d.setSize(80, max(length, 10))
        self.grid3d.resetTransform()
        self.grid3d.translate(0, -length / 2, 0)
        self._place_time_labels(tt, y, t_ref, window, top)

    def _place_time_labels(self, tt, y, t_ref, window, top):
        """
        Elapsed-time ticks along the bead: 0 s at the newest profile, back to -window.

        Progressive spacing (0, 1, 2, 5, 10, 20 ... s): at F8000 one second is ~130 mm of
        bead, so evenly spaced ticks would all sit far from the nozzle where the camera
        looks. The last tick always marks the full window.
        """
        ticks = [v for v in (0, 1, 2, 5, 10, 20, 30, 60, 120, 300) if v < window] + [window]
        ticks = ticks[:len(self.time_labels)]
        for i, lbl in enumerate(self.time_labels):
            if i >= len(ticks):
                lbl.setVisible(False)
                continue
            t_k = t_ref - int(ticks[i] * NS)
            if t_k < tt[0]:
                lbl.setVisible(False)
                continue
            j = min(int(np.searchsorted(tt, t_k)), len(tt) - 1)
            pt = self._project(36.0, float(y[j]), float(top) + 2.0)
            if pt is None:
                lbl.setVisible(False)
                continue
            lbl.setText("{}{:g} s".format("-" if ticks[i] else "", ticks[i]))
            lbl.adjustSize()
            lbl.move(int(pt.x()) + 4, int(pt.y()) - lbl.height() // 2)
            lbl.setVisible(True)

    def _project(self, x, y, z):
        """Screen position of a 3D point in the GL view, or None if off-screen/behind."""
        rect = QtCore.QRectF(self.gl.rect())
        to_screen = QtGui.QMatrix4x4()
        to_screen.viewport(rect.left(), rect.bottom(), rect.width(), -rect.height())
        mvp = self.surface.mvpMatrix()
        clip = mvp.map(QtGui.QVector4D(x, y, z, 1.0))
        if clip.w() <= 0:
            return None
        pt = (to_screen * mvp).map(QtGui.QVector3D(x, y, z)).toPointF()
        return pt if rect.adjusted(-5, -5, 5, 5).contains(pt) else None

    def _refresh_layers(self, s):
        tp, pv = s.points.between(s.points.first_t() or 0, self._t_ref)
        if len(tp) == 0:
            for lm in self.layer_maps:
                lm.show(None, [], [], [], self.brushes, None)
            return
        xs, ys, zs, ws = pv[:, 0], pv[:, 1], pv[:, 2], pv[:, 4]
        layers = recent_layers(zs, n=3, min_points=15)
        masks = [np.round(zs) == z for z in layers]
        sel = np.any(masks, axis=0) if masks else np.zeros(len(zs), bool)
        bbox = (float(xs[sel].min()), float(xs[sel].max()), float(ys[sel].min()),
                float(ys[sel].max())) if sel.any() else None
        for i, lm in enumerate(self.layer_maps):
            if i < len(layers):
                m = masks[i]
                lm.show(layers[i], xs[m], ys[m], ws[m], self.brushes, bbox)
            else:
                lm.show(None, [], [], [], self.brushes, None)
        est = s.tracker.at(self._t_ref) if not self.paused else (xs[-1], ys[-1], zs[-1])
        cur = self.layer_maps[0]
        if est is not None:
            cur.marker.setData([est[0]], [est[1]])

    def _refresh_rates(self):
        now = time.perf_counter()
        dt = now - self._last_rate_t
        self._fps = self._frames / dt if dt > 0 else 0
        self._frames = 0
        self._last_rate_t = now
        s = self.sink
        if s is None:
            self.status_label.setText("stopped")
            return
        if s.emulated and not self.is_sim and not self.banner.isVisible():
            self.banner.setText("EMULATED acquisition services -- recorded frames re-streamed "
                                "by `printmon emulate`  --  NOT THE MACHINE")
            self.banner.show()
            self.setWindowTitle("printmon -- EMULATED")
        # Per-signal sample rates, shown on each gauge.
        for row in self.rows:
            total = row.ring(s).total
            prev = self._last_totals.get(row.key)
            row.hz = (total - prev) / dt if prev is not None and dt > 0 else None
            self._last_totals[row.key] = total
        counts = dict(s.counts)
        if self._last_counts is not None and dt > 0:
            self.rates = {k: (counts[k] - self._last_counts.get(k, 0)) / dt for k in counts}
        self._last_counts = counts
        if self.paused:
            self._sync_history_bar()
        r = self.rates
        parts = ["pump {:.0f} Hz".format(r.get("pump", 0)),
                 "gantry I/O {:.0f} Hz".format(r.get("gantry", 0)),
                 "NC pos {:.0f} Hz".format(r.get("nc", 0)),
                 "scan {:.0f} Hz".format(r.get("scan", 0)),
                 "dropped profiles {}".format(s.dropped),
                 "render {:.0f} FPS".format(self._fps)]
        if isinstance(self.source, ReplaySource) and self.source.duration_s:
            parts.append("t {:.0f}/{:.0f} s".format(self.source.position_s,
                                                    self.source.duration_s))
        parts += ["{}: {}".format(k, v) for k, v in sorted(s.status.items())]
        self.status_label.setText("   |   ".join(parts))

    # --- lifecycle --------------------------------------------------------------

    def closeEvent(self, event):  # noqa: N802 -- Qt API
        self.stop()
        super().closeEvent(event)

    def screenshot_all(self, path):
        """Save one PNG per tab. Used by --screenshot for smoke tests and reports."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        saved = []
        for i in range(self.tabs.count()):
            self.tabs.setCurrentIndex(i)
            QtWidgets.QApplication.processEvents()
            self._refresh_fast()
            self._refresh_slow()
            for _ in range(5):
                QtWidgets.QApplication.processEvents()
                time.sleep(0.05)
            name = re.sub(r"[^a-z0-9]+", "_", self.tabs.tabText(i).lower()).strip("_")
            out = path.with_name("{}_{}{}".format(path.stem, name, path.suffix or ".png"))
            self.grab().save(str(out))
            saved.append(out)
        self.tabs.setCurrentIndex(0)
        return saved


def add_arguments(p):
    p.add_argument("--sim", action="store_true", help="start in simulation (replay) mode")
    p.add_argument("--run", default="", help="replay: substring of the run name")
    p.add_argument("--speed", type=float, default=1.0, help="replay speed factor")
    p.add_argument("--start", type=float, default=0.0, help="replay: skip the first N seconds")
    p.add_argument("--no-loop", action="store_true", help="replay: stop at end of run")
    p.add_argument("--data", default=None, help="recorded runs directory (default data/)")
    p.add_argument("--window", type=int, default=30, help="time window in seconds")
    p.add_argument("--max-rows", type=int, default=250,
                   help="3D bead: max profiles drawn (dense at the nozzle, sparse behind)")
    # 30 FPS is smooth for trends and leaves CPU for the acquisition services, which run
    # on the same PC. Measured ceiling on the lab workstation: ~38 FPS (2D), ~28 with the
    # OpenGL view present (Qt then composites the whole window through GL, vsync-bound).
    p.add_argument("--fps", type=int, default=30, help="render rate cap")
    p.add_argument("--target-width", type=float, default=35.0, help="width target, mm")
    p.add_argument("--width-tol", type=float, default=5.0,
                   help="+/- range of the width colour scale, mm")
    p.add_argument("--no-3d", action="store_true", help="disable the OpenGL 3D view")
    p.add_argument("--pause-after", type=float, default=None,
                   help="testing: pause after N seconds and rewind by --rewind seconds")
    p.add_argument("--rewind", type=float, default=0.0, help="testing: see --pause-after")
    p.add_argument("--screenshot", default=None, help="save a PNG per tab, then exit")
    p.add_argument("--quit-after", type=float, default=None, help="exit after N seconds")


def run(opts):
    pg.setConfigOptions(antialias=False, background="k", foreground="#dddddd")
    app = pg.mkQApp("printmon")
    win = MonitorWindow(opts)
    win.show()
    if opts.pause_after:
        def pause_and_rewind():
            win.pause_btn.setChecked(True)
            xmin, xmax = win.master.viewRange()[0]
            win.master.setXRange(xmin - opts.rewind, xmax - opts.rewind, padding=0)
        QtCore.QTimer.singleShot(int(opts.pause_after * 1000), pause_and_rewind)
    if opts.quit_after or opts.screenshot:
        def finish():
            if opts.screenshot:
                for out in win.screenshot_all(opts.screenshot):
                    print("saved {}".format(out))
            print("render {:.0f} FPS".format(win._fps))
            s = win.sink
            if s is not None:
                print("records: {}  scan {}  dropped {}  paused {}".format(
                    "  ".join("{} {}".format(k, r.total) for k, r in s.signals.items()),
                    s.width.total, s.dropped, win.paused))
            win.close()
            app.quit()
        QtCore.QTimer.singleShot(int((opts.quit_after or 8) * 1000), finish)
    app.exec()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="printmon view", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(p)
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
