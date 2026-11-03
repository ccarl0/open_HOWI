# SPDX-License-Identifier: GPL-3.0-or-later
"""Turn part geometry into a sequence of synchronized XY/UV carriage moves."""

from __future__ import annotations

from dataclasses import dataclass

from .geometry import Point
from .machine import Machine
from .wing import WingPanel, place_section, project_to_towers


class PlacementError(ValueError):
    """The part cannot be placed on the machine as requested."""


class MachineLimitError(PlacementError):
    """The toolpath leaves the machine's travel range."""


@dataclass(frozen=True)
class Move:
    xy: Point
    uv: Point
    cutting: bool
    """True when the wire is in foam (cut feed), False for moves through air."""
    feed_scale: float = 1.0
    """Multiplier on the cut or travel feed (used by calibration cuts)."""


@dataclass(frozen=True)
class Toolpath:
    moves: tuple[Move, ...]
    start: tuple[Point, Point] = ((0.0, 0.0), (0.0, 0.0))
    """Carriage positions the job starts from (the machine origin)."""
    foam_span: tuple[float, float] | None = None
    """Span positions (from the XY carriage) of the ends of the foam. While cutting, the
    wire moves at the cut feed at whichever end moves faster. ``None``: limit the carriages."""

    def extents(self) -> dict[str, tuple[float, float, float, float]]:
        """``{"XY": (xmin, ymin, xmax, ymax), "UV": (...)}`` including the start point."""
        result = {}
        for name, idx in (("XY", 0), ("UV", 1)):
            pts = [self.start[idx]] + [m.xy if idx == 0 else m.uv for m in self.moves]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            result[name] = (min(xs), min(ys), max(xs), max(ys))
        return result


@dataclass(frozen=True)
class Placement:
    """Where the wing panel sits in the machine."""

    root_side: str = "xy"
    """Which carriage the root faces: ``"xy"`` or ``"uv"`` (swap to cut the opposite-hand panel)."""
    root_offset: float = 0.0
    """Distance from that carriage to the root face of the panel (mm)."""
    x_margin: float = 20.0
    """Machine X of the rearmost trailing-edge point. The foam's rear face must lie between X=0 and here."""
    y_offset: float = 30.0
    """Machine Y of the root chord line."""

    def __post_init__(self) -> None:
        if self.root_side not in ("xy", "uv"):
            raise ValueError('root_side must be "xy" or "uv"')
        if self.root_offset < 0:
            raise ValueError("root_offset must be >= 0")
        if self.x_margin <= 0:
            raise ValueError("x_margin must be > 0 so the wire can enter from outside the foam")


@dataclass(frozen=True)
class WingLayout:
    """A wing panel placed in machine coordinates (X towards the foam, Y up)."""

    z_root: float
    z_tip: float
    root_shape: list[Point]
    """Finished root section (no kerf), for display."""
    tip_shape: list[Point]
    xy: list[Point]
    """Kerf-compensated path of the XY carriage."""
    uv: list[Point]


def span_positions(span: float, machine: Machine, placement: Placement) -> tuple[float, float]:
    """Positions along the wire (from the XY carriage) of a part's root and tip faces."""
    d = machine.tower_distance
    if placement.root_side == "xy":
        z_root = placement.root_offset
        z_tip = z_root + span
    else:
        z_root = d - placement.root_offset
        z_tip = z_root - span
    if not (0.0 <= z_root <= d and 0.0 <= z_tip <= d):
        raise PlacementError(
            f"a {span:g} mm long part {placement.root_offset:g} mm from the {placement.root_side.upper()} "
            f"carriage does not fit between carriages {d:g} mm apart"
        )
    return z_root, z_tip


def layout_wing(wing: WingPanel, machine: Machine, placement: Placement) -> WingLayout:
    d = machine.tower_distance
    z_root, z_tip = span_positions(wing.span, machine, placement)
    xy, uv = project_to_towers(wing.root_contour(), z_root, wing.tip_contour(), z_tip, d)

    # Mirror X so the trailing edge faces the origin and the wire enters from behind it.
    x_back = max(p[0] for p in xy + uv)

    def to_machine(points: list[Point]) -> list[Point]:
        return [(placement.x_margin + (x_back - x), placement.y_offset + y) for x, y in points]

    assert wing.tip_airfoil is not None and wing.tip_chord is not None
    root_shape = place_section(wing.root_airfoil, wing.root_chord, wing.points_per_surface)
    tip_shape = place_section(
        wing.tip_airfoil,
        wing.tip_chord,
        wing.points_per_surface,
        x_le=wing.sweep,
        y_le=wing.tip_offset_y,
        twist_deg=wing.washout,
        pivot=wing.washout_pivot,
    )
    return WingLayout(z_root, z_tip, to_machine(root_shape), to_machine(tip_shape), to_machine(xy), to_machine(uv))


def check_limits(toolpath: Toolpath, machine: Machine, tol: float = 1e-6) -> None:
    problems = []
    for name, (xmin, ymin, xmax, ymax) in toolpath.extents().items():
        if xmin < -tol or xmax > machine.x_travel + tol:
            problems.append(f"{name} horizontal range {xmin:.1f}..{xmax:.1f} mm exceeds 0..{machine.x_travel:g} mm")
        if ymin < -tol or ymax > machine.y_travel + tol:
            problems.append(f"{name} vertical range {ymin:.1f}..{ymax:.1f} mm exceeds 0..{machine.y_travel:g} mm")
    if problems:
        raise MachineLimitError("; ".join(problems))


def wing_toolpath(layout: WingLayout, machine: Machine) -> Toolpath:
    """Toolpath for one wing core, trailing edge facing the machine origin.

    The wire rises at X=0 (outside the foam) to the trailing-edge height,
    cuts in to the trailing edge, goes over the upper surface to the leading
    edge, back along the lower surface, out the entry slot, and down to the
    origin.
    """
    xy, uv = layout.xy, layout.uv
    moves = [Move((0.0, xy[0][1]), (0.0, uv[0][1]), cutting=False)]
    moves += [Move(a, b, cutting=True) for a, b in zip(xy, uv)]
    moves.append(Move((0.0, xy[-1][1]), (0.0, uv[-1][1]), cutting=True))
    moves.append(Move((0.0, 0.0), (0.0, 0.0), cutting=False))

    span = (min(layout.z_root, layout.z_tip), max(layout.z_root, layout.z_tip))
    toolpath = Toolpath(tuple(moves), foam_span=span)
    check_limits(toolpath, machine)
    return toolpath


def build_wing_toolpath(wing: WingPanel, machine: Machine, placement: Placement) -> Toolpath:
    return wing_toolpath(layout_wing(wing, machine, placement), machine)
