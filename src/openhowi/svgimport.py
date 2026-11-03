# SPDX-License-Identifier: GPL-3.0-or-later
"""Read the outlines in an SVG file as polylines in millimetres.

Supports paths (every command, including arcs), rect, circle, ellipse, line,
polyline, polygon, groups, nested transforms and ``<use>`` clones. Text must
be converted to paths in the drawing program first.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterator

from . import curves
from .drawing import Drawing, Polyline, ShapeError
from .geometry import Point

Matrix = tuple[float, float, float, float, float, float]  # a b c d e f: x' = a x + c y + e, y' = b x + d y + f
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
UNITS_MM = {"": 25.4 / 96, "px": 25.4 / 96, "mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72, "pc": 25.4 / 6,
            "q": 0.25}  # fmt: skip
SKIP = {"defs", "symbol", "clipPath", "mask", "marker", "pattern", "style", "script", "title", "desc", "metadata",
        "linearGradient", "radialGradient", "filter", "foreignObject", "namedview"}  # fmt: skip
UNSUPPORTED = {"text", "image", "textPath", "tspan", "flowRoot"}
MAX_USE_DEPTH = 16
MAX_NODES = 200_000
MAX_POINTS = 2_000_000
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_LENGTH = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-zA-Z%]*)\s*$")
_TRANSFORM = re.compile(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)")


def multiply(m: Matrix, n: Matrix) -> Matrix:
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a * a2 + c * b2,
        b * a2 + d * b2,
        a * c2 + c * d2,
        b * c2 + d * d2,
        a * e2 + c * f2 + e,
        b * e2 + d * f2 + f,
    )


def apply(m: Matrix, p: Point) -> Point:
    return (m[0] * p[0] + m[2] * p[1] + m[4], m[1] * p[0] + m[3] * p[1] + m[5])


def parse_transform(text: str | None) -> Matrix:
    m = IDENTITY
    for name, args in _TRANSFORM.findall(text or ""):
        v = [float(x) for x in _NUMBER.findall(args)]
        if name == "matrix" and len(v) == 6:
            t: Matrix = (v[0], v[1], v[2], v[3], v[4], v[5])
        elif name == "translate" and v:
            t = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0.0)
        elif name == "scale" and v:
            t = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate" and v:
            a = math.radians(v[0])
            t = (math.cos(a), math.sin(a), -math.sin(a), math.cos(a), 0, 0)
            if len(v) == 3:
                t = multiply(multiply((1, 0, 0, 1, v[1], v[2]), t), (1, 0, 0, 1, -v[1], -v[2]))
        elif name == "skewX" and v:
            t = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewY" and v:
            t = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            continue
        m = multiply(m, t)
    return m


def _length_mm(text: str | None) -> float | None:
    """A width/height attribute in millimetres, or None if absent or relative."""
    match = _LENGTH.match(text or "")
    if not match or match.group(2) == "%":
        return None
    unit = match.group(2).lower()
    if unit not in UNITS_MM:
        raise ShapeError(f"unknown SVG unit {unit!r}")
    return float(match.group(1)) * UNITS_MM[unit]


def _num(el: ET.Element, name: str, default: float = 0.0) -> float:
    value = el.get(name)
    if value is None:
        return default
    match = _LENGTH.match(value)
    if not match:
        raise ShapeError(f"cannot read {name}={value!r} on <{_tag(el)}>")
    return float(match.group(1))


def _tag(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _hidden(el: ET.Element) -> bool:
    style = (el.get("style") or "").replace(" ", "").lower()
    return el.get("display") == "none" or "display:none" in style or el.get("visibility") == "hidden"


class _PathReader:
    def __init__(self, d: str):
        self.d, self.i = d, 0

    def _skip(self) -> None:
        while self.i < len(self.d) and self.d[self.i] in " \t\r\n,":
            self.i += 1

    def done(self) -> bool:
        self._skip()
        return self.i >= len(self.d)

    def command(self) -> str | None:
        self._skip()
        if self.i < len(self.d) and self.d[self.i].isalpha() and self.d[self.i] not in "eE":
            self.i += 1
            return self.d[self.i - 1]
        return None

    def number(self) -> float:
        self._skip()
        match = _NUMBER.match(self.d, self.i)
        if not match:
            raise ShapeError(f"bad SVG path data near {self.d[self.i : self.i + 15]!r}")
        self.i = match.end()
        return float(match.group())

    def flag(self) -> bool:
        self._skip()
        if self.i < len(self.d) and self.d[self.i] in "01":
            self.i += 1
            return self.d[self.i - 1] == "1"
        raise ShapeError(f"bad arc flag in SVG path near {self.d[self.i : self.i + 15]!r}")


def parse_path(d: str, tol: float) -> list[Polyline]:
    """Flatten SVG path data into polylines in the path's own coordinates."""
    reader = _PathReader(d)
    out: list[Polyline] = []
    current: list[Point] = []
    x = y = sx = sy = 0.0
    last_ctrl: Point | None = None
    last_cmd = ""
    cmd = None

    def flush(closed: bool = False) -> None:
        nonlocal current
        if len(current) > 1:
            out.append(Polyline(current, closed))
        current = []

    while not reader.done():
        letter = reader.command()
        if letter is not None:
            cmd = letter
        elif cmd is None:
            raise ShapeError("SVG path data must start with a move command")
        assert cmd is not None
        rel = cmd.islower()
        c = cmd.upper()
        ox, oy = (x, y) if rel else (0.0, 0.0)
        if c == "Z":
            if current:
                if current[-1] != current[0]:
                    current.append(current[0])
                flush(True)
            x, y = sx, sy
            last_ctrl, last_cmd = None, "Z"
            continue
        if c == "M":
            flush()
            x, y = ox + reader.number(), oy + reader.number()
            sx, sy = x, y
            current = [(x, y)]
            cmd = "l" if rel else "L"  # further coordinate pairs are line-tos
            last_ctrl, last_cmd = None, "M"
            continue
        if not current:  # drawing after Z starts a new subpath at the last point
            current = [(x, y)]
        if c == "L":
            x, y = ox + reader.number(), oy + reader.number()
            current.append((x, y))
        elif c == "H":
            x = ox + reader.number()
            current.append((x, y))
        elif c == "V":
            y = oy + reader.number()
            current.append((x, y))
        elif c in "CS":
            if c == "C":
                c1 = (ox + reader.number(), oy + reader.number())
            else:
                c1 = (2 * x - last_ctrl[0], 2 * y - last_ctrl[1]) if last_cmd in "CS" and last_ctrl else (x, y)
            c2 = (ox + reader.number(), oy + reader.number())
            end = (ox + reader.number(), oy + reader.number())
            current += curves.cubic((x, y), c1, c2, end, tol)
            last_ctrl, (x, y) = c2, end
        elif c in "QT":
            if c == "Q":
                q = (ox + reader.number(), oy + reader.number())
            else:
                q = (2 * x - last_ctrl[0], 2 * y - last_ctrl[1]) if last_cmd in "QT" and last_ctrl else (x, y)
            end = (ox + reader.number(), oy + reader.number())
            current += curves.quadratic((x, y), q, end, tol)
            last_ctrl, (x, y) = q, end
        elif c == "A":
            rx, ry, phi = reader.number(), reader.number(), reader.number()
            large, sweep = reader.flag(), reader.flag()
            end = (ox + reader.number(), oy + reader.number())
            current += curves.svg_arc((x, y), end, rx, ry, phi, large, sweep, tol)
            x, y = end
        else:
            raise ShapeError(f"unknown SVG path command {cmd!r}")
        last_cmd = c
        if c not in "CSQT":
            last_ctrl = None
    flush()
    return out


def _points_attr(text: str | None) -> list[Point]:
    v = [float(n) for n in _NUMBER.findall(text or "")]
    return list(zip(v[0::2], v[1::2]))


def _shape_paths(el: ET.Element, tag: str, tol: float) -> list[Polyline]:
    if tag == "path":
        return parse_path(el.get("d") or "", tol)
    if tag == "rect":
        x, y, w, h = _num(el, "x"), _num(el, "y"), _num(el, "width"), _num(el, "height")
        if w <= 0 or h <= 0:
            return []
        rx = el.get("rx")
        ry = el.get("ry")
        rxv = _num(el, "rx") if rx is not None else (_num(el, "ry") if ry is not None else 0.0)
        ryv = _num(el, "ry") if ry is not None else rxv
        rxv, ryv = min(rxv, w / 2), min(ryv, h / 2)
        if rxv <= 0 or ryv <= 0:
            return [Polyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)], True)]
        d = (
            f"M{x + rxv},{y} H{x + w - rxv} A{rxv},{ryv} 0 0 1 {x + w},{y + ryv} V{y + h - ryv} "
            f"A{rxv},{ryv} 0 0 1 {x + w - rxv},{y + h} H{x + rxv} A{rxv},{ryv} 0 0 1 {x},{y + h - ryv} "
            f"V{y + ryv} A{rxv},{ryv} 0 0 1 {x + rxv},{y} Z"
        )
        return parse_path(d, tol)
    if tag in ("circle", "ellipse"):
        cx, cy = _num(el, "cx"), _num(el, "cy")
        rx = _num(el, "r") if tag == "circle" else _num(el, "rx")
        ry = rx if tag == "circle" else _num(el, "ry")
        if rx <= 0 or ry <= 0:
            return []
        points = [(cx + rx, cy)] + curves.ellipse((cx, cy), (rx, 0.0), (0.0, ry), 0.0, 2 * math.pi, tol)
        return [Polyline(points, True)]
    if tag == "line":
        return [Polyline([(_num(el, "x1"), _num(el, "y1")), (_num(el, "x2"), _num(el, "y2"))], False)]
    if tag in ("polyline", "polygon"):
        pts = _points_attr(el.get("points"))
        if len(pts) < 2:
            return []
        if tag == "polygon":
            return [Polyline(pts + [pts[0]], True)]
        return [Polyline(pts, False)]
    return []


def parse_svg(text: str, tol: float = 0.05) -> Drawing:
    if re.search(r"<!ENTITY", text[:10000], re.IGNORECASE):
        raise ShapeError("SVG files with entity declarations are not supported")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ShapeError(f"not a valid SVG file: {exc}") from exc
    if _tag(root) != "svg":
        raise ShapeError("not an SVG file (the root element is not <svg>)")

    # Document units: viewBox -> millimetres
    vb = [float(v) for v in _NUMBER.findall(root.get("viewBox") or "")]
    width_mm, height_mm = _length_mm(root.get("width")), _length_mm(root.get("height"))
    px = UNITS_MM["px"]
    if len(vb) == 4 and vb[2] > 0 and vb[3] > 0:
        sx = width_mm / vb[2] if width_mm else (height_mm / vb[3] if height_mm else px)
        sy = height_mm / vb[3] if height_mm else sx
        doc: Matrix = (sx, 0.0, 0.0, -sy, -vb[0] * sx, vb[1] * sy)  # y flipped: SVG y points down
    else:
        doc = (px, 0.0, 0.0, -px, 0.0, 0.0)

    ids = {el.get("id"): el for el in root.iter() if el.get("id")}
    drawing = Drawing()
    ignored: Counter[str] = Counter()
    budget = {"nodes": MAX_NODES, "points": MAX_POINTS}

    def walk(el: ET.Element, m: Matrix, depth: int, in_use: frozenset[str]) -> Iterator[Polyline]:
        budget["nodes"] -= 1
        if budget["nodes"] < 0:
            raise ShapeError("the SVG file is too complex (too many elements or clones)")
        tag = _tag(el)
        if tag in SKIP or _hidden(el):
            return
        if tag in UNSUPPORTED:
            ignored[tag] += 1
            return
        m = multiply(m, parse_transform(el.get("transform")))
        if tag == "use":
            href = el.get("href") or el.get("{http://www.w3.org/1999/xlink}href") or ""
            target = ids.get(href[1:]) if href.startswith("#") else None
            if target is None or depth >= MAX_USE_DEPTH or href in in_use:
                return
            m = multiply(m, (1, 0, 0, 1, _num(el, "x"), _num(el, "y")))
            if _tag(target) == "symbol":
                for child in target:
                    yield from walk(child, m, depth + 1, in_use | {href})
            else:
                yield from walk(target, m, depth + 1, in_use | {href})
            return
        if tag in ("svg", "g", "a", "switch") or el is root:
            if tag == "svg" and el is not root:
                m = multiply(m, (1, 0, 0, 1, _num(el, "x"), _num(el, "y")))
            for child in el:
                yield from walk(child, m, depth, in_use)
            return
        scale = math.sqrt(abs(m[0] * m[3] - m[1] * m[2])) or 1.0
        for poly in _shape_paths(el, tag, tol / scale):
            budget["points"] -= len(poly.points)
            if budget["points"] < 0:
                raise ShapeError("the SVG file is too complex (too many points)")
            yield Polyline([apply(m, p) for p in poly.points], poly.closed)

    drawing.polylines = list(walk(root, doc, 0, frozenset()))
    if ignored:
        kinds = ", ".join(f"{n} <{k}>" for k, n in ignored.items())
        drawing.warnings.append(
            f"Ignored {kinds}: convert text and images to paths first (Inkscape: Path → Object to Path)."
        )
    return drawing
