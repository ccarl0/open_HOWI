# SPDX-License-Identifier: GPL-3.0-or-later
"""A 3D model (STL file), and the 2D outlines of it that a hot wire can cut.

The wire only cuts straight lines, so a 3D model is cut through outlines of it:

* a silhouette: the model's outline seen along one axis, cut right through the block;
* a section: the outline of a thin slice through the model, for slabs cut between two sections.

Both are found on a fine grid (a raster) and traced back into outlines. That copes with the
untidy meshes many STL files have: overlapping parts, flipped faces, doubled triangles.

The model's own frame, after it is turned the right way up: X along the machine's X axis (its
length), Y up, Z along the wire (its width). The corner of its bounding box is at the origin.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import re
import struct
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

from .geometry import Point, signed_area
from .shapes import Region, build_regions

Point3 = tuple[float, float, float]
Triangle = tuple[Point3, Point3, Point3]

MAX_BYTES = 64 * 1024 * 1024
MAX_TRIANGLES = 2_000_000
UPS = ("+z", "-z", "+y", "-y", "+x", "-x")
"""Which axis of the file points up."""
VIEWS = ("side", "top", "front")
"""Silhouettes: seen along Z (X across, Y up), along Y from above (X across, Z up), along X (Z across, Y up)."""
MAX_CELLS = 6_000_000


class MeshError(ValueError):
    """The file is not a usable 3D model."""


# -- reading ---------------------------------------------------------------------------------


def read_stl(data: bytes) -> list[Triangle]:
    """The triangles of a binary or text (ASCII) STL file."""
    if len(data) > MAX_BYTES:
        raise MeshError(f"the STL file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if 84 + 50 * count == len(data):  # a text file of exactly this length is not plausible
            return _binary(data, count)
    head = data[:4096].lstrip().lower()
    if head.startswith(b"solid") and (b"facet" in head or b"endsolid" in head) or head.startswith(b"facet"):
        return _ascii(data)
    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if count and 84 + 50 * count <= len(data):  # trailing bytes after the triangles: some exporters add them
            return _binary(data, count)
    raise MeshError("not an STL file (neither a binary nor a text STL)")


def _binary(data: bytes, count: int) -> list[Triangle]:
    if count > MAX_TRIANGLES:
        raise MeshError(f"the model has {count:,} triangles; at most {MAX_TRIANGLES:,} are supported")
    out: list[Triangle] = []
    for rec in struct.iter_unpack("<12fH", data[84 : 84 + 50 * count]):
        out.append(((rec[3], rec[4], rec[5]), (rec[6], rec[7], rec[8]), (rec[9], rec[10], rec[11])))
    return _checked(out)


_VERTEX = re.compile(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", re.IGNORECASE)


def _ascii(data: bytes) -> list[Triangle]:
    try:
        coords = [(float(a), float(b), float(c)) for a, b, c in _VERTEX.findall(data)]
    except ValueError as exc:
        raise MeshError(f"the STL file has a vertex that is not a number ({exc})") from exc
    if len(coords) % 3:
        raise MeshError("the STL file has an incomplete triangle")
    if len(coords) // 3 > MAX_TRIANGLES:
        raise MeshError(f"the model has {len(coords) // 3:,} triangles; at most {MAX_TRIANGLES:,} are supported")
    return _checked([(coords[i], coords[i + 1], coords[i + 2]) for i in range(0, len(coords), 3)])


def _checked(triangles: list[Triangle]) -> list[Triangle]:
    if not triangles:
        raise MeshError("the STL file has no triangles")
    for t in triangles:
        for p in t:
            if not all(math.isfinite(c) for c in p):
                raise MeshError("the STL file has a vertex that is not a finite number")
    return triangles


# -- turning the right way up ----------------------------------------------------------------


def _up(p: Point3, up: str) -> Point3:
    x, y, z = p
    if up == "+z":
        return (x, z, -y)
    if up == "-z":
        return (x, -z, y)
    if up == "-y":
        return (x, -y, -z)
    if up == "+x":
        return (-y, x, z)
    if up == "-x":
        return (y, -x, z)
    return p


def _spin(p: Point3, quarter_turns: int) -> Point3:
    x, y, z = p
    for _ in range(quarter_turns % 4):
        x, z = z, -x  # a quarter turn about the vertical axis
    return (x, y, z)


@dataclass(frozen=True)
class Mesh:
    """A model in its own frame (X length, Y up, Z along the wire), its bounding box's corner at the origin."""

    triangles: tuple[Triangle, ...]
    size: Point3
    key: str = ""
    """Identifies the file and orientation, for caching its outlines ("" = not cached)."""

    @staticmethod
    def build(triangles: Sequence[Triangle], up: str = "+z", spin: float = 0.0, scale: float = 1.0) -> Mesh:
        if up not in UPS:
            raise MeshError(f"up must be one of {', '.join(UPS)}")
        if abs(spin / 90 - round(spin / 90)) > 1e-9:
            raise MeshError("the model turns in steps of 90°")
        if not scale > 0:
            raise MeshError("scale must be > 0")
        turns = round(spin / 90)
        moved = [tuple(_spin(_up(p, up), turns) for p in t) for t in triangles]
        lo = [min(p[i] for t in moved for p in t) for i in range(3)]
        hi = [max(p[i] for t in moved for p in t) for i in range(3)]
        size = tuple((hi[i] - lo[i]) * scale for i in range(3))
        if min(size) <= 1e-6:
            raise MeshError("the model is flat (it has no thickness in one direction)")
        tris = tuple(
            tuple(((p[0] - lo[0]) * scale, (p[1] - lo[1]) * scale, (p[2] - lo[2]) * scale) for p in t)  # type: ignore[misc]
            for t in moved
        )
        return Mesh(tris, size)  # type: ignore[arg-type]

    def silhouette(self, view: str, cell: float) -> list[Region]:
        """The outline seen along an axis (see ``VIEWS``), as shapes with holes, in that view's 2D frame."""
        axes = {"side": (0, 1), "top": (0, 2), "front": (2, 1)}.get(view)
        if axes is None:
            raise MeshError(f"view must be one of {', '.join(VIEWS)}")
        u, v = axes
        grid = _Grid(self.size[u], self.size[v], cell)
        for t in self.triangles:
            grid.fill_triangle((t[0][u], t[0][v]), (t[1][u], t[1][v]), (t[2][u], t[2][v]))
        return grid.regions()

    def section(self, z: float, cell: float) -> tuple[list[Region], int]:
        """The outline where the plane Z = z cuts the model (X across, Y up), and how many grid rows
        crossed it an odd number of times (a model that is not closed)."""
        grid = _Grid(self.size[0], self.size[1], cell)
        # Nudge the plane off any vertex lying on it, so every edge is clearly above or below it.
        zs = {p[2] for t in self.triangles for p in t if abs(p[2] - z) < 1e-9}
        if zs:
            z += 1e-7 * max(self.size)
        rows = grid.rows_for_segments(self._plane_segments(z))
        return grid.regions(), rows

    def _plane_segments(self, z: float) -> list[tuple[Point, Point]]:
        segments = []
        for t in self.triangles:
            below = [p for p in t if p[2] < z]
            if not below or len(below) == 3:
                continue
            above = [p for p in t if p[2] >= z]
            lone, pair = (below[0], above) if len(below) == 1 else (above[0], below)
            a, b = (_cross(lone, q, z) for q in pair)
            segments.append((a, b))
        return segments

    def display(self, limit: int = 12000) -> tuple[list[float], list[int]]:
        """A lighter copy for the 3D view: flat vertex coordinates and triangle indices."""
        # Start near the cell that brings the count under the limit: merging vertices on a grid
        # removes triangles roughly in proportion to the cell's area.
        cell = max(self.size) / 400 * max(1.0, 0.8 * math.sqrt(len(self.triangles) / limit))
        while True:
            vertices: dict[tuple[int, int, int], int] = {}
            coords: list[float] = []
            faces: set[tuple[int, int, int]] = set()
            ordered: list[int] = []
            for t in self.triangles:
                ids = []
                for p in t:
                    key = (round(p[0] / cell), round(p[1] / cell), round(p[2] / cell))
                    index = vertices.get(key)
                    if index is None:
                        index = vertices[key] = len(vertices)
                        coords += [round(p[0], 2), round(p[1], 2), round(p[2], 2)]
                    ids.append(index)
                if ids[0] == ids[1] or ids[1] == ids[2] or ids[0] == ids[2]:
                    continue
                rot = min(range(3), key=lambda k: ids[k])
                face = (ids[rot], ids[(rot + 1) % 3], ids[(rot + 2) % 3])
                if face not in faces:
                    faces.add(face)
                    ordered += face
            if len(ordered) // 3 <= limit or cell > max(self.size) / 8:
                return coords, ordered
            cell *= 1.4


def _cross(a: Point3, b: Point3, z: float) -> Point:
    """Where the edge a-b crosses the plane Z = z; the same point whichever end it starts from."""
    if (a[2], a[0], a[1]) > (b[2], b[0], b[1]):
        a, b = b, a
    t = (z - a[2]) / (b[2] - a[2])
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


# -- the grid --------------------------------------------------------------------------------


class _Grid:
    """Pixels over a W x H area with an empty border; a pixel is filled when its centre is inside."""

    def __init__(self, width: float, height: float, cell: float) -> None:
        if not cell > 0:
            raise MeshError("the grid cell must be > 0")
        self.cell = cell
        self.ox = self.oy = -1.5 * cell
        self.nx = math.ceil(width / cell) + 4
        self.ny = math.ceil(height / cell) + 4
        if self.nx * self.ny > MAX_CELLS:
            raise MeshError(
                f"the outline grid would have {self.nx * self.ny:,} cells: make the detail coarser (a larger value)"
            )
        self.rows = [bytearray(self.nx) for _ in range(self.ny)]

    def _span(self, j: int, xa: float, xb: float) -> None:
        if xb < xa:
            xa, xb = xb, xa
        i0 = max(0, math.ceil((xa - self.ox) / self.cell - 0.5 - 1e-9))
        i1 = min(self.nx - 1, math.floor((xb - self.ox) / self.cell - 0.5 + 1e-9))
        if i1 >= i0:
            self.rows[j][i0 : i1 + 1] = b"\x01" * (i1 - i0 + 1)

    def fill_triangle(self, a: Point, b: Point, c: Point) -> None:
        a, b, c = sorted((a, b, c), key=lambda p: p[1])
        ay, cy = a[1], c[1]
        if cy - ay < 1e-12:
            return
        cell, oy = self.cell, self.oy
        j0 = max(0, math.ceil((ay - oy) / cell - 0.5 - 1e-9))
        j1 = min(self.ny - 1, math.floor((cy - oy) / cell - 0.5 + 1e-9))
        for j in range(j0, j1 + 1):
            y = oy + (j + 0.5) * cell
            x_long = a[0] + (c[0] - a[0]) * (y - ay) / (cy - ay)
            if y < b[1] or (y == b[1] and b[1] > ay):
                lo, hi = a, b
            else:
                lo, hi = b, c
            if hi[1] - lo[1] < 1e-12:
                self._span(j, min(x_long, lo[0], hi[0]), max(x_long, lo[0], hi[0]))
                continue
            x_short = lo[0] + (hi[0] - lo[0]) * (y - lo[1]) / (hi[1] - lo[1])
            self._span(j, x_long, x_short)

    def rows_for_segments(self, segments: list[tuple[Point, Point]]) -> int:
        """Fill the inside of a set of segments row by row (even-odd); returns the rows crossed an odd number of times."""
        cell, oy = self.cell, self.oy
        crossings: dict[int, list[float]] = {}
        for (x0, y0), (x1, y1) in segments:
            if y0 == y1:
                continue
            if y0 > y1:
                x0, y0, x1, y1 = x1, y1, x0, y0
            j0 = max(0, math.ceil((y0 - oy) / cell - 0.5))
            j1 = min(self.ny - 1, math.ceil((y1 - oy) / cell - 0.5) - 1)  # half-open: y0 <= y < y1
            for j in range(j0, j1 + 1):
                y = oy + (j + 0.5) * cell
                crossings.setdefault(j, []).append(x0 + (x1 - x0) * (y - y0) / (y1 - y0))
        odd = 0
        for j, xs in crossings.items():
            xs.sort()
            if len(xs) % 2:
                odd += 1
                xs.pop()
            for k in range(0, len(xs), 2):
                self._span(j, xs[k], xs[k + 1])
        return odd

    def regions(self) -> list[Region]:
        loops = [_simplify(loop, 0.55 * self.cell) for loop in self._contours()]
        minimum = 4 * self.cell * self.cell
        return build_regions([lp for lp in loops if len(lp) >= 3 and abs(signed_area(lp)) >= minimum])

    def _contours(self) -> list[list[Point]]:
        """Trace the filled pixels' outlines (marching squares), filled side on the left."""
        nexts: dict[tuple[int, int], tuple[int, int]] = {}
        ints = [int.from_bytes(r, "little") for r in self.rows]
        for r in range(self.ny - 1):
            a, b = ints[r], ints[r + 1]
            if not (a or b):
                continue
            mask = (a ^ (a >> 8)) | (b ^ (b >> 8)) | (a ^ b)
            while mask:
                low = mask & -mask
                i = (low.bit_length() - 1) >> 3
                mask ^= low
                if i >= self.nx - 1:
                    continue
                case = (
                    (a >> (8 * i) & 1)
                    | (a >> (8 * i + 8) & 1) << 1
                    | (b >> (8 * i + 8) & 1) << 2
                    | (b >> (8 * i) & 1) << 3
                )
                for s, e in _CASES[case]:
                    nexts[(2 * i + s[0], 2 * r + s[1])] = (2 * i + e[0], 2 * r + e[1])
        loops = []
        cell, ox, oy = self.cell, self.ox, self.oy
        while nexts:
            start, here = next(iter(nexts.items()))
            del nexts[start]
            ring = [start]
            while here != start:
                ring.append(here)
                step = nexts.pop(here, None)
                if step is None:  # cannot happen with an empty border; give up on this piece
                    break
                here = step
            loops.append([(ox + (kx / 2 + 0.5) * cell, oy + (ky / 2 + 0.5) * cell) for kx, ky in ring])
        return loops


# Edge midpoints in doubled cell units: bottom (1, 0), right (2, 1), top (1, 2), left (0, 1).
_B, _R, _T, _L = (1, 0), (2, 1), (1, 2), (0, 1)
_CASES: dict[int, list[tuple[tuple[int, int], tuple[int, int]]]] = {
    0: [],
    1: [(_B, _L)],
    2: [(_R, _B)],
    3: [(_R, _L)],
    4: [(_T, _R)],
    5: [(_B, _L), (_T, _R)],
    6: [(_T, _B)],
    7: [(_T, _L)],
    8: [(_L, _T)],
    9: [(_B, _T)],
    10: [(_R, _B), (_L, _T)],
    11: [(_R, _T)],
    12: [(_L, _R)],
    13: [(_B, _R)],
    14: [(_L, _B)],
    15: [],
}


def _simplify(ring: list[Point], tol: float) -> list[Point]:
    """Douglas-Peucker on a closed ring."""
    n = len(ring)
    if n < 4:
        return ring
    far = max(range(n), key=lambda k: (ring[k][0] - ring[0][0]) ** 2 + (ring[k][1] - ring[0][1]) ** 2)
    keep = [False] * n
    keep[0] = keep[far] = True
    stack = [(0, far), (far, n)]
    while stack:
        i, j = stack.pop()
        a, b = ring[i], ring[j % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        best, worst = -1.0, -1
        for k in range(i + 1, j):
            p = ring[k]
            if length < 1e-12:
                d = math.hypot(p[0] - a[0], p[1] - a[1])
            else:
                d = abs(dx * (p[1] - a[1]) - dy * (p[0] - a[0])) / length
            if d > best:
                best, worst = d, k
        if best > tol:
            keep[worst] = True
            stack += [(i, worst), (worst, j)]
    return [p for p, k in zip(ring, keep) if k]


# -- caching ---------------------------------------------------------------------------------

_MESHES: OrderedDict[str, Mesh] = OrderedDict()
_OUTLINES: OrderedDict[tuple[object, ...], object] = OrderedDict()


def cached_mesh(data: bytes, up: str, spin: float, scale: float) -> Mesh:
    """The oriented model, kept for a while: the app re-slices on every edit."""
    key = f"{hashlib.sha1(data).hexdigest()}:{up}:{float(spin)}:{float(scale)}"
    mesh = _MESHES.get(key)
    if mesh is None:
        mesh = dataclasses.replace(Mesh.build(read_stl(data), up, spin, scale), key=key)
        _MESHES[key] = mesh
        while len(_MESHES) > 4:
            _MESHES.popitem(last=False)
    else:
        _MESHES.move_to_end(key)
    return mesh


def cached(mesh: Mesh, what: str, *args: object) -> object:
    """``mesh.silhouette(...)``, ``mesh.section(...)`` or ``mesh.display(...)``, remembered."""
    if not mesh.key:
        return getattr(mesh, what)(*args)
    key = (mesh.key, what, *args)
    value = _OUTLINES.get(key)
    if value is None:
        value = getattr(mesh, what)(*args)
        _OUTLINES[key] = value
        while len(_OUTLINES) > 64:
            _OUTLINES.popitem(last=False)
    else:
        _OUTLINES.move_to_end(key)
    return value


def default_cell(mesh: Mesh) -> float:
    """A grid fine enough for foam: about 600 cells along the longest side, between 0.2 and 2 mm."""
    return min(2.0, max(0.2, round(max(mesh.size) / 600, 2)))


__all__ = ["UPS", "VIEWS", "Mesh", "MeshError", "cached", "cached_mesh", "default_cell", "read_stl"]
