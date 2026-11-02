"""Dependency-free 2D polyline helpers used by the CAM code."""

from __future__ import annotations

import bisect
import itertools
import math
from collections.abc import Sequence

Point = tuple[float, float]


def distance(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def dedupe(points: Sequence[Point], tol: float = 1e-9) -> list[Point]:
    """Drop points that coincide with the previously kept point."""
    out: list[Point] = []
    for p in points:
        q = (float(p[0]), float(p[1]))
        if not out or distance(out[-1], q) > tol:
            out.append(q)
    return out


def cumulative_lengths(points: Sequence[Point]) -> list[float]:
    cum = [0.0]
    for a, b in itertools.pairwise(points):
        cum.append(cum[-1] + distance(a, b))
    return cum


def polyline_length(points: Sequence[Point]) -> float:
    return cumulative_lengths(points)[-1]


def point_at_length(points: Sequence[Point], cum: Sequence[float], s: float) -> Point:
    """Point at arc length ``s`` along ``points`` (``cum`` from :func:`cumulative_lengths`)."""
    if s <= 0.0:
        return points[0]
    if s >= cum[-1]:
        return points[-1]
    i = bisect.bisect_right(cum, s) - 1
    seg = cum[i + 1] - cum[i]
    t = 0.0 if seg == 0.0 else (s - cum[i]) / seg
    a, b = points[i], points[i + 1]
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def cosine_spacing(n: int) -> list[float]:
    """``n`` fractions in [0, 1], clustered at both ends (where curvature is high)."""
    if n < 2:
        raise ValueError("need at least 2 samples")
    return [0.5 * (1.0 - math.cos(math.pi * i / (n - 1))) for i in range(n)]


def resample(points: Sequence[Point], fractions: Sequence[float]) -> list[Point]:
    """Sample a polyline at the given fractions of its total arc length.

    The result always has exactly ``len(fractions)`` points, which is what
    keeps the root and tip paths of a 4-axis cut in step with each other.
    """
    pts = dedupe(points)
    if len(pts) < 2:
        raise ValueError("polyline needs at least two distinct points")
    cum = cumulative_lengths(pts)
    total = cum[-1]
    return [point_at_length(pts, cum, f * total) for f in fractions]


def signed_area(points: Sequence[Point]) -> float:
    """Shoelace area of the implicitly closed polygon; > 0 means counter-clockwise."""
    area = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return area / 2.0


def offset_polyline(points: Sequence[Point], d: float, miter_limit: float = 4.0) -> list[Point]:
    """Offset an open polyline by ``d`` to its left (negative ``d`` offsets right).

    Point count and order are preserved so matched root/tip paths stay matched.
    Interior vertices use a mitred bisector, capped at ``miter_limit * |d|``.
    """
    if len(points) < 2:
        raise ValueError("polyline needs at least two points")
    if d == 0.0:
        return [(float(x), float(y)) for x, y in points]

    normals: list[Point] = []
    for a, b in itertools.pairwise(points):
        length = distance(a, b)
        if length == 0.0:
            raise ValueError("cannot offset a polyline with zero-length segments")
        normals.append(((a[1] - b[1]) / length, (b[0] - a[0]) / length))

    out: list[Point] = []
    last = len(points) - 1
    for i, p in enumerate(points):
        if i == 0:
            n, scale = normals[0], 1.0
        elif i == last:
            n, scale = normals[-1], 1.0
        else:
            n1, n2 = normals[i - 1], normals[i]
            sx, sy = n1[0] + n2[0], n1[1] + n2[1]
            norm = math.hypot(sx, sy)
            if norm < 1e-12:  # path doubles back on itself
                n, scale = n1, 1.0
            else:
                n = (sx / norm, sy / norm)
                cos_half = n[0] * n1[0] + n[1] * n1[1]
                scale = min(1.0 / cos_half, miter_limit)
        out.append((p[0] + n[0] * d * scale, p[1] + n[1] * d * scale))
    return out
