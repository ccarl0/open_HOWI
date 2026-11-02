// SPDX-License-Identifier: GPL-3.0-or-later
import { Viewer } from './viewer.js';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const clone = (v) => JSON.parse(JSON.stringify(v));
let uid = 0;

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') e.className = v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  e.append(...children.filter((c) => c !== null && c !== undefined));
  return e;
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

const saved = {
  get(key) { try { return JSON.parse(localStorage.getItem(key)); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ } },
};

function stable(v) {
  if (Array.isArray(v)) return `[${v.map(stable).join(',')}]`;
  if (v && typeof v === 'object') return `{${Object.keys(v).sort().map((k) => `${k}:${stable(v[k])}`).join(',')}}`;
  return JSON.stringify(v);
}

function fmtTime(s) {
  s = Math.round(s);
  const h = Math.floor(s / 3600); const m = Math.floor((s % 3600) / 60); const sec = s % 60;
  return h ? `${h} h ${String(m).padStart(2, '0')} min` : `${m} min ${String(sec).padStart(2, '0')} s`;
}
const clock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
const num = (v, d = 1) => Number(v).toFixed(d);

function download(name, text, type = 'text/plain') {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = el('a', { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const slug = (s) => (s || 'part').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'part';

const state = {
  info: null, presets: { machine: [], material: [] }, airfoils: { naca: [], library: [] },
  project: null, result: null, sliceSeq: 0, sliceTimer: null, tab: 'prepare',
  device: { connected: false, status: { state: 'Disconnected' }, job: null, lastLog: 0, polling: false },
  scrub: { t: Infinity, playing: false },
};

// Project ---------------------------------------------------------------------

function withDefaults(project) {
  const d = state.info.default_project;
  return {
    ...clone(d), ...project,
    machine: { ...d.machine, ...project.machine }, material: { ...d.material, ...project.material },
    part: { ...d.part, ...project.part }, placement: { ...d.placement, ...project.placement },
  };
}

function setProject(project) {
  state.project = withDefaults(project);
  renderSettings();
  changed();
}

function changed() {
  saved.set('openhowi.project', state.project);
  renderPresetSelect('machine');
  renderPresetSelect('material');
  clearTimeout(state.sliceTimer);
  state.sliceTimer = setTimeout(slice, 250);
}

async function slice() {
  const seq = ++state.sliceSeq;
  $('#busy').hidden = false;
  try {
    const result = await api('/api/slice', { project: state.project });
    if (seq !== state.sliceSeq) return;
    state.result = result;
    showBanner(null);
    state.viewer.setResult(result);
  } catch (err) {
    if (seq !== state.sliceSeq) return;
    state.result = null;
    showBanner(err.message);
    const m = state.project.machine;
    state.viewer.setMachine({ tower_distance: m.tower_distance, x_travel: m.x_travel, y_travel: m.y_travel });
    state.viewer.clearPart();
  } finally {
    if (seq === state.sliceSeq) $('#busy').hidden = true;
  }
  renderResult();
  applyTabView();
}

function showBanner(message) {
  const b = $('#banner');
  b.hidden = !message;
  b.textContent = message ? `Can't slice: ${message}` : '';
}

// Forms -------------------------------------------------------------------------

function numberInput(id, f, value, set) {
  const input = el('input', { id, type: 'number', step: f.step ?? 'any', min: f.min, max: f.max });
  input.value = value ?? '';
  if (f.optional) input.placeholder = 'auto';
  input.addEventListener('input', () => {
    const raw = input.value.trim();
    if (raw === '') {
      input.classList.toggle('invalid', !f.optional);
      if (f.optional) set(null);
      return;
    }
    const v = Number(raw);
    const bad = !Number.isFinite(v) || (f.min !== undefined && v < f.min) || (f.max !== undefined && v > f.max);
    input.classList.toggle('invalid', bad);
    if (!bad) set(f.step === 1 ? Math.round(v) : v);
  });
  return input;
}

function airfoilInput(id, f, value, set) {
  const sel = el('select', { id });
  if (f.optional) sel.append(new Option('Same as root', ''));
  const naca = el('optgroup', { label: 'NACA 4-digit' });
  for (const code of state.airfoils.naca) naca.append(new Option(`NACA ${code}`, `naca${code}`));
  sel.append(naca);
  if (state.airfoils.library.length) {
    const lib = el('optgroup', { label: 'Library' });
    for (const a of state.airfoils.library) lib.append(new Option(a.name, `lib:${a.file}`));
    sel.append(lib);
  }
  let current = '';
  if (value && typeof value === 'object') {
    sel.append(new Option(`${value.name} (in project)`, 'embedded'));
    current = 'embedded';
  } else if (typeof value === 'string') {
    current = value.toLowerCase().replace(/[\s-]/g, '');
    if (![...sel.options].some((o) => o.value === current)) sel.append(new Option(value, current));
  }
  sel.append(new Option('Load .dat file…', 'load'));
  sel.value = current;
  sel.addEventListener('change', async () => {
    const v = sel.value;
    try {
      if (v === 'load') {
        const picked = await pickAirfoilFile();
        if (picked) { set(picked); renderPartForm(); } else sel.value = current;
      } else if (v.startsWith('lib:')) {
        const file = v.slice(4);
        const entry = state.airfoils.library.find((a) => a.file === file);
        const r = await api(`/api/airfoils/file?file=${encodeURIComponent(file)}`);
        set({ name: entry ? entry.name : file, dat: r.dat });
        renderPartForm();
      } else if (v !== 'embedded') {
        set(v || null);
      }
    } catch (err) {
      alert(err.message);
      sel.value = current;
    }
  });
  return sel;
}

function pickAirfoilFile() {
  return new Promise((resolve) => {
    const input = $('#file-airfoil');
    input.value = '';
    input.onchange = async () => {
      const file = input.files[0];
      if (!file) { resolve(null); return; }
      try {
        const dat = await file.text();
        const r = await api('/api/airfoils', { filename: file.name, dat });
        state.airfoils.library = r.library;
        resolve({ name: r.added.name, dat });
      } catch (err) {
        alert(`Could not load ${file.name}: ${err.message}`);
        resolve(null);
      }
    };
    input.click();
  });
}

function fieldEl(f, value, set, showHelp) {
  const id = `f${uid++}`;
  let input;
  if (f.type === 'select') {
    input = el('select', { id });
    for (const [v, label] of f.options) input.append(new Option(label, v, false, v === value));
    input.addEventListener('change', () => set(input.value));
  } else if (f.type === 'airfoil') {
    input = airfoilInput(id, f, value, set);
  } else if (f.type === 'text') {
    input = el('input', { id, type: 'text', spellcheck: 'false' });
    input.value = value ?? '';
    input.addEventListener('input', () => set(input.value.trim()));
  } else {
    input = numberInput(id, f, value, set);
  }
  const control = f.unit ? el('div', { class: 'unit-wrap' }, input, el('span', { class: 'unit' }, f.unit)) : input;
  const row = el('div', { class: `field${f.type === 'airfoil' ? ' airfoil' : ''}`, title: f.help },
    el('label', { for: id }, f.label), control);
  if (showHelp && f.help) row.append(el('div', { class: 'help' }, f.help));
  return row;
}

function buildForm(container, fields, values, onChange, showHelp = false) {
  container.replaceChildren();
  let group = null;
  for (const f of fields) {
    if (f.group && f.group !== group) {
      group = f.group;
      container.append(el('div', { class: 'group-title' }, group));
    }
    container.append(fieldEl(f, values[f.key], (v) => onChange(f.key, v), showHelp));
  }
}

function renderPartForm() {
  buildForm($('#part-form'), state.info.schema.part, state.project.part, (k, v) => {
    state.project.part[k] = v; changed();
  });
}

function renderSettings() {
  $('#project-title').value = state.project.title || '';
  renderPartForm();
  buildForm($('#placement-form'), state.info.schema.placement, state.project.placement, (k, v) => {
    state.project.placement[k] = v; changed();
  });
  const power = state.project.material.wire_power;
  if (power !== null && power !== undefined) $('#wire-power').value = power;
}

// Presets ----------------------------------------------------------------------

function currentPreset(kind) {
  return state.presets[kind].find((p) => p.name === state.project[`${kind}_preset`]);
}

function renderPresetSelect(kind) {
  const sel = $(`#sel-${kind}`);
  const current = state.project[`${kind}_preset`];
  const preset = currentPreset(kind);
  const modified = preset && stable(preset.values) !== stable(state.project[kind]);
  sel.replaceChildren();
  for (const [label, builtin] of [['System presets', true], ['User presets', false]]) {
    const list = state.presets[kind].filter((p) => p.builtin === builtin);
    if (!list.length) continue;
    const group = el('optgroup', { label });
    for (const p of list) group.append(new Option(p.name + (p.name === current && modified ? ' (modified)' : ''), p.name));
    sel.append(group);
  }
  if (!preset) sel.append(new Option(`${current || 'Custom'} (from project)`, current || ''));
  sel.value = current || '';
}

function selectPreset(kind, name) {
  const preset = state.presets[kind].find((p) => p.name === name);
  if (!preset) return;
  state.project[kind] = clone(preset.values);
  state.project[`${kind}_preset`] = name;
  if (kind === 'material') renderSettings();
  changed();
}

async function editPreset(kind) {
  const dlg = $('#dlg-preset');
  const values = clone(state.project[kind]);
  const preset = currentPreset(kind);
  $('#dlg-preset-title').textContent = kind === 'machine' ? 'Machine settings' : 'Material settings';
  $('#dlg-preset-hint').textContent = kind === 'material'
    ? 'Built-in materials are starting points. Calibrate yours with Kerf test, then save your own preset.' : '';
  buildForm($('#dlg-preset-form'), state.info.schema[kind], values, (k, v) => { values[k] = v; }, true);
  $('#dlg-preset-delete').hidden = !preset || preset.builtin;
  dlg.returnValue = '';
  dlg.showModal();
  await new Promise((r) => dlg.addEventListener('close', r, { once: true }));
  try {
    if (dlg.returnValue === 'apply') {
      state.project[kind] = values;
    } else if (dlg.returnValue === 'saveas') {
      const suggestion = preset && !preset.builtin ? preset.name : `${state.project[`${kind}_preset`] || 'My preset'} (copy)`;
      const name = prompt('Preset name', suggestion);
      if (!name) return;
      state.presets = await api('/api/presets/save', { kind, name, values });
      state.project[kind] = values;
      state.project[`${kind}_preset`] = name.trim();
    } else if (dlg.returnValue === 'delete') {
      if (!confirm(`Delete the preset "${preset.name}"?`)) return;
      state.presets = await api('/api/presets/delete', { kind, name: preset.name });
      selectPreset(kind, state.presets[kind][0].name);
      return;
    } else {
      return;
    }
  } catch (err) {
    alert(err.message);
    return;
  }
  if (kind === 'material') renderSettings();
  changed();
}

// Results ------------------------------------------------------------------------

function renderThumbs() {
  const box = $('#thumbs');
  box.replaceChildren();
  const r = state.result;
  if (!r) return;
  for (const [label, pts, name] of [['Root', r.part.root, r.stats.airfoils.root], ['Tip', r.part.tip, r.stats.airfoils.tip]]) {
    const xs = pts.map((p) => p[0]); const ys = pts.map((p) => p[1]);
    const x0 = Math.min(...xs); const x1 = Math.max(...xs); const y0 = Math.min(...ys); const y1 = Math.max(...ys);
    // Leading edge on the left, as airfoils are usually drawn.
    const d = pts.map((p, i) => `${i ? 'L' : 'M'}${(x1 - p[0]).toFixed(2)} ${(y1 - p[1]).toFixed(2)}`).join(' ') + 'Z';
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', `${-2} ${-2} ${x1 - x0 + 4} ${y1 - y0 + 4}`);
    svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d);
    svg.append(path);
    box.append(el('figure', {}, svg, el('figcaption', {}, `${label} · ${name}`)));
  }
}

function renderResult() {
  const r = state.result;
  renderThumbs();
  const stats = $('#stats');
  stats.replaceChildren();
  const warnings = $('#warnings');
  warnings.replaceChildren();
  $('#btn-export').disabled = !r;
  $('#btn-send').disabled = !r;
  if (!r) {
    stats.append(el('dt', {}, 'Status'), el('dd', {}, 'Not sliced'));
    $('#summary').textContent = 'Fix the settings to slice.';
    $('#block-hint').textContent = '';
    $('#gcode-view').textContent = '';
    updateScrubber();
    return;
  }
  const s = r.stats; const a = r.machine.axis_letters;
  const xy = a.slice(0, 2); const uv = a.slice(2);
  const slow = s.speed_ratio.tip < s.speed_ratio.root ? 'tip' : 'root';
  const rows = [
    ['Estimated time', fmtTime(s.time_s)],
    ['Cut length', `${xy} ${num(s.cut_length.XY, 0)} · ${uv} ${num(s.cut_length.UV, 0)} mm`],
    ['Kerf', `root ${num(s.kerf.root, 2)} · tip ${num(s.kerf.tip, 2)} mm`],
    ['Slower end', `${slow}, ${Math.round(s.speed_ratio[slow] * 100)} % of cut speed`],
    ['Peak carriage speed', `${num(s.peak_speed.XY, 0)} · ${num(s.peak_speed.UV, 0)} mm/min`],
    [`${xy} range`, `X ${num(s.extents.XY[2], 0)} · Y ${num(s.extents.XY[3], 0)} mm`],
    [`${uv} range`, `X ${num(s.extents.UV[2], 0)} · Y ${num(s.extents.UV[3], 0)} mm`],
    ['G-code', `${s.gcode_lines} lines`],
  ];
  for (const [k, v] of rows) stats.append(el('dt', {}, k), el('dd', {}, v));
  for (const w of r.warnings) warnings.append(el('li', {}, w));

  const b = r.block;
  const size = `${num(b.x1 - b.x0, 0)} × ${num(b.y1 - b.y0, 0)} × ${num(b.z1 - b.z0, 0)} mm`;
  $('#block-hint').textContent = `At least ${size} (depth × height × length). Rear face between X 0 and `
    + `${num(state.project.placement.x_margin, 0)} mm, bottom at or below Y ${num(b.y0, 0)} mm, `
    + `spanning ${num(b.z0, 0)}–${num(b.z1, 0)} mm from the XY carriage.`;
  $('#summary').replaceChildren(el('b', {}, fmtTime(s.time_s)), ` · foam ${size}`);
  const lines = r.gcode.split('\n');
  $('#gcode-view').textContent = lines.slice(0, 400).join('\n') + (lines.length > 400 ? `\n… ${lines.length - 400} more lines` : '');
  updateScrubber();
}

// Preview scrubber -------------------------------------------------------------------

function updateScrubber() {
  const r = state.result;
  $('#scrubber').hidden = !(r && state.tab === 'preview');
  if (!r) return;
  const total = r.path.t.at(-1);
  const t = Math.min(state.scrub.t, total);
  $('#scrub').value = total ? Math.round((t / total) * 1000) : 1000;
  $('#scrub-time').textContent = `${clock(t)} / ${clock(total)}`;
  if (state.tab === 'preview') showTime(t);
}

function showTime(t) {
  const p = state.result.path;
  let lo = 0; let hi = p.t.length - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (p.t[mid] <= t) lo = mid; else hi = mid - 1; }
  const next = Math.min(lo + 1, p.t.length - 1);
  const span = p.t[next] - p.t[lo];
  state.viewer.setProgress(lo, span > 0 ? Math.min(1, (t - p.t[lo]) / span) : 0);
}

function togglePlay() {
  const r = state.result;
  if (!r) return;
  state.scrub.playing = !state.scrub.playing;
  $('#btn-play').textContent = state.scrub.playing ? '❚❚' : '▶';
  if (!state.scrub.playing) return;
  const total = r.path.t.at(-1);
  if (state.scrub.t >= total) state.scrub.t = 0;
  let last = performance.now();
  const step = (now) => {
    if (!state.scrub.playing || !state.result) return;
    state.scrub.t = Math.min(total, state.scrub.t + ((now - last) / 1000) * Number($('#play-speed').value));
    last = now;
    updateScrubber();
    if (state.scrub.t >= total) { togglePlay(); return; }
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

// Tabs ----------------------------------------------------------------------------

function setTab(tab) {
  state.tab = tab;
  for (const b of $$('.tabs button')) b.setAttribute('aria-selected', String(b.dataset.tab === tab));
  for (const p of $$('.panel')) p.hidden = p.dataset.panel !== tab;
  if (state.scrub.playing && tab !== 'preview') togglePlay();
  applyTabView();
  if (tab === 'device') pollDevice();
}

function applyTabView() {
  updateScrubber();
  const r = state.result;
  if (state.tab === 'prepare' && r) {
    state.viewer.setProgress(r.path.t.length - 1, 1);
    state.viewer.setWire(r.path.xy[0], r.path.uv[0]);
  } else if (state.tab === 'device') {
    if (r) state.viewer.setProgress(r.path.t.length - 1, 1);
    showLiveWire();
  }
}

// Device ---------------------------------------------------------------------------

async function refreshPorts() {
  try {
    const { ports } = await api('/api/device/ports');
    const sel = $('#sel-port');
    const previous = sel.value || saved.get('openhowi.port');
    sel.replaceChildren(...ports.map((p) => new Option(p.label, p.id)));
    if (ports.some((p) => p.id === previous)) sel.value = previous;
  } catch (err) {
    log('error', err.message);
  }
}

async function deviceCall(path, body = {}) {
  try {
    const r = await api(`/api/device/${path}`, body);
    pollDevice();
    return r;
  } catch (err) {
    log('error', err.message);
    $('#conn-hint').textContent = err.message;
    return null;
  }
}

function log(kind, text) {
  const box = $('#console');
  box.append(el('div', { class: kind }, text));
  while (box.childElementCount > 500) box.firstElementChild.remove();
  box.scrollTop = box.scrollHeight;
}

async function pollDevice() {
  const d = state.device;
  if (d.polling || !(d.connected || state.tab === 'device')) return;
  d.polling = true;
  try {
    const r = await api(`/api/device/state?since=${d.lastLog}`);
    for (const e of r.log) { log(e.kind, e.kind === 'tx' ? `> ${e.text}` : e.text); d.lastLog = e.id; }
    Object.assign(d, { connected: r.connected, status: r.status, job: r.job, ready: r.ready, axes: r.axis_letters });
    renderDevice();
  } catch { /* server gone; try again next tick */ } finally {
    d.polling = false;
  }
}

function showLiveWire() {
  const w = state.device.status.wpos;
  if (state.tab === 'device' && state.device.connected && w && w.length >= 4) state.viewer.setWire([w[0], w[1]], [w[2], w[3]]);
}

function renderDevice() {
  const d = state.device;
  const s = d.status.state || 'Disconnected';
  const pill = $('#state-pill');
  pill.textContent = s; pill.className = `pill ${s}`;
  $('#tab-device-dot').className = `dot ${d.connected ? (['Run', 'Jog', 'Hold'].includes(s) ? 'busy' : 'on') : ''}`;
  $('#btn-connect').textContent = d.connected ? 'Disconnect' : 'Connect';
  $('#btn-connect').classList.toggle('primary', !d.connected);
  $('#sel-port').disabled = d.connected;
  $('#btn-estop').hidden = !d.connected;
  if (d.connected) $('#conn-hint').textContent = d.ready ? '' : 'Waiting for the controller…';

  const axes = d.axes || state.project.machine.axis_letters || 'XYUV';
  const wpos = d.status.wpos || [];
  $('#dro').replaceChildren(...[...axes].map((a, i) => el('div', {}, el('b', {}, a),
    el('span', {}, wpos[i] !== undefined ? wpos[i].toFixed(2) : '—'))));

  const job = d.job;
  const active = job && ['running', 'paused', 'finishing'].includes(job.state);
  const jp = $('#job-pill');
  jp.textContent = job ? job.state : 'No job';
  jp.className = `pill ${job ? job.state : ''}`;
  $('#job-bar').style.width = job ? `${(100 * job.acked) / job.total}%` : '0';
  if (job) {
    const extra = job.error ? ` — ${job.error}` : '';
    $('#job-hint').textContent = `${job.name}: ${job.acked}/${job.total} lines, ${clock(job.elapsed || 0)} elapsed${extra}`;
  }
  const idle = d.connected && d.ready && s === 'Idle' && !active;
  $('#btn-start').disabled = !(idle && state.result);
  $('#btn-pause').disabled = !(active && job.state !== 'paused');
  $('#btn-resume').disabled = !(active && job.state === 'paused');
  $('#btn-stop').disabled = !d.connected;
  for (const id of ['#btn-home', '#btn-unlock', '#btn-zero', '#btn-go-zero', '#btn-wire-on', '#btn-wire-off']) {
    $(id).disabled = !d.connected || active;
  }
  for (const b of $$('[data-jog]')) b.disabled = !d.connected || active;
  $('#console-input').disabled = !d.connected || active;
  showLiveWire();
}

function jog(dir) {
  if (dir === 'cancel') { deviceCall('jog_cancel'); return; }
  const [dx, dy] = dir.split(',').map(Number);
  const step = Number($('#jog-step').value);
  const mode = $('input[name="jog-mode"]:checked').value;
  const a = state.device.axes || state.project.machine.axis_letters;
  const moves = {};
  if (mode !== 'uv') { moves[a[0]] = dx * step; moves[a[1]] = dy * step; }
  if (mode !== 'xy') { moves[a[2]] = dx * step; moves[a[3]] = dy * step; }
  for (const k of Object.keys(moves)) if (!moves[k]) delete moves[k];
  deviceCall('jog', { moves, feed: Number($('#jog-feed').value) || 600 });
}

async function confirmDialog(title, text, ok = 'Start') {
  const dlg = $('#dlg-confirm');
  $('#confirm-title').textContent = title;
  $('#confirm-text').textContent = text;
  $('#confirm-ok').textContent = ok;
  dlg.returnValue = '';
  dlg.showModal();
  await new Promise((r) => dlg.addEventListener('close', r, { once: true }));
  return dlg.returnValue === 'ok';
}

async function startJob(gcode, name, details) {
  const d = state.device;
  if (!d.connected) {
    setTab('device');
    $('#conn-hint').textContent = 'Connect to the machine first.';
    return;
  }
  const ok = await confirmDialog('Start cutting?',
    `${details} Before you start: both carriages at the origin (X0 Y0 U0 V0, wire below and behind the foam), `
    + 'foam clamped, and you can reach the STOP button.');
  if (!ok) return;
  await deviceCall('job', { gcode, name });
}

function startCurrentJob() {
  const r = state.result;
  if (!r) return;
  const b = r.block;
  startJob(r.gcode, state.project.title || 'Wing panel',
    `Estimated time ${fmtTime(r.stats.time_s)}. Foam ${num(b.x1 - b.x0, 0)} × ${num(b.y1 - b.y0, 0)} × `
    + `${num(b.z1 - b.z0, 0)} mm, rear face before X ${num(state.project.placement.x_margin, 0)} mm.`);
}

// Kerf test ---------------------------------------------------------------------------

async function kerfTest() {
  let r;
  try {
    r = await api('/api/calibration/kerf', { machine: state.project.machine, material: state.project.material });
  } catch (err) { alert(err.message); return; }
  $('#kerf-instructions').textContent = r.instructions;
  const dlg = $('#dlg-kerf');
  dlg.returnValue = '';
  dlg.showModal();
  await new Promise((res) => dlg.addEventListener('close', res, { once: true }));
  if (dlg.returnValue === 'download') download('kerf-test.gcode', r.gcode);
  if (dlg.returnValue === 'send') startJob(r.gcode, 'Kerf test', 'Kerf test: two 60 × 30 mm pieces.');
}

// Wiring ------------------------------------------------------------------------------

function wire() {
  for (const b of $$('.tabs button')) b.addEventListener('click', () => setTab(b.dataset.tab));
  for (const b of $$('[data-view]')) b.addEventListener('click', () => state.viewer.view(b.dataset.view));
  $('#sel-machine').addEventListener('change', (e) => selectPreset('machine', e.target.value));
  $('#sel-material').addEventListener('change', (e) => selectPreset('material', e.target.value));
  $('#edit-machine').addEventListener('click', () => editPreset('machine'));
  $('#edit-material').addEventListener('click', () => editPreset('material'));
  $('#project-title').addEventListener('input', (e) => { state.project.title = e.target.value; changed(); });

  $('#scrub').addEventListener('input', (e) => {
    if (!state.result) return;
    if (state.scrub.playing) togglePlay();
    state.scrub.t = (Number(e.target.value) / 1000) * state.result.path.t.at(-1);
    updateScrubber();
  });
  $('#btn-play').addEventListener('click', togglePlay);

  $('#btn-export').addEventListener('click', () => {
    if (state.result) download(`${slug(state.project.title)}.gcode`, state.result.gcode);
  });
  $('#btn-send').addEventListener('click', () => { setTab('device'); startCurrentJob(); });

  $('#btn-new').addEventListener('click', () => {
    if (confirm('Start a new project? Unsaved changes are lost.')) setProject(clone(state.info.default_project));
  });
  $('#btn-save').addEventListener('click', () => {
    download(`${slug(state.project.title)}.howi.json`, JSON.stringify(state.project, null, 2), 'application/json');
  });
  $('#btn-open').addEventListener('click', () => $('#file-project').click());
  $('#file-project').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    try {
      const project = JSON.parse(await file.text());
      if (!project || typeof project !== 'object' || project.version !== state.info.default_project.version) {
        throw new Error('not an open_HOWI project file');
      }
      setProject(project);
    } catch (err) { alert(`Could not open ${file.name}: ${err.message}`); }
  });
  $('#btn-kerf').addEventListener('click', kerfTest);

  $('#btn-ports').addEventListener('click', refreshPorts);
  $('#btn-connect').addEventListener('click', async () => {
    if (state.device.connected) { await deviceCall('disconnect'); return; }
    const port = $('#sel-port').value;
    saved.set('openhowi.port', port);
    $('#conn-hint').textContent = '';
    const m = state.project.machine;
    if (await deviceCall('connect', { port, baud: Math.round(m.baud || 115200), axis_letters: m.axis_letters })) {
      state.device.connected = true;
    }
  });
  for (const b of $$('[data-jog]')) b.addEventListener('click', () => jog(b.dataset.jog));
  $('#btn-home').addEventListener('click', () => deviceCall('home'));
  $('#btn-unlock').addEventListener('click', () => deviceCall('unlock'));
  $('#btn-zero').addEventListener('click', () => deviceCall('zero'));
  $('#btn-go-zero').addEventListener('click', () => deviceCall('go_zero', { feed: Number($('#jog-feed').value) || 600 }));
  $('#btn-wire-on').addEventListener('click', () => deviceCall('wire', { on: true, power: Number($('#wire-power').value) || 0 }));
  $('#btn-wire-off').addEventListener('click', () => deviceCall('wire', { on: false }));
  $('#btn-start').addEventListener('click', startCurrentJob);
  $('#btn-pause').addEventListener('click', () => deviceCall('pause'));
  $('#btn-resume').addEventListener('click', () => deviceCall('resume'));
  $('#btn-stop').addEventListener('click', () => deviceCall('stop'));
  $('#btn-estop').addEventListener('click', () => deviceCall('stop'));
  $('#console-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const line = $('#console-input').value.trim();
    if (!line) return;
    $('#console-input').value = '';
    deviceCall('command', { line });
  });
}

async function init() {
  state.viewer = new Viewer($('#view'));
  const [info, presets, airfoils] = await Promise.all([api('/api/info'), api('/api/presets'), api('/api/airfoils')]);
  Object.assign(state, { info, presets, airfoils });
  const previous = saved.get('openhowi.project');
  state.project = withDefaults(previous && previous.version === info.default_project.version ? previous : info.default_project);
  wire();
  renderSettings();
  renderDevice();
  changed();
  refreshPorts();
  setInterval(pollDevice, 300);
}

init().catch((err) => showBanner(`could not start: ${err.message}`));
