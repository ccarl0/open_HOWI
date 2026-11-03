import itertools
import json
import math

import pytest

from openhowi.airfoil import naca4
from openhowi.config import load_wing_job
from openhowi.core import CoreError, CoreSpec, core_runs, materialize, run_counts
from openhowi.geometry import distance
from openhowi.machine import Machine
from openhowi.project import SliceError, default_project, slice_project
from openhowi.toolpath import Placement, build_wing_toolpath
from openhowi.wing import WingPanel, apply_kerf, place_section

FOIL = naca4("2412")


def section(chord=250.0, n=80, **spec):
    runs = core_runs(FOIL, chord, CoreSpec(**spec))
    return materialize(runs, run_counts(runs, n)), runs


def y_at(points, x, upper):
    """Surface height of a section at chord position x (upper or lower surface)."""
    candidates = []
    for a, b in itertools.pairwise(points):
        if min(a[0], b[0]) <= x <= max(a[0], b[0]) and a[0] != b[0]:
            t = (x - a[0]) / (b[0] - a[0])
            candidates.append(a[1] + (b[1] - a[1]) * t)
    return max(candidates) if upper else min(candidates)


def test_plain_core_is_the_resampled_airfoil():
    pts, runs = section()
    expected = place_section(FOIL, 250, 80)
    assert len(pts) == len(expected)
    assert max(distance(p, q) for p, q in zip(pts, expected)) < 1e-9
    assert runs.te_cutback == runs.le_cutback == 0


def test_sheeting_shrinks_each_surface_by_its_thickness():
    plain, _ = section()
    pts, runs = section(sheeting_top=1.5, sheeting_bottom=0.6)
    x = 0.3 * 250
    assert y_at(pts, x, True) == pytest.approx(y_at(plain, x, True) - 1.5, abs=0.05)
    assert y_at(pts, x, False) == pytest.approx(y_at(plain, x, False) + 0.6, abs=0.05)
    # Near the trailing edge the airfoil is thinner than both sheets: the core stops early.
    assert 5 < runs.te_cutback < 20
    assert max(p[0] for p in pts) == pytest.approx(250 - runs.te_cutback, abs=0.01)


def test_le_and_te_stock_cut_the_core_flat():
    pts, runs = section(le_stock=6, te_stock=10)
    assert min(p[0] for p in pts) == pytest.approx(6)
    assert max(p[0] for p in pts) == pytest.approx(240)
    assert pts[0][0] == pytest.approx(240) and pts[-1][0] == pytest.approx(240)  # vertical TE face
    assert pts[0][1] - pts[-1][1] > 1  # blunt: the face has height
    assert (runs.le_cutback, runs.te_cutback) == (6, 10)


def test_spar_slots_top_and_bottom():
    plain, _ = section()
    pts, _ = section(spar_width=6, spar_depth_top=2, spar_depth_bottom=3, spar_position=30)
    xc = 75.0
    top_floor = y_at(plain, xc, True) - 2
    bottom_floor = y_at(plain, xc, False) + 3

    def has(x, y):
        return any(abs(px - x) < 1e-9 and abs(py - y) < 0.05 for px, py in pts)

    # Floors measured from the airfoil surface at the slot centre (compared at 0.05 mm: the
    # reference here is the 80-point resampled outline, the core uses the full airfoil).
    assert has(72, top_floor) and has(78, top_floor)
    assert has(72, bottom_floor) and has(78, bottom_floor)
    # inside the slot the upper outline is at the floor, not the surface
    assert y_at(pts, xc, True) == pytest.approx(top_floor, abs=0.05)


def test_root_and_tip_stay_index_matched():
    wing = WingPanel(FOIL, root_chord=260, span=600, tip_airfoil=naca4("0010"), tip_chord=140, sweep=40, washout=2,
                     sheeting_top=1.5, sheeting_bottom=1.0, le_stock=4, te_stock=6, spar_width=5,
                     spar_depth_top=2, spar_depth_bottom=2, spar_position=28, spar_position_tip=32)  # fmt: skip
    root, tip, cutbacks = wing.cores()
    assert len(root) == len(tip)
    assert len(wing.root_contour()) == len(wing.tip_contour()) == len(root)
    assert cutbacks["root_le"] == cutbacks["tip_le"] == 4


@pytest.mark.parametrize(
    "spec, message",
    [
        ({"sheeting_top": 20, "sheeting_bottom": 20}, "no foam"),
        ({"spar_width": 6, "spar_depth_top": 2, "spar_position": 1}, "outside the core"),
        ({"spar_width": 6, "spar_depth_top": 15, "spar_depth_bottom": 15}, "less than 1 mm"),
        ({"le_stock": 150, "te_stock": 150}, "leave no core"),
        ({"spar_width": 40, "spar_depth_top": 0.2, "spar_position": 85}, "slopes too much"),
    ],
)
def test_core_errors(spec, message):
    with pytest.raises(CoreError, match=message):
        section(**spec)


def test_core_spec_validation():
    with pytest.raises(ValueError):
        CoreSpec(sheeting_top=-1)
    with pytest.raises(ValueError):
        CoreSpec(spar_position=0)


def test_kerf_on_a_sharp_trailing_edge_keeps_the_two_end_points_together():
    pts, _ = section()
    kerfed = apply_kerf(pts, 1.2)
    assert distance(kerfed[0], kerfed[-1]) < 1e-9
    assert kerfed[0][0] > 250  # pushed out behind the trailing edge


def test_toolpath_cuts_the_trailing_edge_face():
    machine = Machine(tower_distance=1000, x_travel=600, y_travel=300)
    wing = WingPanel(FOIL, root_chord=250, span=600, te_stock=10, root_kerf=1, tip_kerf=1)
    tp = build_wing_toolpath(wing, machine, Placement(root_offset=200, x_margin=25, y_offset=40))
    cut = [m for m in tp.moves if m.cutting]
    closing, lead_out = cut[-2], cut[-1]
    first = cut[0]  # the lead-in from X=0 ends on the first outline point (upper trailing-edge corner)
    assert closing.xy == first.xy  # back to the upper trailing-edge corner after the lower one
    assert distance(cut[-3].xy, closing.xy) > 1  # the face has height on a blunt core
    assert lead_out.xy == (0.0, first.xy[1])  # leaves along the entry cut


def test_project_with_core_options():
    project = default_project()
    project["part"].update(sheeting_top=1.5, sheeting_bottom=1.5, le_stock=5, spar_width=6, spar_depth_top=2,
                           spar_depth_bottom=2, spar_position=30)  # fmt: skip
    result = slice_project(project)
    data = result.data
    assert data["stats"]["core"]["le_cutback"] == {"root": 5.0, "tip": 5.0}
    assert data["stats"]["core"]["te_cutback"]["root"] > 5
    assert any("short of the trailing edge" in w for w in data["warnings"])
    assert len(data["part"]["skins"]) == 1
    assert "(spar slots 6 mm wide at 30 %: top 2 mm, bottom 2 mm deep)" in result.gcode
    json.dumps(data)
    project["part"]["spar_depth_top"] = 40
    with pytest.raises(SliceError, match="less than 1 mm"):
        slice_project(project)


def test_plain_project_has_no_core_stats():
    data = slice_project(default_project()).data
    assert "core" not in data["stats"]
    assert data["part"]["skins"] == []


def test_toml_job_accepts_core_options(tmp_path):
    cfg = tmp_path / "job.toml"
    cfg.write_text(
        '[wing]\nroot_airfoil = "naca2412"\nroot_chord = 250\nspan = 500\n'
        "sheeting_top = 1.5\nsheeting_bottom = 1.5\nspar_width = 6\nspar_depth_top = 2\n"
    )
    job = load_wing_job(cfg)
    assert job.wing.sheeting_top == 1.5 and job.wing.core_spec().slot_top
    assert math.isfinite(job.wing.cores()[2]["root_te"])
