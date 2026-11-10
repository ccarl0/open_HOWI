from importlib import resources

import pytest

from openhowi import airfoildb
from openhowi.airfoil import AirfoilError, load_airfoil, naca4, parse_dat
from openhowi.cli import main
from openhowi.project import SliceError, default_project, resolve_airfoil, slice_project


def test_every_entry_loads_and_every_file_is_listed():
    files = {
        p.name.removesuffix(".dat")
        for p in (resources.files("openhowi") / "airfoils").iterdir()
        if p.name.endswith(".dat")
    }
    ids = [e.id for e in airfoildb.ENTRIES]
    assert len(ids) == len(set(ids))
    assert files == {i for i in ids if not i.startswith("naca") or i in files}  # NACA 4-digit ones are generated
    for e in airfoildb.ENTRIES:
        assert e.category in airfoildb.CATEGORIES
        foil = airfoildb.get(e.id)
        assert foil.name == e.name
        assert len(foil.points) > 30
        assert foil.resampled(40)  # both surfaces usable for cutting


def test_measured_properties():
    rows = {r["id"]: r for r in airfoildb.catalog(with_outlines=False)}
    assert rows["naca0012"]["thickness"] == 12.0 and rows["naca0012"]["camber"] == 0
    assert rows["naca4412"]["camber"] == pytest.approx(4.0, abs=0.3)
    assert rows["naca4412"]["camber_at"] == pytest.approx(40, abs=4)
    assert rows["clarky"]["thickness"] == pytest.approx(11.7, abs=0.1)
    assert rows["e325"]["reflex"] and rows["mh49"]["reflex"]
    assert not rows["sd7037"]["reflex"]
    assert all(r["camber"] == 0 for r in rows.values() if r["category"] == "Symmetric & tail" and r["id"] != "ht13")


def test_catalog_outlines_and_search():
    row = airfoildb.catalog()[0]
    assert len(row["outline"]) == 59 and row["outline"][0][0] == pytest.approx(1.0)
    assert [r["id"] for r in airfoildb.search("SD 7037")] == ["sd7037"]
    wings = airfoildb.search("", category="Flying wings")
    assert wings and all(r["category"] == "Flying wings" for r in wings)
    assert {r["id"] for r in airfoildb.search("hand launch")} >= {"ag24", "ag25", "ag26"}


def test_lookups_by_id():
    assert airfoildb.key(" SD-7037 ") == "sd7037"
    assert load_airfoil("sd7037").name == "SD7037"
    assert load_airfoil("NACA 2412").name == "NACA 2412"
    assert resolve_airfoil("e387").name == "Eppler E387"
    assert resolve_airfoil("naca2412").points == naca4("2412").points
    with pytest.raises(AirfoilError, match="neither an airfoil file nor in the airfoil database"):
        load_airfoil("nosuchfoil")
    with pytest.raises(SliceError, match="unknown airfoil"):
        resolve_airfoil("../etc/passwd")  # never reads files


def test_project_with_database_airfoils_slices():
    project = default_project()
    project["part"].update(root_airfoil="sd7062", tip_airfoil="sd7037")
    result = slice_project(project)
    assert result.data["stats"]["airfoils"] == {"root": "SD7062", "tip": "SD7037"}
    assert "root SD7062" in result.gcode


def test_parser_skips_notes_after_the_coordinates():
    text = "Foil\n1 0\n0.5 0.05\n0 0\n0.5 -0.03\n1 0\n\nSome notes about it.\nhttp://example.com\n"
    assert len(parse_dat(text).points) == 5
    with pytest.raises(AirfoilError):
        parse_dat("Foil\n1 0\n0.5 0.05\nnot a number\n0 0\n")


def test_airfoils_command(tmp_path, capsys):
    assert main(["airfoils", "flying", "wing"]) == 0
    out = capsys.readouterr().out
    assert "Flying wings:" in out and "mh45" in out and "sd7037" not in out
    target = tmp_path / "e387.dat"
    assert main(["airfoils", "--export", "e387", "-o", str(target)]) == 0
    assert parse_dat(target.read_text()).name == "Eppler E387"
    assert main(["airfoils", "zzzz"]) == 1
