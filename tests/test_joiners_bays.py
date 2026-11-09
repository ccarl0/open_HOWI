import math
import xml.dom.minidom

import pytest

from openhowi import airfoildb
from openhowi.airfoil import naca4
from openhowi.cli import main
from openhowi.core import Channel, CoreError, HoleShape, Joiner, ServoBay, Spar
from openhowi.geometry import outline_gap
from openhowi.project import SliceError, bay_template, default_project, slice_project
from openhowi.template import bay_template_svg
from openhowi.wing import WingPanel, axis_basis, cut_path

FOIL = naca4("2415")


def axis_distances(wing, k):
    """Distance from the joiner axis of every point of the ruled surface the wire cuts round joiner ``k``."""
    sections = wing.sections()
    (c0, c1) = wing.joiner_axes()[k]
    offset = len(wing.channels)
    (_, cr, sr), (_, ct, st) = sections.root_holes[offset + k], sections.tip_holes[offset + k]
    root, tip = sr.ring(cr)[:-1], st.ring(ct)[:-1]
    a = (c0[0], c0[1], 0.0)
    d = (c1[0] - c0[0], c1[1] - c0[1], wing.span)
    length = math.sqrt(sum(v * v for v in d))
    d = tuple(v / length for v in d)
    out = []
    for t in (0.0, 0.25, 0.5, 1.0):
        for p, q in zip(root, tip):
            v = (p[0] + (q[0] - p[0]) * t - a[0], p[1] + (q[1] - p[1]) * t - a[1], wing.span * t)
            along = sum(v[i] * d[i] for i in range(3))
            out.append([v[i] - along * d[i] for i in range(3)])
    return out, d


def test_round_joiner_square_to_the_root_is_a_straight_cylinder():
    wing = WingPanel(FOIL, 250, 600, tip_chord=230, sweep=10, joiners=[Joiner(30, diameter=10)])
    ((c0, c1),) = wing.joiner_axes()
    assert c0[0] == c1[0] == pytest.approx(75.0) and c0[1] == c1[1]
    radii = [math.hypot(*v) for v in axis_distances(wing, 0)[0]]
    assert min(radii) == pytest.approx(5.0, abs=1e-9) and max(radii) == pytest.approx(5.0, abs=1e-9)


def test_slanted_joiners_keep_their_true_size():
    wing = WingPanel(FOIL, 260, 700, tip_chord=180, sweep=60, washout=2,
                     joiners=[Joiner(30, diameter=8, position_tip=40, dihedral=0.6),
                              Joiner(55, "rect", width=3, height=14, position_tip=60, side="top")])  # fmt: skip
    radii = [math.sqrt(sum(c * c for c in v)) for v in axis_distances(wing, 0)[0]]
    assert min(radii) == pytest.approx(4.0, abs=1e-9) and max(radii) == pytest.approx(4.0, abs=1e-9)
    # The blade: measured across (horizontal, square to the axis) and up, in the joiner's own frame.
    points, d = axis_distances(wing, 1)
    e1 = (d[2], 0.0, -d[0])
    n = math.hypot(e1[0], e1[2])
    e1 = (e1[0] / n, 0.0, e1[2] / n)
    e2 = (d[1] * e1[2] - d[2] * e1[1], d[2] * e1[0] - d[0] * e1[2], d[0] * e1[1] - d[1] * e1[0])
    across = [sum(v[i] * e1[i] for i in range(3)) for v in points]
    up = [sum(v[i] * e2[i] for i in range(3)) for v in points]
    assert max(across) - min(across) == pytest.approx(3.0, abs=1e-9)
    assert max(up) - min(up) == pytest.approx(14.0, abs=1e-9)
    # The joiner comes out of the tip at 40 % of the tip chord.
    assert wing.joiner_axes()[0][1][0] == pytest.approx(wing.tip_point(0.4)[0])


def test_dihedral_tilts_the_joiner_down_towards_the_tip():
    wing = WingPanel(FOIL, 250, 300, joiners=[Joiner(35, diameter=6, dihedral=4)])
    ((c0, c1),) = wing.joiner_axes()
    assert c0[1] - c1[1] == pytest.approx(300 * math.tan(math.radians(2)))
    wing.sections()


def test_axis_basis_of_a_square_hole_is_the_identity():
    e1, e2 = axis_basis((0.0, 0.0))
    assert e1 == pytest.approx((1.0, 0.0)) and e2 == pytest.approx((0.0, 1.0))


def test_joiner_cut_path_and_kerf():
    wing = WingPanel(FOIL, 250, 600, root_kerf=1.0, tip_kerf=1.0, joiners=[Joiner(40, "rect", width=4, height=16)])
    sections = wing.sections()
    root_path, tip_path = wing.contours()
    assert len(root_path) == len(tip_path) == len(sections.root) + 7  # 6 ring points + back out along the slit
    ((index, center, shape),) = sections.root_holes
    ring = shape.ring(center, inset=0.5)
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    assert max(xs) - min(xs) == pytest.approx(3.0) and max(ys) - min(ys) == pytest.approx(15.0)
    assert ring[0] == pytest.approx((center[0], center[1] - 7.5))  # the slit comes in from below
    with pytest.raises(CoreError, match="narrower than"):
        cut_path(sections.root, [(index, center, HoleShape("rect", 0.4, 10, "bottom"))], 1.0)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"tip_chord": 150, "sweep": 150, "joiners": [Joiner(25, diameter=8)]}, "joiner 1 .* at the tip does not fit.*tip position"),
        ({"joiners": [Joiner(30, diameter=10, dihedral=8)]}, "joiner 1 .* at the tip leaves less than 1 mm"),
        ({"joiners": [Joiner(30, diameter=10), Joiner(33, diameter=10)]}, "joiner 2 .* too close to joiner 1"),
        ({"spars": [Spar(30, 6, 2)], "joiners": [Joiner(30, diameter=10, side="top")]}, "slit into joiner 1 .* runs through spar 1"),
        ({"sweep": 60, "tip_chord": 180, "spars": [Spar(25, 6, 2, 2)], "joiners": [Joiner(35, diameter=8)]},
         "spar 1 and joiner 1 swap places on the bottom surface"),
        ({"channels": [Channel(45, 8)], "joiners": [Joiner(45, diameter=6, offset=6, side="top")]},
         "joiner 1 .* too close to servo channel 1"),
    ],
)  # fmt: skip
def test_joiner_errors(options, message):
    with pytest.raises(CoreError, match=message):
        WingPanel(FOIL, 260, 700, **{"tip_chord": 220, **options}).sections()


def test_joiner_validation():
    with pytest.raises(ValueError, match="shape"):
        Joiner(shape="oval")
    with pytest.raises(ValueError, match="sizes"):
        Joiner(diameter=0)
    with pytest.raises(ValueError, match="dihedral"):
        Joiner(dihedral=60)


BASE = {
    "tip_chord": 180,
    "sweep": 60,
    "washout": 2,
    "spars": [Spar(25, 6, 2, 2)],
    "channels": [Channel(45, 8)],
    "joiners": [Joiner(33, diameter=8, position_tip=33, side="top")],
}


def bays(*items):
    return WingPanel(FOIL, 260, 700, bays=items, **BASE).bay_geometry()


def test_servo_bay_geometry():
    (bay,) = bays(ServoBay(400, 45, 30, 14, 12, "bottom"))
    assert (bay.z0, bay.z1) == (385, 415)
    t = 400 / 700
    le = 60 * t  # the tip leading edge sits 60 mm back (washout moves it a hair)
    assert bay.le == pytest.approx(le, abs=0.1)
    assert bay.chord == pytest.approx(260 + (180 - 260) * t, abs=0.1)
    assert (bay.x0 + bay.x1) / 2 == pytest.approx(bay.le + 0.45 * bay.chord)
    assert bay.x1 - bay.x0 == pytest.approx(14)
    assert bay.channels == (1,)  # deep enough to reach the servo channel
    (shallow,) = bays(ServoBay(400, 45, 30, 14, 5))
    assert shallow.channels == ()
    near, far = bay.faces
    assert len(near) == len(far) == 4 and min(p[1] for p in near) < bay.floor + 1e-9


@pytest.mark.parametrize(
    ("items", "message"),
    [
        ([ServoBay(10, 45, 30, 14, 12)], "runs past the root"),
        ([ServoBay(690, 45, 30, 14, 12)], "runs past the tip"),
        ([ServoBay(400, 25, 30, 14, 5)], "cuts into the slot of spar 1"),
        ([ServoBay(400, 45, 30, 14, 29.5)], "leaves less than 1 mm of foam"),
        ([ServoBay(400, 98, 30, 14, 5)], "does not fit in the core"),
        ([ServoBay(200, 33, 30, 14, 14, "top")], "cuts into joiner 1"),
        ([ServoBay(400, 45, 30, 14, 8), ServoBay(410, 50, 30, 14, 8)], "servo bay 2 overlaps servo bay 1"),
        ([ServoBay(400, 60, 30, 14, 14), ServoBay(400, 60, 30, 14, 14, "top")], "less than 1 mm of foam between them"),
    ],
)
def test_servo_bay_errors(items, message):
    with pytest.raises(CoreError, match=message):
        bays(*items)


def test_bays_on_both_sides_and_validation():
    assert len(bays(ServoBay(400, 45, 30, 14, 8), ServoBay(410, 50, 30, 14, 8, "top"))) == 2
    with pytest.raises(ValueError):
        ServoBay(depth=0)
    with pytest.raises(ValueError):
        ServoBay(side="left")


def test_outline_gap():
    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    assert outline_gap(square, [(12, 0), (14, 0), (14, 2), (12, 2)]) == pytest.approx(2)
    assert outline_gap(square, [(2, 2), (3, 2), (3, 3)]) == 0  # inside
    assert outline_gap(square, [(5, 5), (5, 20)]) == 0  # a segment crossing out
    assert outline_gap(square, [(5, 12), (5, 20)]) == pytest.approx(2)


def project_with_everything():
    project = default_project()
    project["title"] = "Test panel"
    project["part"].update(
        root_airfoil="sd7062", root_chord=240, tip_chord=200, span=550, sweep=20,
        spars=[{"position": 22, "width": 6, "depth_top": 1.5, "depth_bottom": 1.5}],
        channels=[{"position": 62, "diameter": 7, "offset": 2, "side": "top"}],
        joiners=[{"shape": "round", "diameter": 10, "position": 33, "position_tip": 33},
                 {"shape": "rect", "width": 3, "height": 14, "position": 43, "position_tip": 43, "side": "top"}],
        bays=[{"station": 330, "position": 62, "length": 32, "width": 13, "depth": 10, "side": "top"}],
    )  # fmt: skip
    return project


def test_project_with_joiners_and_bays():
    result = slice_project(project_with_everything())
    data = result.data
    core = data["stats"]["core"]
    assert (core["spars"], core["channels"], core["joiners"]) == (1, 1, 2)
    (bay,) = core["bays"]
    assert bay["from_root"] == 314 and bay["channels"] == [1] and bay["side"] == "top"
    assert len(data["part"]["solids"][0]["root"]["holes"]) == 3
    (shown,) = data["part"]["bays"]
    assert shown["z1"] - shown["z0"] == pytest.approx(32) and len(shown["root"]["outer"]) == 4
    assert any("2 joiner hole(s)" in w for w in data["warnings"])
    assert any("servo bay(s) cannot be cut by the wire" in w for w in data["warnings"])
    assert "(joiner 2: 3 x 14 mm at 43 %" in result.gcode
    assert "(servo bay 1 [cut by hand]: 32 x 13 mm, 10 mm deep, top" in result.gcode


def test_project_item_errors():
    project = project_with_everything()
    project["part"]["joiners"][0]["shape"] = 3
    with pytest.raises(SliceError, match="joiner 1: shape must be text"):
        slice_project(project)
    project = project_with_everything()
    project["part"]["bays"][0]["depth"] = 40
    with pytest.raises(SliceError, match="servo bay 1 leaves less than 1 mm"):
        slice_project(project)


def test_bay_template():
    name, svg = bay_template(project_with_everything())
    assert name == "Test panel-servo-bays.svg"
    doc = xml.dom.minidom.parseString(svg)
    root = doc.documentElement
    assert root.getAttribute("width").endswith("mm") and root.getAttribute("viewBox").startswith("0 0 ")
    assert float(root.getAttribute("width")[:-2]) == float(root.getAttribute("viewBox").split()[2])  # 1 unit = 1 mm
    assert "Bay 1: 32 × 13 mm, 10 mm deep" in svg and "50 mm" in svg
    assert "Top surface" in svg and "Bottom surface" not in svg
    with pytest.raises(SliceError, match="no servo bays"):
        bay_template(default_project())
    with pytest.raises(ValueError):
        bay_template_svg(WingPanel(FOIL, 250, 600))


def test_bay_rectangle_is_drawn_full_size():
    wing = WingPanel(airfoildb.get("sd7062"), 240, 550, bays=[ServoBay(300, 50, 30, 14, 8)])
    svg = bay_template_svg(wing)
    rects = xml.dom.minidom.parseString(svg).getElementsByTagName("rect")
    bay = next(r for r in rects if r.getAttribute("fill") == "#ffd9cc")
    assert float(bay.getAttribute("width")) == pytest.approx(30) and float(bay.getAttribute("height")) == pytest.approx(
        14
    )


def test_toml_joiners_bays_and_template(tmp_path, capsys):
    job = tmp_path / "job.toml"
    job.write_text(
        'title = "T"\n[wing]\nroot_airfoil = "sd7062"\nroot_chord = 240\nspan = 500\n'
        "[[wing.joiners]]\nposition = 33\ndiameter = 10\n"
        '[[wing.bays]]\nstation = 250\nposition = 55\nlength = 30\nwidth = 14\ndepth = 9\nside = "top"\n'
    )
    template = tmp_path / "bays.svg"
    assert main(["wing", str(job), "-o", str(tmp_path / "t.gcode"), "--bay-template", str(template)]) == 0
    out = capsys.readouterr().out
    assert "servo bay 1  cut by hand" in out
    assert "Bay 1:" in template.read_text()
    assert "(joiner 1: 10 mm round at 33 %" in (tmp_path / "t.gcode").read_text()
