# SPDX-License-Identifier: GPL-3.0-or-later
"""Airfoil loading (Selig / Lednicer ``.dat`` files) and NACA 4-digit generation.

Internally an airfoil is always stored in Selig order: trailing edge, along the
upper surface to the leading edge, then along the lower surface back to the
trailing edge (counter-clockwise with x pointing aft and y up).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from .geometry import Point, cosine_spacing, dedupe, resample, signed_area


class AirfoilError(ValueError):
    """Raised when an airfoil cannot be parsed or generated."""


@dataclass(frozen=True)
class Airfoil:
    name: str
    points: tuple[Point, ...]

    def __post_init__(self) -> None:
        if len(self.points) < 5:
            raise AirfoilError(f"airfoil {self.name!r} has too few points ({len(self.points)})")

    @classmethod
    def from_file(cls, path: str | Path) -> Airfoil:
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise AirfoilError(f"cannot read airfoil file {path}: {exc}") from exc
        return parse_dat(text, default_name=path.stem)

    @property
    def le_index(self) -> int:
        """Index of the leading edge (the point with the smallest x)."""
        return min(range(len(self.points)), key=lambda i: self.points[i][0])

    def upper(self) -> list[Point]:
        """Upper surface, trailing edge to leading edge."""
        return list(self.points[: self.le_index + 1])

    def lower(self) -> list[Point]:
        """Lower surface, leading edge to trailing edge."""
        return list(self.points[self.le_index :])

    def normalized(self) -> Airfoil:
        """Leading edge at the origin and unit chord."""
        lx, ly = self.points[self.le_index]
        te_x = 0.5 * (self.points[0][0] + self.points[-1][0])
        chord = te_x - lx
        if chord <= 0.0:
            raise AirfoilError(f"airfoil {self.name!r} has no positive chord")
        return Airfoil(self.name, tuple(((x - lx) / chord, (y - ly) / chord) for x, y in self.points))

    def resampled(self, n_per_surface: int) -> Airfoil:
        """Resample both surfaces with ``n_per_surface`` cosine-spaced points.

        Two airfoils resampled with the same ``n_per_surface`` have matching
        point indices (trailing edges and leading edge line up), which is what
        a 4-axis cut between two different sections needs.
        """
        if n_per_surface < 3:
            raise AirfoilError("need at least 3 points per surface")
        le = self.le_index
        if le == 0 or le == len(self.points) - 1:
            raise AirfoilError(f"could not find the leading edge of {self.name!r}")
        fractions = cosine_spacing(n_per_surface)
        upper = resample(self.points[: le + 1], fractions)
        lower = resample(self.points[le:], fractions)
        return Airfoil(self.name, tuple(upper + lower[1:]))

    def to_dat(self) -> str:
        """Serialise in Selig format."""
        lines = [self.name] + [f"{x:.6f} {y:.6f}" for x, y in self.points]
        return "\n".join(lines) + "\n"


def thicken_te(airfoil: Airfoil, thickness: float) -> Airfoil:
    """The airfoil with its trailing edge opened to at least ``thickness`` (a fraction of the chord).

    Each surface moves away from the chord line in proportion to the distance from the leading edge,
    so the nose is untouched and the section thickens smoothly towards the back. A foam trailing
    edge thinner than the kerf would melt away.
    """
    foil = airfoil.normalized()
    gap = foil.points[0][1] - foil.points[-1][1]
    extra = thickness - gap
    if extra <= 1e-9:
        return airfoil
    le = foil.le_index
    points = tuple(
        (x, y + (extra / 2 if i <= le else -extra / 2) * x if i != le else y) for i, (x, y) in enumerate(foil.points)
    )
    return Airfoil(airfoil.name, points)


def blend(a: Airfoil, b: Airfoil, t: float, n_per_surface: int = 120) -> Airfoil:
    """The section ``t`` of the way from ``a`` to ``b`` (0: ``a``, 1: ``b``), as a wing tapering from one
    airfoil to the other has it in between."""
    if t <= 1e-9 or a.points == b.points:
        return a
    if t >= 1 - 1e-9:
        return b
    pa = a.normalized().resampled(n_per_surface).points
    pb = b.normalized().resampled(n_per_surface).points
    points = tuple((xa + (xb - xa) * t, ya + (yb - ya) * t) for (xa, ya), (xb, yb) in zip(pa, pb))
    return Airfoil(f"{a.name}/{b.name} {t:.0%}", points)


def _canonical(points: list[Point]) -> tuple[Point, ...]:
    pts = dedupe(points)
    if signed_area(pts) < 0.0:  # TE -> lower -> LE -> upper; flip to Selig order
        pts.reverse()
    return tuple(pts)


def _pair(line: str) -> Point | None:
    tokens = line.replace(",", " ").split()
    if len(tokens) < 2:
        return None
    try:
        return (float(tokens[0]), float(tokens[1]))
    except ValueError:
        return None


def parse_dat(text: str, default_name: str = "airfoil") -> Airfoil:
    """Parse a Selig or Lednicer formatted airfoil coordinate file.

    The first line is the name. Notes after the coordinates are ignored when a
    blank line separates them, as in many files of the UIUC database.
    """
    name: str | None = None
    pairs: list[Point] = []
    blank = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            blank = True
            continue
        pair = _pair(line)
        if pair is None:
            if name is None and not pairs:
                name = line
                continue
            if pairs and blank:
                break  # trailing notes
            raise AirfoilError(f"cannot parse airfoil line: {raw!r}")
        pairs.append(pair)
        blank = False

    if not pairs:
        raise AirfoilError("no coordinates found")

    # Lednicer files start with the point counts of each surface, e.g. "17. 17."
    first, body = pairs[0], pairs[1:]
    looks_like_counts = all(v >= 2 and v == int(v) for v in first)
    if looks_like_counts and int(first[0]) + int(first[1]) == len(body):
        n_upper = int(first[0])
        upper, lower = body[:n_upper], body[n_upper:]  # both leading edge -> trailing edge
        points = list(reversed(upper)) + lower
    elif looks_like_counts and body and max(abs(x) for x, _ in body) <= 1.5:
        raise AirfoilError(f"Lednicer header announces {int(first[0])}+{int(first[1])} points, found {len(body)}")
    else:
        points = pairs

    return Airfoil(name or default_name, _canonical(points))


def naca4(code: str, n: int = 100, closed_te: bool = True) -> Airfoil:
    """Generate a NACA 4-digit airfoil (unit chord) in Selig order."""
    digits = code.strip().upper().removeprefix("NACA").strip(" -")
    if len(digits) != 4 or not digits.isdigit():
        raise AirfoilError(f"not a NACA 4-digit designation: {code!r}")
    m = int(digits[0]) / 100.0
    p = int(digits[1]) / 10.0
    t = int(digits[2:]) / 100.0
    if t <= 0.0:
        raise AirfoilError(f"NACA {digits}: thickness must be > 0")
    if m > 0.0 and p == 0.0:
        raise AirfoilError(f"NACA {digits}: cambered airfoil needs a camber position")

    a4 = 0.1036 if closed_te else 0.1015
    upper: list[Point] = []
    lower: list[Point] = []
    for x in cosine_spacing(n):
        yt = 5.0 * t * (0.2969 * math.sqrt(x) - 0.1260 * x - 0.3516 * x**2 + 0.2843 * x**3 - a4 * x**4)
        if m == 0.0:
            yc = dyc = 0.0
        elif x < p:
            yc = m / p**2 * (2.0 * p * x - x * x)
            dyc = 2.0 * m / p**2 * (p - x)
        else:
            yc = m / (1.0 - p) ** 2 * ((1.0 - 2.0 * p) + 2.0 * p * x - x * x)
            dyc = 2.0 * m / (1.0 - p) ** 2 * (p - x)
        theta = math.atan(dyc)
        upper.append((x - yt * math.sin(theta), yc + yt * math.cos(theta)))
        lower.append((x + yt * math.sin(theta), yc - yt * math.cos(theta)))

    return Airfoil(f"NACA {digits}", _canonical(list(reversed(upper)) + lower[1:]))


NACA_SPEC = re.compile(r"naca\s*-?\s*(\d{4})", re.IGNORECASE)


def load_airfoil(spec: str, base_dir: str | Path | None = None) -> Airfoil:
    """Load an airfoil from a NACA 4-digit code (``"naca2412"``), a ``.dat`` file path,
    or the id of an airfoil in the built-in database (``"sd7037"``, see ``openhowi airfoils``)."""
    match = NACA_SPEC.fullmatch(spec.strip())
    if match:
        return naca4(match.group(1))
    path = Path(spec).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = Path(base_dir) / path
    if not path.exists():
        from . import airfoildb

        if airfoildb.entry(spec) is not None:
            return airfoildb.get(spec)
        raise AirfoilError(f"{spec!r} is neither an airfoil file nor in the airfoil database (see `openhowi airfoils`)")
    return Airfoil.from_file(path)
