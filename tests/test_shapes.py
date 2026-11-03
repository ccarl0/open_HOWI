import copy
import itertools
import json
from pathlib import Path

import pytest

from openhowi.drawing import Polyline, ShapeError
from openhowi.geometry import bounds, offset_polygon, point_in_polygon, segments_cross, signed_area
from openhowi.machine import Machine
from openhowi.project import (
    DEFAULT_PARTS,
    DEFAULT_PLACEMENTS,
    SliceError,
    default_project,
    inspect_drawing,
    slice_project,
)
from openhowi.shapes import (
    Region,
    build_regions,
    join_polylines,
    layout_shapes,
    plan_region,
    region_paths,
    shapes_toolpath,
    transform_regions,
)
from openhowi.toolpath import Placement

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "shapes"


def square(x, y, size, ccw=True):
    pts = [(x, y), (x + size, y), (x + size, y + size), (x, y + size)]
    return pts if ccw else pts[::-1]


def shape_project(name, **part):
    project = default_project()
    project["part"] = copy.deepcopy(DEFAULT_PARTS["shape"]) | part
    project["placement"] = copy.deepcopy(DEFAULT_PLACEMENTS["shape"])
    data = (EXAMPLES / name).read_text()
    project["part"]["source"] = {"name": name, "format": name.rsplit(".", 1)[1], "data": data}
    return project


# Geometry helpers -------------------------------------------------------------------------------------


def test_offset_polygon_grows_ccw_square_to_the_right():
    out = offset_polygon(square(0, 0, 10), -1.0)
    assert bounds(out) == pytest.approx((-1, -1, 11, 11))


def test_point_in_polygon_and_crossing():
    sq = square(0, 0, 10)
    assert point_in_polygon((5, 5), sq) and not point_in_polygon((15, 5), sq)
    assert segments_cross((0, 0), (10, 10), (0, 10), (10, 0))
    assert not segments_cross((0, 0), (10, 0), (10, 0), (10, 10))  # touching ends do not count


# Outlines ---------------------------------------------------------------------------------------------


def test_join_line_soup_into_a_closed_outline():
    segs = [((0, 0), (10, 0)), ((10, 10), (10, 0)), ((10, 10), (0, 10)), ((0, 10.05), (0, 0))]  # mixed directions
    closed, leftovers = join_polylines([Polyline(list(s)) for s in segs], gap=0.1)
    assert leftovers == 0 and len(closed) == 1
    assert abs(signed_area(closed[0])) == pytest.approx(100, rel=0.01)


def test_join_reports_open_paths():
    closed, leftovers = join_polylines([Polyline([(0, 0), (10, 0)]), Polyline([(20, 0), (30, 5)])], gap=0.1)
    assert closed == [] and leftovers == 2


def test_regions_nest_holes_and_islands():
    outlines = [square(0, 0, 100), square(10, 10, 80), square(20, 20, 20), square(200, 0, 10)]
    regions = build_regions(outlines)
    assert len(regions) == 3  # big square with a hole, the island inside that hole, the separate small square
    big = max(regions, key=lambda r: abs(signed_area(r.outer)))
    assert len(big.holes) == 1
    assert signed_area(big.outer) > 0 and signed_area(big.holes[0]) < 0


def test_transform_mirror_keeps_orientation_and_moves_to_origin():
    (region,) = transform_regions(build_regions([square(5, 5, 10), square(7, 7, 2)]), 2.0, 90.0, True)
    assert bounds(region.outer) == pytest.approx((0, 0, 20, 20))
    assert signed_area(region.outer) > 0 and signed_area(region.holes[0]) < 0


# Planning ---------------------------------------------------------------------------------------------


def test_entry_avoids_other_shapes_and_bridge_is_short():
    blocker = Region(square(0, -5, 20))  # in the way of the target's lower corners
    target = Region(square(30, 0, 40), [square(40, 3, 10, ccw=False)])
    plan = plan_region(target, 1, [blocker, target], x_far=-10)
    assert not plan.shadowed
    entry = target.outer[plan.entry]
    assert entry[1] > 15  # the corner at y=0 is behind the blocker; the top-left corner is clear
    ((hole, spot_outer, spot_hole),) = plan.bridges
    assert hole == 1
    assert spot_outer[1] > 0  # lands part-way along the bottom edge, not on a corner
    assert target.holes[0][spot_hole[0]][1] == 3  # starts from a point on the hole's bottom edge
    root, _ = region_paths(plan, target, target)
    assert root[0] == root[-1] == entry
    # the bridge drops straight down from the hole to the bottom edge (3 mm) rather than to a corner
    jumps = [(a, b) for a, b in itertools.pairwise(root) if (a[1] == 0) != (b[1] == 0) and abs(a[0] - b[0]) < 1e-9]
    assert any(abs(a[1] - b[1]) == pytest.approx(3) for a, b in jumps)


def test_shadowed_shape_is_reported():
    wall = Region(square(0, -100, 10)[:2] + [(10, 200), (0, 200)])
    target = Region(square(30, 0, 10))
    assert plan_region(target, 1, [wall, target], x_far=-10).shadowed


def test_layout_kerf_and_tiny_holes():
    machine = Machine(tower_distance=1000, x_travel=500, y_travel=300)
    slot = [(5, 40), (5, 40.8), (35, 40.8), (35, 40)]  # 0.8 mm wide: narrower than the 1 mm kerf
    regions = [Region(square(0, 0, 50), [square(10, 10, 20, ccw=False), square(40, 40, 0.5, ccw=False), slot])]
    layout = layout_shapes(regions, machine, Placement(root_offset=100, x_margin=20, y_offset=20), 100,
                           root_kerf=1.0, tip_kerf=1.0)  # fmt: skip
    assert any("Skipped 2 hole(s) narrower than the kerf" in w for w in layout.warnings)
    assert len(layout.root_regions[0].holes) == 1
    xy, uv = layout.paths[0]
    assert xy == uv  # no taper: both carriages follow the same path
    xs = [p[0] for p in xy]
    ys = [p[1] for p in xy]
    assert min(xs) == pytest.approx(20)  # rearmost cut point at x_margin
    assert min(ys) == pytest.approx(19.5)  # shape bottom at y_offset, wire half a kerf below it
    assert max(xs) - min(xs) == pytest.approx(51)
    toolpath = shapes_toolpath(layout, machine)
    assert toolpath.moves[0].xy[0] == 0 and toolpath.moves[-1].xy == (0, 0)


def test_taper_too_steep_for_position():
    machine = Machine(tower_distance=1000, x_travel=500, y_travel=300)
    regions = [Region(square(0, 0, 50))]
    with pytest.raises(ShapeError, match="too steep"):
        layout_shapes(regions, machine, Placement(root_offset=100), 100, tip_scale=0.7)


# Projects -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name, shapes, holes", [("former.svg", 1, 3), ("star_and_d.dxf", 2, 1)])
def test_example_drawings_slice(name, shapes, holes):
    data = slice_project(shape_project(name)).data
    assert data["kind"] == "shape"
    drawing = data["stats"]["drawing"]
    assert (drawing["shapes"], drawing["holes"], drawing["open_paths"]) == (shapes, holes, 0)
    assert len(data["part"]["solids"]) == shapes
    assert any("slit" in w for w in data["warnings"])
    json.dumps(data)


def test_shape_options_change_the_cut():
    base = slice_project(shape_project("former.svg")).data
    bigger = slice_project(shape_project("former.svg", scale=150)).data
    assert bigger["stats"]["drawing"]["size"] == pytest.approx([150, 150])
    assert bigger["stats"]["time_s"] > base["stats"]["time_s"]
    tapered = slice_project(shape_project("former.svg", tip_scale=90)).data
    assert tapered["stats"]["kerf"]["tip"] > tapered["stats"]["kerf"]["root"]  # slower, narrower end melts more
    assert tapered["stats"]["extents"]["XY"] != tapered["stats"]["extents"]["UV"]


def test_shape_errors():
    project = shape_project("former.svg")
    project["part"]["source"] = None
    with pytest.raises(SliceError, match="load a DXF or SVG"):
        slice_project(project)
    project["part"]["source"] = {"name": "x.pdf", "format": "pdf", "data": ""}
    with pytest.raises(SliceError, match="unsupported"):
        slice_project(project)
    project["part"]["source"] = {"name": "open.svg", "format": "svg",
                                 "data": '<svg xmlns="http://www.w3.org/2000/svg"><line x1="0" y1="0" x2="9" y2="9"/></svg>'}  # fmt: skip
    with pytest.raises(SliceError, match="no closed outlines"):
        slice_project(project)
    with pytest.raises(SliceError, match="too steep"):
        slice_project(shape_project("former.svg", tip_scale=50))


def test_inspect_drawing():
    info = inspect_drawing("star_and_d.dxf", (EXAMPLES / "star_and_d.dxf").read_text())
    assert info["format"] == "dxf" and info["shapes"] == 2 and info["holes"] == 1
    assert any("TEXT" in w for w in info["warnings"])
    with pytest.raises(SliceError, match="svg or .dxf"):
        inspect_drawing("drawing.pdf", "")
