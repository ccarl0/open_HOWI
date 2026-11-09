# SPDX-License-Identifier: GPL-3.0-or-later
"""Fuselages: a body described by stations (or by a side and a top view) and cut on a hot wire.

Fuselage coordinates: x runs aft from the nose (x = 0), y is up from the
datum (the reference line of the drawings), u is sideways from the centre
line. A cross-section is a "superellipse" in its upper and lower halves, so
each half can be anything from an ellipse (roundness 100 %) to a square box
(0 %): a flat-bottomed fuselage with a rounded top is round_bottom = 0,
round_top = 100.

Two ways to cut one, both in ``fuselage_jobs`` / the project layer:

* two views: the side view, then (the slab turned on its side) the top view,
  which leaves square-ish sections to round off by hand;
* lengthwise segments: the wire runs along the fuselage and each segment is
  a ruled surface between the sections at its two ends, like a wing panel,
  so the sections come out rounded. Segments can be hollow, and can be cut
  as left and right half shells.
"""

from __future__ import annotations

import bisect
import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .airfoil import Airfoil
from .geometry import Point, cumulative_lengths, offset_polygon, point_at_length

MIN_SECTION = 2.0
"""Smallest section (mm) a lengthwise segment is cut with: the wire cannot cut a point."""


class FuselageError(ValueError):
    """The fuselage cannot be built or cut as described."""


@dataclass(frozen=True)
class Station:
    """The fuselage's size at one point along its length."""

    x: float
    """Distance from the nose (mm)."""
    top: float
    """Height of the top line above the datum (mm)."""
    bottom: float
    """Height of the bottom line (mm); below the datum is negative."""
    width: float
    """Full width (mm)."""
    round_top: float = 100.0
    """Upper half of the section: 100 % elliptical, 0 % square corners."""
    round_bottom: float = 100.0

    def __post_init__(self) -> None:
        if self.top < self.bottom:
            raise ValueError(f"station at {self.x:g} mm: top must be above bottom")
        if self.width < 0:
            raise ValueError(f"station at {self.x:g} mm: width must be >= 0")
        for name in ("round_top", "round_bottom"):
            if not 0 <= getattr(self, name) <= 100:
                raise ValueError(f"station at {self.x:g} mm: {name} must be between 0 and 100 %")


@dataclass(frozen=True)
class Saddle:
    """Where a wing sits on (or under) the fuselage: the fuselage line takes the shape of its airfoil."""

    airfoil: Airfoil
    position: float = 120.0
    """Leading edge of the wing, mm from the nose."""
    chord: float = 200.0
    height: float = 40.0
    """Height of the wing's leading edge above the datum (mm)."""
    incidence: float = 0.0
    """Wing incidence (degrees, nose up)."""
    side: str = "top"
    """"top": the wing sits on the fuselage (high/shoulder wing); "bottom": it runs under it (low wing)."""

    def __post_init__(self) -> None:
        if self.chord <= 0:
            raise ValueError("saddle chord must be > 0")
        if self.side not in ("top", "bottom"):
            raise ValueError('saddle side must be "top" or "bottom"')
        if not -15 <= self.incidence <= 15:
            raise ValueError("saddle incidence must be between -15 and 15 degrees")

    def line(self) -> list[Point]:
        """The airfoil surface the fuselage follows (lower surface for a top saddle), in fuselage coordinates."""
        foil = self.airfoil.normalized()
        surface = foil.lower() if self.side == "top" else foil.upper()
        phi = math.radians(-self.incidence)  # nose up: the trailing edge goes down
        c, s = math.cos(phi), math.sin(phi)
        pts = sorted(
            (self.position + (x * c - y * s) * self.chord, self.height + (x * s + y * c) * self.chord)
            for x, y in surface
        )
        return pts


def _linear(xs: Sequence[float], ys: Sequence[float]) -> Callable[[float], float]:
    def f(x: float) -> float:
        if x <= xs[0]:
            return ys[0]
        if x >= xs[-1]:
            return ys[-1]
        i = bisect.bisect_right(xs, x) - 1
        t = (x - xs[i]) / (xs[i + 1] - xs[i])
        return ys[i] + (ys[i + 1] - ys[i]) * t

    return f


def _pchip(xs: Sequence[float], ys: Sequence[float]) -> Callable[[float], float]:
    """Smooth interpolation through the stations that never overshoots between them (Fritsch-Carlson)."""
    n = len(xs)
    if n == 2:
        return _linear(xs, ys)
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    d = [(ys[i + 1] - ys[i]) / h[i] for i in range(n - 1)]
    m = [0.0] * n
    for i in range(1, n - 1):
        if d[i - 1] * d[i] > 0:
            w1, w2 = 2 * h[i] + h[i - 1], h[i] + 2 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])

    def end(h0: float, h1: float, d0: float, d1: float) -> float:
        slope = ((2 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
        if slope * d0 <= 0:
            return 0.0
        if d0 * d1 <= 0 and abs(slope) > 3 * abs(d0):
            return 3 * d0
        return slope

    m[0] = end(h[0], h[1], d[0], d[1])
    m[-1] = end(h[-1], h[-2], d[-1], d[-2])

    def f(x: float) -> float:
        if x <= xs[0]:
            return ys[0]
        if x >= xs[-1]:
            return ys[-1]
        i = min(bisect.bisect_right(xs, x) - 1, n - 2)
        t = (x - xs[i]) / h[i]
        t2, t3 = t * t, t * t * t
        return (
            (2 * t3 - 3 * t2 + 1) * ys[i]
            + (t3 - 2 * t2 + t) * h[i] * m[i]
            + (-2 * t3 + 3 * t2) * ys[i + 1]
            + (t3 - t2) * h[i] * m[i + 1]
        )

    return f


def _exponent(roundness: float) -> float:
    """Superellipse exponent: 2 for an ellipse (100 %), 40 for a box with sharp corners (0 %)."""
    r = min(1.0, max(0.0, roundness / 100.0))
    return 2.0 / (r + (1.0 - r) * 0.05)


def _quarter(a: float, b: float, n_exp: float, start: float, stop: float, samples: int) -> list[Point]:
    """Points of a superellipse |u/a|^n + |v/b|^n = 1 for polar angles from ``start`` to ``stop``."""
    out = []
    for k in range(samples + 1):
        t = start + (stop - start) * k / samples
        c, s = math.cos(t), math.sin(t)
        r = 1.0 / (abs(c / a) ** n_exp + abs(s / b) ** n_exp) ** (1.0 / n_exp) if a > 0 and b > 0 else 0.0
        out.append((r * c, r * s))
    return out


def _even(points: list[Point], count: int) -> list[Point]:
    """``count`` points spread evenly along a polyline, both ends included."""
    cum = cumulative_lengths(points)
    total = cum[-1]
    if total <= 0:
        return [points[0]] * count
    return [point_at_length(points, cum, total * k / (count - 1)) for k in range(count)]


class Fuselage:
    """Top, bottom and width lines along the fuselage, section roundness, and wing saddles."""

    def __init__(
        self,
        xs: Sequence[float],
        tops: Sequence[float],
        bottoms: Sequence[float],
        widths: Sequence[float],
        round_tops: Sequence[float],
        round_bottoms: Sequence[float],
        saddles: Sequence[Saddle] = (),
        smooth: bool = True,
        stations: Sequence[float] | None = None,
    ):
        if len(xs) < 2:
            raise FuselageError("a fuselage needs at least two stations")
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        xs = [float(xs[i]) for i in order]
        if any(b - a < 1e-6 for a, b in itertools.pairwise(xs)):
            raise FuselageError("two stations are at the same position")
        x0 = xs[0]
        self.xs = [x - x0 for x in xs]
        self.length = self.xs[-1]
        if self.length < 10:
            raise FuselageError("the fuselage must be at least 10 mm long")
        interp = _pchip if smooth else _linear

        def pick(values: Sequence[float]) -> list[float]:
            return [float(values[i]) for i in order]

        self._top = interp(self.xs, pick(tops))
        self._bottom = interp(self.xs, pick(bottoms))
        self._width = interp(self.xs, pick(widths))
        self._round_top = _linear(self.xs, pick(round_tops))
        self._round_bottom = _linear(self.xs, pick(round_bottoms))
        self.stations = [x - x0 for x in stations] if stations is not None else None
        self.saddles = tuple(saddles)
        self._saddle_lines = [(s, s.line()) for s in self.saddles]
        for saddle, line in self._saddle_lines:
            if line[-1][0] <= 0 or line[0][0] >= self.length:
                raise FuselageError(f"the wing saddle at {saddle.position:g} mm is outside the fuselage")

    @classmethod
    def from_stations(cls, stations: Sequence[Station], saddles: Sequence[Saddle] = ()) -> Fuselage:
        stations = sorted(stations, key=lambda s: s.x)
        return cls(
            [s.x for s in stations],
            [s.top for s in stations],
            [s.bottom for s in stations],
            [s.width for s in stations],
            [s.round_top for s in stations],
            [s.round_bottom for s in stations],
            saddles,
            smooth=True,
            stations=[s.x for s in stations],
        )

    @classmethod
    def from_views(
        cls,
        side: Sequence[Point],
        top: Sequence[Point],
        round_top: float = 100.0,
        round_bottom: float = 100.0,
        saddles: Sequence[Saddle] = (),
        samples: int = 240,
    ) -> tuple[Fuselage, list[str]]:
        """From a side view and a top view (closed outlines, nose towards smaller x)."""
        warnings: list[str] = []
        sx0, sx1 = min(p[0] for p in side), max(p[0] for p in side)
        tx0, tx1 = min(p[0] for p in top), max(p[0] for p in top)
        length = sx1 - sx0
        if length < 10 or tx1 - tx0 < 10:
            raise FuselageError("the side and top views must be at least 10 mm long")
        stretch = length / (tx1 - tx0)
        if abs(stretch - 1) > 0.02:
            warnings.append(
                f"The top view is {tx1 - tx0:.1f} mm long and the side view {length:.1f} mm: the top view was "
                "stretched to match. Draw both at the same scale."
            )
        xs, tops, bottoms, widths = [], [], [], []
        for k in range(samples + 1):
            f = min(max(k / samples, 1e-6), 1 - 1e-6)  # just inside the ends, where the outline is crossed
            ys = _crossings(side, sx0 + f * length)
            ws = _crossings(top, tx0 + f * (tx1 - tx0))
            if len(ys) < 2 or len(ws) < 2:
                continue
            xs.append(k / samples * length)
            tops.append(max(ys))
            bottoms.append(min(ys))
            widths.append(max(ws) - min(ws))
        if len(xs) < 3:
            raise FuselageError("could not read the side and top views: draw each as one closed outline")
        n = len(xs)
        fuse = cls(xs, tops, bottoms, widths, [round_top] * n, [round_bottom] * n, saddles, smooth=False)
        return fuse, warnings

    # Lines along the fuselage -----------------------------------------------------------------

    def top(self, x: float, saddles: bool = True) -> float:
        y = self._top(x)
        if saddles:
            for saddle, line in self._saddle_lines:
                if saddle.side == "top" and line[0][0] <= x <= line[-1][0]:
                    y = min(y, _linear([p[0] for p in line], [p[1] for p in line])(x))
        return y

    def bottom(self, x: float, saddles: bool = True) -> float:
        y = self._bottom(x)
        if saddles:
            for saddle, line in self._saddle_lines:
                if saddle.side == "bottom" and line[0][0] <= x <= line[-1][0]:
                    y = max(y, _linear([p[0] for p in line], [p[1] for p in line])(x))
        return y

    def width(self, x: float) -> float:
        return max(0.0, self._width(x))

    def roundness(self, x: float) -> tuple[float, float]:
        return self._round_top(x), self._round_bottom(x)

    def sample_xs(self, step: float | None = None) -> list[float]:
        """Positions to draw the outlines at: an even grid plus the stations and the saddles' own points."""
        step = step or max(1.0, self.length / 400)
        n = max(2, math.ceil(self.length / step))
        xs = {self.length * k / n for k in range(n + 1)}
        xs.update(self.xs if self.stations is not None else [])
        for _, line in self._saddle_lines:
            xs.update(x for x, _ in line if 0 < x < self.length)
        return sorted(xs)

    def side_lines(self, saddles: bool = True) -> tuple[list[Point], list[Point]]:
        """Top and bottom lines, nose to tail."""
        xs = self.sample_xs()
        top = [(x, self.top(x, saddles)) for x in xs]
        bottom = [(x, self.bottom(x, saddles)) for x in xs]
        for (x, t), (_, b) in zip(top, bottom):
            if t - b < 1.0 and 0 < x < self.length and self.top(x, False) - self.bottom(x, False) >= 1.0:
                raise FuselageError(f"the wing saddle leaves less than 1 mm of fuselage {x:.0f} mm from the nose")
        return top, bottom

    def side_outline(self, saddles: bool = True) -> list[Point]:
        """Closed side view, counter-clockwise: along the bottom to the tail, back along the top."""
        top, bottom = self.side_lines(saddles)
        return _ring(bottom + top[::-1])

    def plan_outline(self) -> list[Point]:
        """Closed top view, counter-clockwise (u to the left of the nose is negative)."""
        xs = self.sample_xs()
        right = [(x, -self.width(x) / 2) for x in xs]
        left = [(x, self.width(x) / 2) for x in xs]
        return _ring(right + left[::-1])

    def max_width(self) -> float:
        return max(self.width(x) for x in self.sample_xs())

    def height_range(self, saddles: bool = True) -> tuple[float, float]:
        xs = self.sample_xs()
        return min(self.bottom(x, saddles) for x in xs), max(self.top(x, saddles) for x in xs)

    # Sections ---------------------------------------------------------------------------------

    def section(self, x: float, n: int = 64, inset: float = 0.0, minimum: float = 0.0) -> list[Point]:
        """The cross-section at ``x`` as ``n`` points (n a multiple of 4), counter-clockwise from the
        bottom centre: bottom, right side, top, left side. Saddles are not included.

        Every quarter gets the same number of points, so sections of different
        shapes stay matched point for point (bottom to bottom, side to side).
        ``inset`` shrinks the section on every side (a skin or wall thickness).
        """
        if n % 4 or n < 16:
            raise ValueError("a section needs a multiple of 4 points, at least 16")
        top, bottom = self.top(x, False), self.bottom(x, False)
        a = max(self.width(x) / 2, minimum / 2)
        mid = (top + bottom) / 2
        half_h = max((top - bottom) / 2, minimum / 2)
        rt, rb = self.roundness(x)
        et, eb = _exponent(rt), _exponent(rb)
        q = n // 4
        quarters = [
            _quarter(a, half_h, eb, -math.pi / 2, 0.0, 240),
            _quarter(a, half_h, et, 0.0, math.pi / 2, 240),
            _quarter(a, half_h, et, math.pi / 2, math.pi, 240),
            _quarter(a, half_h, eb, math.pi, 1.5 * math.pi, 240),
        ]
        ring: list[Point] = []
        for pts in quarters:
            ring += _even(pts, q + 1)[:-1]
        ring = [(u, v + mid) for u, v in ring]
        if inset > 0:
            if inset >= min(a, half_h) - 0.5:
                raise FuselageError(
                    f"a {inset:g} mm wall or skin does not fit in the {2 * a:.0f} x {2 * half_h:.0f} mm section "
                    f"{x:.0f} mm from the nose"
                )
            ring = offset_polygon(ring, inset)  # counter-clockwise: left is inwards
        return ring

    def former_positions(self, spacing: float) -> list[float]:
        """Where to draw formers: at the stations, or every ``spacing`` mm for a fuselage from drawings."""
        if self.stations is not None:
            return list(self.xs)
        n = max(1, round(self.length / spacing))
        return [self.length * k / n for k in range(n + 1)]

    def segments(self, max_length: float) -> list[tuple[float, float]]:
        """Split the length into equal segments no longer than ``max_length``."""
        if max_length < 10:
            raise FuselageError("segments must be at least 10 mm long")
        n = max(1, math.ceil(self.length / max_length - 1e-9))
        return [(self.length * k / n, self.length * (k + 1) / n) for k in range(n)]


def _crossings(ring: Sequence[Point], x: float) -> list[float]:
    """Heights where a vertical line at ``x`` crosses a closed outline."""
    ys = []
    for (x1, y1), (x2, y2) in zip(ring, list(ring[1:]) + [ring[0]]):
        if (x1 <= x < x2 or x2 <= x < x1) and x1 != x2:
            ys.append(y1 + (y2 - y1) * (x - x1) / (x2 - x1))
    return ys


def _ring(points: list[Point]) -> list[Point]:
    """Drop repeated points (e.g. a pointed nose or tail where both lines meet)."""
    out: list[Point] = []
    for p in points:
        if not out or math.dist(p, out[-1]) > 1e-9:
            out.append(p)
    if len(out) > 1 and math.dist(out[0], out[-1]) <= 1e-9:
        out.pop()
    return out


def half_ring(section: list[Point], inner: list[Point] | None = None) -> list[Point]:
    """The right half of a section (u >= 0) as a closed ring, flat along the centre line.

    With ``inner`` (the same section shrunk by the wall), the half is a C-shaped
    shell: out along the outside, back along the inside, open at the centre line.
    """
    n = len(section)
    right = section[: n // 2 + 1]  # bottom centre, right side, top centre
    if inner is None:
        return [(max(u, 0.0), v) for u, v in right]
    inside = inner[: n // 2 + 1][::-1]
    return [(max(u, 0.0), v) for u, v in right + inside]
