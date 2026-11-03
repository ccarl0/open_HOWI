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

from .airfoil import Airfoil, AirfoilError, load_airfoil, parse_dat
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
from .wing import WingPanel

PROJECT_VERSION = 1
SLOW_END_WARNING = 0.4

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
    {"key": "spar_width", "label": "Slot width", "unit": "mm", "type": "number", "min": 0, "step": 0.5,
     "group": "Spar slots", "help": "Width of the spar cap. 0: no spar slots."},
    {"key": "spar_depth_top", "label": "Top slot depth", "unit": "mm", "type": "number", "min": 0, "step": 0.1,
     "group": "Spar slots", "help": "Depth below the core surface (under the sheeting). 0: no slot on top."},
    {"key": "spar_depth_bottom", "label": "Bottom slot depth", "unit": "mm", "type": "number", "min": 0, "step": 0.1,
     "group": "Spar slots", "help": "0: no slot underneath."},
    {"key": "spar_position", "label": "Position", "unit": "% chord", "type": "number", "min": 1, "max": 99, "step": 1,
     "group": "Spar slots", "help": "Centre of the slots, measured from the leading edge."},
    {"key": "spar_position_tip", "label": "Tip position", "unit": "% chord", "type": "number", "min": 1, "max": 99,
     "step": 1, "optional": True, "group": "Spar slots", "help": "Blank: same as the root."},
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
        "spar_width": 0.0,
        "spar_depth_top": 0.0,
        "spar_depth_bottom": 0.0,
        "spar_position": 25.0,
        "spar_position_tip": None,
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
    """``"naca2412"`` or ``{"name": ..., "dat": "<file contents>"}`` (embedded, so projects are self-contained)."""
    if isinstance(source, str):
        if not source.strip().lower().startswith("naca"):
            raise SliceError(f"unknown airfoil {source!r}; use a NACA code or load a .dat file")
        return load_airfoil(source)
    if isinstance(source, dict) and isinstance(source.get("dat"), str):
        return parse_dat(source["dat"], default_name=str(source.get("name") or "airfoil"))
    raise SliceError("airfoil must be a NACA code or an embedded .dat file")


@dataclass
class SliceResult:
    machine: Machine
    material: Material
    toolpath: Toolpath
    gcode: str
    data: dict[str, Any] = field(default_factory=dict)
    """JSON-ready summary for the GUI."""


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
    core = {
        key: _number(part, key, 0.0) or 0.0
        for key in ("sheeting_top", "sheeting_bottom", "le_stock", "te_stock", "spar_width", "spar_depth_top",
                    "spar_depth_bottom")
    }  # fmt: skip
    spar_position = _number(part, "spar_position", 25.0)
    spar_position_tip = _number(part, "spar_position_tip")

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
            spar_position=25.0 if spar_position is None else spar_position,
            spar_position_tip=spar_position_tip,
            **core,
        )
        # Kerf from the material, at the speed each end of the core actually moves.
        root_core, tip_core, cutbacks = wing.cores()
        ratio = _speed_ratios(polyline_length(root_core + root_core[:1]), polyline_length(tip_core + tip_core[:1]))
        kerf = _kerfs(part, material, ratio)
        wing = dataclasses.replace(wing, root_kerf=kerf[0], tip_kerf=kerf[1])
    except (TypeError, ValueError) as exc:  # includes CoreError
        raise SliceError(str(exc)) from exc

    layout = layout_wing(wing, machine, placement)
    comments = [
        f"root {root_airfoil.name} {root_chord:g} mm, tip {tip_airfoil.name} {tip_chord:g} mm, span {span:g} mm"
    ]
    warnings: list[str] = []
    stats: dict[str, Any] = {"airfoils": {"root": root_airfoil.name, "tip": tip_airfoil.name}}
    spec = wing.core_spec()
    built_up = spec.sheeted or spec.le_stock > 0 or spec.te_stock > 0 or spec.slot_top or spec.slot_bottom
    if built_up:
        comments.append(
            f"core: sheeting top {spec.sheeting_top:g} / bottom {spec.sheeting_bottom:g} mm, "
            f"LE stock {spec.le_stock:g} mm, TE stock {spec.te_stock:g} mm"
        )
        if spec.slot_top or spec.slot_bottom:
            where = f"{spec.spar_position:g} %"
            if spar_position_tip is not None:
                where += f" (tip {spar_position_tip:g} %)"
            comments.append(
                f"spar slots {spec.spar_width:g} mm wide at {where}: top {spec.spar_depth_top:g} mm, "
                f"bottom {spec.spar_depth_bottom:g} mm deep"
            )
        stats["core"] = {
            "te_cutback": {"root": round(cutbacks["root_te"], 1), "tip": round(cutbacks["tip_te"], 1)},
            "le_cutback": {"root": round(cutbacks["root_le"], 1), "tip": round(cutbacks["tip_le"], 1)},
        }
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
        root_regions=[Region(layout.root_shape)],
        tip_regions=[Region(layout.tip_shape)],
        kerf=kerf,
        ratio=ratio,
        comments=comments,
        warnings=warnings,
        stats=stats,
        skins=[(layout.root_skin, layout.tip_skin)] if built_up else [],
    )


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
        },
        "block": block,
        "path": {"t": times, "xy": xy_pts, "uv": uv_pts, "cut": cut},
    }
    return SliceResult(machine, material, toolpath, gcode, data)
