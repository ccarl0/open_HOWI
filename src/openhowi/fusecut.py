# SPDX-License-Identifier: GPL-3.0-or-later
"""Cutting a fuselage: the two-view passes, or lengthwise segments.

Two views
    Pass 1 cuts the side view's top and bottom lines from one end of the block
    to the other, so the slab between them keeps the block's flat end faces;
    those locate it for pass 2, where it lies on its side and the top view is
    cut out. The sections come out square-ish and are rounded by hand.

Lengthwise segments
    The wire runs along the fuselage. Each segment is a ruled surface between
    the sections at its ends, like a wing panel: the sections come out
    rounded. A segment can be hollow (reached through a slit along the bottom
    centre line) or cut as a left and a right half shell, open along the
    centre line, which needs no slit. The fuselage lies on its side, bottom
    towards the back of the machine (X = 0).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .fuselage import MIN_SECTION, Fuselage, FuselageError, half_ring
from .geometry import Point, offset_polygon, offset_polyline, polyline_length, signed_area
from .machine import Machine
from .shapes import Region, ShapeLayout, layout_shapes, shapes_toolpath
from .toolpath import Move, Placement, Toolpath, WingLayout, check_limits, span_positions, wing_toolpath
from .wing import apply_kerf, project_to_towers

METHODS = ("two_view", "segments")
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

    def __post_init__(self) -> None:
        if self.method not in METHODS:
            raise ValueError('cut method must be "two_view" or "segments"')
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


KerfFn = Callable[[tuple[float, float]], tuple[float, float]]


def job_labels(fuse: Fuselage, options: CutOptions) -> list[str]:
    if options.method == "two_view":
        return ["Pass 1 · side view", "Pass 2 · top view"]
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
    )
