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
const projectName = () => state.project.title || { wing: 'wing-panel', shape: 'shape', fuselage: 'fuselage', nest: 'block' }[partType()] || 'part';
// A field shown only when another field has one of the given values (a value or a list of them)
const matches = (want, have) => (Array.isArray(want) ? want.includes(have) : want === have);

const state = {
  info: null, presets: { machine: [], material: [] }, airfoils: { naca: [], library: [], database: [] }, foilDb: null,
  project: null, result: null, sliceSeq: 0, sliceTimer: null, tab: 'prepare', stash: {}, drawingInfo: null, viewInfo: {},
  device: { connected: false, status: { state: 'Disconnected' }, job: null, lastLog: 0, polling: false },
  scrub: { t: Infinity }, run: null, shown: null, caption: '',
};

// Project ---------------------------------------------------------------------

// Projects saved before spars became a list had one spar as flat fields.
function upgradePart(part) {
  if (!part || Array.isArray(part.spars) || !('spar_width' in part)) return part;
  const p = { ...part, spars: [] };
  if (p.spar_width > 0 && (p.spar_depth_top > 0 || p.spar_depth_bottom > 0)) {
    p.spars.push({
      position: p.spar_position ?? 25, position_tip: p.spar_position_tip ?? null, width: p.spar_width,
      depth_top: p.spar_depth_top || 0, depth_bottom: p.spar_depth_bottom || 0,
    });
  }
  for (const k of ['spar_width', 'spar_depth_top', 'spar_depth_bottom', 'spar_position', 'spar_position_tip']) delete p[k];
  return p;
}

function withDefaults(project) {
  const d = state.info.default_project;
  const type = project.part?.type || 'wing';
  return {
    ...clone(d), ...project,
    machine: { ...d.machine, ...project.machine }, material: { ...d.material, ...project.material },
    part: { ...clone(state.info.default_parts[type] || {}), ...upgradePart(project.part) },
    placement: { ...clone(state.info.default_placements[type] || {}), ...project.placement },
  };
}

function setProject(project) {
  state.project = withDefaults(project);
  state.stash = {};
  state.drawingInfo = null;
  renderSettings();
  changed();
}

const partType = () => state.project.part.type || 'wing';

function switchPartType(type) {
  const current = partType();
  if (type === current) return;
  // Keep the other part's settings for this session, so switching back restores them.
  state.stash[current] = { part: clone(state.project.part), placement: clone(state.project.placement) };
  const next = state.stash[type] || {
    part: clone(state.info.default_parts[type]), placement: clone(state.info.default_placements[type]),
  };
  state.project.part = next.part;
  state.project.placement = next.placement;
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
    stopPlayback();
    state.result = result;
    state.shown = null;
    showBanner(null);
    state.viewer.setResult(result);
  } catch (err) {
    if (seq !== state.sliceSeq) return;
    stopPlayback();
    state.result = null;
    state.shown = null;
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
    current = value.toLowerCase().replace(/[\s_-]/g, '');
    if (![...sel.options].some((o) => o.value === current)) {
      const entry = state.airfoils.database.find((a) => a.id === current);
      const db = el('optgroup', { label: 'Database' });
      db.append(new Option(entry ? `${entry.name} · ${num(entry.thickness, 1)} %` : value, current));
      sel.append(db);
    }
  }
  sel.append(new Option('Browse airfoil database…', 'browse'));
  sel.append(new Option('Load .dat file…', 'load'));
  sel.value = current;
  sel.addEventListener('change', async () => {
    const v = sel.value;
    try {
      if (v === 'browse') {
        const picked = await browseAirfoils(current);
        if (picked) { set(picked); renderPartForm(); } else sel.value = current;
      } else if (v === 'load') {
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

// Airfoil database browser: resolves with the chosen airfoil id, or null.
async function browseAirfoils(current) {
  if (!state.foilDb) state.foilDb = await api('/api/airfoils/database');
  const db = state.foilDb;
  const dlg = $('#dlg-airfoils');
  const search = $('#foil-search');
  const list = $('#foil-list');
  const cats = $('#foil-cats');
  let category = null;
  let chosen = null;
  const svgNS = 'http://www.w3.org/2000/svg';
  const thumb = (outline) => {
    const svg = document.createElementNS(svgNS, 'svg');
    const ys = outline.map((q) => q[1]);
    const y0 = Math.min(...ys); const y1 = Math.max(...ys);
    svg.setAttribute('viewBox', `-0.02 ${(-y1 - 0.02).toFixed(3)} 1.04 ${(y1 - y0 + 0.04).toFixed(3)}`);
    svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    const path = document.createElementNS(svgNS, 'path');
    path.setAttribute('d', outline.map((q, i) => `${i ? 'L' : 'M'}${q[0]} ${-q[1]}`).join(' ') + 'Z');
    svg.append(path);
    return svg;
  };
  const render = () => {
    const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
    const rows = db.airfoils.filter((a) => (!category || a.category === category) && words.every((w) => (
      `${a.id} ${a.name} ${a.note} ${a.category}`.toLowerCase().includes(w) || a.id.includes(w.replace(/[\s_-]/g, '')))));
    list.replaceChildren(...rows.map((a) => {
      const props = a.camber ? `${num(a.thickness, 1)} % thick · camber ${num(a.camber, 1)} %` : `${num(a.thickness, 1)} % thick · symmetric`;
      const button = el('button', { type: 'button', class: `foil${a.id === current ? ' current' : ''}`, title: a.category },
        thumb(a.outline),
        el('span', { class: 'name' }, el('span', {}, a.name), el('code', {}, a.id)),
        el('span', { class: 'props' }, el('span', { class: 'nums' }, props), a.reflex ? el('span', { class: 'tag' }, 'reflexed') : null),
        el('span', { class: 'note' }, a.note));
      button.addEventListener('click', () => { chosen = a.id; dlg.close('pick'); });
      return button;
    }));
    if (!rows.length) list.append(el('div', { class: 'foil-empty' }, 'No airfoil matches.'));
  };
  const chip = (label, value) => {
    const b = el('button', { type: 'button', role: 'radio', 'aria-checked': String(category === value) }, label);
    b.addEventListener('click', () => {
      category = value;
      for (const c of $$('button', cats)) c.setAttribute('aria-checked', String(c === b));
      render();
    });
    return b;
  };
  cats.replaceChildren(chip('All', null), ...db.categories.map((c) => chip(c, c)));
  search.value = '';
  search.oninput = render;
  render();
  return new Promise((resolve) => {
    dlg.addEventListener('close', () => resolve(chosen), { once: true });
    dlg.showModal();
    search.focus();
    $('.foil.current', list)?.scrollIntoView({ block: 'nearest' });
  });
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

function drawingSummary(info) {
  if (!info) return '';
  const [w, h] = info.size;
  const parts = [`${info.shapes} shape${info.shapes === 1 ? '' : 's'}`, `${info.holes} hole${info.holes === 1 ? '' : 's'}`];
  return `${parts.join(', ')} · ${num(w, 1)} × ${num(h, 1)} mm`;
}

function drawingField(f, value, set) {
  const main = f.key === 'source';  // the 2D shape's drawing; fuselages have a side and a top view
  const info = main ? state.drawingInfo : state.viewInfo[f.key];
  const meta = value
    ? (main ? drawingSummary(info) : (info ? `${num(info.size[0], 1)} × ${num(info.size[1], 1)} mm` : ''))
    : (main ? 'SVG or DXF with closed outlines' : f.help);
  const box = el('div', { class: `drawing-box${value ? '' : ' empty'}` },
    main ? null : el('div', { class: 'meta' }, f.label),
    el('div', { class: 'name' }, value ? value.name : 'No drawing loaded'),
    el('div', { class: 'meta', id: main ? 'drawing-meta' : undefined }, meta));
  const button = el('button', { type: 'button', class: value ? '' : 'primary' }, value ? 'Replace…' : 'Load DXF or SVG…');
  button.addEventListener('click', async () => {
    const picked = await pickDrawingFile();
    if (!picked) return;
    if (!main) state.viewInfo[f.key] = state.drawingInfo;
    if (!state.project.title && main) state.project.title = picked.name.replace(/\.[^.]+$/, '');
    set(picked);
    renderSettings();
  });
  box.append(button);
  return el('div', { class: 'field drawing', title: f.help }, box);
}

// A part to nest in a block: taken from this session (the wing or shape set up before switching
// to the block) or from a saved project file.
function partSummary(src) {
  const p = src.part;
  if (p.type === 'shape') return `2D shape${p.source ? ` from ${p.source.name}` : ''}, ${num(p.span, 0)} mm long`;
  const plan = p.planform && p.planform !== 'panel' ? `${p.planform} wing` : 'wing panel';
  const size = p.planform === 'stations' || p.planform === 'drawing' ? '' : ` ${num(p.root_chord, 0)}/${num(p.tip_chord ?? p.root_chord, 0)} × ${num(p.span, 0)} mm`;
  return `${plan}${size}`;
}

function partField(f, value, set) {
  const box = el('div', { class: `drawing-box${value ? '' : ' empty'}` },
    el('div', { class: 'name' }, value ? value.title : 'No part chosen'),
    el('div', { class: 'meta' }, value ? partSummary(value) : f.help));
  const take = (type) => {
    const part = clone(state.stash[type]?.part || state.info.default_parts[type]);
    const label = { wing: 'Wing panel', shape: '2D shape' }[type];
    set({ title: state.project.title ? `${state.project.title} · ${label}` : label, part });
    renderPartForm();
  };
  const row = el('div', { class: 'button-row' });
  for (const [type, label] of [['wing', 'This wing'], ['shape', 'This 2D shape']]) {
    const b = el('button', { type: 'button' }, label);
    b.addEventListener('click', () => take(type));
    row.append(b);
  }
  const load = el('button', { type: 'button' }, 'Load project…');
  load.addEventListener('click', () => {
    const input = el('input', { type: 'file', accept: '.json,application/json' });
    input.addEventListener('change', async () => {
      const file = input.files[0];
      if (!file) return;
      try {
        const project = JSON.parse(await file.text());
        const type = project?.part?.type || 'wing';
        if (!['wing', 'shape'].includes(type)) throw new Error('only wing panels and 2D shapes can be nested');
        const part = { ...clone(state.info.default_parts[type]), ...upgradePart(project.part) };
        set({ title: project.title || file.name.replace(/\.howi\.json$|\.json$/, ''), part });
        renderPartForm();
      } catch (err) { alert(`Could not load ${file.name}: ${err.message}`); }
    });
    input.click();
  });
  row.append(load);
  box.append(row);
  return el('div', { class: 'field drawing', title: f.help }, box);
}

// A table of numbers (fuselage stations): one row per item.
function tableField(f, value, set) {
  const rows = Array.isArray(value) ? value : [];
  const cols = f.columns;
  const table = el('div', { class: 'table-field', style: `--cols: ${cols.length}` },
    el('div', { class: 'table-row head' },
      ...cols.map((c) => el('span', { title: c.help || '' }, c.label, c.unit ? el('small', {}, c.unit) : null)), el('span')));
  rows.forEach((row, i) => {
    const line = el('div', { class: 'table-row' });
    for (const c of cols) {
      const input = numberInput(`f${uid++}`, c, row[c.key], (v) => { row[c.key] = v; set(rows); });
      input.setAttribute('aria-label', `${c.label}, row ${i + 1}`);
      if (c.help) input.title = c.help;
      line.append(input);
    }
    const remove = el('button', { type: 'button', class: 'icon ghost', title: `Remove row ${i + 1}`, disabled: rows.length <= (f.min_rows || 0) }, '✕');
    remove.addEventListener('click', () => { rows.splice(i, 1); set(rows); renderPartForm(); });
    line.append(remove);
    table.append(line);
  });
  const add = el('button', { type: 'button', class: 'add' }, `+ ${f.add_label || 'Add row'}`);
  add.addEventListener('click', () => {
    const last = rows.at(-1) || Object.fromEntries(cols.map((c) => [c.key, 0]));
    rows.push({ ...last, ...('x' in last ? { x: (Number(last.x) || 0) + 50 } : {}) });  // the next station along
    set(rows);
    renderPartForm();
  });
  if (rows.length >= (f.max || 40)) add.disabled = true;
  return el('div', { class: 'field wide' }, el('div', { class: 'list-field' }, table, f.help ? el('p', { class: 'hint' }, f.help) : null, add));
}

function pickDrawingFile() {
  return new Promise((resolve) => {
    const input = $('#file-drawing');
    input.value = '';
    input.onchange = async () => {
      const file = input.files[0];
      if (!file) { resolve(null); return; }
      try {
        const data = await file.text();
        const info = await api('/api/shapes/inspect', { filename: file.name, data });
        state.drawingInfo = info;
        resolve({ name: file.name, format: info.format, data });
      } catch (err) {
        alert(`Could not load ${file.name}: ${err.message}`);
        resolve(null);
      }
    };
    input.click();
  });
}

// A list of items (spars, servo channels), each edited with its own small form.
function listField(f, value, set) {
  const items = Array.isArray(value) ? value : [];
  const box = el('div', { class: 'list-field' });
  items.forEach((item, index) => {
    const remove = el('button', { type: 'button', class: 'icon ghost', title: `Remove ${f.item_label.toLowerCase()} ${index + 1}` }, '✕');
    remove.addEventListener('click', () => {
      items.splice(index, 1);
      set(items);
      renderPartForm();
    });
    const card = el('div', { class: 'list-item' }, el('div', { class: 'list-head' }, el('span', {}, `${f.item_label} ${index + 1}`), remove));
    // Fields shown only for some values of another field (e.g. diameter for round joiners)
    const controls = new Set(f.item_fields.flatMap((sub) => Object.keys(sub.show_if || {})));
    for (const sub of f.item_fields) {
      const shown = Object.entries(sub.show_if || {}).every(([k, v]) => matches(v, item[k] ?? f.default_item[k]));
      if (!shown) continue;
      card.append(fieldEl(sub, item[sub.key] ?? f.default_item[sub.key], (v) => {
        item[sub.key] = v;
        set(items);
        if (controls.has(sub.key)) renderPartForm();
      }));
    }
    box.append(card);
  });
  if (!items.length && f.empty) box.append(el('p', { class: 'hint list-empty' }, f.empty));
  if (items.length < (f.max || 8)) {
    const add = el('button', { type: 'button', class: 'add' }, `+ ${f.add_label}`);
    add.addEventListener('click', () => {
      items.push(clone(f.default_item));
      set(items);
      renderPartForm();
    });
    box.append(add);
  }
  return el('div', { class: 'field wide', title: f.help }, box);
}

function fieldEl(f, value, set, showHelp) {
  const id = `f${uid++}`;
  let input;
  if (f.type === 'list') return listField(f, value, set);
  if (f.type === 'table') return tableField(f, value, set);
  if (f.type === 'part') return partField(f, value, set);
  if (f.type === 'select') {
    input = el('select', { id });
    for (const [v, label] of f.options) input.append(new Option(label, v, false, v === value));
    const numeric = typeof f.options[0]?.[0] === 'number';
    input.addEventListener('change', () => set(numeric ? Number(input.value) : input.value));
  } else if (f.type === 'airfoil') {
    input = airfoilInput(id, f, value, set);
  } else if (f.type === 'drawing') {
    return drawingField(f, value, set);
  } else if (f.type === 'bool') {
    input = el('input', { id, type: 'checkbox' });
    input.checked = Boolean(value);
    input.addEventListener('change', () => set(input.checked));
  } else if (f.type === 'text') {
    input = el('input', { id, type: 'text', spellcheck: 'false' });
    input.value = value ?? '';
    input.addEventListener('input', () => set(input.value.trim()));
  } else {
    input = numberInput(id, f, value, set);
  }
  const control = f.unit ? el('div', { class: 'unit-wrap' }, input, el('span', { class: 'unit' }, f.unit)) : input;
  const kind = { airfoil: ' airfoil', bool: ' bool' }[f.type] || '';
  const row = el('div', { class: `field${kind}`, title: f.help },
    el('label', { for: id }, f.label), control);
  if (showHelp && f.help) row.append(el('div', { class: 'help' }, f.help));
  return row;
}

function buildForm(container, fields, values, onChange, showHelp = false) {
  container.replaceChildren();
  let group = null;
  const controls = new Set(fields.flatMap((f) => Object.keys(f.show_if || {})));
  const rebuild = () => buildForm(container, fields, values, onChange, showHelp);
  for (const f of fields) {
    if (!Object.entries(f.show_if || {}).every(([k, v]) => matches(v, values[k]))) continue;
    if (f.group && f.group !== group) {
      group = f.group;
      container.append(el('div', { class: 'group-title' }, group));
    }
    container.append(fieldEl(f, values[f.key], (v) => {
      onChange(f.key, v);
      if (controls.has(f.key)) rebuild();
    }, showHelp));
  }
}

function renderPartForm() {
  buildForm($('#part-form'), state.info.schema.part[partType()] || [], state.project.part, (k, v) => {
    state.project.part[k] = v; changed();
  });
}

function renderPartType() {
  const box = $('#part-type');
  box.replaceChildren(...state.info.part_types.map((t) => {
    const b = el('button', { type: 'button', role: 'radio', 'aria-checked': String(t.type === partType()) }, t.label);
    b.addEventListener('click', () => switchPartType(t.type));
    return b;
  }));
}

function renderSettings() {
  renderPartType();
  $('#project-title').value = state.project.title || '';
  $('#project-title').placeholder = { wing: 'Wing panel', shape: '2D shape', fuselage: 'Fuselage' }[partType()] || 'Part';
  renderPartForm();
  buildForm($('#placement-form'), state.info.schema.placement[partType()] || [], state.project.placement, (k, v) => {
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
  const p = state.project.part;
  let faces;
  const views = (r.views || []).map((v) => [v.label, null, '', v.rings, v]);
  if (r.kind === 'wing') {
    // Root and tip sections, then the planform of a wing cut in panels
    faces = [['Root', 'root', r.stats.airfoils.root], ['Tip', 'tip', r.stats.airfoils.tip], ...views];
  } else if (views.length) {
    faces = views;
  } else {
    const tapered = (p.tip_scale ?? 100) !== 100 || p.tip_offset_x || p.tip_offset_y;
    faces = tapered ? [['Root', 'root', ''], ['Tip', 'tip', '']] : [['Shape', 'root', r.stats.drawing.name]];
  }
  for (const [label, face, name, own, view] of faces) {
    const rings = own || r.part.solids.flatMap((sd) => [sd[face].outer, ...sd[face].holes]);
    const skins = own ? [] : (r.part.skins || []).map((sk) => sk[face]);
    const pts = rings.flat().concat(skins.flat());
    const plan = Boolean(view && view.plan);
    const xs = pts.map((q) => q[0]); const ys = pts.map((q) => q[1]);
    const x0 = Math.min(...xs); const x1 = Math.max(...xs); const y0 = Math.min(...ys); const y1 = Math.max(...ys);
    // Airfoils are drawn leading edge left, as usual; drawings as they appear in the machine.
    const fx = r.kind === 'wing' && !plan ? (x) => x1 - x : (x) => x - x0;
    const toPath = (list) => list.map((ring) => ring.map((q, i) => `${i ? 'L' : 'M'}${fx(q[0]).toFixed(2)} ${(y1 - q[1]).toFixed(2)}`).join(' ') + 'Z').join(' ');
    const d = toPath(rings);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    const pad = Math.max(x1 - x0, y1 - y0) * 0.02;
    svg.setAttribute('viewBox', `${-pad} ${-pad} ${x1 - x0 + 2 * pad} ${y1 - y0 + 2 * pad}`);
    svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d);
    path.setAttribute('fill-rule', 'evenodd');
    svg.append(path);
    if (skins.length) {
      const skin = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      skin.setAttribute('d', toPath(skins));
      skin.setAttribute('class', 'skin');
      svg.append(skin);
    }
    // A planform: the panel being cut, and the joints between panels
    for (const [list, cls] of [[view?.marks, 'mark'], [view?.lines, 'joint']]) {
      if (!list || !list.length) continue;
      const extra = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      extra.setAttribute('d', toPath(list));
      extra.setAttribute('class', cls);
      svg.append(extra);
    }
    box.append(el('figure', { class: plan ? 'wide' : '' }, svg, el('figcaption', {}, name ? `${label} · ${name}` : label)));
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
    renderJobs(null);
    updateScrubber();
    return;
  }
  const s = r.stats; const a = r.machine.axis_letters;
  const xy = a.slice(0, 2); const uv = a.slice(2);
  const slow = s.speed_ratio.tip < s.speed_ratio.root ? 'tip' : 'root';
  const ends = r.kind === 'fuselage' ? { root: 'larger end', tip: 'smaller end' } : { root: 'root', tip: 'tip' };
  if (s.drawing) {
    state.drawingInfo = s.drawing;
    const meta = $('#drawing-meta');
    if (meta) meta.textContent = drawingSummary(s.drawing);
  }
  const rows = [
    ['Estimated time', fmtTime(s.time_s)],
    ...(s.drawing ? [['Drawing', drawingSummary(s.drawing)]] : []),
    ['Cut length', `${xy} ${num(s.cut_length.XY, 0)} · ${uv} ${num(s.cut_length.UV, 0)} mm`],
    ['Kerf', `${ends.root} ${num(s.kerf.root, 2)} · ${ends.tip} ${num(s.kerf.tip, 2)} mm`],
    ...(s.speed_ratio[slow] < 0.999 ? [['Slower end', `${ends[slow]}, ${Math.round(s.speed_ratio[slow] * 100)} % of cut speed`]] : []),
    ...(s.core && (s.core.te_cutback.root > 0.05 || s.core.te_cutback.tip > 0.05)
      ? [['Core ends before TE', `root ${num(s.core.te_cutback.root, 1)} · tip ${num(s.core.te_cutback.tip, 1)} mm`]] : []),
    ...(s.core && (s.core.le_cutback.root > 0.05 || s.core.le_cutback.tip > 0.05)
      ? [['Core starts behind LE', `root ${num(s.core.le_cutback.root, 1)} · tip ${num(s.core.le_cutback.tip, 1)} mm`]] : []),
    ...(s.core && (s.core.spars || s.core.channels || s.core.joiners)
      ? [['Spars · channels · joiners', `${s.core.spars} · ${s.core.channels} · ${s.core.joiners}`]] : []),
    ...(s.core?.bays || []).map((b) => [`Servo bay ${b.number}`,
      `${b.size.map((v) => num(v, 1).replace(/\.0$/, '')).join(' × ')} mm, ${b.side}; ${num(b.from_root, 1)} mm from the root, `
      + `${num(b.behind_front, 1)} mm behind the front${b.channels.length ? `; reaches channel ${b.channels.join(', ')}` : ''}`]),
    ...(s.planform ? [['Wing', `${s.planform.kind === 'panel' ? 'straight' : s.planform.kind}, ${num(s.planform.span, 0)} mm in ${s.planform.panels} panels`
      + (s.planform.kind === 'panel' ? '' : `, within ${num(s.planform.worst, 1)} mm of the outline`)],
    ['This panel', `${s.planform.panel} of ${s.planform.panels}: ${num(s.planform.from, 0)}–${num(s.planform.to, 0)} mm from the root`]] : []),
    ...(s.control ? [[s.control.name[0].toUpperCase() + s.control.name.slice(1),
      `${s.control.hinge === 'center' ? 'centre' : s.control.hinge} hinge, ${s.control.bevel ? `${num(s.control.bevel, 0)}° V` : 'straight cut'}; `
      + `${num(s.control.depth.root, 0)} → ${num(s.control.depth.tip, 0)} mm deep`]] : []),
    ...(s.nest ? [['Nested', `${s.nest.parts} part${s.nest.parts === 1 ? '' : 's'} in ${s.nest.columns} column${s.nest.columns === 1 ? '' : 's'}`]] : []),
    ...(s.fuselage ? [
      ['Fuselage', `${num(s.fuselage.length, 0)} × ${num(s.fuselage.width, 0)} × ${num(s.fuselage.height, 0)} mm · ${num(s.fuselage.volume_l, 2)} l`],
      ['This cut', `${r.job} of ${r.jobs.length}: ${s.fuselage.cut}`],
    ] : []),
    ['Peak carriage speed', `${num(s.peak_speed.XY, 0)} · ${num(s.peak_speed.UV, 0)} mm/min`],
    [`${xy} range`, `X ${num(s.extents.XY[2], 0)} · Y ${num(s.extents.XY[3], 0)} mm`],
    [`${uv} range`, `X ${num(s.extents.UV[2], 0)} · Y ${num(s.extents.UV[3], 0)} mm`],
    ['G-code', `${s.gcode_lines} lines`],
  ];
  for (const [k, v] of rows) stats.append(el('dt', {}, k), el('dd', {}, v));
  for (const w of r.warnings) warnings.append(el('li', {}, w));
  const hasBays = (r.part.bays || []).length > 0;
  $('#bay-row').hidden = !hasBays;
  $('#legend-bay').hidden = !hasBays;
  $('#legend-ghost').hidden = !(r.part.ghosts || []).length;
  $('#legend-ghost-text').textContent = r.part.ghost_label || 'Finished shape';
  renderJobs(r);

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

// Parts cut in several goes (fuselage passes and segments): pick the cut, see its steps.
function renderJobs(r) {
  // Keep the last list when a cut fails to slice, so another cut can still be picked.
  if (r) state.jobs = { type: r.kind, list: r.jobs || [] };
  const jobs = state.jobs && state.jobs.type === partType() ? state.jobs.list : [];
  const row = $('#job-row');
  row.hidden = jobs.length < 2;
  if (jobs.length >= 2) {
    const sel = $('#sel-job');
    sel.replaceChildren(...jobs.map((j, i) => new Option(`${i + 1}/${jobs.length} · ${j.label}`, String(i + 1))));
    sel.value = String(r ? r.job : Math.min(state.project.part.job || 1, jobs.length));
  }
  const notes = (r && r.notes) || [];
  const fuselage = partType() === 'fuselage';
  const current = r ? r.job : Number($('#sel-job').value || 1);
  $('#steps-card').hidden = !(notes.length || fuselage);
  $('#steps-pill').textContent = jobs.length > 1 ? `cut ${current} of ${jobs.length}` : '';
  $('#steps-pill').hidden = jobs.length < 2;
  // Illustrated steps: when they carry their own notes they say everything; otherwise the
  // job's notes come first and the drawings follow.
  const steps = (r && r.steps) || [];
  const narrated = steps.some((st) => (st.notes || []).length);
  $('#job-notes').replaceChildren(...(narrated ? [] : notes.map((n) => el('li', {}, n))));
  $('#job-notes').hidden = narrated || !notes.length;
  $('#job-steps').replaceChildren(...steps.map((st) => {
    const box = el('div', { class: 'step' });
    if (st.title) box.append(el('h3', {}, st.title));
    if (st.svg) {
      const doc = new DOMParser().parseFromString(st.svg, 'image/svg+xml');
      if (doc.documentElement.nodeName === 'svg') box.append(document.importNode(doc.documentElement, true));
    }
    if ((st.notes || []).length) box.append(el('ul', {}, ...st.notes.map((n) => el('li', {}, n))));
    return box;
  }));
  $('#btn-export-all').hidden = jobs.length < 2;
  for (const id of ['#btn-plans', '#btn-guide', '#btn-stl']) $(id).hidden = !fuselage;
}

async function exportAllCuts() {
  const jobs = state.result?.jobs || [];
  try {
    for (let i = 0; i < jobs.length; i++) {
      const project = clone(state.project);
      project.part.job = i + 1;
      const r = await api('/api/slice', { project });
      download(`${slug(projectName())}-${i + 1}-${slug(jobs[i].label)}.gcode`, r.gcode);
      await new Promise((ok) => setTimeout(ok, 250));
    }
  } catch (err) {
    alert(err.message);
  }
}

// Preview scrubber -------------------------------------------------------------------

function updateScrubber() {
  const r = state.shown || state.result;
  $('#scrubber').hidden = !(r && state.tab === 'preview');
  if (!r) return;
  const total = r.path.t.at(-1);
  const t = Math.min(state.scrub.t, total);
  $('#scrub').value = total ? Math.round((t / total) * 1000) : 1000;
  $('#scrub-time').textContent = `${clock(t)} / ${clock(total)}`;
  if (state.tab === 'preview') showTime(t);
}

function showTime(t) {
  const p = (state.shown || state.result).path;
  let lo = 0; let hi = p.t.length - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (p.t[mid] <= t) lo = mid; else hi = mid - 1; }
  const next = Math.min(lo + 1, p.t.length - 1);
  const span = p.t[next] - p.t[lo];
  state.viewer.setProgress(lo, span > 0 ? Math.min(1, (t - p.t[lo]) / span) : 0);
}

// Playback ----------------------------------------------------------------------------
// One cut plays from the scrubber's time to its end: the wire moves at `rate` seconds of cutting per
// second, stops hold for a moment with their message (the block turns, for a turned fuselage), and at
// the end the finished pieces lift out of the block. The timelapse plays every cut of the part in turn,
// fitted into about 15 seconds, and can be recorded as a video.

const TIMELAPSE_SECONDS = 15;
const frame = () => new Promise((ok) => requestAnimationFrame(ok));
const ease = (f) => f * f * (3 - 2 * f);

function showCaption(text) {
  state.caption = text || '';
  $('#caption').textContent = state.caption;
  $('#caption').hidden = !state.caption;
}

function setPlayButtons() {
  const run = state.run;
  $('#btn-play').textContent = run && run.kind === 'play' ? '❚❚' : '▶';
  $('#btn-play').title = run && run.kind === 'play' ? 'Pause' : 'Play';
  $('#btn-timelapse').textContent = run && run.kind === 'timelapse' && !run.record ? 'Stop' : 'Timelapse';
  $('#btn-video').textContent = run && run.record ? 'Stop recording' : 'Save video';
  $('#btn-timelapse').disabled = Boolean(run && run.record);
  $('#btn-video').disabled = Boolean(run && run.kind === 'timelapse' && !run.record) || !window.MediaRecorder;
}

function stopPlayback() {
  if (state.run) state.run.cancelled = true;
}

// Show the frames of `ms` milliseconds, calling step(f) with f from 0 to 1. False when cancelled.
async function animate(ms, run, step) {
  const start = performance.now();
  for (;;) {
    const now = await frame();
    if (run.cancelled) return false;
    const f = Math.min(1, (now - start) / ms);
    step(f);
    if (f >= 1) return true;
  }
}

async function playCut(rate, run) {
  const r = state.shown;
  const p = r.path;
  const total = p.t.at(-1);
  const stops = p.stops || [];
  let k = stops.findIndex((st) => st.t > state.scrub.t + 1e-6 || (run.fromStart && st.t >= state.scrub.t));
  if (k < 0) k = stops.length;
  const v = state.viewer;
  let last = performance.now();
  while (state.scrub.t < total) {
    const now = await frame();
    if (run.cancelled) return false;
    const t = state.scrub.t + ((now - last) / 1000) * rate;
    last = now;
    const stop = stops[k];
    if (stop && t >= stop.t) {  // hold here: say what happens, and turn the block
      state.scrub.t = stop.t;
      updateScrubber();
      showCaption(stop.message);
      const a = v.passAngle(k); const b = v.passAngle(k + 1);
      const ok = await animate(1700, run, (f) => {
        if (r.turn) v.setTurn(a + (b - a) * ease(Math.min(1, f * 1.4)));
        v.render();
      });
      showCaption(run.title || '');
      if (!ok) return false;
      k += 1;
      last = performance.now();
      continue;
    }
    state.scrub.t = Math.min(total, t);
    updateScrubber();
  }
  return animate(1800, run, (f) => v.explode(f));
}

async function togglePlay() {
  if (state.run) {
    const wasPlay = state.run.kind === 'play';
    stopPlayback();
    if (wasPlay) return;
    await frame();
  }
  if (!state.result) return;
  state.shown = state.result;
  const total = state.shown.path.t.at(-1);
  if (state.scrub.t >= total) state.scrub.t = 0;
  const run = { kind: 'play', cancelled: false, fromStart: state.scrub.t === 0 };
  state.run = run;
  setPlayButtons();
  await playCut(Number($('#play-speed').value), run);
  if (state.run === run) state.run = null;
  setPlayButtons();
}

// Every cut of the part, one after the other, in about TIMELAPSE_SECONDS; optionally recorded.
async function timelapse(record = false) {
  if (state.run) {
    const same = state.run.kind === 'timelapse';
    stopPlayback();
    if (same) return;
    await frame();
  }
  const current = state.result;
  if (!current) return;
  const run = { kind: 'timelapse', record, cancelled: false, fromStart: true };
  state.run = run;
  setPlayButtons();
  if (state.tab !== 'preview') setTab('preview');
  const jobs = current.jobs && current.jobs.length > 1 ? current.jobs : [{ label: '' }];
  const results = [];
  try {
    for (let k = 1; k <= jobs.length; k++) {
      if (jobs.length > 1) showCaption(`Preparing cut ${k} of ${jobs.length}…`);
      if (k === (current.job || 1)) { results.push(current); continue; }
      const project = clone(state.project);
      project.part.job = k;
      results.push(await api('/api/slice', { project }));
      if (run.cancelled) break;
    }
  } catch (err) {
    showCaption('');
    alert(err.message);
    state.run = null;
    setPlayButtons();
    return;
  }
  const total = results.reduce((sum, r) => sum + r.path.t.at(-1), 0);
  const rate = Math.max(1, total / TIMELAPSE_SECONDS);
  const recorder = record && !run.cancelled ? startRecording() : null;
  const name = state.project.title || { wing: 'Wing panel', shape: '2D shape', fuselage: 'Fuselage', nest: 'Block of parts' }[partType()];
  for (let k = 0; k < results.length && !run.cancelled; k++) {
    state.shown = results[k];
    state.viewer.setResult(results[k]);
    state.viewer.showCuts(true);
    state.scrub.t = 0;
    updateScrubber();
    run.title = results.length > 1 ? `Cut ${k + 1} of ${results.length} · ${jobs[k].label}` : name;
    showCaption(run.title);
    if (!(await animate(results.length > 1 ? 1400 : 900, run, () => state.viewer.render()))) break;
    if (!(await playCut(rate, run))) break;
    if (!(await animate(600, run, () => state.viewer.render()))) break;
  }
  showCaption('');
  if (recorder) await recorder.finish(`${slug(projectName())}-timelapse`);
  if (state.result === current && state.shown === current && !run.cancelled) {
    // One cut, played to the end: leave the pieces lifted out until the next move.
    $('#scrub-time').textContent = `${clock(current.path.t.at(-1))} / ${clock(current.path.t.at(-1))}`;
  } else if (state.result === current) {  // back to the cut that was showing
    state.shown = current;
    state.viewer.setResult(current);
    state.viewer.showCuts(state.tab === 'preview');
    state.scrub.t = Infinity;
    updateScrubber();
  } else {  // a new slice replaced it meanwhile
    state.shown = state.result;
    state.scrub.t = Infinity;
    updateScrubber();
  }
  if (state.run === run) state.run = null;
  setPlayButtons();
}

// Records the 3D view, with the caption and the clock drawn in, while the timelapse plays.
function startRecording() {
  const source = state.viewer.canvas;
  const canvas = document.createElement('canvas');
  canvas.width = source.width;
  canvas.height = source.height;
  const ctx = canvas.getContext('2d');
  const scale = canvas.width / Math.max(1, source.clientWidth);
  const draw = () => {
    ctx.drawImage(source, 0, 0);
    const pad = 14 * scale;
    ctx.font = `600 ${15 * scale}px system-ui, sans-serif`;
    ctx.textBaseline = 'middle';
    const lines = [];
    if (state.caption) lines.push(state.caption);
    const r = state.shown;
    if (r) lines.push(`${clock(Math.min(state.scrub.t, r.path.t.at(-1)))} / ${clock(r.path.t.at(-1))} of cutting`);
    lines.forEach((text, i) => {
      const w = Math.min(canvas.width - 2 * pad, ctx.measureText(text).width + 2 * pad);
      const y = canvas.height - pad - (lines.length - i) * 30 * scale;
      ctx.fillStyle = 'rgba(20, 24, 31, 0.72)';
      ctx.fillRect(pad, y, w, 26 * scale);
      ctx.fillStyle = '#ffffff';
      ctx.fillText(text, 2 * pad, y + 13 * scale, canvas.width - 4 * pad);
    });
    ctx.font = `600 ${13 * scale}px system-ui, sans-serif`;
    ctx.fillStyle = 'rgba(20, 24, 31, 0.6)';
    ctx.fillText('open_HOWI', pad, pad + 8 * scale);
  };
  draw();
  const type = ['video/mp4;codecs=avc1', 'video/mp4', 'video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm']
    .find((t) => MediaRecorder.isTypeSupported(t)) || '';
  const stream = canvas.captureStream(30);
  const recorder = new MediaRecorder(stream, type ? { mimeType: type, videoBitsPerSecond: 6e6 } : {});
  const chunks = [];
  recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  state.viewer.onFrame = draw;
  recorder.start(500);
  return {
    finish(base) {
      return new Promise((ok) => {
        recorder.onstop = () => {
          state.viewer.onFrame = null;
          const mime = recorder.mimeType || type || 'video/webm';
          const blob = new Blob(chunks, { type: mime });
          const a = el('a', { href: URL.createObjectURL(blob), download: `${base}.${mime.includes('mp4') ? 'mp4' : 'webm'}` });
          document.body.append(a);
          a.click();
          setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 2000);
          ok();
        };
        draw();
        recorder.stop();
      });
    },
  };
}

// Tabs ----------------------------------------------------------------------------

function setTab(tab) {
  state.tab = tab;
  for (const b of $$('.tabs button')) b.setAttribute('aria-selected', String(b.dataset.tab === tab));
  for (const p of $$('.panel')) p.hidden = p.dataset.panel !== tab;
  if (state.run && tab !== 'preview' && !state.run.record) stopPlayback();
  applyTabView();
  if (tab === 'device') pollDevice();
}

function applyTabView() {
  updateScrubber();
  const r = state.result;
  state.viewer.showCuts(state.tab === 'preview');
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
  // A pause written into the job (e.g. "turn the foam"): say what to do, then Resume.
  const prompt = job && job.state === 'paused' && job.message;
  const box = $('#job-prompt');
  const appeared = prompt && (box.hidden || $('#job-prompt-text').textContent !== job.message);
  box.hidden = !prompt;
  if (prompt) $('#job-prompt-text').textContent = job.message;
  if (appeared && state.tab === 'device') box.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
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
  startJob(r.gcode, state.project.title || { wing: 'Wing panel', shape: '2D shape', fuselage: 'Fuselage' }[partType()],
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
    const r = state.shown || state.result;
    if (!r) return;
    if (state.run && state.run.kind === 'play') stopPlayback();
    state.scrub.t = (Number(e.target.value) / 1000) * r.path.t.at(-1);
    updateScrubber();
  });
  $('#btn-play').addEventListener('click', togglePlay);
  $('#btn-timelapse').addEventListener('click', () => timelapse(false));
  $('#btn-video').addEventListener('click', () => (state.run && state.run.record ? stopPlayback() : timelapse(true)));

  $('#btn-export').addEventListener('click', () => {
    const r = state.result;
    if (!r) return;
    const suffix = (r.jobs || []).length > 1 ? `-${r.job}-${slug(r.jobs[r.job - 1].label)}` : '';
    download(`${slug(projectName())}${suffix}.gcode`, r.gcode);
  });
  $('#btn-send').addEventListener('click', () => { setTab('device'); startCurrentJob(); });

  $('#btn-new').addEventListener('click', () => {
    if (confirm('Start a new project? Unsaved changes are lost.')) setProject(clone(state.info.default_project));
  });
  $('#btn-save').addEventListener('click', () => {
    download(`${slug(projectName())}.howi.json`, JSON.stringify(state.project, null, 2), 'application/json');
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
  $('#btn-quit').hidden = !state.info.desktop;
  $('#btn-quit').addEventListener('click', async () => {
    if (!confirm('Quit open_HOWI? The machine is disconnected; unsaved changes stay in this browser.')) return;
    try { await api('/api/app/quit', {}); } catch (err) { alert(err.message); return; }
    document.body.replaceChildren(el('main', { class: 'quit-note' }, el('h1', {}, 'open_HOWI has stopped'),
      el('p', {}, 'You can close this tab. Start it again from its shortcut.')));
  });
  $('#sel-job').addEventListener('change', () => {
    state.project.part.job = Number($('#sel-job').value);
    changed();
  });
  $('#btn-export-all').addEventListener('click', exportAllCuts);
  $('#btn-guide').addEventListener('click', async () => {
    const tab = window.open('', '_blank');  // open now, while the click still counts as the user's
    try {
      const r = await api('/api/guide/fuselage', { project: state.project });
      const url = URL.createObjectURL(new Blob([r.html], { type: 'text/html' }));
      if (tab) tab.location.href = url;
      else download(r.filename.replace(/[^\w.-]+/g, '-'), r.html, 'text/html');
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch (err) {
      if (tab) tab.close();
      alert(err.message);
    }
  });
  $('#btn-stl').addEventListener('click', async () => {
    try {
      const r = await api('/api/export/fuselage-stl', { project: state.project });
      const bytes = Uint8Array.from(atob(r.stl), (c) => c.charCodeAt(0));
      const url = URL.createObjectURL(new Blob([bytes], { type: 'model/stl' }));
      const a = el('a', { href: url, download: r.filename.replace(/[^\w.-]+/g, '-') });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      alert(err.message);
    }
  });
  $('#btn-plans').addEventListener('click', async () => {
    try {
      const r = await api('/api/template/fuselage', { project: state.project });
      download(r.filename.replace(/[^\w.-]+/g, '-'), r.svg, 'image/svg+xml');
    } catch (err) {
      alert(err.message);
    }
  });
  $('#btn-bays').addEventListener('click', async () => {
    try {
      const r = await api('/api/template/bays', { project: state.project });
      download(r.filename.replace(/[^\w.-]+/g, '-'), r.svg, 'image/svg+xml');
    } catch (err) {
      alert(err.message);
    }
  });

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
  setPlayButtons();
  renderSettings();
  renderDevice();
  changed();
  refreshPorts();
  setInterval(pollDevice, 300);
}

init().catch((err) => showBanner(`could not start: ${err.message}`));
