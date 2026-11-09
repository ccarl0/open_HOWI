# SPDX-License-Identifier: GPL-3.0-or-later
"""Foam core sections for built-up wings: sheeting, LE/TE stock, spar slots and servo channels.

The core is the airfoil shrunk by the sheeting thickness (top and bottom can
differ), optionally cut back at the leading and trailing edges for hardwood
or balsa stock, with rectangular slots for spar caps and round servo-lead
channels. Where the airfoil gets thinner than the two sheets together (near
the trailing edge, or at a sharp leading edge), the core ends where the
shrunk surfaces meet.

Servo channels and joiner holes are cut the way builders do it by hand: the
wire comes in through a straight slit from the top or bottom surface, goes
round the hole and leaves along the slit. A joiner hole is a straight tube
or blade-shaped hole that may run at an angle to the span; its section in
each face is worked out from the joiner's axis, so the hole comes out true.

Each section is built as a list of runs (surface curves, slot corners,
channel entries) so that root and tip can be resampled to exactly the same
number of points.
"""

from __future__ import annotations

import bisect
import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from .airfoil import Airfoil
from .geometry import Point, cosine_spacing, dedupe, offset_polyline, outline_gap, polyline_length, resample

MIN_WEB = 1.0
"""Thinnest foam (mm) a slot or channel may leave."""
CIRCLE_POINTS = 32
"""Points around a round hole (the same at root and tip)."""
HOLE_SHAPES = ("round", "rect")


class CoreError(ValueError):
    """The requested core cannot be made from this airfoil."""


def _check_position(name: str, value: float | None) -> None:
    if value is not None and not 0 < value < 100:
        raise ValueError(f"{name} must be between 0 and 100 % of the chord")


@dataclass(frozen=True)
class Spar:
    """Rectangular slots for spar caps, in the top and/or bottom of the core."""

    position: float = 25.0
    """Centre of the slots at the root, % of chord from the leading edge."""
    width: float = 6.0
    depth_top: float = 0.0
    """Depth below the core surface (mm); 0 = no slot on top."""
    depth_bottom: float = 0.0
    position_tip: float | None = None
    """Position at the tip (% of chord); defaults to the root position."""

    def __post_init__(self) -> None:
        _check_position("position", self.position)
        _check_position("position_tip", self.position_tip)
        if self.width <= 0:
            raise ValueError("width must be > 0")
        if self.depth_top < 0 or self.depth_bottom < 0:
            raise ValueError("depths must be >= 0")

    def at(self, tip: bool) -> float:
        return self.position_tip if tip and self.position_tip is not None else self.position


@dataclass(frozen=True)
class Channel:
    """A round spanwise channel for servo leads, reached through a slit from one surface."""

    position: float = 45.0
    """Centre at the root, % of chord from the leading edge."""
    diameter: float = 8.0
    offset: float = 0.0
    """Height of the centre above the middle of the core (mm)."""
    side: str = "bottom"
    """Surface the slit comes in from: "bottom" or "top"."""
    position_tip: float | None = None

    def __post_init__(self) -> None:
        _check_position("position", self.position)
        _check_position("position_tip", self.position_tip)
        if self.diameter <= 0:
            raise ValueError("diameter must be > 0")
        if self.side not in ("bottom", "top"):
            raise ValueError('side must be "bottom" or "top"')

    def at(self, tip: bool) -> float:
        return self.position_tip if tip and self.position_tip is not None else self.position


@dataclass(frozen=True)
class Joiner:
    """A straight hole through the whole panel for a wing joiner: a round tube or rod, or a flat blade."""

    position: float = 30.0
    """Centre at the root, % of the root chord from the leading edge."""
    shape: str = "round"
    """"round" (tube, rod) or "rect" (blade, box)."""
    diameter: float = 10.0
    """Round joiners."""
    width: float = 4.0
    """Rectangular joiners: size along the chord (mm)."""
    height: float = 20.0
    """Rectangular joiners: size up and down (mm)."""
    offset: float = 0.0
    """Height of the centre above the middle of the root core (mm)."""
    side: str = "bottom"
    """Surface the slit comes in from: "bottom" or "top"."""
    position_tip: float | None = None
    """Where the hole comes out of the tip, % of the tip chord. Blank: square to the root face."""
    dihedral: float = 0.0
    """Total dihedral at the root joint (degrees) for a straight joiner: the hole then runs
    down towards the tip by half of it. 0 for a joiner bent to the dihedral, or a flat wing."""

    def __post_init__(self) -> None:
        _check_position("position", self.position)
        _check_position("position_tip", self.position_tip)
        if self.shape not in HOLE_SHAPES:
            raise ValueError('shape must be "round" or "rect"')
        if self.diameter <= 0 or self.width <= 0 or self.height <= 0:
            raise ValueError("joiner sizes must be > 0")
        if self.side not in ("bottom", "top"):
            raise ValueError('side must be "bottom" or "top"')
        if not -40 < self.dihedral < 40:
            raise ValueError("dihedral must be between -40 and 40 degrees")

    @property
    def size(self) -> tuple[float, float]:
        return (self.diameter, self.diameter) if self.shape == "round" else (self.width, self.height)

    def describe(self) -> str:
        w, h = self.size
        return f"{w:g} mm round" if self.shape == "round" else f"{w:g} x {h:g} mm"


@dataclass(frozen=True)
class ServoBay:
    """A pocket for a wing servo. It stops short of the root and tip, so the wire cannot cut it:
    it is checked against the core and cut by hand from a printed template."""

    station: float = 150.0
    """Centre of the bay, mm from the root face."""
    position: float = 40.0
    """Centre of the bay, % of the local chord from the leading edge."""
    length: float = 35.0
    """Along the span (mm)."""
    width: float = 15.0
    """Along the chord (mm)."""
    depth: float = 12.0
    """Below the core surface at the centre of the bay (mm)."""
    side: str = "bottom"
    """"bottom" or "top"."""

    def __post_init__(self) -> None:
        _check_position("position", self.position)
        if self.length <= 0 or self.width <= 0 or self.depth <= 0:
            raise ValueError("servo bay length, width and depth must be > 0")
        if self.station <= 0:
            raise ValueError("station must be > 0")
        if self.side not in ("bottom", "top"):
            raise ValueError('side must be "bottom" or "top"')

    def describe(self) -> str:
        return f"{self.length:g} x {self.width:g} mm, {self.depth:g} mm deep, {self.side}"


@dataclass(frozen=True)
class HoleShape:
    """The section of a spanwise hole in one face, reached through a slit from one surface."""

    shape: str
    """"round" or "rect"."""
    width: float
    """Across (the diameter of a round hole)."""
    height: float
    side: str
    basis: tuple[Point, Point] = ((1.0, 0.0), (0.0, 1.0))
    """Where the hole's own across and up directions land in the face. Longer than 1 when
    the hole runs at an angle to the span: its section in the face is then stretched."""

    def rotated(self, degrees: float) -> HoleShape:
        """The same hole in a frame turned like ``wing.position_section`` turns a section."""
        c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
        basis = tuple((x * c - y * s, x * s + y * c) for x, y in self.basis)
        return HoleShape(self.shape, self.width, self.height, self.side, basis)  # type: ignore[arg-type]

    def ring(self, center: Point, inset: float = 0.0, n: int = CIRCLE_POINTS) -> list[Point]:
        """Closed outline (first point repeated at the end), counter-clockwise, starting where the slit meets it.

        ``inset`` shrinks the hole on every side, e.g. by half the kerf for the wire path.
        """
        a, b = self.width / 2 - inset, self.height / 2 - inset
        if min(a, b) < 0.125:
            raise CoreError(f"a {self.describe()} hole is narrower than the {2 * inset:g} mm kerf")
        sign = 1.0 if self.side == "top" else -1.0
        if self.shape == "round":
            t0 = sign * math.pi / 2
            uv = [
                (a * math.cos(t0 + 2 * math.pi * k / n), b * math.sin(t0 + 2 * math.pi * k / n)) for k in range(n + 1)
            ]
        else:
            uv = [(0.0, sign * b), (-sign * a, sign * b), (-sign * a, -sign * b), (sign * a, -sign * b),
                  (sign * a, sign * b), (0.0, sign * b)]  # fmt: skip
        (e1x, e1y), (e2x, e2y) = self.basis
        return [(center[0] + u * e1x + v * e2x, center[1] + u * e1y + v * e2y) for u, v in uv]

    def outline(self, center: Point) -> list[Point]:
        """The finished hole as a clockwise ring without a repeated end point, for display."""
        return self.ring(center)[-2::-1]

    def describe(self) -> str:
        return f"{self.width:g} mm round" if self.shape == "round" else f"{self.width:g} x {self.height:g} mm"


@dataclass(frozen=True)
class Hole:
    """A wire-cut hole in one face, in the section's own frame (leading edge at x = 0, chord along x)."""

    label: str
    tag: str
    shape: HoleShape
    x: float
    """x of the centre."""
    y: float | None = None
    """y of the centre. None: the middle of the core at x, plus ``offset``."""
    offset: float = 0.0


@dataclass(frozen=True)
class CoreSpec:
    sheeting_top: float = 0.0
    sheeting_bottom: float = 0.0
    le_stock: float = 0.0
    """Cut the core back this far (mm, along the chord) from the leading edge."""
    te_stock: float = 0.0
    """Cut the core back this far (mm) from the trailing edge."""
    spars: tuple[Spar, ...] = ()
    channels: tuple[Channel, ...] = ()
    joiners: tuple[Joiner, ...] = ()
    """Listed here for completeness; their holes depend on the whole panel, so the wing
    resolves them per face and hands them to ``core_runs`` as ``holes``."""

    def __post_init__(self) -> None:
        for name in ("sheeting_top", "sheeting_bottom", "le_stock", "te_stock"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        for name in ("spars", "channels", "joiners"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def sheeted(self) -> bool:
        return self.sheeting_top > 0 or self.sheeting_bottom > 0

    @property
    def slotted(self) -> bool:
        return any(s.depth_top > 0 or s.depth_bottom > 0 for s in self.spars)

    @property
    def plain(self) -> bool:
        return not (self.sheeted or self.le_stock or self.te_stock or self.slotted or self.channels or self.joiners)


@dataclass
class Run:
    kind: str
    """"curve" (a stretch of surface, resampled) or "corner" (fixed points)."""
    surface: int
    """0 = upper, 1 = lower."""
    points: list[Point]
    skip_first: bool = False
    """The run starts on the previous run's last point; leave that duplicate out."""
    tag: str | None = None
    """Names the run's last point (where the slit into a hole starts)."""


@dataclass
class HoleGeometry:
    tag: str
    center: Point
    shape: HoleShape


@dataclass
class CoreRuns:
    """One core section, untwisted, leading edge at x = 0, as runs of points."""

    runs: list[Run] = field(default_factory=list)
    holes: list[HoleGeometry] = field(default_factory=list)
    """Servo channels, then joiners, in the order given."""
    te_cutback: float = 0.0
    """Distance (mm) from the airfoil's trailing edge to the core's."""
    le_cutback: float = 0.0
    order: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())
    """Slots and slits on the top and bottom surface, front to back. Root and tip must agree."""


def _monotone(points: list[Point], decreasing: bool) -> list[Point]:
    """Drop points that double back in x (offset loops at a tight leading edge)."""
    out: list[Point] = []
    for p in points:
        if not out or (p[0] < out[-1][0] if decreasing else p[0] > out[-1][0]):
            out.append(p)
    return out


class _Curve:
    """A surface as y(x), from points with increasing x."""

    def __init__(self, points: list[Point]):
        self.xs = [p[0] for p in points]
        self.ys = [p[1] for p in points]

    def __call__(self, x: float) -> float:
        xs, ys = self.xs, self.ys
        if x <= xs[0]:
            return ys[0]
        if x >= xs[-1]:
            return ys[-1]
        i = bisect.bisect_right(xs, x) - 1
        t = (x - xs[i]) / (xs[i + 1] - xs[i])
        return ys[i] + (ys[i + 1] - ys[i]) * t


def _clip(points: list[Point], lo: float | None, hi: float | None, curve: _Curve) -> list[Point]:
    """Part of a monotone-in-x curve inside [lo, hi], with exact end points on the boundaries."""
    out = [p for p in points if (lo is None or p[0] > lo) and (hi is None or p[0] < hi)]
    increasing = points[-1][0] > points[0][0]
    first, last = (lo, hi) if increasing else (hi, lo)
    if first is not None and (points[0][0] < first if increasing else points[0][0] > first):
        out.insert(0, (first, curve(first)))
    if last is not None and (points[-1][0] > last if increasing else points[-1][0] < last):
        out.append((last, curve(last)))
    return out


def _split(points: list[Point], x: float, curve: _Curve) -> tuple[list[Point], list[Point]]:
    increasing = points[-1][0] > points[0][0]
    cut = (x, curve(x))
    if increasing:
        return [p for p in points if p[0] < x] + [cut], [cut] + [p for p in points if p[0] > x]
    return [p for p in points if p[0] > x] + [cut], [cut] + [p for p in points if p[0] < x]


def _crossing(thickness, a: float, b: float) -> float:  # type: ignore[no-untyped-def]
    """Bisect for where the core thickness reaches zero between a (positive) and b (negative)."""
    for _ in range(60):
        mid = (a + b) / 2
        if thickness(mid) > 0:
            a = mid
        else:
            b = mid
    return a


def _shrunk(airfoil: Airfoil, chord: float, spec: CoreSpec) -> tuple[list[Point], list[Point], _Curve, _Curve]:
    """Upper (trailing edge -> leading edge) and lower surfaces of the core, before any cut-back."""
    foil = airfoil.normalized()
    upper = dedupe([(x * chord, y * chord) for x, y in foil.upper()])  # trailing edge -> leading edge
    lower = dedupe([(x * chord, y * chord) for x, y in foil.lower()])  # leading edge -> trailing edge
    if spec.sheeting_top > 0:
        upper = offset_polyline(upper, spec.sheeting_top)  # left of travel = into the airfoil
    if spec.sheeting_bottom > 0:
        lower = offset_polyline(lower, spec.sheeting_bottom)
    upper = _monotone(upper, decreasing=True)
    lower = _monotone(lower, decreasing=False)
    if len(upper) < 2 or len(lower) < 2:
        raise CoreError("the sheeting is too thick for this airfoil")
    return upper, lower, _Curve(upper[::-1]), _Curve(lower)


def core_middle(airfoil: Airfoil, chord: float, spec: CoreSpec, x: float) -> float:
    """Height of the middle of the core (between its top and bottom surfaces) at ``x``."""
    _, _, y_up, y_lo = _shrunk(airfoil, chord, spec)
    return (y_up(x) + y_lo(x)) / 2


def core_runs(
    airfoil: Airfoil, chord: float, spec: CoreSpec, tip: bool = False, holes: Sequence[Hole] = ()
) -> CoreRuns:
    """Build the core section for the root (``tip=False``) or the tip of a panel.

    ``holes`` are extra wire-cut holes (joiners) already placed in this section's frame;
    the spec's servo channels are added in front of them.
    """
    upper, lower, y_up, y_lo = _shrunk(airfoil, chord, spec)

    def thickness(x: float) -> float:
        return y_up(x) - y_lo(x)

    x_min = max(upper[-1][0], lower[0][0])
    x_max = min(upper[0][0], lower[-1][0])
    grid = [x_min + (x_max - x_min) * k / 400 for k in range(401)]
    best = max(range(len(grid)), key=lambda k: thickness(grid[k]))
    if thickness(grid[best]) <= 0:
        raise CoreError(f"the sheeting is thicker than this {chord:g} mm airfoil: no foam is left")

    # Where the shrunk surfaces meet (only possible when sheeted; plain airfoils are left untouched).
    hi = lo = None
    if spec.sheeted:
        for k in range(best + 1, len(grid)):
            if thickness(grid[k]) <= 0:
                hi = _crossing(thickness, grid[k - 1], grid[k])
                break
        for k in range(best - 1, -1, -1):
            if thickness(grid[k]) <= 0:
                lo = _crossing(thickness, grid[k + 1], grid[k])
                break
    if spec.te_stock > 0:
        hi = min(hi, chord - spec.te_stock) if hi is not None else chord - spec.te_stock
    if spec.le_stock > 0:
        lo = max(lo, spec.le_stock) if lo is not None else spec.le_stock
    start, end = lo if lo is not None else x_min, hi if hi is not None else x_max
    if end - start < 2.0 or thickness((start + end) / 2) <= 0:
        raise CoreError(f"the LE/TE stock and sheeting leave no core on the {chord:g} mm section")
    upper = _clip(upper, lo, hi, y_up)
    lower = _clip(lower, lo, hi, y_lo)
    result = CoreRuns(te_cutback=chord - hi if hi is not None else 0.0, le_cutback=lo if lo is not None else 0.0)
    where = "tip" if tip else "root"

    # Features on each surface: (x_from, x_to, kind, data), kind "slot" or "entry".
    features: tuple[list[tuple[float, float, str, object]], list[tuple[float, float, str, object]]] = ([], [])
    slots: list[tuple[float, float, float | None, float | None, str]] = []  # xl, xr, top floor, bottom floor, label
    for number, spar in enumerate(spec.spars, start=1):
        if spar.depth_top <= 0 and spar.depth_bottom <= 0:
            continue
        position = spar.at(tip)
        label = f"spar {number} ({position:g} % of the {chord:g} mm {where} chord)"
        xc = position / 100.0 * chord
        xl, xr = xc - spar.width / 2, xc + spar.width / 2
        if xl <= start + 0.5 or xr >= end - 0.5:
            raise CoreError(f"{label} is outside the core")
        floor_top = y_up(xc) - spar.depth_top if spar.depth_top > 0 else None
        floor_bottom = y_lo(xc) + spar.depth_bottom if spar.depth_bottom > 0 else None
        if (floor_top is not None and floor_top > min(y_up(xl), y_up(xr)) - 0.1) or (
            floor_bottom is not None and floor_bottom < max(y_lo(xl), y_lo(xr)) + 0.1
        ):
            raise CoreError(f"the surface slopes too much across {label}; make the slot deeper or narrower, or move it")
        xs = [xl + (xr - xl) * k / 20 for k in range(21)]
        top_limit = floor_top if floor_top is not None else min(y_up(x) for x in xs)
        bottom_limit = floor_bottom if floor_bottom is not None else max(y_lo(x) for x in xs)
        if top_limit - bottom_limit < MIN_WEB:
            raise CoreError(f"the slots of {label} leave less than {MIN_WEB:g} mm of foam; make them shallower")
        slots.append((xl, xr, floor_top, floor_bottom, label))
        if floor_top is not None:
            features[0].append((xl, xr, "slot", (floor_top, label)))
        if floor_bottom is not None:
            features[1].append((xl, xr, "slot", (floor_bottom, label)))

    # Wire-cut holes: servo channels, then joiners. Each is checked against the surfaces, the spar
    # slots and the holes before it, and its slit must not run through another hole.
    all_holes = [
        Hole(
            label=f"servo channel {number} ({channel.at(tip):g} % of the {chord:g} mm {where} chord)",
            tag=f"channel{number}",
            shape=HoleShape("round", channel.diameter, channel.diameter, channel.side),
            x=channel.at(tip) / 100.0 * chord,
            offset=channel.offset,
        )
        for number, channel in enumerate(spec.channels, start=1)
    ] + list(holes)
    obstacles: list[tuple[list[Point], str]] = []  # slot pockets, hole outlines and slits
    for xl, xr, floor_top, floor_bottom, label in slots:
        if floor_top is not None:
            top = max(y_up(xl), y_up(xr), y_up((xl + xr) / 2)) + 10.0
            obstacles.append(([(xl, floor_top), (xr, floor_top), (xr, top), (xl, top)], label))
        if floor_bottom is not None:
            bottom = min(y_lo(xl), y_lo(xr), y_lo((xl + xr) / 2)) - 10.0
            obstacles.append(([(xl, bottom), (xr, bottom), (xr, floor_bottom), (xl, floor_bottom)], label))
    for hole in all_holes:
        label = hole.label
        yc = hole.y if hole.y is not None else (y_up(hole.x) + y_lo(hole.x)) / 2 + hole.offset
        center = (hole.x, yc)
        ring = hole.shape.ring(center)
        xs = [p[0] for p in ring]
        if min(xs) <= start + MIN_WEB or max(xs) >= end - MIN_WEB:
            raise CoreError(f"{label} does not fit in the core")
        room = math.inf
        for a, b in itertools.pairwise(ring):
            for k in range(8):
                x, y = a[0] + (b[0] - a[0]) * k / 8, a[1] + (b[1] - a[1]) * k / 8
                room = min(room, y_up(x) - y, y - y_lo(x))
        if room < MIN_WEB:
            raise CoreError(f"{label} leaves less than {MIN_WEB:g} mm of foam; make it smaller or move it")
        entry_x = ring[0][0]
        slit = [ring[0], (entry_x, y_up(entry_x) if hole.shape.side == "top" else y_lo(entry_x))]
        outline = ring[:-1]
        for other, other_label in obstacles:
            if outline_gap(outline, other) < MIN_WEB:
                raise CoreError(f"{label} is too close to {other_label}")
            if len(other) > 2 and outline_gap(slit, other) < MIN_WEB / 2:
                raise CoreError(f"the slit into {label} runs through {other_label}; bring it in from the other side")
        obstacles += [(outline, label), (slit, f"the slit into {label}")]
        result.holes.append(HoleGeometry(hole.tag, center, hole.shape))
        features[0 if hole.shape.side == "top" else 1].append((entry_x, entry_x, "entry", (hole.tag, label)))

    # Features on one surface must not overlap.
    for surface in features:
        surface.sort(key=lambda f: f[0])
        for a, b in itertools.pairwise(surface):
            if b[0] < a[1] + 0.5:
                raise CoreError(f"{a[3][1]} and {b[3][1]} overlap")  # type: ignore[index]
    result.order = tuple(tuple(f[3][1].split(" (")[0] for f in surface) for surface in features)  # type: ignore[assignment,index]

    def build(surface_index: int, curve: list[Point], y: _Curve, skip_first: bool) -> None:
        increasing = surface_index == 1
        items = sorted(features[surface_index], key=lambda f: f[0], reverse=not increasing)
        pending_skip = skip_first
        for x_from, x_to, kind, data in items:
            near, far = (x_from, x_to) if increasing else (x_to, x_from)
            if kind == "slot":
                floor = data[0]  # type: ignore[index]
                piece, rest = _split(curve, near, y)
                _, curve = _split(rest, far, y)
                result.runs += [
                    Run("curve", surface_index, piece, pending_skip),
                    Run("corner", surface_index, [(near, floor)]),
                    Run("corner", surface_index, [(far, floor)]),
                ]
                pending_skip = False
            else:
                piece, curve = _split(curve, near, y)
                result.runs.append(Run("curve", surface_index, piece, pending_skip, tag=data[0]))  # type: ignore[index]
                pending_skip = True
        result.runs.append(Run("curve", surface_index, curve, pending_skip))

    build(0, upper, y_up, False)
    # A plain leading edge: the lower surface starts on the upper surface's last point.
    build(1, lower, y_lo, not spec.sheeted and spec.le_stock == 0)
    return result


def run_counts(section: CoreRuns, n_per_surface: int) -> list[int]:
    """Points per run, shared by root and tip so their outlines stay index-matched."""
    counts = [len(run.points) for run in section.runs]
    for surface in (0, 1):
        curves = [i for i, run in enumerate(section.runs) if run.kind == "curve" and run.surface == surface]
        lengths = [max(polyline_length(section.runs[i].points), 1e-9) for i in curves]
        total = sum(lengths)
        for i, length in zip(curves, lengths):
            counts[i] = n_per_surface if len(curves) == 1 else max(3, round(n_per_surface * length / total))
    return counts


def materialize(section: CoreRuns, counts: list[int]) -> tuple[list[Point], dict[str, int]]:
    """The section's points, and the index of every tagged point (servo channel entries)."""
    out: list[Point] = []
    tags: dict[str, int] = {}
    for run, count in zip(section.runs, counts):
        pts = run.points if run.kind == "corner" else resample(run.points, cosine_spacing(count))
        if run.skip_first:
            pts = pts[1:]
        out += pts
        if run.tag:
            tags[run.tag] = len(out) - 1
    return out, tags
