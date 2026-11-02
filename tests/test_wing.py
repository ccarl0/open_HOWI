import pytest

from openhowi.airfoil import naca4
from openhowi.geometry import polyline_length
from openhowi.wing import WingPanel, place_section, project_to_towers


def flat(points):
    return [c for p in points for c in p]


def test_projection_straight_prism_is_identity():
    pts = [(0, 0), (10, 5), (20, 0)]
    xy, uv = project_to_towers(pts, 200, pts, 800, 1000)
    assert flat(xy) == pytest.approx(flat(pts))
    assert flat(uv) == pytest.approx(flat(pts))


def test_projection_extrapolates_linearly():
    # Root chord 100 at z=250, tip chord 50 at z=750: the wire line hits
    # chord 125 at z=0 and 25 at z=1000.
    xy, uv = project_to_towers([(0, 0), (100, 0)], 250, [(0, 0), (50, 0)], 750, 1000)
    assert xy[1][0] - xy[0][0] == pytest.approx(125)
    assert uv[1][0] - uv[0][0] == pytest.approx(25)


def test_projection_root_on_uv_side():
    xy, uv = project_to_towers([(0, 0), (100, 0)], 750, [(0, 0), (50, 0)], 250, 1000)
    assert xy[1][0] == pytest.approx(25)
    assert uv[1][0] == pytest.approx(125)


def test_projection_requires_matched_paths():
    with pytest.raises(ValueError):
        project_to_towers([(0, 0)], 0, [(0, 0), (1, 1)], 1, 2)


def test_place_section_chord_and_washout():
    foil = naca4("0012")
    pts = place_section(foil, 200, 30, x_le=10, y_le=5)
    le = pts[29]
    assert le == pytest.approx((10, 5))
    assert pts[0][0] == pytest.approx(210)

    twisted = place_section(foil, 200, 30, twist_deg=2.0, pivot=0.25)
    # Nose-down: the leading edge drops, the trailing edge rises.
    assert twisted[29][1] < 0
    assert twisted[0][1] > 0
    assert twisted[29][1] == pytest.approx(-50 * 0.0349, rel=1e-2)


def test_kerf_grows_the_wire_path():
    foil = naca4("2412")
    plain = WingPanel(foil, root_chord=200, span=500).root_contour()
    kerfed = WingPanel(foil, root_chord=200, span=500, root_kerf=2.0).root_contour()
    assert len(plain) == len(kerfed)
    assert max(y for _, y in kerfed) == pytest.approx(max(y for _, y in plain) + 1.0, abs=0.02)
    assert min(y for _, y in kerfed) == pytest.approx(min(y for _, y in plain) - 1.0, abs=0.02)
    assert polyline_length(kerfed) > polyline_length(plain)


def test_wing_panel_defaults_and_validation():
    foil = naca4("0012")
    panel = WingPanel(foil, root_chord=200, span=500)
    assert panel.tip_airfoil is foil
    assert panel.tip_chord == 200
    assert flat(panel.root_contour()) == pytest.approx(flat(panel.tip_contour()))
    with pytest.raises(ValueError):
        WingPanel(foil, root_chord=200, span=0)
    with pytest.raises(ValueError):
        WingPanel(foil, root_chord=200, span=500, root_kerf=-1)


def test_tip_contour_matches_root_point_count_with_different_airfoils():
    panel = WingPanel(naca4("2412"), root_chord=250, span=600, tip_airfoil=naca4("0009"), tip_chord=150)
    assert len(panel.root_contour()) == len(panel.tip_contour())
