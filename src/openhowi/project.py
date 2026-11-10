# SPDX-License-Identifier: GPL-3.0-or-later
"""Slicer-style project: machine preset + material preset + part + placement.

A project is a plain JSON document so it can be saved, shared and sliced from
either the GUI or the command line (``openhowi slice project.howi.json``).
"""

from __future__ import annotations

import copy
import dataclasses
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from . import airfoildb
from .airfoil import NACA_SPEC, Airfoil, AirfoilError, blend, load_airfoil, parse_dat, thicken_te
from .core import CONTROL_NAMES, Channel, ControlSurface, CoreSpec, Joiner, ServoBay, Spar
from .drawing import ShapeError
from .fusecut import CutOptions, build_job, job_labels
from .fuselage import Fuselage, Saddle, Station
from .gcode import generate_gcode, segments
from .geometry import Point, bounds, polyline_length, signed_area
from .machine import Machine
from .material import Material
from .nest import NestError, NestItem, at, item_paths, nest
from .planform import PanelCut, Planform, PlanformError
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
from .toolpath import MachineLimitError, Placement, PlacementError, Toolpath, layout_wing, wing_toolpath
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

CONTROL_FIELDS: list[dict[str, Any]] = [
    {"key": "name", "label": "Surface", "type": "select", "options": [[n, n] for n in CONTROL_NAMES]},
    {"key": "position", "label": "Hinge line", "unit": "% chord", "type": "number", "min": 1, "max": 99, "step": 1,
     "help": "Where the surface comes off at its inboard end, measured from the leading edge: 75 % leaves a "
             "surface a quarter of the chord deep."},
    {"key": "position_tip", "label": "Hinge at outboard end", "unit": "% chord", "type": "number", "min": 1,
     "max": 99, "step": 1, "optional": True, "help": "Blank: the same %. The hinge line is straight."},
    {"key": "hinge", "label": "Hinged on", "type": "select",
     "options": [["top", "top (tape)"], ["bottom", "bottom (tape)"], ["center", "centre line"]],
     "help": "Top or bottom: a tape hinge along that surface. Centre line: pinned or CA hinges."},
    {"key": "bevel", "label": "V in front", "unit": "°", "type": "number", "min": 0, "max": 60, "step": 1,
     "help": "The wedge cut out between the pieces: how far the surface can swing towards the V (down for a top "
             "hinge, up for a bottom hinge, both ways on the centre line). 0: a straight cut."},
    {"key": "start", "label": "Starts at", "unit": "mm", "type": "number", "min": 0, "optional": True,
     "help": "Distance from the wing root. Blank: at the root. The wing is split into panels where the surface "
             "starts and ends, because the wire cuts the whole length of a panel."},
    {"key": "end", "label": "Ends at", "unit": "mm", "type": "number", "min": 1, "optional": True,
     "help": "Distance from the wing root. Blank: at the tip."},
]  # fmt: skip

WING_STATION_COLUMNS: list[dict[str, Any]] = [
    {"key": "x", "label": "From root", "unit": "mm", "min": 0, "help": "Distance along the span from the wing root"},
    {"key": "chord", "label": "Chord", "unit": "mm", "min": 1},
    {"key": "le", "label": "LE aft", "unit": "mm", "help": "Leading edge behind the root's leading edge"},
    {"key": "twist", "label": "Twist", "unit": "°", "help": "Washout relative to the root, nose-down"},
    {"key": "height", "label": "Height", "unit": "mm", "help": "Leading edge above the root's (dihedral, upsweep)"},
]  # fmt: skip

CURVED = ["elliptical", "stations", "drawing"]

PART_FIELDS: list[dict[str, Any]] = [
    {"key": "root_airfoil", "label": "Root airfoil", "type": "airfoil", "group": "Airfoils"},
    {"key": "tip_airfoil", "label": "Tip airfoil", "type": "airfoil", "optional": True, "group": "Airfoils",
     "help": "Leave as “same as root” for a constant section. A wing split into panels blends between the two."},
    {"key": "planform", "label": "Planform", "type": "select", "group": "Planform",
     "options": [["panel", "straight panel"], ["elliptical", "elliptical"],
                 ["stations", "from stations (curved or cranked)"], ["drawing", "from a drawing (seen from above)"]],
     "help": "A curved wing is cut as straight panels whose ends sit on the true outline."},
    {"key": "plan_view", "label": "Planform drawing", "type": "drawing", "group": "Planform",
     "show_if": {"planform": "drawing"},
     "help": "One closed outline of the half wing at 1:1: root on the left, leading edge at the top. Trace it over "
             "a picture of the plan in a drawing program."},
    {"key": "wing_stations", "label": "Stations", "type": "table", "group": "Planform", "columns": WING_STATION_COLUMNS,
     "show_if": {"planform": "stations"}, "min_rows": 2, "max": 40, "add_label": "Add station",
     "help": "The half wing from root to tip: chord, leading edge, twist and height at each station."},
    {"key": "curve", "label": "Between stations", "type": "select", "group": "Planform",
     "show_if": {"planform": "stations"},
     "options": [["smooth", "smooth curves"], ["straight", "straight lines (a joint at each station)"]]},
    {"key": "root_chord", "label": "Root chord", "unit": "mm", "type": "number", "min": 1, "group": "Planform",
     "show_if": {"planform": ["panel", "elliptical"]}},
    {"key": "tip_chord", "label": "Tip chord", "unit": "mm", "type": "number", "min": 1, "group": "Planform",
     "show_if": {"planform": ["panel", "elliptical", "drawing"]},
     "help": "Elliptical: where the ellipse is cut off. Drawing: the wing is cut out to where it gets this narrow; "
             "the rest of a rounded tip is carved by hand."},
    {"key": "span", "label": "Span", "unit": "mm", "type": "number", "min": 1, "group": "Planform",
     "show_if": {"planform": ["panel", "elliptical"]},
     "help": "Straight panel: its length along the wire. Elliptical: the half span, root to tip."},
    {"key": "line", "label": "Straight line at", "unit": "% chord", "type": "number", "min": 0, "max": 100, "step": 5,
     "group": "Planform", "show_if": {"planform": "elliptical"},
     "help": "This line runs straight from root to tip: 0 % a straight leading edge, 25 % the classic elliptical "
             "wing, 100 % a straight trailing edge."},
    {"key": "sweep", "label": "Sweep", "unit": "mm", "type": "number", "group": "Planform",
     "show_if": {"planform": ["panel", "elliptical"]},
     "help": "How far the tip leading edge sits behind the root leading edge. Elliptical: how far the straight "
             "line sits further back at the tip."},
    {"key": "washout", "label": "Washout", "unit": "°", "type": "number", "step": 0.1, "group": "Planform",
     "show_if": {"planform": ["panel", "elliptical", "drawing"]}, "help": "Tip twist, positive nose-down."},
    {"key": "washout_pivot", "label": "Twist axis", "unit": "× chord", "type": "number", "min": 0, "max": 1,
     "step": 0.05, "group": "Planform"},
    {"key": "tip_offset_y", "label": "Tip height offset", "unit": "mm", "type": "number", "group": "Planform",
     "show_if": {"planform": ["panel", "elliptical", "drawing"]},
     "help": "Raise the tip leading edge, e.g. to cut dihedral into a flat-bottomed panel."},
    {"key": "panels", "label": "Panels", "type": "number", "min": 0, "max": 40, "step": 1, "group": "Panels",
     "help": "How many straight panels to cut the wing in. 0: as few as follow the outline within the tolerance "
             "(a straight panel: one, unless it is longer than the longest panel)."},
    {"key": "tolerance", "label": "Follow the outline within", "unit": "mm", "type": "number", "min": 0.1,
     "step": 0.1, "group": "Panels", "show_if": {"planform": CURVED},
     "help": "How far the straight edges of a panel may stray from the curve; sand the rest fair."},
    {"key": "max_panel", "label": "Longest panel", "unit": "mm", "type": "number", "min": 20, "optional": True,
     "group": "Panels", "help": "Blank: a curved wing's panels are as long as fit between the carriages."},
    {"key": "min_panel", "label": "Shortest panel", "unit": "mm", "type": "number", "min": 5, "step": 5,
     "group": "Panels", "show_if": {"planform": CURVED},
     "help": "A short panel that tapers a lot swings the carriages far: its cut is carried out to them. Longer "
             "tip panels stray further from the curve but stay on the machine."},
    {"key": "controls", "label": "Control surface", "type": "list", "group": "Control surface",
     "item_label": "Control surface", "add_label": "Add aileron, flap or elevator", "max": 1,
     "item_fields": CONTROL_FIELDS,
     "empty": "No control surface. Add one to cut it off along its hinge line in the same cut.",
     "default_item": {"name": "aileron", "position": 75.0, "position_tip": None, "hinge": "top", "bevel": 25.0,
                      "start": None, "end": None}},
    {"key": "sheeting_top", "label": "Top sheeting", "unit": "mm", "type": "number", "min": 0, "step": 0.1,
     "group": "Core", "help": "Skin glued on top (e.g. 1.5 mm balsa, 0.6 mm obeche): the core is cut this much undersize."},
    {"key": "sheeting_bottom", "label": "Bottom sheeting", "unit": "mm", "type": "number", "min": 0, "step": 0.1,
     "group": "Core"},
    {"key": "le_stock", "label": "LE stock", "unit": "mm", "type": "number", "min": 0, "step": 0.5, "group": "Core",
     "help": "Cut the core back this far from the leading edge to fit a balsa or hardwood leading-edge strip."},
    {"key": "te_stock", "label": "TE stock", "unit": "mm", "type": "number", "min": 0, "step": 0.5, "group": "Core",
     "help": "Cut the core back this far from the trailing edge to fit a trailing-edge strip."},
    {"key": "te_thickness", "label": "TE thickness", "unit": "mm", "type": "number", "min": 0, "step": 0.1,
     "group": "Core",
     "help": "Open a sharp trailing edge to at least this thickness: a foam edge thinner than the kerf melts away. "
             "The section thickens smoothly from the nose back. 0: as the airfoil is."},
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
    {"key": "pair", "label": "Both hands in one block", "type": "bool", "group": "Nesting",
     "help": "Cut the left and the right panel in one go: the opposite hand stacks above (or in front of) this one, "
             "over the same stretch of wire."},
    {"key": "nest_gap", "label": "Gap between them", "unit": "mm", "type": "number", "min": 1, "step": 1,
     "group": "Nesting", "show_if": {"pair": True}},
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

STATION_COLUMNS: list[dict[str, Any]] = [
    {"key": "x", "label": "x", "unit": "mm", "help": "Distance from the nose"},
    {"key": "top", "label": "Top", "unit": "mm", "help": "Height of the top line above the datum"},
    {"key": "bottom", "label": "Bottom", "unit": "mm", "help": "Height of the bottom line (below the datum: negative)"},
    {"key": "width", "label": "Width", "unit": "mm", "min": 0, "help": "Full width"},
    {"key": "round_top", "label": "Rnd ↑", "unit": "%", "min": 0, "max": 100,
     "help": "Upper half of the section: 100 % elliptical, 0 % square corners"},
    {"key": "round_bottom", "label": "Rnd ↓", "unit": "%", "min": 0, "max": 100,
     "help": "Lower half of the section: 100 % elliptical, 0 % flat bottom with square corners"},
]  # fmt: skip

SADDLE_FIELDS: list[dict[str, Any]] = [
    {"key": "airfoil", "label": "Wing airfoil", "type": "airfoil"},
    {"key": "position", "label": "Leading edge", "unit": "mm", "type": "number", "min": 0,
     "help": "Distance of the wing's leading edge from the nose."},
    {"key": "chord", "label": "Chord", "unit": "mm", "type": "number", "min": 1},
    {"key": "height", "label": "LE height", "unit": "mm", "type": "number", "step": 0.5,
     "help": "Height of the wing's leading edge above the datum."},
    {"key": "incidence", "label": "Incidence", "unit": "°", "type": "number", "step": 0.25,
     "help": "Wing incidence, nose up."},
    {"key": "side", "label": "Wing", "type": "select",
     "options": [["top", "on top (high wing)"], ["bottom", "underneath (low wing)"]]},
]  # fmt: skip

FUSELAGE_FIELDS: list[dict[str, Any]] = [
    {"key": "source", "label": "Shape from", "type": "select", "group": "Shape",
     "options": [["stations", "stations"], ["views", "side and top view drawings"]]},
    {"key": "stations", "label": "Stations", "type": "table", "group": "Shape", "columns": STATION_COLUMNS,
     "show_if": {"source": "stations"}, "min_rows": 2, "max": 40, "add_label": "Add station",
     "help": "Sizes along the fuselage. Between stations the lines run smoothly. Heights are from a datum "
             "line of your choice, e.g. the thrust line."},
    {"key": "side_view", "label": "Side view", "type": "drawing", "group": "Shape", "show_if": {"source": "views"},
     "help": "One closed outline: nose on the left, top up."},
    {"key": "top_view", "label": "Top view", "type": "drawing", "group": "Shape", "show_if": {"source": "views"},
     "help": "One closed outline: nose on the left, drawn at the same scale as the side view."},
    {"key": "round_top", "label": "Roundness, top", "unit": "%", "type": "number", "min": 0, "max": 100, "step": 5,
     "group": "Shape", "show_if": {"source": "views"}, "help": "100 %: elliptical sections; 0 %: square corners."},
    {"key": "round_bottom", "label": "Roundness, bottom", "unit": "%", "type": "number", "min": 0, "max": 100,
     "step": 5, "group": "Shape", "show_if": {"source": "views"}},
    {"key": "saddles", "label": "Wing saddles", "type": "list", "group": "Wing saddle", "item_label": "Saddle",
     "add_label": "Add wing saddle", "max": 3, "item_fields": SADDLE_FIELDS,
     "empty": "No wing saddle. A saddle shapes the top (or bottom) line to the wing's airfoil.",
     "default_item": {"airfoil": "naca2412", "position": 120.0, "chord": 200.0, "height": 45.0, "incidence": 0.0,
                      "side": "top"}},
    {"key": "method", "label": "Cut as", "type": "select", "group": "Cut",
     "options": [["two_view", "two views: side, then top"], ["turned", "turned: N passes round it"],
                 ["segments", "lengthwise segments (rounded)"]],
     "help": "Two views: two passes, then round the corners by hand. Turned: the block turns about the fuselage "
             "between passes (by hand on a jig, or on a rotary axis) for an N-sided fuselage. Segments: the wire "
             "runs along the fuselage and cuts rounded sections; glue the segments together."},
    {"key": "facets", "label": "Passes (sides)", "type": "select", "group": "Cut", "show_if": {"method": "turned"},
     "options": [[n, f"{n} passes, {n}-sided"] for n in (4, 6, 8, 12, 16, 24, 32)],
     "help": "More passes leave a rounder fuselage and less to sand."},
    {"key": "plate", "label": "Jig plate thickness", "unit": "mm", "type": "number", "min": 0, "step": 0.5,
     "group": "Cut", "show_if": {"method": "turned"},
     "help": "The two end plates the block is rolled on. Not used on a machine with a rotary axis."},
    {"key": "hold", "label": "Foam kept round the axis", "unit": "mm", "type": "number", "min": 1, "step": 0.5,
     "group": "Cut", "show_if": {"method": "turned"},
     "help": "So the fuselage stays joined to the end stubs until the last pass."},
    {"key": "axis", "label": "Turning axis height", "unit": "mm", "type": "number", "optional": True, "step": 0.5,
     "group": "Cut", "show_if": {"method": "turned"},
     "help": "Above the datum. Blank: the height that stays deepest inside the fuselage."},
    {"key": "segment_length", "label": "Longest segment", "unit": "mm", "type": "number", "min": 10,
     "group": "Cut", "show_if": {"method": "segments"}},
    {"key": "halves", "label": "Left and right halves", "type": "bool", "group": "Cut",
     "show_if": {"method": "segments"},
     "help": "Cut every segment as two half shells, glued along the centre line: hollow halves need no slit."},
    {"key": "wall", "label": "Hollow: wall", "unit": "mm", "type": "number", "min": 0, "step": 0.5, "group": "Cut",
     "show_if": {"method": "segments"}, "help": "0: solid. A hollow full segment is reached through a slit."},
    {"key": "spare", "label": "Spare foam", "unit": "mm", "type": "number", "min": 1, "step": 1, "group": "Cut",
     "help": "Foam left around the fuselage on every side of the block."},
    {"key": "skin", "label": "Planking / skin", "unit": "mm", "type": "number", "min": 0, "step": 0.1,
     "group": "Plans", "help": "The formers in the plans are drawn this much inside the fuselage."},
    {"key": "points", "label": "Points per section", "type": "number", "min": 16, "max": 400, "step": 4,
     "group": "Advanced", "show_if": {"method": "segments"}, "help": "A multiple of 4."},
    {"key": "kerf_root_override", "label": "Front kerf override", "unit": "mm", "type": "number", "min": 0,
     "step": 0.05, "optional": True, "group": "Advanced", "help": "Blank: from the material."},
    {"key": "kerf_tip_override", "label": "Back kerf override", "unit": "mm", "type": "number", "min": 0,
     "step": 0.05, "optional": True, "group": "Advanced"},
]  # fmt: skip

FUSELAGE_PLACEMENT_FIELDS: list[dict[str, Any]] = [
    {"key": "root_side", "label": "Measure from", "type": "select",
     "options": [["xy", "the XY carriage"], ["uv", "the UV carriage"]]},
    {"key": "root_offset", "label": "Distance from that carriage", "unit": "mm", "type": "number", "min": 0},
    {"key": "x_margin", "label": "Foam rear face X", "unit": "mm", "type": "number", "min": 1,
     "help": "Machine X of the back of the foam block (the side towards X = 0)."},
    {"key": "y_offset", "label": "Foam underside Y", "unit": "mm", "type": "number", "min": 0,
     "help": "Machine Y of the underside of the foam, i.e. of the support it rests on."},
]  # fmt: skip

NEST_ITEM_FIELDS: list[dict[str, Any]] = [
    {"key": "source", "label": "Part", "type": "part",
     "help": "A wing panel or a 2D shape: take the one set up in this session, or load a saved project."},
    {"key": "job", "label": "Panel", "type": "number", "min": 0, "step": 1,
     "help": "For a wing cut in several panels: which one. 0: all of them."},
    {"key": "copies", "label": "Copies", "type": "number", "min": 1, "max": 50, "step": 1},
    {"key": "opposite", "label": "Opposite hand", "type": "bool",
     "help": "Root at the other end of the block: the other wing half."},
]  # fmt: skip

NEST_FIELDS: list[dict[str, Any]] = [
    {"key": "items", "label": "Parts", "type": "list", "group": "Parts", "item_label": "Part", "add_label": "Add part",
     "max": 12, "item_fields": NEST_ITEM_FIELDS,
     "empty": "No parts yet. Add wing panels and 2D shapes to cut them all from one block, in one go.",
     "default_item": {"source": None, "job": 1, "copies": 1, "opposite": False}},
    {"key": "gap", "label": "Gap between parts", "unit": "mm", "type": "number", "min": 1, "step": 1,
     "group": "Block"},
    {"key": "max_height", "label": "Block height", "unit": "mm", "type": "number", "min": 10, "optional": True,
     "group": "Block",
     "help": "Tallest stack of parts; the next ones start a column in front. Blank: as high as the machine reaches."},
]  # fmt: skip

NEST_PLACEMENT_FIELDS: list[dict[str, Any]] = [
    {"key": "root_offset", "label": "Block from XY carriage", "unit": "mm", "type": "number", "min": 0,
     "help": "Distance from the XY carriage to the near end of the block, where every part starts."},
    {"key": "x_margin", "label": "Rear of the cut X", "unit": "mm", "type": "number", "min": 1,
     "help": "Machine X of the back of the parts. The foam's rear face goes between X=0 and here."},
    {"key": "y_offset", "label": "Lowest part Y", "unit": "mm", "type": "number", "min": 0,
     "help": "Machine Y of the bottom of the lowest part in each column."},
]  # fmt: skip

PART_TYPES = [
    {"type": "wing", "label": "Wing panel"},
    {"type": "shape", "label": "2D shape"},
    {"type": "fuselage", "label": "Fuselage"},
    {"type": "nest", "label": "Block of parts"},
]
PART_SCHEMAS = {"wing": PART_FIELDS, "shape": SHAPE_FIELDS, "fuselage": FUSELAGE_FIELDS, "nest": NEST_FIELDS}
PLACEMENT_SCHEMAS = {"wing": PLACEMENT_FIELDS, "shape": SHAPE_PLACEMENT_FIELDS, "fuselage": FUSELAGE_PLACEMENT_FIELDS,
                     "nest": NEST_PLACEMENT_FIELDS}  # fmt: skip
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
        "te_thickness": 0.0,
        "spars": [],
        "channels": [],
        "joiners": [],
        "bays": [],
        "kerf_root_override": None,
        "kerf_tip_override": None,
        "points_per_surface": 80,
        "planform": "panel",
        "plan_view": None,
        "wing_stations": [
            {"x": x, "chord": c, "le": le, "twist": tw, "height": h}
            for x, c, le, tw, h in (
                (0.0, 250.0, 0.0, 0.0, 0.0),
                (350.0, 225.0, 12.0, 0.5, 0.0),
                (600.0, 120.0, 70.0, 2.0, 15.0),
            )
        ],
        "curve": "smooth",
        "line": 25.0,
        "panels": 0,
        "tolerance": 2.0,
        "max_panel": None,
        "min_panel": 40.0,
        "controls": [],
        "job": 1,
        "pair": False,
        "nest_gap": 8.0,
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
DEFAULT_PARTS["fuselage"] = {
    "type": "fuselage",
    "source": "stations",
    "stations": [
        {"x": x, "top": t, "bottom": b, "width": w, "round_top": rt, "round_bottom": rb}
        for x, t, b, w, rt, rb in (
            (0.0, 22.0, -22.0, 44.0, 100.0, 100.0),
            (30.0, 34.0, -30.0, 58.0, 100.0, 90.0),
            (120.0, 48.0, -38.0, 72.0, 80.0, 50.0),
            (250.0, 44.0, -34.0, 64.0, 80.0, 50.0),
            (400.0, 30.0, -10.0, 34.0, 70.0, 60.0),
            (500.0, 22.0, 6.0, 16.0, 60.0, 60.0),
        )
    ],
    "side_view": None,
    "top_view": None,
    "round_top": 100.0,
    "round_bottom": 100.0,
    "saddles": [],
    "method": "two_view",
    "segment_length": 150.0,
    "halves": False,
    "wall": 0.0,
    "spare": 5.0,
    "skin": 0.0,
    "points": 64,
    "facets": 8,
    "plate": 6.0,
    "hold": 5.0,
    "axis": None,
    "job": 1,
    "kerf_root_override": None,
    "kerf_tip_override": None,
}
DEFAULT_PARTS["nest"] = {"type": "nest", "items": [], "gap": 8.0, "max_height": None}
DEFAULT_PLACEMENTS: dict[str, dict[str, Any]] = {
    "nest": {"root_side": "xy", "root_offset": 200.0, "x_margin": 25.0, "y_offset": 20.0},
    "wing": {"root_side": "xy", "root_offset": 200.0, "x_margin": 25.0, "y_offset": 40.0},
    "shape": {"root_side": "xy", "root_offset": 300.0, "x_margin": 20.0, "y_offset": 20.0},
    "fuselage": {"root_side": "xy", "root_offset": 30.0, "x_margin": 20.0, "y_offset": 10.0},
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
    fuselage: Fuselage | None = None


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
    fuselage: Fuselage | None = None
    jobs: list[str] = field(default_factory=list)
    """Labels of the separate cuts a part needs (e.g. two passes); ``job`` is the one sliced (1-based)."""
    job: int = 1
    notes: list[str] = field(default_factory=list)
    """Step-by-step instructions for this cut."""
    views: list[dict[str, Any]] = field(default_factory=list)
    """2D views to show as thumbnails: {"label", "rings"}."""
    ghosts: list[list[list[tuple[float, float, float]]]] = field(default_factory=list)
    """Finished shapes to show faintly in 3D, as rings to loft."""
    block: dict[str, float] | None = None
    """The foam block, when the part type knows it better than the outline's bounds."""
    steps: list[dict[str, Any]] = field(default_factory=list)
    """Illustrated steps for this cut: {"title", "notes", "svg"}."""
    ghost_label: str = ""
    jig: dict[str, float] | None = None
    solid_z: list[tuple[float, float]] = field(default_factory=list)
    """Root and tip positions of each solid when they differ (nested parts); empty: all at z_root/z_tip."""
    skin_z: list[tuple[float, float]] = field(default_factory=list)


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

    build = {"wing": _wing_part, "shape": _shape_part, "fuselage": _fuselage_part, "nest": _nest_part}[kind]
    built = build(part, machine, material, placement)
    return _finish(project, kind, machine, material, placement, built)


_SPLITS: OrderedDict[str, list[float]] = OrderedDict()


def build_planform(part: dict[str, Any], warnings: list[str] | None = None) -> Planform:
    """The half wing seen from above, as the part describes it."""
    warnings = [] if warnings is None else warnings
    kind = part.get("planform") or "panel"
    washout = _number(part, "washout", 0.0) or 0.0
    rise = _number(part, "tip_offset_y", 0.0) or 0.0
    try:
        if kind in ("panel", "elliptical"):
            root_chord, span = _number(part, "root_chord"), _number(part, "span")
            if root_chord is None or span is None:
                raise SliceError("root_chord and span are required")
            tip_chord = _number(part, "tip_chord") or root_chord
            sweep = _number(part, "sweep", 0.0) or 0.0
            if kind == "panel":
                return Planform.trapezoid(root_chord, tip_chord, span, sweep, washout, rise)
            line = _number(part, "line", 25.0)
            return Planform.elliptical(root_chord, tip_chord, span, 25.0 if line is None else line, sweep, washout,
                                       rise)  # fmt: skip
        if kind == "stations":
            raw = part.get("wing_stations") or []
            if not isinstance(raw, list):
                raise SliceError("wing stations must be a list")
            rows = []
            for number, row in enumerate(raw, start=1):
                if not isinstance(row, dict):
                    raise SliceError(f"wing station {number} must be an object")
                values = []
                for key in ("x", "chord", "le", "twist", "height"):
                    value = row.get(key) or 0.0
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise SliceError(f"wing station {number}: {key} must be a number")
                    values.append(float(value))
                rows.append(tuple(values))
            return Planform.from_stations(rows, smooth=part.get("curve", "smooth") != "straight")  # type: ignore[arg-type]
        if kind == "drawing":
            outline = _view_outline(part, "plan_view", "planform drawing", warnings)
            plan, more = Planform.from_outline(outline, _number(part, "tip_chord") or 20.0, washout, rise)
            warnings += more
            return plan
    except PlanformError as exc:
        raise SliceError(str(exc)) from exc
    raise SliceError(f"unknown planform {kind!r}")


def _split_planform(plan: Planform, key: dict[str, Any], **options: Any) -> list[float]:
    """Panel joints, cached: the GUI slices on every edit and curved planforms take a moment to split."""
    cache_key = json.dumps([key, options], sort_keys=True, default=str)
    if cache_key in _SPLITS:
        _SPLITS.move_to_end(cache_key)
        return _SPLITS[cache_key]
    try:
        joints = plan.split(**options)
    except PlanformError as exc:
        raise SliceError(str(exc)) from exc
    _SPLITS[cache_key] = joints
    while len(_SPLITS) > 32:
        _SPLITS.popitem(last=False)
    return joints


def _panel_features(
    plan: Planform,
    panel: PanelCut,
    spars: tuple[Spar, ...],
    channels: tuple[Channel, ...],
    joiners: tuple[Joiner, ...],
    bays: tuple[ServoBay, ...],
    control: ControlSurface | None,
) -> tuple[tuple[Spar, ...], tuple[Channel, ...], tuple[Joiner, ...], tuple[ServoBay, ...], ControlSurface | None]:
    """A wing's features as one of its panels has them.

    Spars, channels and the hinge line run straight (seen from above) from their root position to
    their tip position; joiners go in the root panel; servo bays in the panel they sit in.
    """
    span, where = plan.span, f"panel {panel.number}"

    def along(label: str, root: float, tip: float, sa: float = 0.0, sb: float = span) -> tuple[float, float]:
        ends = []
        for s in (panel.s0, panel.s1):
            value = plan.line_position(s, (sa, root), (sb, tip))
            if not 1.0 <= value <= 99.0:
                raise SliceError(
                    f"{where}: {label} runs off the chord ({value:.0f} % of it {s:.0f} mm from the root); set "
                    "its tip position so it stays inside the wing"
                )
            ends.append(round(value, 4))
        return ends[0], ends[1]

    new_spars = []
    for number, spar in enumerate(spars, start=1):
        a, b = along(f"spar {number}", spar.position, spar.at(True))
        new_spars.append(dataclasses.replace(spar, position=a, position_tip=b))
    new_channels = []
    for number, channel in enumerate(channels, start=1):
        a, b = along(f"servo channel {number}", channel.position, channel.at(True))
        new_channels.append(dataclasses.replace(channel, position=a, position_tip=b))
    new_bays = []
    for number, bay in enumerate(bays, start=1):
        lo, hi = bay.station - bay.length / 2, bay.station + bay.length / 2
        if lo >= panel.s0 - 1e-6 and hi <= panel.s1 + 1e-6:
            new_bays.append(dataclasses.replace(bay, station=bay.station - panel.s0))
    new_control = None
    if control is not None:
        sa = control.start or 0.0
        sb = control.end if control.end is not None else span
        if panel.s0 >= sa - 1.0 and panel.s1 <= sb + 1.0:
            a, b = along(f"the {control.name}'s hinge line", control.position, control.at(True), sa, min(sb, span))
            new_control = dataclasses.replace(control, position=a, position_tip=b, start=None, end=None)
    return tuple(new_spars), tuple(new_channels), joiners if panel.s0 < 1e-6 else (), tuple(new_bays), new_control


def _wing_part(part: dict[str, Any], machine: Machine, material: Material, placement: Placement) -> _Part:
    pair = part.get("pair", False)
    if not isinstance(pair, bool):
        raise SliceError("pair must be true or false")
    if not pair:
        return _wing_cut(part, machine, material, placement)
    roomy = _roomy(machine)
    lifted = dataclasses.replace(placement, y_offset=placement.y_offset + LIFT)  # cut clear of the table, then placed
    first = _wing_cut(part, roomy, material, lifted)
    second = _wing_cut(part, roomy, material, _opposite(lifted, first, machine))
    bottom = min(y for path in item_paths(first.toolpath) for z in (first.z_root, first.z_tip)
                 for _, y in at(path, z, machine.tower_distance))  # fmt: skip
    gap = _number(part, "nest_gap", 8.0) or 8.0
    try:
        combined = combine_parts([first, second], ["as drawn", "opposite hand"], machine, placement, gap,
                                 bottom - LIFT)  # fmt: skip
    except MachineLimitError as exc:
        if not first.jobs:
            raise
        raise SliceError(f"panel {first.job}: {exc}. {TAPER_ADVICE}") from exc
    combined.notes.append(
        "Both hands come out of this block: the lower core as drawn, the upper one the opposite hand (its root "
        "at the other end of the block). Mark them before taking them out."
    )
    return combined


TAPER_ADVICE = (
    "The wire carries a panel's taper on out to the carriages: use fewer or longer panels (Panels, Shortest panel), "
    "a larger tip chord, or move the towers closer together"
)
LIFT = 5e5
"""How far above the table parts are cut on their own before they are nested (and moved down)."""


def _roomy(machine: Machine) -> Machine:
    """The machine with room to spare, to cut parts on their own before they are nested and checked."""
    return dataclasses.replace(machine, x_travel=1e6, y_travel=2e6)


def _opposite(placement: Placement, cut: _Part, machine: Machine) -> Placement:
    """Where the opposite-hand part goes: root at the other carriage, over the same stretch of wire."""
    lo, hi = min(cut.z_root, cut.z_tip), max(cut.z_root, cut.z_tip)
    if placement.root_side == "xy":
        return dataclasses.replace(placement, root_side="uv", root_offset=machine.tower_distance - hi)
    return dataclasses.replace(placement, root_side="xy", root_offset=lo)


def combine_parts(
    parts: list[_Part],
    labels: list[str],
    machine: Machine,
    placement: Placement,
    gap: float,
    base_y: float | None = None,
    max_height: float | None = None,
) -> _Part:
    """Several parts, each cut as if on its own, nested in one block and cut in one go."""
    items = [NestItem(label, item_paths(p.toolpath), p.z_root, p.z_tip) for p, label in zip(parts, labels)]
    try:
        result = nest(items, machine, placement.x_margin, gap, base_y, max_height)
    except NestError as exc:
        raise SliceError(str(exc)) from exc

    def move(points: list[Point], dx: float, dy: float) -> list[Point]:
        return [(x + dx, y + dy) for x, y in points]

    def move_region(r: Region, dx: float, dy: float) -> Region:
        return Region(move(r.outer, dx, dy), [move(h, dx, dy) for h in r.holes])

    first = parts[0]
    out = _Part(
        toolpath=result.toolpath,
        z_root=result.z0,
        z_tip=result.z1,
        root_regions=[],
        tip_regions=[],
        kerf=first.kerf,
        ratio=first.ratio,
        comments=[f"{len(parts)} parts nested in one block"],
        warnings=[],
        stats=dict(first.stats),
        wing=first.wing,
        jobs=first.jobs,
        job=first.job,
        notes=list(first.notes),
        views=first.views,
    )
    for part, placed, label in zip(parts, result.placed, labels):
        dx, dy = placed.dx, placed.dy
        out.root_regions += [move_region(r, dx, dy) for r in part.root_regions]
        out.tip_regions += [move_region(r, dx, dy) for r in part.tip_regions]
        out.solid_z += [(part.z_root, part.z_tip)] * len(part.root_regions)
        out.skins += [(move(a, dx, dy), move(b, dx, dy)) for a, b in part.skins]
        out.skin_z += [(part.z_root, part.z_tip)] * len(part.skins)
        out.bays += [(z0, z1, move(a, dx, dy), move(b, dx, dy)) for z0, z1, a, b in part.bays]
        out.comments += [f"{label}: {line}" for line in part.comments]
        for warning in part.warnings:
            if warning not in out.warnings:
                out.warnings.append(warning)
    out.warnings += result.warnings
    out.stats["nest"] = {
        "parts": len(parts),
        "columns": 1 + max(pl.column for pl in result.placed),
        "items": [{"label": label, "column": pl.column + 1} for label, pl in zip(labels, result.placed)],
    }
    return out


def _wing_cut(part: dict[str, Any], machine: Machine, material: Material, placement: Placement) -> _Part:
    root_airfoil = resolve_airfoil(part.get("root_airfoil", "naca2412"))
    tip_source = part.get("tip_airfoil")
    tip_airfoil = resolve_airfoil(tip_source) if tip_source else root_airfoil

    pivot = _number(part, "washout_pivot", 0.25)
    pivot = 0.25 if pivot is None else pivot
    n = int(_number(part, "points_per_surface", 80) or 80)
    core = {key: _number(part, key, 0.0) or 0.0 for key in ("sheeting_top", "sheeting_bottom", "le_stock", "te_stock")}
    spars = _items(legacy_spars(part), Spar, "spar")
    channels = _items(part.get("channels") or [], Channel, "servo channel")
    joiners = _items(part.get("joiners") or [], Joiner, "joiner")
    bays = _items(part.get("bays") or [], ServoBay, "servo bay")
    controls = _items(part.get("controls") or [], ControlSurface, "control surface")
    if len(controls) > 1:
        raise SliceError("a wing can have one control surface per cut; split it into panels for more")
    control = controls[0] if controls else None

    warnings: list[str] = []
    plan = build_planform(part, warnings)
    kind = plan.kind
    forced = []
    if control is not None:
        if control.start is not None and control.start >= plan.span - 1:
            raise SliceError(f"the {control.name} starts beyond the {plan.span:.0f} mm half span")
        forced = [v for v in (control.start, control.end) if v is not None and 1.0 < v < plan.span - 1.0]
    count = int(_number(part, "panels", 0) or 0)
    if count < 0:
        raise SliceError("panels must be 0 or more")
    tolerance = max(0.05, _number(part, "tolerance", 2.0) or 2.0)
    room = machine.tower_distance - placement.root_offset
    max_panel = _number(part, "max_panel")
    if max_panel is None:
        max_panel = float("inf") if kind == "panel" else room
    if max_panel < 20:
        raise SliceError(
            f"a panel {placement.root_offset:g} mm from the carriage does not fit between carriages "
            f"{machine.tower_distance:g} mm apart"
        )
    key = {k: part.get(k) for k in ("planform", "root_chord", "tip_chord", "span", "sweep", "line", "washout",
                                    "tip_offset_y", "wing_stations", "curve", "plan_view")}  # fmt: skip
    min_panel = _number(part, "min_panel", 40.0) or 40.0
    joints = _split_planform(plan, key, forced=forced, count=count, tolerance=tolerance, max_length=max_panel,
                             min_length=min(min_panel, max_panel))  # fmt: skip
    panels = plan.panels(joints)
    for number, bay in enumerate(bays, start=1):  # wherever it sits, whichever panel is being cut
        for joint in joints[1:-1]:
            if bay.station - bay.length / 2 < joint < bay.station + bay.length / 2:
                raise SliceError(f"servo bay {number} crosses the joint between panels {joint:.0f} mm from the root")
    job = min(max(int(_number(part, "job", 1) or 1), 1), len(panels))
    panel = panels[job - 1]
    whole = kind == "panel" and len(panels) == 1
    if whole:
        root_chord = _number(part, "root_chord") or panel.root_chord
        dims = (root_chord, _number(part, "tip_chord") or root_chord, panel.span, _number(part, "sweep", 0.0) or 0.0,
                _number(part, "washout", 0.0) or 0.0, _number(part, "tip_offset_y", 0.0) or 0.0)  # fmt: skip
        root_foil, tip_foil = root_airfoil, tip_airfoil
        features = (spars, channels, joiners, bays, control)
    else:
        dims = (panel.root_chord, panel.tip_chord, panel.span, panel.sweep, panel.washout, panel.rise)
        root_foil = blend(root_airfoil, tip_airfoil, panel.s0 / plan.span)
        tip_foil = blend(root_airfoil, tip_airfoil, panel.s1 / plan.span)
        features = _panel_features(plan, panel, spars, channels, joiners, bays, control)
    root_chord, tip_chord, span, sweep, washout, tip_offset_y = dims
    spars, channels, joiners, bays, control = features
    te_thickness = _number(part, "te_thickness", 0.0) or 0.0
    if te_thickness < 0:
        raise SliceError("te_thickness must be >= 0")
    if te_thickness > 0:
        root_foil = thicken_te(root_foil, te_thickness / root_chord)
        tip_foil = thicken_te(tip_foil, te_thickness / tip_chord)

    try:
        wing = WingPanel(
            root_airfoil=root_foil,
            root_chord=root_chord,
            span=span,
            tip_airfoil=tip_foil,
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
            control=control,
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
        prefix = "" if len(panels) == 1 else f"panel {job}: "
        raise SliceError(f"{prefix}{exc}") from exc

    comments = [
        f"root {root_foil.name} {root_chord:.1f} mm, tip {tip_foil.name} {tip_chord:.1f} mm, span {span:.1f} mm"
        if not whole else
        f"root {root_foil.name} {root_chord:g} mm, tip {tip_foil.name} {tip_chord:g} mm, span {span:g} mm"
    ]  # fmt: skip
    stats: dict[str, Any] = {"airfoils": {"root": root_foil.name, "tip": tip_foil.name}}
    notes: list[str] = []
    views: list[dict[str, Any]] = []
    labels = [f"Panel {pc.number} of {len(panels)} · {pc.s0:.0f}–{pc.s1:.0f} mm" for pc in panels]
    if not whole:
        worst = max(pc.error for pc in panels)
        comments.insert(0, f"{kind} half wing {plan.span:.0f} mm in {len(panels)} panels; panel {job}: "
                           f"{panel.s0:.1f}-{panel.s1:.1f} mm from the root")  # fmt: skip
        stats["planform"] = {
            "kind": kind,
            "span": round(plan.span, 1),
            "panels": len(panels),
            "panel": job,
            "from": round(panel.s0, 1),
            "to": round(panel.s1, 1),
            "error": round(panel.error, 2),
            "worst": round(worst, 2),
        }
        notes += _panel_notes(plan, panels, panel, root_foil, tip_foil)
        if kind != "panel" and panel.error > tolerance + 0.05:
            warnings.append(
                f"Panel {job} strays up to {panel.error:.1f} mm from the outline (more than the {tolerance:g} mm "
                f"asked for): the tip curves too tightly for panels of at least {min_panel:g} mm. Sand it fair."
            )
        outline = plan.outline(joints=joints)
        lines = [[(s, -plan.le(s)), (s, -plan.te(s))] for s in joints[1:-1]]
        current = [(panel.s0, -plan.le(panel.s0)), (panel.s1, -plan.le(panel.s1)), (panel.s1, -plan.te(panel.s1)),
                   (panel.s0, -plan.te(panel.s0))]  # fmt: skip
        views.append({"label": f"Planform · panel {job} of {len(panels)}", "rings": [outline], "plan": True,
                      "marks": [current], "lines": lines})  # fmt: skip
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
    if te_thickness > 0:
        comments.append(f"trailing edge opened to {te_thickness:g} mm")
    if control is not None:
        depth = (root_chord * (1 - control.position / 100), tip_chord * (1 - control.at(True) / 100))
        comments.append(
            f"{control.name}: hinge line at {control.position:g} % (tip {control.at(True):g} %), {control.describe()}"
        )
        stats["control"] = {"name": control.name, "hinge": control.hinge, "bevel": control.bevel,
                            "depth": {"root": round(depth[0], 1), "tip": round(depth[1], 1)}}  # fmt: skip
        notes += control_notes(control, depth)
        if len(panels) > 1:
            labels[job - 1] += f" · {control.name}"
    if not whole:
        for pc in panels:  # name the other panels that carry the surface too
            if pc.number != job and labels[pc.number - 1].count("·") == 1 and _carries(controls, pc, plan.span):
                labels[pc.number - 1] += f" · {controls[0].name}"
    try:
        toolpath = wing_toolpath(layout, machine)
    except MachineLimitError as exc:
        if len(panels) == 1:
            raise
        taper = abs(tip_chord - root_chord)
        raise SliceError(
            f"panel {job}: {exc}. It is {span:.0f} mm long and its chord changes by {taper:.0f} mm. {TAPER_ADVICE}"
        ) from exc
    return _Part(
        toolpath=toolpath,
        z_root=layout.z_root,
        z_tip=layout.z_tip,
        root_regions=[Region(ring, holes) for ring, holes in layout.root_pieces],
        tip_regions=[Region(ring, holes) for ring, holes in layout.tip_pieces],
        kerf=kerf,
        ratio=ratio,
        comments=comments,
        warnings=warnings,
        stats=stats,
        skins=[(layout.root_skin, layout.tip_skin)] if built_up else [],
        bays=layout.bays,
        wing=wing,
        jobs=labels if len(panels) > 1 else [],
        job=job,
        notes=notes,
        views=views,
    )


def _carries(controls: tuple[ControlSurface, ...], panel: PanelCut, span: float) -> bool:
    if not controls:
        return False
    sa, sb = controls[0].start or 0.0, controls[0].end if controls[0].end is not None else span
    return panel.s0 >= sa - 1.0 and panel.s1 <= sb + 1.0


def _panel_notes(plan: Planform, panels: list[PanelCut], panel: PanelCut, root: Airfoil, tip: Airfoil) -> list[str]:
    k, n = panel.number, len(panels)
    notes = [
        f"Panel {k} of {n} runs from {panel.s0:.0f} to {panel.s1:.0f} mm out from the wing root: chord "
        f"{panel.root_chord:.1f} mm at its root and {panel.tip_chord:.1f} mm at its tip, sweep {panel.sweep:.1f} mm"
        + (f", washout {panel.washout:.2f}°" if abs(panel.washout) > 0.005 else "")
        + (f", tip {panel.rise:.1f} mm higher" if abs(panel.rise) > 0.05 else "")
        + "."
    ]
    if root.name != tip.name or "/" in root.name:
        notes.append(f"Its root section is {root.name} and its tip section {tip.name}, blended along the wing.")
    if plan.kind != "panel" and panel.error > 0.05:
        notes.append(
            f"Its straight edges stray up to {panel.error:.1f} mm from the curved outline between the joints: sand "
            "the leading and trailing edges fair after gluing the panels together."
        )
    if k > 1:
        notes.append(f"Glue its root face to the tip face of panel {k - 1}; the sections match there.")
    if k < n:
        notes.append(f"Mark its tip face before you take it off the machine: panel {k + 1} is glued to it.")
    notes.append("Cut every panel again with “Root faces: UV carriage” for the other wing half.")
    return notes


def control_notes(control: ControlSurface, depth: tuple[float, float]) -> list[str]:
    """How to finish and hinge a control surface that came off in the cut."""
    notes = [
        (
            f"The {control.name} comes off in the same cut, along a straight hinge line: {depth[0]:.0f} mm deep at the "
            f"root and {depth[1]:.0f} mm at the tip. Mark which way round it goes before separating the pieces."
        )
    ]
    if control.bevel == 0:
        notes.append(
            "The hinge cut is straight, one kerf wide: sand a bevel on the surface's front face for the throw you need."
        )
    elif control.hinge == "center":
        notes.append(
            f"Hinge it on the centre line (pinned or CA hinges): the {control.bevel:g}° V above and below lets it "
            f"swing {control.bevel:g}° each way."
        )
    else:
        other = "bottom" if control.hinge == "top" else "top"
        swing = "down" if control.hinge == "top" else "up"
        notes.append(
            f"Hinge it with tape along the {control.hinge} surface, where the pieces meet: the {control.bevel:g}° V in "
            f"the {other} surface lets it swing {control.bevel:g}° {swing}; the other way it opens freely."
        )
    return notes


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
            if key in ("side", "shape", "hinge", "name"):
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


def _nest_part(part: dict[str, Any], machine: Machine, material: Material, placement: Placement) -> _Part:
    raw = part.get("items") or []
    if not isinstance(raw, list):
        raise SliceError("the parts must be a list")
    if not raw:
        raise SliceError("add the wing panels and 2D shapes to cut from this block")
    roomy = _roomy(machine)
    # Cut each part on its own well clear of the table (it is stacked afresh when nested).
    base = Placement("xy", placement.root_offset, placement.x_margin, placement.y_offset + LIFT)
    parts: list[_Part] = []
    labels: list[str] = []
    for number, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise SliceError(f"part {number} must be an object")
        source = item.get("source")
        if not isinstance(source, dict) or not isinstance(source.get("part"), dict):
            raise SliceError(f"part {number}: choose a wing panel or a 2D shape")
        sub = dict(source["part"], pair=False)
        kind = sub.get("type", "wing")
        if kind not in ("wing", "shape"):
            raise SliceError(f"part {number}: only wing panels and 2D shapes can be nested")
        title = str(source.get("title") or ("Wing panel" if kind == "wing" else "2D shape"))
        values = {}
        for key, default in (("copies", 1), ("job", 1)):
            value = item.get(key, default)
            if value is None:
                value = default
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
                raise SliceError(f"part {number}: {key} must be a whole number")
            values[key] = int(value)
        copies, job = values["copies"], values["job"]
        if not 1 <= copies <= 50 or job < 0:
            raise SliceError(f"part {number}: copies must be 1 to 50 and the panel 0 or more")
        opposite = item.get("opposite", False)
        if not isinstance(opposite, bool):
            raise SliceError(f"part {number}: opposite hand must be true or false")
        build = _wing_cut if kind == "wing" else _shape_part

        def cut(panel: int, build=build, sub=sub, opposite=opposite, number=number) -> _Part:
            this = dict(sub, job=panel)
            try:
                done = build(this, roomy, material, base)
                return build(this, roomy, material, _opposite(base, done, machine)) if opposite else done
            except (SliceError, PlacementError, ShapeError) as exc:
                raise SliceError(f"part {number}: {exc}") from exc

        first = cut(job or 1)
        panels = list(range(1, len(first.jobs) + 1)) if job == 0 and first.jobs else [job or 1]
        for panel in panels:
            done = first if panel == (job or 1) else cut(panel)
            label = title + (f" panel {panel}" if done.jobs else "") + (" (opposite hand)" if opposite else "")
            for copy_number in range(1, copies + 1):
                parts.append(done)
                labels.append(label + (f" #{copy_number}" if copies > 1 else ""))
    gap = _number(part, "gap", 8.0) or 8.0
    out = combine_parts(parts, labels, machine, placement, gap, placement.y_offset, _number(part, "max_height"))
    out.jobs, out.job, out.views = [], 1, []
    out.stats = {"nest": out.stats["nest"]}
    out.comments.insert(0, f"block from {out.z_root:g} to {out.z_tip:g} mm along the wire")
    order = {}
    for entry in out.stats["nest"]["items"]:
        column = entry["column"]
        order[column] = order.get(column, 0) + 1
        place = "at the bottom" if order[column] == 1 else f"number {order[column]} from the bottom"
        out.notes.append(f"{entry['label']}: column {column} from the back, {place}.")
    out.notes.append("Every part spans the block from end to end; mark each before taking it out.")
    near = out.z_root
    for label, face in (("Near end", True), ("Far end", False)):
        rings = []
        for (zr, zt), r, t in zip(out.solid_z, out.root_regions, out.tip_regions):
            use_root = (abs(zr - near) < abs(zt - near)) == face
            rings += (r if use_root else t).rings()
        out.views.append({"label": label, "rings": rings})
    return out


def _finish(
    project: dict[str, Any], kind: str, machine: Machine, material: Material, placement: Placement, built: _Part
) -> SliceResult:
    toolpath = built.toolpath
    kerf_root, kerf_tip = built.kerf
    ratio_root, ratio_tip = built.ratio
    title = str(project.get("title") or {"wing": "Wing panel", "shape": "2D shape", "fuselage": "Fuselage",
                                         "nest": "Block of parts"}[kind])  # fmt: skip
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
    if built.block is not None:
        block = {k: _r(v) for k, v in built.block.items()}

    warnings = list(built.warnings)
    root_name, tip_name = ("larger", "smaller") if kind == "fuselage" else ("root", "tip")
    slow_end, slow_ratio = (tip_name, ratio_tip) if ratio_tip < ratio_root else (root_name, ratio_root)
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
                {"root": region_json(r), "tip": region_json(tp)}
                | ({"z_root": _r(built.solid_z[k][0]), "z_tip": _r(built.solid_z[k][1])} if built.solid_z else {})
                for k, (r, tp) in enumerate(zip(built.root_regions, built.tip_regions))
            ],
            "skins": [
                {"root": _pts(r), "tip": _pts(tp)}
                | ({"z_root": _r(built.skin_z[k][0]), "z_tip": _r(built.skin_z[k][1])} if built.skin_z else {})
                for k, (r, tp) in enumerate(built.skins)
            ],
            "bays": [
                {
                    "z0": _r(z0),
                    "z1": _r(z1),
                    "root": {"outer": _pts(a), "holes": []},
                    "tip": {"outer": _pts(b), "holes": []},
                }
                for z0, z1, a, b in built.bays
            ],
            "ghosts": [[[[_r(c) for c in p] for p in ring] for ring in ghost] for ghost in built.ghosts],
            "ghost_label": built.ghost_label,
        },
        "steps": built.steps,
        "jig": built.jig,
        "jobs": [{"label": label} for label in built.jobs],
        "job": built.job,
        "notes": built.notes,
        "views": [
            {"label": v["label"], "rings": [_pts(r) for r in v["rings"]]}
            | ({"plan": True} if v.get("plan") else {})
            | {k: [_pts(r) for r in v[k]] for k in ("marks", "lines") if v.get(k)}
            for v in built.views
        ],
        "block": block,
        "path": {"t": times, "xy": xy_pts, "uv": uv_pts, "cut": cut},
    }
    return SliceResult(machine, material, toolpath, gcode, data, built.wing, built.fuselage)


def bay_template(project: dict[str, Any]) -> tuple[str, str]:
    """File name and SVG of the 1:1 servo bay template for a wing project."""
    from .template import bay_template_svg

    result = slice_project(project)
    if result.wing is None or not result.wing.bays:
        raise SliceError("this part has no servo bays")
    title = str(project.get("title") or "Wing panel")
    return f"{title}-servo-bays.svg", bay_template_svg(result.wing, title)


# Fuselages ------------------------------------------------------------------------------------------


def _view_outline(part: dict[str, Any], key: str, label: str, warnings: list[str]) -> list[Point]:
    source = part.get(key)
    if not isinstance(source, dict) or not all(isinstance(source.get(k), str) for k in ("name", "format", "data")):
        raise SliceError(f"load the {label} (an SVG or DXF drawing with one closed outline)")
    if source["format"] not in FORMATS:
        raise SliceError(f"unsupported drawing format {source['format']!r}")
    try:
        regions, _, _ = drawing_regions(source["format"], source["data"], 0.05, 0.2)
    except ShapeError as exc:
        raise SliceError(f"{label}: {exc}") from exc
    outline = max((r.outer for r in regions), key=lambda ring: abs(signed_area(ring)))
    if len(regions) > 1:
        warnings.append(f"The {label} has {len(regions)} outlines: only the largest is used.")
    return outline


def build_fuselage(part: dict[str, Any]) -> tuple[Fuselage, list[str]]:
    """The fuselage described by a project's part (stations or drawings, with wing saddles)."""
    warnings: list[str] = []
    raw = part.get("saddles") or []
    if not isinstance(raw, list):
        raise SliceError("wing saddles must be a list")
    saddles = []
    for number, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise SliceError(f"wing saddle {number} must be an object")
        values = {k: v for k, v in item.items() if k in ("position", "chord", "height", "incidence", "side")}
        for key, value in values.items():
            if key != "side" and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise SliceError(f"wing saddle {number}: {key} must be a number")
        try:
            saddles.append(Saddle(resolve_airfoil(item.get("airfoil") or "naca2412"), **values))
        except (TypeError, ValueError) as exc:
            raise SliceError(f"wing saddle {number}: {exc}") from exc
    source = part.get("source", "stations")
    try:
        if source == "stations":
            stations = _items(part.get("stations") or [], Station, "station")
            if len(stations) < 2:
                raise SliceError("a fuselage needs at least two stations")
            return Fuselage.from_stations(stations, saddles), warnings
        if source == "views":
            side = _view_outline(part, "side_view", "side view", warnings)
            top = _view_outline(part, "top_view", "top view", warnings)
            rt = _number(part, "round_top", 100.0)
            rb = _number(part, "round_bottom", 100.0)
            fuse, more = Fuselage.from_views(side, top, 100.0 if rt is None else rt, 100.0 if rb is None else rb,
                                             saddles)  # fmt: skip
            return fuse, warnings + more
    except SliceError:
        raise
    except ValueError as exc:
        raise SliceError(str(exc)) from exc
    raise SliceError(f"unknown fuselage source {source!r}")


def _decimate(points: list[Point], limit: int = 240) -> list[Point]:
    step = max(1, len(points) // limit)
    return points[::step]


def _fuselage_part(part: dict[str, Any], machine: Machine, material: Material, placement: Placement) -> _Part:
    fuse, warnings = build_fuselage(part)
    halves = part.get("halves", False)
    if not isinstance(halves, bool):
        raise SliceError("halves must be true or false")
    try:
        options = CutOptions(
            method=str(part.get("method", "two_view")),
            segment_length=_number(part, "segment_length", 150.0) or 150.0,
            halves=halves,
            wall=_number(part, "wall", 0.0) or 0.0,
            spare=_number(part, "spare", 5.0) or 5.0,
            points=int(_number(part, "points", 64) or 64),
            facets=int(_number(part, "facets", 8) or 8),
            plate=_number(part, "plate", 6.0) or 0.0,
            hold=_number(part, "hold", 5.0) or 5.0,
            axis=_number(part, "axis", None),
        )
        labels = job_labels(fuse, options)
        job = min(max(int(_number(part, "job", 1) or 1), 1), len(labels))
        cut = build_job(fuse, options, job - 1, machine, placement, lambda ratio: _kerfs(part, material, ratio))
    except PlacementError:
        raise
    except (TypeError, ValueError) as exc:
        raise SliceError(str(exc)) from exc

    lo, hi = fuse.height_range()
    width = fuse.max_width()
    n_vol = 100
    volume = sum(abs(signed_area(fuse.section(fuse.length * (k + 0.5) / n_vol, 32))) for k in range(n_vol))
    volume *= fuse.length / n_vol
    widest = max(fuse.sample_xs(fuse.length / 60), key=lambda x: fuse.width(x) * (fuse.top(x) - fuse.bottom(x)))
    views: list[dict[str, Any]] = [
        {"label": "Side", "rings": [_decimate(fuse.side_outline())]},
        {"label": "Top", "rings": [_decimate(fuse.plan_outline())]},
    ]
    if options.method == "segments":
        xa, xb = fuse.segments(options.segment_length)[(job - 1) // (2 if options.halves else 1)]
        for name, x in (("Front", xa), ("Back", xb)):
            ring = fuse.section(x, 64, minimum=2.0)
            try:
                inner = fuse.section(x, 64, inset=options.wall, minimum=2.0) if options.wall > 0 else None
            except ValueError:
                inner = None
            if options.halves:
                from .fuselage import half_ring

                rings = [half_ring(ring, inner)]
            else:
                rings = [ring] + ([inner] if inner else [])
            views.append({"label": f"{name} · {x:.0f} mm", "rings": rings})
    else:
        views.append({"label": f"Section · {widest:.0f} mm", "rings": [fuse.section(widest, 64)]})
    method = {"two_view": "two views", "turned": f"turned, {options.facets} passes", "segments": "lengthwise segments"}[
        options.method
    ]
    return _Part(
        toolpath=cut.toolpath,
        z_root=cut.z_root,
        z_tip=cut.z_tip,
        root_regions=cut.root_regions,
        tip_regions=cut.tip_regions,
        kerf=cut.kerf,
        ratio=cut.ratio,
        comments=[
            f"fuselage {fuse.length:.0f} mm long, {width:.0f} mm wide, {hi - lo:.0f} mm high, cut as {method}",
            f"cut {job} of {len(labels)}: {cut.label}",
        ],
        warnings=warnings
        + cut.warnings
        + (
            [
                (
                    "Wing saddles are not cut by lengthwise segments: shape them by hand, using the side view in the "
                    "plans (Preview → Plans & formers) as a template."
                )
            ]
            if fuse.saddles and options.method == "segments"
            else []
        ),
        stats={
            "fuselage": {
                "length": round(fuse.length, 1),
                "width": round(width, 1),
                "height": round(hi - lo, 1),
                "volume_l": round(volume / 1e6, 2),
                "method": options.method,
                "cut": cut.label,
            }
        },
        fuselage=fuse,
        jobs=labels,
        job=job,
        notes=cut.notes,
        views=views,
        ghosts=[cut.ghost] if cut.ghost else [],
        block=cut.block,
        steps=cut.steps,
        ghost_label=cut.ghost_label,
        jig=cut.jig,
    )


def fuselage_plans(project: dict[str, Any]) -> tuple[str, str]:
    """File name and SVG of the 1:1 fuselage plans: side and top views and the formers."""
    from .template import fuselage_plans_svg

    part = project.get("part") or {}
    if not isinstance(part, dict) or part.get("type") != "fuselage":
        raise SliceError("the plans are for fuselages")
    fuse, _ = build_fuselage(part)
    title = str(project.get("title") or "Fuselage")
    skin = _number(part, "skin", 0.0) or 0.0
    spacing = _number(part, "segment_length", 150.0) or 150.0
    jig = slice_project(project).data.get("jig") if part.get("method") == "turned" else None
    try:
        svg = fuselage_plans_svg(fuse, title, skin=skin, spacing=spacing, jig=jig)
    except ValueError as exc:
        raise SliceError(str(exc)) from exc
    return f"{title}-plans.svg", svg


def fuselage_stl(project: dict[str, Any]) -> tuple[str, bytes]:
    """File name and binary STL of the finished fuselage (for 3D printing or CAD)."""
    from .fuselage import fuselage_mesh, stl_binary

    part = project.get("part") or {}
    if not isinstance(part, dict) or part.get("type") != "fuselage":
        raise SliceError("the 3D model is for fuselages")
    fuse, _ = build_fuselage(part)
    title = str(project.get("title") or "Fuselage")
    return f"{title}.stl", stl_binary(fuselage_mesh(fuse), f"open_HOWI {title}")
