// SPDX-License-Identifier: GPL-3.0-or-later
// 3D view of the machine: work area, foam block, part, carriage paths and the wire.
// Scene axes: x = machine X (towards the foam), y = machine Y (up), z = position along the wire.
import * as THREE from './vendor/three/three.module.min.js';
import { OrbitControls } from './vendor/three/OrbitControls.js';

function cssColor(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return new THREE.Color(v || fallback);
}

function disposeGroup(group) {
  for (const child of [...group.children]) {
    group.remove(child);
    child.traverse((o) => {
      o.geometry?.dispose();
      if (o.material) [].concat(o.material).forEach((m) => { m.map?.dispose(); m.dispose(); });
    });
  }
}

function textSprite(text, color) {
  const canvas = document.createElement('canvas');
  canvas.width = 128; canvas.height = 64;
  const ctx = canvas.getContext('2d');
  ctx.font = '600 40px system-ui, sans-serif';
  ctx.fillStyle = color;
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(text, 64, 34);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(canvas), depthTest: false }));
  sprite.scale.set(60, 30, 1);
  return sprite;
}

export class Viewer {
  constructor(canvas) {
    this.canvas = canvas;
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(40, 1, 1, 50000);
    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = false;
    this.controls.addEventListener('change', () => this.render());
    this.controls.addEventListener('start', () => { this.userMoved = true; });

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x8899aa, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(0.6, 1, 0.8);
    this.scene.add(sun);

    this.machineGroup = new THREE.Group();
    this.partGroup = new THREE.Group();
    this.pathGroup = new THREE.Group();
    this.scene.add(this.machineGroup, this.partGroup, this.pathGroup);

    this.wire = new THREE.Mesh(new THREE.CylinderGeometry(1.6, 1.6, 1, 8), new THREE.MeshBasicMaterial());
    this.carriages = [0, 1].map(() => new THREE.Mesh(new THREE.BoxGeometry(14, 14, 14), new THREE.MeshStandardMaterial()));
    this.scene.add(this.wire, ...this.carriages);

    this.machine = { tower_distance: 1000, x_travel: 600, y_travel: 300 };
    this.path = null;
    this.readColors();
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      this.readColors(); this.buildMachine(); this.rebuild(); this.render();
    });
    new ResizeObserver(() => this.resize()).observe(canvas.parentElement);
    this.buildMachine();
    this.setWire([0, 0], [0, 0]);
    this.view('iso');
  }

  readColors() {
    this.colors = {
      bg: cssColor('--view-bg', '#e9edf2'), grid: cssColor('--view-grid', '#c9d0d9'),
      frame: cssColor('--view-frame', '#8d97a3'), xy: cssColor('--xy', '#1c7ed6'), uv: cssColor('--uv', '#9c36b5'),
      wire: cssColor('--wire', '#f03e3e'), part: cssColor('--part', '#f0dcae'), block: cssColor('--block', '#9fb3c8'),
      bay: cssColor('--bay', '#e03131'),
      muted: cssColor('--muted', '#66717e'),
    };
    this.scene.background = this.colors.bg;
    this.wire.material.color = this.colors.wire;
    this.carriages[0].material.color = this.colors.xy;
    this.carriages[1].material.color = this.colors.uv;
  }

  resize() {
    const { clientWidth: w, clientHeight: h } = this.canvas.parentElement;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    if (!this.userMoved) this.view(this.currentView || 'iso');
    else this.render();
  }

  render() {
    this.renderer.render(this.scene, this.camera);
    if (this.onFrame) this.onFrame(this.canvas);  // e.g. a video recorder copying the frame
  }

  setMachine(machine) {
    const changed = ['tower_distance', 'x_travel', 'y_travel'].some((k) => machine[k] !== this.machine[k]);
    this.machine = { ...machine };
    if (changed) { this.buildMachine(); this.view(this.currentView || 'iso'); }
  }

  buildMachine() {
    disposeGroup(this.machineGroup);
    const { tower_distance: D, x_travel: X, y_travel: Y } = this.machine;
    const grid = [];
    const step = X > 800 || D > 1500 ? 100 : 50;
    for (let x = 0; x <= X + 1e-6; x += step) grid.push(x, 0, 0, x, 0, D);
    for (let z = 0; z <= D + 1e-6; z += step) grid.push(0, 0, z, X, 0, z);
    const gridGeom = new THREE.BufferGeometry();
    gridGeom.setAttribute('position', new THREE.Float32BufferAttribute(grid, 3));
    this.machineGroup.add(new THREE.LineSegments(gridGeom, new THREE.LineBasicMaterial({ color: this.colors.grid })));

    const frame = [];
    for (const z of [0, D]) {
      frame.push(0, 0, z, X, 0, z, X, 0, z, X, Y, z, X, Y, z, 0, Y, z, 0, Y, z, 0, 0, z);
    }
    frame.push(0, Y, 0, 0, Y, D, X, Y, 0, X, Y, D);
    const frameGeom = new THREE.BufferGeometry();
    frameGeom.setAttribute('position', new THREE.Float32BufferAttribute(frame, 3));
    this.machineGroup.add(new THREE.LineSegments(frameGeom, new THREE.LineBasicMaterial({ color: this.colors.frame })));

    const labels = [['XY', this.colors.xy, 0], ['UV', this.colors.uv, D]];
    for (const [text, color, z] of labels) {
      const s = textSprite(text, `#${color.getHexString()}`);
      s.position.set(-30, Y + 20, z);
      this.machineGroup.add(s);
    }
    const origin = textSprite('0', `#${this.colors.muted.getHexString()}`);
    origin.position.set(-18, -12, 0);
    origin.scale.set(36, 18, 1);
    this.machineGroup.add(origin);
  }

  view(name) {
    this.currentView = name;
    this.userMoved = false;
    const { tower_distance: D, x_travel: X, y_travel: Y } = this.machine;
    const target = new THREE.Vector3(X / 2, Y / 2, D / 2);
    const dir = {
      iso: new THREE.Vector3(0.75, 0.55, 0.75),
      profile: new THREE.Vector3(0, 0, 1),
      top: new THREE.Vector3(0, 1, 0.001),
      side: new THREE.Vector3(1, 0, 0),
    }[name].normalize();
    // Fit the bounding sphere of the work volume into the narrower field of view.
    const radius = 0.5 * (name === 'profile' ? Math.hypot(X, Y) : Math.hypot(X, Y, D));
    const vfov = THREE.MathUtils.degToRad(this.camera.fov);
    const hfov = 2 * Math.atan(Math.tan(vfov / 2) * this.camera.aspect);
    // The sphere is conservative when height limits the view; when width does (narrow screens),
    // the box spans almost the whole sphere, so keep a small margin instead.
    const dist = (radius / Math.sin(Math.min(vfov, hfov) / 2)) * (hfov < vfov ? 1.04 : 0.92);
    if (name === 'profile') target.z = D;
    this.camera.position.copy(target).addScaledVector(dir, dist);
    this.controls.target.copy(target);
    this.controls.update();
    this.render();
  }

  clearPart() {
    disposeGroup(this.partGroup);
    disposeGroup(this.pathGroup);
    this.path = null;
    this.render();
  }

  setResult(result) {
    this.result = result;
    this.setMachine(result.machine);
    this.rebuild();
  }

  rebuild() {
    disposeGroup(this.partGroup);
    disposeGroup(this.pathGroup);
    this.pieces = [];
    this.cuts = [];
    this.turn = null;
    const r = this.result;
    if (!r) { this.render(); return; }
    const D = r.machine.tower_distance;

    // The block and everything in it sit in a pivot, so a block turned between passes (a turned
    // fuselage) can turn about its axis, which runs along X.
    this.turn = r.turn || null;
    this.pivot = new THREE.Group();
    this.content = new THREE.Group();
    if (this.turn) {
      this.pivot.position.set(0, this.turn.y, this.turn.z);
      this.content.position.set(0, -this.turn.y, -this.turn.z);
    }
    this.pivot.add(this.content);
    this.partGroup.add(this.pivot);

    // Foam block
    const b = r.block;
    const box = new THREE.BoxGeometry(b.x1 - b.x0, b.y1 - b.y0, b.z1 - b.z0);
    box.translate((b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2, (b.z0 + b.z1) / 2);
    this.content.add(new THREE.Mesh(box, new THREE.MeshStandardMaterial({
      color: this.colors.block, transparent: true, opacity: 0.12, depthWrite: false,
    })));
    this.content.add(new THREE.LineSegments(new THREE.EdgesGeometry(box),
      new THREE.LineBasicMaterial({ color: this.colors.block, transparent: true, opacity: 0.7 })));

    // Part: each solid is a loft between its root and tip faces (outline and holes), plus end caps
    // With a finished shape to show inside it (a fuselage pass), the part cut by this job is see-through.
    const ghosted = (r.part.ghosts || []).length > 0;
    const partMat = new THREE.MeshStandardMaterial({
      color: this.colors.part, roughness: 0.85, side: THREE.DoubleSide, flatShading: r.kind === 'shape',
      ...(ghosted ? { transparent: true, opacity: 0.28, depthWrite: false } : {}),
    });
    // Nested parts each have their own root and tip positions
    for (const solid of r.part.solids) {
      const obj = this.addSolid(solid, solid.z_root ?? r.part.z_root, solid.z_tip ?? r.part.z_tip, partMat);
      if (!ghosted) this.pieces.push({ obj, role: solid.role || 'part' });
    }
    // Servo bays (cut by hand): the pocket, standing slightly proud of the surface so it shows
    const bayMat = new THREE.MeshStandardMaterial({
      color: this.colors.bay, roughness: 0.7, side: THREE.DoubleSide, transparent: true, opacity: 0.85,
    });
    for (const bay of r.part.bays || []) this.addSolid(bay, bay.z0, bay.z1, bayMat);
    // The finished shape (e.g. a fuselage after sanding), faint, lofted through its sections
    const ghostMat = new THREE.MeshStandardMaterial({ color: this.colors.part, roughness: 0.6, side: THREE.DoubleSide });
    for (const rings of r.part.ghosts || []) this.pieces.push({ obj: this.addLoft(rings, ghostMat), role: 'part' });
    // Finished outline (e.g. the sheeted airfoil) around the core, at both faces
    const skinMat = new THREE.LineBasicMaterial({ color: this.colors.frame, transparent: true, opacity: 0.9 });
    for (const skin of r.part.skins || []) {
      for (const [pts, z] of [[skin.root, skin.z_root ?? r.part.z_root], [skin.tip, skin.z_tip ?? r.part.z_tip]]) {
        const geom = new THREE.BufferGeometry();
        geom.setAttribute('position', new THREE.Float32BufferAttribute(pts.flatMap(([x, y]) => [x, y, z]), 3));
        this.content.add(new THREE.LineLoop(geom, skinMat));
      }
    }

    // Carriage paths: faint full path, bright overlay for the part already cut
    const p = r.path;
    this.path = p;
    this.overlays = [];
    for (const [key, z, color] of [['xy', 0, this.colors.xy], ['uv', D, this.colors.uv]]) {
      const geom = new THREE.BufferGeometry();
      geom.setAttribute('position', new THREE.Float32BufferAttribute(p[key].flatMap(([x, y]) => [x, y, z]), 3));
      this.pathGroup.add(new THREE.Line(geom, new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.3 })));
      const done = new THREE.Line(geom.clone(), new THREE.LineBasicMaterial({ color }));
      this.pathGroup.add(done);
      this.overlays.push(done);
    }
    this.buildCuts(r);
    this.setProgress(p.t.length - 1, 1);
  }

  // The surface the wire sweeps through the foam (from one end of the block to the other), drawn as far
  // as the cut has got. Each pass between turns of the block is its own mesh, fixed to the block.
  buildCuts(r) {
    const p = r.path;
    const D = r.machine.tower_distance;
    const { z0, z1 } = r.block;
    const stops = p.stops || [];
    this.cutMat = new THREE.MeshStandardMaterial({
      color: this.colors.wire, roughness: 0.9, side: THREE.DoubleSide, transparent: true, opacity: 0.38, depthWrite: false,
    });
    const at = (i, z) => {
      const [ax, ay] = p.xy[i]; const [bx, by] = p.uv[i];
      return [ax + (bx - ax) * (z / D), ay + (by - ay) * (z / D), z];
    };
    const bounds = [0, ...stops.map((st) => st.i), p.t.length - 1];
    for (let k = 0; k + 1 < bounds.length; k++) {
      const from = bounds[k]; const to = bounds[k + 1];
      const verts = []; const idx = []; const counts = [0];
      for (let i = from + 1; i <= to; i++) {
        if (p.cut[i]) {
          const n = verts.length / 3;
          verts.push(...at(i - 1, z0), ...at(i - 1, z1), ...at(i, z0), ...at(i, z1));
          idx.push(n, n + 1, n + 2, n + 1, n + 3, n + 2);
        }
        counts.push(idx.length);
      }
      const geom = new THREE.BufferGeometry();
      geom.setAttribute('position', new THREE.Float32BufferAttribute(verts, 3));
      geom.setIndex(idx);
      geom.computeVertexNormals();
      const mesh = new THREE.Mesh(geom, this.cutMat);
      // Cut while the block was turned by this pass's angle: undo that turn, so it moves with the block.
      const angle = this.passAngle(k);
      let holder = mesh;
      if (this.turn && angle) {
        holder = new THREE.Group();
        holder.position.set(0, this.turn.y, this.turn.z);
        holder.rotation.x = -THREE.MathUtils.degToRad(angle);
        const back = new THREE.Group();
        back.position.set(0, -this.turn.y, -this.turn.z);
        back.add(mesh);
        holder.add(back);
      }
      this.content.add(holder);
      this.cuts.push({ geom, from, to, counts });
    }
    this.showCuts(this.cutsShown ?? false);
  }

  // How far the block is turned during pass k (0 for the first), in degrees.
  passAngle(k) {
    if (!this.turn || k <= 0) return 0;
    const st = (this.path.stops || [])[k - 1];
    return st && st.angle != null ? st.angle : k * this.turn.step;
  }

  // Which pass segment `i` belongs to (the pass ending at a stop includes the stop's point).
  passAt(i) {
    return (this.path.stops || []).filter((st) => st.i < i).length;
  }

  showCuts(on) {
    this.cutsShown = on;
    for (const c of this.cuts || []) c.geom.setDrawRange(0, on ? c.geom.index.count : 0);
    if (this.cutMat) this.cutMat.visible = on;
    if (this.path && on) this.setProgress(this.progress ?? this.path.t.length - 1, 1);
    else this.render();
  }

  setTurn(angle) {
    if (this.pivot) this.pivot.rotation.x = THREE.MathUtils.degToRad(angle);
  }

  // The finished pieces lifted out of the block (f from 0 to 1); a control surface drops off its panel.
  explode(f) {
    const r = this.result;
    if (!r || !this.pieces) return;
    const lift = (r.block.y1 - r.block.y0) + 30;
    const e = f * f * (3 - 2 * f);  // ease in and out
    this.pieces.forEach(({ obj, role }, k) => {
      const delay = Math.min(0.4, k * 0.08);
      const g = Math.max(0, Math.min(1, (e - delay) / (1 - delay)));
      if (role === 'surface') obj.position.set(-12 * g, (lift - 14) * g, 0);
      else obj.position.set(0, lift * g, 0);
    });
    if (this.turn && this.path) {  // the last turn brings the block back upright to lift the part out
      const last = this.passAngle((this.path.stops || []).length);
      this.setTurn(last + (this.turn.step) * Math.min(1, f * 2.5));
    }
    if (this.cutMat) this.cutMat.opacity = 0.38 * (1 - 0.6 * e);
    this.render();
  }

  // A surface through rings of 3D points with the same count.
  addLoft(rings, material) {
    if (rings.length < 2) return;
    const m = rings[0].length;
    const verts = rings.flat(2);
    const idx = [];
    for (let k = 0; k + 1 < rings.length; k++) {
      for (let i = 0; i < m; i++) {
        const a = k * m + i; const b = k * m + ((i + 1) % m); const c = a + m; const d = b + m;
        idx.push(a, b, c, b, d, c);
      }
    }
    const geom = new THREE.BufferGeometry();
    geom.setAttribute('position', new THREE.Float32BufferAttribute(verts, 3));
    geom.setIndex(idx);
    geom.computeVertexNormals();
    const mesh = new THREE.Mesh(geom, material);
    (this.content || this.partGroup).add(mesh);
    return mesh;
  }

  addSolid(solid, zr, zt, material) {
    const group = new THREE.Group();
    (this.content || this.partGroup).add(group);
    // Rings without a repeated end point; root and tip rings are index-matched.
    const ring = (pts) => (pts.length > 2 && pts[0][0] === pts.at(-1)[0] && pts[0][1] === pts.at(-1)[1] ? pts.slice(0, -1) : pts);
    const rootRings = [solid.root.outer, ...solid.root.holes].map(ring);
    const tipRings = [solid.tip.outer, ...solid.tip.holes].map(ring);
    rootRings.forEach((rootRing, k) => {
      const tipRing = tipRings[k];
      const n = rootRing.length;
      const verts = [];
      for (const [pts, z] of [[rootRing, zr], [tipRing, zt]]) for (const [x, y] of pts) verts.push(x, y, z);
      const idx = [];
      for (let i = 0; i < n; i++) {
        const j = (i + 1) % n;
        idx.push(i, j, n + i, j, n + j, n + i);
      }
      const side = new THREE.BufferGeometry();
      side.setAttribute('position', new THREE.Float32BufferAttribute(verts, 3));
      side.setIndex(idx);
      side.computeVertexNormals();
      group.add(new THREE.Mesh(side, material));
    });
    for (const [rings, z] of [[rootRings, zr], [tipRings, zt]]) {
      const [outer, ...holes] = rings.map((pts) => pts.map(([x, y]) => new THREE.Vector2(x, y)));
      const tris = THREE.ShapeUtils.triangulateShape(outer, holes);
      const all = [outer, ...holes].flat();
      const cap = new THREE.BufferGeometry();
      cap.setAttribute('position', new THREE.Float32BufferAttribute(all.flatMap((p) => [p.x, p.y, z]), 3));
      cap.setIndex(tris.flat());
      cap.computeVertexNormals();
      group.add(new THREE.Mesh(cap, material));
    }
    return group;
  }

  // Show the cut up to segment `i` (index into path arrays), `frac` of the way into it.
  setProgress(i, frac) {
    const p = this.path;
    if (!p) return;
    i = Math.max(0, Math.min(i, p.t.length - 1));
    this.progress = i;
    for (const line of this.overlays) line.geometry.setDrawRange(0, i + 1);
    if (this.cutsShown) {
      for (const c of this.cuts) c.geom.setDrawRange(0, i <= c.from ? 0 : c.counts[Math.min(i, c.to) - c.from]);
    }
    if (this.turn) this.setTurn(this.passAngle(this.passAt(i)));
    if (this.pieces) for (const { obj } of this.pieces) obj.position.set(0, 0, 0);
    // While the cut is under way the swept surface shows clearly; once it is done it fades, so the part shows.
    if (this.cutMat) this.cutMat.opacity = i >= p.t.length - 1 ? 0.12 : 0.38;
    const lerp = (a, b) => [a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac];
    const j = Math.min(i + 1, p.t.length - 1);
    this.setWire(lerp(p.xy[i], p.xy[j]), lerp(p.uv[i], p.uv[j]));
  }

  setWire(xy, uv) {
    const D = this.machine.tower_distance;
    const a = new THREE.Vector3(xy[0], xy[1], 0);
    const b = new THREE.Vector3(uv[0], uv[1], D);
    const dir = b.clone().sub(a);
    this.wire.scale.set(1, dir.length(), 1);
    this.wire.position.copy(a.clone().add(b).multiplyScalar(0.5));
    this.wire.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir.normalize());
    this.carriages[0].position.copy(a);
    this.carriages[1].position.copy(b);
    this.render();
  }
}
