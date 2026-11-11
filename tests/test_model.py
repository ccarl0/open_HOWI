import base64
import copy
import math

import pytest

from openhowi.fuselage import stl_binary
from openhowi.geometry import signed_area
from openhowi.machine import Machine
from openhowi.mesh import Mesh, MeshError, cached_mesh, read_stl
from openhowi.modelcut import ModelOptions, build_job, job_labels, mesh_volume, slab_ranges
from openhowi.project import (
    DEFAULT_PARTS,
    DEFAULT_PLACEMENTS,
    SliceError,
    default_project,
    inspect_model,
    slice_project,
)
from openhowi.shapes import Region, ShapeError, layout_shapes
from openhowi.toolpath import Placement

MACHINE = Machine()
PLACEMENT = Placement("xy", 30, 20, 10)


def kerfs(ratio):
    return 1.0, 1.0


def box(x, y, z):
    """A closed box from the origin, as triangles."""
    v = [(i * x, j * y, k * z) for i in (0, 1) for j in (0, 1) for k in (0, 1)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    tris = []
    for a, b, c, d in faces:
        tris += [(v[a], v[b], v[c]), (v[a], v[c], v[d])]
    return tris


def sphere(r, n=32, m=64):
    def p(i, j):
        t, f = math.pi * i / n, 2 * math.pi * j / m
        return (r * math.sin(t) * math.cos(f), r * math.sin(t) * math.sin(f), r * math.cos(t))

    tris = []
    for i in range(n):
        for j in range(m):
            a, b, c, d = p(i, j), p(i + 1, j), p(i + 1, j + 1), p(i, j + 1)
            if i:
                tris.append((a, b, d))
            if i < n - 1:
                tris.append((b, c, d))
    return tris


def torus(big, small, n=64, m=32):
    def p(i, j):
        u, w = 2 * math.pi * i / n, 2 * math.pi * j / m
        rr = big + small * math.cos(w)
        return (rr * math.cos(u), rr * math.sin(u), small * math.sin(w))

    tris = []
    for i in range(n):
        for j in range(m):
            a, b, c, d = p(i, j), p(i + 1, j), p(i + 1, j + 1), p(i, j + 1)
            tris += [(a, b, c), (a, c, d)]
    return tris


def ascii_stl(tris):
    lines = ["solid test"]
    for t in tris:
        lines += [" facet normal 0 0 0", "  outer loop"]
        lines += [f"   vertex {x} {y} {z}" for x, y, z in t]
        lines += ["  endloop", " endfacet"]
    return ("\n".join([*lines, "endsolid test"]) + "\n").encode()


# -- reading and turning ---------------------------------------------------------------------


def test_reads_binary_and_text_stl():
    tris = box(10, 20, 30)
    assert len(read_stl(stl_binary(tris))) == 12
    assert len(read_stl(ascii_stl(tris))) == 12
    assert [c for p in read_stl(ascii_stl(tris))[0] for c in p] == pytest.approx([c for p in tris[0] for c in p])


@pytest.mark.parametrize(
    "data, message",
    [(b"", "not an STL"), (b"hello world" * 20, "not an STL"), (b"solid x\nendsolid x\n", "no triangles")],
)
def test_rejects_what_is_not_a_model(data, message):
    with pytest.raises(MeshError, match=message):
        read_stl(data)


def test_turns_the_model_the_right_way_up():
    tris = box(100, 20, 50)  # file: 100 long (X), 20 deep (Y), 50 high (Z)
    assert Mesh.build(tris, "+z").size == pytest.approx((100, 50, 20))  # length, up, along the wire
    assert Mesh.build(tris, "+y").size == pytest.approx((100, 20, 50))
    assert Mesh.build(tris, "+z", spin=90).size == pytest.approx((20, 50, 100))
    assert Mesh.build(tris, "+z", scale=2).size == pytest.approx((200, 100, 40))
    mesh = Mesh.build(tris, "-x")
    assert min(c for t in mesh.triangles for p in t for c in p) == pytest.approx(0.0)
    with pytest.raises(MeshError):
        Mesh.build(tris, "up")
    with pytest.raises(MeshError):
        Mesh.build(tris, spin=45)


def test_a_turned_model_is_not_mirrored():
    # A right-handed corner stays right-handed whichever way up the model is turned.
    tris = sphere(10, 8, 16)
    for up in ("+z", "-z", "+y", "-y", "+x", "-x"):
        for spin in (0, 90, 180, 270):
            assert mesh_volume(Mesh.build(tris, up, spin)) == pytest.approx(mesh_volume(Mesh.build(tris, "+y")))
    flat = Mesh.build([((0, 0, 0), (1, 0, 0), (0, 1, 0))] * 2 + box(1, 1, 1), "+y")
    assert flat.size == pytest.approx((1, 1, 1))


# -- outlines ---------------------------------------------------------------------------------


def test_silhouettes_of_a_box_and_a_sphere():
    mesh = Mesh.build(box(120, 40, 60), "+y")
    side = mesh.silhouette("side", 0.5)
    assert len(side) == 1 and signed_area(side[0].outer) == pytest.approx(120 * 40, rel=0.02)
    top = mesh.silhouette("top", 0.5)
    assert signed_area(top[0].outer) == pytest.approx(120 * 60, rel=0.02)
    ball = Mesh.build(sphere(50), "+z")
    for view in ("side", "top", "front"):
        (region,) = ball.silhouette(view, 0.5)
        assert signed_area(region.outer) == pytest.approx(math.pi * 50**2, rel=0.01)


def test_a_ring_seen_from_above_has_a_hole_and_cut_across_two_pieces():
    ring = Mesh.build(torus(60, 20), "+z")
    (top,) = ring.silhouette("top", 0.5)
    assert len(top.holes) == 1
    assert -signed_area(top.holes[0]) == pytest.approx(math.pi * 40**2, rel=0.02)
    pieces, odd = ring.section(ring.size[2] / 2, 0.5)
    assert odd == 0 and len(pieces) == 2
    assert [signed_area(p.outer) for p in pieces] == pytest.approx([math.pi * 20**2] * 2, rel=0.02)


def test_sections_of_a_sphere():
    ball = Mesh.build(sphere(50), "+z")
    (middle,), odd = ball.section(50, 0.5)
    assert odd == 0 and signed_area(middle.outer) == pytest.approx(math.pi * 50**2, rel=0.01)
    (high,), _ = ball.section(90, 0.5)
    assert signed_area(high.outer) == pytest.approx(math.pi * (50**2 - 40**2), rel=0.02)
    assert ball.section(100.5, 0.5)[0] == []


def test_a_lighter_copy_for_the_3d_view():
    ball = Mesh.build(sphere(50, 64, 128), "+z")
    coords, faces = ball.display(limit=2000)
    assert len(faces) // 3 <= 2000 and len(coords) % 3 == 0
    assert max(faces) < len(coords) // 3


def test_the_volume_of_a_closed_model():
    assert mesh_volume(Mesh.build(box(10, 20, 30), "+y")) == pytest.approx(6000)


# -- cutting ----------------------------------------------------------------------------------


def det(rows):
    (a, b, c), (d, e, f), (g, h, i) = rows
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def test_two_views_cut_the_side_then_the_top_from_the_same_block():
    mesh = Mesh.build(box(200, 40, 60), "+y")
    options = ModelOptions("two_view", spare=5)
    assert job_labels(mesh, options) == ["Pass 1 · side view", "Pass 2 · top view"]
    side = build_job(mesh, options, 0, MACHINE, PLACEMENT, kerfs)
    top = build_job(mesh, options, 1, MACHINE, PLACEMENT, kerfs)
    for job, up in ((side, 40), (top, 60)):
        xs = [p[0] for r in job.root_regions for p in r.outer]
        ys = [p[1] for r in job.root_regions for p in r.outer]
        # The model's corner lands 5 mm inside the foam's rear face (X 20) and underside (Y 10).
        assert min(xs) == pytest.approx(25, abs=0.5) and max(xs) == pytest.approx(225, abs=0.5)
        assert min(ys) == pytest.approx(15, abs=0.5) and max(ys) == pytest.approx(15 + up, abs=0.5)
        assert det(job.placement[1]) == pytest.approx(1)  # turned, never mirrored
    assert side.block["z1"] - side.block["z0"] == pytest.approx(70)  # the model's width + spare along the wire
    assert top.block["z1"] - top.block["z0"] == pytest.approx(50)  # turned: its height runs along the wire
    assert "Keep every offcut" in side.notes[1] and "quarter turn" in top.notes[0]


def test_slabs_follow_the_model_across_the_wire():
    mesh = Mesh.build(sphere(60), "+z")
    options = ModelOptions("slabs", thickness=40)
    assert slab_ranges(mesh, options) == [(0, 40), (40, 80), (80, 120)]
    for index in range(3):
        job = build_job(mesh, options, index, MACHINE, PLACEMENT, kerfs)
        # Root and tip outlines match point for point (the wire joins them) and the slab sits midway.
        assert [len(r.outer) for r in job.root_regions] == [len(t.outer) for t in job.tip_regions]
        assert (job.z_root + job.z_tip) / 2 == pytest.approx(MACHINE.tower_distance / 2)
        assert abs(job.z_tip - job.z_root) == pytest.approx(40)
        assert det(job.placement[1]) == pytest.approx(1)
    middle = build_job(mesh, options, 1, MACHINE, PLACEMENT, kerfs)
    assert not any("faster than the wire" in w for w in middle.warnings)


def test_a_slab_that_changes_too_fast_is_cut_as_far_as_the_machine_reaches():
    mesh = Mesh.build(sphere(60), "+z")
    job = build_job(mesh, ModelOptions("slabs", thickness=25), 0, MACHINE, PLACEMENT, kerfs)
    assert any("faster than the wire can follow" in w for w in job.warnings)
    lo_x, lo_y, hi_x, hi_y = job.toolpath.extents()["UV"]
    assert 0 <= lo_x and hi_x <= MACHINE.x_travel and 0 <= lo_y and hi_y <= MACHINE.y_travel


def test_slabs_from_the_other_carriage_are_not_mirrored():
    mesh = Mesh.build(box(100, 30, 60), "+y")
    job = build_job(mesh, ModelOptions("slabs", thickness=30), 0, MACHINE, Placement("uv", 30, 20, 10), kerfs)
    assert job.z_tip < job.z_root
    assert det(job.placement[1]) == pytest.approx(1)


def test_a_ring_cut_in_slabs_comes_out_in_pieces():
    mesh = Mesh.build(torus(60, 20), "+z")
    labels = job_labels(mesh, ModelOptions("slabs", thickness=40))
    assert len(labels) == 4
    job = build_job(mesh, ModelOptions("slabs", thickness=40), 1, MACHINE, PLACEMENT, kerfs)
    assert len(job.root_regions) == 2


def test_options_are_checked():
    with pytest.raises(ValueError):
        ModelOptions("turned")
    with pytest.raises(ValueError):
        ModelOptions(thickness=1)
    with pytest.raises(ValueError):
        ModelOptions(spare=0)


def test_tip_outlines_must_match_the_root_ones():
    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    with pytest.raises(ShapeError, match="point for point"):
        layout_shapes([Region(square)], MACHINE, PLACEMENT, 50, tip_regions=[Region(square[:3])])


# -- in the app -------------------------------------------------------------------------------


def model_project(tris, **part):
    project = default_project()
    data = base64.b64encode(stl_binary(tris)).decode()
    project["part"] = copy.deepcopy(DEFAULT_PARTS["model"]) | {
        "source": {"name": "m.stl", "format": "stl", "data": data}
    }
    project["part"].update(part)
    project["placement"] = copy.deepcopy(DEFAULT_PLACEMENTS["model"])
    return project


def test_a_3d_model_part_slices_in_the_app():
    project = model_project(box(200, 60, 40), up="+z")  # Z up: 200 long, 40 high, 60 wide
    data = slice_project(project).data
    assert data["kind"] == "model" and [j["label"] for j in data["jobs"]] == ["Pass 1 · side view", "Pass 2 · top view"]
    assert data["stats"]["model"]["size"] == [200, 40, 60]
    assert data["stats"]["model"]["volume_l"] == pytest.approx(0.48)
    mesh = data["part"]["mesh"]
    assert len(mesh["positions"]) % 3 == 0 and mesh["indices"]
    assert [v["label"] for v in data["views"]] == ["Side", "Top", "Front"]
    assert "G93" in slice_project(project).gcode
    project["part"].update(method="slabs", thickness=20, job=3)
    data = slice_project(project).data
    assert len(data["jobs"]) == 3 and data["job"] == 3 and data["views"][-1]["label"] == "Face at 60 mm"


def test_model_problems_are_reported_plainly():
    project = model_project(box(10, 10, 10))
    project["part"]["source"] = None
    with pytest.raises(SliceError, match="load an STL"):
        slice_project(project)
    project["part"]["source"] = {"name": "m.stl", "format": "stl", "data": "%%%"}
    with pytest.raises(SliceError, match="damaged"):
        slice_project(project)
    project["part"]["source"] = {"name": "m.stl", "format": "stl", "data": base64.b64encode(b"nonsense").decode()}
    with pytest.raises(SliceError, match="not an STL"):
        slice_project(project)


def test_inspecting_a_loaded_file():
    info = inspect_model("part.stl", base64.b64encode(stl_binary(box(10, 20, 30))).decode())
    assert info == {"name": "part.stl", "format": "stl", "triangles": 12, "size": [10, 20, 30]}


def test_the_same_file_is_read_once():
    data = stl_binary(box(10, 20, 30))
    assert cached_mesh(data, "+z", 0, 1.0) is cached_mesh(data, "+z", 0.0, 1.0)
