# SPDX-License-Identifier: GPL-3.0-or-later
"""Cutting a 3D model (an STL file) out of foam.

Two views
    Pass 1 cuts the model's side outline right through the block. Keep the offcuts and tape
    them back in place, then turn the block a quarter turn about its length: pass 2 cuts the
    outline seen from above. What is left is the model as both views see it, square-ish in
    section; round it off with a sanding block. Quick, two cuts, for anything whose shape is
    mostly in its side and top views (fuselages, figures, letters, simple objects).

Slabs
    The model is sliced across the wire into slabs as thick as your foam sheets. Each slab is
    cut on its own as a ruled surface between the model's sections at its two faces (the
    4-axis cut follows the change from one face to the other), and the slabs are glued back
    together. More cuts and some gluing, but it follows any shape, hollows included.

The model lies with its length along the machine's X axis, up along Y, and its width along
the wire; its bounding box sits ``spare`` mm inside the foam, the foam's rear face at
``x_margin`` and its underside at ``y_offset``.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

from .geometry import (
    Point,
    bounds,
    cumulative_lengths,
    point_at_length,
    point_in_polygon,
    point_segment_distance,
    polyline_length,
    signed_area,
)
from .machine import Machine
from .mesh import Mesh, MeshError, cached, default_cell
from .shapes import Region, ShapeLayout, all_points, layout_shapes, shapes_toolpath
from .toolpath import MachineLimitError, Placement, Toolpath, span_positions

METHODS = ("two_view", "slabs")
MIN_THICKNESS = 2.0

KerfFn = Callable[[tuple[float, float]], tuple[float, float]]


class ModelError(ValueError):
    """The model cannot be cut this way."""


@dataclass(frozen=True)
class ModelOptions:
    method: str = "two_view"
    spare: float = 5.0
    """Foam left round the model on every side (mm)."""
    thickness: float = 30.0
    """Slabs: thickness of each slab along the wire (your foam sheets), mm."""
    detail: float | None = None
    """Size of the grid the outlines are traced on (mm); None picks one from the model's size."""

    def __post_init__(self) -> None:
        if self.method not in METHODS:
            raise ValueError('cut method must be "two_view" or "slabs"')
        if self.spare < 1:
            raise ValueError("spare foam must be >= 1 mm")
        if self.thickness < MIN_THICKNESS:
            raise ValueError(f"slab thickness must be >= {MIN_THICKNESS:g} mm")
        if self.detail is not None and not 0.05 <= self.detail <= 10:
            raise ValueError("outline detail must be between 0.05 and 10 mm")


@dataclass
class ModelJob:
    label: str
    toolpath: Toolpath
    z_root: float
    z_tip: float
    root_regions: list[Region]
    tip_regions: list[Region]
    kerf: tuple[float, float]
    ratio: tuple[float, float]
    block: dict[str, float]
    placement: tuple[tuple[float, float, float], tuple[tuple[float, float, float], ...]]
    """Where the model is in this cut: machine = origin + matrix · model (rows of the matrix)."""
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    views: list[dict[str, object]] = field(default_factory=list)
    """2D views to show: {"label", "rings"}."""


def cell_for(mesh: Mesh, options: ModelOptions) -> float:
    return options.detail or default_cell(mesh)


def slab_ranges(mesh: Mesh, options: ModelOptions) -> list[tuple[float, float]]:
    """The slabs across the model (Z ranges), each ``thickness`` thick but the last."""
    width, t = mesh.size[2], options.thickness
    n = max(1, math.ceil(width / t - 1e-6))
    return [(k * t, min(width, (k + 1) * t)) for k in range(n)]


def job_labels(mesh: Mesh, options: ModelOptions) -> list[str]:
    if options.method == "two_view":
        return ["Pass 1 · side view", "Pass 2 · top view"]
    ranges = slab_ranges(mesh, options)
    return [f"Slab {k} of {len(ranges)} · {za:.0f}–{zb:.0f} mm" for k, (za, zb) in enumerate(ranges, start=1)]


def build_job(
    mesh: Mesh, options: ModelOptions, index: int, machine: Machine, placement: Placement, kerfs: KerfFn
) -> ModelJob:
    labels = job_labels(mesh, options)
    if not 0 <= index < len(labels):
        raise ModelError(f"there is no cut {index + 1}; this model has {len(labels)}")
    if options.method == "two_view":
        job = _two_view(mesh, options, index, machine, placement, kerfs((1.0, 1.0))[0])
    else:
        job = _slab(mesh, options, index, machine, placement, kerfs)
    job.label = labels[index]
    return job


# -- helpers ---------------------------------------------------------------------------------


def _block(x0: float, x1: float, y0: float, y1: float, z0: float, z1: float) -> dict[str, float]:
    return {"x0": x0, "x1": x1, "y0": y0, "y1": y1, "z0": min(z0, z1), "z1": max(z0, z1)}


def _size(block: dict[str, float]) -> str:
    return f"{block['x1'] - block['x0']:.0f} × {block['y1'] - block['y0']:.0f} × {block['z1'] - block['z0']:.0f} mm"


def _shift(layout: ShapeLayout, dx: float, dy: float) -> ShapeLayout:
    def s(points: list[Point]) -> list[Point]:
        return [(x + dx, y + dy) for x, y in points]

    def r(regions: list[Region]) -> list[Region]:
        return [Region(s(g.outer), [s(h) for h in g.holes]) for g in regions]

    return ShapeLayout(
        layout.z_root,
        layout.z_tip,
        r(layout.root_regions),
        r(layout.tip_regions),
        [(s(xy), s(uv)) for xy, uv in layout.paths],
        layout.lengths,
        layout.warnings,
        layout.carriage_scales,
    )


def _mirror_x(regions: list[Region], length: float) -> list[Region]:
    """Turned end for end (X → length − X), rings kept counter-clockwise / clockwise."""

    def m(ring: list[Point]) -> list[Point]:
        return [(length - x, y) for x, y in reversed(ring)]

    return [Region(m(r.outer), [m(h) for h in r.holes]) for r in regions]


def _laid_out(
    regions: list[Region],
    machine: Machine,
    placement: Placement,
    origin: Point,
    span: float,
    kerf: tuple[float, float],
    tip_regions: list[Region] | None = None,
) -> ShapeLayout:
    """Lay the outlines out so the model's (0, 0) lands on ``origin`` in the machine."""
    plan = Placement(placement.root_side, placement.root_offset, origin[0], origin[1])
    layout = layout_shapes(regions, machine, plan, span, 1.0, (0.0, 0.0), kerf[0], kerf[1], tip_regions)
    model_x0, model_y0, _, _ = bounds(all_points(regions + (tip_regions or [])))
    laid_x0, laid_y0, _, _ = bounds(all_points(layout.root_regions + layout.tip_regions))
    return _shift(layout, origin[0] + model_x0 - laid_x0, origin[1] + model_y0 - laid_y0)


def _rings(regions: list[Region], limit: int = 400) -> list[list[Point]]:
    out = []
    for r in regions:
        for ring in r.rings():
            step = max(1, len(ring) // limit)
            out.append(ring[::step])
    return out


# -- two views -------------------------------------------------------------------------------


def _two_view(
    mesh: Mesh, opt: ModelOptions, index: int, machine: Machine, placement: Placement, kerf: float
) -> ModelJob:
    length, height, width = mesh.size
    cell = cell_for(mesh, opt)
    s = opt.spare
    origin = (placement.x_margin + s, placement.y_offset + s)
    view, up, along = ("side", height, width) if index == 0 else ("top", width, height)
    regions = cached(mesh, "silhouette", view, cell)
    if not regions:
        raise ModelError(f"the model has no outline seen from the {view}")
    layout = _laid_out(regions, machine, placement, origin, along + 2 * s, (kerf, kerf))  # type: ignore[arg-type]
    toolpath = shapes_toolpath(layout, machine)
    zlo = min(layout.z_root, layout.z_tip)
    block = _block(placement.x_margin, origin[0] + length + s, placement.y_offset, origin[1] + up + s,
                   layout.z_root, layout.z_tip)  # fmt: skip
    where = (
        f"rear face at X {block['x0']:g} mm, underside at Y {block['y0']:g} mm, from {block['z0']:.0f} to "
        f"{block['z1']:.0f} mm from the XY carriage"
    )
    if index == 0:
        transform = ((origin[0], origin[1], zlo + s), ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
        notes = [
            f"Foam block at least {_size(block)} (length × height × width along the wire): {where}.",
            (
                "This pass cuts the model's side outline right through the block. Keep every offcut: they go back "
                "in place for pass 2."
            ),
        ]
    else:
        # A quarter turn about the length: the model's width now points up and its height runs along the wire.
        transform = ((origin[0], origin[1], zlo + s + height), ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0)))
        notes = [
            (
                "Tape the offcuts from pass 1 back in place, so the block is whole again. Turn it a quarter turn "
                "about its length: the face that was towards the XY carriage goes underneath, and the model's top "
                f"now faces the XY carriage. Then: {where}."
            ),
            (
                "This pass cuts the outline seen from above and frees the model. Round off its square edges with "
                "a sanding block."
            ),
        ]
    return ModelJob(
        "",
        toolpath,
        layout.z_root,
        layout.z_tip,
        layout.root_regions,
        layout.tip_regions,
        (kerf, kerf),
        (1.0, 1.0),
        block,
        transform,
        notes=notes,
        warnings=list(layout.warnings),
    )


# -- slabs -----------------------------------------------------------------------------------


def _ring_resample(ring: list[Point], n: int) -> list[Point]:
    closed = [*ring, ring[0]]
    cum = cumulative_lengths(closed)
    total = cum[-1]
    return [point_at_length(closed, cum, total * k / n) for k in range(n)]


def _centroid(ring: list[Point]) -> Point:
    a = signed_area(ring)
    if abs(a) < 1e-12:
        return (sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring))
    cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        cross = x0 * y1 - x1 * y0
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    return (cx / (6 * a), cy / (6 * a))


def _aligned(a: list[Point], b: list[Point], step: float) -> tuple[list[Point], list[Point]]:
    """Both rings with the same number of points, b's turned so its points face a's (centres put together)."""
    n = min(400, max(24, round(max(polyline_length([*a, a[0]]), polyline_length([*b, b[0]])) / step)))
    ra, rb = _ring_resample(a, n), _ring_resample(b, n)
    (ax, ay), (bx, by) = _centroid(ra), _centroid(rb)
    rel_a = [(x - ax, y - ay) for x, y in ra]
    rel_b = [(x - bx, y - by) for x, y in rb]
    best = min(
        range(n),
        key=lambda k: sum(
            (p[0] - rel_b[(i + k) % n][0]) ** 2 + (p[1] - rel_b[(i + k) % n][1]) ** 2 for i, p in enumerate(rel_a)
        ),
    )
    return ra, rb[best:] + rb[:best]


def _match(a: list[list[Point]], b: list[list[Point]]) -> list[tuple[int, int]] | None:
    """Pair rings by their centres, nearest first; None when the counts differ."""
    if len(a) != len(b):
        return None
    ca, cb = [_centroid(r) for r in a], [_centroid(r) for r in b]
    pairs = sorted(((math.dist(ca[i], cb[j]), i, j) for i in range(len(a)) for j in range(len(b))), key=lambda t: t[0])
    used_a: set[int] = set()
    used_b: set[int] = set()
    out = []
    for _, i, j in pairs:
        if i not in used_a and j not in used_b:
            used_a.add(i)
            used_b.add(j)
            out.append((i, j))
    return out


def _pair_faces(
    root: list[Region], tip: list[Region], step: float, where: str, warnings: list[str]
) -> tuple[list[Region], list[Region]]:
    """Outlines for both faces with matching points, so the wire can join them."""
    if not root and not tip:
        raise ModelError(f"{where}: the model does not reach into this slab")
    straight = None
    if not root or not tip:
        straight = root or tip
        warnings.append(f"{where}: the model ends inside this slab; it is cut straight through, as its other face.")
    else:
        pairs = _match([r.outer for r in root], [t.outer for t in tip])
        if pairs is None:
            bigger = max((root, tip), key=lambda rs: sum(signed_area(r.outer) for r in rs))
            straight = bigger
            warnings.append(
                f"{where}: the model splits or joins up between the slab's faces, so the slab is cut straight "
                "through, as its larger face. Thinner slabs follow it more closely."
            )
    if straight is not None:
        out = []
        for r in straight:
            outer = _ring_resample(r.outer, min(400, max(24, round(polyline_length([*r.outer, r.outer[0]]) / step))))
            out.append(Region(outer, [h for h in r.holes]))
        return out, [Region(list(r.outer), [list(h) for h in r.holes]) for r in out]

    roots, tips = [], []
    dropped = 0
    for i, j in pairs:
        ro, to = _aligned(root[i].outer, tip[j].outer, step)
        holes_r: list[list[Point]] = []
        holes_t: list[list[Point]] = []
        hole_pairs = _match(root[i].holes, tip[j].holes)
        if hole_pairs is None:
            dropped += len(root[i].holes) + len(tip[j].holes)
        else:
            for hi, hj in hole_pairs:
                # Holes run clockwise: align them as counter-clockwise rings, then turn them back.
                hr, ht = _aligned(root[i].holes[hi][::-1], tip[j].holes[hj][::-1], step)
                holes_r.append(hr[::-1])
                holes_t.append(ht[::-1])
        roots.append(Region(ro, holes_r))
        tips.append(Region(to, holes_t))
    if dropped:
        warnings.append(f"{where}: its holes change between the faces, so they are not cut; hollow it by hand.")
    return roots, tips


def _blend(a: list[Region], b: list[Region], f: float) -> list[Region]:
    """Point by point, ``f`` of the way from outlines ``a`` to the matching outlines ``b``."""

    def mix(p: list[Point], q: list[Point]) -> list[Point]:
        return [(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f) for (x0, y0), (x1, y1) in zip(p, q)]

    return [
        Region(mix(ra.outer, rb.outer), [mix(h0, h1) for h0, h1 in zip(ra.holes, rb.holes)]) for ra, rb in zip(a, b)
    ]


def _poke(container: list[list[Point]], regions: list[Region]) -> float:
    """How far the outlines of ``regions`` reach outside the rings of ``container`` (mm)."""
    worst = 0.0
    for region in regions:
        for p in region.outer:
            if any(point_in_polygon(p, ring) for ring in container):
                continue
            gap = min(
                point_segment_distance(p, ring[k], ring[(k + 1) % len(ring)])
                for ring in container
                for k in range(len(ring))
            )
            worst = max(worst, gap)
    return worst


def _bulge(roots: list[Region], tips: list[Region], middle: list[Region]) -> float:
    """How far the model's section halfway through the slab pokes out of the straight cut there (mm)."""
    halfway = [[((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in zip(r.outer, t.outer)] for r, t in zip(roots, tips)]
    return _poke(halfway, middle)


def _slab(mesh: Mesh, opt: ModelOptions, index: int, machine: Machine, placement: Placement, kerfs: KerfFn) -> ModelJob:
    length, height, _ = mesh.size
    cell = cell_for(mesh, opt)
    s = opt.spare
    ranges = slab_ranges(mesh, opt)
    za, zb = ranges[index]
    where = f"slab {index + 1}"
    inset = min(0.2, (zb - za) / 10)
    root, odd_a = cached(mesh, "section", za + inset, cell)  # type: ignore[misc]
    tip, odd_b = cached(mesh, "section", zb - inset, cell)  # type: ignore[misc]
    warnings: list[str] = []
    if odd_a or odd_b:
        warnings.append(
            "The model is not closed (its surface has gaps), so some of its sections may be wrong: check them in the "
            "views, or repair the model in a 3D program."
        )
    step = max(1.0, 2 * cell)
    roots, tips = _pair_faces(root, tip, step, where, warnings)  # type: ignore[arg-type]
    model_roots, model_tips = roots, tips  # in the model's own frame, for the checks below

    def outline_length(regions: list[Region]) -> float:
        return sum(polyline_length([*ring, ring[0]]) for r in regions for ring in r.rings())

    def speeds(a: list[Region], b: list[Region]) -> tuple[float, float]:
        """How fast each face's outline is cut relative to the faster one (the kerf depends on it)."""
        la, lb = outline_length(a), outline_length(b)
        return la / max(la, lb), lb / max(la, lb)

    ratio = speeds(roots, tips)
    kerf = kerfs(ratio)
    origin = (placement.x_margin + s, placement.y_offset + s)
    # A slab is thin along the wire and the carriages are far apart, so a change between its faces
    # tilts the wire a long way at the carriages. Midway between them the tilt is least.
    middle_place = Placement(
        placement.root_side, max(0.0, (machine.tower_distance - (zb - za)) / 2), placement.x_margin, placement.y_offset
    )
    z_root, z_tip = span_positions(zb - za, machine, middle_place)
    flipped = z_tip < z_root  # the slab's first face is the one further from the XY carriage
    if flipped:
        # Seen from the other side the slab is a mirror image: turn it end for end so it is not.
        roots, tips = _mirror_x(roots, length), _mirror_x(tips, length)
    # Where the wire cannot taper all the way, it starts from the face that the other pokes out of least
    # (the larger one, when one sits inside the other), so the cut stays outside the model.
    poke_root, poke_tip = _poke([r.outer for r in roots], tips), _poke([t.outer for t in tips], roots)
    root_big = poke_root < poke_tip - 1e-6 or (
        abs(poke_root - poke_tip) <= 1e-6
        and sum(signed_area(r.outer) for r in roots) >= sum(signed_area(t.outer) for t in tips)
    )

    def faces(f: float) -> tuple[list[Region], list[Region]]:
        """Taper ``f`` of the way from the starting face's outline to the other one's (1 = the true shape)."""
        return (roots, _blend(roots, tips, f)) if root_big else (_blend(tips, roots, f), tips)

    def attempt(f: float) -> tuple[ShapeLayout, Toolpath]:
        a, b = faces(f)
        layout = _laid_out(a, machine, middle_place, origin, zb - za, kerf, b)
        return layout, shapes_toolpath(layout, machine)

    try:
        layout, toolpath = attempt(1.0)
    except MachineLimitError:
        layout, toolpath = attempt(0.0)  # not even straight: the sheet does not fit, and the error says why
        lo, hi = 0.0, 1.0
        for _ in range(7):
            f = (lo + hi) / 2
            try:
                layout, toolpath = attempt(f)
                lo = f
            except MachineLimitError:
                hi = f
        # A smaller taper evens out the speeds at the two faces, and with them the kerf.
        ratio = speeds(*faces(lo))
        kerf, wider = kerfs(ratio), kerf
        try:
            layout, toolpath = attempt(lo)
        except MachineLimitError:
            kerf = wider
            layout, toolpath = attempt(lo)
        if lo < 0.95:
            warnings.append(
                f"{where.capitalize()} changes shape faster than the wire can follow over {zb - za:.0f} mm, so it is "
                f"cut {lo:.0%} of the way from one face's outline to the other's and comes out stepped: sand it to "
                "shape. Thicker slabs can taper more."
            )
    else:
        lo = 1.0
    # What is cut halfway through the slab, against the model there: where the model pokes out, the
    # cut goes into it (a bulge between the faces, which a straight line between them cannot follow).
    if root_big:
        cut_a, cut_b = model_roots, _blend(model_roots, model_tips, lo)
    else:
        cut_a, cut_b = _blend(model_tips, model_roots, lo), model_tips
    middle, _ = cached(mesh, "section", (za + zb) / 2, cell)  # type: ignore[misc]
    bulge = _bulge(cut_a, cut_b, middle)  # type: ignore[arg-type]
    if bulge > max(1.0, 2 * cell):
        warnings.append(
            f"{where.capitalize()}: halfway through, the model bulges up to {bulge:.1f} mm outside the cut between "
            "the slab's faces, so the cut goes into it there. Thinner slabs follow it more closely."
        )
    block = _block(placement.x_margin, origin[0] + length + s, placement.y_offset, origin[1] + height + s,
                   layout.z_root, layout.z_tip)  # fmt: skip
    if flipped:
        transform = (
            (origin[0] + length, origin[1], layout.z_root + za),
            ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, -1.0)),
        )
    else:
        transform = ((origin[0], origin[1], layout.z_root - za), ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
    near = "UV" if flipped else "XY"
    notes = [
        (
            f"Slab {index + 1} of {len(ranges)}: {za:.0f} to {zb:.0f} mm across the model, {zb - za:.0f} mm thick. "
            f"Foam sheet {_size(block)}: rear face at X {block['x0']:g} mm, underside at Y {block['y0']:g} mm, from "
            f"{block['z0']:.0f} to {block['z1']:.0f} mm from the XY carriage."
        ),
        (
            f"The face towards the {near} carriage is the one nearer the start of the model (0 mm across it). Number "
            "the slab and mark that face before you take it out."
        ),
        (
            "Cut every slab from a sheet of the same size, placed the same way: their outer frames then line up, so "
            "stacked frames make a jig to glue the slabs together in register."
        ),
    ]
    if index == len(ranges) - 1 and zb - za < opt.thickness - 0.5:
        notes.append(f"This last slab is only {zb - za:.0f} mm thick: cut it from a thinner sheet, or sand it down.")
    views = [
        {"label": f"Face at {za:.0f} mm", "rings": _rings(root)},  # type: ignore[arg-type]
        {"label": f"Face at {zb:.0f} mm", "rings": _rings(tip)},  # type: ignore[arg-type]
    ]
    return ModelJob(
        "",
        toolpath,
        layout.z_root,
        layout.z_tip,
        layout.root_regions,
        layout.tip_regions,
        kerf,
        ratio,
        block,
        transform,
        notes=notes,
        warnings=warnings + list(layout.warnings),
        views=views,
    )


def model_views(mesh: Mesh, options: ModelOptions) -> list[dict[str, object]]:
    """The model's silhouettes from the side, the top and the front, to check it lies the right way."""
    cell = cell_for(mesh, options) * 2  # thumbnails: a coarser grid is plenty
    labels = {"side": "Side", "top": "Top", "front": "Front"}
    return [{"label": labels[v], "rings": _rings(cached(mesh, "silhouette", v, cell), 240)} for v in labels]  # type: ignore[arg-type]


def mesh_volume(mesh: Mesh) -> float:
    """The volume inside the surface (mm³); only right for a closed model."""
    total = 0.0
    for a, b, c in mesh.triangles:
        total += (
            a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0]) + a[2] * (b[0] * c[1] - b[1] * c[0])
        )
    return abs(total) / 6


__all__ = [
    "METHODS",
    "MeshError",
    "ModelError",
    "ModelJob",
    "ModelOptions",
    "build_job",
    "job_labels",
    "mesh_volume",
    "model_views",
    "slab_ranges",
]
