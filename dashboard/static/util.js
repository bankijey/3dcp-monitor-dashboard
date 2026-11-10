// Small helpers shared by the sidebar and the tabs.

export async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `The server answered ${r.status}.`);
  }
  return r.json();
}

export const post = (path, body) => api(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

export const store = {
  get(k, fallback) {
    try { const v = localStorage.getItem(k); return v === null ? fallback : JSON.parse(v); }
    catch (e) { return fallback; }
  },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private window */ } },
};

export function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (v === true) n.setAttribute(k, '');
    else if (v !== false && v !== null && v !== undefined) n.setAttribute(k, v);
  }
  n.append(...kids.filter((c) => c !== null && c !== undefined));
  return n;
}

// design tokens, read once per theme: the replay asks for them every frame
const tokens = new Map();
export function token(name) {
  if (!tokens.has(name)) {
    tokens.set(name, getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim());
  }
  return tokens.get(name);
}
export const clearTokens = () => tokens.clear();

export function clock(s) {
  s = Math.max(0, Math.floor(s));
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

// index of the last value <= x in a sorted array (0 when x is before the first)
export function lastAtOrBefore(a, x) {
  let lo = 0, hi = a.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (a[mid] <= x) lo = mid; else hi = mid - 1;
  }
  return lo;
}

export const VIRIDIS = ['#440154', '#482878', '#3e4989', '#31688e', '#26828e',
  '#1f9e89', '#35b779', '#6ece58', '#b5de2b', '#fde725'];

// one colour per specimen, in the order of the sidebar's list, on every page
export const SERIES = ['#2a78d6', '#eb6834', '#1baf7a', '#4a3aa7', '#d03b3b', '#c79a00',
  '#c2408a', '#0f9fb8', '#6b4f2a', '#008300', '#7a7a7a', '#8a6fd1'];

// what every chart shares: fonts, grid and surface from the tokens
export function chartBase(extra = {}) {
  const ink2 = token('ink-2'), ink3 = token('ink-3'), line = token('line');
  const axis = {
    gridcolor: line, linecolor: line, zeroline: false, tickfont: { size: 11, color: ink3 },
    title: { font: { size: 12, color: ink2 } }, automargin: true,
  };
  return {
    axis,
    layout: {
      paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
      font: { family: token('font'), size: 12, color: ink2 },
      margin: { l: 44, r: 12, t: 12, b: 34 },
      hoverlabel: { bgcolor: token('surface'), bordercolor: token('line-2'), font: { color: token('ink'), size: 12 } },
      legend: { orientation: 'h', x: 0, y: 1.02, yanchor: 'bottom', font: { size: 12, color: ink2 } },
      ...extra,
    },
  };
}

export const PLOT_CONFIG = { displayModeBar: false, responsive: true };
