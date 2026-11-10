import math
import re

import pytest

from openhowi.airfoil import naca4
from openhowi.gcode import fmt, generate_gcode, job_minutes, segment_feed, segments
from openhowi.machine import Machine
from openhowi.toolpath import (
    MachineLimitError,
    Move,
    Placement,
    PlacementError,
    Toolpath,
    build_wing_toolpath,
)
from openhowi.wing import WingPanel

MACHINE = Machine(tower_distance=1000, x_travel=500, y_travel=250, cut_feed=200, travel_feed=600, wire_power=500)
WING = WingPanel(
    naca4("2412"), root_chord=250, span=600, tip_chord=160, sweep=40, washout=1.5, root_kerf=1.0, tip_kerf=1.3
)
PLACE = Placement(root_offset=200, x_margin=25, y_offset=40)


def test_wing_toolpath_shape():
    tp = build_wing_toolpath(WING, MACHINE, PLACE)
    first, last = tp.moves[0], tp.moves[-1]
    assert first.xy[0] == 0 and first.uv[0] == 0 and not first.cutting
    assert last.xy == (0, 0) and last.uv == (0, 0) and not last.cutting
    # Trailing edge sits at x_margin, leading edge further along +X.
    cut = [m for m in tp.moves if m.cutting][:-1]
    assert min(m.xy[0] for m in cut) == pytest.approx(25)
    le = max(cut, key=lambda m: m.xy[0])
    assert le.xy[0] > 250
    # Root is on the XY side and bigger than the tip, so the XY path is longer.
    assert max(m.xy[0] for m in cut) > max(m.uv[0] for m in cut)


def test_root_on_uv_side_swaps_paths():
    a = build_wing_toolpath(WING, MACHINE, PLACE)
    b = build_wing_toolpath(WING, MACHINE, Placement(root_side="uv", root_offset=200, x_margin=25, y_offset=40))
    for ma, mb in zip(a.moves, b.moves):
        assert ma.xy == pytest.approx(mb.uv)
        assert ma.uv == pytest.approx(mb.xy)


def test_panel_must_fit_between_towers():
    with pytest.raises(PlacementError):
        build_wing_toolpath(WING, MACHINE, Placement(root_offset=500))


def test_machine_limits_are_enforced():
    small = Machine(tower_distance=1000, x_travel=200, y_travel=250)
    with pytest.raises(MachineLimitError, match="horizontal"):
        build_wing_toolpath(WING, small, PLACE)
    with pytest.raises(MachineLimitError, match="vertical"):
        build_wing_toolpath(WING, MACHINE, Placement(root_offset=200, y_offset=0.5))


def test_segment_feed_modes():
    assert segment_feed(100, 50, 200, "inverse-time") == pytest.approx(2.0)
    assert segment_feed(30, 40, 100, "units-per-minute") == pytest.approx(100 * 50 / 40)
    with pytest.raises(ValueError):
        segment_feed(0, 0, 100, "inverse-time")


def test_fmt():
    assert fmt(1.5, 3) == "1.5"
    assert fmt(2.0, 3) == "2"
    assert fmt(-0.0001, 3) == "0"
    assert fmt(12.34567, 3) == "12.346"


GCODE_LINE = re.compile(r"G1 X(?P<x>[-\d.]+) Y(?P<y>[-\d.]+) U(?P<u>[-\d.]+) V(?P<v>[-\d.]+) F(?P<f>[\d.]+)")


def _moves(gcode):
    return [{k: float(v) for k, v in m.groupdict().items()} for m in GCODE_LINE.finditer(gcode)]


def test_inverse_time_gcode_synchronises_carriages():
    tp = Toolpath(
        (
            Move((0, 10), (0, 10), cutting=False),
            Move((100, 10), (50, 10), cutting=True),
            Move((100, 10), (50, 10), cutting=True),  # duplicate: dropped
            Move((0, 0), (0, 0), cutting=False),
        )
    )
    gcode = generate_gcode(tp, MACHINE, ["test job"])
    lines = gcode.splitlines()
    assert "(test job)" in lines
    assert lines.index("G93") < lines.index("M3 S500") < lines.index("M5") < lines.index("G94") < lines.index("M2")
    moves = _moves(gcode)[1:]  # skip the G94 move to the origin
    assert len(moves) == 3
    # 10 mm rise in air at 600 mm/min; 100 mm cut on the longer side at 200 mm/min.
    assert moves[0]["f"] == pytest.approx(60)
    assert moves[1]["f"] == pytest.approx(2)
    assert job_minutes(tp, MACHINE) == pytest.approx(10 / 600 + 100 / 200 + math.hypot(100, 10) / 600)


def test_units_per_minute_gcode():
    machine = Machine(feed_mode="units-per-minute", cut_feed=200, travel_feed=600)
    tp = Toolpath((Move((30, 0), (0, 40), cutting=True),))
    gcode = generate_gcode(tp, machine)
    assert "G93" not in gcode
    assert "M3" not in gcode and "(switch the wire heat on now)" in gcode
    assert _moves(gcode)[-1]["f"] == pytest.approx(200 * 50 / 40)


def test_custom_axis_letters_and_comment_sanitising():
    machine = Machine(axis_letters="xyza")
    gcode = generate_gcode(Toolpath((Move((1, 2), (3, 4), True),)), machine, ["a (nested) comment"])
    assert "G1 X1 Y2 Z3 A4 F" in gcode
    assert "(a [nested] comment)" in gcode


@pytest.mark.parametrize("letters", ["XYU", "XYXY", "XYUF"])
def test_bad_axis_letters(letters):
    with pytest.raises(ValueError):
        Machine(axis_letters=letters)


def test_segments_skip_moves_lost_to_rounding():
    tp = Toolpath((Move((0.0001, 0), (0, -0.0004), True), Move((5, 0), (5, 0), True)))
    assert len(list(segments(tp, Machine()))) == 1


def test_full_wing_gcode_stays_in_limits():
    gcode = generate_gcode(build_wing_toolpath(WING, MACHINE, PLACE), MACHINE)
    for m in _moves(gcode):
        assert 0 <= m["x"] <= 500 and 0 <= m["u"] <= 500
        assert 0 <= m["y"] <= 250 and 0 <= m["v"] <= 250
        assert m["f"] > 0


def test_comments_are_plain_ascii():
    gcode = generate_gcode(Toolpath((Move((1, 2), (3, 4), True),)), Machine(), ["EPS 20 kg/m³; white"])
    assert "(EPS 20 kg/m3, white)" in gcode
    gcode.encode("ascii")
