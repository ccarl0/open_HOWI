# SPDX-License-Identifier: GPL-3.0-or-later
"""Foam core sections for built-up wings: sheeting allowance, LE/TE stock and spar slots.

The core is the airfoil shrunk by the sheeting thickness (top and bottom can
differ), optionally cut back at the leading and trailing edges for hardwood
or balsa stock, with rectangular slots for spar caps. Where the airfoil gets
thinner than the two sheets together (near the trailing edge, or at a sharp
leading edge), the core ends where the shrunk surfaces meet.

Each section is built as a list of runs (surface curves and slot corners) so
that root and tip can be resampled to exactly the same number of points.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from .airfoil import Airfoil
from .geometry import Point, cosine_spacing, dedupe, offset_polyline, polyline_length, resample

MIN_WEB = 1.0
"""Thinnest foam (mm) a spar slot may leave."""


class CoreError(ValueError):
    """The requested core cannot be made from this airfoil."""


@dataclass(frozen=True)
class CoreSpec:
    sheeting_top: float = 0.0
    sheeting_bottom: float = 0.0
    le_stock: float = 0.0
    """Cut the core back this far (mm, along the chord) from the leading edge."""
    te_stock: float = 0.0
    """Cut the core back this far (mm) from the trailing edge."""
    spar_position: float = 25.0
    """Centre of the spar slots, % of chord from the leading edge."""
    spar_width: float = 0.0
    spar_depth_top: float = 0.0
    spar_depth_bottom: float = 0.0

    def __post_init__(self) -> None:
        for name in ("sheeting_top", "sheeting_bottom", "le_stock", "te_stock", "spar_width",
                     "spar_depth_top", "spar_depth_bottom"):  # fmt: skip
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if not 0 < self.spar_position < 100:
            raise ValueError("spar_position must be between 0 and 100 % of the chord")

    @property
    def sheeted(self) -> bool:
        return self.sheeting_top > 0 or self.sheeting_bottom > 0

    @property
    def slot_top(self) -> bool:
        return self.spar_width > 0 and self.spar_depth_top > 0

    @property
    def slot_bottom(self) -> bool:
        return self.spar_width > 0 and self.spar_depth_bottom > 0


@dataclass
class CoreRuns:
    """One core section, untwisted, leading edge at x = 0, as runs of points."""

    runs: list[tuple[str, int, list[Point]]] = field(default_factory=list)
    """(kind, surface, points): kind "curve" or "corner"; surface 0 = upper, 1 = lower."""
    drop_lower_start: bool = False
    """The lower surface starts on the upper surface's last point (plain leading edge)."""
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


def core_runs(airfoil: Airfoil, chord: float, spec: CoreSpec, spar_position: float | None = None) -> CoreRuns:
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

    result = CoreRuns(
        drop_lower_start=not spec.sheeted and spec.le_stock == 0,
        te_cutback=chord - hi if hi is not None else 0.0,
        le_cutback=lo if lo is not None else 0.0,
    )

    # Spar slots: rectangular notches, walls perpendicular to the chord.
    position = spec.spar_position if spar_position is None else spar_position
    if spec.slot_top or spec.slot_bottom:
        xc = position / 100.0 * chord
        xl, xr = xc - spec.spar_width / 2, xc + spec.spar_width / 2
        if xl <= start + 0.5 or xr >= end - 0.5:
            raise CoreError(f"the spar slot at {position:g} % of the {chord:g} mm chord is outside the core")
        floor_top = y_up(xc) - spec.spar_depth_top if spec.slot_top else None
        floor_bottom = y_lo(xc) + spec.spar_depth_bottom if spec.slot_bottom else None
        if (floor_top is not None and floor_top > min(y_up(xl), y_up(xr)) - 0.1) or (
            floor_bottom is not None and floor_bottom < max(y_lo(xl), y_lo(xr)) + 0.1
        ):
            raise CoreError(
                f"the surface slopes too much across the spar slot at {position:g} % of the {chord:g} mm chord; "
                "make the slot deeper or narrower, or move it"
            )
        xs = [xl + (xr - xl) * k / 20 for k in range(21)]
        top_limit = floor_top if floor_top is not None else min(y_up(x) for x in xs)
        bottom_limit = floor_bottom if floor_bottom is not None else max(y_lo(x) for x in xs)
        if top_limit - bottom_limit < MIN_WEB:
            raise CoreError(
                f"the spar slots leave less than {MIN_WEB:g} mm of foam on the {chord:g} mm section; make them shallower"
            )
    else:
        xl = xr = 0.0
        floor_top = floor_bottom = None

    if floor_top is not None:
        a, rest = _split(upper, xr, y_up)
        _, b = _split(rest, xl, y_up)
        result.runs += [("curve", 0, a), ("corner", 0, [(xr, floor_top)]), ("corner", 0, [(xl, floor_top)]),
                        ("curve", 0, b)]  # fmt: skip
    else:
        result.runs.append(("curve", 0, upper))
    if floor_bottom is not None:
        a, rest = _split(lower, xl, y_lo)
        _, b = _split(rest, xr, y_lo)
        result.runs += [("curve", 1, a), ("corner", 1, [(xl, floor_bottom)]), ("corner", 1, [(xr, floor_bottom)]),
                        ("curve", 1, b)]  # fmt: skip
    else:
        result.runs.append(("curve", 1, lower))
    return result


def run_counts(section: CoreRuns, n_per_surface: int) -> list[int]:
    """Points per run, shared by root and tip so their outlines stay index-matched."""
    counts = [1] * len(section.runs)
    for surface in (0, 1):
        curves = [i for i, (kind, s, _) in enumerate(section.runs) if kind == "curve" and s == surface]
        lengths = [max(polyline_length(section.runs[i][2]), 1e-9) for i in curves]
        total = sum(lengths)
        for i, length in zip(curves, lengths):
            counts[i] = n_per_surface if len(curves) == 1 else max(3, round(n_per_surface * length / total))
    return counts


def materialize(section: CoreRuns, counts: list[int]) -> list[Point]:
    out: list[Point] = []
    first_lower = True
    for (kind, surface, points), count in zip(section.runs, counts):
        if kind == "corner":
            out += points
            continue
        pts = resample(points, cosine_spacing(count))
        if surface == 1 and first_lower:
            first_lower = False
            if section.drop_lower_start:
                pts = pts[1:]
        out += pts
    return out
