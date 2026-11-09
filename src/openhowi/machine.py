# SPDX-License-Identifier: GPL-3.0-or-later
"""Description of a 4-axis hot-wire machine."""

from __future__ import annotations

from dataclasses import dataclass

FEED_MODES = ("inverse-time", "units-per-minute")
AXIS_WORDS = "XYZABCUVW"


@dataclass(frozen=True)
class Machine:
    """A 4-axis hot-wire cutter: two carriages (XY and UV) holding the wire ends.

    Machine coordinates: X horizontal (towards the foam), Y vertical, origin at
    the lower corner nearest the operator. The XY carriage sits at span
    position 0 and the UV carriage at ``tower_distance``.
    """

    tower_distance: float = 1000.0
    """Distance between the XY and UV carriages (the free wire length) in mm."""
    x_travel: float = 600.0
    y_travel: float = 300.0
    axis_letters: str = "XYUV"
    """G-code letters for the XY-carriage horizontal/vertical and UV-carriage horizontal/vertical axes."""
    cut_feed: float = 200.0
    """Cutting speed in mm/min: the wire's speed through the foam at the faster end of the part
    (or of the faster carriage when the toolpath doesn't say where the foam is)."""
    travel_feed: float = 600.0
    """Speed in mm/min for moves through air."""
    feed_mode: str = "inverse-time"
    """``inverse-time`` (G93, recommended) or ``units-per-minute`` (G94 with a compensated F)."""
    wire_power: float | None = None
    """S value sent with M3 to heat the wire; ``None`` means the wire is switched by hand."""
    preheat: float = 0.0
    """Seconds to dwell after switching the wire on, before moving."""
    decimals: int = 3
    rotary_axis: str = ""
    """G-code letter of a rotary axis (spinning table) turning the foam about a line along X, or ""."""
    rotary_height: float = 0.0
    """Machine Y of the rotary axis (mm)."""

    def __post_init__(self) -> None:
        letters = self.axis_letters.upper()
        if len(letters) != 4 or len(set(letters)) != 4 or not set(letters) <= set(AXIS_WORDS):
            raise ValueError(f"axis_letters must be 4 distinct letters from {AXIS_WORDS}, got {self.axis_letters!r}")
        object.__setattr__(self, "axis_letters", letters)
        rotary = (self.rotary_axis or "").strip().upper()
        if rotary and (len(rotary) != 1 or rotary not in AXIS_WORDS or rotary in letters):
            raise ValueError(f"rotary_axis must be one letter from {AXIS_WORDS} not used by the carriages, or blank")
        object.__setattr__(self, "rotary_axis", rotary)
        if self.rotary_height < 0:
            raise ValueError("rotary_height must be >= 0")
        for name in ("tower_distance", "x_travel", "y_travel", "cut_feed", "travel_feed"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")
        if self.feed_mode not in FEED_MODES:
            raise ValueError(f"feed_mode must be one of {', '.join(FEED_MODES)}")
        if self.wire_power is not None and self.wire_power < 0:
            raise ValueError("wire_power must be >= 0")
        if self.preheat < 0:
            raise ValueError("preheat must be >= 0")
        if not 0 <= self.decimals <= 6:
            raise ValueError("decimals must be between 0 and 6")
