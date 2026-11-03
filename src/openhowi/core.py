# SPDX-License-Identifier: GPL-3.0-or-later
"""Foam core sections for built-up wings: sheeting, LE/TE stock, spar slots and servo channels.

The core is the airfoil shrunk by the sheeting thickness (top and bottom can
differ), optionally cut back at the leading and trailing edges for hardwood
or balsa stock, with rectangular slots for spar caps and round servo-lead
channels. Where the airfoil gets thinner than the two sheets together (near
the trailing edge, or at a sharp leading edge), the core ends where the
shrunk surfaces meet.

A servo channel is cut the way builders do it by hand: the wire comes in
through a straight slit from the top or bottom surface, goes round the
channel and leaves along the slit.

Each section is built as a list of runs (surface curves, slot corners,
channel entries) so that root and tip can be resampled to exactly the same
number of points.
"""

from __future__ import annotations

import bisect
import itertools
import math
from dataclasses import dataclass, field

from .airfoil import Airfoil
from .geometry import Point, cosine_spacing, dedupe, offset_polyline, polyline_length, resample

MIN_WEB = 1.0
"""Thinnest foam (mm) a slot or channel may leave."""
CIRCLE_POINTS = 32
"""Points around a servo channel (the same at root and tip)."""


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
class CoreSpec:
    sheeting_top: float = 0.0
    sheeting_bottom: float = 0.0
    le_stock: float = 0.0
    """Cut the core back this far (mm, along the chord) from the leading edge."""
    te_stock: float = 0.0
    """Cut the core back this far (mm) from the trailing edge."""
    spars: tuple[Spar, ...] = ()
    channels: tuple[Channel, ...] = ()

    def __post_init__(self) -> None:
        for name in ("sheeting_top", "sheeting_bottom", "le_stock", "te_stock"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        object.__setattr__(self, "spars", tuple(self.spars))
        object.__setattr__(self, "channels", tuple(self.channels))

    @property
    def sheeted(self) -> bool:
        return self.sheeting_top > 0 or self.sheeting_bottom > 0

    @property
    def slotted(self) -> bool:
        return any(s.depth_top > 0 or s.depth_bottom > 0 for s in self.spars)

    @property
    def plain(self) -> bool:
        return not (self.sheeted or self.le_stock or self.te_stock or self.slotted or self.channels)


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
    """Names the run's last point (a servo channel's entry)."""


@dataclass
class ChannelGeometry:
    tag: str
    center: Point
    radius: float
    side: str


@dataclass
class CoreRuns:
    """One core section, untwisted, leading edge at x = 0, as runs of points."""

    runs: list[Run] = field(default_factory=list)
    channels: list[ChannelGeometry] = field(default_factory=list)
    te_cutback: float = 0.0
    """Distance (mm) from the airfoil's trailing edge to the core's."""
    le_cutback: float = 0.0


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


def core_runs(airfoil: Airfoil, chord: float, spec: CoreSpec, tip: bool = False) -> CoreRuns:
    """Build the core section for the root (``tip=False``) or the tip of a panel."""
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
    y_up, y_lo = _Curve(upper[::-1]), _Curve(lower)

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
    slots: list[tuple[float, float, float | None, float | None]] = []  # xl, xr, top floor, bottom floor
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
        slots.append((xl, xr, floor_top, floor_bottom))
        if floor_top is not None:
            features[0].append((xl, xr, "slot", (floor_top, label)))
        if floor_bottom is not None:
            features[1].append((xl, xr, "slot", (floor_bottom, label)))

    circles: list[tuple[float, float, float, str]] = []
    for number, channel in enumerate(spec.channels, start=1):
        position = channel.at(tip)
        label = f"servo channel {number} ({position:g} % of the {chord:g} mm {where} chord)"
        xc = position / 100.0 * chord
        r = channel.diameter / 2
        if xc - r <= start + MIN_WEB or xc + r >= end - MIN_WEB:
            raise CoreError(f"{label} does not fit in the core")
        yc = (y_up(xc) + y_lo(xc)) / 2 + channel.offset
        room = math.inf
        for k in range(21):
            x = xc - r + 2 * r * k / 20
            half = math.sqrt(max(0.0, r * r - (x - xc) ** 2))
            room = min(room, y_up(x) - (yc + half), (yc - half) - y_lo(x))
        for xl, xr, floor_top, floor_bottom in slots:
            if xl < xc + r + MIN_WEB and xr > xc - r - MIN_WEB:
                if floor_top is not None:
                    room = min(room, floor_top - (yc + r))
                if floor_bottom is not None:
                    room = min(room, (yc - r) - floor_bottom)
        for other_x, other_y, other_r, other_label in circles:
            room = min(room, math.hypot(xc - other_x, yc - other_y) - r - other_r)
            if room < MIN_WEB:
                raise CoreError(f"{label} is too close to {other_label}")
        if room < MIN_WEB:
            raise CoreError(f"{label} leaves less than {MIN_WEB:g} mm of foam; make it smaller or move it")
        circles.append((xc, yc, r, label))
        tag = f"channel{number}"
        result.channels.append(ChannelGeometry(tag, (xc, yc), r, channel.side))
        features[0 if channel.side == "top" else 1].append((xc, xc, "entry", (tag, label)))

    # Features on one surface must not overlap.
    for surface in features:
        surface.sort(key=lambda f: f[0])
        for a, b in itertools.pairwise(surface):
            if b[0] < a[1] + 0.5:
                raise CoreError(f"{a[3][1]} and {b[3][1]} overlap")  # type: ignore[index]

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


def channel_path(entry: Point, center: Point, radius: float, n: int = CIRCLE_POINTS) -> list[Point]:
    """From the entry point down the slit, once round the channel, and back out (entry point excluded)."""
    a0 = math.atan2(entry[1] - center[1], entry[0] - center[0])
    circle = [
        (
            center[0] + radius * math.cos(a0 + 2 * math.pi * k / n),
            center[1] + radius * math.sin(a0 + 2 * math.pi * k / n),
        )
        for k in range(n + 1)
    ]
    return circle + [entry]


def channel_outline(center: Point, radius: float, n: int = CIRCLE_POINTS) -> list[Point]:
    """The finished channel as a clockwise ring, for display."""
    return [
        (center[0] + radius * math.cos(-2 * math.pi * k / n), center[1] + radius * math.sin(-2 * math.pi * k / n))
        for k in range(n)
    ]
