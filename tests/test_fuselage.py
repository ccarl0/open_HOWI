import copy
import json
import math
import xml.dom.minidom
from pathlib import Path

import pytest

from openhowi import airfoildb
from openhowi.cli import main
from openhowi.fusecut import CutOptions, build_job, job_labels
from openhowi.fuselage import Fuselage, FuselageError, Saddle, Station, half_ring
from openhowi.geometry import signed_area
from openhowi.machine import Machine
from openhowi.project import (
    DEFAULT_PARTS,
    DEFAULT_PLACEMENTS,
    SliceError,
    build_fuselage,
    default_project,
    fuselage_plans,
    slice_project,
)
from openhowi.toolpath import Placement

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
STATIONS = [
    Station(0, 22, -22, 44),
    Station(30, 34, -30, 58),
    Station(120, 48, -38, 72, 80, 50),
    Station(250, 44, -34, 64, 80, 50),
    Station(400, 30, -10, 34, 70, 60),
    Station(500, 22, 6, 16, 60, 60),
]
MACHINE = Machine()
PLACEMENT = Placement("xy", 30, 20, 10)


def fuselage(**kw):
    return Fuselage.from_stations(STATIONS, **kw)


def no_kerf(ratio):
    return (0.0, 0.0)


def test_lines_pass_through_the_stations_without_overshoot():
    f = fuselage()
    for s in STATIONS:
        assert f.top(s.x) == pytest.approx(s.top) and f.bottom(s.x) == pytest.approx(s.bottom)
        assert f.width(s.x) == pytest.approx(s.width)
    # between two stations the line stays between their values (shape preserving)
    for x in range(120, 250, 10):
        assert 64 - 1e-9 <= f.width(x) <= 72 + 1e-9
    assert f.length == 500 and f.max_width() == pytest.approx(72)
    with pytest.raises(FuselageError, match="at least two"):
        Fuselage.from_stations(STATIONS[:1])
    with pytest.raises(ValueError, match="top must be above bottom"):
        Station(0, -5, 5, 10)


def test_sections_match_the_lines_and_roundness():
    f = fuselage()
    s = f.section(120, 64)
    assert len(s) == 64 and signed_area(s) > 0
    assert s[0] == pytest.approx((0, -38), abs=1e-6)  # bottom centre first
    assert s[16] == pytest.approx((36, 5), abs=1e-6)  # right side, at mid height
    assert s[32] == pytest.approx((0, 48), abs=1e-6)  # top centre
    us, vs = [p[0] for p in s], [p[1] for p in s]
    assert max(us) - min(us) == pytest.approx(72, abs=1e-6) and max(vs) - min(vs) == pytest.approx(86, abs=1e-6)
    # 100 % round: an ellipse; 0 %: nearly the full rectangle
    box = Fuselage.from_stations([Station(0, 10, -10, 40, 0, 0), Station(100, 10, -10, 40, 0, 0)])
    oval = Fuselage.from_stations([Station(0, 10, -10, 40), Station(100, 10, -10, 40)])
    assert abs(signed_area(oval.section(50, 256))) == pytest.approx(math.pi * 20 * 10, rel=0.01)
    assert abs(signed_area(box.section(50, 256))) == pytest.approx(40 * 20, rel=0.03)
    with pytest.raises(ValueError, match="multiple of 4"):
        f.section(100, 30)


def test_wall_inset_and_half_shells():
    f = fuselage()
    outer = f.section(250, 64)
    inner = f.section(250, 64, inset=3)
    walls = [min(math.dist(p, q) for q in outer) for p in inner]
    assert min(walls) == pytest.approx(3, abs=0.05)
    half = half_ring(outer)
    assert len(half) == 33 and all(u >= 0 for u, _ in half)
    shell = half_ring(outer, inner)
    assert len(shell) == 66 and shell[0] == pytest.approx(outer[0]) and shell[-1] == pytest.approx(inner[0])
    with pytest.raises(FuselageError, match="does not fit"):
        f.section(500, 64, inset=10)


def test_saddle_follows_the_wing_airfoil():
    clarky = airfoildb.get("clarky")
    saddle = Saddle(clarky, position=110, chord=180, height=44, incidence=0)
    f = fuselage(saddles=[saddle])
    lower = dict(saddle.line())
    x = min(lower, key=lambda k: abs(k - 200))
    assert f.top(x) == pytest.approx(min(f.top(x, False), lower[x]), abs=0.2)
    assert f.top(x) < f.top(x, False) - 5  # the saddle is cut into the top line
    assert f.top(400) == f.top(400, False)  # behind the wing nothing changes
    under = Saddle(clarky, position=110, chord=180, height=-30, side="bottom")
    g = fuselage(saddles=[under])
    assert g.bottom(200) > g.bottom(200, False)
    with pytest.raises(FuselageError, match="less than 1 mm"):
        fuselage(saddles=[Saddle(clarky, position=110, chord=180, height=-60)]).side_lines()
    with pytest.raises(ValueError):
        Saddle(clarky, side="left")


def test_outlines_and_views_round_trip():
    f = fuselage()
    side, plan = f.side_outline(), f.plan_outline()
    assert signed_area(side) > 0 and signed_area(plan) > 0
    g, warnings = Fuselage.from_views(side, plan)
    assert not warnings and g.length == pytest.approx(500)
    for x in (30, 120, 250, 400):
        assert g.top(x) == pytest.approx(f.top(x), abs=0.3)
        assert g.bottom(x) == pytest.approx(f.bottom(x), abs=0.3)
        assert g.width(x) == pytest.approx(f.width(x), abs=0.3)
    stretched = [(x * 1.1, y) for x, y in plan]
    _, warnings = Fuselage.from_views(side, stretched)
    assert "stretched" in warnings[0]


def test_two_view_pass_1_keeps_the_block_end_faces():
    f = fuselage()
    opt = CutOptions(spare=5)
    assert job_labels(f, opt) == ["Pass 1 · side view", "Pass 2 · top view"]
    job = build_job(f, opt, 0, MACHINE, PLACEMENT, lambda r: (1.0, 1.0))
    cuts = [m.xy for m in job.toolpath.moves if m.cutting]
    xs = [p[0] for p in cuts]
    assert min(p for p in xs if p > 0) == pytest.approx(20)  # rear face of the block
    assert max(xs) == pytest.approx(20 + 5 + 500 + 5 + 5)  # out past the front face, through the air
    assert all(m.xy == m.uv for m in job.toolpath.moves)  # a 2D cut
    # the top line runs half a kerf above the fuselage's top
    top_at_250 = [p for p in cuts if abs(p[0] - (25 + 250)) < 1.3]
    lo = min(s.bottom for s in STATIONS)
    assert max(p[1] for p in top_at_250) == pytest.approx(10 + 5 - lo + f.top(250) + 0.5, abs=0.15)
    block = job.block
    assert (block["x0"], block["x1"], block["y0"]) == (20, 530, 10)
    assert block["z1"] - block["z0"] == pytest.approx(72 + 10)
    assert job.ghost and len(job.ghost[0]) == 32
    assert "pass 2" in job.notes[1]


def test_two_view_pass_2_cuts_the_top_view_in_place():
    f = fuselage()
    job = build_job(f, CutOptions(spare=5), 1, MACHINE, PLACEMENT, no_kerf)
    outer = job.root_regions[0].outer
    assert min(p[0] for p in outer) == pytest.approx(25)  # nose where pass 1 put it
    assert min(p[1] for p in outer) == pytest.approx(15) and max(p[1] for p in outer) == pytest.approx(15 + 72)
    assert job.z_tip - job.z_root == pytest.approx(f.height_range()[1] - f.height_range()[0])
    assert "on its side" in job.notes[0]


def test_segments_put_the_larger_end_at_the_near_carriage():
    f = fuselage()
    opt = CutOptions(method="segments", segment_length=150)
    labels = job_labels(f, opt)
    assert len(labels) == 4 and labels[0] == "Segment 1 of 4 · 0–125 mm"
    job = build_job(f, opt, 0, MACHINE, PLACEMENT, no_kerf)  # nose segment: the back end is larger
    assert job.z_root == pytest.approx(30) and job.z_tip == pytest.approx(155)
    root, tip = job.root_regions[0].outer, job.tip_regions[0].outer
    assert abs(signed_area(root)) > abs(signed_area(tip))
    # The fuselage lies on its side: the section's height runs along X, its width along Y.
    assert max(p[0] for p in root) - min(p[0] for p in root) == pytest.approx(f.top(125) - f.bottom(125), abs=1e-6)
    assert max(p[1] for p in root) - min(p[1] for p in root) == pytest.approx(f.width(125), abs=1e-6)
    assert "back end (125 mm from the nose)" in job.notes[1]
    assert len(job.toolpath.moves) == 64 + 4  # one ring, plus entry, closing, exit and return
    assert any("spacer" in w for w in job.warnings)  # this steep segment lifts the foam for the far carriage
    assert job.block["y0"] > 10


def test_hollow_segments_and_half_shells():
    f = fuselage()
    hollow = build_job(f, CutOptions(method="segments", wall=3), 1, MACHINE, PLACEMENT, lambda r: (1.0, 1.0))
    assert len(hollow.root_regions[0].holes) == 1
    # outline + hollow (64 points each), back out along the slit (2), then closing the outline and leaving (2)
    assert len([m for m in hollow.toolpath.moves if m.cutting]) == 64 + 64 + 2 + 2
    assert any("slit along the bottom" in n for n in hollow.notes)

    opt = CutOptions(method="segments", wall=3, halves=True)
    assert len(job_labels(f, opt)) == 8
    right = build_job(f, opt, 2, MACHINE, PLACEMENT, no_kerf)
    left = build_job(f, opt, 3, MACHINE, PLACEMENT, no_kerf)
    r_outer, l_outer = right.root_regions[0].outer, left.root_regions[0].outer
    assert not right.root_regions[0].holes and len(r_outer) == 66
    # right half: flat face at the bottom of the block; left half: the same shape upside down
    flat_r = [p for p in r_outer if abs(p[1] - min(q[1] for q in r_outer)) < 1e-6]
    flat_l = [p for p in l_outer if abs(p[1] - max(q[1] for q in l_outer)) < 1e-6]
    assert len(flat_r) >= 4 and len(flat_l) >= 4
    assert "flat centre-line face down" in right.notes[1] and "face up" in left.notes[1]


def test_wall_that_does_not_fit_cuts_the_segment_solid():
    f = fuselage()
    job = build_job(f, CutOptions(method="segments", wall=9), 3, MACHINE, PLACEMENT, no_kerf)
    assert not job.root_regions[0].holes
    assert any("cut solid" in w for w in job.warnings)


def test_cut_options_validation():
    with pytest.raises(ValueError):
        CutOptions(method="spiral")
    with pytest.raises(ValueError):
        CutOptions(points=30)
    with pytest.raises(FuselageError, match="no cut 3"):
        build_job(fuselage(), CutOptions(), 2, MACHINE, PLACEMENT, no_kerf)


def fuselage_project(**part):
    project = default_project()
    project["part"] = copy.deepcopy(DEFAULT_PARTS["fuselage"]) | part
    project["placement"] = copy.deepcopy(DEFAULT_PLACEMENTS["fuselage"])
    return project


def test_project_slices_every_cut():
    for method in ("two_view", "segments"):
        project = fuselage_project(method=method)
        data = slice_project(project).data
        assert data["kind"] == "fuselage" and data["job"] == 1
        for job in range(1, len(data["jobs"]) + 1):
            project["part"]["job"] = job
            result = slice_project(project)
            assert result.data["job"] == job and result.gcode.startswith("(open_HOWI")
            assert f"(cut {job} of {len(data['jobs'])}:" in result.gcode
    data = slice_project(fuselage_project(job=99)).data
    assert data["job"] == 2  # clamped to the last cut
    assert [v["label"] for v in data["views"]][:2] == ["Side", "Top"]
    assert data["part"]["ghosts"] and data["notes"]
    stats = data["stats"]["fuselage"]
    assert stats["length"] == 500 and stats["width"] == 72 and stats["height"] == 86
    assert 1.0 < stats["volume_l"] < 2.5


def test_project_from_drawings_and_errors():
    side = (EXAMPLES / "fuselage" / "side.svg").read_text()
    top = (EXAMPLES / "fuselage" / "top.svg").read_text()
    project = fuselage_project(
        source="views",
        side_view={"name": "side.svg", "format": "svg", "data": side},
        top_view={"name": "top.svg", "format": "svg", "data": top},
    )
    data = slice_project(project).data
    assert data["stats"]["fuselage"]["length"] == pytest.approx(500, abs=2)
    assert data["stats"]["fuselage"]["width"] == pytest.approx(72, abs=1)
    with pytest.raises(SliceError, match="load the top view"):
        slice_project(fuselage_project(source="views", side_view=project["part"]["side_view"]))
    with pytest.raises(SliceError, match="at least two stations"):
        slice_project(fuselage_project(stations=[{"x": 0, "top": 10, "bottom": -10, "width": 20}]))
    with pytest.raises(SliceError, match="cut method"):
        slice_project(fuselage_project(method="spiral"))
    with pytest.raises(SliceError, match="wing saddle 1: chord"):
        slice_project(fuselage_project(saddles=[{"airfoil": "clarky", "chord": "long"}]))


def test_example_project_and_plans():
    project = json.loads((EXAMPLES / "sport_fuselage.howi.json").read_text())
    fuse, _ = build_fuselage(project["part"])
    assert fuse.saddles and fuse.top(200) < fuse.top(200, False)
    name, svg = fuselage_plans(project)
    assert name == "Sport fuselage-plans.svg"
    doc = xml.dom.minidom.parseString(svg).documentElement
    assert float(doc.getAttribute("width")[:-2]) == float(doc.getAttribute("viewBox").split()[2])
    assert svg.count(">F") >= 12  # six formers, labelled in both views and under each former
    assert "1.5 mm inside the skin" in svg and "wing" in svg
    with pytest.raises(SliceError, match="for fuselages"):
        fuselage_plans(default_project())


def test_cli_writes_one_file_per_cut_and_the_plans(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    project = fuselage_project(method="segments", segment_length=260)
    path = tmp_path / "fuse.howi.json"
    path.write_text(json.dumps(project))
    assert main(["slice", str(path), "--plans", "plans.svg"]) == 0
    out = capsys.readouterr().out
    assert "Cut 1 of 2: Segment 1 of 2" in out and "Foam block at least" in out
    assert (tmp_path / "fuse-1.gcode").exists() and (tmp_path / "fuse-2.gcode").exists()
    assert "Formers" in (tmp_path / "plans.svg").read_text()
