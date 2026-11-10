// Replay tab: the replay video's picture made interactive. The print builds up
// in 3D while the pump signals run on the same clock, with a play button and a
// slider. The charts hold the whole record; a mask hides what has not happened
// yet, so playing never redraws Plotly. Zoom is one time window shared by every
// panel, with each panel's scale fitted to what the window shows.
import { Scene } from './scene.js';
import { PLOT_CONFIG, VIRIDIS, api, chartBase, clock, el, lastAtOrBefore, post, token } from './util.js';

const PLAY = '<svg viewBox="0 0 16 16"><path d="M4 2.2v11.6a.6.6 0 0 0 .92.5l9-5.8a.6.6 0 0 0 0-1l-9-5.8A.6.6 0 0 0 4 2.2z"/></svg>';
const PAUSE = '<svg viewBox="0 0 16 16"><rect x="3" y="2" width="3.6" height="12" rx="1"/><rect x="9.4" y="2" width="3.6" height="12" rx="1"/></svg>';
const START = '<svg viewBox="0 0 24 24"><path d="M6 5v14M19 5.5 9.5 12l9.5 6.5z"/></svg>';
const EXPAND = '<svg viewBox="0 0 24 24"><path d="M9 4H4v5M15 4h5v5M9 20H4v-5M15 20h5v-5"/></svg>';
const SPEEDS = [5, 10, 20, 40, 80, 160];
const GAP = 0.06;
const PANELS = ['Torque', 'P1', 'W/S', 'Width', 'Feed'];
const SPANS = [['All', null], ['10 min', 600], ['5 min', 300], ['2 min', 120]];
const MIN_SPAN_S = 20;                 // the closest zoom
const FOLLOW_EDGE = 0.04;              // share of the window kept ahead of the cursor
const AUTOPLAY = true;                 // the public build plays a print as soon as its data is in

// y range of the values, stray ends cut off, never tighter than minSpan
function fit(values, cut, minSpan) {
  const v = values.filter((x) => x !== null && x !== undefined).sort((p, r) => p - r);
  if (!v.length) return null;
  const at = (q) => v[Math.floor(q * (v.length - 1))];
  const lo = at(cut), hi = at(1 - cut), pad = Math.max((hi - lo) * 0.1, (minSpan - (hi - lo)) / 2);
  return [lo - pad, hi + pad];
}

// a width's colour on the 3D view's scale (a bar trace given numbers logs a warning per bar)
function viridisAt(v, lo, hi) {
  const f = Math.max(0, Math.min(1, (v - lo) / ((hi - lo) || 1))) * (VIRIDIS.length - 1);
  const k = Math.min(Math.floor(f), VIRIDIS.length - 2), u = f - k;
  const part = (hex, i) => parseInt(hex.slice(i, i + 2), 16);
  return `rgb(${[1, 3, 5].map((i) => Math.round(part(VIRIDIS[k], i) * (1 - u) + part(VIRIDIS[k + 1], i) * u)).join(',')})`;
}

// the whiskers as one line with gaps: a stem and a cap beyond each end of each box
function whiskerPath(bars, centre, cap) {
  const x = [], y = [];
  centre.forEach((c, i) => {
    for (const [from, to] of [[bars.hi[i], bars.whi[i]], [bars.lo[i], bars.wlo[i]]]) {
      x.push(c, c, null, c - cap, c + cap, null);
      y.push(from, to, null, to, to, null);
    }
  });
  return { x, y };
}

// one interval's box (a bar from the lower to the upper quartile, coloured by the
// median) and its whiskers, as two traces on `yaxis`
function boxTraces(bars, yaxis, colorOf, unit, nd, name) {
  const centre = bars.t.map((v) => v / 60 - bars.bin_s / 120), boxWidth = bars.bin_s / 60 * 0.9;
  return [
    { name, type: 'bar', x: centre, width: boxWidth, base: bars.lo, y: bars.hi.map((v, i) => v - bars.lo[i]), yaxis,
      marker: { color: bars.w.map(colorOf), line: { color: `${token('ink')}59`, width: 1 } },   // translucent: on a long print a bar is 2 px wide
      customdata: bars.w.map((v, i) => [v, bars.lo[i], bars.hi[i], bars.wlo[i], bars.whi[i], bars.n[i]]),
      hovertemplate: `median %{customdata[0]:.${nd}f} ${unit}, middle 50 % %{customdata[1]:.${nd}f} to %{customdata[2]:.${nd}f}, `
        + `whiskers %{customdata[3]:.${nd}f} to %{customdata[4]:.${nd}f}, %{customdata[5]} profiles<extra></extra>` },
    { name: `${name} whiskers`, yaxis, mode: 'lines', ...whiskerPath(bars, centre, boxWidth * 0.25), line: { color: token('ink-2'), width: 1 }, hoverinfo: 'skip' },
  ];
}

export class ReplayTab {
  constructor(root) {
    this.root = root;
    this.playing = false;
    this.t = 0;
    this.speed = 40;
    this.request = 0;
    this.full = false;
    this.notice('Pick a specimen in the sidebar.');
    document.addEventListener('keydown', (e) => this.key(e));
    // Esc in the browser's own full screen never reaches the page as a key
    document.addEventListener('fullscreenchange', () => { if (!document.fullscreenElement) this.setFull(false); });
  }

  notice(text, busy = false) {
    this.pause();
    this.resizer?.disconnect();                // the chart it watches is about to go
    this.built = false;
    this.root.replaceChildren(el('div', { class: 'card notice' },
      el('div', {}, busy ? el('div', { class: 'spinner' }) : null, text)));
  }

  async load(name, keys) {
    if (!name) { this.notice('Pick a specimen in the sidebar.'); return; }
    const mine = ++this.request;
    this.notice(`Loading ${name}…`, true);
    try {
      const one = keys.length === 1 && keys[0][0] === 'corpus';   // one corpus print: a file a static host can serve
      const d = one ? await api(`./api/replay/${encodeURIComponent(keys[0][1])}.json`) : await post('./api/replay', { name, keys });
      if (mine !== this.request) return;
      this.view = null;
      this.picked = new Set();
      this.show(d);
      this.wantsPlay = AUTOPLAY && !matchMedia('(prefers-reduced-motion: reduce)').matches;
      this.startIfWanted();
    } catch (e) {
      if (mine === this.request) this.notice(e.message);
    }
  }

  // ---------------------------------------------------------------- layout
  show(d) {
    this.d = d;
    this.full = false;
    const stat = (id, label) => el('div', { class: 'stat' }, el('label', {}, label), el('output', {}, this.out(id)));
    this.outs = {};
    const stats = el('div', { class: 'stats' },
      stat('time', 'Print time'), stat('layer', 'Layer'), stat('z', 'Nozzle Z'), stat('torque', 'Torque (30 s)'),
      stat('P1', 'Pressure P1'), stat('P2', 'Pressure P2'), stat('T1', 'Temp. T1'),
      stat('ws', 'W/S, mixer out'), stat('width', 'Scan width'));
    const sub = [`${d.layers} layers of ${+d.layer_h.toFixed(1)} mm`, d.feed ? `at ${+d.feed.toFixed(2)} m/min` : null].filter(Boolean).join(' ');

    this.sceneHost = el('div', { class: 'scene card' });
    const tools = el('div', { class: 'scene-tools' });
    this.bandsBtn = el('button', {
      class: 'chip', 'aria-pressed': 'true', hidden: !d.bands,
      onclick: () => { this.scene.showBands = !this.scene.showBands; this.bandsBtn.setAttribute('aria-pressed', this.scene.showBands); this.scene.dirty(); },
    }, 'Scan sections');
    tools.append(this.bandsBtn, el('button', { class: 'chip', onclick: () => this.scene.home() }, 'Reset view'));
    const key = el('div', { class: 'scene-key' });
    if (d.range[0] !== null) {
      key.append(el('div', { class: 'bar' }, 'Bead width (mm)', el('i'),
        el('div', {}, ...this.ticks(d.range).map((v) => el('span', {}, String(v))))));
    }
    if (d.bands) key.append(el('span', { class: 'key-item' }, el('i', { style: `background:${token('band')};opacity:.35` }), 'width measured here'));
    else if (d.range[0] !== null) key.append(el('span', { class: 'key-item' }, `C follows the path: ${Math.round(d.band_share * 100)} % of profiles usable`));
    key.append(el('span', { class: 'key-item' }, el('i', { style: `background:${token('bead')}` }),
      d.range[0] === null ? 'no laser scan on this print, so no width' : 'no width'));
    this.sceneHost.append(tools, key);

    this.plot = el('div', { class: 'plot' });
    this.mask = el('canvas');
    this.spanSeg = el('div', { class: 'seg', role: 'group', 'aria-label': 'Time window' },
      ...SPANS.map(([label, span]) => el('button', { onclick: () => this.zoomTo(span) }, label)));
    this.panelSeg = el('div', { class: 'seg', role: 'group', 'aria-label': 'Panels' },
      ...['All', ...PANELS].map((label, i) => el('button', { onclick: () => this.togglePanel(i ? i - 1 : null) }, label)));
    this.fullBtn = el('button', { class: 'btn tool-full', onclick: () => this.setFull(!this.full) }, 'Full screen');
    this.panelFull = PANELS.map((name, row) => {
      const btn = el('button', {
        class: 'panel-full', title: `${name} full screen`, 'aria-label': `${name} full screen`,
        onclick: () => this.fullPanel(row),
      });
      btn.innerHTML = EXPAND;
      return btn;
    });
    const chart = el('div', { class: 'plot-wrap card' },
      el('div', { class: 'chart-tools' }, 'Time', this.spanSeg, 'Panel', this.panelSeg,
        el('span', { class: 'tool-hint' }, 'Drag on a chart to zoom, double-click to reset'), this.fullBtn),
      el('div', { class: 'plot-area' }, this.plot, this.mask, ...this.panelFull));
    chart.addEventListener('wheel', (e) => this.wheel(e), { passive: false });

    this.playBtn = el('button', { class: 'play', 'aria-label': 'Play', onclick: () => this.toggle() });
    this.playBtn.innerHTML = PLAY;
    const start = el('button', { class: 'icon-btn', 'aria-label': 'Back to the start', onclick: () => { this.pause(); this.seek(0); } });
    start.innerHTML = START;
    this.slider = el('input', {
      type: 'range', min: 0, max: d.t_end, step: 0.1, 'aria-label': 'Print time',
      oninput: (e) => { this.pause(); this.seek(+e.target.value); },
    });
    const ticks = el('div', { class: 'ticks' }, ...d.layer_t.map((t) => el('i', { style: `left:${t / d.t_end * 100}%` })));
    const pick = SPEEDS.reduce((a, b) => (Math.abs(b - d.t_end / 48) < Math.abs(a - d.t_end / 48) ? b : a));
    this.speed = pick;
    const speed = el('label', { class: 'speed' }, 'Speed',
      el('select', { onchange: (e) => { this.speed = +e.target.value; } },
        ...SPEEDS.map((s) => el('option', { value: s, selected: s === pick }, `${s}×`))));
    this.clockNow = el('b');
    const transport = el('div', { class: 'transport card' },
      el('div', { class: 't-btns' }, start, this.playBtn),
      el('div', { class: 'track' }, ticks, this.slider),
      el('div', { class: 'clock' }, this.clockNow, ` / ${clock(d.t_end)}`), speed);

    this.frame = el('div', { class: 'replay' },
      el('div', { class: 'page-head' }, el('h2', {}, d.title), el('p', {}, sub)),
      transport,
      this.recordCard(d.record),
      stats,
      el('div', { class: 'stage' }, this.sceneHost, chart));
    this.root.replaceChildren(this.frame);

    this.scene = new Scene(this.sceneHost);
    if (!this.scene.ok) this.sceneHost.append(el('div', { class: 'notice' }, 'This browser has no WebGL2, so the 3D view is off. The signals below still play.'));
    this.scene.setPrint(d);
    this.pt = new Float64Array(d.path.t);
    this.breaks = new Set(d.path.breaks);
    this.st = new Float64Array(d.sig.t);
    this.bt = new Float64Array(d.width_bars.t);
    this.doseT = new Float64Array(d.doses.t);
    this.drawChart();
    this.built = true;
    this.resizer?.disconnect();
    this.resizer = new ResizeObserver(() => {
      if (this.root.hidden) return;            // Plotly refuses to resize a hidden chart; shown() catches up
      Plotly.Plots.resize(this.plot);
      this.fitMask();
    });
    this.resizer.observe(chart);
    this.seek(d.t_end);
  }

  out(id) {
    const n = el('span');
    this.outs ??= {};
    this.outs[id] = n;
    return n;
  }

  ticks([lo, hi]) {
    const n = 4, out = [];
    for (let i = 0; i <= n; i++) out.push(+(lo + (hi - lo) * i / n).toFixed(1));
    return out;
  }

  // the print's record, as the header of a batch sheet: what was set, what the scanner saw
  recordCard(r) {
    if (!r) return null;
    const item = (k, v) => (v === null || v === undefined ? null : el('div', {}, el('dt', {}, k), el('dd', {}, v)));
    const pct = (v) => (v === null || v === undefined ? null : `${Math.round(v * 100)} %`);
    return el('dl', { class: 'record card' },
      item('Date', r.date || null), item('First to last layer', clock(r.printing_s)),
      item('Layers', `${r.layers} × ${+r.layer_h.toFixed(1)} mm`),
      item('Head speed', r.feed_mm_min === null ? null : `${(r.feed_mm_min / 1000).toFixed(1)} m/min`),
      item('Water valve', r.valve === null ? null : (r.valve[0] === r.valve[1] ? `${r.valve[0]} %` : `${r.valve[0]} to ${r.valve[1]} %`)),
      item('Mortar', r.mortar), item('Scan profiles', r.profiles ? String(r.profiles) : null),
      item('With a width', pct(r.width_share)));
  }

  // ---------------------------------------------------------------- charts
  drawChart() {
    const d = this.d, { axis, layout } = chartBase();
    const mins = (a) => a.map((v) => v / 60);
    const x = mins(d.sig.t), bars = d.width_bars;
    const rows = this.shownRows();
    const h = (1 - GAP * (rows.length - 1)) / rows.length;
    const dom = (i) => {
      const at = rows.indexOf(i);
      if (at < 0) return [0, 0.001];
      const top = 1 - at * (h + GAP);
      return [top - h, top];
    };
    const [wLo, wHi] = d.range;
    const [a, b] = this.window();
    const fits = this.fits(a, b);
    const hov = (u, nd) => `%{y:.${nd}f} ${u}<extra></extra>`;
    const traces = [
      { name: 'torque', x, y: d.sig.torque, yaxis: 'y', mode: 'lines', line: { color: token('s1'), width: 1.6 }, hovertemplate: hov('%', 1) },
      { name: 'P1', x, y: d.sig.P1, yaxis: 'y2', mode: 'lines', line: { color: token('s2'), width: 1.2 }, hovertemplate: hov('bar', 1) },
      { name: 'W/S', x, y: d.sig.ws, yaxis: 'y3', mode: 'lines', line: { color: token('ws'), width: 1.6 }, hovertemplate: hov('kg/kg', 3) },
      { name: 'dose', x: mins(d.doses.t), y: d.doses.ws, yaxis: 'y3', mode: 'markers',
        marker: { size: rows.length > 1 ? 5 : 8, color: token('ink'), line: { color: token('surface'), width: 1 } },
        hovertemplate: 'dose %{y:.3f} kg/kg<extra></extra>' },
      // one rectangle per interval: across the seconds it covers, up the middle 50 %
      ...boxTraces(bars, 'y4', (v) => viridisAt(v, wLo ?? 0, wHi ?? 1), 'mm', 1, 'width'),
      { name: 'feed', x, y: d.sig.feed, yaxis: 'y5', mode: 'lines', line: { color: token('feed'), width: 1.6 }, hovertemplate: hov('m/min', 2) },
    ];
    const key = (color, dash) => `<span style="color:${color}">${dash ? '╌╌' : '━━'}</span>`;
    const dotKey = (color) => `<span style="color:${color}">●</span>`;
    for (const t of traces) t.visible = rows.includes({ y: 0, y2: 1, y3: 2, y4: 3, y5: 4 }[t.yaxis]);
    const title = (i, text) => ({
      text, xref: 'paper', yref: 'paper', x: 0, y: dom(i)[1], xanchor: 'left', yanchor: 'bottom', showarrow: false, yshift: 1,
      font: { size: 12, color: token('ink-2') }, visible: rows.includes(i), captureevents: true,
    });
    const yaxis = (i) => ({ ...axis, domain: dom(i), range: fits[i] ?? undefined, fixedrange: true, nticks: rows.length > 1 ? 4 : 8, visible: rows.includes(i) });
    const last = rows[rows.length - 1];
    this.layout = {
      ...layout,
      margin: { l: 44, r: 14, t: 24, b: 34 },
      showlegend: false,
      hovermode: 'x unified',
      hoversubplots: 'axis',
      dragmode: 'zoom',
      xaxis: { ...axis, title: { text: 'print time (min)', font: axis.title.font }, range: [a / 60, b / 60], anchor: last ? `y${last + 1}` : 'y', showspikes: false, hoverformat: '.1f' },
      yaxis: yaxis(0),
      yaxis2: yaxis(1),
      yaxis3: yaxis(2),
      yaxis4: yaxis(3),
      yaxis5: yaxis(4),
      annotations: [
        title(0, 'Pump torque (%), 30 s mean'),
        title(1, 'Pressure P1 (bar)'),
        title(2, `W/S leaving the mixer (kg/kg)  ${key(token('ws'))} stirred-tank model  ${dotKey(token('ink'))} each dose`),
        title(3, `Bead width (mm) per ${bars.bin_s} s  <span style="color:${VIRIDIS[5]}">■</span> middle 50 % of profiles, colour is the median  <span style="color:${token('ink-2')}">┃</span> whiskers reach 1.5 × the box`),
        title(4, 'Feed, head speed (m/min)'),
      ],
    };
    Plotly.react(this.plot, traces, this.layout, { ...PLOT_CONFIG, doubleClick: false }).then(() => {
      if (!this.plot.wired) {
        this.plot.wired = true;
        this.plot.on('plotly_afterplot', () => this.fitMask());
        this.plot.on('plotly_relayout', (e) => this.dragged(e));
        this.plot.on('plotly_doubleclick', () => this.resetZoom());
        this.plot.ondblclick = () => this.resetZoom();        // between panels Plotly sees nothing
        this.plot.on('plotly_clickannotation', (e) => this.soloPanel(e.index));
      }
      this.fitMask();
    });
    this.plot.onclick = (e) => {
      const g = this.geometry(), [a0, b0] = this.window();
      if (!g) return;
      const r = this.plot.getBoundingClientRect(), px = e.clientX - r.left - g.l;
      const now = performance.now();
      if (px < 0 || px > g.w || e.detail > 1 || now - (this.doubleAt ?? 0) < 1000) return;
      if (now - (this.clickAt ?? 0) > 400) this.clickFrom = this.t;
      this.clickAt = now;
      this.pause();
      this.seek(a0 + px / g.w * (b0 - a0));
    };
    this.syncTools();
  }

  // ---------------------------------------------------------------- zoom
  window() { return this.view ?? [0, this.d.t_end]; }

  // each panel's y range for the window a..b (s)
  fits(a, b) {
    const d = this.d, s = d.sig, bars = d.width_bars;
    const i0 = lastAtOrBefore(this.st, a), i1 = lastAtOrBefore(this.st, b) + 1;
    const cut = (arr) => arr.slice(i0, i1);
    const j0 = lastAtOrBefore(this.bt, a), j1 = lastAtOrBefore(this.bt, b) + 1;
    const k0 = lastAtOrBefore(this.doseT, a), k1 = lastAtOrBefore(this.doseT, b) + 1;
    const ws = fit(cut(s.ws), 0.01, 0.02), dose = fit(d.doses.ws.slice(k0, k1), 0.01, 0.02);   // the doses scatter far beyond the line
    return [
      fit(cut(s.torque), 0.005, 2),
      fit(cut(s.P1), 0.005, 1),
      ws && dose ? [Math.min(ws[0], dose[0]), Math.max(ws[1], dose[1])] : ws ?? dose,
      fit([...bars.wlo.slice(j0, j1), ...bars.whi.slice(j0, j1)], 0.01, 3),
      fit(cut(s.feed), 0.005, 1),
    ];
  }

  // a double-click is a reset, not two jumps of the cursor
  resetZoom() {
    this.doubleAt = performance.now();
    this.seek(this.clickFrom ?? this.t);
    this.setView(null, null);
  }

  // a drag on the chart: Plotly has moved the time axis, take it from there
  dragged(e) {
    const a = e['xaxis.range[0]'], b = e['xaxis.range[1]'];
    if (a !== undefined && b !== undefined) this.setView(a * 60, b * 60);
  }

  setView(a, b) {
    const T = this.d.t_end;
    let next = null;
    if (a !== null && (a > 0 || b < T)) {
      const span = Math.min(Math.max(b - a, MIN_SPAN_S), T);
      const start = Math.max(0, Math.min(a, T - span));
      if (span < T) next = [start, start + span];
    }
    const [c, e] = this.window(), [p, q] = next ?? [0, T];
    if (Math.abs(c - p) < 0.5 && Math.abs(e - q) < 0.5 && !this.drawnElsewhere(p, q)) return;
    this.view = next;
    const up = { 'xaxis.range': [p / 60, q / 60] };
    this.fits(p, q).forEach((r, i) => { if (r) up[`yaxis${i ? i + 1 : ''}.range`] = r; });
    Plotly.relayout(this.plot, up);
    this.syncTools();
  }

  // the axis Plotly shows, when a drag has moved it past the view kept here
  drawnElsewhere(p, q) {
    const r = this.plot._fullLayout?.xaxis?.range;
    return r && (Math.abs(r[0] * 60 - p) > 0.5 || Math.abs(r[1] * 60 - q) > 0.5);
  }

  // a window of this length with the cursor past its middle: the past, and what comes next
  zoomTo(span) {
    if (span === null) this.setView(null, null);
    else this.setView(this.t - 0.6 * span, this.t + 0.4 * span);
  }

  // the panels on show, top to bottom: the ones picked, or all when none is
  shownRows() {
    return this.picked.size ? [...this.picked].sort((a, b) => a - b) : PANELS.map((_, i) => i);
  }

  // a panel button: any number can be on; null is the All button
  togglePanel(row) {
    if (row === null) this.picked.clear();
    else if (this.picked.has(row)) this.picked.delete(row);
    else this.picked.add(row);
    if (this.picked.size === PANELS.length) this.picked.clear();
    this.drawChart();
  }

  // a panel's title: that panel alone, or back to all of them
  soloPanel(row) {
    this.picked = this.picked.size === 1 && this.picked.has(row) ? new Set() : new Set([row]);
    this.drawChart();
  }

  // ---------------------------------------------------------------- full screen
  // The same chart and the same transport, laid out over everything else: nothing
  // is rebuilt, so a replay that is running keeps running.
  fullPanel(row) {
    this.pickedBefore = this.picked;
    this.picked = new Set([row]);
    this.drawChart();
    this.setFull(true);
  }

  setFull(on) {
    if (on === this.full || !this.built) return;
    this.full = on;
    this.frame.classList.toggle('full', on);
    this.fullBtn.textContent = on ? 'Exit full screen' : 'Full screen';
    if (on) {
      this.frame.requestFullscreen?.().catch(() => {});       // refused: the page's own layout still fills the window
    } else {
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
      if (this.pickedBefore !== undefined) {
        this.picked = this.pickedBefore;
        this.pickedBefore = undefined;
        this.drawChart();
      }
    }
    Plotly.Plots.resize(this.plot).then(() => this.fitMask());
  }

  // each panel's full screen button, at its top right corner
  placePanelButtons() {
    const g = this.geometry(), L = this.plot._fullLayout;
    if (!g || !L) return;
    this.panelFull.forEach((btn, row) => {
      const ya = L[`yaxis${row ? row + 1 : ''}`];
      const show = !this.full && this.shownRows().includes(row);
      btn.hidden = !show;
      if (show) { btn.style.left = `${g.l + g.w - 22}px`; btn.style.top = `${ya._offset - 22}px`; }
    });
  }

  wheel(e) {
    const scroller = this.root.closest('main');
    if (!e.ctrlKey && scroller.scrollHeight > scroller.clientHeight + 2) return;   // the page scrolls here
    const g = this.geometry();
    if (!g) return;
    e.preventDefault();
    const [a, b] = this.window(), r = this.plot.getBoundingClientRect();
    const f = Math.min(1, Math.max(0, (e.clientX - r.left - g.l) / g.w)), at = a + f * (b - a);
    const span = (b - a) * Math.exp(e.deltaY * 0.0015);
    this.wanted = [at - f * span, at + (1 - f) * span];
    if (this.wheelQueued) return;
    this.wheelQueued = true;
    requestAnimationFrame(() => { this.wheelQueued = false; this.setView(...this.wanted); });
  }

  // a zoomed window travels with the cursor: on a page turn while playing, centred after a jump
  follow() {
    if (!this.view) return;
    const [a, b] = this.view, span = b - a;
    if (this.t >= a && this.t <= b - FOLLOW_EDGE * span) return;
    if (b >= this.d.t_end && this.t >= a) return;
    const start = this.t - (this.playing ? 0.1 : 0.6) * span;
    this.setView(start, start + span);
  }

  syncTools() {
    const span = this.view ? this.view[1] - this.view[0] : null;
    [...this.spanSeg.children].forEach((btn, i) => {
      const want = SPANS[i][1];
      btn.setAttribute('aria-pressed', want === null ? span === null : span !== null && Math.abs(span - want) < 1);
    });
    [...this.panelSeg.children].forEach((btn, i) => btn.setAttribute('aria-pressed', i ? this.picked.has(i - 1) : this.picked.size === 0));
  }

  geometry() {
    const s = this.plot._fullLayout?._size;
    return s ? { l: s.l, t: s.t, w: s.w, h: s.h } : null;
  }

  fitMask() {
    const r = this.mask.getBoundingClientRect();
    if (!r.width) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.mask.width = r.width * dpr;
    this.mask.height = r.height * dpr;
    this.maskSize = [r.width, r.height];
    this.maskCtx = this.mask.getContext('2d');
    this.maskCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.placePanelButtons();
    this.paintMask();
  }

  paintMask() {
    const c = this.maskCtx, g = this.geometry();
    if (!c || !g) return;
    c.clearRect(0, 0, ...this.maskSize);
    const raw = this.px(this.t, g), x = Math.min(Math.max(raw, g.l), g.l + g.w);
    c.fillStyle = token('mask');
    c.fillRect(x, 0, g.l + g.w + 4 - x, g.t + g.h + 2);
    if (raw < g.l || raw > g.l + g.w) return;
    c.strokeStyle = token('ink');
    c.lineWidth = 1;
    c.beginPath();
    c.moveTo(x + 0.5, g.t);
    c.lineTo(x + 0.5, g.t + g.h);
    c.stroke();
  }

  // print time (s) -> pixels across the chart
  px(t, g) {
    const [a, b] = this.window();
    return g.l + g.w * (t - a) / (b - a);
  }

  // ---------------------------------------------------------------- transport
  seek(t) {
    const d = this.d;
    this.t = Math.max(0, Math.min(t, d.t_end));
    const k = lastAtOrBefore(this.pt, this.t), started = this.t >= this.pt[0];
    const p = d.path, s = d.sig, i = lastAtOrBefore(this.st, this.t);
    const b = lastAtOrBefore(this.bt, this.t);                 // the latest bar, if it is recent
    const scan = this.bt.length && this.t >= this.bt[b] && this.t - this.bt[b] <= 2 * d.width_bars.bin_s ? d.width_bars.w[b] : null;
    this.scene.setTime(this.t, this.nozzleAt(k));
    this.slider.value = this.t;
    this.slider.style.setProperty('--p', `${this.t / d.t_end * 100}%`);
    this.clockNow.textContent = clock(this.t);
    const set = (id, v, nd, unit) => {
      this.outs[id].replaceChildren(v === null || v === undefined ? '–' : v.toFixed(nd), unit && v !== null ? el('small', {}, unit) : '');
    };
    this.outs.time.textContent = clock(this.t);
    this.outs.layer.replaceChildren(started ? String(p.layer[k]) : '–', el('small', {}, ` of ${d.layers}`));
    set('z', started ? p.z[k] : null, 0, 'mm');
    set('torque', s.torque[i], 1, '%');
    set('P1', s.P1[i], 1, 'bar');
    set('P2', s.P2[i], 1, 'bar');
    set('T1', s.T1[i], 1, '°C');
    set('ws', s.ws[i], 3, 'kg/kg');
    set('width', scan, 1, 'mm');
    this.follow();
    this.paintMask();
  }

  // the head between two position updates, at the front of the bead
  nozzleAt(k) {
    const p = this.d.path, j = Math.min(k + 1, this.pt.length - 1);
    const f = j > k && !this.breaks.has(k) ? Math.max(0, Math.min(1, (this.t - this.pt[k]) / (this.pt[j] - this.pt[k]))) : 0;
    return [p.x[k] + f * (p.x[j] - p.x[k]), p.y[k] + f * (p.y[j] - p.y[k]), p.z[k] + f * (p.z[j] - p.z[k])];
  }

  toggle() { this.playing ? this.pause() : this.play(); }

  play() {
    if (!this.built || this.playing) return;
    if (this.t >= this.d.t_end) this.seek(0);
    this.playing = true;
    this.playBtn.innerHTML = PAUSE;
    this.playBtn.setAttribute('aria-label', 'Pause');
    const run = (this.run = (this.run || 0) + 1);   // a loop from an earlier play stops itself
    let last = performance.now();
    const step = (now) => {
      if (!this.playing || run !== this.run) return;
      this.seek(this.t + (now - last) / 1000 * this.speed);
      last = now;
      if (this.t >= this.d.t_end) { this.pause(); return; }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  pause() {
    this.playing = false;
    if (this.playBtn) { this.playBtn.innerHTML = PLAY; this.playBtn.setAttribute('aria-label', 'Play'); }
  }

  key(e) {
    if (!this.built || this.root.hidden) return;
    if (e.key === 'Escape') this.setFull(false);
    if (['SELECT', 'TEXTAREA'].includes(e.target.tagName)) return;
    if (e.target.tagName === 'INPUT' && e.target !== this.slider) return;
    if (e.code === 'Space') { e.preventDefault(); this.toggle(); }
    if (e.target === this.slider) return;
    if (e.key === 'ArrowRight') { this.pause(); this.seek(this.t + 10); }
    if (e.key === 'ArrowLeft') { this.pause(); this.seek(this.t - 10); }
    if (e.key === 'Home') { this.pause(); this.seek(0); }
    if (e.key === 'End') { this.pause(); this.seek(this.d.t_end); }
  }

  shown() {
    if (!this.built) return;
    Plotly.Plots.resize(this.plot);
    this.scene.resize();
    this.fitMask();
    this.startIfWanted();
  }

  // once per load, and only while the tab is on show
  startIfWanted() {
    if (!this.wantsPlay || this.root.hidden) return;
    this.wantsPlay = false;
    this.seek(0);
    this.play();
  }

  theme() {
    if (!this.built) return;
    const t = this.t, was = this.playing;
    this.pause();
    this.show(this.d);
    this.seek(t);
    if (was) this.play();
  }
}
