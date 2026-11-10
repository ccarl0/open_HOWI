import math

import pytest

from openhowi import curves
from openhowi.drawing import ShapeError
from openhowi.dxfimport import parse_dxf
from openhowi.geometry import bounds, distance, signed_area
from openhowi.svgimport import parse_path, parse_svg


def dxf(*pairs, units=None):
    body = []
    if units is not None:
        body += [(0, "SECTION"), (2, "HEADER"), (9, "$INSUNITS"), (70, str(units)), (0, "ENDSEC")]
    body += [(0, "SECTION"), (2, "ENTITIES"), *pairs, (0, "ENDSEC"), (0, "EOF")]
    return "\n".join(f"{c}\n{v}" for c, v in body) + "\n"


def svg(body, attrs='width="100mm" height="100mm" viewBox="0 0 100 100"'):
    return f'<svg xmlns="http://www.w3.org/2000/svg" {attrs}>{body}</svg>'


def box(poly):
    return [round(v, 3) for v in bounds(poly.points)]


# Curves -------------------------------------------------------------------------------------------


def test_bulge_semicircle():
    pts = curves.bulge((0, 0), (2, 0), 1.0, 0.001)
    assert pts[-1] == (2, 0)
    assert all(math.hypot(x - 1, y) == pytest.approx(1, abs=1e-9) for x, y in pts)
    assert min(y for _, y in pts) == pytest.approx(-1, abs=1e-3)  # positive bulge = counter-clockwise


def test_cubic_stays_within_tolerance():
    p0, p1, p2, p3 = (0, 0), (0, 10), (10, 10), (10, 0)
    pts = [p0] + curves.cubic(p0, p1, p2, p3, 0.01)
    assert pts[-1] == p3
    peak = max(y for _, y in pts)
    assert peak == pytest.approx(7.5, abs=0.02)  # true maximum of this Bézier


def test_svg_arc_lands_on_endpoint_and_respects_flags():
    small = curves.svg_arc((0, 0), (10, 0), 10, 10, 0, False, True, 0.01)
    large = curves.svg_arc((0, 0), (10, 0), 10, 10, 0, True, True, 0.01)
    assert small[-1] == large[-1] == (10, 0)
    assert len(large) > len(small)


def test_bspline_clamped_passes_through_ends():
    ctrl = [(0, 0), (5, 10), (10, 0)]
    pts = curves.bspline(ctrl, [0, 0, 0, 1, 1, 1], 2, None, 20)
    assert pts[0] == pytest.approx((0, 0))
    assert pts[-1] == pytest.approx((10, 0))
    assert pts[10] == pytest.approx((5, 5))
    with pytest.raises(ValueError):
        curves.bspline(ctrl, [0, 1], 2, None, 5)


# SVG ------------------------------------------------------------------------------------------------


def test_svg_units_and_y_flip():
    (p,) = parse_svg(
        svg('<rect x="10" y="20" width="30" height="40"/>', 'width="50mm" viewBox="0 0 100 100"')
    ).polylines
    assert p.closed
    assert box(p) == [5.0, -30.0, 20.0, -10.0]  # half scale, y pointing up


def test_svg_default_units_are_css_pixels():
    (p,) = parse_svg(svg('<rect width="96" height="96"/>', "")).polylines
    assert box(p) == [0.0, -25.4, 25.4, 0.0]


def test_svg_transforms_nest():
    body = '<g transform="translate(50 50)"><g transform="rotate(90)"><rect width="10" height="20"/></g></g>'
    (p,) = parse_svg(svg(body)).polylines
    assert box(p) == pytest.approx([30.0, -60.0, 50.0, -50.0])


def test_svg_path_commands():
    d = "M0 0 h10 v10 H0 Z m20 0 l10 0 0 10 -10 0z M40 0 Q50 10 60 0 T80 0 M0 20 C0 30 10 30 10 20 S20 10 20 20 a5 5 0 0110 0"
    polys = parse_path(d, 0.01)
    assert len(polys) == 4
    assert polys[0].closed and polys[1].closed
    assert box(polys[1]) == [20, 0, 30, 10]
    assert polys[2].points[-1] == pytest.approx((80, 0))
    assert polys[3].points[-1] == pytest.approx((30, 20))


def test_svg_circle_ellipse_polygon_line():
    body = (
        '<circle cx="50" cy="50" r="10"/><ellipse cx="20" cy="20" rx="10" ry="5"/>'
        '<polygon points="0,0 10,0 5,8"/><polyline points="0 0 5 5 10 0"/><line x1="0" y1="0" x2="5" y2="0"/>'
    )
    polys = parse_svg(svg(body), tol=0.01).polylines
    assert [p.closed for p in polys] == [True, True, True, False, False]
    assert abs(signed_area(polys[0].points)) == pytest.approx(math.pi * 100, rel=0.01)


def test_svg_use_hidden_and_text():
    body = (
        '<defs><rect id="r" width="10" height="10"/></defs>'
        '<use href="#r" x="20"/><use href="#r" x="40"/>'
        '<rect width="5" height="5" style="display: none"/><text>Hi</text>'
    )
    drawing = parse_svg(svg(body))
    assert sorted(box(p)[0] for p in drawing.polylines) == [20, 40]
    assert "text" in drawing.warnings[0]


def test_svg_rejects_entities_and_garbage():
    with pytest.raises(ShapeError):
        parse_svg('<!DOCTYPE svg [<!ENTITY a "aaaa">]><svg/>')
    with pytest.raises(ShapeError):
        parse_svg("not xml")
    with pytest.raises(ShapeError):
        parse_svg("<html/>")
    with pytest.raises(ShapeError, match="path"):
        parse_svg(svg('<path d="M 0 0 X 5"/>'))


def test_svg_clone_explosion_is_stopped():
    body = '<rect id="g0" width="1" height="1"/>'
    for level in range(1, 12):
        body += f'<g id="g{level}">' + "".join(f'<use href="#g{level - 1}"/>' for _ in range(10)) + "</g>"
    with pytest.raises(ShapeError, match="too complex"):
        parse_svg(svg(f"<defs>{body}</defs><use href='#g11'/>"))


# DXF ------------------------------------------------------------------------------------------------


def test_dxf_lwpolyline_with_bulge_and_units():
    text = dxf(
        (0, "LWPOLYLINE"), (90, "2"), (70, "1"), (10, "0"), (20, "0"), (42, "1"), (10, "2"), (20, "0"), (42, "1"),
        units=1,  # inches
    )  # fmt: skip
    (p,) = parse_dxf(text, tol=0.01).polylines
    assert p.closed
    assert abs(signed_area(p.points)) == pytest.approx(math.pi * 25.4**2, rel=0.01)  # unit circle, in mm²


def test_dxf_polyline_vertices():
    text = dxf(
        (0, "POLYLINE"), (66, "1"), (70, "1"),
        (0, "VERTEX"), (10, "0"), (20, "0"), (0, "VERTEX"), (10, "10"), (20, "0"), (0, "VERTEX"), (10, "10"), (20, "5"),
        (0, "SEQEND"),
    )  # fmt: skip
    (p,) = parse_dxf(text).polylines
    assert p.closed and p.points == [(0, 0), (10, 0), (10, 5), (0, 0)]


def test_dxf_circle_arc_ellipse_spline():
    text = dxf(
        (0, "CIRCLE"), (10, "0"), (20, "0"), (40, "5"),
        (0, "ARC"), (10, "0"), (20, "0"), (40, "10"), (50, "0"), (51, "90"),
        (0, "ELLIPSE"), (10, "0"), (20, "0"), (11, "10"), (21, "0"), (40, "0.5"), (41, "0"), (42, str(2 * math.pi)),
        (0, "SPLINE"), (70, "0"), (71, "2"), (72, "6"), (73, "3"),
        (40, "0"), (40, "0"), (40, "0"), (40, "1"), (40, "1"), (40, "1"),
        (10, "0"), (20, "0"), (10, "5"), (20, "10"), (10, "10"), (20, "0"),
    )  # fmt: skip
    circle, arc, ellipse, spline = parse_dxf(text, tol=0.01).polylines
    assert circle.closed and box(circle) == pytest.approx([-5, -5, 5, 5], abs=0.01)
    assert arc.points[0] == pytest.approx((10, 0)) and arc.points[-1] == pytest.approx((0, 10))
    assert ellipse.closed and box(ellipse) == pytest.approx([-10, -5, 10, 5], abs=0.01)
    assert spline.points[0] == pytest.approx((0, 0)) and spline.points[-1] == pytest.approx((10, 0))


def test_dxf_insert_and_mirrored_arc():
    text = "\n".join(
        f"{c}\n{v}"
        for c, v in [
            (0, "SECTION"), (2, "BLOCKS"), (0, "BLOCK"), (2, "SQ"), (10, "0"), (20, "0"),
            (0, "LWPOLYLINE"), (90, "4"), (70, "1"), (10, "0"), (20, "0"), (10, "1"), (20, "0"), (10, "1"), (20, "1"),
            (10, "0"), (20, "1"), (0, "ENDBLK"), (0, "ENDSEC"),
            (0, "SECTION"), (2, "ENTITIES"),
            (0, "INSERT"), (2, "SQ"), (10, "100"), (20, "0"), (41, "10"), (42, "10"), (50, "90"),
            (0, "ARC"), (10, "5"), (20, "0"), (40, "1"), (50, "0"), (51, "90"), (230, "-1"),
            (0, "TEXT"), (1, "note"), (0, "DIMENSION"),
            (0, "ENDSEC"), (0, "EOF"),
        ]
    )  # fmt: skip
    drawing = parse_dxf(text)
    square, arc = drawing.polylines
    assert box(square) == pytest.approx([90, 0, 100, 10])  # scaled x10, rotated 90° about the insertion point
    assert arc.points[0] == pytest.approx((-6, 0))  # extrusion -Z: centre (5, 0) and start (6, 0) mirror in x
    assert "1 DIMENSION" in drawing.warnings[0] and "1 TEXT" in drawing.warnings[0]


def test_dxf_errors():
    with pytest.raises(ShapeError, match="binary"):
        parse_dxf("AutoCAD Binary DXF\r\n\x1a\x00")
    with pytest.raises(ShapeError):
        parse_dxf("hello\nworld\n")
    with pytest.raises(ShapeError, match="ENTITIES"):
        parse_dxf("0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n")


def test_dxf_recursive_inserts_are_bounded():
    pairs = [(0, "SECTION"), (2, "BLOCKS")]
    pairs += [(0, "BLOCK"), (2, "B0"), (10, "0"), (20, "0"), (0, "ENDBLK")]
    for level in range(1, 9):
        pairs += [(0, "BLOCK"), (2, f"B{level}"), (10, "0"), (20, "0")]
        pairs += [(0, "INSERT"), (2, f"B{level - 1}")] * 20
        pairs += [(0, "ENDBLK")]
    pairs += [(0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES"), (0, "INSERT"), (2, "B8"), (0, "ENDSEC"), (0, "EOF")]
    with pytest.raises(ShapeError, match="too complex"):
        parse_dxf("\n".join(f"{c}\n{v}" for c, v in pairs))


def test_dxf_lines_keep_their_endpoints():
    (line,) = parse_dxf(dxf((0, "LINE"), (10, "1"), (20, "2"), (11, "4"), (21, "6"))).polylines
    assert distance(*line.points) == pytest.approx(5)
