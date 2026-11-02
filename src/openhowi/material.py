# SPDX-License-Identifier: GPL-3.0-or-later
"""Foam material settings and the kerf model."""

from __future__ import annotations

from dataclasses import dataclass

MIN_SPEED_RATIO = 0.2


@dataclass(frozen=True)
class Material:
    """How to cut one kind of foam with one wire setup.

    Kerf is the width of the slot the wire melts. It grows as the wire slows
    down, which matters on tapered parts where one end moves more slowly than
    the other. Measure both values with the kerf calibration cut.
    """

    cut_feed: float = 200.0
    """Speed of the wire through the foam at the faster end of the part (mm/min)."""
    kerf: float = 1.0
    """Slot width at the cut speed (mm)."""
    kerf_half_speed: float | None = None
    """Slot width at half the cut speed (mm); ``None`` assumes the kerf does not change with speed."""
    wire_power: float | None = None
    """S value sent with M3; ``None`` means the wire is switched on by hand."""
    preheat: float = 0.0
    """Seconds to wait after switching the wire on."""

    def __post_init__(self) -> None:
        if self.cut_feed <= 0:
            raise ValueError("cut_feed must be > 0")
        if self.kerf < 0:
            raise ValueError("kerf must be >= 0")
        if self.kerf_half_speed is not None and self.kerf_half_speed < 0:
            raise ValueError("kerf_half_speed must be >= 0")
        if self.wire_power is not None and self.wire_power < 0:
            raise ValueError("wire_power must be >= 0")
        if self.preheat < 0:
            raise ValueError("preheat must be >= 0")

    def kerf_at(self, speed_ratio: float) -> float:
        """Kerf when the wire moves at ``speed_ratio`` times the cut speed.

        Linear through the two measured points (full and half speed),
        extrapolated down to :data:`MIN_SPEED_RATIO` and never below ``kerf``.
        """
        if self.kerf_half_speed is None:
            return self.kerf
        r = min(1.0, max(MIN_SPEED_RATIO, speed_ratio))
        return max(self.kerf, self.kerf + (self.kerf_half_speed - self.kerf) * (1.0 - r) / 0.5)
