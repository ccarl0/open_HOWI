# SPDX-License-Identifier: GPL-3.0-or-later
"""Wing panel geometry: root/tip sections and their projection onto the towers.

Wing coordinates: x points aft (leading edge at x = 0 on the root), y points up,
and sections sit at span positions measured from the XY carriage.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from .airfoil import Airfoil
from .core import CoreSpec, core_runs, materialize, run_counts
from .geometry import Point, offset_polygon, signed_area


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
    return position_section([(x * chord, y * chord) for x, y in base], chord, x_le, y_le, twist_deg, pivot)


def position_section(
    points: Sequence[Point], chord: float, x_le: float, y_le: float, twist_deg: float, pivot: float
) -> list[Point]:
    """Twist a section given in its own frame (leading edge at the origin) and move it into place."""
    phi = math.radians(twist_deg)
    c, s = math.cos(phi), math.sin(phi)
    px = pivot * chord
    return [(x_le + px + (x - px) * c - y * s, y_le + (x - px) * s + y * c) for x, y in points]


def apply_kerf(contour: Sequence[Point], kerf: float) -> list[Point]:
    """Move the wire path outwards by half the kerf so the part keeps its size.

    The contour is treated as closed: the edge from its last point back to its
    first is the trailing-edge face (zero length on a sharp trailing edge).
    """
    if kerf == 0.0:
        return list(contour)
    half = kerf / 2.0
    # Outside of a counter-clockwise contour is on the right of travel.
    return offset_polygon(contour, -half if signed_area(contour) > 0.0 else half)


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
    sheeting_top: float = 0.0
    """Thickness of the top skin (balsa, obeche, ...) the core is undersized for (mm)."""
    sheeting_bottom: float = 0.0
    le_stock: float = 0.0
    """Cut the core back this far from the leading edge for a LE strip (mm along the chord)."""
    te_stock: float = 0.0
    """Cut the core back this far from the trailing edge for a TE strip (mm)."""
    spar_position: float = 25.0
    """Centre of the spar slots at the root, % of chord from the leading edge."""
    spar_position_tip: float | None = None
    """Spar position at the tip (% of chord); defaults to the root position."""
    spar_width: float = 0.0
    spar_depth_top: float = 0.0
    """Depth of the slot cut into the top surface (mm); 0 = no slot."""
    spar_depth_bottom: float = 0.0

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
        if self.spar_position_tip is not None and not 0 < self.spar_position_tip < 100:
            raise ValueError("spar_position_tip must be between 0 and 100 % of the chord")
        self.core_spec()  # validates the core options

    def core_spec(self) -> CoreSpec:
        return CoreSpec(
            sheeting_top=self.sheeting_top,
            sheeting_bottom=self.sheeting_bottom,
            le_stock=self.le_stock,
            te_stock=self.te_stock,
            spar_position=self.spar_position,
            spar_width=self.spar_width,
            spar_depth_top=self.spar_depth_top,
            spar_depth_bottom=self.spar_depth_bottom,
        )

    def cores(self) -> tuple[list[Point], list[Point], dict[str, float]]:
        """Finished-size foam core at the root and tip (no kerf), index-matched, in place.

        Also returns how far each core is cut back from the airfoil's leading and trailing edges.
        """
        assert self.tip_airfoil is not None and self.tip_chord is not None
        spec = self.core_spec()
        root = core_runs(self.root_airfoil, self.root_chord, spec)
        tip = core_runs(self.tip_airfoil, self.tip_chord, spec, self.spar_position_tip)
        counts = run_counts(root, self.points_per_surface)
        root_pts = position_section(materialize(root, counts), self.root_chord, 0.0, 0.0, 0.0, self.washout_pivot)
        tip_pts = position_section(
            materialize(tip, counts), self.tip_chord, self.sweep, self.tip_offset_y, self.washout, self.washout_pivot
        )
        cutbacks = {
            "root_te": root.te_cutback,
            "tip_te": tip.te_cutback,
            "root_le": root.le_cutback,
            "tip_le": tip.le_cutback,
        }
        return root_pts, tip_pts, cutbacks

    def skins(self) -> tuple[list[Point], list[Point]]:
        """Outline of the finished airfoil at the root and tip (for display)."""
        assert self.tip_airfoil is not None and self.tip_chord is not None
        root = place_section(self.root_airfoil, self.root_chord, self.points_per_surface, pivot=self.washout_pivot)
        tip = place_section(
            self.tip_airfoil,
            self.tip_chord,
            self.points_per_surface,
            x_le=self.sweep,
            y_le=self.tip_offset_y,
            twist_deg=self.washout,
            pivot=self.washout_pivot,
        )
        return root, tip

    def root_contour(self) -> list[Point]:
        """Kerf-compensated wire path in the root plane."""
        return apply_kerf(self.cores()[0], self.root_kerf)

    def tip_contour(self) -> list[Point]:
        """Kerf-compensated wire path in the tip plane (index-matched to the root)."""
        return apply_kerf(self.cores()[1], self.tip_kerf)
