# SPDX-License-Identifier: GPL-3.0-or-later
"""Full-size (1:1) plan-view template for cutting servo bays by hand.

The wire can only cut what runs through the whole panel, so servo bays are
marked from a printed template instead. It shows the core in plan view with
the spar slots, slits and bays at their true positions, plus the distances to
measure from the root face and the front of the core, and a 50 mm bar to
check the print scale.
"""

from __future__ import annotations

import math
from html import escape

from . import __version__
from .fuselage import Fuselage
from .geometry import Point
from .wing import BayGeometry, WingPanel

MARGIN = 15.0
FONT = 3.5


def _fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def bay_template_svg(wing: WingPanel, title: str = "Wing panel") -> str:
    """An SVG drawing in millimetres; print it at 100 % (actual size)."""
    bays = wing.bay_geometry()
    if not bays:
        raise ValueError("this panel has no servo bays")
    assert wing.tip_chord is not None
    sections = wing.sections()
    cut = sections.cutbacks
    tc, span = wing.tip_chord, wing.span

    def tip_x(fraction: float) -> float:
        return wing.tip_point(fraction)[0]

    planform = [(0.0, 0.0), (span, tip_x(0.0)), (span, tip_x(1.0)), (0.0, wing.root_chord)]  # (z, x)
    core = [
        (0.0, cut["root_le"]),
        (span, tip_x(cut["tip_le"] / tc)),
        (span, tip_x(1.0 - cut["tip_te"] / tc)),
        (0.0, wing.root_chord - cut["root_te"]),
    ]
    cut_back = any(v > 0.01 for v in cut.values())
    x_min = min(x for _, x in planform)
    x_max = max(x for _, x in planform)
    depth = x_max - x_min

    # Lines along the span: (label, surfaces it shows on, root x, tip x, slot width or None)
    lines: list[tuple[str, set[str], float, float, float | None]] = []
    for number, spar in enumerate(wing.spars, start=1):
        sides = {side for side, d in (("top", spar.depth_top), ("bottom", spar.depth_bottom)) if d > 0}
        if sides:
            x0 = spar.position / 100.0 * wing.root_chord
            lines.append((f"spar {number}", sides, x0, tip_x(spar.at(True) / 100.0), spar.width))
    names = [f"servo channel {n}" for n in range(1, len(wing.channels) + 1)]
    names += [f"joiner {n}" for n in range(1, len(wing.joiners) + 1)]
    for name, root_hole, tip_hole in zip(names, sections.root_holes, sections.tip_holes):
        (_, c0, s0), (_, c1, s1) = root_hole, tip_hole
        lines.append((f"{name} slit", {s0.side}, s0.ring(c0)[0][0], s1.ring(c1)[0][0], None))

    width = max(span + 2 * MARGIN + 55.0, 180.0)
    parts: list[str] = []
    y = MARGIN
    parts.append(f'<text x="{MARGIN}" y="{y + 5}" font-size="6" font-weight="bold">{escape(title)}: servo bays</text>')
    y += 11
    for line in (
        "Print at 100 % (actual size, no “fit to page”); tile large panels over several sheets.",
        "Check the 50 mm bar below after printing. Distances are in mm.",
    ):
        parts.append(f'<text x="{MARGIN}" y="{y}" font-size="{FONT}">{escape(line)}</text>')
        y += FONT * 1.5

    for side in ("top", "bottom"):
        side_bays = [b for b in bays if b.bay.side == side]
        if not side_bays:
            continue
        y += 6
        caption = (
            "Top surface, seen from above: right-hand panel (print mirrored for the left-hand one)"
            if side == "top"
            else "Bottom surface, seen from below with the panel turned over, root still on the left"
        )
        parts.append(f'<text x="{MARGIN}" y="{y}" font-size="{FONT * 1.2}" font-weight="bold">{escape(caption)}</text>')
        y += 6
        top = y

        def to_svg(z: float, x: float, top: float = top, side: str = side) -> Point:
            return (MARGIN + 10.0 + z, top + (x - x_min if side == "top" else x_max - x))

        def poly(points: list[tuple[float, float]], style: str) -> str:
            pts = " ".join(f"{_fmt(px)},{_fmt(py)}" for px, py in (to_svg(z, x) for z, x in points))
            return f'<polygon points="{pts}" {style}/>'

        if cut_back:
            parts.append(poly(planform, 'fill="none" stroke="#888" stroke-width="0.25" stroke-dasharray="2 1.5"'))
        parts.append(poly(core, 'fill="none" stroke="#000" stroke-width="0.4"'))
        for label, line_sides, x0, x1, w in lines:
            here = side in line_sides
            style = 'stroke="#555" stroke-width="0.25"' + ("" if here else ' stroke-dasharray="0.6 1.2"')
            if w is not None and here:
                band = [(0.0, x0 - w / 2), (span, x1 - w / 2), (span, x1 + w / 2), (0.0, x0 + w / 2)]
                parts.append(poly(band, f'fill="#ddd" {style}'))
            else:
                (ax, ay), (bx, by) = to_svg(0.0, x0), to_svg(span, x1)
                parts.append(f'<line x1="{_fmt(ax)}" y1="{_fmt(ay)}" x2="{_fmt(bx)}" y2="{_fmt(by)}" {style}/>')
            lx, ly = to_svg(span, x1)
            note = label if here else f"{label} (other surface)"
            parts.append(
                f'<text x="{_fmt(lx + 1.5)}" y="{_fmt(ly + 1)}" font-size="2.5" fill="#555">{escape(note)}</text>'
            )
        rx0, ry0 = to_svg(0.0, x_min)
        _, ry1 = to_svg(0.0, x_max)
        parts.append(
            f'<text x="{_fmt(rx0 - 2)}" y="{_fmt((ry0 + ry1) / 2)}" font-size="2.5" text-anchor="middle" '
            f'transform="rotate(-90 {_fmt(rx0 - 2)} {_fmt((ry0 + ry1) / 2)})">root face</text>'
        )
        le_label = to_svg(span / 2, (tip_x(0.0) + 0.0) / 2)
        parts.append(
            f'<text x="{_fmt(le_label[0])}" y="{_fmt(le_label[1] + (-1.5 if side == "top" else 3.5))}" font-size="2.5" '
            f'fill="#555" text-anchor="middle">leading edge</text>'
        )
        for bay in side_bays:
            parts += _bay(bay, to_svg)
        y = top + depth + 6
        for bay in side_bays:
            reaches = f"; servo channel {', '.join(map(str, bay.channels))} runs into it" if bay.channels else ""
            text = (
                f"Bay {bay.number}: {_fmt(bay.bay.length)} × {_fmt(bay.bay.width)} mm, {_fmt(bay.bay.depth)} mm deep. "
                f"Inboard edge {_fmt(bay.z0)} mm from the root face, front edge {_fmt(bay.x0 - bay.core_le)} mm "
                f"behind the front of the core{reaches}."
            )
            parts.append(f'<text x="{MARGIN}" y="{_fmt(y)}" font-size="{FONT}">{escape(text)}</text>')
            y += FONT * 1.5

    y += 6
    parts.append(
        f'<path d="M{MARGIN},{_fmt(y)} v-2 M{MARGIN},{_fmt(y)} h50 v-2 M{MARGIN + 10},{_fmt(y)} v-1 '
        f'M{MARGIN + 20},{_fmt(y)} v-1 M{MARGIN + 30},{_fmt(y)} v-1 M{MARGIN + 40},{_fmt(y)} v-1" '
        'fill="none" stroke="#000" stroke-width="0.4"/>'
    )
    parts.append(f'<text x="{MARGIN + 53}" y="{_fmt(y)}" font-size="{FONT}">50 mm</text>')
    y += 8
    parts.append(f'<text x="{MARGIN}" y="{_fmt(y)}" font-size="2.5" fill="#777">open_HOWI {__version__}</text>')
    height = y + MARGIN / 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(width)}mm" height="{_fmt(height)}mm" '
        f'viewBox="0 0 {_fmt(width)} {_fmt(height)}" font-family="Helvetica, Arial, sans-serif">\n'
        f'<rect width="100%" height="100%" fill="#fff"/>\n' + "\n".join(parts) + "\n</svg>\n"
    )


def _bay(bay: BayGeometry, to_svg) -> list[str]:  # type: ignore[no-untyped-def]
    (ax, ay), (bx, by) = to_svg(bay.z0, bay.x0), to_svg(bay.z1, bay.x1)
    x, y, w, h = min(ax, bx), min(ay, by), abs(bx - ax), abs(by - ay)
    out = [
        (
            f'<rect x="{_fmt(x)}" y="{_fmt(y)}" width="{_fmt(w)}" height="{_fmt(h)}" fill="#ffd9cc" '
            'stroke="#c0392b" stroke-width="0.4"/>'
        ),
        (
            f'<text x="{_fmt(x + w / 2)}" y="{_fmt(y + h / 2 + 1.5)}" font-size="4" font-weight="bold" '
            f'text-anchor="middle" fill="#c0392b">{bay.number}</text>'
        ),
    ]
    xc = (bay.x0 + bay.x1) / 2
    # From the root face to the inboard edge
    (r0x, r0y), (r1x, _) = to_svg(0.0, xc), to_svg(bay.z0, xc)
    out.append(
        f'<path d="M{_fmt(r0x)},{_fmt(r0y)} H{_fmt(r1x)} M{_fmt(r1x)},{_fmt(r0y - 1)} v2" fill="none" '
        'stroke="#c0392b" stroke-width="0.2"/>'
    )
    out.append(
        f'<text x="{_fmt((r0x + r1x) / 2)}" y="{_fmt(r0y - 1)}" font-size="2.8" text-anchor="middle" '
        f'fill="#c0392b">{_fmt(bay.z0)}</text>'
    )
    # From the front of the core to the front edge, at the bay's centre station
    zc = (bay.z0 + bay.z1) / 2
    (f0x, f0y), (_, f1y) = to_svg(zc, bay.core_le), to_svg(zc, bay.x0)
    out.append(
        f'<path d="M{_fmt(f0x)},{_fmt(f0y)} V{_fmt(f1y)} M{_fmt(f0x - 1)},{_fmt(f0y)} h2" fill="none" '
        'stroke="#c0392b" stroke-width="0.2"/>'
    )
    out.append(
        f'<text x="{_fmt(f0x + 1)}" y="{_fmt((f0y + f1y) / 2 + 1)}" font-size="2.8" fill="#c0392b">'
        f"{_fmt(bay.x0 - bay.core_le)}</text>"
    )
    return out


# Fuselage plans ---------------------------------------------------------------------------------------


def _clip(ring: list[Point], limit: float, keep_below: bool) -> list[Point]:
    """The part of a closed outline below (or above) a horizontal line."""

    def inside(p: Point) -> bool:
        return p[1] <= limit if keep_below else p[1] >= limit

    out: list[Point] = []
    for a, b in zip(ring, ring[1:] + ring[:1]):
        if inside(a):
            out.append(a)
        if inside(a) != inside(b):
            t = (limit - a[1]) / (b[1] - a[1])
            out.append((a[0] + (b[0] - a[0]) * t, limit))
    return out


def fuselage_plans_svg(fuse: Fuselage, title: str = "Fuselage", skin: float = 0.0, spacing: float = 150.0) -> str:
    """Side view, top view and formers at 1:1, in millimetres; print at 100 %."""
    gap = 12.0
    length = fuse.length
    width = max(length + 2 * MARGIN + 40.0, 190.0)
    parts: list[str] = []
    y = MARGIN
    parts.append(f'<text x="{MARGIN}" y="{y + 5}" font-size="6" font-weight="bold">{escape(title)}: plans</text>')
    y += 11
    for line in (
        "Print at 100 % (actual size, no “fit to page”); tile long fuselages over several sheets.",
        "Check the 50 mm bar after printing. Heights are from the datum line; distances in mm from the nose.",
    ):
        parts.append(f'<text x="{MARGIN}" y="{_fmt(y)}" font-size="{FONT}">{escape(line)}</text>')
        y += FONT * 1.5

    formers = [x for x in fuse.former_positions(spacing) if fuse.width(x) >= 3 and fuse.top(x) - fuse.bottom(x) >= 3]
    left = MARGIN + 10.0

    def path(points: list[Point], close: bool = True) -> str:
        d = " ".join(f"{'L' if i else 'M'}{_fmt(px)},{_fmt(py)}" for i, (px, py) in enumerate(points))
        return d + (" Z" if close else "")

    # Side view
    lo, hi = fuse.height_range()
    y += 8
    parts.append(f'<text x="{MARGIN}" y="{_fmt(y)}" font-size="{FONT * 1.2}" font-weight="bold">Side view</text>')
    base = y + 6 + hi  # svg y of the datum

    def side(p: Point) -> Point:
        return (left + p[0], base - p[1])

    parts.append(
        f'<path d="{path([side(p) for p in fuse.side_outline()])}" fill="none" stroke="#000" stroke-width="0.4"/>'
    )
    for saddle in fuse.saddles:
        foil = saddle.airfoil.normalized()
        phi = math.radians(-saddle.incidence)
        c, s = math.cos(phi), math.sin(phi)
        wing = [
            side((saddle.position + (fx * c - fy * s) * saddle.chord, saddle.height + (fx * s + fy * c) * saddle.chord))
            for fx, fy in foil.points
        ]
        parts.append(
            f'<path d="{path(wing)}" fill="none" stroke="#1c7ed6" stroke-width="0.25" stroke-dasharray="1.5 1"/>'
        )
        wx, wy = side((saddle.position + saddle.chord / 2, saddle.height))
        parts.append(
            f'<text x="{_fmt(wx)}" y="{_fmt(wy - 2)}" font-size="2.5" fill="#1c7ed6" text-anchor="middle">wing</text>'
        )
    if lo < 0 < hi:
        (dx0, dy0), (dx1, _) = side((-5, 0)), side((length + 5, 0))
        parts.append(
            f'<path d="M{_fmt(dx0)},{_fmt(dy0)} H{_fmt(dx1)}" stroke="#888" stroke-width="0.2" stroke-dasharray="4 1 1 1"/>'
        )
        parts.append(f'<text x="{_fmt(dx1 + 1)}" y="{_fmt(dy0 + 1)}" font-size="2.5" fill="#888">datum</text>')
    for k, x in enumerate(formers, start=1):
        (sx, sy0), (_, sy1) = side((x, fuse.top(x) + 3)), side((x, fuse.bottom(x) - 3))
        parts.append(f'<path d="M{_fmt(sx)},{_fmt(sy0)} V{_fmt(sy1)}" stroke="#c0392b" stroke-width="0.2"/>')
        parts.append(
            f'<text x="{_fmt(sx)}" y="{_fmt(sy0 - 1)}" font-size="2.5" fill="#c0392b" text-anchor="middle">F{k}</text>'
        )
    y = base - lo + 8

    # Top view
    half = fuse.max_width() / 2
    parts.append(f'<text x="{MARGIN}" y="{_fmt(y)}" font-size="{FONT * 1.2}" font-weight="bold">Top view</text>')
    mid = y + 6 + half

    def plan(p: Point) -> Point:
        return (left + p[0], mid - p[1])

    parts.append(
        f'<path d="{path([plan(p) for p in fuse.plan_outline()])}" fill="none" stroke="#000" stroke-width="0.4"/>'
    )
    (cx0, cy), (cx1, _) = plan((-5, 0)), plan((length + 5, 0))
    parts.append(
        f'<path d="M{_fmt(cx0)},{_fmt(cy)} H{_fmt(cx1)}" stroke="#888" stroke-width="0.2" stroke-dasharray="4 1 1 1"/>'
    )
    for k, x in enumerate(formers, start=1):
        w = fuse.width(x) / 2 + 3
        (sx, sy0), (_, sy1) = plan((x, w)), plan((x, -w))
        parts.append(f'<path d="M{_fmt(sx)},{_fmt(sy0)} V{_fmt(sy1)}" stroke="#c0392b" stroke-width="0.2"/>')
        parts.append(
            f'<text x="{_fmt(sx)}" y="{_fmt(sy0 - 1)}" font-size="2.5" fill="#c0392b" text-anchor="middle">F{k}</text>'
        )
    y = mid + half + 10

    # Formers
    if formers:
        label = "Formers" + (f", {_fmt(skin)} mm inside the skin (dashed)" if skin > 0 else "")
        parts.append(
            f'<text x="{MARGIN}" y="{_fmt(y)}" font-size="{FONT * 1.2}" font-weight="bold">{escape(label)}</text>'
        )
        y += 6
        cursor, row_height = MARGIN, 0.0
        for k, x in enumerate(formers, start=1):
            outer = fuse.section(x, 96)
            try:
                inner = fuse.section(x, 96, inset=skin) if skin > 0 else outer
            except ValueError:
                inner = outer
            for saddle in fuse.saddles:  # a former under the wing stops at the saddle
                limit = fuse.top(x) if saddle.side == "top" else fuse.bottom(x)
                line = saddle.line()
                if line[0][0] <= x <= line[-1][0]:
                    inner = _clip(inner, limit - (skin if saddle.side == "top" else -skin), saddle.side == "top")
            us = [u for u, _ in outer]
            vs = [v for _, v in outer]
            w, h = max(us) - min(us), max(vs) - min(vs)
            if cursor + w > width - MARGIN and cursor > MARGIN:
                cursor, y = MARGIN, y + row_height + 14
                row_height = 0.0
            ox, oy = cursor - min(us), y + max(vs)

            def at(p: Point, ox: float = ox, oy: float = oy) -> Point:
                return (ox + p[0], oy - p[1])

            if skin > 0:
                parts.append(
                    f'<path d="{path([at(p) for p in outer])}" fill="none" stroke="#888" stroke-width="0.25" '
                    'stroke-dasharray="1.5 1"/>'
                )
            parts.append(f'<path d="{path([at(p) for p in inner])}" fill="none" stroke="#000" stroke-width="0.4"/>')
            (vx, vy0), (_, vy1) = at((0, max(vs) + 2)), at((0, min(vs) - 2))
            parts.append(
                f'<path d="M{_fmt(vx)},{_fmt(vy0)} V{_fmt(vy1)}" stroke="#888" stroke-width="0.2" stroke-dasharray="2 1"/>'
            )
            if min(vs) < 0 < max(vs):
                (hx0, hy), (hx1, _) = at((min(us) - 2, 0)), at((max(us) + 2, 0))
                parts.append(
                    f'<path d="M{_fmt(hx0)},{_fmt(hy)} H{_fmt(hx1)}" stroke="#888" stroke-width="0.2" stroke-dasharray="2 1"/>'
                )
            parts.append(
                f'<text x="{_fmt(cursor + w / 2)}" y="{_fmt(y + h + 5)}" font-size="2.8" text-anchor="middle">'
                f"F{k} · {x:.0f} mm · {w:.0f} × {h:.0f}</text>"
            )
            cursor += max(w, 30.0) + gap
            row_height = max(row_height, h)
        y += row_height + 12

    y += 4
    parts.append(
        f'<path d="M{MARGIN},{_fmt(y)} v-2 M{MARGIN},{_fmt(y)} h50 v-2 M{MARGIN + 10},{_fmt(y)} v-1 '
        f'M{MARGIN + 20},{_fmt(y)} v-1 M{MARGIN + 30},{_fmt(y)} v-1 M{MARGIN + 40},{_fmt(y)} v-1" '
        'fill="none" stroke="#000" stroke-width="0.4"/>'
    )
    parts.append(f'<text x="{MARGIN + 53}" y="{_fmt(y)}" font-size="{FONT}">50 mm</text>')
    y += 8
    parts.append(f'<text x="{MARGIN}" y="{_fmt(y)}" font-size="2.5" fill="#777">open_HOWI {__version__}</text>')
    height = y + MARGIN / 2
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_fmt(width)}mm" height="{_fmt(height)}mm" '
        f'viewBox="0 0 {_fmt(width)} {_fmt(height)}" font-family="Helvetica, Arial, sans-serif">\n'
        f'<rect width="100%" height="100%" fill="#fff"/>\n' + "\n".join(parts) + "\n</svg>\n"
    )
