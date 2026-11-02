"""Wing panel geometry: root/tip sections and their projection onto the towers.

Wing coordinates: x points aft (leading edge at x = 0 on the root), y points up,
and sections sit at span positions measured from the XY carriage.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .airfoil import Airfoil
from .geometry import Point, offset_polyline, signed_area


def place_section(
    airfoil: Airfoil,
    chord: float,
    n_per_surface: int,
    x_le: float = 0.0,
    y_le: float = 0.0,
    twist_deg: float = 0.0,
    pivot: float = 0.25,
) -> list[Point]:
    """Scale an airfoil to ``chord``, twist it and move its leading edge to ``(x_le, y_le)``.

    Positive ``twist_deg`` is nose-down (washout); the rotation is about the
    chord-line point at ``pivot`` (fraction of chord from the leading edge).
    """
    base = airfoil.normalized().resampled(n_per_surface).points
    phi = math.radians(twist_deg)
    c, s = math.cos(phi), math.sin(phi)
    px = pivot * chord
    out: list[Point] = []
    for x, y in base:
        dx, dy = x * chord - px, y * chord
        out.append((x_le + px + dx * c - dy * s, y_le + dx * s + dy * c))
    return out


def apply_kerf(contour: Sequence[Point], kerf: float) -> list[Point]:
    """Move the wire path outwards by half the kerf so the part keeps its size."""
    if kerf == 0.0:
        return list(contour)
    half = kerf / 2.0
    # Outside of a counter-clockwise contour is on the right of travel.
    return offset_polyline(contour, -half if signed_area(contour) > 0.0 else half)


def project_to_towers(
    root: Sequence[Point],
    z_root: float,
    tip: Sequence[Point],
    z_tip: float,
    tower_distance: float,
) -> tuple[list[Point], list[Point]]:
    """Extend the straight wire through matching root/tip points to both carriages.

    Returns the paths the XY carriage (span position 0) and the UV carriage
    (span position ``tower_distance``) must follow.
    """
    if len(root) != len(tip):
        raise ValueError("root and tip paths must have the same number of points")
    if z_root == z_tip:
        raise ValueError("root and tip sections must be at different span positions")

    def at(z: float) -> list[Point]:
        t = (z - z_root) / (z_tip - z_root)
        return [(r[0] + (p[0] - r[0]) * t, r[1] + (p[1] - r[1]) * t) for r, p in zip(root, tip)]

    return at(0.0), at(tower_distance)


@dataclass(frozen=True)
class WingPanel:
    root_airfoil: Airfoil
    root_chord: float
    span: float
    tip_airfoil: Airfoil | None = None
    """Defaults to the root airfoil."""
    tip_chord: float | None = None
    """Defaults to the root chord."""
    sweep: float = 0.0
    """How far the tip leading edge sits aft of the root leading edge (mm)."""
    washout: float = 0.0
    """Tip twist relative to the root in degrees, positive nose-down."""
    washout_pivot: float = 0.25
    """Twist axis position as a fraction of the tip chord."""
    tip_offset_y: float = 0.0
    """Height of the tip leading edge above the root leading edge (mm)."""
    root_kerf: float = 0.0
    """Width of the slot the wire melts at the root (mm); measure with a test cut."""
    tip_kerf: float = 0.0
    """Width of the slot at the tip. The slower-moving end usually melts a wider slot."""
    points_per_surface: int = 80

    def __post_init__(self) -> None:
        if self.tip_airfoil is None:
            object.__setattr__(self, "tip_airfoil", self.root_airfoil)
        if self.tip_chord is None:
            object.__setattr__(self, "tip_chord", self.root_chord)
        if self.root_chord <= 0 or self.tip_chord <= 0:
            raise ValueError("chords must be > 0")
        if self.span <= 0:
            raise ValueError("span must be > 0")
        if self.root_kerf < 0 or self.tip_kerf < 0:
            raise ValueError("kerf must be >= 0")
        if not 0.0 <= self.washout_pivot <= 1.0:
            raise ValueError("washout_pivot must be between 0 and 1")
        if self.points_per_surface < 3:
            raise ValueError("points_per_surface must be >= 3")

    def root_contour(self) -> list[Point]:
        """Kerf-compensated wire path in the root plane."""
        path = place_section(self.root_airfoil, self.root_chord, self.points_per_surface)
        return apply_kerf(path, self.root_kerf)

    def tip_contour(self) -> list[Point]:
        """Kerf-compensated wire path in the tip plane (index-matched to the root)."""
        assert self.tip_airfoil is not None and self.tip_chord is not None
        path = place_section(
            self.tip_airfoil,
            self.tip_chord,
            self.points_per_surface,
            x_le=self.sweep,
            y_le=self.tip_offset_y,
            twist_deg=self.washout,
            pivot=self.washout_pivot,
        )
        return apply_kerf(path, self.tip_kerf)
