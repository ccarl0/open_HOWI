# SPDX-License-Identifier: GPL-3.0-or-later
"""Machine and material presets: built-in starting points plus the user's own.

User presets and the airfoil library live in the per-user config directory
(``OPENHOWI_CONFIG_DIR`` overrides it):

* Linux: ``$XDG_CONFIG_HOME/openhowi`` or ``~/.config/openhowi``
* macOS: ``~/Library/Application Support/openhowi``
* Windows: ``%APPDATA%\\openhowi``
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

from .airfoil import AirfoilError, parse_dat
from .machine import Machine
from .material import Material

KINDS = ("machine", "material")

# Machine preset keys that are not part of the Machine dataclass.
MACHINE_EXTRA = {"baud": 115200}
MACHINE_KEYS = ("tower_distance", "x_travel", "y_travel", "axis_letters", "travel_feed", "feed_mode", "decimals")

MACHINE_FIELDS: list[dict[str, Any]] = [
    {"key": "tower_distance", "label": "Distance between carriages", "unit": "mm", "type": "number", "min": 1,
     "help": "Free length of the wire between the XY and UV carriages."},
    {"key": "x_travel", "label": "Horizontal travel (X/U)", "unit": "mm", "type": "number", "min": 1},
    {"key": "y_travel", "label": "Vertical travel (Y/V)", "unit": "mm", "type": "number", "min": 1},
    {"key": "axis_letters", "label": "Axis letters", "type": "text",
     "help": "G-code letters for the XY carriage (horizontal, vertical) then the UV carriage, e.g. XYUV or XYZA."},
    {"key": "travel_feed", "label": "Travel speed", "unit": "mm/min", "type": "number", "min": 1,
     "help": "Speed for moves through air."},
    {"key": "feed_mode", "label": "Feed mode", "type": "select",
     "options": [["inverse-time", "Inverse time (G93)"], ["units-per-minute", "Units per minute (G94)"]],
     "help": "G93 works the same on every controller. Use G94 only if yours lacks G93."},
    {"key": "decimals", "label": "Coordinate decimals", "type": "number", "min": 0, "max": 6, "step": 1},
    {"key": "baud", "label": "Serial baud rate", "type": "number", "min": 1200, "step": 1},
]  # fmt: skip

MATERIAL_FIELDS: list[dict[str, Any]] = [
    {"key": "cut_feed", "label": "Cut speed", "unit": "mm/min", "type": "number", "min": 1,
     "help": "Speed of the wire through the foam at the faster end of the part."},
    {"key": "kerf", "label": "Kerf at cut speed", "unit": "mm", "type": "number", "min": 0, "step": 0.05,
     "help": "Width of the slot the wire melts. Measure it with Calibration → Kerf test."},
    {"key": "kerf_half_speed", "label": "Kerf at half speed", "unit": "mm", "type": "number", "min": 0,
     "step": 0.05, "optional": True,
     "help": "Slot width at half the cut speed. Used for the slower end of tapered parts. Blank: same as above."},
    {"key": "wire_power", "label": "Wire power (S)", "type": "number", "min": 0, "optional": True,
     "help": "S value sent with M3 if your controller switches the wire. Blank: switch the wire by hand."},
    {"key": "preheat", "label": "Preheat", "unit": "s", "type": "number", "min": 0, "step": 0.5,
     "help": "Wait after switching the wire on, before moving."},
]  # fmt: skip

BUILTIN: dict[str, list[dict[str, Any]]] = {
    "machine": [
        {"name": "Generic 4-axis, 1000 mm wire (XYUV)",
         "values": {"tower_distance": 1000.0, "x_travel": 600.0, "y_travel": 300.0, "axis_letters": "XYUV",
                    "travel_feed": 800.0, "feed_mode": "inverse-time", "decimals": 3, "baud": 115200}},
        {"name": "Generic 4-axis, 1000 mm wire (XYZA)",
         "values": {"tower_distance": 1000.0, "x_travel": 600.0, "y_travel": 300.0, "axis_letters": "XYZA",
                    "travel_feed": 800.0, "feed_mode": "inverse-time", "decimals": 3, "baud": 115200}},
        {"name": "Large 4-axis, 1500 mm wire (XYUV)",
         "values": {"tower_distance": 1500.0, "x_travel": 1000.0, "y_travel": 400.0, "axis_letters": "XYUV",
                    "travel_feed": 800.0, "feed_mode": "inverse-time", "decimals": 3, "baud": 115200}},
    ],
    # Starting points only: kerf and speed depend on wire, temperature and foam. Calibrate.
    "material": [
        {"name": "EPS white, 15-20 kg/m³",
         "values": {"cut_feed": 200.0, "kerf": 1.2, "kerf_half_speed": 1.7, "wire_power": None, "preheat": 3.0}},
        {"name": "XPS extruded (blue/pink)",
         "values": {"cut_feed": 150.0, "kerf": 0.9, "kerf_half_speed": 1.3, "wire_power": None, "preheat": 3.0}},
        {"name": "EPP",
         "values": {"cut_feed": 90.0, "kerf": 1.4, "kerf_half_speed": 2.0, "wire_power": None, "preheat": 5.0}},
    ],
}  # fmt: skip


class PresetError(ValueError):
    pass


def config_dir() -> Path:
    override = os.environ.get("OPENHOWI_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "openhowi"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "openhowi"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "openhowi"


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise PresetError("preset name must contain letters or digits")
    return slug[:80]


def machine_from_values(values: dict[str, Any], material: Material) -> Machine:
    unknown = set(values) - set(MACHINE_KEYS) - set(MACHINE_EXTRA)
    if unknown:
        raise PresetError(f"unknown machine setting(s): {', '.join(sorted(unknown))}")
    kwargs = {k: values[k] for k in MACHINE_KEYS if k in values}
    try:
        return Machine(**kwargs, cut_feed=material.cut_feed, wire_power=material.wire_power, preheat=material.preheat)
    except (TypeError, ValueError) as exc:
        raise PresetError(f"machine: {exc}") from exc


def material_from_values(values: dict[str, Any]) -> Material:
    allowed = {f.name for f in fields(Material)}
    unknown = set(values) - allowed
    if unknown:
        raise PresetError(f"unknown material setting(s): {', '.join(sorted(unknown))}")
    try:
        return Material(**values)
    except (TypeError, ValueError) as exc:
        raise PresetError(f"material: {exc}") from exc


def validate(kind: str, values: dict[str, Any]) -> None:
    if kind == "material":
        material_from_values(values)
    elif kind == "machine":
        machine_from_values(values, Material())
        baud = values.get("baud", MACHINE_EXTRA["baud"])
        if not isinstance(baud, int) or baud <= 0:
            raise PresetError("machine: baud must be a positive whole number")
    else:
        raise PresetError(f"unknown preset kind {kind!r}")


class PresetStore:
    def __init__(self, root: Path | None = None):
        self.root = root or config_dir()

    def _dir(self, kind: str) -> Path:
        if kind not in KINDS:
            raise PresetError(f"unknown preset kind {kind!r}")
        return self.root / "presets" / f"{kind}s"

    def list(self, kind: str) -> list[dict[str, Any]]:
        directory = self._dir(kind)
        presets = [{**p, "builtin": True} for p in BUILTIN[kind]]
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    validate(kind, data["values"])
                    presets.append({"name": str(data["name"]), "values": data["values"], "builtin": False})
                except (OSError, ValueError, KeyError, TypeError):
                    continue  # skip unreadable or outdated presets rather than failing the whole list
        return presets

    def save(self, kind: str, name: str, values: dict[str, Any]) -> None:
        directory = self._dir(kind)
        name = name.strip()
        if any(p["name"] == name for p in BUILTIN[kind]):
            raise PresetError(f"{name!r} is a built-in preset; save it under a new name")
        validate(kind, values)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_slug(name)}.json"
        path.write_text(json.dumps({"name": name, "values": values}, indent=2), encoding="utf-8")

    def delete(self, kind: str, name: str) -> None:
        path = self._dir(kind) / f"{_slug(name)}.json"
        if not path.is_file():
            raise PresetError(f"no user preset named {name!r}")
        path.unlink()

    # Airfoil library -------------------------------------------------------

    @property
    def airfoil_dir(self) -> Path:
        return self.root / "airfoils"

    def airfoils(self) -> list[dict[str, str]]:
        if not self.airfoil_dir.is_dir():
            return []
        out = []
        for path in sorted(self.airfoil_dir.glob("*.dat")):
            try:
                foil = parse_dat(path.read_text(encoding="utf-8", errors="replace"), default_name=path.stem)
            except (OSError, AirfoilError):
                continue
            out.append({"file": path.name, "name": foil.name})
        return out

    def airfoil_text(self, file: str) -> str:
        path = self.airfoil_dir / Path(file).name
        if path.suffix.lower() != ".dat" or not path.is_file():
            raise PresetError(f"no airfoil file {file!r} in the library")
        return path.read_text(encoding="utf-8", errors="replace")

    def add_airfoil(self, filename: str, text: str) -> dict[str, str]:
        foil = parse_dat(text, default_name=Path(filename).stem)  # validates
        self.airfoil_dir.mkdir(parents=True, exist_ok=True)
        path = self.airfoil_dir / f"{_slug(Path(filename).stem)}.dat"
        path.write_text(text, encoding="utf-8")
        return {"file": path.name, "name": foil.name}
