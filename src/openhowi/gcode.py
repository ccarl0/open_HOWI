"""G-code output for 4-axis hot-wire toolpaths.

Each segment moves both carriages, usually by different distances. The feed is
set so that the carriage travelling farther moves at the requested speed and
both arrive at the same time:

* ``inverse-time`` (G93): ``F = 1 / segment_minutes``. Every controller
  interprets this the same way, so it is the default.
* ``units-per-minute`` (G94): F is scaled for controllers that measure feed
  along the combined 4-axis vector (e.g. GRBL).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from .geometry import Point, distance
from .machine import Machine
from .toolpath import Toolpath


def fmt(value: float, decimals: int) -> str:
    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def segment_feed(d_xy: float, d_uv: float, feed: float, mode: str) -> float:
    """F word for a segment whose longest carriage move should run at ``feed`` mm/min."""
    longest = max(d_xy, d_uv)
    if longest <= 0.0:
        raise ValueError("zero-length segment")
    if mode == "inverse-time":
        return feed / longest
    if mode == "units-per-minute":
        return feed * math.hypot(d_xy, d_uv) / longest
    raise ValueError(f"unknown feed mode {mode!r}")


@dataclass(frozen=True)
class Segment:
    xy: Point
    uv: Point
    d_xy: float
    d_uv: float
    feed: float
    """Target speed (mm/min) of the farther-travelling carriage."""
    cutting: bool

    @property
    def minutes(self) -> float:
        return max(self.d_xy, self.d_uv) / self.feed


def segments(toolpath: Toolpath, machine: Machine) -> Iterator[Segment]:
    """Moves rounded to output precision, with zero-length moves dropped."""

    def rnd(p: Point) -> Point:
        return (round(p[0], machine.decimals), round(p[1], machine.decimals))

    pos_xy, pos_uv = rnd(toolpath.start[0]), rnd(toolpath.start[1])
    for move in toolpath.moves:
        xy, uv = rnd(move.xy), rnd(move.uv)
        d_xy, d_uv = distance(pos_xy, xy), distance(pos_uv, uv)
        if max(d_xy, d_uv) == 0.0:
            continue
        feed = machine.cut_feed if move.cutting else machine.travel_feed
        yield Segment(xy, uv, d_xy, d_uv, feed, move.cutting)
        pos_xy, pos_uv = xy, uv


def job_minutes(toolpath: Toolpath, machine: Machine) -> float:
    return sum(seg.minutes for seg in segments(toolpath, machine))


def _comment(text: str) -> str:
    return "(" + text.replace("(", "[").replace(")", "]") + ")"


def generate_gcode(toolpath: Toolpath, machine: Machine, comments: Iterable[str] = ()) -> str:
    a = machine.axis_letters
    dec = machine.decimals
    inverse = machine.feed_mode == "inverse-time"
    f_dec = 5 if inverse else 2

    def coords(xy: Point, uv: Point) -> str:
        return f"{a[0]}{fmt(xy[0], dec)} {a[1]}{fmt(xy[1], dec)} {a[2]}{fmt(uv[0], dec)} {a[3]}{fmt(uv[1], dec)}"

    lines = [_comment("open_HOWI hot-wire G-code")]
    lines += [_comment(c) for c in comments]
    lines += [
        _comment(
            f"{a[0]}{a[1]} carriage at span 0, {a[2]}{a[3]} carriage at span {fmt(machine.tower_distance, dec)} mm"
        ),
        _comment("start with both carriages at the origin, wire below and behind the foam"),
        "G21 G90 G94",
        f"G1 {coords(*toolpath.start)} F{fmt(machine.travel_feed, 2)}",
    ]
    if inverse:
        lines.append("G93")
    if machine.wire_power is not None:
        lines.append(f"M3 S{fmt(machine.wire_power, 2)}")
        if machine.preheat > 0:
            lines.append(f"G4 P{fmt(machine.preheat, 2)}")
    else:
        lines.append(_comment("switch the wire heat on now"))

    for seg in segments(toolpath, machine):
        feed = segment_feed(seg.d_xy, seg.d_uv, seg.feed, machine.feed_mode)
        lines.append(f"G1 {coords(seg.xy, seg.uv)} F{fmt(feed, f_dec)}")

    if machine.wire_power is not None:
        lines.append("M5")
    if inverse:
        lines.append("G94")
    lines.append("M2")
    return "\n".join(lines) + "\n"
