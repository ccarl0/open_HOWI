import copy
import json
import re

import pytest

from openhowi.airfoil import naca4
from openhowi.calibration import KERF_TEST_WIDTH, kerf_test
from openhowi.gcode import segments, wire_displacement
from openhowi.machine import Machine
from openhowi.material import Material
from openhowi.presets import BUILTIN, PresetError, PresetStore, machine_from_values, material_from_values
from openhowi.project import SliceError, default_project, slice_project


def test_kerf_model():
    m = Material(kerf=1.0, kerf_half_speed=1.6)
    assert m.kerf_at(1.0) == pytest.approx(1.0)
    assert m.kerf_at(0.5) == pytest.approx(1.6)
    assert m.kerf_at(0.75) == pytest.approx(1.3)
    assert m.kerf_at(0.0) == pytest.approx(1.0 + 0.6 * 0.8 / 0.5)  # clamped at 20 % speed
    assert Material(kerf=1.0).kerf_at(0.3) == 1.0
    assert Material(kerf=1.0, kerf_half_speed=0.8).kerf_at(0.5) == 1.0  # never below the full-speed kerf


def test_default_project_slices():
    result = slice_project(default_project())
    data = result.data
    assert data["ok"] and result.gcode.startswith("(open_HOWI")
    # Tapered panel: the tip moves more slowly, so it gets the wider kerf.
    assert data["stats"]["speed_ratio"]["root"] == 1.0
    assert data["stats"]["kerf"]["tip"] > data["stats"]["kerf"]["root"]
    path = data["path"]
    assert len(path["t"]) == len(path["xy"]) == len(path["uv"]) == len(path["cut"])
    assert path["t"] == sorted(path["t"])
    assert path["t"][-1] == pytest.approx(data["stats"]["time_s"], abs=0.1)
    assert len(data["part"]["root"]) == len(data["part"]["tip"])
    json.dumps(data)  # must be JSON serialisable for the GUI


def test_cut_feed_applies_in_the_foam_not_at_the_carriage():
    project = default_project()
    result = slice_project(project)
    z0, z1 = result.toolpath.foam_span
    feed = result.machine.cut_feed
    for seg in segments(result.toolpath, result.machine):
        if not seg.cutting:
            continue
        # Speed in the foam never exceeds the cut speed at either end of the part ...
        assert seg.drive / seg.minutes == pytest.approx(feed)
    # ... while the XY carriage, beyond the root, has to move faster than that.
    assert result.data["stats"]["peak_speed"]["XY"] > feed
    assert z0 == project["placement"]["root_offset"]
    assert z1 == z0 + project["part"]["span"]


def test_wire_displacement_interpolates_along_the_wire():
    assert wire_displacement((10, 0), (20, 0), 0, 1000) == pytest.approx(10)
    assert wire_displacement((10, 0), (20, 0), 1000, 1000) == pytest.approx(20)
    assert wire_displacement((10, 0), (20, 0), 500, 1000) == pytest.approx(15)


def test_kerf_override_and_embedded_airfoil():
    project = default_project()
    project["part"].update(kerf_root_override=0.5, kerf_tip_override=0.7)
    project["part"]["tip_airfoil"] = {"name": "thin", "dat": naca4("0009").to_dat()}
    data = slice_project(project).data
    assert data["stats"]["kerf"] == {"root": 0.5, "tip": 0.7}
    assert data["stats"]["airfoils"] == {"root": "NACA 2412", "tip": "NACA 0009"}


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda p: p["placement"].update(root_offset=900), "does not fit"),
        (lambda p: p["machine"].update(x_travel=100), "exceeds"),
        (lambda p: p["part"].update(root_airfoil="clarky"), "unknown airfoil"),
        (lambda p: p["part"].update(span="long"), "must be a number"),
        (lambda p: p["material"].update(cut_feed=0), "cut_feed"),
        (lambda p: p["machine"].update(bogus=1), "unknown machine setting"),
        (lambda p: p["part"].update(type="sign"), "unsupported part type"),
    ],
)
def test_slice_errors_are_reported(mutate, message):
    project = default_project()
    mutate(project)
    with pytest.raises(SliceError, match=message):
        slice_project(project)


def test_slow_end_warning():
    project = default_project()
    project["part"].update(root_chord=300, tip_chord=90, span=500)
    assert any("only" in w for w in slice_project(project).data["warnings"])


def test_project_survives_a_json_round_trip():
    project = json.loads(json.dumps(default_project()))
    assert slice_project(project).gcode == slice_project(default_project()).gcode


def test_kerf_test_cuts_two_rectangles_at_full_and_half_speed():
    material = Material(cut_feed=200)
    machine = machine_from_values(BUILTIN["machine"][0]["values"], material)
    gcode, instructions = kerf_test(machine)
    assert f"Kerf = {KERF_TEST_WIDTH:g} − width" in instructions
    feeds = [float(f) for f in re.findall(r"G1 X[\d.]+ Y[\d.]+ U[\d.]+ V[\d.]+ F([\d.]+)", gcode)]
    # Vertical 30 mm edges: 200 mm/min -> F 6.66667, at half speed -> F 3.33333
    assert pytest.approx(200 / 30, rel=1e-4) in feeds
    assert pytest.approx(100 / 30, rel=1e-4) in feeds


def test_kerf_test_checks_machine_size():
    with pytest.raises(ValueError, match="exceeds"):
        kerf_test(Machine(x_travel=100))


def test_preset_store(tmp_path):
    store = PresetStore(tmp_path)
    assert [p["builtin"] for p in store.list("material")] == [True] * len(BUILTIN["material"])
    values = copy.deepcopy(BUILTIN["material"][0]["values"]) | {"kerf": 0.8}
    store.save("material", "My EPS", values)
    mine = [p for p in store.list("material") if not p["builtin"]]
    assert mine == [{"name": "My EPS", "values": values, "builtin": False}]
    with pytest.raises(PresetError, match="built-in"):
        store.save("material", BUILTIN["material"][0]["name"], values)
    with pytest.raises(PresetError, match="cut_feed"):
        store.save("material", "Broken", values | {"cut_feed": -1})
    with pytest.raises(PresetError):
        store.save("gadget", "x", {})
    store.delete("material", "My EPS")
    assert all(p["builtin"] for p in store.list("material"))
    with pytest.raises(PresetError):
        store.delete("material", "My EPS")


def test_preset_names_cannot_escape_the_config_dir(tmp_path):
    store = PresetStore(tmp_path / "cfg")
    store.save("machine", "../../evil", BUILTIN["machine"][0]["values"])
    written = list((tmp_path / "cfg").rglob("*.json"))
    assert len(written) == 1 and written[0].parent == tmp_path / "cfg" / "presets" / "machines"


def test_airfoil_library(tmp_path):
    store = PresetStore(tmp_path)
    entry = store.add_airfoil("../My Foil.dat", naca4("4415").to_dat())
    assert entry == {"file": "my-foil.dat", "name": "NACA 4415"}
    assert store.airfoils() == [entry]
    assert store.airfoil_text("my-foil.dat").startswith("NACA 4415")
    with pytest.raises(PresetError):
        store.airfoil_text("../../etc/passwd")


def test_material_from_values_rejects_unknown_keys():
    with pytest.raises(PresetError, match="unknown material"):
        material_from_values({"kerf": 1, "color": "white"})
