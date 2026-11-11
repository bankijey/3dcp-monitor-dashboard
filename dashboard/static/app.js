// Print dashboard shell: the tabs, the theme, the print selector that replaces the
// sidebar when the catalogue is small, the footer, and the sidebar's pick handed on
// to the tabs.
import { Sidebar } from './sidebar.js';
import { ReplayTab } from './replay.js';
import { WidthTab } from './width.js';
import { api, clearTokens, el, store } from './util.js';

const $ = (id) => document.getElementById(id);
const COMPACT_MAX = 6;                 // up to this many prints: a selector in the top bar, no sidebar
const TABS = ['replay', 'width'];

// the step test page is about one print: the selector shows that one and locks the rest
let lockedPrint = null;

function syncSelector(sidebar) {
  const seg = $('prints');
  if (seg.hidden) return;
  const cur = sidebar.current ? sidebar.specimens[sidebar.current] : null;
  const curId = cur && cur.length === 1 && cur[0][0] === 'corpus' ? cur[0][1] : null;
  for (const b of seg.children) {
    b.setAttribute('aria-pressed', b.dataset.id === (lockedPrint ?? curId));
    b.disabled = lockedPrint !== null;
  }
}

// a small catalogue: every print is a specimen, picked from the top bar
function compact(sidebar, site) {
  $('app').classList.add('compact');
  const label = (r) => site.prints?.[r.id]?.title ?? sidebar.label([r]);
  for (const r of sidebar.rows) sidebar.addIds([label(r), r.id]);
  const seg = $('prints');
  seg.replaceChildren(...sidebar.rows.map((r) => {             // the badge is the print's mortar
    const mortar = site.prints?.[r.id]?.mortar ?? '';
    return el('button', {
      'data-id': r.id, 'data-mortar': mortar, title: mortar ? `Mortar ${mortar}` : 'Mortar not recorded',
      onclick: () => sidebar.add(label(r), [['corpus', r.id]]),
    }, el('i', {}, mortar || '?'), label(r));
  }));
  seg.hidden = false;
  if (!sidebar.current || !sidebar.specimens[sidebar.current]) sidebar.pick(Object.keys(sidebar.specimens)[0]);
  else syncSelector(sidebar);
}

function footer(site) {
  const foot = $('foot');
  const doi = (label, v) => (v ? el('span', {}, `${label} `,
    el('a', { href: `https://doi.org/${v}`, target: '_blank', rel: 'noopener' }, `doi:${v}`)) : null);
  const notes = site.notes ?? [];
  foot.replaceChildren(el('div', { class: 'foot-row' },
    site.credit ? el('span', { class: 'foot-credit' }, site.credit) : null,
    doi('Data', site.data_doi), doi('Code', site.code_doi),
    notes.length ? el('details', {}, el('summary', {}, 'How the numbers are made'),
      el('div', { class: 'foot-notes' }, ...notes.map((n) => el('p', {}, n)))) : null));
  foot.hidden = !site.credit && !site.data_doi && !site.code_doi && !notes.length;
}

async function start() {
  const replay = new ReplayTab($('view-replay'));
  const width = new WidthTab($('view-width'), () => {
    sidebar.add('Step test', [['corpus', sidebar.control.board]]);
    show('replay');
  });
  const sidebar = new Sidebar($('sidebar'), (name, keys) => {
    $('app').classList.remove('open');
    replay.load(name, keys);
    syncSelector(sidebar);
  });

  function show(tab) {
    for (const t of TABS) {
      $(`view-${t}`).hidden = t !== tab;
      $(`tab-${t}`).setAttribute('aria-selected', t === tab);
    }
    store.set('tab', tab);
    lockedPrint = tab === 'width' ? sidebar.control?.board ?? null : null;
    syncSelector(sidebar);
    if (tab !== 'replay') replay.pause();
    if (tab === 'replay') replay.shown();
    if (tab === 'width') width.open();
  }

  for (const t of TABS) $(`tab-${t}`).onclick = () => show(t);
  $('menu').onclick = () => $('app').classList.toggle('open');
  $('scrim').onclick = () => $('app').classList.remove('open');
  $('theme').onclick = () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
    root.dataset.theme = dark ? 'light' : 'dark';
    try { localStorage.setItem('theme', root.dataset.theme); } catch (e) { /* private window */ }
    clearTokens();
    replay.theme();
    width.theme();
  };

  let site = {};
  try {
    site = await api('./api/site.json').catch(() => ({}));
    await sidebar.init(site.default_print);
  } catch (e) {
    $('view-replay').replaceChildren(el('div', { class: 'card notice' }, `No corpus: ${e.message}`));
  }
  $('tab-width').hidden = site.width_control === false;
  footer(site);
  if (sidebar.rows.length && sidebar.rows.length <= COMPACT_MAX) compact(sidebar, site);
  const saved = store.get('tab', 'replay');   // it may name a tab this build does not have
  show(TABS.includes(saved) && !$(`tab-${saved}`).hidden ? saved : 'replay');
}

window.addEventListener('DOMContentLoaded', start);
