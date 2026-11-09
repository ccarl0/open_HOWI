import base64
import copy
import json
import math
import struct
import time
import xml.dom.minidom

import pytest

from openhowi.cli import main
from openhowi.controller import MachineController
from openhowi.diagrams import polygon, turned_end_view
from openhowi.fusecut import CutOptions, build_job, job_labels
from openhowi.fuselage import Fuselage, FuselageError, Station, finished_section
from openhowi.gcode import generate_gcode
from openhowi.guide import cutting_guide_html
from openhowi.machine import Machine
from openhowi.project import (
    DEFAULT_PARTS,
    DEFAULT_PLACEMENTS,
    default_project,
    fuselage_plans,
    fuselage_stl,
    slice_project,
)
from openhowi.simulator import SimulatedGrbl
from openhowi.toolpath import Move, Placement, Stop, Toolpath

STATIONS = [
    Station(0, 22, -22, 44),
    Station(30, 34, -30, 58),
    Station(120, 48, -38, 72, 80, 50),
    Station(250, 44, -34, 64, 80, 50),
    Station(400, 30, -10, 34, 70, 60),
    Station(500, 22, 6, 16, 60, 60),
]
PLACEMENT = Placement("xy", 30, 20, 10)
TP = Toolpath(
    (
        Move((10, 10), (10, 10), False),
        Move((20, 10), (20, 10), True),
        Move((0, 10), (0, 10), False),
        Move((0, 10), (0, 10), False),  # zero length: dropped, but a stop on it still happens
        Move((30, 20), (30, 20), True),
        Move((0, 0), (0, 0), False),
    ),
    stops=(Stop(3, "Pass 2: turn the block."),),
)


def no_kerf(ratio):
    return (0.0, 0.0)


# G-code pauses and rotary turns ---------------------------------------------------------------------


def test_pause_switches_the_wire_off_and_back_on():
    g = generate_gcode(TP, Machine(wire_power=800, preheat=1.5))
    block = g[g.index("(PAUSE: Pass 2: turn the block.)") :].splitlines()[:5]
    assert block == ["(PAUSE: Pass 2: turn the block.)", "M5", "M0", "M3 S800", "G4 P1.5"]
    assert g.index("M0") < g.index("G1 X30 Y20")
    manual = generate_gcode(TP, Machine())
    assert "Switch the wire off first, and on again before resuming." in manual and "M5" not in manual


def test_rotary_turns_instead_of_pausing():
    tp = Toolpath(TP.moves, stops=(Stop(3, "pass 2", rotate=45.0),))
    g = generate_gcode(tp, Machine(rotary_axis="a"))
    assert "G0 A45" in g and g.rstrip().endswith("M2") and "G0 A0" in g and "M0" not in g
    assert "(rotary axis A must read 0 deg at the start)" in g
    with pytest.raises(ValueError, match="the machine has none"):
        generate_gcode(tp, Machine())


def test_machine_rotary_validation():
    assert Machine(rotary_axis=" b ").rotary_axis == "B"
    with pytest.raises(ValueError, match="rotary_axis"):
        Machine(axis_letters="XYZA", rotary_axis="A")
    with pytest.raises(ValueError):
        Machine(rotary_height=-1)


def test_simulator_holds_at_m0_until_cycle_start():
    sim = SimulatedGrbl(speedup=1000.0)
    sim.readline()
    for line in ("G1X5F600", "M0", "G1X10F600"):
        sim.write((line + "\n").encode())
    deadline = time.monotonic() + 2
    while sim.state != "Hold" and time.monotonic() < deadline:
        sim.write(b"?")
        time.sleep(0.01)
    assert sim.state == "Hold" and sim.current_position()[0] == pytest.approx(5)
    time.sleep(0.05)
    assert sim.current_position()[0] == pytest.approx(5)  # stays put while held
    sim.write(b"~")
    deadline = time.monotonic() + 2
    while sim.current_position()[0] < 10 and time.monotonic() < deadline:
        sim.write(b"?")
        time.sleep(0.01)
    assert sim.current_position()[0] == pytest.approx(10)


def test_controller_tells_the_operator_what_each_pause_is_for():
    controller = MachineController(opener=lambda port, baud, axes: SimulatedGrbl(axes, speedup=500.0))
    controller.connect("sim", 115200)
    try:
        deadline = time.monotonic() + 3
        while not controller.ready and time.monotonic() < deadline:
            time.sleep(0.02)
        tp = Toolpath(TP.moves, stops=(Stop(1, "First turn."), Stop(4, "Second turn.")))
        controller.start_job(generate_gcode(tp, Machine(wire_power=500)), "t")
        for expected in ("First turn.", "Second turn."):
            deadline = time.monotonic() + 5
            while controller.snapshot()["job"]["state"] != "paused" and time.monotonic() < deadline:
                time.sleep(0.02)
            job = controller.snapshot()["job"]
            assert job["state"] == "paused" and job["message"] == expected and job["pauses"] == 2
            controller.resume()
        deadline = time.monotonic() + 5
        while controller.snapshot()["job"]["state"] != "done" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert controller.snapshot()["job"]["state"] == "done"
    finally:
        controller.disconnect()


# The turned method -------------------------------------------------------------------------------------


def test_turned_passes_cut_the_silhouette_all_round():
    f = Fuselage.from_stations(STATIONS)
    opt = CutOptions(method="turned", facets=8)
    assert job_labels(f, opt) == ["All 8 passes, turning the block"]
    job = build_job(f, opt, 0, Machine(), PLACEMENT, no_kerf)
    stops = job.toolpath.stops
    assert len(stops) == 7 and all(s.rotate is None for s in stops)
    assert "roll the block onto edge 2" in stops[0].message and "(" not in stops[0].message
    jig = job.jig
    assert jig["sides"] == 8 and jig["apothem"] == pytest.approx(jig["half"] * math.sqrt(2) + 3)
    # pass 1 (edge 1 down) cuts along the fuselage's top, measured from the turning axis
    y_axis = 10 + jig["apothem"]
    first = [m.xy for m in job.toolpath.moves[: stops[0].index] if m.cutting]
    from openhowi.fuselage import auto_axis

    axis = auto_axis(f)
    widest = 120.0
    x_machine = 20 + 6 + 10 + widest  # fence + plate + end stub
    at = min(first, key=lambda p: abs(p[0] - x_machine))
    top = max(v for _, v in finished_section(f, widest, 96)) - axis
    assert at[1] == pytest.approx(y_axis + top, abs=0.3)
    assert len(job.steps) == 1 + 1 + 8 and job.steps[0]["title"] == "Prepare the block and the jig"
    assert job.ghost and len(job.ghost[0]) == 48
    assert "8-sided" in job.ghost_label


def test_more_passes_leave_less_to_sand():
    f = Fuselage.from_stations(STATIONS)

    def excess(n):
        job = build_job(f, CutOptions(method="turned", facets=n), 0, Machine(y_travel=400), PLACEMENT, no_kerf)
        return float(job.notes[-1].split("up to ")[1].split(" mm")[0])

    assert excess(4) > excess(8) > excess(16) > 0


def test_turned_on_a_rotary_axis():
    f = Fuselage.from_stations(STATIONS)
    machine = Machine(rotary_axis="A", rotary_height=120)
    job = build_job(f, CutOptions(method="turned", facets=6), 0, machine, PLACEMENT, no_kerf)
    assert [s.rotate for s in job.toolpath.stops] == [60, 120, 180, 240, 300]
    assert job.jig is None and job.steps[0]["title"] == "Prepare the block"
    g = generate_gcode(job.toolpath, machine)
    assert g.count("G0 A") == 6 and "M0" not in g


def test_turned_needs_room_above_the_jig():
    f = Fuselage.from_stations(STATIONS)
    with pytest.raises(FuselageError, match="vertical travel"):
        build_job(f, CutOptions(method="turned"), 0, Machine(y_travel=150), PLACEMENT, no_kerf)


def test_diagrams_are_svg():
    pts = polygon(8, 50)
    assert len(pts) == 8 and min(y for _, y in pts) == pytest.approx(-50)  # edge 1 flat at the bottom
    svg = turned_end_view([(30 * math.cos(a / 10), 30 * math.sin(a / 10)) for a in range(63)], 40, 8, 3, 30, [30, 30],
                          60)  # fmt: skip
    doc = xml.dom.minidom.parseString(svg)
    assert doc.documentElement.tagName == "svg" and "edge 3 down" in svg


# Project, guide, STL --------------------------------------------------------------------------------------


def turned_project(**part):
    project = default_project()
    project["title"] = "Turned"
    project["part"] = copy.deepcopy(DEFAULT_PARTS["fuselage"]) | {"method": "turned"} | part
    project["placement"] = copy.deepcopy(DEFAULT_PLACEMENTS["fuselage"])
    return project


def test_project_turned_slice_and_plans():
    project = turned_project(facets=12)
    data = slice_project(project).data
    assert len(data["jobs"]) == 1 and data["jig"]["sides"] == 12
    assert len(data["steps"]) == 14 and all(step["svg"] for step in data["steps"][1:])
    assert data["part"]["ghost_label"] == "The 12-sided fuselage it cuts"
    _, svg = fuselage_plans(project)
    assert "Jig plates" in svg and xml.dom.minidom.parseString(svg)
    with pytest.raises(Exception, match="facets"):
        slice_project(turned_project(facets=7))


def test_cutting_guide_for_every_method():
    for method in ("two_view", "turned", "segments"):
        name, html = cutting_guide_html(turned_project(method=method))
        assert name == "Turned-cutting-guide.html" and html.startswith("<!doctype html>")
        assert "Formers (1:1)" in html and "Side and top views (reduced)" in html
        assert ("Jig plates (1:1)" in html) == (method == "turned")
        assert "<script" not in html
    _, html = cutting_guide_html(turned_project(method="two_view"))
    assert html.count("<h2>Cut ") == 2


def test_stl_is_a_closed_mesh():
    name, data = fuselage_stl(turned_project())
    count = struct.unpack("<I", data[80:84])[0]
    assert name == "Turned.stl" and len(data) == 84 + 50 * count
    edges: dict[tuple, int] = {}
    for k in range(count):
        v = struct.unpack("<9f", data[84 + 50 * k + 12 : 84 + 50 * k + 48])
        pts = [tuple(round(c, 3) for c in v[i : i + 3]) for i in (0, 3, 6)]
        for a, b in ((pts[0], pts[1]), (pts[1], pts[2]), (pts[2], pts[0])):
            key = (min(a, b), max(a, b))
            edges[key] = edges.get(key, 0) + 1
    assert all(n == 2 for n in edges.values())  # every edge shared by two triangles: watertight


def test_cli_and_api(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "t.howi.json").write_text(json.dumps(turned_project()))
    assert main(["slice", "t.howi.json", "--guide", "g.html", "--stl", "f.stl"]) == 0
    out = capsys.readouterr().out
    assert "Wrote g.html" in out and (tmp_path / "f.stl").stat().st_size > 1000
    assert "roll the block onto edge 2" in (tmp_path / "t.gcode").read_text()

    from openhowi.presets import PresetStore
    from openhowi.server import App

    app = App(PresetStore(tmp_path / "cfg"), MachineController())
    guide = app.handle("POST", "/api/guide/fuselage", {"project": turned_project()})
    assert guide["filename"].endswith(".html") and "Jig plates" in guide["html"]
    model = app.handle("POST", "/api/export/fuselage-stl", {"project": turned_project()})
    assert base64.b64decode(model["stl"])[:80].startswith(b"open_HOWI Turned")
