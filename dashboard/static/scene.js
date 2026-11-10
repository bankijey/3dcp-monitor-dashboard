// The print in 3D. WebGL2 without a library: the bead is a cross-section swept
// along the logged path at its measured width, coloured by that width. Every
// vertex carries its print time and the shader drops what lies after the current
// time, so playing and scrubbing cost one uniform and no new geometry.
import { VIRIDIS, token } from './util.js';

const RING = 8;                       // vertices around the bead
const FOV = 30 * Math.PI / 180;
const VIEW = { az: -74, el: 32 };     // the replay video's opening camera
const AZ_DRIFT = 12;                  // degrees the camera turns over one replay
const LIFT = 0.07;                    // print sits this far above centre, clear of the key

const BEAD_VS = `#version 300 es
layout(location=0) in vec3 a_pos;
layout(location=1) in vec3 a_nrm;
layout(location=2) in vec3 a_wtv;
uniform mat4 u_vp;
out vec3 v_n;
out vec3 v_wtv;
void main() { v_n = a_nrm; v_wtv = a_wtv; gl_Position = u_vp * vec4(a_pos, 1.0); }`;

const BEAD_FS = `#version 300 es
precision highp float;
in vec3 v_n;
in vec3 v_wtv;
uniform float u_t;
uniform vec2 u_range;
uniform sampler2D u_lut;
uniform vec3 u_grey;
out vec4 o;
void main() {
  if (v_wtv.y > u_t) discard;
  float f = clamp((v_wtv.x - u_range.x) / (u_range.y - u_range.x), 0.0, 1.0);
  vec3 c = v_wtv.z > 0.5 ? texture(u_lut, vec2(f, 0.5)).rgb : u_grey;
  vec3 n = normalize(gl_FrontFacing ? v_n : -v_n);
  float sun = max(dot(n, normalize(vec3(0.35, -0.55, 0.76))), 0.0);
  float sky = 0.5 + 0.5 * n.z;
  o = vec4(c * (0.5 + 0.28 * sky + 0.42 * sun), 1.0);
}`;

const DOT_VS = `#version 300 es
layout(location=0) in vec4 a_pt;
uniform mat4 u_vp;
uniform float u_size;
out float v_t;
void main() { v_t = a_pt.w; gl_Position = u_vp * vec4(a_pt.xyz, 1.0); gl_PointSize = u_size / gl_Position.w; }`;

const DOT_FS = `#version 300 es
precision highp float;
in float v_t;
uniform float u_t;
uniform vec4 u_color;
out vec4 o;
void main() {
  if (v_t > u_t) discard;
  vec2 d = gl_PointCoord - 0.5;
  if (dot(d, d) > 0.25) discard;
  o = u_color;
}`;

const LINE_VS = `#version 300 es
layout(location=0) in vec3 a_pos;
uniform mat4 u_vp;
void main() { gl_Position = u_vp * vec4(a_pos, 1.0); }`;

const LINE_FS = `#version 300 es
precision highp float;
uniform vec4 u_color;
out vec4 o;
void main() { o = u_color; }`;

const rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);

function lut() {
  const out = new Uint8Array(256 * 4);
  const stops = VIRIDIS.map(rgb);
  for (let i = 0; i < 256; i++) {
    const f = i / 255 * (stops.length - 1), k = Math.min(Math.floor(f), stops.length - 2), u = f - k;
    for (let c = 0; c < 3; c++) out[i * 4 + c] = 255 * (stops[k][c] * (1 - u) + stops[k + 1][c] * u);
    out[i * 4 + 3] = 255;
  }
  return out;
}

const norm = (v) => { const l = Math.hypot(...v) || 1; return v.map((x) => x / l); };
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];

function perspective(fov, aspect, near, far) {
  const f = 1 / Math.tan(fov / 2), nf = 1 / (near - far);
  return [f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) * nf, -1, 0, 0, 2 * far * near * nf, 0];
}

function lookAt(eye, at, up) {
  const z = norm(eye.map((v, i) => v - at[i])), x = norm(cross(up, z)), y = cross(z, x);
  return [x[0], y[0], z[0], 0, x[1], y[1], z[1], 0, x[2], y[2], z[2], 0,
    -dot(x, eye), -dot(y, eye), -dot(z, eye), 1];
}

function multiply(a, b) {
  const o = new Float32Array(16);
  for (let c = 0; c < 4; c++) {
    for (let r = 0; r < 4; r++) {
      let s = 0;
      for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k];
      o[c * 4 + r] = s;
    }
  }
  return o;
}

function niceStep(span, target = 7) {
  const raw = span / target, p = 10 ** Math.floor(Math.log10(raw)), f = raw / p;
  return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * p;
}

export class Scene {
  constructor(host) {
    this.host = host;
    this.canvas = document.createElement('canvas');
    this.labels = document.createElement('canvas');
    this.labels.className = 'labels';
    host.prepend(this.canvas, this.labels);
    this.ctx = this.labels.getContext('2d');
    const gl = this.gl = this.canvas.getContext('webgl2', { antialias: true, alpha: true, preserveDrawingBuffer: true });
    this.ok = !!gl;
    this.t = Infinity;
    this.showBands = true;
    this.follow = true;                // the camera drifts with the print until you take it
    this.nozzle = null;
    this.tEnd = 1;
    this.resetView();
    if (!gl) return;
    this.bead = this.program(BEAD_VS, BEAD_FS);
    this.dots = this.program(DOT_VS, DOT_FS);
    this.lines = this.program(LINE_VS, LINE_FS);
    this.lut = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.lut);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, lut());
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    this.controls();
    new ResizeObserver(() => this.resize()).observe(host);
    this.resize();
  }

  program(vs, fs) {
    const gl = this.gl, p = gl.createProgram();
    for (const [type, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]]) {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      gl.attachShader(p, s);
    }
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    const u = {};
    for (let i = 0; i < gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS); i++) {
      const { name } = gl.getActiveUniform(p, i);
      u[name] = gl.getUniformLocation(p, name);
    }
    return { p, u };
  }

  resetView() {
    this.az = VIEW.az; this.el = VIEW.el; this.zoom = 1; this.pan = [0, 0, 0];
  }

  // ---------------------------------------------------------------- geometry
  setPrint(d, keepView = false) {
    if (!this.ok) return;
    const gl = this.gl, { path } = d, n = path.t.length;
    const finite = (a) => a.filter((v) => v !== null);
    const bounds = (a) => { let lo = Infinity, hi = -Infinity; for (const v of finite(a)) { if (v < lo) lo = v; if (v > hi) hi = v; } return [lo, hi]; };
    const [x0, x1] = bounds(path.x), [y0, y1] = bounds(path.y), [z0, z1] = bounds(path.z);
    const h = d.layer_h, floor = z0 - h;
    this.origin = [(x0 + x1) / 2, (y0 + y1) / 2, floor];
    this.box = { x0, x1, y0, y1, z0: floor, z1: z1 + h / 2 };
    this.radius = Math.hypot(x1 - x0, y1 - y0, z1 - floor) / 2 + 2 * h;
    this.tEnd = d.t_end;
    this.range = d.range[0] === null ? [20, 40] : d.range;

    const widths = finite(path.w).sort((a, b) => a - b);
    const mid = widths.length ? widths[widths.length >> 1] : 30;
    // a point without a width takes its neighbour's value, flagged grey, so the
    // colour does not sweep through the scale over the last segment before it
    const w = path.w.slice();
    for (let i = 1; i < n; i++) if (w[i] === null) w[i] = w[i - 1];
    for (let i = n - 2; i >= 0; i--) if (w[i] === null) w[i] = w[i + 1];

    const pos = new Float32Array(n * RING * 3), nrm = new Float32Array(n * RING * 3), wtv = new Float32Array(n * RING * 3);
    const cuts = new Set(path.breaks), idx = [];
    const o = this.origin;
    let tx = 1, ty = 0;
    for (let i = 0; i < n; i++) {
      const first = i === 0 || cuts.has(i - 1), last = i === n - 1 || cuts.has(i);
      const a = first ? i : i - 1, b = last ? i : i + 1;
      const dx = path.x[b] - path.x[a], dy = path.y[b] - path.y[a], l = Math.hypot(dx, dy);
      if (l > 1e-6) { tx = dx / l; ty = dy / l; }
      const half = (w[i] ?? mid) / 2, valid = path.w[i] === null ? 0 : 1;
      const cx = path.x[i] - o[0], cy = path.y[i] - o[1], cz = path.z[i] - o[2] - h / 2;
      for (let k = 0; k < RING; k++) {
        const th = (k + 0.5) / RING * 2 * Math.PI, c = Math.cos(th), s = Math.sin(th);
        const u = Math.sign(c) * Math.abs(c) ** 0.55, v = Math.sign(s) * Math.abs(s) ** 0.55;
        const j = (i * RING + k) * 3;
        pos[j] = cx - ty * half * u; pos[j + 1] = cy + tx * half * u; pos[j + 2] = cz + h / 2 * v;
        nrm[j] = -ty * c; nrm[j + 1] = tx * c; nrm[j + 2] = s;
        wtv[j] = w[i] ?? mid; wtv[j + 1] = path.t[i]; wtv[j + 2] = valid;
      }
      if (!last) {
        for (let k = 0; k < RING; k++) {
          const p = i * RING + k, q = i * RING + (k + 1) % RING, r = p + RING, t = q + RING;
          idx.push(p, q, r, q, t, r);
        }
      }
    }
    if (this.vao) gl.deleteVertexArray(this.vao);
    this.vao = gl.createVertexArray();
    gl.bindVertexArray(this.vao);
    [pos, nrm, wtv].forEach((a, loc) => {
      gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
      gl.bufferData(gl.ARRAY_BUFFER, a, gl.STATIC_DRAW);
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 3, gl.FLOAT, false, 0, 0);
    });
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint32Array(idx), gl.STATIC_DRAW);
    this.count = idx.length;

    const u = d.usable, m = d.bands ? u.t.length : 0, pts = new Float32Array(m * 4);
    for (let i = 0; i < m; i++) {
      pts.set([u.x[i] - o[0], u.y[i] - o[1], u.z[i] - o[2], u.t[i]], i * 4);
    }
    if (this.dotVao) gl.deleteVertexArray(this.dotVao);
    this.dotVao = gl.createVertexArray();
    gl.bindVertexArray(this.dotVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, pts, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 4, gl.FLOAT, false, 0, 0);
    this.dotCount = m;
    gl.bindVertexArray(null);
    this.buildGrid();
    if (!keepView) { this.resetView(); this.follow = true; }
    this.dirty();
  }

  buildGrid() {
    const gl = this.gl, b = this.box, o = this.origin, pad = 40;
    const step = niceStep(Math.max(b.x1 - b.x0, b.y1 - b.y0));
    const gx0 = Math.floor((b.x0 - pad) / step) * step, gx1 = Math.ceil((b.x1 + pad) / step) * step;
    const gy0 = Math.floor((b.y0 - pad) / step) * step, gy1 = Math.ceil((b.y1 + pad) / step) * step;
    const v = [];
    for (let x = gx0; x <= gx1 + 1e-6; x += step) v.push(x - o[0], gy0 - o[1], 0, x - o[0], gy1 - o[1], 0);
    for (let y = gy0; y <= gy1 + 1e-6; y += step) v.push(gx0 - o[0], y - o[1], 0, gx1 - o[0], y - o[1], 0);
    this.grid = { gx0, gx1, gy0, gy1, step };
    if (this.gridVao) gl.deleteVertexArray(this.gridVao);
    this.gridVao = gl.createVertexArray();
    gl.bindVertexArray(this.gridVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(v), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
    this.gridCount = v.length / 3;
  }

  // ---------------------------------------------------------------- view
  camera() {
    const az = (this.az + (this.follow ? AZ_DRIFT * Math.min(this.t, this.tEnd) / this.tEnd : 0)) * Math.PI / 180;
    const el = this.el * Math.PI / 180, aspect = this.w / this.h;
    const fit = this.radius / Math.sin(FOV / 2) * Math.max(1, 1.2 / aspect) * 0.92;
    const dist = fit / this.zoom;
    const b = this.box, mid = (b.z1 - b.z0) / 2;
    const at = [this.pan[0], this.pan[1], mid + this.pan[2]];
    const eye = [at[0] + dist * Math.cos(el) * Math.cos(az), at[1] + dist * Math.cos(el) * Math.sin(az), at[2] + dist * Math.sin(el)];
    const view = lookAt(eye, at, [0, 0, 1]);
    this.vp = multiply(perspective(FOV, aspect, Math.max(1, dist - 3 * this.radius), dist + 4 * this.radius), view);
    for (let c = 0; c < 4; c++) this.vp[c * 4 + 1] += LIFT * this.vp[c * 4 + 3];
    this.eye = eye;
    this.dist = dist;
    return this.vp;
  }

  project(x, y, z) {                  // world (print) mm -> CSS px, null behind the camera
    const m = this.vp, o = this.origin;
    const X = x - o[0], Y = y - o[1], Z = z - o[2];
    const w = m[3] * X + m[7] * Y + m[11] * Z + m[15];
    if (w <= 0) return null;
    return [(1 + (m[0] * X + m[4] * Y + m[8] * Z + m[12]) / w) / 2 * this.w,
      (1 - (m[1] * X + m[5] * Y + m[9] * Z + m[13]) / w) / 2 * this.h];
  }

  resize() {
    const r = this.host.getBoundingClientRect();
    if (!r.width || !r.height) return;
    this.dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.w = r.width; this.h = r.height;
    for (const c of [this.canvas, this.labels]) { c.width = r.width * this.dpr; c.height = r.height * this.dpr; }
    this.dirty();
  }

  dirty() {
    if (this.queued || !this.ok) return;
    this.queued = true;
    requestAnimationFrame(() => { this.queued = false; this.draw(); });
  }

  setTime(t, nozzle) { this.t = t; this.nozzle = nozzle; this.dirty(); }

  draw() {
    if (!this.count || !this.w) return;
    const gl = this.gl, vp = this.camera();
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.enable(gl.DEPTH_TEST);
    gl.disable(gl.BLEND);

    gl.useProgram(this.lines.p);
    gl.uniformMatrix4fv(this.lines.u.u_vp, false, vp);
    gl.uniform4fv(this.lines.u.u_color, [...rgb(token('line-2')), 1]);
    gl.bindVertexArray(this.gridVao);
    gl.drawArrays(gl.LINES, 0, this.gridCount);

    const { p, u } = this.bead;
    gl.useProgram(p);
    gl.uniformMatrix4fv(u.u_vp, false, vp);
    gl.uniform1f(u.u_t, this.t);
    gl.uniform2fv(u.u_range, this.range);
    gl.uniform3fv(u.u_grey, rgb(token('bead')));
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.lut);
    gl.uniform1i(u.u_lut, 0);
    gl.bindVertexArray(this.vao);
    gl.drawElements(gl.TRIANGLES, this.count, gl.UNSIGNED_INT, 0);

    if (this.showBands && this.dotCount) {
      gl.enable(gl.BLEND);
      gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.depthMask(false);
      const dp = this.dots;
      gl.useProgram(dp.p);
      gl.uniformMatrix4fv(dp.u.u_vp, false, vp);
      gl.uniform1f(dp.u.u_t, this.t);
      gl.uniform4fv(dp.u.u_color, [...rgb(token('band')), 0.16]);
      // a disc as wide as a bead, in pixels at the point's distance
      gl.uniform1f(dp.u.u_size, 46 * this.canvas.height / (2 * Math.tan(FOV / 2)));
      gl.bindVertexArray(this.dotVao);
      gl.drawArrays(gl.POINTS, 0, this.dotCount);
      gl.depthMask(true);
    }
    gl.bindVertexArray(null);
    this.drawLabels();
  }

  drawLabels() {
    const c = this.ctx, g = this.grid, b = this.box;
    c.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    c.clearRect(0, 0, this.w, this.h);
    const ink = token('ink-3');
    c.font = `11px ${token('font')}`;
    c.fillStyle = ink;
    const eye = [this.eye[0] + this.origin[0], this.eye[1] + this.origin[1]];
    const yEdge = eye[1] < (g.gy0 + g.gy1) / 2 ? g.gy0 : g.gy1;   // the edges facing the camera
    const xEdge = eye[0] < (g.gx0 + g.gx1) / 2 ? g.gx0 : g.gx1;
    const zf = b.z0, label = (txt, p, dx = 0, dy = 0, align = 'center') => {
      if (!p) return;
      c.textAlign = align;
      c.fillText(txt, p[0] + dx, p[1] + dy);
    };
    const skip = Math.ceil((g.gx1 - g.gx0) / g.step / 8);
    let i = 0;
    for (let x = g.gx0; x <= g.gx1 + 1e-6; x += g.step, i++) if (i % skip === 0) label(String(Math.round(x)), this.project(x, yEdge, zf), 0, 14);
    i = 0;
    for (let y = g.gy0; y <= g.gy1 + 1e-6; y += g.step, i++) {   // the corner belongs to X
      if (i % skip === 0 && y !== yEdge) label(String(Math.round(y)), this.project(xEdge, y, zf), xEdge === g.gx0 ? -10 : 10, 12, xEdge === g.gx0 ? 'right' : 'left');
    }
    label('X (mm)', this.project((g.gx0 + g.gx1) / 2, yEdge, zf), 0, 30);
    label('Y (mm)', this.project(xEdge, (g.gy0 + g.gy1) / 2, zf), xEdge === g.gx0 ? -34 : 34, 28);
    if (this.nozzle && this.t < this.tEnd) {
      const p = this.project(...this.nozzle);
      if (p) {
        c.beginPath();
        c.arc(p[0], p[1], 5.5, 0, 2 * Math.PI);
        c.fillStyle = token('s2');
        c.fill();
        c.lineWidth = 1.2;
        c.strokeStyle = token('ink');
        c.stroke();
      }
    }
  }

  // ---------------------------------------------------------------- mouse, touch
  controls() {
    const pts = new Map();
    let pinch = 0;
    const host = this.host;
    host.addEventListener('pointerdown', (e) => {
      if (e.target.closest('button')) return;
      host.setPointerCapture(e.pointerId);
      pts.set(e.pointerId, [e.clientX, e.clientY]);
      pinch = 0;
    });
    host.addEventListener('pointermove', (e) => {
      const last = pts.get(e.pointerId);
      if (!last) return;
      const dx = e.clientX - last[0], dy = e.clientY - last[1];
      pts.set(e.pointerId, [e.clientX, e.clientY]);
      if (pts.size === 2) {
        const [a, b] = [...pts.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]);
        if (pinch) this.zoomBy(d / pinch);
        pinch = d;
      } else if (e.shiftKey || e.buttons === 2 || e.buttons === 4) {
        this.panBy(dx, dy);
      } else {
        if (this.follow) { this.az += AZ_DRIFT * Math.min(this.t, this.tEnd) / this.tEnd; this.follow = false; }
        this.az -= dx * 0.4;
        this.el = Math.max(-5, Math.min(89, this.el + dy * 0.3));
      }
      this.dirty();
    });
    const up = (e) => { pts.delete(e.pointerId); pinch = 0; };
    host.addEventListener('pointerup', up);
    host.addEventListener('pointercancel', up);
    host.addEventListener('contextmenu', (e) => e.preventDefault());
    host.addEventListener('wheel', (e) => {
      e.preventDefault();
      this.zoomBy(Math.exp(-e.deltaY * 0.0012));
    }, { passive: false });
    host.addEventListener('dblclick', (e) => { if (!e.target.closest('button')) this.home(); });
  }

  zoomBy(f) { this.zoom = Math.max(0.5, Math.min(14, this.zoom * f)); this.dirty(); }

  panBy(dx, dy) {
    const az = this.az * Math.PI / 180, k = this.dist * 2 * Math.tan(FOV / 2) / this.h;
    const right = [-Math.sin(az), Math.cos(az)];
    this.pan[0] -= dx * k * right[0];
    this.pan[1] -= dx * k * right[1];
    this.pan[2] += dy * k;
    this.dirty();
  }

  home() { this.resetView(); this.follow = true; this.dirty(); }
}
