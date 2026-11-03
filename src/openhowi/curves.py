# SPDX-License-Identifier: GPL-3.0-or-later
"""Flatten curves (Bézier, arcs, ellipses, B-splines, DXF bulges) into polyline points.

Every function returns the points *after* the start point, so consecutive
pieces can be appended to one polyline. ``tol`` is the largest allowed
distance between the curve and its polyline, in the curve's own units.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from .geometry import Point

MAX_SEGMENTS = 20000
MAX_DEPTH = 18


def _line_distance(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length == 0.0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    return abs((p[0] - a[0]) * dy - (p[1] - a[1]) * dx) / length


def cubic(p0: Point, p1: Point, p2: Point, p3: Point, tol: float) -> list[Point]:
    out: list[Point] = []

    def split(a: Point, b: Point, c: Point, d: Point, depth: int) -> None:
        if depth >= MAX_DEPTH or max(_line_distance(b, a, d), _line_distance(c, a, d)) <= tol:
            out.append(d)
            return
        ab = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        bc = ((b[0] + c[0]) / 2, (b[1] + c[1]) / 2)
        cd = ((c[0] + d[0]) / 2, (c[1] + d[1]) / 2)
        abc = ((ab[0] + bc[0]) / 2, (ab[1] + bc[1]) / 2)
        bcd = ((bc[0] + cd[0]) / 2, (bc[1] + cd[1]) / 2)
        mid = ((abc[0] + bcd[0]) / 2, (abc[1] + bcd[1]) / 2)
        split(a, ab, abc, mid, depth + 1)
        split(mid, bcd, cd, d, depth + 1)

    split(p0, p1, p2, p3, 0)
    return out


def quadratic(p0: Point, p1: Point, p2: Point, tol: float) -> list[Point]:
    c1 = (p0[0] + 2 / 3 * (p1[0] - p0[0]), p0[1] + 2 / 3 * (p1[1] - p0[1]))
    c2 = (p2[0] + 2 / 3 * (p1[0] - p2[0]), p2[1] + 2 / 3 * (p1[1] - p2[1]))
    return cubic(p0, c1, c2, p2, tol)


def _steps(radius: float, sweep: float, tol: float) -> int:
    if radius <= 0.0 or sweep == 0.0:
        return 1
    step = 2.0 * math.acos(max(-1.0, 1.0 - tol / radius)) if tol < radius else math.pi / 2
    return max(1, min(MAX_SEGMENTS, math.ceil(abs(sweep) / max(step, 1e-6))))


def arc(center: Point, radius: float, start: float, sweep: float, tol: float) -> list[Point]:
    """Circular arc from angle ``start`` (radians) through ``sweep`` (positive = counter-clockwise)."""
    n = _steps(radius, sweep, tol)
    return [
        (center[0] + radius * math.cos(start + sweep * k / n), center[1] + radius * math.sin(start + sweep * k / n))
        for k in range(1, n + 1)
    ]


def ellipse(center: Point, major: Point, minor: Point, t0: float, t1: float, tol: float) -> list[Point]:
    """Points ``center + cos(t)·major + sin(t)·minor`` for t from t0 to t1 (radians)."""
    radius = max(math.hypot(*major), math.hypot(*minor))
    n = _steps(radius, t1 - t0, tol)
    out = []
    for k in range(1, n + 1):
        t = t0 + (t1 - t0) * k / n
        c, s = math.cos(t), math.sin(t)
        out.append((center[0] + c * major[0] + s * minor[0], center[1] + c * major[1] + s * minor[1]))
    return out


def bulge(p1: Point, p2: Point, b: float, tol: float) -> list[Point]:
    """DXF polyline segment from p1 to p2 with bulge ``b`` (tan of a quarter of the arc angle)."""
    if abs(b) < 1e-9:
        return [p2]
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    chord = math.hypot(dx, dy)
    if chord == 0.0:
        return [p2]
    sweep = 4.0 * math.atan(b)
    h = (chord / 2.0) * (1.0 - b * b) / (2.0 * b)  # signed distance from the chord midpoint to the centre
    center = ((p1[0] + p2[0]) / 2 - dy / chord * h, (p1[1] + p2[1]) / 2 + dx / chord * h)
    radius = math.hypot(p1[0] - center[0], p1[1] - center[1])
    start = math.atan2(p1[1] - center[1], p1[0] - center[0])
    points = arc(center, radius, start, sweep, tol)
    points[-1] = p2  # land exactly on the next vertex
    return points


def svg_arc(
    p1: Point, p2: Point, rx: float, ry: float, phi_deg: float, large: bool, sweep_flag: bool, tol: float
) -> list[Point]:
    """SVG elliptical arc, endpoint parameterisation (SVG 1.1 appendix F.6)."""
    if p1 == p2:
        return []
    rx, ry = abs(rx), abs(ry)
    if rx == 0.0 or ry == 0.0:
        return [p2]
    phi = math.radians(phi_deg % 360.0)
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    dx2, dy2 = (p1[0] - p2[0]) / 2.0, (p1[1] - p2[1]) / 2.0
    x1p = cos_p * dx2 + sin_p * dy2
    y1p = -sin_p * dx2 + cos_p * dy2
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1.0:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep_flag:
        coef = -coef
    cxp, cyp = coef * rx * y1p / ry, -coef * ry * x1p / rx
    cx = cos_p * cxp - sin_p * cyp + (p1[0] + p2[0]) / 2.0
    cy = sin_p * cxp + cos_p * cyp + (p1[1] + p2[1]) / 2.0

    def angle(ux: float, uy: float, vx: float, vy: float) -> float:
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

    t1 = angle(1.0, 0.0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep_flag and dt > 0:
        dt -= 2 * math.pi
    elif sweep_flag and dt < 0:
        dt += 2 * math.pi
    major = (rx * cos_p, rx * sin_p)
    minor = (-ry * sin_p, ry * cos_p)
    points = ellipse((cx, cy), major, minor, t1, t1 + dt, tol)
    points[-1] = p2
    return points


def bspline(
    control: Sequence[Point], knots: Sequence[float], degree: int, weights: Sequence[float] | None, samples: int
) -> list[Point]:
    """Evaluate a (rational) B-spline at ``samples + 1`` evenly spaced parameters, start point included."""
    n = len(control)
    if degree < 1 or n <= degree or len(knots) != n + degree + 1:
        raise ValueError("inconsistent B-spline definition")
    w = list(weights) if weights and len(weights) == n else [1.0] * n
    t_start, t_end = knots[degree], knots[n]
    out = []
    for k in range(samples + 1):
        t = t_start + (t_end - t_start) * k / samples
        span = degree
        while span < n - 1 and knots[span + 1] <= t:
            span += 1
        d = [[control[j][0] * w[j], control[j][1] * w[j], w[j]] for j in range(span - degree, span + 1)]
        for r in range(1, degree + 1):
            for j in range(degree, r - 1, -1):
                i = j + span - degree
                denom = knots[i + degree - r + 1] - knots[i]
                alpha = 0.0 if denom == 0 else (t - knots[i]) / denom
                d[j] = [(1 - alpha) * d[j - 1][m] + alpha * d[j][m] for m in range(3)]
        x, y, ww = d[degree]
        out.append((x / ww, y / ww) if ww else (x, y))
    return out
