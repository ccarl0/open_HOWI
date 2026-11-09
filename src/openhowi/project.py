# SPDX-License-Identifier: GPL-3.0-or-later
"""Slicer-style project: machine preset + material preset + part + placement.

A project is a plain JSON document so it can be saved, shared and sliced from
either the GUI or the command line (``openhowi slice project.howi.json``).
"""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass, field
from typing import Any

from . import airfoildb
from .airfoil import NACA_SPEC, Airfoil, AirfoilError, load_airfoil, parse_dat
from .core import Channel, CoreSpec, Joiner, ServoBay, Spar
from .drawing import ShapeError
from .gcode import generate_gcode, segments
from .geometry import Point, bounds, polyline_length
from .machine import Machine
from .material import Material
from .presets import BUILTIN, PresetError, machine_from_values, material_from_values
from .shapes import (
    FORMATS,
    Region,
    all_points,
    build_regions,
    join_polylines,
    layout_shapes,
    read_drawing,
    shapes_toolpath,
    transform_regions,
)
from .toolpath import Placement, PlacementError, Toolpath, layout_wing, wing_toolpath
from .wing import BayGeometry, WingPanel

PROJECT_VERSION = 1
SLOW_END_WARNING = 0.4

SPAR_FIELDS: list[dict[str, Any]] = [
    {"key": "position", "label": "Position", "unit": "% chord", "type": "number", "min": 1, "max": 99, "step": 1,
     "help": "Centre of the slots, measured from the leading edge."},
    {"key": "position_tip", "label": "Tip position", "unit": "% chord", "type": "number", "min": 1, "max": 99,
     "step": 1, "optional": True, "help": "Blank: same as the root."},
    {"key": "width", "label": "Width", "unit": "mm", "type": "number", "min": 0.5, "step": 0.5},
    {"key": "depth_top", "label": "Top depth", "unit": "mm", "type": "number", "min": 0, "step": 0.1,
     "help": "Depth below the core surface (under the sheeting). 0: no slot on top."},
    {"key": "depth_bottom", "label": "Bottom depth", "unit": "mm", "type": "number", "min": 0, "step": 0.1,
     "help": "0: no slot underneath."},
]  # fmt: skip

CHANNEL_FIELDS: list[dict[str, Any]] = [
    {"key": "position", "label": "Position", "unit": "% chord", "type": "number", "min": 1, "max": 99, "step": 1,
     "help": "Centre of the channel, measured from the leading edge."},
    {"key": "position_tip", "label": "Tip position", "unit": "% chord", "type": "number", "min": 1, "max": 99,
     "step": 1, "optional": True, "help": "Blank: same as the root."},
    {"key": "diameter", "label": "Diameter", "unit": "mm", "type": "number", "min": 1, "step": 0.5},
    {"key": "offset", "label": "Height offset", "unit": "mm", "type": "number", "step": 0.5,
     "help": "Move the channel up (or down, negative) from the middle of the core."},
    {"key": "side", "label": "Slit from", "type": "select",
     "options": [["bottom", "bottom"], ["top", "top"]],
     "help": "The wire reaches the channel through a straight slit from this surface."},
]  # fmt: skip

JOINER_FIELDS: list[dict[str, Any]] = [
    {"key": "shape", "label": "Joiner", "type": "select",
     "options": [["round", "tube or rod"], ["rect", "blade"]]},
    {"key": "diameter", "label": "Diameter", "unit": "mm", "type": "number", "min": 1, "step": 0.5,
     "show_if": {"shape": "round"}},
    {"key": "width", "label": "Thickness", "unit": "mm", "type": "number", "min": 0.5, "step": 0.5,
     "show_if": {"shape": "rect"}, "help": "Size of the blade along the chord."},
    {"key": "height", "label": "Height", "unit": "mm", "type": "number", "min": 1, "step": 0.5,
     "show_if": {"shape": "rect"}},
    {"key": "position", "label": "Root position", "unit": "% chord", "type": "number", "min": 1, "max": 99,
     "step": 1, "help": "Centre of the hole at the root, measured from the leading edge."},
    {"key": "position_tip", "label": "Tip position", "unit": "% chord", "type": "number", "min": 1, "max": 99,
     "step": 1, "optional": True,
     "help": "Where the hole comes out of the tip. Blank: square to the root face, as most joiners are."},
    {"key": "offset", "label": "Height offset", "unit": "mm", "type": "number", "step": 0.5,
     "help": "Move the hole up (or down, negative) from the middle of the root core."},
    {"key": "dihedral", "label": "Dihedral", "unit": "°", "type": "number", "step": 0.5,
     "help": "Total dihedral at the root joint for a straight joiner: the hole runs down towards the tip by half "
             "of it. 0 for a bent joiner or a flat wing."},
    {"key": "side", "label": "Slit from", "type": "select",
     "options": [["bottom", "bottom"], ["top", "top"]]},
]  # fmt: skip

BAY_FIELDS: list[dict[str, Any]] = [
    {"key": "station", "label": "From root", "unit": "mm", "type": "number", "min": 1,
     "help": "Centre of the bay, measured from the root face."},
    {"key": "position", "label": "Position", "unit": "% chord", "type": "number", "min": 1, "max": 99, "step": 1,
     "help": "Centre of the bay, measured from the leading edge of the local chord."},
    {"key": "length", "label": "Length", "unit": "mm", "type": "number", "min": 1, "step": 0.5,
     "help": "Along the span."},
    {"key": "width", "label": "Width", "unit": "mm", "type": "number", "min": 1, "step": 0.5,
     "help": "Along the chord."},
    {"key": "depth", "label": "Depth", "unit": "mm", "type": "number", "min": 0.5, "step": 0.5,
     "help": "Below the core surface (under the sheeting) at the centre of the bay."},
    {"key": "side", "label": "Surface", "type": "select", "options": [["bottom", "bottom"], ["top", "top"]]},
]  # fmt: skip

PART_FIELDS: list[dict[str, Any]] = [
    {"key": "root_airfoil", "label": "Root airfoil", "type": "airfoil", "group": "Airfoils"},
    {"key": "tip_airfoil", "label": "Tip airfoil", "type": "airfoil", "optional": True, "group": "Airfoils",
     "help": "Leave as “same as root” for a constant section."},
    {"key": "root_chord", "label": "Root chord", "unit": "mm", "type": "number", "min": 1, "group": "Planform"},
    {"key": "tip_chord", "label": "Tip chord", "unit": "mm", "type": "number", "min": 1, "group": "Planform"},
    {"key": "span", "label": "Span", "unit": "mm", "type": "number", "min": 1, "group": "Planform",
     "help": "Length of this panel along the wire."},
    {"key": "sweep", "label": "Sweep", "unit": "mm", "type": "number", "group": "Planform",
     "help": "How far the tip leading edge sits behind the root leading edge."},
    {"key": "washout", "label": "Washout", "unit": "°", "type": "number", "step": 0.1, "group": "Planform",
     "help": "Tip twist, positive nose-down."},
    {"key": "washout_pivot", "label": "Twist axis", "unit": "× chord", "type": "number", "min": 0, "max": 1,
     "step": 0.05, "group": "Planform"},
    {"key": "tip_offset_y", "label": "Tip height offset", "unit": "mm", "type": "number", "group": "Planform",
     "help": "Raise the tip leading edge, e.g. to cut dihedral into a flat-bottomed panel."},
    {"key": "sheeting_top", "label": "Top sheeting", "unit": "mm", "type": "number", "min": 0, "step": 0.1,
     "group": "Core", "help": "Skin glued on top (e.g. 1.5 mm balsa, 0.6 mm obeche): the core is cut this much undersize."},
    {"key": "sheeting_bottom", "label": "Bottom sheeting", "unit": "mm", "type": "number", "min": 0, "step": 0.1,
     "group": "Core"},
    {"key": "le_stock", "label": "LE stock", "unit": "mm", "type": "number", "min": 0, "step": 0.5, "group": "Core",
     "help": "Cut the core back this far from the leading edge to fit a balsa or hardwood leading-edge strip."},
    {"key": "te_stock", "label": "TE stock", "unit": "mm", "type": "number", "min": 0, "step": 0.5, "group": "Core",
     "help": "Cut the core back this far from the trailing edge to fit a trailing-edge strip."},
    {"key": "spars", "label": "Spar slots", "type": "list", "group": "Spars", "item_label": "Spar",
     "add_label": "Add spar", "empty": "No spar slots.", "max": 6, "item_fields": SPAR_FIELDS,
     "default_item": {"position": 25.0, "position_tip": None, "width": 6.0, "depth_top": 1.5, "depth_bottom": 1.5}},
    {"key": "channels", "label": "Servo channels", "type": "list", "group": "Servo channels", "item_label": "Channel",
     "add_label": "Add servo channel", "empty": "No servo channels.", "max": 4, "item_fields": CHANNEL_FIELDS,
     "default_item": {"position": 45.0, "position_tip": None, "diameter": 8.0, "offset": 0.0, "side": "bottom"}},
    {"key": "joiners", "label": "Joiners", "type": "list", "group": "Joiners", "item_label": "Joiner",
     "add_label": "Add joiner hole", "empty": "No joiner holes.", "max": 4, "item_fields": JOINER_FIELDS,
     "help": "A straight hole through the whole panel for a joiner tube, rod or blade, cut through a slit.",
     "default_item": {"shape": "round", "diameter": 10.0, "width": 4.0, "height": 20.0, "position": 30.0,
                      "position_tip": None, "offset": 0.0, "dihedral": 0.0, "side": "bottom"}},
    {"key": "bays", "label": "Servo bays", "type": "list", "group": "Servo bays", "item_label": "Bay",
     "add_label": "Add servo bay", "max": 6, "item_fields": BAY_FIELDS,
     "empty": "No servo bays. The wire cannot cut them: they are checked here and cut by hand from a printed template.",
     "default_item": {"station": 150.0, "position": 40.0, "length": 35.0, "width": 15.0, "depth": 12.0,
                      "side": "bottom"}},
    {"key": "kerf_root_override", "label": "Root kerf override", "unit": "mm", "type": "number", "min": 0,
     "step": 0.05, "optional": True, "group": "Advanced", "help": "Blank: from the material and the cut speed at the root."},
    {"key": "kerf_tip_override", "label": "Tip kerf override", "unit": "mm", "type": "number", "min": 0,
     "step": 0.05, "optional": True, "group": "Advanced"},
    {"key": "points_per_surface", "label": "Points per surface", "type": "number", "min": 10, "max": 400,
     "step": 1, "group": "Advanced", "help": "More points: smoother cut, longer G-code."},
]  # fmt: skip

PLACEMENT_FIELDS: list[dict[str, Any]] = [
    {"key": "root_side", "label": "Root faces", "type": "select",
     "options": [["xy", "XY carriage"], ["uv", "UV carriage"]], "help": "Swap to cut the opposite-hand panel."},
    {"key": "root_offset", "label": "Root distance from carriage", "unit": "mm", "type": "number", "min": 0},
    {"key": "x_margin", "label": "Trailing edge X", "unit": "mm", "type": "number", "min": 1,
     "help": "Machine X of the trailing edge. The foam's rear face goes between X=0 and here."},
    {"key": "y_offset", "label": "Chord line Y", "unit": "mm", "type": "number",
     "help": "Machine Y of the root chord line."},
]  # fmt: skip


SHAPE_FIELDS: list[dict[str, Any]] = [
    {"key": "source", "label": "Drawing", "type": "drawing", "group": "Drawing",
     "help": "An SVG or DXF file with closed outlines. Outlines inside outlines become holes."},
    {"key": "scale", "label": "Scale", "unit": "%", "type": "number", "min": 0.1, "step": 1, "group": "Drawing"},
    {"key": "rotate", "label": "Rotate", "unit": "°", "type": "number", "step": 90, "group": "Drawing"},
    {"key": "mirror", "label": "Mirror", "type": "bool", "group": "Drawing",
     "help": "Flip left-right, e.g. when the drawing is seen from the other side of the machine."},
    {"key": "span", "label": "Length along wire", "unit": "mm", "type": "number", "min": 1, "group": "Extrusion",
     "help": "Thickness of the cut part along the wire."},
    {"key": "tip_scale", "label": "Tip scale", "unit": "%", "type": "number", "min": 1, "step": 1,
     "group": "Extrusion", "help": "Below 100 % the shape tapers towards the tip face, like a tapered fuselage."},
    {"key": "tip_offset_x", "label": "Tip shift X", "unit": "mm", "type": "number", "group": "Extrusion"},
    {"key": "tip_offset_y", "label": "Tip shift Y", "unit": "mm", "type": "number", "group": "Extrusion"},
    {"key": "tolerance", "label": "Curve tolerance", "unit": "mm", "type": "number", "min": 0.005, "step": 0.01,
     "group": "Advanced", "help": "How closely curves are followed. Smaller: smoother, longer G-code."},
    {"key": "gap", "label": "Close gaps up to", "unit": "mm", "type": "number", "min": 0, "step": 0.05,
     "group": "Advanced", "help": "Line ends closer than this are joined into closed outlines."},
    {"key": "kerf_root_override", "label": "Root kerf override", "unit": "mm", "type": "number", "min": 0,
     "step": 0.05, "optional": True, "group": "Advanced", "help": "Blank: from the material."},
    {"key": "kerf_tip_override", "label": "Tip kerf override", "unit": "mm", "type": "number", "min": 0,
     "step": 0.05, "optional": True, "group": "Advanced"},
]  # fmt: skip

SHAPE_PLACEMENT_FIELDS: list[dict[str, Any]] = [
    {"key": "root_side", "label": "Root (100 %) face", "type": "select",
     "options": [["xy", "at the XY carriage"], ["uv", "at the UV carriage"]]},
    {"key": "root_offset", "label": "Root distance from carriage", "unit": "mm", "type": "number", "min": 0},
    {"key": "x_margin", "label": "Rear of the cut X", "unit": "mm", "type": "number", "min": 1,
     "help": "Machine X of the rearmost point of the cut. The foam's rear face goes between X=0 and here."},
    {"key": "y_offset", "label": "Shape bottom Y", "unit": "mm", "type": "number",
     "help": "Machine Y of the bottom of the shape."},
]  # fmt: skip

PART_TYPES = [{"type": "wing", "label": "Wing panel"}, {"type": "shape", "label": "2D shape (DXF/SVG)"}]
PART_SCHEMAS = {"wing": PART_FIELDS, "shape": SHAPE_FIELDS}
PLACEMENT_SCHEMAS = {"wing": PLACEMENT_FIELDS, "shape": SHAPE_PLACEMENT_FIELDS}
DEFAULT_PARTS: dict[str, dict[str, Any]] = {
    "wing": {
        "type": "wing",
        "root_airfoil": "naca2412",
        "tip_airfoil": None,
        "root_chord": 250.0,
        "tip_chord": 180.0,
        "span": 600.0,
        "sweep": 25.0,
        "washout": 1.0,
        "washout_pivot": 0.25,
        "tip_offset_y": 0.0,
        "sheeting_top": 0.0,
        "sheeting_bottom": 0.0,
        "le_stock": 0.0,
        "te_stock": 0.0,
        "spars": [],
        "channels": [],
        "joiners": [],
        "bays": [],
        "kerf_root_override": None,
        "kerf_tip_override": None,
        "points_per_surface": 80,
    },
    "shape": {
        "type": "shape",
        "source": None,
        "scale": 100.0,
        "rotate": 0.0,
        "mirror": False,
        "span": 100.0,
        "tip_scale": 100.0,
        "tip_offset_x": 0.0,
        "tip_offset_y": 0.0,
        "tolerance": 0.05,
        "gap": 0.2,
        "kerf_root_override": None,
        "kerf_tip_override": None,
    },
}
DEFAULT_PLACEMENTS: dict[str, dict[str, Any]] = {
    "wing": {"root_side": "xy", "root_offset": 200.0, "x_margin": 25.0, "y_offset": 40.0},
    "shape": {"root_side": "xy", "root_offset": 300.0, "x_margin": 20.0, "y_offset": 20.0},
}


class SliceError(ValueError):
    pass


def default_project() -> dict[str, Any]:
    machine, material = BUILTIN["machine"][0], BUILTIN["material"][0]
    return {
        "version": PROJECT_VERSION,
        "machine_preset": machine["name"],
        "machine": copy.deepcopy(machine["values"]),
        "material_preset": material["name"],
        "material": copy.deepcopy(material["values"]),
        "part": copy.deepcopy(DEFAULT_PARTS["wing"]),
        "placement": copy.deepcopy(DEFAULT_PLACEMENTS["wing"]),
    }


def resolve_airfoil(source: Any) -> Airfoil:
    """A NACA 4-digit code (``"naca2412"``), a database id (``"sd7037"``) or ``{"name": ..., "dat": "<file
    contents>"}`` (embedded, so projects are self-contained). Never reads files."""
    if isinstance(source, str):
        if NACA_SPEC.fullmatch(source.strip()):
            return load_airfoil(source)
        if airfoildb.entry(source) is None:
            raise SliceError(f"unknown airfoil {source!r}; pick one from the database or load a .dat file")
        return airfoildb.get(source)
    if isinstance(source, dict) and isinstance(source.get("dat"), str):
        return parse_dat(source["dat"], default_name=str(source.get("name") or "airfoil"))
    raise SliceError("airfoil must be a NACA code, a database airfoil or an embedded .dat file")


@dataclass
class SliceResult:
    machine: Machine
    material: Material
    toolpath: Toolpath
    gcode: str
    data: dict[str, Any] = field(default_factory=dict)
    """JSON-ready summary for the GUI."""
    wing: WingPanel | None = None
    """The wing panel, for wing projects."""


@dataclass
class _Part:
    """What a part type hands to the shared output stage."""

    toolpath: Toolpath
    z_root: float
    z_tip: float
    root_regions: list[Region]
    tip_regions: list[Region]
    kerf: tuple[float, float]
    ratio: tuple[float, float]
    comments: list[str]
    warnings: list[str]
    stats: dict[str, Any]
    skins: list[tuple[list[Point], list[Point]]] = field(default_factory=list)
    """Finished outlines (root, tip) to draw around the cores, e.g. the sheeted airfoil."""
    bays: list[tuple[float, float, list[Point], list[Point]]] = field(default_factory=list)
    """Hand-cut pockets to show: span positions of both ends and their sections there."""
    wing: WingPanel | None = None


def _r(v: float) -> float:
    return round(v, 3)


def _pts(points: list[Point]) -> list[list[float]]:
    return [[_r(x), _r(y)] for x, y in points]


def _number(part: dict[str, Any], key: str, default: float | None = None) -> float | None:
    value = part.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SliceError(f"{key} must be a number")
    return float(value)


def _speed_ratios(root_len: float, tip_len: float) -> tuple[float, float]:
    longest = max(root_len, tip_len)
    return root_len / longest, tip_len / longest


def _kerfs(part: dict[str, Any], material: Material, ratio: tuple[float, float]) -> tuple[float, float]:
    root = _number(part, "kerf_root_override")
    tip = _number(part, "kerf_tip_override")
    return (
        material.kerf_at(ratio[0]) if root is None else root,
        material.kerf_at(ratio[1]) if tip is None else tip,
    )


def _placement(project: dict[str, Any]) -> Placement:
    try:
        return Placement(**dict(project.get("placement") or {}))
    except (TypeError, ValueError) as exc:
        raise SliceError(f"placement: {exc}") from exc


def slice_project(project: dict[str, Any]) -> SliceResult:
    try:
        return _slice(project)
    except (PresetError, AirfoilError, PlacementError, ShapeError) as exc:
        raise SliceError(str(exc)) from exc


def _slice(project: dict[str, Any]) -> SliceResult:
    if not isinstance(project, dict):
        raise SliceError("project must be a JSON object")
    part = project.get("part") or {}
    if not isinstance(part, dict):
        raise SliceError("part must be an object")
    kind = part.get("type", "wing")
    if kind not in PART_SCHEMAS:
        raise SliceError(f"unsupported part type {kind!r}")

    material = material_from_values(dict(project.get("material") or {}))
    machine_values = dict(project.get("machine") or {})
    if isinstance(machine_values.get("decimals"), float) and machine_values["decimals"].is_integer():
        machine_values["decimals"] = int(machine_values["decimals"])
    machine = machine_from_values(machine_values, material)
    placement = _placement(project)

    built = (_wing_part if kind == "wing" else _shape_part)(part, machine, material, placement)
    return _finish(project, kind, machine, material, placement, built)


def _wing_part(part: dict[str, Any], machine: Machine, material: Material, placement: Placement) -> _Part:
    root_airfoil = resolve_airfoil(part.get("root_airfoil", "naca2412"))
    tip_source = part.get("tip_airfoil")
    tip_airfoil = resolve_airfoil(tip_source) if tip_source else root_airfoil

    root_chord = _number(part, "root_chord")
    span = _number(part, "span")
    if root_chord is None or span is None:
        raise SliceError("root_chord and span are required")
    tip_chord = _number(part, "tip_chord") or root_chord
    sweep = _number(part, "sweep", 0.0) or 0.0
    washout = _number(part, "washout", 0.0) or 0.0
    pivot = _number(part, "washout_pivot", 0.25)
    pivot = 0.25 if pivot is None else pivot
    tip_offset_y = _number(part, "tip_offset_y", 0.0) or 0.0
    n = int(_number(part, "points_per_surface", 80) or 80)
    core = {key: _number(part, key, 0.0) or 0.0 for key in ("sheeting_top", "sheeting_bottom", "le_stock", "te_stock")}
    spars = _items(legacy_spars(part), Spar, "spar")
    channels = _items(part.get("channels") or [], Channel, "servo channel")
    joiners = _items(part.get("joiners") or [], Joiner, "joiner")
    bays = _items(part.get("bays") or [], ServoBay, "servo bay")

    try:
        wing = WingPanel(
            root_airfoil=root_airfoil,
            root_chord=root_chord,
            span=span,
            tip_airfoil=tip_airfoil,
            tip_chord=tip_chord,
            sweep=sweep,
            washout=washout,
            washout_pivot=pivot,
            tip_offset_y=tip_offset_y,
            points_per_surface=n,
            spars=spars,
            channels=channels,
            joiners=joiners,
            bays=bays,
            **core,
        )
        # Kerf from the material, at the speed each end of the core actually moves.
        root_core, tip_core, cutbacks = wing.cores()
        ratio = _speed_ratios(polyline_length(root_core + root_core[:1]), polyline_length(tip_core + tip_core[:1]))
        kerf = _kerfs(part, material, ratio)
        wing = dataclasses.replace(wing, root_kerf=kerf[0], tip_kerf=kerf[1])
        layout = layout_wing(wing, machine, placement)
        bay_info = wing.bay_geometry()
    except (TypeError, ValueError) as exc:  # includes CoreError
        if isinstance(exc, PlacementError):
            raise
        raise SliceError(str(exc)) from exc

    comments = [
        f"root {root_airfoil.name} {root_chord:g} mm, tip {tip_airfoil.name} {tip_chord:g} mm, span {span:g} mm"
    ]
    warnings: list[str] = []
    stats: dict[str, Any] = {"airfoils": {"root": root_airfoil.name, "tip": tip_airfoil.name}}
    spec = wing.core_spec()
    built_up = not spec.plain or bool(bay_info)
    if built_up:
        comments += core_comments(spec) + bay_comments(bay_info)
        stats["core"] = {
            "te_cutback": {"root": round(cutbacks["root_te"], 1), "tip": round(cutbacks["tip_te"], 1)},
            "le_cutback": {"root": round(cutbacks["root_le"], 1), "tip": round(cutbacks["tip_le"], 1)},
            "spars": sum(1 for sp in spec.spars if sp.depth_top > 0 or sp.depth_bottom > 0),
            "channels": len(spec.channels),
            "joiners": len(spec.joiners),
            "bays": [
                {
                    "number": b.number,
                    "side": b.bay.side,
                    "size": [b.bay.length, b.bay.width, b.bay.depth],
                    "from_root": round(b.z0, 1),
                    "behind_front": round(b.x0 - b.core_le, 1),
                    "channels": list(b.channels),
                }
                for b in bay_info
            ],
        }
        holes = list(spec.channels) + list(spec.joiners)
        if holes:
            sides = sorted({h.side for h in holes})
            kinds = [
                f"{n} {what}"
                for n, what in ((len(spec.channels), "servo channel(s)"), (len(spec.joiners), "joiner hole(s)"))
                if n
            ]
            warnings.append(
                f"{' and '.join(kinds)} are reached through a slit from the {' and '.join(sides)} surface, one kerf wide: "
                "tape or sheet over it."
            )
        if bay_info:
            warnings.append(
                f"{len(bay_info)} servo bay(s) cannot be cut by the wire: print the 1:1 template "
                "(Preview → Servo bay template) and cut them by hand."
            )
        meet = max(cutbacks["root_te"], cutbacks["tip_te"])
        if spec.sheeted and meet > spec.te_stock + 0.5:
            warnings.append(
                f"The core stops {cutbacks['root_te']:.1f} mm (root) / {cutbacks['tip_te']:.1f} mm (tip) short of the "
                "trailing edge, where it gets thinner than the sheeting: glue the skins together there or fit a "
                "trailing-edge strip."
            )
    return _Part(
        toolpath=wing_toolpath(layout, machine),
        z_root=layout.z_root,
        z_tip=layout.z_tip,
        root_regions=[Region(layout.root_shape, layout.root_holes)],
        tip_regions=[Region(layout.tip_shape, layout.tip_holes)],
        kerf=kerf,
        ratio=ratio,
        comments=comments,
        warnings=warnings,
        stats=stats,
        skins=[(layout.root_skin, layout.tip_skin)] if built_up else [],
        bays=layout.bays,
        wing=wing,
    )


def legacy_spars(part: dict[str, Any]) -> list[Any]:
    """Spars as a list, also reading the single-spar fields of earlier project files."""
    spars = part.get("spars")
    if spars:
        return list(spars) if isinstance(spars, list) else spars
    width = part.get("spar_width") or 0
    if width and ((part.get("spar_depth_top") or 0) > 0 or (part.get("spar_depth_bottom") or 0) > 0):
        return [{
            "position": part.get("spar_position", 25.0),
            "position_tip": part.get("spar_position_tip"),
            "width": width,
            "depth_top": part.get("spar_depth_top") or 0.0,
            "depth_bottom": part.get("spar_depth_bottom") or 0.0,
        }]  # fmt: skip
    return []


def _items(raw: Any, cls: type, label: str) -> tuple[Any, ...]:
    if not isinstance(raw, list):
        raise SliceError(f"{label}s must be a list")
    allowed = {f.name for f in dataclasses.fields(cls)}
    out = []
    for number, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise SliceError(f"{label} {number} must be an object")
        values = {k: v for k, v in item.items() if k in allowed}
        for key, value in values.items():
            if key in ("side", "shape"):
                if not isinstance(value, str):
                    raise SliceError(f"{label} {number}: {key} must be text")
            elif value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise SliceError(f"{label} {number}: {key} must be a number")
        try:
            out.append(cls(**values))
        except (TypeError, ValueError) as exc:
            raise SliceError(f"{label} {number}: {exc}") from exc
    return tuple(out)


def core_comments(spec: CoreSpec) -> list[str]:
    """G-code header lines describing a built-up core."""
    lines = [
        (
            f"core: sheeting top {spec.sheeting_top:g} / bottom {spec.sheeting_bottom:g} mm, "
            f"LE stock {spec.le_stock:g} mm, TE stock {spec.te_stock:g} mm"
        )
    ]

    def where(item: Spar | Channel) -> str:
        tip = f" (tip {item.position_tip:g} %)" if item.position_tip is not None else ""
        return f"{item.position:g} %{tip}"

    for number, spar in enumerate(spec.spars, start=1):
        if spar.depth_top > 0 or spar.depth_bottom > 0:
            lines.append(
                f"spar {number}: {spar.width:g} mm wide at {where(spar)}, "
                f"top {spar.depth_top:g} / bottom {spar.depth_bottom:g} mm deep"
            )
    for number, channel in enumerate(spec.channels, start=1):
        lines.append(
            f"servo channel {number}: {channel.diameter:g} mm at {where(channel)}, "
            f"offset {channel.offset:g} mm, slit from the {channel.side}"
        )
    for number, joiner in enumerate(spec.joiners, start=1):
        tip = "square to the root" if joiner.position_tip is None else f"tip {joiner.position_tip:g} %"
        dihedral = f", dihedral {joiner.dihedral:g} deg" if joiner.dihedral else ""
        lines.append(
            f"joiner {number}: {joiner.describe()} at {joiner.position:g} % ({tip}), offset {joiner.offset:g} mm"
            f"{dihedral}, slit from the {joiner.side}"
        )
    return lines


def bay_comments(bays: list[BayGeometry]) -> list[str]:
    """G-code header lines for servo bays, which are cut by hand."""
    return [
        f"servo bay {b.number} (cut by hand): {b.bay.describe()}, {b.z0:.1f} mm from the root, "
        f"front edge {b.x0 - b.core_le:.1f} mm behind the front of the core"
        for b in bays
    ]


def _drawing_source(part: dict[str, Any]) -> tuple[str, str, str]:
    source = part.get("source")
    if not source:
        raise SliceError("load a DXF or SVG drawing for this part")
    if not isinstance(source, dict) or not all(isinstance(source.get(k), str) for k in ("name", "format", "data")):
        raise SliceError("the drawing must be an embedded SVG or DXF file")
    if source["format"] not in FORMATS:
        raise SliceError(f"unsupported drawing format {source['format']!r}")
    return source["name"], source["format"], source["data"]


def drawing_regions(
    fmt: str, data: str, tolerance: float, gap: float, scale: float = 1.0, rotate: float = 0.0, mirror: bool = False
) -> tuple[list[Region], int, list[str]]:
    """Closed shapes from a drawing, transformed and moved to the origin, plus the open-path count and warnings."""
    drawing = read_drawing(fmt, data, tolerance)
    outlines, open_paths = join_polylines(drawing.polylines, gap)
    regions = build_regions(outlines)
    if not regions:
        extra = f" ({open_paths} open path(s) could not be closed; try a larger gap)" if open_paths else ""
        raise SliceError(f"the drawing has no closed outlines{extra}")
    return transform_regions(regions, scale, rotate, mirror), open_paths, list(drawing.warnings)


def inspect_drawing(filename: str, data: str, tolerance: float = 0.05, gap: float = 0.2) -> dict[str, Any]:
    """Summary shown when a drawing is loaded into the app."""
    from .shapes import format_for

    try:
        fmt = format_for(filename)
        regions, open_paths, warnings = drawing_regions(fmt, data, tolerance, gap)
    except ShapeError as exc:
        raise SliceError(str(exc)) from exc
    x0, y0, x1, y1 = bounds(all_points(regions))
    return {
        "name": filename,
        "format": fmt,
        "size": [round(x1 - x0, 2), round(y1 - y0, 2)],
        "shapes": len(regions),
        "holes": sum(len(r.holes) for r in regions),
        "open_paths": open_paths,
        "warnings": warnings,
    }


def _shape_part(part: dict[str, Any], machine: Machine, material: Material, placement: Placement) -> _Part:
    name, fmt, data = _drawing_source(part)
    tolerance = max(0.005, _number(part, "tolerance", 0.05) or 0.05)
    gap = max(0.0, _number(part, "gap", 0.2) or 0.0)
    scale = (_number(part, "scale", 100.0) or 100.0) / 100.0
    rotate = _number(part, "rotate", 0.0) or 0.0
    mirror = part.get("mirror", False)
    if not isinstance(mirror, bool):
        raise SliceError("mirror must be true or false")
    span = _number(part, "span")
    if span is None or span <= 0:
        raise SliceError("span must be > 0")
    tip_scale = (_number(part, "tip_scale", 100.0) or 100.0) / 100.0
    tip_offset = (_number(part, "tip_offset_x", 0.0) or 0.0, _number(part, "tip_offset_y", 0.0) or 0.0)

    regions, open_paths, warnings = drawing_regions(fmt, data, tolerance, gap, scale, rotate, mirror)
    ratio = _speed_ratios(1.0, tip_scale)  # every tip outline is the root outline scaled by tip_scale
    kerf = _kerfs(part, material, ratio)
    layout = layout_shapes(regions, machine, placement, span, tip_scale, tip_offset, kerf[0], kerf[1])
    if open_paths:
        warnings.append(f"{open_paths} open path(s) in the drawing were not cut (only closed outlines are).")
    warnings += layout.warnings

    try:
        toolpath = shapes_toolpath(layout, machine)
    except PlacementError as exc:
        sx, su = layout.carriage_scales
        if abs(sx - 1) < 1e-6 and abs(su - 1) < 1e-6:
            raise
        raise SliceError(
            f"{exc}. With this taper the outline is {sx:.0%} of its root size at the XY carriage and {su:.0%} at "
            "the UV carriage: reduce the taper or move the part closer to the carriage where it grows."
        ) from exc

    x0, y0, x1, y1 = bounds(all_points(regions))
    holes = sum(len(r.holes) for r in layout.root_regions)
    return _Part(
        toolpath=toolpath,
        z_root=layout.z_root,
        z_tip=layout.z_tip,
        root_regions=layout.root_regions,
        tip_regions=layout.tip_regions,
        kerf=kerf,
        ratio=ratio,
        comments=[
            (
                f"drawing {name}: {len(regions)} shape(s), {holes} hole(s), {x1 - x0:.1f} x {y1 - y0:.1f} mm, "
                f"length {span:g} mm, tip scale {tip_scale * 100:g} %"
            )
        ],
        warnings=warnings,
        stats={
            "drawing": {
                "name": name,
                "size": [round(x1 - x0, 2), round(y1 - y0, 2)],
                "shapes": len(regions),
                "holes": holes,
                "open_paths": open_paths,
            }
        },
    )


def _finish(
    project: dict[str, Any], kind: str, machine: Machine, material: Material, placement: Placement, built: _Part
) -> SliceResult:
    toolpath = built.toolpath
    kerf_root, kerf_tip = built.kerf
    ratio_root, ratio_tip = built.ratio
    title = str(project.get("title") or ("Wing panel" if kind == "wing" else "2D shape"))
    gcode = generate_gcode(
        toolpath,
        machine,
        [
            title,
            f"machine: {project.get('machine_preset', 'custom')}; material: {project.get('material_preset', 'custom')}",
            *built.comments,
            f"kerf root {kerf_root:.2f} mm, tip {kerf_tip:.2f} mm, cut speed {material.cut_feed:g} mm/min",
        ],
    )

    # Timeline for the preview slider.
    t, times = 0.0, [0.0]
    xy_pts, uv_pts, cut = [list(toolpath.start[0])], [list(toolpath.start[1])], [False]
    cut_xy = cut_uv = 0.0
    peak = {"XY": 0.0, "UV": 0.0}
    for seg in segments(toolpath, machine):
        t += seg.minutes * 60.0
        times.append(round(t, 3))
        xy_pts.append([_r(seg.xy[0]), _r(seg.xy[1])])
        uv_pts.append([_r(seg.uv[0]), _r(seg.uv[1])])
        cut.append(seg.cutting)
        if seg.cutting:
            cut_xy += seg.d_xy
            cut_uv += seg.d_uv
            peak["XY"] = max(peak["XY"], seg.d_xy / seg.minutes)
            peak["UV"] = max(peak["UV"], seg.d_uv / seg.minutes)

    shown = all_points(built.root_regions) + all_points(built.tip_regions)
    _, sy0, sx1, sy1 = bounds(shown)
    block = {
        "x0": _r(placement.x_margin / 2.0),
        "x1": _r(sx1 + 10.0),
        "y0": _r(max(0.0, sy0 - 10.0)),
        "y1": _r(sy1 + 10.0),
        "z0": _r(min(built.z_root, built.z_tip)),
        "z1": _r(max(built.z_root, built.z_tip)),
    }

    warnings = list(built.warnings)
    slow_end, slow_ratio = ("tip", ratio_tip) if ratio_tip < ratio_root else ("root", ratio_root)
    if slow_ratio < SLOW_END_WARNING:
        warnings.append(
            f"The {slow_end} end moves at only {slow_ratio:.0%} of the cut speed, so it will melt a wide, rough "
            "slot. Expect a poorer finish there, or reduce the taper."
        )
    if machine.wire_power is None:
        warnings.append("Wire power is blank in the material: switch the wire on by hand when the job starts.")

    def region_json(r: Region) -> dict[str, Any]:
        return {"outer": _pts(r.outer), "holes": [_pts(h) for h in r.holes]}

    data = {
        "ok": True,
        "kind": kind,
        "warnings": warnings,
        "stats": {
            "time_s": round(t, 1),
            "cut_length": {"XY": round(cut_xy, 1), "UV": round(cut_uv, 1)},
            "peak_speed": {k: round(v, 1) for k, v in peak.items()},
            "kerf": {"root": round(kerf_root, 3), "tip": round(kerf_tip, 3)},
            "speed_ratio": {"root": round(ratio_root, 3), "tip": round(ratio_tip, 3)},
            "extents": {k: [_r(v) for v in ext] for k, ext in toolpath.extents().items()},
            "gcode_lines": gcode.count("\n"),
            **built.stats,
        },
        "machine": {
            "tower_distance": machine.tower_distance,
            "x_travel": machine.x_travel,
            "y_travel": machine.y_travel,
            "axis_letters": machine.axis_letters,
        },
        "part": {
            "z_root": _r(built.z_root),
            "z_tip": _r(built.z_tip),
            "solids": [
                {"root": region_json(r), "tip": region_json(tp)} for r, tp in zip(built.root_regions, built.tip_regions)
            ],
            "skins": [{"root": _pts(r), "tip": _pts(tp)} for r, tp in built.skins],
            "bays": [
                {
                    "z0": _r(z0),
                    "z1": _r(z1),
                    "root": {"outer": _pts(a), "holes": []},
                    "tip": {"outer": _pts(b), "holes": []},
                }
                for z0, z1, a, b in built.bays
            ],
        },
        "block": block,
        "path": {"t": times, "xy": xy_pts, "uv": uv_pts, "cut": cut},
    }
    return SliceResult(machine, material, toolpath, gcode, data, built.wing)


def bay_template(project: dict[str, Any]) -> tuple[str, str]:
    """File name and SVG of the 1:1 servo bay template for a wing project."""
    from .template import bay_template_svg

    result = slice_project(project)
    if result.wing is None or not result.wing.bays:
        raise SliceError("this part has no servo bays")
    title = str(project.get("title") or "Wing panel")
    return f"{title}-servo-bays.svg", bay_template_svg(result.wing, title)
