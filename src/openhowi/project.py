# SPDX-License-Identifier: GPL-3.0-or-later
"""Slicer-style project: machine preset + material preset + part + placement.

A project is a plain JSON document so it can be saved, shared and sliced from
either the GUI or the command line (``openhowi slice project.howi.json``).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .airfoil import Airfoil, AirfoilError, load_airfoil, parse_dat
from .gcode import generate_gcode, segments
from .geometry import polyline_length
from .machine import Machine
from .material import Material
from .presets import BUILTIN, PresetError, machine_from_values, material_from_values
from .toolpath import Placement, PlacementError, Toolpath, layout_wing, wing_toolpath
from .wing import WingPanel, place_section

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
        "part": {
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
            "kerf_root_override": None,
            "kerf_tip_override": None,
            "points_per_surface": 80,
        },
        "placement": {"root_side": "xy", "root_offset": 200.0, "x_margin": 25.0, "y_offset": 40.0},
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
    wing: WingPanel
    toolpath: Toolpath
    gcode: str
    data: dict[str, Any] = field(default_factory=dict)
    """JSON-ready summary for the GUI."""


def _r(v: float) -> float:
    return round(v, 3)


def _pts(points: list[tuple[float, float]]) -> list[list[float]]:
    return [[_r(x), _r(y)] for x, y in points]


def _number(part: dict[str, Any], key: str, default: float | None = None) -> float | None:
    value = part.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SliceError(f"{key} must be a number")
    return float(value)


def slice_project(project: dict[str, Any]) -> SliceResult:
    try:
        return _slice(project)
    except (PresetError, AirfoilError, PlacementError) as exc:
        raise SliceError(str(exc)) from exc


def _slice(project: dict[str, Any]) -> SliceResult:
    if not isinstance(project, dict):
        raise SliceError("project must be a JSON object")
    part = project.get("part") or {}
    if part.get("type", "wing") != "wing":
        raise SliceError(f"unsupported part type {part.get('type')!r}")

    material = material_from_values(dict(project.get("material") or {}))
    machine_values = dict(project.get("machine") or {})
    if isinstance(machine_values.get("decimals"), float) and machine_values["decimals"].is_integer():
        machine_values["decimals"] = int(machine_values["decimals"])
    machine = machine_from_values(machine_values, material)

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
    tip_offset_y = _number(part, "tip_offset_y", 0.0) or 0.0
    n = int(_number(part, "points_per_surface", 80) or 80)

    # Kerf from the material, at the speed each end of the part actually moves.
    try:
        root_len = polyline_length(place_section(root_airfoil, root_chord, n))
        tip_len = polyline_length(
            place_section(
                tip_airfoil,
                tip_chord,
                n,
                x_le=sweep,
                y_le=tip_offset_y,
                twist_deg=washout,
                pivot=0.25 if pivot is None else pivot,
            )
        )
    except (AirfoilError, ValueError) as exc:
        raise SliceError(str(exc)) from exc
    longest = max(root_len, tip_len)
    ratio_root, ratio_tip = root_len / longest, tip_len / longest
    kerf_root = _number(part, "kerf_root_override")
    kerf_tip = _number(part, "kerf_tip_override")
    kerf_root = material.kerf_at(ratio_root) if kerf_root is None else kerf_root
    kerf_tip = material.kerf_at(ratio_tip) if kerf_tip is None else kerf_tip

    try:
        wing = WingPanel(
            root_airfoil=root_airfoil,
            root_chord=root_chord,
            span=span,
            tip_airfoil=tip_airfoil,
            tip_chord=tip_chord,
            sweep=sweep,
            washout=washout,
            washout_pivot=0.25 if pivot is None else pivot,
            tip_offset_y=tip_offset_y,
            root_kerf=kerf_root,
            tip_kerf=kerf_tip,
            points_per_surface=n,
        )
        placement_values = dict(project.get("placement") or {})
        placement = Placement(**placement_values)
    except (TypeError, ValueError) as exc:
        raise SliceError(str(exc)) from exc

    layout = layout_wing(wing, machine, placement)
    toolpath = wing_toolpath(layout, machine)

    title = str(project.get("title") or "Wing panel")
    gcode = generate_gcode(
        toolpath,
        machine,
        [
            title,
            f"machine: {project.get('machine_preset', 'custom')}; material: {project.get('material_preset', 'custom')}",
            f"root {root_airfoil.name} {root_chord:g} mm, tip {tip_airfoil.name} {tip_chord:g} mm, span {span:g} mm",
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

    shapes = layout.root_shape + layout.tip_shape
    block = {
        "x0": _r(placement.x_margin / 2.0),
        "x1": _r(max(x for x, _ in shapes) + 10.0),
        "y0": _r(max(0.0, min(y for _, y in shapes) - 10.0)),
        "y1": _r(max(y for _, y in shapes) + 10.0),
        "z0": _r(min(layout.z_root, layout.z_tip)),
        "z1": _r(max(layout.z_root, layout.z_tip)),
    }

    warnings = []
    slow_end, slow_ratio = ("tip", ratio_tip) if ratio_tip < ratio_root else ("root", ratio_root)
    if slow_ratio < SLOW_END_WARNING:
        warnings.append(
            f"The {slow_end} end moves at only {slow_ratio:.0%} of the cut speed, so it will melt a wide, rough "
            "slot. Expect a poorer finish there, or split the panel into less tapered pieces."
        )
    if machine.wire_power is None:
        warnings.append("Wire power is blank in the material: switch the wire on by hand when the job starts.")

    data = {
        "ok": True,
        "warnings": warnings,
        "stats": {
            "time_s": round(t, 1),
            "cut_length": {"XY": round(cut_xy, 1), "UV": round(cut_uv, 1)},
            "peak_speed": {k: round(v, 1) for k, v in peak.items()},
            "kerf": {"root": round(kerf_root, 3), "tip": round(kerf_tip, 3)},
            "speed_ratio": {"root": round(ratio_root, 3), "tip": round(ratio_tip, 3)},
            "extents": {k: [_r(v) for v in ext] for k, ext in toolpath.extents().items()},
            "gcode_lines": gcode.count("\n"),
            "airfoils": {"root": root_airfoil.name, "tip": tip_airfoil.name},
        },
        "machine": {
            "tower_distance": machine.tower_distance,
            "x_travel": machine.x_travel,
            "y_travel": machine.y_travel,
            "axis_letters": machine.axis_letters,
        },
        "part": {
            "z_root": _r(layout.z_root),
            "z_tip": _r(layout.z_tip),
            "root": _pts(layout.root_shape),
            "tip": _pts(layout.tip_shape),
        },
        "block": block,
        "path": {"t": times, "xy": xy_pts, "uv": uv_pts, "cut": cut},
    }
    return SliceResult(machine, material, wing, toolpath, gcode, data)
