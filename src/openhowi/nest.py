# SPDX-License-Identifier: GPL-3.0-or-later
"""Nesting: several parts cut from one foam block in one job.

The wire spans the whole block, so every part's cut runs from one end of the
block to the other; the parts sit side by side across the block's section.
They are stacked in columns, bottom to top, the first column at the back of
the block (towards X = 0) and further columns in front of it.

Each part is packed by its bounding box at the two ends of the block. The wire
moves straight from one end to the other, so between the ends a part stays
inside that box, and boxes kept apart at both ends never meet anywhere along
the block.

The wire reaches a part in the first column straight from behind the block, as
it would on its own. A part further forward is reached over the top of the
columns behind it: along a cut above them, down through the gap behind its own
column, then in to the part. It leaves the same way.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .geometry import Point, signed_area
from .machine import Machine
from .toolpath import Move, PlacementError, Toolpath, check_limits
from .wing import project_to_towers

Path = tuple[list[Point], list[Point]]
"""A cut through the foam as the two carriages follow it: (XY points, UV points)."""


class NestError(PlacementError):
    """The parts cannot be nested in one block."""


@dataclass
class NestItem:
    label: str
    paths: list[Path]
    """The part's cuts through the foam, each from where the wire comes in to where it leaves.
    The wire reaches each one straight from behind the part, at the height where it starts."""
    z_root: float
    z_tip: float


@dataclass
class Placed:
    item: NestItem
    dx: float
    dy: float
    column: int


@dataclass
class NestResult:
    toolpath: Toolpath
    placed: list[Placed]
    z0: float
    z1: float
    box: tuple[float, float, float, float]
    """Everything cut, at the block's ends: (x0, y0, x1, y1)."""
    warnings: list[str] = field(default_factory=list)


def at(path: Path, z: float, tower_distance: float) -> list[Point]:
    """Where the wire crosses the plane ``z`` mm from the XY carriage."""
    t = z / tower_distance
    return [(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t) for a, b in zip(*path)]


def _box(points: Sequence[Point]) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def item_paths(toolpath: Toolpath) -> list[Path]:
    """Split a part's toolpath into its cuts through the foam (each between two visits to X = 0)."""
    paths: list[Path] = []
    current: list[Move] = []
    for move in toolpath.moves:
        if not move.cutting:
            current = []
            continue
        if abs(move.xy[0]) < 1e-9 and abs(move.uv[0]) < 1e-9 and current:
            paths.append(([m.xy for m in current], [m.uv for m in current]))
            current = []
            continue
        current.append(move)
    return paths


def nest(
    items: Sequence[NestItem],
    machine: Machine,
    x_margin: float,
    gap: float,
    base_y: float | None = None,
    max_height: float | None = None,
) -> NestResult:
    """Pack ``items`` (each placed as if cut on its own, its back at ``x_margin``) into one block.

    ``base_y``: height of the bottom of the lowest part in each column; None keeps the first part
    where it is. ``max_height``: tallest column; None fills the machine's vertical travel.
    """
    if not items:
        raise NestError("nothing to nest")
    if gap < 1:
        raise NestError("the gap between nested parts must be at least 1 mm")
    d = machine.tower_distance
    z0 = min(min(i.z_root, i.z_tip) for i in items)
    z1 = max(max(i.z_root, i.z_tip) for i in items)
    warnings: list[str] = []

    boxes = []
    for item in items:
        ends = [p for path in item.paths for z in (z0, z1) for p in at(path, z, d)]
        boxes.append(_box(ends))
        _check_extension(item, z0, z1, d)

    first_bottom = boxes[0][1] if base_y is None else base_y
    top_limit = first_bottom + max_height if max_height is not None else machine.y_travel - gap
    placed: list[Placed] = []
    columns: list[list[int]] = [[]]
    column_top = first_bottom - gap
    for k, (bx0, by0, bx1, by1) in enumerate(boxes):
        height = by1 - by0
        if columns[-1] and column_top + gap + height > top_limit + 1e-6:
            columns.append([])
            column_top = first_bottom - gap
        bottom = column_top + gap
        columns[-1].append(k)
        column_top = bottom + height
        placed.append(Placed(items[k], 0.0, bottom - by0, len(columns) - 1))
        if column_top > top_limit + 1e-6:
            raise NestError(
                f"{items[k].label} is {height:.0f} mm high and does not fit in the {top_limit - first_bottom:.0f} mm "
                "of height the block may have"
            )
    if base_y is None:
        placed[0].dy = 0.0

    # Columns front to back: the first keeps its place (as cut on its own), the others stand in front.
    fronts: list[float] = []
    for c, members in enumerate(columns):
        if c == 0:
            fronts.append(max(boxes[k][2] for k in members))
            continue
        rear = fronts[-1] + gap
        for k in members:
            placed[k].dx = rear - boxes[k][0]
        fronts.append(max(boxes[k][2] + placed[k].dx for k in members))

    # Cut column by column, bottom to top; route over the columns behind.
    moves: list[Move] = []
    tops = [max(boxes[k][3] + placed[k].dy for k in members) for members in columns]
    for c, members in enumerate(columns):
        route = max(tops[:c]) + gap / 2 if c else None
        if route is not None and route > machine.y_travel:
            raise NestError("the parts are stacked too high to reach the front columns over the back ones")
        x_gap = fronts[c - 1] + gap / 2 if c else 0.0
        for k in members:
            pl = placed[k]
            for path in pl.item.paths:
                xy = [(x + pl.dx, y + pl.dy) for x, y in path[0]]
                uv = [(x + pl.dx, y + pl.dy) for x, y in path[1]]
                moves += _visit(xy, uv, z0, z1, d, route, x_gap)
    moves.append(Move((0.0, 0.0), (0.0, 0.0), cutting=False))
    toolpath = Toolpath(tuple(moves), foam_span=(z0, z1))
    check_limits(toolpath, machine)
    shifted = [(b[0] + p.dx, b[1] + p.dy, b[2] + p.dx, b[3] + p.dy) for b, p in zip(boxes, placed)]
    box = (min(b[0] for b in shifted), min(b[1] for b in shifted), max(b[2] for b in shifted),
           max(b[3] for b in shifted))  # fmt: skip
    if len(columns) > 1:
        warnings.append(
            f"The parts are nested in {len(columns)} columns: the wire reaches the front ones through cuts over the "
            "parts behind. Weigh the block down so the slices stay put."
        )
    return NestResult(toolpath, placed, z0, z1, box, warnings)


def _visit(
    xy: list[Point], uv: list[Point], z0: float, z1: float, d: float, route: float | None, x_gap: float
) -> list[Move]:
    """Air to the start, in to the cut, round it and back out the same way."""
    if route is None:  # straight in from behind the block, as on its own
        lead_xy, lead_uv = [(0.0, xy[0][1])], [(0.0, uv[0][1])]
        out_xy, out_uv = [(0.0, xy[-1][1])], [(0.0, uv[-1][1])]
    else:
        # Waypoints fixed in the foam at both ends of the block, then carried out to the carriages.
        def waypoints(first: bool) -> tuple[list[Point], list[Point]]:
            k = 0 if first else -1
            e0 = at(([xy[k]], [uv[k]]), z0, d)[0]
            e1 = at(([xy[k]], [uv[k]]), z1, d)[0]
            a = [(0.0, route), (x_gap, route), (x_gap, e0[1])]
            b = [(0.0, route), (x_gap, route), (x_gap, e1[1])]
            return project_to_towers(a, z0, b, z1, d)

        lead_xy, lead_uv = waypoints(True)
        back_xy, back_uv = waypoints(False)
        out_xy, out_uv = back_xy[::-1], back_uv[::-1]
    moves = [Move(lead_xy[0], lead_uv[0], cutting=False)]
    moves += [Move(a, b, cutting=True) for a, b in zip(lead_xy[1:], lead_uv[1:])]
    moves += [Move(a, b, cutting=True) for a, b in zip(xy, uv)]
    moves += [Move(a, b, cutting=True) for a, b in zip(out_xy, out_uv)]
    return moves


def _check_extension(item: NestItem, z0: float, z1: float, d: float) -> None:
    """A part shorter than the block is cut on to its ends: refuse when that turns the cut inside out."""
    span = abs(item.z_tip - item.z_root)
    if span >= z1 - z0 - 1e-6:
        return
    for path in item.paths:
        own = [signed_area(at(path, z, d)) for z in (item.z_root, item.z_tip)]
        for z in (z0, z1):
            area = signed_area(at(path, z, d))
            if min(abs(a) for a in own) < 1e-6:
                continue
            if area * own[0] <= 0 or abs(area) < 0.2 * min(abs(a) for a in own):
                raise NestError(
                    f"{item.label} is {span:.0f} mm long but the block is {z1 - z0:.0f} mm: carried on to the end of "
                    "the block, its tapered cut would shrink to nothing or turn inside out. Nest it with parts of "
                    "the same length, or cut it on its own."
                )
