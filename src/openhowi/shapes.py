# SPDX-License-Identifier: GPL-3.0-or-later
"""2D shapes from DXF/SVG drawings: outlines, holes, kerf and the cut path.

A hot wire cannot be lifted, so every outline is reached by cutting through
foam. Each shape is entered from behind the block (X = 0) along a horizontal
lead-in that avoids the other shapes. Holes are cut on the way round the
outline through a short "bridge" cut from the nearest point of the outline;
that leaves one kerf-wide slit in the part per hole, to be glued afterwards.
"""

from __future__ import annotations

import hashlib
import math
from collections import OrderedDict
from dataclasses import dataclass, field

from .drawing import Drawing, Polyline, ShapeError
from .geometry import (
    Point,
    bounds,
    dedupe,
    distance,
    offset_polygon,
    point_in_polygon,
    polyline_length,
    segments_cross,
    signed_area,
)
from .machine import Machine
from .toolpath import Move, Placement, Toolpath, check_limits, span_positions
from .wing import project_to_towers

FORMATS = ("svg", "dxf")
MAX_SOURCE_BYTES = 8 * 1024 * 1024


@dataclass
class Region:
    """One cuttable shape: an outer outline (counter-clockwise) and its holes (clockwise)."""

    outer: list[Point]
    holes: list[list[Point]] = field(default_factory=list)

    def rings(self) -> list[list[Point]]:
        return [self.outer, *self.holes]


# Reading drawings ---------------------------------------------------------------------------

_cache: OrderedDict[tuple[str, str, float], Drawing] = OrderedDict()


def read_drawing(fmt: str, data: str, tolerance: float) -> Drawing:
    """Parse an SVG or DXF document (cached, since the GUI re-slices on every edit)."""
    from .dxfimport import parse_dxf
    from .svgimport import parse_svg

    if fmt not in FORMATS:
        raise ShapeError(f"unsupported drawing format {fmt!r}; use SVG or DXF")
    if len(data) > MAX_SOURCE_BYTES:
        raise ShapeError("the drawing is larger than 8 MB")
    key = (fmt, hashlib.sha256(data.encode("utf-8", "replace")).hexdigest(), tolerance)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    try:
        drawing = parse_svg(data, tolerance) if fmt == "svg" else parse_dxf(data, tolerance)
    except ShapeError:
        raise
    except (ValueError, IndexError, KeyError) as exc:
        raise ShapeError(f"could not read the {fmt.upper()} file: {exc}") from exc
    _cache[key] = drawing
    while len(_cache) > 8:
        _cache.popitem(last=False)
    return drawing


def format_for(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in FORMATS:
        raise ShapeError("choose an .svg or .dxf file")
    return ext


# Outlines -------------------------------------------------------------------------------------


def _clean(points: list[Point], eps: float = 1e-6) -> list[Point] | None:
    pts = dedupe(points, eps)
    if len(pts) > 1 and distance(pts[0], pts[-1]) <= eps:
        pts.pop()
    if len(pts) < 3 or abs(signed_area(pts)) < 1e-6:
        return None
    return pts


def join_polylines(polylines: list[Polyline], gap: float) -> tuple[list[list[Point]], int]:
    """Chain loose segments into closed outlines.

    Returns the closed outlines and how many open paths were left over.
    Endpoints closer than ``gap`` are joined.
    """
    closed: list[list[Point]] = []
    open_paths: list[list[Point]] = []
    for poly in polylines:
        pts = dedupe(poly.points)
        if len(pts) < 2:
            continue
        if poly.closed or (len(pts) > 2 and distance(pts[0], pts[-1]) <= gap):
            closed.append(pts)
        else:
            open_paths.append(pts)

    cell = max(gap, 1e-9)

    def key(p: Point) -> tuple[int, int]:
        return (math.floor(p[0] / cell), math.floor(p[1] / cell))

    grid: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for idx, pts in enumerate(open_paths):
        for end in (0, 1):
            grid.setdefault(key(pts[-end]), []).append((idx, end))
    used = [False] * len(open_paths)

    def take_nearest(p: Point) -> tuple[int, int] | None:
        kx, ky = key(p)
        best, best_d = None, gap
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for idx, end in grid.get((kx + dx, ky + dy), ()):
                    if not used[idx]:
                        d = distance(p, open_paths[idx][-end])
                        if d <= best_d:
                            best, best_d = (idx, end), d
        if best is not None:
            used[best[0]] = True
        return best

    leftovers = 0
    for start in range(len(open_paths)):
        if used[start]:
            continue
        used[start] = True
        chain = list(open_paths[start])
        for _ in range(2):  # grow the tail, then (reversed) the other end
            while distance(chain[0], chain[-1]) > gap or len(chain) < 3:
                found = take_nearest(chain[-1])
                if found is None:
                    break
                idx, end = found
                piece = open_paths[idx] if end == 0 else open_paths[idx][::-1]
                chain.extend(piece[1:])
            if len(chain) > 2 and distance(chain[0], chain[-1]) <= gap:
                break
            chain.reverse()
        if len(chain) > 2 and distance(chain[0], chain[-1]) <= gap:
            closed.append(chain)
        else:
            leftovers += 1
    return closed, leftovers


def build_regions(outlines: list[list[Point]]) -> list[Region]:
    """Sort outlines into shapes and holes by how deeply each is nested (even depth = shape)."""
    rings = [r for r in (_clean(o) for o in outlines) if r is not None]
    rings.sort(key=lambda r: -abs(signed_area(r)))
    boxes = [bounds(r) for r in rings]
    parent: list[int | None] = []
    depth: list[int] = []
    for i, ring in enumerate(rings):
        probe = ring[0]
        bx = boxes[i]
        owner, level = None, 0
        for j in range(i):
            bj = boxes[j]
            inside_box = bj[0] <= bx[0] and bj[1] <= bx[1] and bj[2] >= bx[2] and bj[3] >= bx[3]
            if inside_box and point_in_polygon(probe, rings[j]):
                level += 1
                owner = j  # rings are sorted by size, so the last match is the innermost
        parent.append(owner)
        depth.append(level)

    regions: dict[int, Region] = {}
    for i, ring in enumerate(rings):
        if depth[i] % 2 == 0:
            regions[i] = Region(ring if signed_area(ring) > 0 else ring[::-1])
    for i, ring in enumerate(rings):
        if depth[i] % 2 == 1 and parent[i] in regions:
            regions[parent[i]].holes.append(ring if signed_area(ring) < 0 else ring[::-1])
    return list(regions.values())


def all_points(regions: list[Region]) -> list[Point]:
    return [p for r in regions for ring in r.rings() for p in ring]


def transform_regions(regions: list[Region], scale: float, rotate_deg: float, mirror: bool) -> list[Region]:
    """Scale, rotate and mirror, then move the bounding box corner to (0, 0)."""
    if scale <= 0:
        raise ShapeError("scale must be > 0")
    a = math.radians(rotate_deg)
    c, s = math.cos(a) * scale, math.sin(a) * scale
    mx = -1.0 if mirror else 1.0

    def tf(p: Point) -> Point:
        x = p[0] * mx
        return (c * x - s * p[1], s * x + c * p[1])

    moved = [Region([tf(p) for p in r.outer], [[tf(p) for p in h] for h in r.holes]) for r in regions]
    if mirror:  # mirroring flips the direction of travel; restore outer CCW / holes CW
        moved = [Region(r.outer[::-1], [h[::-1] for h in r.holes]) for r in moved]
    x0, y0, _, _ = bounds(all_points(moved))
    return [Region([(x - x0, y - y0) for x, y in r.outer], [[(x - x0, y - y0) for x, y in h] for h in r.holes])
            for r in moved]  # fmt: skip


def scale_about(regions: list[Region], factor: float, center: Point, offset: Point) -> list[Region]:
    def tf(p: Point) -> Point:
        return (
            center[0] + (p[0] - center[0]) * factor + offset[0],
            center[1] + (p[1] - center[1]) * factor + offset[1],
        )

    return [Region([tf(p) for p in r.outer], [[tf(p) for p in h] for h in r.holes]) for r in regions]


# Cut planning ------------------------------------------------------------------------------------


# A spot on a ring: (segment index, t in [0, 1)); t == 0 is the segment's start vertex.
Spot = tuple[int, float]


def _skip_edges(n: int, spot: Spot) -> frozenset[int]:
    i, t = spot
    return frozenset({i}) if t > 0 else frozenset({(i - 1) % n, i})


def _hits(a: Point, b: Point, ring: list[Point], skip: frozenset[int] = frozenset()) -> bool:
    n = len(ring)
    for i in range(n):
        if i not in skip and segments_cross(a, b, ring[i], ring[(i + 1) % n]):
            return True
    return False


def _at(ring: list[Point], spot: Spot) -> Point:
    i, t = spot
    a, b = ring[i], ring[(i + 1) % len(ring)]
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _project(p: Point, a: Point, b: Point) -> tuple[float, float]:
    """Parameter and distance of the point on segment ab closest to p."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return t, math.hypot(a[0] + dx * t - p[0], a[1] + dy * t - p[1])


def _spot(i: int, t: float, n: int) -> Spot:
    if t >= 1.0 - 1e-9:
        return ((i + 1) % n, 0.0)
    return (i, 0.0) if t <= 1e-9 else (i, t)


def _sample(n: int, limit: int) -> range:
    return range(0, n, max(1, math.ceil(n / limit)))


@dataclass
class RegionPlan:
    entry: int
    """Outline vertex where the wire comes in from behind the block."""
    bridges: list[tuple[int, Spot, Spot]] = field(default_factory=list)
    """(hole number from 1, spot on the outline, spot on the hole) for every hole that is cut."""
    skipped_holes: int = 0
    shadowed: bool = False


def _closest(p: Point, ring: list[Point], limit: int) -> tuple[float, Spot]:
    n = len(ring)
    best_d, best = math.inf, (0, 0.0)
    for i in _sample(n, limit):
        t, d = _project(p, ring[i], ring[(i + 1) % n])
        if d < best_d:
            best_d, best = d, _spot(i, t, n)
    return best_d, best


def plan_region(region: Region, index: int, regions: list[Region], x_far: float) -> RegionPlan:
    """Where to enter one shape, and the shortest straight bridge cut from its outline into each hole."""
    outer = region.outer
    no = len(outer)
    others = [ring for k, r in enumerate(regions) if k != index for ring in r.rings()]

    # Entry: the rearmost outline point that can be reached from behind without crossing anything.
    order = sorted(range(no), key=lambda i: (outer[i][0], outer[i][1]))
    plan = RegionPlan(order[0], shadowed=True)
    for i in order[:400]:
        start = (x_far, outer[i][1])
        if _hits(start, outer[i], outer, _skip_edges(no, (i, 0.0))):
            continue
        if any(_hits(start, outer[i], ring) for ring in [*region.holes, *others]):
            continue
        plan.entry, plan.shadowed = i, False
        break

    for h_idx, hole in enumerate(region.holes, start=1):
        nh = len(hole)
        candidates: list[tuple[float, Spot, Spot]] = []
        for j in _sample(nh, 200):  # hole vertices to the nearest point of the outline
            d, spot = _closest(hole[j], outer, 400)
            candidates.append((d, spot, (j, 0.0)))
        for i in _sample(no, 200):  # outline vertices to the nearest point of the hole
            d, spot = _closest(outer[i], hole, 400)
            candidates.append((d, (i, 0.0), spot))
        candidates.sort(key=lambda c: c[0])
        for _, so, sh in candidates[:200]:
            a, b = _at(outer, so), _at(hole, sh)
            if _hits(a, b, outer, _skip_edges(no, so)) or _hits(a, b, hole, _skip_edges(nh, sh)):
                continue
            if any(_hits(a, b, other) for k, other in enumerate(region.holes, start=1) if k != h_idx):
                continue
            plan.bridges.append((h_idx, so, sh))
            break
        else:
            plan.skipped_holes += 1
    return plan


def _augment(ring: list[Point], spots: list[Spot]) -> tuple[list[Point], dict[Spot, int]]:
    """Insert the given mid-edge spots as vertices; returns the new ring and where each spot ended up."""
    inserts: dict[int, set[float]] = {}
    for i, t in spots:
        if t > 0:
            inserts.setdefault(i, set()).add(t)
    out: list[Point] = []
    index: dict[Spot, int] = {}
    for i, p in enumerate(ring):
        index[(i, 0.0)] = len(out)
        out.append(p)
        for t in sorted(inserts.get(i, ())):
            index[(i, t)] = len(out)
            out.append(_at(ring, (i, t)))
    return out, index


def region_paths(plan: RegionPlan, root: Region, tip: Region) -> tuple[list[Point], list[Point]]:
    """Root and tip point sequences for one shape: the outline from the entry, with each hole on the way."""
    spots = [so for _, so, _ in plan.bridges] + [(plan.entry, 0.0)]
    ro, idx = _augment(root.outer, spots)
    to, _ = _augment(tip.outer, spots)
    detours: dict[int, list[tuple[list[Point], list[Point], int]]] = {}
    for h, so, sh in plan.bridges:
        hr, hidx = _augment(root.holes[h - 1], [sh])
        ht, _ = _augment(tip.holes[h - 1], [sh])
        detours.setdefault(idx[so], []).append((hr, ht, hidx[sh]))

    n, entry = len(ro), idx[(plan.entry, 0.0)]
    root_seq: list[Point] = []
    tip_seq: list[Point] = []
    for step in range(n + 1):
        i = (entry + step) % n
        root_seq.append(ro[i])
        tip_seq.append(to[i])
        if step == n:
            break
        for hr, ht, j in detours.pop(i, []):
            m = len(hr)
            for k in range(m + 1):
                root_seq.append(hr[(j + k) % m])
                tip_seq.append(ht[(j + k) % m])
            root_seq.append(ro[i])
            tip_seq.append(to[i])
    return root_seq, tip_seq


# Layout ----------------------------------------------------------------------------------------------


@dataclass
class ShapeLayout:
    z_root: float
    z_tip: float
    root_regions: list[Region]
    """Finished shapes at the root face (no kerf), machine coordinates."""
    tip_regions: list[Region]
    paths: list[tuple[list[Point], list[Point]]]
    """For each shape, the XY and UV carriage paths (closed loops from the entry point)."""
    lengths: tuple[float, float]
    """Total outline length at the root and tip faces, for the kerf speed model."""
    warnings: list[str] = field(default_factory=list)
    carriage_scales: tuple[float, float] = (1.0, 1.0)
    """Size of the outline at the XY and UV carriages relative to the root face."""


def _distance_to_ring(p: Point, ring: list[Point]) -> float:
    n = len(ring)
    return min(_project(p, ring[i], ring[(i + 1) % n])[1] for i in range(n))


def _collapsed(hole: list[Point], offset: list[Point], d: float) -> bool:
    """True when a hole is narrower than the kerf everywhere, so offsetting it inwards leaves nothing.

    A correct inward offset stays ``d`` away from the original outline; when the hole is too
    narrow, the offset points land on or near the opposite side instead.
    """
    if d <= 0:
        return False
    if signed_area(offset) >= 0:
        return True
    samples = [offset[i] for i in _sample(len(offset), 200)]
    near = sum(1 for p in samples if _distance_to_ring(p, hole) < 0.9 * d)
    return near >= 0.8 * len(samples)


def outline_lengths(regions: list[Region]) -> float:
    return sum(polyline_length(ring + ring[:1]) for r in regions for ring in r.rings())


def layout_shapes(
    regions: list[Region],
    machine: Machine,
    placement: Placement,
    span: float,
    tip_scale: float = 1.0,
    tip_offset: Point = (0.0, 0.0),
    root_kerf: float = 0.0,
    tip_kerf: float = 0.0,
) -> ShapeLayout:
    if not regions:
        raise ShapeError("the drawing has no closed outlines to cut")
    if tip_scale <= 0:
        raise ShapeError("tip scale must be > 0")
    z_root, z_tip = span_positions(span, machine, placement)
    # The wire extends the taper linearly out to both carriages.
    carriage_scales = tuple(
        1.0 + (tip_scale - 1.0) * (z - z_root) / (z_tip - z_root) for z in (0.0, machine.tower_distance)
    )
    for side, factor in zip(("XY", "UV"), carriage_scales):
        if factor <= 0.05:
            raise ShapeError(
                f"a {tip_scale:.0%} taper over {span:g} mm is too steep for this position: extended to the {side} "
                f"carriage the outline would shrink to nothing. Make the part longer, reduce the taper, or move "
                f"the part closer to the {side} carriage."
            )
    x0, y0, x1, y1 = bounds(all_points(regions))
    tip_regions = scale_about(regions, tip_scale, ((x0 + x1) / 2, (y0 + y1) / 2), tip_offset)
    warnings: list[str] = []

    # Kerf: move every ring away from the material (to its right; outlines CCW, holes CW).
    root_k: list[Region] = []
    tip_k: list[Region] = []
    nominal_root: list[Region] = []
    nominal_tip: list[Region] = []
    tiny_holes = 0
    for r, t in zip(regions, tip_regions):
        try:
            ro, to = offset_polygon(r.outer, -root_kerf / 2), offset_polygon(t.outer, -tip_kerf / 2)
        except ValueError as exc:
            raise ShapeError(f"cannot offset an outline: {exc}") from exc
        holes_r, holes_t, keep_r, keep_t = [], [], [], []
        for hr, ht in zip(r.holes, t.holes):
            kr, kt = offset_polygon(hr, -root_kerf / 2), offset_polygon(ht, -tip_kerf / 2)
            if _collapsed(hr, kr, root_kerf / 2) or _collapsed(ht, kt, tip_kerf / 2):
                tiny_holes += 1
                continue
            holes_r.append(kr)
            holes_t.append(kt)
            keep_r.append(hr)
            keep_t.append(ht)
        root_k.append(Region(ro, holes_r))
        tip_k.append(Region(to, holes_t))
        nominal_root.append(Region(r.outer, keep_r))
        nominal_tip.append(Region(t.outer, keep_t))
    if tiny_holes:
        warnings.append(f"Skipped {tiny_holes} hole(s) narrower than the kerf.")

    # Plan on the root face; the tip rings have the same points, so the same plan applies.
    x_far = bounds(all_points(root_k))[0] - 10.0
    plans = [plan_region(r, k, root_k, x_far) for k, r in enumerate(root_k)]
    raw_paths = []
    for plan, rr, tt in zip(plans, root_k, tip_k):
        root_seq, tip_seq = region_paths(plan, rr, tt)
        raw_paths.append(project_to_towers(root_seq, z_root, tip_seq, z_tip, machine.tower_distance))

    bridges = sum(len(p.bridges) for p in plans)
    if bridges:
        warnings.append(
            f"{bridges} hole(s) are reached through a straight cut from the outline: "
            "each leaves a kerf-wide slit in the part to glue."
        )
    skipped = sum(p.skipped_holes for p in plans)
    if skipped:
        warnings.append(f"{skipped} hole(s) could not be reached with a straight cut and were skipped.")
    shadowed = sum(p.shadowed for p in plans)
    if shadowed:
        warnings.append(
            f"{shadowed} shape(s) can only be reached by cutting through another shape: "
            "move the shapes apart vertically."
        )

    # Machine coordinates: rearmost carriage position at x_margin, bottom of the root shapes at y_offset.
    min_x = min(p[0] for xy, uv in raw_paths for p in xy + uv)
    min_y = bounds(all_points(regions))[1]
    dx, dy = placement.x_margin - min_x, placement.y_offset - min_y

    def shift(points: list[Point]) -> list[Point]:
        return [(x + dx, y + dy) for x, y in points]

    def shift_regions(rs: list[Region]) -> list[Region]:
        return [Region(shift(r.outer), [shift(h) for h in r.holes]) for r in rs]

    paths = [(shift(xy), shift(uv)) for xy, uv in raw_paths]
    paths.sort(key=lambda p: p[0][0][1])  # bottom to top: less travel along X = 0
    return ShapeLayout(
        z_root,
        z_tip,
        shift_regions(nominal_root),
        shift_regions(nominal_tip),
        paths,
        (outline_lengths(regions), outline_lengths(tip_regions)),
        warnings,
        (carriage_scales[0], carriage_scales[1]),
    )


def shapes_toolpath(layout: ShapeLayout, machine: Machine) -> Toolpath:
    """Enter every shape from X = 0, cut it, leave the way it came in, then return to the origin."""
    moves: list[Move] = []
    for xy, uv in layout.paths:
        moves.append(Move((0.0, xy[0][1]), (0.0, uv[0][1]), cutting=False))
        moves += [Move(a, b, cutting=True) for a, b in zip(xy, uv)]
        moves.append(Move((0.0, xy[-1][1]), (0.0, uv[-1][1]), cutting=True))
    moves.append(Move((0.0, 0.0), (0.0, 0.0), cutting=False))
    span = (min(layout.z_root, layout.z_tip), max(layout.z_root, layout.z_tip))
    toolpath = Toolpath(tuple(moves), foam_span=span)
    check_limits(toolpath, machine)
    return toolpath


__all__ = [
    "FORMATS",
    "Region",
    "ShapeError",
    "ShapeLayout",
    "build_regions",
    "format_for",
    "join_polylines",
    "layout_shapes",
    "plan_region",
    "read_drawing",
    "region_paths",
    "shapes_toolpath",
    "transform_regions",
]
