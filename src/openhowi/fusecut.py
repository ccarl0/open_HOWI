# SPDX-License-Identifier: GPL-3.0-or-later
"""Cutting a fuselage: the two-view passes, or lengthwise segments.

Two views
    Pass 1 cuts the side view's top and bottom lines from one end of the block
    to the other, so the slab between them keeps the block's flat end faces;
    those locate it for pass 2, where it lies on its side and the top view is
    cut out. The sections come out square-ish and are rounded by hand.

Turned
    The block is turned about the fuselage's axis between passes, on a
    polygon jig rolled by hand (or by a rotary axis); every pass cuts the top
    silhouette, so N passes leave an N-sided fuselage that needs little sanding.

Lengthwise segments
    The wire runs along the fuselage. Each segment is a ruled surface between
    the sections at its ends, like a wing panel: the sections come out
    rounded. A segment can be hollow (reached through a slit along the bottom
    centre line) or cut as a left and a right half shell, open along the
    centre line, which needs no slit. The fuselage lies on its side, bottom
    towards the back of the machine (X = 0).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

from . import diagrams
from .fuselage import MIN_SECTION, Fuselage, FuselageError, auto_axis, finished_section, half_ring, ray_distance
from .geometry import Point, offset_polygon, offset_polyline, polyline_length, signed_area
from .machine import Machine
from .shapes import Region, ShapeLayout, layout_shapes, shapes_toolpath
from .toolpath import Move, Placement, Stop, Toolpath, WingLayout, check_limits, span_positions, wing_toolpath
from .wing import apply_kerf, project_to_towers

METHODS = ("two_view", "turned", "segments")
FACETS = (4, 6, 8, 12, 16, 24, 32)
AIR = 5.0
"""How far past the block the wire turns round in pass 1 (mm)."""

Ring3 = list[tuple[float, float, float]]


@dataclass(frozen=True)
class CutOptions:
    method: str = "two_view"
    segment_length: float = 150.0
    """Longest segment along the wire (lengthwise segments)."""
    halves: bool = False
    """Cut every segment as a left and a right half."""
    wall: float = 0.0
    """Hollow segments: wall thickness (mm); 0 = solid."""
    spare: float = 5.0
    """Foam left around the part on every side (mm)."""
    points: int = 64
    """Points per section (lengthwise segments), a multiple of 4."""
    facets: int = 8
    """Turned: passes all round, i.e. sides of the cut fuselage."""
    plate: float = 6.0
    """Turned by hand: thickness of the jig's end plates (mm)."""
    hold: float = 5.0
    """Turned: foam kept round the axis (mm), so the part stays joined to the end stubs."""
    axis: float | None = None
    """Turned: height of the turning axis above the datum (mm); None picks one."""

    def __post_init__(self) -> None:
        if self.method not in METHODS:
            raise ValueError('cut method must be "two_view", "turned" or "segments"')
        if self.facets not in FACETS:
            raise ValueError(f"facets must be one of {', '.join(map(str, FACETS))}")
        if self.plate < 0 or self.hold < 1:
            raise ValueError("plate thickness must be >= 0 and the foam kept round the axis >= 1 mm")
        if self.wall < 0 or self.spare < 1:
            raise ValueError("wall must be >= 0 and spare foam >= 1 mm")
        if self.points % 4 or not 16 <= self.points <= 400:
            raise ValueError("points per section must be a multiple of 4 between 16 and 400")


@dataclass
class CutJob:
    label: str
    toolpath: Toolpath
    z_root: float
    z_tip: float
    root_regions: list[Region]
    tip_regions: list[Region]
    kerf: tuple[float, float]
    ratio: tuple[float, float]
    block: dict[str, float]
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ghost: list[Ring3] = field(default_factory=list)
    """The finished fuselage (rounded) in this job's machine position, as rings to loft, for display."""
    ghost_label: str = "Finished shape (after sanding)"
    steps: list[dict[str, object]] = field(default_factory=list)
    """What to do, step by step: {"title", "notes", "svg"}."""
    jig: dict[str, float] | None = None
    """Turned by hand: the end plates (sides, apothem, circumradius, thickness, block half side)."""


KerfFn = Callable[[tuple[float, float]], tuple[float, float]]


def job_labels(fuse: Fuselage, options: CutOptions) -> list[str]:
    if options.method == "two_view":
        return ["Pass 1 · side view", "Pass 2 · top view"]
    if options.method == "turned":
        return [f"All {options.facets} passes, turning the block"]
    labels = []
    segs = fuse.segments(options.segment_length)
    for k, (xa, xb) in enumerate(segs, start=1):
        base = f"Segment {k} of {len(segs)} · {xa:.0f}–{xb:.0f} mm"
        labels += [f"{base} · right half", f"{base} · left half"] if options.halves else [base]
    return labels


def build_job(
    fuse: Fuselage, options: CutOptions, index: int, machine: Machine, placement: Placement, kerfs: KerfFn
) -> CutJob:
    labels = job_labels(fuse, options)
    if not 0 <= index < len(labels):
        raise FuselageError(f"there is no cut {index + 1}; this fuselage has {len(labels)}")
    if options.method == "two_view":
        kerf = kerfs((1.0, 1.0))[0]
        job = (_side_pass if index == 0 else _plan_pass)(fuse, options, machine, placement, kerf)
    elif options.method == "turned":
        job = _turned(fuse, options, machine, placement, kerfs((1.0, 1.0))[0])
    else:
        per = 2 if options.halves else 1
        job = _segment(fuse, options, index // per, index % per == 1, machine, placement, kerfs)
    job.label = labels[index]
    return job


def _block(x0: float, x1: float, y0: float, y1: float, z0: float, z1: float) -> dict[str, float]:
    return {"x0": x0, "x1": x1, "y0": y0, "y1": y1, "z0": min(z0, z1), "z1": max(z0, z1)}


def _size(block: dict[str, float]) -> str:
    return f"{block['x1'] - block['x0']:.0f} × {block['y1'] - block['y0']:.0f} × {block['z1'] - block['z0']:.0f} mm"


def _ghost(fuse: Fuselage, to_machine: Callable[[float, float, float], tuple[float, float, float]]) -> list[Ring3]:
    n = 48
    xs = [fuse.length * k / n for k in range(n + 1)]
    return [[to_machine(x, u, v) for u, v in fuse.section(x, 32, minimum=0.2)] for x in xs]


def _side_pass(fuse: Fuselage, opt: CutOptions, machine: Machine, placement: Placement, kerf: float) -> CutJob:
    top, bottom = fuse.side_lines()
    lo, hi = min(y for _, y in bottom), max(y for _, y in top)
    width = fuse.max_width()
    z_root, z_tip = span_positions(width + 2 * opt.spare, machine, placement)
    nose_x = placement.x_margin + opt.spare
    dy = placement.y_offset + opt.spare - lo
    b0, b1 = placement.x_margin, nose_x + fuse.length + opt.spare

    def m(p: Point) -> Point:
        return (nose_x + p[0], dy + p[1])

    # Both lines run out to the block's end faces, so the slab keeps them.
    top_line = [(b0, dy + top[0][1]), *(m(p) for p in top), (b1, dy + top[-1][1])]
    bottom_line = [(b1, dy + bottom[-1][1]), *(m(p) for p in reversed(bottom)), (b0, dy + bottom[0][1])]
    top_k = offset_polyline(top_line, kerf / 2) if kerf else top_line  # travelling +X: left is up, outside
    bottom_k = offset_polyline(bottom_line, kerf / 2) if kerf else bottom_line  # travelling -X: left is down

    air = b1 + AIR

    def move(p: Point, cutting: bool) -> Move:
        return Move(p, p, cutting)

    moves = [move((0.0, top_k[0][1]), False)]
    moves += [move(p, True) for p in top_k]
    moves.append(move((air, top_k[-1][1]), True))
    moves.append(move((air, bottom_k[0][1]), False))
    moves += [move(p, True) for p in bottom_k]
    moves.append(move((0.0, bottom_k[-1][1]), True))
    moves.append(move((0.0, 0.0), False))
    toolpath = Toolpath(tuple(moves), foam_span=(min(z_root, z_tip), max(z_root, z_tip)))
    check_limits(toolpath, machine)

    slab = (top_line + bottom_line)[::-1]  # counter-clockwise
    block = _block(b0, b1, placement.y_offset, dy + hi + opt.spare, z_root, z_tip)
    zc = (z_root + z_tip) / 2
    side_svg = diagrams.side_view(
        (b0, placement.y_offset, b1, block["y1"]), [m(p) for p in fuse.side_outline()], [top_line, bottom_line],
        fence=placement.x_margin, support=placement.y_offset, title="Pass 1: the side view",
    )  # fmt: skip
    widest = max(fuse.sample_xs(fuse.length / 60), key=lambda x: fuse.width(x) * (fuse.top(x) - fuse.bottom(x)))
    end_svg = diagrams.section_view(
        [[(u, v + dy) for u, v in finished_section(fuse, widest)]],
        (-width / 2 - opt.spare, placement.y_offset, width / 2 + opt.spare, block["y1"]),
        "Seen from the nose end", support=placement.y_offset, note="top up, standing on its bottom face",
    )  # fmt: skip
    return CutJob(
        "",
        toolpath,
        z_root,
        z_tip,
        [Region(slab)],
        [Region(slab)],
        (kerf, kerf),
        (1.0, 1.0),
        block,
        notes=[
            (
                f"Foam block at least {_size(block)} (length × height × width along the wire): rear face at "
                f"X {b0:g} mm, underside at Y {placement.y_offset:g} mm, from {block['z0']:.0f} to {block['z1']:.0f} mm "
                "from the XY carriage. The nose points away from X = 0."
            ),
            (
                "This pass cuts the side view's top and bottom lines from end to end. Keep the slab between them: "
                "its flat end faces are the block's, and they locate it for pass 2."
            ),
        ],
        ghost=_ghost(fuse, lambda x, u, v: (nose_x + x, dy + v, zc + u)),
        steps=[
            {"title": "Pass 1 of 2: side view", "svg": side_svg, "notes": []},
            {"title": "", "svg": end_svg, "notes": []},
        ],
    )


def _plan_pass(fuse: Fuselage, opt: CutOptions, machine: Machine, placement: Placement, kerf: float) -> CutJob:
    lo, hi = fuse.height_range()
    width = fuse.max_width()
    nose_x = placement.x_margin + opt.spare
    plan = Placement(placement.root_side, placement.root_offset, nose_x, placement.y_offset + opt.spare)
    layout = layout_shapes([Region(fuse.plan_outline())], machine, plan, hi - lo, 1.0, (0.0, 0.0), kerf, kerf)
    # Put the nose itself (not the kerf path, which a pointed nose pushes forward) at nose_x.
    shift = nose_x - min(p[0] for p in layout.root_regions[0].outer)
    layout = _shift_layout(layout, shift)
    toolpath = shapes_toolpath(layout, machine)
    b1 = nose_x + fuse.length + opt.spare
    block = _block(placement.x_margin, b1, placement.y_offset, placement.y_offset + width + 2 * opt.spare,
                   layout.z_root, layout.z_tip)  # fmt: skip
    zlo = block["z0"]
    centre = placement.y_offset + opt.spare + width / 2
    plan_svg = diagrams.side_view(
        (placement.x_margin, placement.y_offset, b1, block["y1"]),
        [(nose_x + x, centre + u) for x, u in fuse.plan_outline()],
        [[(nose_x + x, centre + u) for x, u in fuse.plan_outline()]],
        fence=placement.x_margin, support=placement.y_offset, title="Pass 2: the top view", closed=True,
    )  # fmt: skip
    widest = max(fuse.sample_xs(fuse.length / 60), key=lambda x: fuse.width(x) * (fuse.top(x) - fuse.bottom(x)))
    turned = [(-(v - lo) + (hi - lo) / 2, u + centre) for u, v in finished_section(fuse, widest)]
    end_svg = diagrams.section_view(
        [turned], (-(hi - lo) / 2, placement.y_offset, (hi - lo) / 2, block["y1"]),
        "Turn the slab 90° onto its side", support=placement.y_offset, arrow=True, note="seen from the nose end",
    )  # fmt: skip
    return CutJob(
        "",
        toolpath,
        layout.z_root,
        layout.z_tip,
        layout.root_regions,
        layout.tip_regions,
        (kerf, kerf),
        (1.0, 1.0),
        block,
        notes=[
            (
                f"Lay the slab from pass 1 on its side on the same support (underside at Y {placement.y_offset:g} mm), "
                f"its rear end face against X {placement.x_margin:g} mm again, between {block['z0']:.0f} and "
                f"{block['z1']:.0f} mm from the XY carriage."
            ),
            (
                "This pass cuts the top view and frees the fuselage. Round its corners with a sanding block, "
                "checking against the formers in the plans (Preview → Plans & formers)."
            ),
        ],
        warnings=list(layout.warnings),
        ghost=_ghost(fuse, lambda x, u, v: (nose_x + x, centre + u, zlo + (v - lo))),
        steps=[
            {"title": "Pass 2 of 2: top view", "svg": end_svg, "notes": []},
            {"title": "", "svg": plan_svg, "notes": []},
        ],
    )


def _shift_layout(layout: ShapeLayout, dx: float) -> ShapeLayout:
    def s(points: list[Point]) -> list[Point]:
        return [(x + dx, y) for x, y in points]

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


def _shrink(ring: list[Point], d: float) -> list[Point]:
    """Move a ring towards its inside by ``d``."""
    return offset_polygon(ring, d if signed_area(ring) > 0 else -d)


def _segment(
    fuse: Fuselage,
    opt: CutOptions,
    k: int,
    left: bool,
    machine: Machine,
    placement: Placement,
    kerfs: KerfFn,
) -> CutJob:
    xa, xb = fuse.segments(opt.segment_length)[k]
    n = opt.points
    warnings: list[str] = []
    for x in (xa, xb):
        if fuse.width(x) < MIN_SECTION or fuse.top(x, False) - fuse.bottom(x, False) < MIN_SECTION:
            warnings.append(
                f"The section {x:.0f} mm from the nose is smaller than {MIN_SECTION:g} mm: it is cut "
                f"{MIN_SECTION:g} mm across (the wire cannot cut a point)."
            )
    outer = [fuse.section(x, n, minimum=MIN_SECTION) for x in (xa, xb)]
    inner: list[list[Point]] | None = None
    if opt.wall > 0:
        try:
            inner = [fuse.section(x, n, inset=opt.wall, minimum=MIN_SECTION) for x in (xa, xb)]
        except FuselageError:
            warnings.append(f"The {opt.wall:g} mm wall does not fit in this segment's smaller end: it is cut solid.")

    # The larger end goes next to the carriage the placement measures from, where the wire's
    # extension beyond the foam is shortest; the smaller end faces the other carriage.
    big_first = polyline_length(outer[0] + outer[0][:1]) >= polyline_length(outer[1] + outer[1][:1])
    ends = (xa, xb) if big_first else (xb, xa)
    if not big_first:
        outer.reverse()
        if inner:
            inner.reverse()

    # The fuselage lies on its side: its bottom towards X = 0, sideways along Y. A left half is
    # the right half upside down (flat centre-line face on top).
    flip = -1.0 if left else 1.0

    def lay(ring: list[Point]) -> list[Point]:
        return [(v, flip * u) for u, v in ring]

    if opt.halves:
        nominal = [lay(half_ring(o, i)) for o, i in zip(outer, inner or [None, None])]
        holes: list[list[list[Point]]] = [[], []]
    else:
        nominal = [lay(o) for o in outer]
        holes = [[lay(i)] for i in inner] if inner else [[], []]

    ratio_raw = [polyline_length(r + r[:1]) for r in nominal]
    longest = max(ratio_raw)
    ratio = (ratio_raw[0] / longest, ratio_raw[1] / longest)
    kerf = kerfs(ratio)

    def cut(ring: list[Point], ring_holes: list[list[Point]], k_: float) -> list[Point]:
        path = apply_kerf(ring, k_)
        if not ring_holes:
            return path
        hole = _shrink(ring_holes[0], k_ / 2)
        # In along a slit from the bottom centre (index 0 of both), once round the hollow, back out.
        return [path[0], *hole, hole[0], *path]

    root_path = cut(nominal[0], holes[0], kerf[0])
    tip_path = cut(nominal[1], holes[1], kerf[1])
    z_root, z_tip = span_positions(xb - xa, machine, placement)
    xy, uv = project_to_towers(root_path, z_root, tip_path, z_tip, machine.tower_distance)

    allp = [p for r in nominal for p in r]
    dx = placement.x_margin + opt.spare - min(p[0] for p in allp)
    dy = placement.y_offset + opt.spare - min(p[1] for p in allp)
    # Beyond the foam the wire carries on to the far carriage, which swings round the fuselage's axis
    # (more so for a strongly tapered segment): lift the foam if that would go below X or Y = 0.
    lift_x = max(0.0, 1.0 - min(p[0] + dx for p in xy + uv))
    lift_y = max(0.0, 1.0 - min(p[1] + dy for p in xy + uv))
    dx, dy = dx + lift_x, dy + lift_y
    if lift_y > 0.05:
        warnings.append(
            f"The foam sits {lift_y:.0f} mm above the placement's underside (Y {placement.y_offset:g} mm), so that "
            "the far carriage stays above Y = 0: put the block on a spacer."
        )
    if lift_x > 0.05:
        warnings.append(
            f"The foam's rear face is {lift_x:.0f} mm further from X = 0 than the placement says, so that the far "
            "carriage stays within its travel."
        )

    def sh(points: list[Point]) -> list[Point]:
        return [(x + dx, y + dy) for x, y in points]

    def region(ring: list[Point], ring_holes: list[list[Point]]) -> Region:
        outer_ring = sh(ring if signed_area(ring) > 0 else ring[::-1])
        return Region(outer_ring, [sh(h if signed_area(h) < 0 else h[::-1]) for h in ring_holes])

    layout = WingLayout(z_root, z_tip, sh(nominal[0]), sh(nominal[1]), sh(xy), sh(uv))
    toolpath = wing_toolpath(layout, machine)
    xs = [p[0] + dx for p in allp]
    ys = [p[1] + dy for p in allp]
    rear, under = placement.x_margin + lift_x, placement.y_offset + lift_y
    block = _block(rear, max(xs) + opt.spare, under, max(ys) + opt.spare, z_root, z_tip)
    near = placement.root_side.upper()
    which = f"its {'front' if big_first else 'back'} end ({ends[0]:.0f} mm from the nose) at the {near} carriage side"
    notes = [
        (
            f"Foam block at least {_size(block)} (depth × height × length along the wire): rear face at "
            f"X {rear:.0f} mm, underside at Y {under:.0f} mm, from {block['z0']:.0f} to "
            f"{block['z1']:.0f} mm from the XY carriage."
        ),
        (
            f"The {'left' if left else 'right'} half lies with its flat centre-line face "
            f"{'up' if left else 'down'}, its bottom towards X = 0 and {which}."
            if opt.halves
            else f"The fuselage lies on its side, its bottom towards X = 0 and {which}."
        ),
    ]
    if inner and not opt.halves:
        notes.append("The hollow is reached through a slit along the bottom centre line: glue it closed.")
    if opt.halves:
        notes.append("Cut the right and the left half of every segment, then glue them along the centre line.")
    notes.append("Glue the segments in order, nose first: their ends match the formers in the plans.")
    svg = diagrams.section_view(
        [sh(nominal[0]), *[sh(h) for h in holes[0]]] if not opt.halves else [sh(nominal[0])],
        (block["x0"], block["y0"], block["x1"], block["y1"]),
        f"Segment {k + 1}: {'right' if not left else 'left'} half" if opt.halves else f"Segment {k + 1}",
        support=under, note=f"seen along the wire, from the {near} carriage side: X = 0 to the left",
    )  # fmt: skip
    return CutJob(
        "",
        toolpath,
        z_root,
        z_tip,
        [region(nominal[0], holes[0])],
        [region(nominal[1], holes[1])],
        kerf,
        ratio,
        block,
        notes,
        warnings,
        steps=[{"title": "", "svg": svg, "notes": []}],
    )


# Turned -----------------------------------------------------------------------------------------------


def _turned(fuse: Fuselage, opt: CutOptions, machine: Machine, placement: Placement, kerf: float) -> CutJob:
    """Every pass cuts the top silhouette; between passes the block turns by 360 / facets degrees."""
    sides = opt.facets
    step = 360.0 / sides
    rotary = bool(machine.rotary_axis)
    y_axis_f = opt.axis if opt.axis is not None else auto_axis(fuse)
    xs = fuse.sample_xs(max(1.5, fuse.length / 300))
    rings = {x: [(u, v - y_axis_f) for u, v in finished_section(fuse, x, 96)] for x in xs}
    radius = max(math.hypot(u, w) for ring in rings.values() for u, w in ring)
    half = radius + opt.spare  # the block: a square round the axis
    warnings: list[str] = []
    thin = [
        x
        for x in xs[::3]
        if 0.02 * fuse.length < x < 0.98 * fuse.length
        and min(ray_distance(rings[x], (0.0, 0.0), math.radians(a)) for a in range(0, 360, 20)) < opt.hold
    ]
    if thin:
        warnings.append(
            f"Between {min(thin):.0f} and {max(thin):.0f} mm from the nose the turning axis runs close to (or "
            f"outside) the fuselage: {opt.hold:g} mm of foam is kept round it there; carve that by hand, or move "
            "the axis."
        )

    # Where the block goes.
    plate_t = 0.0 if rotary else opt.plate
    if rotary:
        apothem = None
        y_axis = machine.rotary_height
        if y_axis - half * math.sqrt(2) < placement.y_offset:
            warnings.append(
                f"On the rotary axis at Y {y_axis:g} mm the turning block reaches down to "
                f"Y {y_axis - half * math.sqrt(2):.0f} mm: check it clears the table."
            )
        top_clear = y_axis + half * math.sqrt(2) + 5
    else:
        apothem = half * math.sqrt(2) + 3  # the block's corners never touch the support
        y_axis = placement.y_offset + apothem
        top_clear = y_axis + apothem / math.cos(math.pi / sides) + 5
    if top_clear > machine.y_travel:
        raise FuselageError(
            f"the wire must pass {top_clear:.0f} mm high to clear the turning block"
            f"{'' if rotary else ' and its jig plates'}, more than the machine's {machine.y_travel:g} mm: use a "
            "machine with more vertical travel, or less spare foam"
        )
    stub = max(opt.spare, 10.0)
    b0 = placement.x_margin + plate_t
    nose_x = b0 + stub
    b1 = nose_x + fuse.length + stub
    x_in, x_out = b0 + 3.0, b1 - 3.0
    z_root, z_tip = span_positions(2 * half, machine, placement)
    zc = (z_root + z_tip) / 2

    # The passes: pass k cuts the top silhouette with the block turned clockwise by (k - 1) * step.
    def silhouette(k: int) -> list[float]:
        b = math.radians((k - 1) * step)
        sb, cb = math.sin(b), math.cos(b)
        return [max(opt.hold, max(-u * sb + w * cb for u, w in rings[x])) for x in xs]

    heights = [silhouette(k) for k in range(1, sides + 1)]
    moves: list[Move] = [Move((0.0, top_clear), (0.0, top_clear), False)]
    stops: list[Stop] = []
    for k, hs in enumerate(heights, start=1):
        if k > 1:
            stops.append(
                Stop(len(moves), f"pass {k} of {sides}: turn to {(k - 1) * step:g} deg", (k - 1) * step)
                if rotary
                else Stop(
                    len(moves),
                    f"Pass {k} of {sides}: roll the block onto edge {k}, the same way round as before, and lift off "
                    "the loose foam.",
                )
            )
        line = (
            [(x_in, y_axis + hs[0])] + [(nose_x + x, y_axis + h) for x, h in zip(xs, hs)] + [(x_out, y_axis + hs[-1])]
        )
        cut = offset_polyline(line, kerf / 2) if kerf else line
        moves.append(Move((x_in, top_clear), (x_in, top_clear), False))
        moves += [Move(p, p, True) for p in cut]
        moves.append(Move((x_out, top_clear), (x_out, top_clear), True))
        moves.append(Move((0.0, top_clear), (0.0, top_clear), False))
    moves.append(Move((0.0, 0.0), (0.0, 0.0), False))
    toolpath = Toolpath(tuple(moves), foam_span=(min(z_root, z_tip), max(z_root, z_tip)), stops=tuple(stops))
    check_limits(toolpath, machine)

    # The faceted result: each section is cut down to the polygon the passes leave.
    normals = [math.radians(90 + (k - 1) * step) for k in range(1, sides + 1)]

    def facet_radius(i: int, angle: float) -> float:
        return min(
            heights[k][i] / math.cos(angle - normals[k]) for k in range(sides) if math.cos(angle - normals[k]) > 1e-6
        )

    rays = [2 * math.pi * j / 48 for j in range(48)]
    excess = 0.0
    for i, x in enumerate(xs):
        if 0.05 * fuse.length < x < 0.95 * fuse.length:
            excess = max(excess, max(facet_radius(i, a) - ray_distance(rings[x], (0.0, 0.0), a) for a in rays[::4]))
    every = max(1, len(xs) // 60)
    ghost = [
        [(nose_x + x, y_axis + facet_radius(i, a) * math.sin(a), zc + facet_radius(i, a) * math.cos(a)) for a in rays]
        for i, x in list(enumerate(xs))[::every]
    ]
    block_box = _block(b0, b1, y_axis - half, y_axis + half, z_root, z_tip)
    box = [(b0, y_axis - half), (b1, y_axis - half), (b1, y_axis + half), (b0, y_axis + half)]

    # Steps, with drawings.
    widest = max(xs, key=lambda x: abs(signed_area(rings[x])))
    near = placement.root_side.upper()
    steps: list[dict[str, object]] = []
    if rotary:
        prepare = [
            (
                f"Cut a foam block {2 * half:.0f} × {2 * half:.0f} mm square and {b1 - b0:.0f} mm long. The fuselage "
                f"axis runs through the centre of both end faces, {y_axis_f - fuse.bottom(0, False):.0f} mm above the "
                "nose's bottom line."
            ),
            (
                f"Mount it on the rotary axis ({machine.rotary_axis}, at Y {y_axis:g} mm) through those centres, "
                f"nose end towards X = 0 with its end face at X {b0:g} mm, bottom face down at 0°, between "
                f"{block_box['z0']:.0f} and {block_box['z1']:.0f} mm from the {near} carriage."
            ),
        ]
    else:
        assert apothem is not None
        prepare = [
            (
                f"Cut a foam block {2 * half:.0f} × {2 * half:.0f} mm square and {b1 - b0:.0f} mm long, and mark the "
                "centre of both end faces: the fuselage axis."
            ),
            (
                f"Make two {sides}-sided jig plates from {plate_t:g} mm plywood or MDF, {2 * apothem:.0f} mm across the "
                f"flats (print the template in the cutting guide). Number the edges 1 to {sides} on the nose-end plate, "
                "counter-clockwise as you look at it."
            ),
            (
                "Screw or glue a plate to each end face, centred on the axis mark, with the block's bottom face parallel "
                "to edge 1 and both plates' edges lined up."
            ),
            (
                f"Stand the jig on edge 1 on a flat board at Y {placement.y_offset:g} mm, the nose-end plate against a "
                f"stop at X {placement.x_margin:g} mm, between {block_box['z0']:.0f} and {block_box['z1']:.0f} mm from "
                f"the {near} carriage. Start the job."
            ),
        ]
    steps.append({"title": "Prepare the block" + ("" if rotary else " and the jig"), "notes": prepare, "svg": ""})
    side_svg = diagrams.side_view(
        (b0, y_axis - half, b1, y_axis + half),
        [(nose_x + x, y_axis + y - y_axis_f) for x, y in fuse.side_outline()],
        [[(x_in, top_clear)] + [(nose_x + x, y_axis + h) for x, h in zip(xs, heights[0])] + [(x_out, top_clear)]],
        fence=placement.x_margin, support=None if rotary else placement.y_offset,
        plates=None if rotary else (b0, b1, apothem / math.cos(math.pi / sides), max(plate_t, 2.0)),
        title="Each pass: down, along the top, back up",
    )  # fmt: skip
    for k in range(1, sides + 1):
        notes = []
        if k == 1:
            notes.append("Edge 1 down." if not rotary else "The rotary axis at 0°.")
        elif rotary:
            notes.append(f"The rotary axis turns to {(k - 1) * step:g}° by itself.")
        else:
            notes.append(
                f"The job pauses with the wire off: roll the block onto edge {k}, always the same way round, and "
                "lift off the loose foam. Then press Resume."
            )
        if k == sides:
            notes.append(
                f"Finally saw the fuselage off the end stubs and sand the corners round (they stand up to "
                f"{excess:.1f} mm proud)."
            )
        steps.append({
            "title": f"Pass {k} of {sides}",
            "notes": notes,
            "svg": diagrams.turned_end_view(
                rings[widest], half, sides, k, heights[k - 1][xs.index(widest)],
                [heights[j][xs.index(widest)] for j in range(k - 1)], apothem,
            ),
        })  # fmt: skip
    steps.insert(1, {"title": "", "notes": [], "svg": side_svg})

    notes = [str(n) for n in prepare] + [
        (
            f"The rotary axis turns the block {step:g}° between passes."
            if rotary
            else f"Between passes the job pauses with the wire off: roll the block onto the next edge (2, 3, … "
            f"{sides}), always the same way round, then press Resume."
        ),
        f"Afterwards saw the fuselage off the end stubs and sand the {sides} corners round (up to {excess:.1f} mm).",
    ]
    jig = None
    if not rotary:
        assert apothem is not None
        jig = {"sides": sides, "apothem": apothem, "circumradius": apothem / math.cos(math.pi / sides),
               "thickness": plate_t, "half": half}  # fmt: skip
    return CutJob(
        "",
        toolpath,
        z_root,
        z_tip,
        [Region(box)],
        [Region(box)],
        (kerf, kerf),
        (1.0, 1.0),
        block_box | {"y0": min(block_box["y0"], placement.y_offset)},
        notes,
        warnings,
        ghost,
        f"The {sides}-sided fuselage it cuts",
        steps,
        jig,
    )
