# SPDX-License-Identifier: GPL-3.0-or-later
"""Calibration cuts."""

from __future__ import annotations

from .gcode import generate_gcode
from .machine import Machine
from .toolpath import Move, Toolpath, check_limits

KERF_TEST_WIDTH = 60.0
KERF_TEST_HEIGHT = 30.0


def kerf_test(machine: Machine, x_start: float = 20.0, y_start: float = 20.0, gap: float = 20.0) -> tuple[str, str]:
    """G-code and instructions for measuring kerf at full and half cut speed.

    Cuts two rectangles straight across the whole wire, the first at the cut
    speed and the second at half of it. Each piece comes out narrower than the
    wire path by exactly one kerf.
    """
    w, h = KERF_TEST_WIDTH, KERF_TEST_HEIGHT
    moves = [Move((0.0, y_start), (0.0, y_start), cutting=False)]
    for i, scale in enumerate((1.0, 0.5)):
        x0 = x_start + i * (w + gap)
        rect = [(x0, y_start), (x0, y_start + h), (x0 + w, y_start + h), (x0 + w, y_start), (x0, y_start)]
        moves += [Move(p, p, cutting=True, feed_scale=scale) for p in rect]
        moves.append(Move((0.0, y_start), (0.0, y_start), cutting=True))
    moves.append(Move((0.0, 0.0), (0.0, 0.0), cutting=False))
    toolpath = Toolpath(tuple(moves))
    check_limits(toolpath, machine)

    depth = x_start + 2 * w + gap + 10
    gcode = generate_gcode(
        toolpath,
        machine,
        [
            "Kerf test: piece A at the cut speed, piece B at half speed",
            f"block at least {depth:g} mm deep and {y_start + h + 10:g} mm tall, rear face between X=0 and X={x_start:g}",
            f"kerf = {w:g} - measured piece width",
        ],
    )
    instructions = (
        f"Use a foam block at least {depth:g} mm deep (X) and {y_start + h + 10:g} mm tall, with its rear face "
        f"between X=0 and X={x_start:g} mm and its bottom below Y={y_start:g} mm. The cut produces two "
        f"{w:g} × {h:g} mm pieces: A (nearer the origin) at the cut speed, B at half speed. Measure each piece's "
        f"width (along X) with calipers. Kerf = {w:g} − width. Enter A's value as “Kerf at cut speed” and B's as "
        "“Kerf at half speed” in the material preset."
    )
    return gcode, instructions
