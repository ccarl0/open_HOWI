# SPDX-License-Identifier: GPL-3.0-or-later
"""Small SVG drawings for the cutting steps: how the foam sits, what is cut, and how to turn it.

They are drawn on white in fixed colours, so they read the same in the app
(light or dark) and on paper in the printable guide.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from html import escape

from .geometry import Point

FOAM = "#f3e3bf"
PART = "#e0c27a"
CUT = "#e03131"
INK = "#333"
MUTED = "#888"
JIG = "#c7d3e0"


def _f(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


class Canvas:
    """World coordinates (mm, y up) fitted into a ``width`` x ``height`` drawing."""

    def __init__(self, bounds: tuple[float, float, float, float], width: float = 300, height: float = 180, pad=18.0):
        x0, y0, x1, y1 = bounds
        self.w, self.h = width, height
        self.scale = min((width - 2 * pad) / max(x1 - x0, 1e-6), (height - 2 * pad) / max(y1 - y0, 1e-6))
        self.ox = (width - (x1 - x0) * self.scale) / 2 - x0 * self.scale
        self.oy = (height + (y1 - y0) * self.scale) / 2 + y0 * self.scale
        self.parts: list[str] = []

    def p(self, x: float, y: float) -> tuple[float, float]:
        return self.ox + x * self.scale, self.oy - y * self.scale

    def poly(self, points: Sequence[Point], fill: str = "none", stroke: str = INK, width: float = 1.0,
             dash: str | None = None, close: bool = True) -> None:  # fmt: skip
        if len(points) < 2:
            return
        d = " ".join(f"{'L' if i else 'M'}{_f(px)},{_f(py)}" for i, (px, py) in enumerate(self.p(*q) for q in points))
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<path d="{d}{" Z" if close else ""}" fill="{fill}" stroke="{stroke}" stroke-width="{width}"'
            f' stroke-linejoin="round"{extra}/>'
        )

    def line(self, a: Point, b: Point, stroke: str = INK, width: float = 1.0, dash: str | None = None) -> None:
        self.poly([a, b], stroke=stroke, width=width, dash=dash, close=False)

    def text(self, x: float, y: float, text: str, size: float = 10, anchor: str = "middle", color: str = INK,
             dy: float = 0.0, weight: str = "normal", halo: bool = False) -> None:  # fmt: skip
        px, py = self.p(x, y)
        outline = ' stroke="#fff" stroke-width="2.5" paint-order="stroke"' if halo else ""
        self.parts.append(
            f'<text x="{_f(px)}" y="{_f(py + dy)}" font-size="{size}" text-anchor="{anchor}" fill="{color}" '
            f'font-weight="{weight}"{outline}>{escape(text)}</text>'
        )

    def arc_arrow(self, centre: Point, radius: float, start: float, stop: float, color: str = CUT) -> None:
        """A curved arrow from angle ``start`` to ``stop`` (degrees, counter-clockwise positive)."""
        pts = [
            (centre[0] + radius * math.cos(math.radians(a)), centre[1] + radius * math.sin(math.radians(a)))
            for a in (start + (stop - start) * k / 16 for k in range(17))
        ]
        self.poly(pts, stroke=color, width=1.6, close=False)
        (ex, ey), (px, py) = self.p(*pts[-1]), self.p(*pts[-2])
        ang = math.atan2(ey - py, ex - px)
        head = [(ex, ey), (ex - 8 * math.cos(ang - 0.45), ey - 8 * math.sin(ang - 0.45)),
                (ex - 8 * math.cos(ang + 0.45), ey - 8 * math.sin(ang + 0.45))]  # fmt: skip
        d = " ".join(f"{'L' if i else 'M'}{_f(x)},{_f(y)}" for i, (x, y) in enumerate(head))
        self.parts.append(f'<path d="{d} Z" fill="{color}"/>')

    def svg(self, title: str = "") -> str:
        caption = (
            f'<text x="8" y="14" font-size="10.5" font-weight="bold" fill="{INK}">{escape(title)}</text>'
            if title
            else ""
        )
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_f(self.w)} {_f(self.h)}" '
            f'font-family="Helvetica, Arial, sans-serif" role="img">'
            f'<rect width="100%" height="100%" rx="6" fill="#fff"/>{caption}{"".join(self.parts)}</svg>'
        )


def _rotate(points: Sequence[Point], degrees: float, centre: Point = (0.0, 0.0)) -> list[Point]:
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return [
        (centre[0] + (x - centre[0]) * c - (y - centre[1]) * s, centre[1] + (x - centre[0]) * s + (y - centre[1]) * c)
        for x, y in points
    ]


def _square(half: float) -> list[Point]:
    return [(-half, -half), (half, -half), (half, half), (-half, half)]


def polygon(sides: int, apothem: float) -> list[Point]:
    """A regular polygon around the origin with a flat edge at the bottom (edge 1), counter-clockwise."""
    r = apothem / math.cos(math.pi / sides)
    return [
        (r * math.cos(math.radians(-90 - 180 / sides + 360 * k / sides)),
         r * math.sin(math.radians(-90 - 180 / sides + 360 * k / sides)))
        for k in range(sides)
    ]  # fmt: skip


def turned_end_view(
    section: Sequence[Point],
    half: float,
    sides: int,
    pass_number: int,
    cut_height: float,
    done: Sequence[float],
    apothem: float | None,
) -> str:
    """The block seen from the nose end during one pass of a turned fuselage.

    ``section`` is the largest section relative to the turning axis; ``done`` the
    heights of the cuts already made (pass 1, 2, ...); ``apothem`` the jig
    plates' (None on a rotary axis).
    """
    step = 360.0 / sides
    turn = -(pass_number - 1) * step  # clockwise, one edge per pass
    reach = (apothem / math.cos(math.pi / sides)) if apothem else half * math.sqrt(2)
    c = Canvas((-reach - 8, -(apothem or reach) - 14, reach + 8, reach + 8), 220, 200)
    if apothem:
        c.line((-reach - 6, -apothem), (reach + 6, -apothem), stroke=INK, width=1.6)
        plate = _rotate(polygon(sides, apothem), turn)
        c.poly(plate, fill=JIG, stroke="#7a8ea3")
    c.poly(_rotate(_square(half), turn), fill=FOAM, stroke=MUTED)
    c.poly(_rotate(section, turn), fill=PART, stroke=INK, width=0.8)
    for k, height in enumerate(done, start=1):  # earlier cuts, turned on with the block
        a = math.radians(90 + turn + (k - 1) * step)
        n = (math.cos(a), math.sin(a))
        t = (-n[1], n[0])
        p = (n[0] * height, n[1] * height)
        c.line((p[0] - t[0] * half * 1.3, p[1] - t[1] * half * 1.3), (p[0] + t[0] * half * 1.3, p[1] + t[1] * half * 1.3),
               stroke=MUTED, width=0.8, dash="3 2")  # fmt: skip
    c.line((-half * 1.45, cut_height), (half * 1.45, cut_height), stroke=CUT, width=2)
    c.text(half * 1.45, cut_height, "wire", size=8.5, anchor="start", color=CUT, dy=3)
    if apothem:
        for k in range(sides):  # edge numbers on the plate, turned with it, drawn on top
            mid = math.radians(-90 + k * step + turn)
            r = apothem * (0.9 if sides <= 8 else 0.93)
            c.text(r * math.cos(mid), r * math.sin(mid), str(k + 1), size=8 if sides <= 12 else 6.5,
                   color="#1f3448", dy=3, weight="bold", halo=True)  # fmt: skip
    if pass_number > 1:
        c.arc_arrow((0, 0), reach * 0.55, 90 + step * 0.9, 90 + step * 0.1)
    c.text(0, -(apothem or reach) - 10, "seen from the nose end", size=8, color=MUTED)
    return c.svg(f"Pass {pass_number}: edge {pass_number} down" if apothem else f"Pass {pass_number}: {-turn:g}°")


def side_view(
    block: tuple[float, float, float, float],
    outline: Sequence[Point],
    cuts: Sequence[Sequence[Point]],
    fence: float | None = None,
    support: float | None = None,
    plates: tuple[float, float, float, float] | None = None,
    title: str = "",
    closed: bool = False,
) -> str:
    """The machine seen along the wire: the foam block, the part, the wire's path in red.

    ``plates`` = (x of the block's rear face, x of its front face, half height, thickness) for a turning jig.
    """
    x0, y0, x1, y1 = block
    mid = (y0 + y1) / 2
    xs = [x0, x1] + ([fence] if fence is not None else []) + [p[0] for c in cuts for p in c]
    ys = [y0, y1] + ([support] if support is not None else []) + [p[1] for c in cuts for p in c]
    if plates:
        xs += [plates[0] - plates[3], plates[1] + plates[3]]
        ys += [mid - plates[2], mid + plates[2]]
    left, right, low, high = min(xs) - 10, max(xs) + 10, min(ys) - 14, max(ys) + 10
    c = Canvas((left, low, right, high), 300, 160)
    if support is not None:
        c.line((left, support), (right, support), stroke=INK, width=1.6)
    if plates:
        rear, front, half_h, thick = plates
        for a, b in ((rear - thick, rear), (front, front + thick)):
            c.poly([(a, mid - half_h), (b, mid - half_h), (b, mid + half_h), (a, mid + half_h)], fill=JIG,
                   stroke="#7a8ea3")  # fmt: skip
    c.poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], fill=FOAM, stroke=MUTED)
    c.poly(outline, fill=PART, stroke=INK, width=0.8)
    for path in cuts:
        c.poly(path, stroke=CUT, width=1.8, close=closed)
    if fence is not None:
        c.line((fence, low + 4), (fence, high - 4), stroke=INK, width=2.2)
        c.text(fence, low + 4, "stop", size=8, color=INK, dy=10)
    c.text(left + 2, high, "X = 0 side", size=8, anchor="start", color=MUTED, dy=10)
    return c.svg(title)


def section_view(
    rings: Sequence[Sequence[Point]],
    block: tuple[float, float, float, float],
    title: str = "",
    support: float | None = None,
    arrow: bool = False,
    note: str = "",
) -> str:
    """A cross-section of the foam (e.g. a slab laid on its side), with an optional turning arrow."""
    x0, y0, x1, y1 = block
    c = Canvas((x0 - 14, (support if support is not None else y0) - 14, x1 + 14, y1 + 10), 220, 170)
    if support is not None:
        c.line((x0 - 12, support), (x1 + 12, support), stroke=INK, width=1.6)
    c.poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], fill=FOAM, stroke=MUTED)
    for ring in rings:
        c.poly(ring, fill=PART, stroke=INK, width=0.8)
    if arrow:
        c.arc_arrow(((x0 + x1) / 2, (y0 + y1) / 2), 0.42 * max(x1 - x0, y1 - y0), 150, 60)
    if note:
        c.text((x0 + x1) / 2, (support if support is not None else y0) - 4, note, size=8, color=MUTED, dy=10)
    return c.svg(title)
