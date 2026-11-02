import pytest

from openhowi.airfoil import AirfoilError, load_airfoil, naca4, parse_dat
from openhowi.geometry import signed_area

SELIG = """MY FOIL
1.0 0.0
0.5 0.06
0.0 0.0
0.5 -0.04
1.0 0.0
"""

LEDNICER = """MY FOIL LEDNICER
3. 3.

0.0 0.0
0.5 0.06
1.0 0.0

0.0 0.0
0.5 -0.04
1.0 0.0
"""


def flat(points):
    return [c for p in points for c in p]


def test_parse_selig():
    foil = parse_dat(SELIG)
    assert foil.name == "MY FOIL"
    assert foil.points[foil.le_index] == (0.0, 0.0)
    assert signed_area(foil.points) > 0


def test_lednicer_matches_selig():
    assert parse_dat(LEDNICER).points == parse_dat(SELIG).points


def test_reversed_file_is_reoriented():
    lines = SELIG.splitlines()
    reversed_text = "\n".join([lines[0]] + lines[1:][::-1])
    assert parse_dat(reversed_text).points == parse_dat(SELIG).points


def test_parse_rejects_garbage():
    with pytest.raises(AirfoilError):
        parse_dat("name\n1.0 0.0\nnot a number\n")


def test_naca_symmetric_and_thickness():
    foil = naca4("0012", n=101)
    upper, lower = foil.upper(), foil.lower()
    for (xu, yu), (xl, yl) in zip(reversed(upper), lower):
        assert xu == pytest.approx(xl)
        assert yu == pytest.approx(-yl)
    assert max(y for _, y in foil.points) == pytest.approx(0.06, abs=5e-4)
    assert foil.points[0] == pytest.approx(foil.points[-1])  # closed trailing edge


def test_naca_cambered_upper_above_lower():
    foil = naca4("NACA 2412")
    assert foil.name == "NACA 2412"
    assert min(y for _, y in foil.upper()) >= -1e-12
    assert signed_area(foil.points) > 0


@pytest.mark.parametrize("code", ["241", "abcd", "2012", "0000"])
def test_naca_invalid(code):
    with pytest.raises(AirfoilError):
        naca4(code)


def test_resampled_aligns_leading_edge():
    a = naca4("2412").resampled(40)
    b = parse_dat(SELIG).resampled(40)
    assert len(a.points) == len(b.points) == 79
    assert a.le_index == b.le_index == 39
    assert a.points[0] == pytest.approx(naca4("2412").points[0])
    assert a.points[-1] == pytest.approx(naca4("2412").points[-1])


def test_normalized_scales_to_unit_chord():
    pts = [(x * 200 + 10, y * 200 + 5) for x, y in parse_dat(SELIG).points]
    text = "big\n" + "\n".join(f"{x} {y}" for x, y in pts)
    foil = parse_dat(text).normalized()
    assert flat(foil.points) == pytest.approx(flat(parse_dat(SELIG).points))


def test_load_airfoil_spec_and_file(tmp_path):
    assert load_airfoil("naca0012").name == "NACA 0012"
    (tmp_path / "foil.dat").write_text(naca4("4415").to_dat())
    foil = load_airfoil("foil.dat", base_dir=tmp_path)
    assert foil.name == "NACA 4415"
    assert flat(foil.points) == pytest.approx(flat(naca4("4415").points), abs=1e-6)


def test_lednicer_count_mismatch_is_reported():
    with pytest.raises(AirfoilError, match="Lednicer"):
        parse_dat(LEDNICER.replace("3. 3.", "3. 4."))
