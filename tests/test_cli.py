from pathlib import Path

from openhowi.cli import main

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "tapered_wing.toml"


def test_wing_command_writes_gcode_and_svg(tmp_path, capsys):
    out, svg = tmp_path / "wing.gcode", tmp_path / "wing.svg"
    assert main(["wing", str(EXAMPLE), "-o", str(out), "--svg", str(svg)]) == 0
    gcode = out.read_text()
    assert gcode.startswith("(open_HOWI hot-wire G-code)")
    assert "G93" in gcode and gcode.rstrip().endswith("M2")
    assert svg.read_text().startswith("<svg")
    assert "est. time" in capsys.readouterr().out


def test_wing_command_reports_config_errors(tmp_path, capsys):
    cfg = tmp_path / "bad.toml"
    cfg.write_text('[wing]\nroot_airfoil = "naca2412"\nroot_chord = 200\nspan = 400\nspam = 1\n')
    assert main(["wing", str(cfg)]) == 1
    assert "unknown key(s): spam" in capsys.readouterr().err


def test_wing_command_reports_missing_airfoil(tmp_path, capsys):
    cfg = tmp_path / "bad.toml"
    cfg.write_text('[wing]\nroot_airfoil = "nope.dat"\nroot_chord = 200\nspan = 400\n')
    assert main(["wing", str(cfg)]) == 1
    assert "nope.dat" in capsys.readouterr().err


def test_naca_command(tmp_path):
    out = tmp_path / "n.dat"
    assert main(["naca", "2412", "-n", "20", "-o", str(out)]) == 0
    lines = out.read_text().splitlines()
    assert lines[0] == "NACA 2412"
    assert len(lines) == 1 + 39


def test_send_dry_run(tmp_path, capsys):
    gcode = tmp_path / "x.gcode"
    gcode.write_text("(comment)\nG21\nG1 X1 Y1 U1 V1 F100\n")
    assert main(["send", str(gcode), "--dry-run"]) == 0
    assert "2 lines" in capsys.readouterr().out


def test_shape_command(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("OPENHOWI_CONFIG_DIR", str(tmp_path / "cfg"))
    drawing = EXAMPLE.parent / "shapes" / "former.svg"
    out = tmp_path / "former.gcode"
    assert main(["shape", str(drawing), "-o", str(out), "--span", "80", "--material", "XPS extruded (blue/pink)"]) == 0
    text = capsys.readouterr().out
    assert "1 shape(s), 3 hole(s), 100 x 100 mm" in text
    assert "material: XPS extruded" in out.read_text()
    assert main(["shape", str(drawing), "--machine", "nope"]) == 1
    assert "no machine preset named 'nope'" in capsys.readouterr().err
    assert main(["shape", str(tmp_path / "x.pdf")]) == 1


def test_sheeted_wing_example(tmp_path, capsys):
    out = tmp_path / "core.gcode"
    assert main(["wing", str(EXAMPLE.parent / "sheeted_wing.toml"), "-o", str(out)]) == 0
    assert "core ends" in capsys.readouterr().out
    assert "(core: sheeting top 1.5 / bottom 1.5 mm" in out.read_text()
