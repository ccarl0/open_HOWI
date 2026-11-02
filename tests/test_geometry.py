import math

import pytest

from openhowi.geometry import cosine_spacing, offset_polyline, polyline_length, resample, signed_area


def test_resample_keeps_ends_and_count():
    pts = [(0, 0), (10, 0), (10, 10)]
    out = resample(pts, cosine_spacing(7))
    assert len(out) == 7
    assert out[0] == (0, 0)
    assert out[-1] == (10, 10)
    assert resample(pts, [0.5]) == [pytest.approx((10, 0))]


def test_resample_ignores_duplicate_points():
    assert resample([(0, 0), (0, 0), (4, 0)], [0.25]) == [pytest.approx((1, 0))]


def test_signed_area_orientation():
    square = [(0, 0), (1, 0), (1, 1), (0, 1)]
    assert signed_area(square) == pytest.approx(1)
    assert signed_area(square[::-1]) == pytest.approx(-1)


def test_offset_circle_grows_radius():
    n = 72
    circle = [(10 * math.cos(2 * math.pi * i / n), 10 * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]
    # Counter-clockwise: outward is to the right, i.e. a negative offset.
    out = offset_polyline(circle, -0.5)
    assert len(out) == len(circle)
    for x, y in out[1:-1]:
        assert math.hypot(x, y) == pytest.approx(10.5, rel=1e-3)


def test_offset_square_corner_is_mitred():
    out = offset_polyline([(0, 0), (10, 0), (10, 10)], 1.0)  # left of travel = inside the corner
    assert out[1] == pytest.approx((9, 1))
    out = offset_polyline([(0, 0), (10, 0), (10, 10)], -1.0)
    assert out[1] == pytest.approx((11, -1))


def test_offset_rejects_zero_length_segments():
    with pytest.raises(ValueError):
        offset_polyline([(0, 0), (0, 0), (1, 0)], 1.0)


def test_polyline_length():
    assert polyline_length([(0, 0), (3, 4), (3, 10)]) == pytest.approx(11)
