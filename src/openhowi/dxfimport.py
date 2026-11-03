# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the outlines in an ASCII DXF file as polylines in millimetres.

Supports LINE, LWPOLYLINE and POLYLINE (with arc bulges), CIRCLE, ARC,
ELLIPSE, SPLINE and INSERT (blocks), the ``$INSUNITS`` drawing units and
mirrored (extrusion -Z) entities. Text, dimensions and hatches are skipped.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from . import curves
from .drawing import Drawing, Polyline, ShapeError
from .geometry import Point, polyline_length

# $INSUNITS code -> millimetres per drawing unit (0 = unitless, read as mm)
INSUNITS_MM = {0: 1.0, 1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0, 8: 25.4e-6, 9: 0.0254, 10: 914.4, 13: 0.001,
               14: 100.0}  # fmt: skip
GEOMETRY = {"LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "ELLIPSE", "SPLINE", "INSERT"}
QUIET = {"VERTEX", "SEQEND", "VIEWPORT", "ATTRIB", "ATTDEF"}  # handled elsewhere or irrelevant
MAX_INSERT_DEPTH = 8
MAX_ENTITIES = 500_000

Matrix = tuple[float, float, float, float, float, float]  # a b c d e f: x' = a x + c y + e, y' = b x + d y + f


@dataclass
class Entity:
    kind: str
    tags: list[tuple[int, str]]
    vertices: list[Entity] = field(default_factory=list)  # POLYLINE's VERTEX entities

    def get(self, code: int, default: str | None = None) -> str | None:
        for c, v in self.tags:
            if c == code:
                return v
        return default

    def num(self, code: int, default: float = 0.0) -> float:
        value = self.get(code)
        try:
            return default if value is None else float(value)
        except ValueError as exc:
            raise ShapeError(f"bad number {value!r} (group {code}) in {self.kind}") from exc

    def all(self, code: int) -> list[float]:
        try:
            return [float(v) for c, v in self.tags if c == code]
        except ValueError as exc:
            raise ShapeError(f"bad number (group {code}) in {self.kind}") from exc

    @property
    def mirrored(self) -> bool:
        """Entity drawn with extrusion direction -Z (common after mirroring in CAD)."""
        return self.num(230, 1.0) < 0


def _pairs(text: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    if len(lines) % 2:
        lines = lines[:-1]
    pairs = []
    for i in range(0, len(lines), 2):
        try:
            code = int(lines[i].strip())
        except ValueError as exc:
            raise ShapeError(f"not a valid ASCII DXF file (line {i + 1}: {lines[i].strip()[:20]!r})") from exc
        pairs.append((code, lines[i + 1].strip()))
    return pairs


def _entities(pairs: list[tuple[int, str]]) -> list[Entity]:
    out: list[Entity] = []
    for code, value in pairs:
        if code == 0:
            out.append(Entity(value, []))
            if len(out) > MAX_ENTITIES:
                raise ShapeError("the DXF file is too large")
        elif out:
            out[-1].tags.append((code, value))
    # Attach VERTEX records to their POLYLINE
    merged: list[Entity] = []
    owner: Entity | None = None
    for ent in out:
        if ent.kind == "VERTEX" and owner is not None:
            owner.vertices.append(ent)
            continue
        owner = ent if ent.kind == "POLYLINE" else (None if ent.kind == "SEQEND" else owner)
        merged.append(ent)
    return merged


def _sections(entities: list[Entity]) -> dict[str, list[Entity]]:
    sections: dict[str, list[Entity]] = {}
    current: list[Entity] | None = None
    for ent in entities:
        if ent.kind == "SECTION":
            current = sections.setdefault(ent.get(2, "") or "", [])
        elif ent.kind == "ENDSEC":
            current = None
        elif current is not None:
            current.append(ent)
    return sections


def _header_units(pairs: list[tuple[int, str]]) -> float:
    for i, (code, value) in enumerate(pairs):
        if code == 9 and value == "$INSUNITS" and i + 1 < len(pairs):
            try:
                return INSUNITS_MM.get(int(pairs[i + 1][1]), 1.0)
            except ValueError:
                return 1.0
    return 1.0


def _apply(m: Matrix, p: Point) -> Point:
    return (m[0] * p[0] + m[2] * p[1] + m[4], m[1] * p[0] + m[3] * p[1] + m[5])


def _multiply(m: Matrix, n: Matrix) -> Matrix:
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a * a2 + c * b2,
        b * a2 + d * b2,
        a * c2 + c * d2,
        b * c2 + d * d2,
        a * e2 + c * f2 + e,
        b * e2 + d * f2 + f,
    )


MIRROR_X: Matrix = (-1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _bulged(points: list[Point], bulges: list[float], closed: bool, tol: float) -> Polyline:
    out = [points[0]]
    count = len(points) if closed else len(points) - 1
    for i in range(count):
        out += curves.bulge(points[i], points[(i + 1) % len(points)], bulges[i], tol)
    return Polyline(out, closed)


def _entity_paths(ent: Entity, tol: float, warnings: list[str]) -> list[Polyline]:
    kind = ent.kind
    if kind == "LINE":
        return [Polyline([(ent.num(10), ent.num(20)), (ent.num(11), ent.num(21))])]
    if kind == "LWPOLYLINE":
        points: list[Point] = []
        bulges: list[float] = []
        for code, value in ent.tags:
            if code == 10:
                points.append((float(value), 0.0))
                bulges.append(0.0)
            elif code == 20 and points:
                points[-1] = (points[-1][0], float(value))
            elif code == 42 and bulges:
                bulges[-1] = float(value)
        if len(points) < 2:
            return []
        return [_bulged(points, bulges, bool(int(ent.num(70)) & 1), tol)]
    if kind == "POLYLINE":
        flags = int(ent.num(70))
        if flags & (16 | 64):  # 3D polygon mesh / polyface mesh
            warnings.append("Skipped a 3D mesh POLYLINE.")
            return []
        verts = [v for v in ent.vertices if not int(v.num(70)) & (16 | 128)]
        if len(verts) < 2:
            return []
        return [_bulged([(v.num(10), v.num(20)) for v in verts], [v.num(42) for v in verts], bool(flags & 1), tol)]
    if kind == "CIRCLE":
        cx, cy, r = ent.num(10), ent.num(20), ent.num(40)
        if r <= 0:
            return []
        return [Polyline([(cx + r, cy)] + curves.arc((cx, cy), r, 0.0, 2 * math.pi, tol), True)]
    if kind == "ARC":
        cx, cy, r = ent.num(10), ent.num(20), ent.num(40)
        a0, a1 = math.radians(ent.num(50)), math.radians(ent.num(51))
        sweep = (a1 - a0) % (2 * math.pi) or 2 * math.pi
        start = (cx + r * math.cos(a0), cy + r * math.sin(a0))
        return [Polyline([start] + curves.arc((cx, cy), r, a0, sweep, tol))]
    if kind == "ELLIPSE":
        c = (ent.num(10), ent.num(20))
        major = (ent.num(11), ent.num(21))
        ratio = ent.num(40, 1.0)
        sign = -1.0 if ent.mirrored else 1.0  # ELLIPSE points are in world coordinates
        minor = (-major[1] * ratio * sign, major[0] * ratio * sign)
        t0, t1 = ent.num(41, 0.0), ent.num(42, 2 * math.pi)
        if t1 <= t0:
            t1 += 2 * math.pi
        start = (c[0] + math.cos(t0) * major[0] + math.sin(t0) * minor[0],
                 c[1] + math.cos(t0) * major[1] + math.sin(t0) * minor[1])  # fmt: skip
        full = abs(t1 - t0 - 2 * math.pi) < 1e-9
        return [Polyline([start] + curves.ellipse(c, major, minor, t0, t1, tol), full)]
    if kind == "SPLINE":
        degree = int(ent.num(71, 3))
        knots = ent.all(40)
        weights = ent.all(41)
        ctrl = list(zip(ent.all(10), ent.all(20)))
        fit = list(zip(ent.all(11), ent.all(21)))
        closed = bool(int(ent.num(70)) & 1)
        if ctrl:
            try:
                extra = int(math.sqrt(polyline_length(ctrl) / max(tol, 1e-6)))
                samples = max(32, min(4000, 8 * len(ctrl) + extra))
                pts = curves.bspline(ctrl, knots, degree, weights or None, samples)
            except ValueError:
                warnings.append("A SPLINE had an inconsistent definition; its control polygon was used instead.")
                pts = ctrl
        elif len(fit) >= 2:
            pts = fit
        else:
            return []
        return [Polyline(pts, closed)]
    return []


def parse_dxf(text: str, tol: float = 0.05) -> Drawing:
    if text.startswith("AutoCAD Binary DXF"):
        raise ShapeError("binary DXF is not supported; save the file as ASCII DXF")
    pairs = _pairs(text)
    unit_mm = _header_units(pairs)
    sections = _sections(_entities(pairs))
    if "ENTITIES" not in sections:
        raise ShapeError("no ENTITIES section found; is this a DXF file?")

    blocks: dict[str, tuple[Point, list[Entity]]] = {}
    current: list[Entity] | None = None
    for ent in sections.get("BLOCKS", []):
        if ent.kind == "BLOCK":
            current = []
            blocks[ent.get(2, "") or ""] = ((ent.num(10), ent.num(20)), current)
        elif ent.kind == "ENDBLK":
            current = None
        elif current is not None:
            current.append(ent)

    drawing = Drawing()
    skipped: Counter[str] = Counter()
    tol_units = tol / unit_mm
    scale_mm: Matrix = (unit_mm, 0.0, 0.0, unit_mm, 0.0, 0.0)

    visits = [MAX_ENTITIES]

    def emit(entities: list[Entity], m: Matrix, depth: int) -> None:
        for ent in entities:
            visits[0] -= 1
            if visits[0] < 0:
                raise ShapeError("the DXF file is too complex (too many nested block inserts)")
            if ent.kind == "INSERT":
                block = blocks.get(ent.get(2, "") or "")
                if block is None or depth >= MAX_INSERT_DEPTH:
                    skipped["INSERT"] += 1
                    continue
                (bx, by), children = block
                sx, sy = ent.num(41, 1.0), ent.num(42, 1.0)
                rot = math.radians(ent.num(50))
                cos_r, sin_r = math.cos(rot), math.sin(rot)
                local: Matrix = (sx * cos_r, sx * sin_r, -sy * sin_r, sy * cos_r, ent.num(10), ent.num(20))
                local = _multiply(local, (1.0, 0.0, 0.0, 1.0, -bx, -by))
                if ent.mirrored:
                    local = _multiply(MIRROR_X, local)
                emit(children, _multiply(m, local), depth + 1)
                continue
            if ent.kind not in GEOMETRY:
                if ent.kind not in QUIET:
                    skipped[ent.kind] += 1
                continue
            em = _multiply(m, MIRROR_X) if ent.mirrored and ent.kind in ("ARC", "CIRCLE", "LWPOLYLINE") else m
            for poly in _entity_paths(ent, tol_units, drawing.warnings):
                drawing.polylines.append(Polyline([_apply(em, p) for p in poly.points], poly.closed))
                if len(drawing.polylines) > MAX_ENTITIES:
                    raise ShapeError("the DXF file is too complex")

    emit(sections["ENTITIES"], scale_mm, 0)
    if skipped:
        kinds = ", ".join(f"{n} {k}" for k, n in sorted(skipped.items()))
        drawing.warnings.append(f"Skipped entities that are not outlines: {kinds}.")
    return drawing
