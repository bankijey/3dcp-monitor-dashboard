// The sidebar, as in the arch dashboard: pick a recorded day on the calendar,
// tick experiments, add them as specimens (separately or chained into one),
// upload pump CSVs, keep a list of specimens. The picked specimen goes to Replay.
import { SERIES, api, el, store } from './util.js';

const WEEK = ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'];
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
  'September', 'October', 'November', 'December'];
const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const CROSS = '<svg viewBox="0 0 12 12"><path d="M2.5 2.5l7 7M9.5 2.5l-7 7"/></svg>';
const MARK = { print: '✓', unsure: '?', 'not a print': '–' };

const iso = (y, m, d) => `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
const shortDay = (day) => { const [y, m, d] = day.split('-'); return `${d}.${m}.${y.slice(2)}`; };

export class Sidebar {
  constructor(root, onPick) {
    this.root = root;
    this.onPick = onPick;
    this.rows = [];
    this.showNon = false;
    this.ticked = new Set();
    this.era = 'Auto';
    this.specimens = store.get('specimens', null);   // name -> run keys
    this.current = store.get('current', null);
  }

  async init(openOn = null) {
    const cat = await api('./api/catalogue.json');
    this.rows = cat.rows;
    this.control = cat.control;
    const firstVisit = !this.specimens;
    if (firstVisit) {
      this.specimens = {};
      for (const r of this.rows) this.addIds([this.label([r]), r.id]);      // the published prints, all listed
    }
    // uploads do not survive a restart of the server, corpus runs may have gone
    const known = new Set(this.rows.map((r) => r.id));
    for (const [n, keys] of Object.entries(this.specimens)) {
      if (keys.some((k) => k[0] === 'corpus' && !known.has(k[1]))) delete this.specimens[n];
    }
    const days = this.days();
    this.day = days[days.length - 1];
    this.month = this.day.slice(0, 7);
    // the print the data folder names as the one to open on, over the last one picked
    const opening = Object.keys(this.specimens).find((n) => this.specimens[n].length === 1 && this.specimens[n][0][1] === openOn);
    if (opening) this.current = opening;
    if (!this.specimens[this.current]) {
      this.current = Object.keys(this.specimens)[0] ?? null;
    }
    this.render();
    if (this.current) this.pick(this.current);
  }

  visible() {
    return this.showNon ? this.rows : this.rows.filter((r) => r.kind !== 'not a print');
  }

  days() { return [...new Set(this.visible().map((r) => r.day))].sort(); }

  months() {
    const count = {};
    for (const r of this.visible()) { const m = r.day.slice(0, 7); count[m] = (count[m] || 0) + 1; }
    return count;
  }

  // ---------------------------------------------------------------- specimens
  add(name, keys, pick = true) {
    const same = Object.entries(this.specimens)
      .find(([, k]) => JSON.stringify(k) === JSON.stringify(keys));
    if (same) name = same[0];
    else {
      const base = name;
      for (let i = 2; this.specimens[name]; i++) name = `${base} (${i})`;
      this.specimens[name] = keys;
    }
    this.save();
    if (pick) this.pick(name);
    return name;
  }

  addIds([name, id]) { this.add(name, [['corpus', id]], false); }

  label(rows) {
    const boards = [...new Set(rows.map((r) => r.board))].join('/');
    const runs = rows.map((r) => (r.run === null ? '?' : r.run)).join('+');
    return `${boards} r${runs} · ${shortDay(rows[0].day)}`;
  }

  save() {
    const keep = Object.fromEntries(Object.entries(this.specimens)
      .filter(([, k]) => k.every((x) => x[0] === 'corpus')));
    store.set('specimens', keep);
    store.set('current', this.current);
  }

  pick(name) {
    this.current = name;
    this.save();
    this.renderSpecimens();
    if (name) this.onPick(name, this.specimens[name]);
  }

  remove(name) {
    delete this.specimens[name];
    if (this.current === name) this.current = Object.keys(this.specimens)[0] ?? null;
    this.save();
    this.renderSpecimens();
    this.onPick(this.current, this.current ? this.specimens[this.current] : null);
  }

  async upload(files) {
    for (const file of files) {
      const form = new FormData();
      form.append('file', file);
      form.append('era', this.era);
      try {
        const r = await api('./api/upload', { method: 'POST', body: form });
        this.add(r.name, [r.key]);
      } catch (e) { this.status.textContent = `${file.name}: ${e.message}`; }
    }
  }

  // ---------------------------------------------------------------- render
  render() {
    this.root.replaceChildren(
      el('div', { class: 'brand' },
        Object.assign(el('div', { class: 'brand-mark' }), {
          innerHTML: '<svg viewBox="0 0 24 24"><path d="M4 19h16M6 15h12M8 11h8M10 7h4"/></svg>',
        }),
        el('div', {}, el('h1', {}, '3DCP Print Dashboard'),
          el('p', {}, 'Replay any print, and the valve step test.'))),
      this.loadBlock(),
      this.specimenBlock());
    this.renderCalendar();
    this.renderSpecimens();
  }

  loadBlock() {
    const toggle = el('input', {
      type: 'checkbox', 'aria-label': 'Show non-prints',
      onchange: (e) => {
        this.showNon = e.target.checked;
        const days = this.days();
        if (!days.includes(this.day)) { this.day = days[days.length - 1]; this.month = this.day.slice(0, 7); }
        this.renderCalendar();
      },
    });
    this.calendar = el('div');
    this.status = el('p', { class: 'hint', role: 'status' });
    const input = el('input', {
      type: 'file', accept: '.csv', multiple: true,
      onchange: (e) => { this.upload([...e.target.files]); e.target.value = ''; },
    });
    const drop = el('label', {
      class: 'drop',
      ondragover: (e) => { e.preventDefault(); drop.classList.add('over'); },
      ondragleave: () => drop.classList.remove('over'),
      ondrop: (e) => { e.preventDefault(); drop.classList.remove('over'); this.upload([...e.dataTransfer.files]); },
    }, input, 'Drop pump CSVs here, or browse');
    const radios = el('div', { class: 'radios' }, 'Calibration',
      ...['Auto', 'New era', 'Old era'].map((era) => el('label', {},
        el('input', {
          type: 'radio', name: 'era', checked: era === this.era,
          onchange: () => { this.era = era; },
        }), era)));
    return el('section', {},
      el('div', { class: 'side-title' }, 'LOAD EXPERIMENTS',
        el('label', {
          class: 'switch',
          title: 'Experiments whose signals show no real print: dry runs and levelling, pump or flush tests, logs under a minute. Unsure ones are always shown (?).',
        }, 'Non-prints', toggle)),
      this.calendar, drop, radios, this.status);
  }

  renderCalendar() {
    const count = {};
    for (const r of this.visible()) count[r.day] = (count[r.day] || 0) + 1;
    const months = this.months();
    const keys = Object.keys(months).sort();
    if (!months[this.month]) this.month = keys[keys.length - 1];
    const i = keys.indexOf(this.month);
    const go = (m) => {
      this.month = m;
      if (this.day.slice(0, 7) !== m) this.day = this.days().find((d) => d.startsWith(m));
      this.ticked.clear();
      this.renderCalendar();
    };
    const nav = el('div', { class: 'month-nav' },
      el('button', { 'aria-label': 'Previous month with experiments', disabled: i === 0, onclick: () => go(keys[i - 1]) }, '‹'),
      el('select', { 'aria-label': 'Month', onchange: (e) => go(e.target.value) },
        ...keys.map((m) => el('option', { value: m, selected: m === this.month },
          `${MONTHS[+m.slice(5) - 1]} ${m.slice(0, 4)} · ${months[m]} exp.`))),
      el('button', { 'aria-label': 'Next month with experiments', disabled: i === keys.length - 1, onclick: () => go(keys[i + 1]) }, '›'));
    const [y, m] = this.month.split('-').map(Number);
    const grid = el('div', { class: 'cal' }, ...WEEK.map((d) => el('div', { class: 'cal-head' }, d)));
    const lead = (new Date(y, m - 1, 1).getDay() + 6) % 7;
    for (let k = 0; k < lead; k++) grid.append(el('div'));
    const last = new Date(y, m, 0).getDate();
    for (let d = 1; d <= last; d++) {
      const day = iso(y, m, d), n = count[day] || 0;
      grid.append(el('button', {
        class: `${n ? 'has' : ''} ${day === this.day ? 'on' : ''}`,
        disabled: !n,
        title: n ? `${d}.${m}.${y}: ${n} experiment${n > 1 ? 's' : ''}` : null,
        onclick: () => { this.day = day; this.ticked.clear(); this.renderCalendar(); },
      }, String(d)));
    }
    this.calendar.replaceChildren(nav, grid, ...this.dayBlock());
  }

  dayBlock() {
    const rows = this.visible().filter((r) => r.day === this.day);
    const [y, m, d] = this.day.split('-').map(Number);
    const name = WEEKDAYS[new Date(y, m - 1, d).getDay()];
    const hint = el('p', { class: 'hint' }, el('b', {}, `${name} ${d}.${m}.${y}`),
      ` · recorded day, from the timestamps. Blue days have ${this.showNon ? 'experiments' : 'prints'}.`);
    const body = rows.map((r) => {
      const box = el('input', {
        type: 'checkbox', checked: this.ticked.has(r.id), 'aria-label': `Add ${r.board} run ${r.run}`,
        onclick: (e) => e.stopPropagation(),
        onchange: (e) => { e.target.checked ? this.ticked.add(r.id) : this.ticked.delete(r.id); sync(); },
      });
      return el('tr', {
        'data-id': r.id,
        onclick: () => box.click(),
      }, el('td', {}, box),
      el('td', { class: 'name', title: r.id }, r.run === null ? r.board : `${r.board} r${r.run}`),
      el('td', {}, r.start), el('td', { class: 'num' }, r.min === null ? '–' : r.min.toFixed(1)),
      el('td', { class: 'mid' }, MARK[r.kind] ?? '?'),
      el('td', { class: 'mid' }, r.pump ? '✓' : '–'), el('td', { class: 'mid' }, r.scan ? '✓' : '–'));
    });
    const table = el('table', { class: 'exp-table' },
      el('thead', {}, el('tr', {}, ...['', 'experiment', 'start', 'min', 'print', 'pump', 'scan']
        .map((h, i) => el('th', { class: i > 3 ? 'mid' : i === 3 ? 'num' : null }, h)))),
      el('tbody', {}, ...body));
    const picked = () => rows.filter((r) => this.ticked.has(r.id));
    const sep = el('button', {
      class: 'btn primary',
      onclick: () => { let name; for (const r of picked()) name = this.add(this.label([r]), [['corpus', r.id]], false); this.ticked.clear(); this.renderCalendar(); if (name) this.pick(name); },
    }, 'Add separately');
    const comb = el('button', {
      class: 'btn', title: 'Chain the ticked runs in time order as one specimen',
      onclick: () => { const p = picked(); this.add(this.label(p), p.map((r) => ['corpus', r.id])); this.ticked.clear(); this.renderCalendar(); },
    }, 'Add combined');
    const sync = () => { sep.disabled = this.ticked.size < 1; comb.disabled = this.ticked.size < 2; };
    sync();
    const renamed = rows.filter((r) => r.renamed);
    return [hint, table,
      renamed.length ? el('p', { class: 'hint' }, `${renamed.length} of these files are named ${renamed[0].named} (logger session date).`) : null,
      el('div', { class: 'btn-row' }, sep, comb)].filter(Boolean);
  }

  specimenBlock() {
    this.list = el('div', { class: 'spec-list' });
    return el('section', {},
      el('div', { class: 'side-title' }, 'SPECIMENS'),
      el('div', { class: 'btn-row', style: 'margin-top:0' },
        el('button', {
          class: 'btn',
          onclick: () => { this.specimens = {}; this.current = null; this.save(); this.renderSpecimens(); this.onPick(null, null); },
        }, 'Clear list')),
      this.list);
  }

  renderSpecimens() {
    if (!this.list) return;
    const names = Object.keys(this.specimens);
    this.list.replaceChildren(...names.map((n, i) => {
      const x = el('button', { 'aria-label': `Remove ${n}`, onclick: (e) => { e.stopPropagation(); this.remove(n); } });
      x.innerHTML = CROSS;
      return el('div', {
        class: `spec ${n === this.current ? 'on' : ''}`, role: 'button', tabindex: '0',
        onclick: () => this.pick(n),
        onkeydown: (e) => { if (e.key === 'Enter') this.pick(n); },
      }, el('i', { style: `background:${SERIES[i % SERIES.length]}` }), el('span', { title: n }, n), x);
    }));
    if (!names.length) this.list.append(el('p', { class: 'hint' }, 'Pick a day and add experiments.'));
  }
}
