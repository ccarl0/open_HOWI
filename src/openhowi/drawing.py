# SPDX-License-Identifier: GPL-3.0-or-later
"""Result type shared by the DXF and SVG importers."""

from __future__ import annotations

from dataclasses import dataclass, field

from .geometry import Point


class ShapeError(ValueError):
    """A drawing could not be read or turned into cuttable shapes."""


@dataclass
class Polyline:
    points: list[Point]
    closed: bool = False


@dataclass
class Drawing:
    """Flattened geometry in millimetres, y up."""

    polylines: list[Polyline] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
