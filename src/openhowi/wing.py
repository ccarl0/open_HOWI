# SPDX-License-Identifier: GPL-3.0-or-later
"""Wing panel geometry: root/tip sections and their projection onto the towers.

Wing coordinates: x points aft (leading edge at x = 0 on the root), y points up,
and sections sit at span positions measured from the XY carriage.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from .airfoil import Airfoil
from .core import (
    MIN_WEB,
    Channel,
    ControlSurface,
    CoreError,
    CoreSpec,
    HingeCut,
    Hole,
    HoleShape,
    Joiner,
    ServoBay,
    Spar,
    core_middle,
    core_runs,
    materialize,
    run_counts,
)
from .geometry import Point, offset_polygon, outline_gap, point_in_polygon, signed_area


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


def unposition_section(
    points: Sequence[Point], chord: float, x_le: float, y_le: float, twist_deg: float, pivot: float
) -> list[Point]:
    """The inverse of ``position_section``: from wing coordinates back to the section's own frame."""
    phi = math.radians(twist_deg)
    c, s = math.cos(phi), math.sin(phi)
    px = pivot * chord
    out = []
    for x, y in points:
        dx, dy = x - x_le - px, y - y_le
        out.append((px + dx * c + dy * s, -dx * s + dy * c))
    return out


def axis_basis(direction: tuple[float, float]) -> tuple[Point, Point]:
    """Section of a hole running along ``(dx/dz, dy/dz)``: where its across and up directions land in a face.

    The hole's own frame keeps "across" horizontal; both directions are projected
    along the axis onto the face, which stretches the section of a slanted hole.
    """
    ax, ay = direction
    norm = math.sqrt(ax * ax + ay * ay + 1.0)
    d = (ax / norm, ay / norm, 1.0 / norm)
    h = math.hypot(d[0], d[2])
    e1 = (d[2] / h, 0.0, -d[0] / h)  # horizontal, square to the axis
    e2 = (d[1] * e1[2] - d[2] * e1[1], d[2] * e1[0] - d[0] * e1[2], d[0] * e1[1] - d[1] * e1[0])  # d x e1

    def project(v: tuple[float, float, float]) -> Point:
        k = v[2] / d[2]
        return (v[0] - k * d[0], v[1] - k * d[1])

    return project(e1), project(e2)


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
    spars: tuple[Spar, ...] = ()
    """Spar-cap slots (any number, top and/or bottom)."""
    channels: tuple[Channel, ...] = ()
    """Round servo-lead channels, each reached through a slit from one surface."""
    joiners: tuple[Joiner, ...] = ()
    """Straight holes through the panel for joiner tubes or blades."""
    bays: tuple[ServoBay, ...] = ()
    """Servo bays: checked and drawn, but cut by hand (the wire can only cut through the whole span)."""
    control: ControlSurface | None = None
    """A control surface (aileron, flap, elevator) cut off along a hinge line in the same cut."""

    def __post_init__(self) -> None:
        if self.tip_airfoil is None:
            object.__setattr__(self, "tip_airfoil", self.root_airfoil)
        if self.tip_chord is None:
            object.__setattr__(self, "tip_chord", self.root_chord)
        for name in ("spars", "channels", "joiners", "bays"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
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
        self.core_spec()  # validates the core options

    def core_spec(self) -> CoreSpec:
        return CoreSpec(
            sheeting_top=self.sheeting_top,
            sheeting_bottom=self.sheeting_bottom,
            le_stock=self.le_stock,
            te_stock=self.te_stock,
            spars=self.spars,
            channels=self.channels,
            joiners=self.joiners,
            control=self.control,
        )

    def tip_point(self, fraction: float) -> Point:
        """Wing coordinates of the point ``fraction`` of the tip chord behind its leading edge."""
        assert self.tip_chord is not None
        chord = self.tip_chord
        return position_section([(fraction * chord, 0.0)], chord, self.sweep, self.tip_offset_y, self.washout,
                                self.washout_pivot)[0]  # fmt: skip

    def joiner_axes(self) -> list[tuple[Point, Point]]:
        """Centre of each joiner hole at the root and at the tip, in wing coordinates."""
        spec = self.core_spec()
        axes = []
        for joiner in self.joiners:
            x0 = joiner.position / 100.0 * self.root_chord
            y0 = core_middle(self.root_airfoil, self.root_chord, spec, x0) + joiner.offset
            x1 = x0 if joiner.position_tip is None else self.tip_point(joiner.position_tip / 100.0)[0]
            y1 = y0 - self.span * math.tan(math.radians(joiner.dihedral / 2))
            axes.append(((x0, y0), (x1, y1)))
        return axes

    def joiner_shapes(self) -> list[HoleShape]:
        """Each joiner's section in a face (wing coordinates; the same at root and tip)."""
        shapes = []
        for joiner, ((x0, y0), (x1, y1)) in zip(self.joiners, self.joiner_axes()):
            basis = axis_basis(((x1 - x0) / self.span, (y1 - y0) / self.span))
            shapes.append(HoleShape(joiner.shape, *joiner.size, joiner.side, basis))
        return shapes

    def _joiner_holes(self) -> tuple[list[Hole], list[Hole]]:
        assert self.tip_chord is not None
        root: list[Hole] = []
        tip: list[Hole] = []
        tip_frame = (self.tip_chord, self.sweep, self.tip_offset_y, self.washout, self.washout_pivot)
        for number, (joiner, (c0, c1), shape) in enumerate(
            zip(self.joiners, self.joiner_axes(), self.joiner_shapes()), start=1
        ):
            name = f"joiner {number} ({joiner.describe()})"
            root.append(Hole(f"{name} at the root", f"joiner{number}", shape, c0[0], c0[1]))
            (xt, yt) = unposition_section([c1], *tip_frame)[0]
            tip.append(Hole(f"{name} at the tip", f"joiner{number}", shape.rotated(-self.washout), xt, yt))
        return root, tip

    def sections(self) -> WingSections:
        """Finished-size foam cores at the root and tip (no kerf), index-matched, in place."""
        assert self.tip_airfoil is not None and self.tip_chord is not None
        spec = self.core_spec()
        root_holes, tip_holes = self._joiner_holes()
        root = core_runs(self.root_airfoil, self.root_chord, spec, holes=root_holes)
        try:
            tip = core_runs(self.tip_airfoil, self.tip_chord, spec, tip=True, holes=tip_holes)
        except CoreError as exc:
            if "joiner" not in str(exc) or "at the tip" not in str(exc):
                raise
            raise CoreError(
                f"{exc}. A joiner hole runs straight through the whole panel: set where it comes out of the tip "
                "(tip position), move it, or cut the joiner hole in a shorter root panel."
            ) from exc
        _check_order(root.order, tip.order)
        counts = run_counts(root, self.points_per_surface)
        root_pts, root_tags = materialize(root, counts)
        tip_pts, tip_tags = materialize(tip, counts)
        placements = (
            (self.root_chord, 0.0, 0.0, 0.0),
            (self.tip_chord, self.sweep, self.tip_offset_y, self.washout),
        )
        (rc, rx, ry, rt), (tc, tx, ty, tt) = placements

        def place(points: list[Point], chord: float, x: float, y: float, twist: float) -> list[Point]:
            return position_section(points, chord, x, y, twist, self.washout_pivot)

        root_holes_placed = [
            (root_tags[h.tag], place([h.center], rc, rx, ry, rt)[0], h.shape.rotated(rt)) for h in root.holes
        ]
        tip_holes_placed = [
            (tip_tags[h.tag], place([h.center], tc, tx, ty, tt)[0], h.shape.rotated(tt)) for h in tip.holes
        ]
        root_placed, tip_placed = place(root_pts, rc, rx, ry, rt), place(tip_pts, tc, tx, ty, tt)
        hinges: list[tuple[int, HingeCut] | None] = [None, None]
        parts = [[root_placed], [tip_placed]]
        for k, (runs, tags, pts, frame) in enumerate(
            ((root, root_tags, root_placed, (rc, rx, ry, rt)), (tip, tip_tags, tip_placed, (tc, tx, ty, tt)))
        ):
            if runs.hinge is None:
                continue
            hinge = runs.hinge.moved(lambda q, frame=frame: place(q, *frame))
            hinges[k] = (tags["hinge"], hinge)
            main_top, main_bottom, front_top, _front_bottom = (tags[t] for t in hinge.tags)
            surface = pts[tags[hinge.tags[3]] :] + pts[: front_top + 1] + ([hinge.h] if hinge.kind == "center" else [])
            parts[k] = [pts[main_top : main_bottom + 1], surface]
        return WingSections(
            root=root_placed,
            tip=tip_placed,
            cutbacks={
                "root_te": root.te_cutback,
                "tip_te": tip.te_cutback,
                "root_le": root.le_cutback,
                "tip_le": tip.le_cutback,
            },
            root_holes=root_holes_placed,
            tip_holes=tip_holes_placed,
            root_hinge=hinges[0],
            tip_hinge=hinges[1],
            root_parts=parts[0],
            tip_parts=parts[1],
        )

    def plain_cores(self) -> tuple[list[Point], list[Point]]:
        """Root and tip cores without slots or holes (sheeting and stock only), index-matched, in place."""
        assert self.tip_airfoil is not None and self.tip_chord is not None
        spec = CoreSpec(self.sheeting_top, self.sheeting_bottom, self.le_stock, self.te_stock)
        root = core_runs(self.root_airfoil, self.root_chord, spec)
        tip = core_runs(self.tip_airfoil, self.tip_chord, spec, tip=True)
        counts = run_counts(root, self.points_per_surface)
        return (
            position_section(materialize(root, counts)[0], self.root_chord, 0.0, 0.0, 0.0, self.washout_pivot),
            position_section(
                materialize(tip, counts)[0], self.tip_chord, self.sweep, self.tip_offset_y, self.washout,
                self.washout_pivot,
            ),
        )  # fmt: skip

    def bay_geometry(self) -> list[BayGeometry]:
        """Where each servo bay sits, checked against the core, the spar slots, joiners and other bays."""
        if not self.bays:
            return []
        assert self.tip_chord is not None
        root_core, tip_core = self.plain_cores()
        sections = self.sections()
        n_channels = len(self.channels)
        tip_le, tip_te = self.tip_point(0.0), self.tip_point(1.0)
        joiners = list(zip(self.joiner_axes(), self.joiner_shapes()))

        def lerp(a: Point, b: Point, t: float) -> Point:
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)

        def face(z: float) -> list[Point]:
            t = z / self.span
            return [lerp(a, b, t) for a, b in zip(root_core, tip_core)]

        out: list[BayGeometry] = []
        for number, bay in enumerate(self.bays, start=1):
            name = f"servo bay {number}"
            z0, z1 = bay.station - bay.length / 2, bay.station + bay.length / 2
            if z0 < 0 or z1 > self.span:
                raise CoreError(f"{name} runs past the {'root' if z0 < 0 else 'tip'} of the {self.span:g} mm panel")
            t = bay.station / self.span
            le, te = lerp((0.0, 0.0), tip_le, t), lerp((self.root_chord, 0.0), tip_te, t)
            chord = te[0] - le[0]
            xc = le[0] + bay.position / 100.0 * chord
            x0, x1 = xc - bay.width / 2, xc + bay.width / 2
            top = bay.side == "top"
            floor = _surface(face(bay.station), xc, top) + (-bay.depth if top else bay.depth)
            for z in (z0, bay.station, z1):
                ring = face(z)
                xs = [p[0] for p in ring]
                if x0 <= min(xs) + MIN_WEB or x1 >= max(xs) - MIN_WEB:
                    raise CoreError(f"{name} does not fit in the core {z:.0f} mm from the root")
                for k in range(9):
                    x = x0 + (x1 - x0) * k / 8
                    web = floor - _surface(ring, x, False) if top else _surface(ring, x, True) - floor
                    if web < MIN_WEB:
                        raise CoreError(
                            f"{name} leaves less than {MIN_WEB:g} mm of foam {z:.0f} mm from the root; make it shallower"
                        )
            if self.control is not None:
                xh = self.root_chord * self.control.position / 100.0
                xh += (self.tip_point(self.control.at(True) / 100.0)[0] - xh) * t
                if x1 + MIN_WEB > xh:
                    raise CoreError(f"{name} runs into the control surface: move it forward of the hinge line")
            pockets = [_pocket(face(z), x0, x1, floor, top) for z in (z0, bay.station, z1)]
            for k, spar in enumerate(self.spars, start=1):
                xs = self.root_chord * spar.position / 100.0
                xs += (self.tip_point(spar.at(True) / 100.0)[0] - xs) * t
                if x1 + MIN_WEB <= xs - spar.width / 2 or x0 - MIN_WEB >= xs + spar.width / 2:
                    continue
                same, other = (spar.depth_top, spar.depth_bottom) if top else (spar.depth_bottom, spar.depth_top)
                if same > 0:
                    raise CoreError(f"{name} cuts into the slot of spar {k}")
                if other > 0:
                    ring = face(bay.station)
                    slot_floor = _surface(ring, xs, not top) + (other if top else -other)
                    if abs(floor - slot_floor) < MIN_WEB:
                        raise CoreError(f"{name} leaves less than {MIN_WEB:g} mm of foam over the slot of spar {k}")
            for k, ((c0, c1), shape) in enumerate(joiners, start=1):
                for z, pocket in zip((z0, bay.station, z1), pockets):
                    hole = shape.ring(lerp(c0, c1, z / self.span))[:-1]
                    if outline_gap(pocket, hole) < MIN_WEB:
                        raise CoreError(f"{name} cuts into joiner {k}")
            reaches = []
            for k in range(n_channels):
                (_, cr, shape_r), (_, ct, _) = sections.root_holes[k], sections.tip_holes[k]
                hole = shape_r.ring(lerp(cr, ct, t))[:-1]
                if outline_gap(pockets[1], hole) == 0.0:
                    reaches.append(k + 1)
            for other in out:
                if (
                    x0 >= other.x1 + MIN_WEB
                    or x1 <= other.x0 - MIN_WEB
                    or z0 >= other.z1 + MIN_WEB
                    or z1 <= other.z0 - MIN_WEB
                ):
                    continue
                if other.bay.side == bay.side:
                    raise CoreError(f"{name} overlaps servo bay {other.number}")
                lower, upper = (other.floor, floor) if top else (floor, other.floor)
                if upper - lower < MIN_WEB:
                    raise CoreError(
                        f"{name} and servo bay {other.number} leave less than {MIN_WEB:g} mm of foam between them"
                    )
            core_le = lerp(
                (sections.cutbacks["root_le"], 0.0), self.tip_point(sections.cutbacks["tip_le"] / self.tip_chord), t
            )
            out.append(
                BayGeometry(number, bay, z0, z1, x0, x1, floor, le[0], core_le[0], chord, (pockets[0], pockets[2]),
                            tuple(reaches))
            )  # fmt: skip
        return out

    def cores(self) -> tuple[list[Point], list[Point], dict[str, float]]:
        """Root and tip cores plus how far they are cut back from the airfoil's edges."""
        sections = self.sections()
        return sections.root, sections.tip, sections.cutbacks

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

    def contours(self) -> tuple[list[Point], list[Point]]:
        """Kerf-compensated wire paths in the root and tip planes, index-matched."""
        sections = self.sections()
        return (
            cut_path(sections.root, sections.root_holes, self.root_kerf, sections.root_hinge),
            cut_path(sections.tip, sections.tip_holes, self.tip_kerf, sections.tip_hinge),
        )

    def root_contour(self) -> list[Point]:
        """Kerf-compensated wire path in the root plane."""
        return self.contours()[0]

    def tip_contour(self) -> list[Point]:
        """Kerf-compensated wire path in the tip plane (index-matched to the root)."""
        return self.contours()[1]


def _check_order(root: tuple[tuple[str, ...], ...], tip: tuple[tuple[str, ...], ...]) -> None:
    """Slots and slits must come in the same order at both ends, or the wire would cross itself."""
    for surface, (a, b) in enumerate(zip(root, tip)):
        if a == b:
            continue
        for i, first in enumerate(a):
            for second in a[i + 1 :]:
                if second in b and first in b and b.index(second) < b.index(first):
                    raise CoreError(
                        f"{first} and {second} swap places on the {'top' if surface == 0 else 'bottom'} surface between "
                        "root and tip, so their cuts would cross inside the panel: move one of them or change its "
                        "tip position"
                    )
        raise CoreError("the root and tip have different slots or slits")  # pragma: no cover


def _surface(ring: Sequence[Point], x: float, top: bool) -> float:
    """Height of the top (or bottom) of a section outline at ``x``."""
    ys = []
    for (x1, y1), (x2, y2) in zip(ring, list(ring[1:]) + [ring[0]]):
        if (x1 <= x <= x2 or x2 <= x <= x1) and x1 != x2:
            ys.append(y1 + (y2 - y1) * (x - x1) / (x2 - x1))
    if not ys:
        raise CoreError(f"no core at x = {x:.1f} mm")
    return max(ys) if top else min(ys)


def _pocket(ring: Sequence[Point], x0: float, x1: float, floor: float, top: bool) -> list[Point]:
    """A bay's section in one face: from its floor out to just past the surface."""
    if top:
        return [(x0, floor), (x1, floor), (x1, _surface(ring, x1, True) + 0.5), (x0, _surface(ring, x0, True) + 0.5)]
    return [(x0, _surface(ring, x0, False) - 0.5), (x1, _surface(ring, x1, False) - 0.5), (x1, floor), (x0, floor)]


@dataclass(frozen=True)
class BayGeometry:
    number: int
    bay: ServoBay
    z0: float
    """Inboard end, mm from the root face."""
    z1: float
    x0: float
    """Front and back of the bay along the chord (wing coordinates)."""
    x1: float
    floor: float
    """Height of the flat floor."""
    le: float
    """x of the leading edge at the bay's station."""
    core_le: float
    """x of the front of the core there (behind the leading edge when cut back for LE stock)."""
    chord: float
    faces: tuple[list[Point], list[Point]]
    """The pocket's section at its inboard and outboard ends, for display."""
    channels: tuple[int, ...] = ()
    """Servo channels (numbered from 1) that run into the bay."""


@dataclass
class WingSections:
    root: list[Point]
    tip: list[Point]
    cutbacks: dict[str, float]
    root_holes: list[tuple[int, Point, HoleShape]]
    """(index of the slit's start in ``root``, centre, section) for each servo channel and joiner."""
    tip_holes: list[tuple[int, Point, HoleShape]]
    root_hinge: tuple[int, HingeCut] | None = None
    """(index of the hinge line's point on the outline, the cut) when a control surface comes off."""
    tip_hinge: tuple[int, HingeCut] | None = None
    root_parts: list[list[Point]] = field(default_factory=list)
    """The finished pieces at the root: the core, or the main panel and the control surface."""
    tip_parts: list[list[Point]] = field(default_factory=list)

    def outlines(self, tip: bool) -> list[list[Point]]:
        """The finished holes at the root or tip, for display."""
        return [shape.outline(center) for _, center, shape in (self.tip_holes if tip else self.root_holes)]

    def pieces(self, tip: bool) -> list[tuple[list[Point], list[list[Point]]]]:
        """Each finished piece at the root or tip with the holes inside it, for display."""
        parts = (self.tip_parts if tip else self.root_parts) or [self.tip if tip else self.root]
        holes = self.outlines(tip)
        out: list[tuple[list[Point], list[list[Point]]]] = [(ring, []) for ring in parts]
        for hole in holes:
            k = next((i for i, ring in enumerate(parts) if point_in_polygon(hole[0], ring)), 0)
            out[k][1].append(hole)
        return out


def cut_path(
    core: Sequence[Point],
    holes: Sequence[tuple[int, Point, HoleShape]],
    kerf: float,
    hinge: tuple[int, HingeCut] | None = None,
) -> list[Point]:
    """Kerf-compensated outline with a detour round every hole (in along its slit, round it and back
    out) and down the hinge line of a control surface."""
    ring = apply_kerf(core, kerf)
    detours: dict[int, list[list[Point]]] = {}
    for index, center, shape in holes:
        detours.setdefault(index, []).append(shape.ring(center, inset=kerf / 2))
    if hinge is not None:
        detours.setdefault(hinge[0], []).append(hinge[1].detour(kerf))
    out: list[Point] = []
    for i, p in enumerate(ring):
        out.append(p)
        for loop in detours.get(i, []):
            out += loop + [p]
    return out
