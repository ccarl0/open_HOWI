import copy
import itertools
import math

import pytest

from openhowi.airfoil import blend, naca4
from openhowi.config import load_wing_job
from openhowi.core import ControlSurface, CoreError, CoreSpec, Joiner, ServoBay, Spar, core_runs
from openhowi.geometry import point_in_polygon, segments_cross, signed_area
from openhowi.machine import Machine
from openhowi.nest import NestError, NestItem, at, item_paths, nest
from openhowi.planform import Planform, PlanformError
from openhowi.project import DEFAULT_PARTS, DEFAULT_PLACEMENTS, SliceError, default_project, slice_project
from openhowi.toolpath import Placement, build_wing_toolpath
from openhowi.wing import WingPanel

FOIL = naca4("2412")


def control(**kw):
    return ControlSurface(**({"position": 75.0} | kw))


# Control surfaces --------------------------------------------------------------------------------------


@pytest.mark.parametrize("hinge", ["top", "bottom", "center"])
@pytest.mark.parametrize("bevel", [0.0, 25.0])
def test_control_surface_comes_off_as_a_second_piece(hinge, bevel):
    wing = WingPanel(FOIL, 250, 600, tip_chord=180, sweep=25, washout=1.5, root_kerf=1.0, tip_kerf=1.3,
                     control=control(position_tip=72, hinge=hinge, bevel=bevel))  # fmt: skip
    sections = wing.sections()
    for parts, chord, position, x_le in ((sections.root_parts, 250, 75, 0), (sections.tip_parts, 180, 72, 25)):
        main, surface = parts
        assert signed_area(main) > 0 and signed_area(surface) > 0
        assert abs(signed_area(main)) > abs(signed_area(surface))
    assert [len(r) for r in sections.root_parts] == [len(r) for r in sections.tip_parts]
    # the main panel ends at the hinge line; the surface starts there (root, untwisted)
    main, surface = sections.root_parts
    assert max(x for x, _ in main) == pytest.approx(187.5, abs=1e-6)
    assert min(x for x, _ in surface) == pytest.approx(187.5, abs=1e-6)
    root, tip = wing.contours()
    assert len(root) == len(tip)
    # the wire runs down the main panel's back face half a kerf behind it (a straight cut: down the line)
    near = [p for p in root if abs(p[0] - (187.5 if bevel == 0 else 188.0)) < 1e-6]
    assert len(near) >= 1
    toolpath = build_wing_toolpath(wing, Machine(), Placement(root_offset=200, x_margin=25, y_offset=40))
    assert len(toolpath.moves) == len(root) + 4


def test_bevel_angle_and_kerf():
    runs = core_runs(FOIL, 250, CoreSpec(control=control(hinge="top", bevel=30)))
    h, m, f = runs.hinge.h, runs.hinge.m, runs.hinge.f
    angle = math.degrees(math.atan2(f[0] - h[0], h[1] - f[1]))
    assert angle == pytest.approx(30, abs=1e-6) and m[0] == h[0]
    apex, p1, _p2, back = runs.hinge.detour(1.0)
    assert apex == back and p1[0] == pytest.approx(h[0] + 0.5)
    # the wedge path stays inside the waste, half a kerf from both faces
    assert apex[0] == pytest.approx(h[0] + 0.5) and apex[1] < h[1]
    wide = runs.hinge.detour(0.0)
    assert wide[0] == pytest.approx(h) and wide[1] == pytest.approx(m) and wide[2] == pytest.approx(f)
    # a V narrower than the kerf all the way down becomes one straight cut down its middle
    thin = core_runs(FOIL, 250, CoreSpec(control=control(hinge="top", bevel=1))).hinge.detour(3.0)
    assert thin[0] == thin[1] == thin[2]


def test_control_surface_checks():
    with pytest.raises(CoreError, match="outside the core"):
        WingPanel(FOIL, 250, 600, control=control(position=99.5)).sections()
    with pytest.raises(CoreError, match="runs out of the back"):
        WingPanel(FOIL, 250, 600, te_stock=20, control=control(position=88, bevel=60)).sections()
    with pytest.raises(CoreError, match="too close"):
        WingPanel(FOIL, 250, 600, spars=[Spar(74, 4, 1.5, 1.5)], control=control()).sections()
    with pytest.raises(CoreError, match="hinge"):
        WingPanel(FOIL, 250, 600, joiners=[Joiner(74, diameter=6)], control=control()).sections()
    with pytest.raises(CoreError, match="runs into the control surface"):
        WingPanel(FOIL, 250, 600, bays=[ServoBay(300, 74, 30, 15, 8)], control=control()).bay_geometry()
    with pytest.raises(ValueError, match="hinge"):
        ControlSurface(hinge="side")
    with pytest.raises(ValueError, match="bevel"):
        ControlSurface(bevel=80)
    with pytest.raises(ValueError, match="one of"):
        ControlSurface(name="spoiler")
    # in front of the hinge, features are fine
    WingPanel(FOIL, 250, 600, spars=[Spar(30, 6, 1.5, 1.5)], joiners=[Joiner(40)], bays=[ServoBay(300, 55, 30, 15, 8)],
              control=control()).bay_geometry()  # fmt: skip


def test_project_control_surface_and_part_span_split():
    project = default_project()
    project["part"]["controls"] = [{"name": "flap", "position": 72, "hinge": "center", "bevel": 20}]
    data = slice_project(project).data
    assert data["stats"]["control"]["name"] == "flap" and len(data["part"]["solids"]) == 2
    assert data["jobs"] == [] and any("pinned or CA" in n for n in data["notes"])
    assert "flap: hinge line at 72 %" in slice_project(project).gcode
    project["part"]["controls"][0].update(start=250, end=None)
    data = slice_project(project).data
    assert [j["label"] for j in data["jobs"]] == ["Panel 1 of 2 · 0–250 mm", "Panel 2 of 2 · 250–600 mm · flap"]
    assert "control" not in data["stats"] and len(data["part"]["solids"]) == 1
    project["part"]["job"] = 2
    data = slice_project(project).data
    assert data["stats"]["control"]["depth"]["tip"] == pytest.approx(180 * 0.28, abs=0.05)
    assert data["stats"]["planform"]["from"] == 250
    project["part"]["controls"][0].update(start=700)
    with pytest.raises(SliceError, match="beyond"):
        slice_project(project)


def test_toml_control_surface(tmp_path):
    (tmp_path / "w.toml").write_text(
        '[wing]\nroot_airfoil = "naca2412"\nroot_chord = 200.0\nspan = 400.0\n'
        '[wing.control]\nposition = 70.0\nhinge = "bottom"\nbevel = 20.0\n'
    )
    job = load_wing_job(tmp_path / "w.toml")
    assert job.wing.control == ControlSurface(70.0, hinge="bottom", bevel=20.0)
    assert len(job.wing.sections().root_parts) == 2


# Planforms ---------------------------------------------------------------------------------------------


def test_elliptical_planform_split():
    plan = Planform.elliptical(250, 50, 700)
    assert plan.chord(0) == pytest.approx(250) and plan.chord(700) == pytest.approx(50)
    assert plan.le(0) == 0 and plan.le(700) + 0.25 * 50 == pytest.approx(0.25 * 250)  # straight quarter chord
    joints = plan.split(tolerance=2.0)
    panels = plan.panels(joints)
    assert joints[0] == 0 and joints[-1] == pytest.approx(700) and 4 <= len(panels) <= 8
    assert max(p.error for p in panels) <= 2.0 + 1e-6
    for a, b in itertools.pairwise(panels):  # neighbours meet face to face
        assert a.tip_chord == pytest.approx(b.root_chord) and a.s1 == b.s0
    fewer = plan.split(tolerance=4.0)
    assert len(fewer) < len(joints)
    four = plan.panels(plan.split(count=4))
    assert len(four) == 4 and max(p.error for p in four) - min(p.error for p in four) < 0.1  # evened out
    assert all(p.span <= 150 + 1e-6 for p in plan.panels(plan.split(max_length=150)))
    assert 400 in plan.split(forced=[400])
    with pytest.raises(PlanformError):
        Planform.elliptical(250, 300, 700)


def test_straight_and_station_planforms():
    plan = Planform.trapezoid(250, 180, 600, 25, 1.5, 10)
    assert plan.split() == [0, 600]
    assert plan.split(max_length=250) == pytest.approx([0, 200, 400, 600])  # equal panels
    assert plan.split(count=2) == pytest.approx([0, 300, 600])
    (p1, p2) = plan.panels([0, 300, 600])
    assert p1.sweep + p2.sweep == pytest.approx(25) and p1.washout + p2.washout == pytest.approx(1.5)
    assert p1.rise == pytest.approx(5) and p1.error == pytest.approx(0, abs=1e-9)
    assert plan.line_position(300, (0, 25), (600, 25)) == pytest.approx(25)
    rows = [(0, 250, 0, 0, 0), (300, 230, 10, 0.5, 0), (500, 150, 60, 1.5, 20)]
    cranked = Planform.from_stations(rows, smooth=False)
    assert cranked.split() == [0, 300, 500]
    assert cranked.panels([0, 300, 500])[1].rise == pytest.approx(20)
    smooth = Planform.from_stations(rows)
    assert smooth.chord(300) == pytest.approx(230) and smooth.knots == ()
    with pytest.raises(PlanformError, match="two stations"):
        Planform.from_stations(rows[:1])


def test_planform_from_a_drawing():
    # half an ellipse: root on the left (x = 0), leading edge on top
    ring = [(300 * math.sin(t), 40 + 60 * math.cos(t)) for t in (math.pi * k / 200 for k in range(201))]
    ring += [(0.0, -20.0)]
    plan, warnings = Planform.from_outline(ring, 20.0)
    assert plan.chord(0) == pytest.approx(120, abs=0.5) and plan.span < 300 and warnings
    assert plan.chord(plan.span) == pytest.approx(20, abs=1.5)


def test_project_elliptical_wing_in_panels():
    project = default_project()
    project["part"].update(planform="elliptical", root_chord=250, tip_chord=60, span=700, sweep=0, washout=2,
                           tip_airfoil="naca0009", joiners=[{"position": 30}],
                           spars=[{"position": 25, "width": 6, "depth_top": 1.5, "depth_bottom": 1.5}],
                           bays=[{"station": 240, "position": 40, "length": 30, "width": 15, "depth": 8}])  # fmt: skip
    project["machine"]["x_travel"] = 800  # the tapered tip panel swings the far carriage a long way
    first = slice_project(project)
    jobs = first.data["jobs"]
    assert len(jobs) >= 4 and first.wing.joiners and first.data["views"][0]["plan"]
    assert first.data["stats"]["planform"]["kind"] == "elliptical"
    panels = []
    for k in range(1, len(jobs) + 1):
        project["part"]["job"] = k
        result = slice_project(project)
        panels.append(result.wing)
        assert result.wing.joiners == (() if k > 1 else first.wing.joiners)
        assert result.data["notes"][0].startswith(f"Panel {k} of {len(jobs)}")
    assert sum(w.span for w in panels) == pytest.approx(700)
    assert sum(w.washout for w in panels) == pytest.approx(2)
    assert sum(len(w.bays) for w in panels) == 1
    for a, b in itertools.pairwise(panels):  # joints match: chord, airfoil, spar position
        assert a.tip_chord == pytest.approx(b.root_chord)
        assert a.tip_airfoil.points == b.root_airfoil.points
        assert a.spars[0].position_tip == pytest.approx(b.spars[0].position)
    assert "NACA 2412/NACA 0009" in panels[1].root_airfoil.name
    project["part"]["bays"][0]["station"] = first.data["stats"]["planform"]["to"]
    with pytest.raises(SliceError, match="crosses the joint"):
        slice_project(project)
    project["part"]["bays"] = []
    project["machine"]["x_travel"] = 600
    with pytest.raises(SliceError, match="move the towers closer"):
        slice_project(project)


def test_project_planform_options():
    project = default_project()
    project["part"].update(planform="stations", curve="straight")
    assert len(slice_project(project).data["jobs"]) == 2
    project["part"].update(planform="panel", panels=3)
    assert [j["label"] for j in slice_project(project).data["jobs"]] == [
        "Panel 1 of 3 · 0–200 mm", "Panel 2 of 3 · 200–400 mm", "Panel 3 of 3 · 400–600 mm"]  # fmt: skip
    project["part"].update(panels=0, max_panel=250)
    assert len(slice_project(project).data["jobs"]) == 3
    project["part"].update(planform="drawing", plan_view=None)
    with pytest.raises(SliceError, match="planform drawing"):
        slice_project(project)
    # a waisted wing: a straight spar from 10 % at the root to 10 % at the tip leaves it in the middle
    project["part"].update(planform="stations", curve="smooth", max_panel=None, wing_stations=[
        {"x": 0, "chord": 250, "le": 0}, {"x": 300, "chord": 100, "le": 75}, {"x": 600, "chord": 250, "le": 0}],
        spars=[{"position": 10, "width": 3, "depth_top": 1}])  # fmt: skip
    with pytest.raises(SliceError, match="runs off the chord"):
        slice_project(project)


def test_airfoil_blend():
    a, b = naca4("2412"), naca4("0009")
    assert blend(a, b, 0) is a and blend(a, b, 1) is b
    mid = blend(a, b, 0.5)
    thickness = max(y for _, y in mid.upper()) - min(y for _, y in mid.lower())
    assert 0.09 < thickness < 0.12


# Nesting -----------------------------------------------------------------------------------------------


def nest_project(*items, **part):
    project = default_project()
    project["part"] = copy.deepcopy(DEFAULT_PARTS["nest"]) | part
    project["placement"] = copy.deepcopy(DEFAULT_PLACEMENTS["nest"])
    project["part"]["items"] = [
        {"source": {"title": title, "part": sub}, "job": 1, "copies": 1, "opposite": False} | extra
        for title, sub, extra in items
    ]
    return project


def wire_never_enters_a_part(data):
    xy, uv = data["path"]["xy"], data["path"]["uv"]
    d = data["machine"]["tower_distance"]
    for solid in data["part"]["solids"]:
        for face in ("root", "tip"):
            z = solid[f"z_{face}"]
            ring = solid[face]["outer"]
            wire = [(a[0] + (b[0] - a[0]) * z / d, a[1] + (b[1] - a[1]) * z / d) for a, b in zip(xy, uv)]
            for p, q in itertools.pairwise(wire):
                for i in range(len(ring)):
                    assert not segments_cross(p, q, ring[i], ring[(i + 1) % len(ring)])


def test_both_hands_in_one_block():
    project = default_project()
    project["part"]["pair"] = True
    alone = slice_project(default_project()).data
    data = slice_project(project).data
    a, b = data["part"]["solids"]
    assert (a["z_root"], a["z_tip"]) == (b["z_tip"], b["z_root"]) == (200, 800)
    assert max(y for _, y in a["root"]["outer"]) + 8 <= min(y for _, y in b["tip"]["outer"]) + 1e-6
    assert a["root"] == alone["part"]["solids"][0]["root"]  # the first stays where it was
    assert data["stats"]["time_s"] == pytest.approx(2 * alone["stats"]["time_s"], rel=0.1)
    assert data["stats"]["nest"]["columns"] == 1 and "opposite hand" in data["notes"][-1]
    wire_never_enters_a_part(data)
    project["machine"]["y_travel"] = 90  # too low to stack: the second goes in front
    data = slice_project(project).data
    assert data["stats"]["nest"]["columns"] == 2
    a, b = data["part"]["solids"]
    assert min(x for x, _ in b["tip"]["outer"]) > max(x for x, _ in a["root"]["outer"])
    wire_never_enters_a_part(data)


def test_block_of_parts():
    wing = copy.deepcopy(DEFAULT_PARTS["wing"])
    stab = copy.deepcopy(DEFAULT_PARTS["wing"]) | {"root_chord": 140, "tip_chord": 100, "root_airfoil": "naca0009",
                                                    "washout": 0}  # fmt: skip
    project = nest_project(("Wing", wing, {}), ("Wing", wing, {"opposite": True}), ("Stab", stab, {"copies": 2}),
                           max_height=75)  # fmt: skip
    result = slice_project(project)
    data = result.data
    nested = data["stats"]["nest"]
    assert nested["parts"] == 4 and nested["columns"] == 2
    assert [i["label"] for i in nested["items"]] == ["Wing", "Wing (opposite hand)", "Stab #1", "Stab #2"]
    assert [v["label"] for v in data["views"]] == ["Near end", "Far end"] and len(data["notes"]) == 5
    assert data["block"]["y0"] == pytest.approx(10, abs=1) and result.gcode.count("(") > 5
    wire_never_enters_a_part(data)
    lowest = min(min(y for _, y in s["root"]["outer"]) for s in data["part"]["solids"])
    assert lowest == pytest.approx(20, abs=1)  # the wire, half a kerf below, at Y 20


def test_block_of_parts_errors():
    with pytest.raises(SliceError, match="add the wing panels"):
        slice_project(nest_project())
    fuse = copy.deepcopy(DEFAULT_PARTS["fuselage"])
    with pytest.raises(SliceError, match="only wing panels and 2D shapes"):
        slice_project(nest_project(("Fuse", fuse, {})))
    ellipse = copy.deepcopy(DEFAULT_PARTS["wing"]) | {"planform": "elliptical", "root_chord": 250, "tip_chord": 60,
                                                      "span": 700, "min_panel": 20}  # fmt: skip
    project = nest_project(("Ellipse", ellipse, {"job": 0}))
    project["machine"]["x_travel"] = 5000
    with pytest.raises(SliceError, match="turn inside out"):
        slice_project(project)
    with pytest.raises(SliceError, match="whole number"):
        slice_project(nest_project(("Wing", copy.deepcopy(DEFAULT_PARTS["wing"]), {"copies": 1.5})))


def test_nest_engine():
    machine = Machine()
    square = [(30.0, 10.0), (60.0, 10.0), (60.0, 30.0), (30.0, 30.0), (30.0, 10.0)]
    item = NestItem("sq", [(square, square)], 200.0, 600.0)
    result = nest([item, item, item], machine, 30.0, 5.0, base_y=10.0, max_height=50.0)
    assert [p.column for p in result.placed] == [0, 0, 1]
    assert [p.dy for p in result.placed] == [0.0, 25.0, 0.0] and result.placed[2].dx == pytest.approx(35)
    assert len(item_paths(result.toolpath)) == 3
    assert at((square, square), 300.0, 1000.0) == square
    with pytest.raises(NestError, match="does not fit"):
        nest([NestItem("tall", [([(30, 0), (30, 400)], [(30, 0), (30, 400)])], 0, 100)], machine, 30.0, 5.0, 0.0)
    with pytest.raises(NestError, match="gap"):
        nest([item], machine, 30.0, 0.5)


def test_api_and_schema():
    from openhowi.controller import MachineController
    from openhowi.presets import PresetStore
    from openhowi.server import App

    app = App(PresetStore(None), MachineController())
    info = app.handle("GET", "/api/info", {})
    assert "nest" in info["schema"]["part"] and any(t["type"] == "nest" for t in info["part_types"])
    wing_keys = {f["key"] for f in info["schema"]["part"]["wing"]}
    assert {"planform", "controls", "panels", "pair"} <= wing_keys
    result = app.handle(
        "POST", "/api/slice", {"project": nest_project(("W", copy.deepcopy(DEFAULT_PARTS["wing"]), {}))}
    )
    assert result["ok"] and result["kind"] == "nest" and point_in_polygon((0, 0), [(-1, -1), (1, -1), (0, 1)])


# Timelapse data -----------------------------------------------------------------------------------------


def test_slice_data_for_the_timelapse():
    project = default_project()
    project["part"]["controls"] = [{"name": "aileron", "position": 75}]
    data = slice_project(project).data
    assert [s.get("role") for s in data["part"]["solids"]] == [None, "surface"]
    assert data["path"]["stops"] == [] and data["turn"] is None
    fuselage = default_project()
    fuselage["part"] = copy.deepcopy(DEFAULT_PARTS["fuselage"]) | {"method": "turned", "facets": 6}
    fuselage["placement"] = copy.deepcopy(DEFAULT_PLACEMENTS["fuselage"])
    data = slice_project(fuselage).data
    stops, path = data["path"]["stops"], data["path"]
    assert len(stops) == 5 and data["turn"]["step"] == 60
    assert [s["i"] for s in stops] == sorted(s["i"] for s in stops)
    for stop in stops:  # each pause sits where a pass ends, out of the foam, at its time on the timeline
        assert path["t"][stop["i"]] == stop["t"] and not path["cut"][stop["i"] + 1]
        assert "roll the block onto edge" in stop["message"]
    ring = data["part"]["ghosts"][0][0]  # the turning axis runs through the fuselage
    assert min(p[1] for p in ring) < data["turn"]["y"] < max(p[1] for p in ring)
    fuselage["machine"]["rotary_axis"] = "A"
    stops = slice_project(fuselage).data["path"]["stops"]
    assert [s["angle"] for s in stops] == [60, 120, 180, 240, 300]
    nested = default_project()
    nested["part"].update(pair=True, controls=[{"name": "aileron", "position": 75}])
    roles = [s.get("role") for s in slice_project(nested).data["part"]["solids"]]
    assert roles == [None, "surface", None, "surface"]
