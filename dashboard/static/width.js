// Width control tab: the valve step test of 16 May 2025, the print behind the
// NDT-CE 2025 paper. Three walls at three water flows, then the arch printed the
// same day at a setting chosen between them.
import { PLOT_CONFIG, api, chartBase, el, token } from './util.js';

const DOI = 'https://doi.org/10.58286/31704';
const LAYER_MARK = { 1: 'circle', 2: 'diamond', 3: 'cross' };
const LAYER_DASH = { 1: 'solid', 2: 'dashdot', 3: 'dash' };
const f1 = (v) => (v === null || v === undefined ? '–' : v.toFixed(1));

export class WidthTab {
  constructor(root, openInReplay) {
    this.root = root;
    this.openInReplay = openInReplay;
    this.layer = 1;
    this.x = 1200;
    this.loaded = false;
  }

  flowColor(flow) { return token({ 8.4: 's1', 8: 's3', 7.6: 's2' }[flow] ?? 'ink-3'); }

  async open() {
    if (this.loaded) { this.resize(); return; }
    if (this.busy) return;
    this.busy = true;
    this.root.replaceChildren(el('div', { class: 'card notice' }, el('div', {}, el('div', { class: 'spinner' }),
      'Reading every scan profile of the step test. This takes about 30 s the first time.')));
    try {
      this.d = await api('./api/width-control.json');
      this.loaded = true;
      this.render();
    } catch (e) {
      this.root.replaceChildren(el('div', { class: 'card notice' }, e.message));
    }
    this.busy = false;
  }

  row(flow, layer) { return this.d.rows.find((r) => r.flow === flow && r.layer === layer); }

  meanOver(flow, key) {
    const v = [1, 2, 3].map((l) => this.row(flow, l)?.[key]).filter((x) => x !== null && x !== undefined);
    return v.reduce((a, b) => a + b, 0) / v.length;
  }

  render() {
    const d = this.d, flows = d.walls.map((w) => w.flow);
    const wide = this.meanOver(flows[0], 'width'), narrow = this.meanOver(flows[flows.length - 1], 'width');
    const mmPerPct = (wide - narrow) / (d.walls[0].valve - d.walls[d.walls.length - 1].valve);
    const card = (w) => el('article', { class: 'card setting' },
      el('h3', {}, el('i', { style: `background:${this.flowColor(w.flow)}` }), `${w.flow.toFixed(1)} L/min · valve ${w.valve} %`),
      el('div', { class: 'big' }, f1(this.meanOver(w.flow, 'width')), el('small', {}, 'mm')),
      el('dl', {},
        el('dt', {}, 'Torque'), el('dd', {}, `${f1(this.meanOver(w.flow, 'torque'))} %`),
        el('dt', {}, 'P2'), el('dd', {}, `${f1(this.meanOver(w.flow, 'P2'))} bar`),
        el('dt', {}, 'T0'), el('dd', {}, `${f1(this.meanOver(w.flow, 'T0'))} °C`)));
    const cards = d.walls.map(card);

    this.plots = {};
    const plot = (id, h) => { const n = el('div', { class: 'plot', style: `height:${h}px` }); this.plots[id] = n; return n; };
    const panel = (title, text, ...kids) => el('section', { class: 'card panel' }, el('h3', {}, title), text ? el('p', {}, text) : null, ...kids);

    this.seg = el('div', { class: 'seg', role: 'group', 'aria-label': 'Layer' }, ...Object.keys(d.along).map(Number).map((l) => el('button', {
      'aria-pressed': l === this.layer, onclick: () => { this.layer = l; this.syncLayer(); },
    }, `Layer ${l}`)));
    this.xOut = el('output', {}, `X ${this.x} mm`);
    this.xIn = el('input', {
      type: 'range', min: d.roi[0], max: d.roi[1], step: 10, value: this.x, 'aria-label': 'X position',
      style: `--p:${(this.x - d.roi[0]) / (d.roi[1] - d.roi[0]) * 100}%`,
      oninput: (e) => {
        this.x = +e.target.value;
        e.target.style.setProperty('--p', `${(this.x - d.roi[0]) / (d.roi[1] - d.roi[0]) * 100}%`);
        this.xOut.textContent = `X ${this.x} mm`;
        this.moveCursor();
        this.loadProfiles();
      },
    });

    const a = d.arch;
    this.root.replaceChildren(el('div', { class: 'width' },
      el('div', {},
        el('div', { class: 'page-head', style: 'margin-bottom:4px' },
          el('h2', {}, 'Width control'),
          el('p', {}, 'Water valve step test, 16 May 2025')),
        el('div', { class: 'card paper-card' },
          el('div', {},
            el('div', { class: 'paper-tag' }, 'Published at NDT-CE 2025'),
            el('div', { class: 'paper-title' }, 'Investigating the Impact of Material Rheology on Geometric Accuracy in 3D Concrete Printing Using Real-Time Monitoring'),
            el('div', { class: 'paper-authors' }, 'O. A. Jeyifous, E. Schönsee, C. Strangfeld, G. Hüsken · doi:10.58286/31704')),
          el('a', { class: 'btn primary', href: DOI, target: '_blank', rel: 'noopener' }, 'Read the paper')),
        el('p', { class: 'lede' }, `Three walls of seven layers, 10 mm high, at ${(d.feed_mm_min / 60).toFixed(0)} mm/s, each after 5 minutes of pumping into a container at the new setting. Less water gave a narrower bead with higher torque, pressure and temperature.`),
        el('div', { class: 'controls' },
          el('button', { class: 'btn', onclick: () => this.openInReplay() }, 'Open this print in the Print tab'))),
      el('div', { class: 'settings' }, ...cards,
        el('article', { class: 'card setting note' },
          el('div', {}, el('b', {}, `${mmPerPct.toFixed(1)} mm`), ' of width per % of valve, layers 1 to 3, X 600 to 1800 mm.'),
          el('div', {}, el('b', {}, `r = ${d.fit[2].r.toFixed(2)}`), ` between width and area in layer 2 (${d.fit[2].n} profiles); ${d.fit[3].r.toFixed(2)} in layer 3. The paper gives 0.98.`))),
      panel('Bead cross-section and width along the wall', 'The same X position on the three walls. Move X, or pick a layer. Width here is read 4 mm below the top of the bead (dashed line), which needs only the upper flank, so the upper layers have widths too; it runs about 0.9 mm below the layer-band width in the cards above. Layers 6 and 7 of the walls at 8.4 and 8.0 L/min were printed after the valve had moved.',
        el('div', { class: 'controls' }, this.seg, el('div', { class: 'x-range' }, this.xIn, this.xOut)),
        el('div', { class: 'two' }, plot('profile', 300), plot('along', 300))),
      el('div', { class: 'two' },
        panel('Print path', 'The three walls from above, with the section the paper averages over.', plot('plan', 300)),
        panel('Width against area, layer 2', 'From layer 3 up the scanner does not always see the full bead, but width still tracks area, so width can stand in for it.', plot('scatter', 300))),
      panel('Width and sensors by water flow', this.compareNote(), plot('compare', 300)),
      panel('The whole experiment', 'Torque (filtered), P0, P2 and T0 over the run. Shaded: the three walls. The valve moved to the next setting after layer 5 of a wall (dashed lines), and the sensors follow over the minutes of pumping before the next wall.', plot('series', 430)),
      a ? panel('Same day, same mortar: the arch at valve 41 %',
        `Layer 2 of the walls at 42 and 40 % averages ${a.w41_at_board_feed.toFixed(1)} mm at ${(a.board_feed / 1000).toFixed(1)} m/min. The arch ran at ${(a.feed_median / 1000).toFixed(2)} m/min, and the same volume at that speed gives ${a.predicted_median.toFixed(1)} mm. Measured: ${a.width_median.toFixed(1)} mm. A check added here, not in the paper.`, plot('arch', 260)) : null,
      el('div', { class: 'two' },
        panel('All walls and layers', 'X 600 to 1800 mm. Seen: profiles with both edges found. On the walls at 8.4 and 8.0 L/min, layers 6 and 7 were printed after the valve had moved.', this.dataTable()),
        panel('Flow table, paper Table 1', 'Mortar characterised to DIN EN 1015-3 at three water contents, separate from the print.', this.flowTable()))));
    this.draw();
    this.loadProfiles();
  }

  compareNote() {
    return 'Averaged over X 600 to 1800 mm, one line per layer.';
  }

  dataTable() {
    const heads = ['Flow (L/min)', 'Layer', 'Width (mm)', 'Seen', 'Torque (%)', 'P0 (bar)', 'P2 (bar)', 'T0 (°C)'];
    const rows = this.d.rows.map((r) => el('tr', {}, ...[r.flow.toFixed(1), r.layer, f1(r.width), `${r.n}/${r.profiles}`, f1(r.torque), f1(r.P0), f1(r.P2), f1(r.T0)]
      .map((v) => el('td', {}, String(v)))));
    return el('div', { class: 'tbl-wrap', style: 'max-height:320px;overflow-y:auto' }, el('table', { class: 'data' },
      el('thead', {}, el('tr', {}, ...heads.map((h) => el('th', {}, h)))), el('tbody', {}, ...rows)));
  }

  flowTable() {
    return el('div', { class: 'tbl-wrap' }, el('table', { class: 'data' },
      el('thead', {}, el('tr', {}, ...['Mix', 'Water (M.-%)', 'Density (g/cm³)', 'Spread (cm)'].map((h) => el('th', {}, h)))),
      el('tbody', {}, ...this.d.flow_table.map((r) => el('tr', {}, ...[r.mix, r.water.toFixed(1), r.density.toFixed(2), r.diameter.toFixed(2)].map((v) => el('td', {}, String(v))))))));
  }

  draw() {
    this.drawCompare();
    this.drawSeries();
    this.drawAlong();
    this.drawPlan();
    this.drawScatter();
    if (this.d.arch) this.drawArch();
  }

  drawCompare() {
    const d = this.d, { axis, layout } = chartBase();
    const flows = [...d.walls].map((w) => w.flow).sort((a, b) => a - b);
    const q = [['width', 'Width (mm)', 1], ['torque', 'Torque (%)', 1], ['P2', 'P2 (bar)', 2], ['T0', 'T0 (°C)', 1]];
    const ink = [token('ink'), token('ink-2'), token('ink-3')];
    const traces = [], ann = [];
    const lay = { ...layout, margin: { l: 40, r: 10, t: 46, b: 30 }, hovermode: 'closest', showlegend: true, legend: { ...layout.legend, y: 1.2 } };
    q.forEach(([key, name, nd], i) => {
      const n = i ? String(i + 1) : '';
      const lo = i * 0.255, hi = lo + 0.2;
      lay[`xaxis${n}`] = { ...axis, domain: [lo, hi], type: 'category', categoryorder: 'array', categoryarray: flows.map((f) => f.toFixed(1)), anchor: `y${n}`, fixedrange: true };
      lay[`yaxis${n}`] = { ...axis, anchor: `x${n}`, nticks: 5, fixedrange: true };
      ann.push({ text: name, xref: `x${n} domain`, yref: 'paper', x: 0, y: 1, xanchor: 'left', yanchor: 'bottom', showarrow: false, font: { size: 12, color: token('ink-2') } });
      for (const l of [1, 2, 3]) {
        const x = flows.map((f) => f.toFixed(1));
        traces.push({
          name: `Layer ${l}`, legendgroup: `l${l}`, showlegend: i === 0, x, y: flows.map((f) => this.row(f, l)?.[key]), xaxis: `x${n}`, yaxis: `y${n}`,
          mode: 'lines+markers', line: { color: ink[l - 1], width: 1.6, dash: LAYER_DASH[l] }, marker: { size: 8, symbol: LAYER_MARK[l], color: ink[l - 1] },
          hovertemplate: `Layer ${l}, %{x} L/min: %{y:.${nd}f}<extra>${name}</extra>`,
        });
      }
    });
    lay.annotations = [...ann, { text: 'water flow (L/min)', xref: 'paper', yref: 'paper', x: 0.5, y: 0, yshift: -30, showarrow: false, font: { size: 12, color: token('ink-2') } }];
    Plotly.newPlot(this.plots.compare, traces, lay, PLOT_CONFIG);
  }

  drawSeries() {
    const d = this.d, s = d.series, { axis, layout } = chartBase();
    const x = s.t.map((v) => v / 60);
    const rows = [['torque', 'torque (%)', 's1', 1], ['P0', 'P0 (bar)', 's2', 1], ['P2', 'P2 (bar)', 's3', 2], ['T0', 'T0 (°C)', 'ink-2', 1]];
    const h = 0.205, gap = 0.06;
    const lay = { ...layout, margin: { l: 44, r: 12, t: 20, b: 34 }, showlegend: false, hovermode: 'x unified', hoversubplots: 'axis', dragmode: 'zoom', annotations: [], shapes: [] };
    lay.xaxis = { ...axis, title: { text: 'time since the log started (min)', font: axis.title.font }, anchor: 'y4', hoverformat: '.1f' };
    // the stops at the start and between walls would flatten everything else
    const span = (a) => {
      const v = a.filter((y) => y !== null).sort((p, q) => p - q), at = (f) => v[Math.floor(f * (v.length - 1))];
      const lo = at(0.04), hi = at(0.998), pad = (hi - lo) * 0.12;
      return [lo - pad, hi + pad];
    };
    const traces = rows.map(([key, name, col, nd], i) => {
      const n = i ? String(i + 1) : '', top = 1 - i * (h + gap);
      lay[`yaxis${n}`] = { ...axis, domain: [top - h, top], nticks: 4, fixedrange: true, range: span(s[key]) };
      lay.annotations.push({ text: name, xref: 'paper', yref: 'paper', x: 0, y: top, xanchor: 'left', yanchor: 'bottom', showarrow: false, font: { size: 12, color: token('ink-2') } });
      return { name, x, y: s[key], yaxis: `y${n}`, mode: 'lines', line: { color: token(col), width: 1.3 }, hovertemplate: `%{y:.${nd}f}<extra>${name}</extra>` };
    });
    for (const w of d.walls) {
      lay.shapes.push({ type: 'rect', xref: 'x', yref: 'paper', x0: w.t0 / 60, x1: w.t1 / 60, y0: 0, y1: 1, fillcolor: this.flowColor(w.flow), opacity: 0.12, line: { width: 0 }, layer: 'below' });
      lay.annotations.push({ text: `${w.flow.toFixed(1)} L/min`, xref: 'x', yref: 'paper', x: (w.t0 + w.t1) / 120, y: 1, yshift: 10, showarrow: false, font: { size: 12, color: token('ink-2') } });
    }
    s.valve.forEach((v, i) => {
      const was = s.valve[i - 1];
      if (i && v !== null && was !== null && Math.abs(v - was) >= 1 && Number.isInteger(v)) {
        lay.shapes.push({ type: 'line', xref: 'x', yref: 'paper', x0: x[i], x1: x[i], y0: 0, y1: 1, line: { color: token('ink-3'), width: 1, dash: 'dash' } });
      }
    });
    Plotly.newPlot(this.plots.series, traces, lay, PLOT_CONFIG);
  }

  drawAlong() {
    const d = this.d, { axis, layout } = chartBase({ margin: { l: 44, r: 12, t: 30, b: 38 }, showlegend: true, hovermode: 'x unified' });
    const traces = d.along[this.layer].map((a) => ({
      name: `${a.flow.toFixed(1)} L/min`, x: a.x, y: a.width, mode: 'lines', line: { color: this.flowColor(a.flow), width: 1.8 },
      connectgaps: false, hovertemplate: '%{y:.1f} mm',
    }));
    const lay = {
      ...layout,
      xaxis: { ...axis, title: { text: 'X position (mm)', font: axis.title.font }, range: d.roi, hoverformat: '.0f' },
      yaxis: { ...axis, title: { text: `width, layer ${this.layer} (mm)`, font: axis.title.font } },
      shapes: [this.cursorShape()],
    };
    Plotly.react(this.plots.along, traces, lay, PLOT_CONFIG);
  }

  cursorShape() {
    return { type: 'line', xref: 'x', yref: 'paper', x0: this.x, x1: this.x, y0: 0, y1: 1, line: { color: token('ink'), width: 1, dash: 'dash' } };
  }

  moveCursor() { Plotly.relayout(this.plots.along, { shapes: [this.cursorShape()] }); }

  async loadProfiles() {
    const mine = (this.profileReq = (this.profileReq || 0) + 1);
    const q = await api(`./api/width-control/profiles/${this.layer}/${this.x}.json`).catch(() => null);
    if (!q || mine !== this.profileReq) return;
    const { axis, layout } = chartBase({ margin: { l: 44, r: 12, t: 30, b: 38 }, showlegend: true, hovermode: 'closest' });
    const traces = q.profiles.map((p) => ({
      name: `${p.flow.toFixed(1)} L/min, ${p.width.toFixed(1)} mm`, x: p.x, y: p.z, mode: 'lines', line: { color: this.flowColor(p.flow), width: 1.8 },
      hovertemplate: `x %{x:.1f} mm, z %{y:.1f} mm<extra>${p.flow.toFixed(1)} L/min at X ${Math.round(p.at_x)}</extra>`,
    }));
    Plotly.react(this.plots.profile, traces, {
      ...layout,
      xaxis: { ...axis, title: { text: 'across the bead, from its centre (mm)', font: axis.title.font }, range: [-26, 26], fixedrange: true },
      yaxis: { ...axis, title: { text: `below the bead top, layer ${this.layer} (mm)`, font: axis.title.font }, range: [-12, 2], fixedrange: true },
      shapes: [{ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: -4, y1: -4, line: { color: token('ink-3'), width: 1, dash: 'dash' } }],
    }, PLOT_CONFIG);
  }

  syncLayer() {
    [...this.seg.children].forEach((b, i) => b.setAttribute('aria-pressed', i + 1 === this.layer));
    this.drawAlong();
    this.loadProfiles();
  }

  drawPlan() {
    const d = this.d, { axis, layout } = chartBase({ margin: { l: 48, r: 12, t: 30, b: 38 }, showlegend: true, hovermode: false });
    const traces = d.walls.map((w) => {
      const keep = d.plan.flow.map((f) => f === w.flow);
      return {
        name: `${w.flow.toFixed(1)} L/min`, x: d.plan.x.filter((_, i) => keep[i]), y: d.plan.y.filter((_, i) => keep[i]),
        mode: 'markers', marker: { size: 3, color: this.flowColor(w.flow) }, hoverinfo: 'skip',
      };
    });
    Plotly.newPlot(this.plots.plan, traces, {
      ...layout,
      xaxis: { ...axis, title: { text: 'X (mm)', font: axis.title.font }, fixedrange: true },
      yaxis: { ...axis, title: { text: 'Y (mm)', font: axis.title.font }, scaleanchor: 'x', fixedrange: true },
      shapes: [{ type: 'rect', xref: 'x', yref: 'paper', x0: d.roi[0], x1: d.roi[1], y0: 0, y1: 1, fillcolor: token('ink-3'), opacity: 0.12, line: { width: 0 }, layer: 'below' }],
    }, PLOT_CONFIG);
  }

  drawScatter() {
    const d = this.d, s = d.scatter, { axis, layout } = chartBase({ margin: { l: 48, r: 12, t: 30, b: 38 }, showlegend: true, hovermode: 'closest' });
    const traces = d.walls.map((w) => {
      const keep = s.flow.map((f) => f === w.flow);
      return {
        name: `${w.flow.toFixed(1)} L/min`, x: s.width.filter((_, i) => keep[i]), y: s.area.filter((_, i) => keep[i]), mode: 'markers',
        marker: { size: 5, color: this.flowColor(w.flow), opacity: 0.55 }, hovertemplate: '%{x:.1f} mm, %{y:.0f} mm²<extra></extra>',
      };
    });
    Plotly.newPlot(this.plots.scatter, traces, {
      ...layout,
      xaxis: { ...axis, title: { text: 'width (mm)', font: axis.title.font } },
      yaxis: { ...axis, title: { text: 'area (mm²)', font: axis.title.font } },
    }, PLOT_CONFIG);
  }

  drawArch() {
    const a = this.d.arch, { axis, layout } = chartBase({ margin: { l: 44, r: 12, t: 30, b: 38 }, showlegend: true, hovermode: 'x unified' });
    Plotly.newPlot(this.plots.arch, [
      { name: 'Measured, median per layer', x: a.layer, y: a.width, mode: 'lines+markers', line: { color: token('s1'), width: 1.6 }, marker: { size: 6, color: token('s1') }, hovertemplate: '%{y:.1f} mm' },
      { name: 'From the step test, scaled by feed', x: a.layer, y: a.predicted, mode: 'lines', line: { color: token('ink-2'), width: 1.6, dash: 'dash' }, hovertemplate: '%{y:.1f} mm' },
    ], {
      ...layout,
      xaxis: { ...axis, title: { text: 'layer', font: axis.title.font }, dtick: 2, hoverformat: '.0f' },
      yaxis: { ...axis, title: { text: 'width (mm)', font: axis.title.font } },
    }, PLOT_CONFIG);
  }

  resize() { for (const p of Object.values(this.plots ?? {})) if (p.data) Plotly.Plots.resize(p); }

  theme() { if (this.loaded) this.render(); }
}
