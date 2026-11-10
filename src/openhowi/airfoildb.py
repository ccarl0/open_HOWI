# SPDX-License-Identifier: GPL-3.0-or-later
"""A built-in database of common model-aircraft airfoils.

The coordinate files in ``openhowi/airfoils`` come from the UIUC Airfoil
Coordinates Database (Michael Selig, UIUC Applied Aerodynamics Group), as
redistributed with AeroSandbox under the MIT license; see the README there.
NACA 4-digit entries are generated rather than stored.

Airfoils are referred to by a short id such as ``"sd7037"`` or ``"naca2412"``.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any

from .airfoil import Airfoil, AirfoilError, naca4, parse_dat
from .geometry import Point, cosine_spacing, resample

CATEGORIES = (
    "Trainer & sport",
    "Gliders",
    "Flying wings",
    "Symmetric & tail",
    "High lift & slow flyers",
    "Full-size & scale",
)


@dataclass(frozen=True)
class Entry:
    id: str
    name: str
    category: str
    note: str


_T, _G, _W, _S, _H, _F = CATEGORIES
ENTRIES: tuple[Entry, ...] = (
    Entry("clarky", "Clark Y", _T, "The classic flat-bottomed section: forgiving and easy to build."),
    Entry("usa35b", "USA-35B", _T, "Flat-bottomed section for trainers and old-timers."),
    Entry("goe398", "Göttingen 398", _T, "Thick, well-cambered classic for slow trainers and old-timers."),
    Entry("s7055", "Selig S7055", _T, "Flat-bottomed sport section."),
    Entry("sd7062", "SD7062", _T, "Thick sport and trainer section with a gentle stall."),
    Entry("e205", "Eppler E205", _T, "All-round sport and glider section."),
    Entry("e214", "Eppler E214", _T, "Cambered section for slow sport models and gliders."),
    Entry("naca2412", "NACA 2412", _T, "General-purpose sport section (generated)."),
    Entry("naca2415", "NACA 2415", _T, "Thicker sport section for stiffer cores (generated)."),
    Entry("naca4412", "NACA 4412", _T, "Well-cambered, nearly flat-bottomed section (generated)."),
    Entry("sd7037", "SD7037", _G, "Popular all-round thermal and sport glider section."),
    Entry("sd7080", "SD7080", _G, "Thermal and sport glider section."),
    Entry("sd7003", "SD7003", _G, "Thin, low-drag section for small gliders and hand launch."),
    Entry("s3021", "Selig S3021", _G, "Thermal glider section."),
    Entry("s4061", "Selig S4061", _G, "Thermal glider section."),
    Entry("s7012", "Selig S7012", _G, "Low-drag section for electric and slope gliders."),
    Entry("s7075", "Selig S7075", _G, "Thermal duration section."),
    Entry("s9000", "Selig S9000", _G, "Low-camber glider section."),
    Entry("sa7035", "SA7035", _G, "Thermal glider series, least camber."),
    Entry("sa7036", "SA7036", _G, "Thermal glider series, medium camber."),
    Entry("sa7038", "SA7038", _G, "Thermal glider series, most camber."),
    Entry("e387", "Eppler E387", _G, "Thoroughly wind-tunnel-tested low-speed section."),
    Entry("e374", "Eppler E374", _G, "Low-camber section for sport and slope gliders."),
    Entry("rg15", "RG-15", _G, "Classic slope racing and F3B section."),
    Entry("rg14", "RG 14", _G, "Thin speed section."),
    Entry("mh32", "MH 32", _G, "Low-drag section for F3B/F3J and electric gliders."),
    Entry("mh30", "MH 30", _G, "Thin speed section for racers."),
    Entry("hq2510", "HQ 2.5/10", _G, "Quabeck thermal and slope section."),
    Entry("fx60100", "Wortmann FX 60-100", _G, "Thermal glider section."),
    Entry("sd6060", "SD6060", _G, "Low-camber section for sport and aerobatic slope models."),
    Entry("hs520", "HS 520", _G, "F5D and F3F slope racing section."),
    Entry("ag24", "AG24", _G, "Hand-launch glider section (root of the AG24/25/26 set)."),
    Entry("ag25", "AG25", _G, "Hand-launch glider section (mid span)."),
    Entry("ag26", "AG26", _G, "Hand-launch glider section (tip)."),
    Entry("mh45", "MH 45", _W, "Low-moment section for flying wings and tailless gliders."),
    Entry("mh60", "MH 60", _W, "Flying-wing section, a little thicker than the MH 45."),
    Entry("mh49", "MH 49", _W, "Reflexed flying-wing section."),
    Entry("s5010", "Selig S5010", _W, "Flying-wing section for sport and slope wings."),
    Entry("s5020", "Selig S5020", _W, "Thinner flying-wing section."),
    Entry("eh1090", "EH 1.0/9.0", _W, "Low-camber flying-wing section."),
    Entry("eh2012", "EH 2.0/12", _W, "Thicker flying-wing section for slower wings."),
    Entry("e182", "Eppler E182", _W, "Low-camber, low-moment section for flying wings and slope models."),
    Entry("e186", "Eppler E186", _W, "Slightly reflexed flying-wing section."),
    Entry("e325", "Eppler E325", _W, "Strongly reflexed section for tailless models."),
    Entry("e330", "Eppler E330", _W, "Reflexed section for tailless models."),
    Entry("clarkys", "Clark YS", _W, "Reflexed version of the Clark Y."),
    Entry("naca0009", "NACA 0009", _S, "Thin symmetric section for tails and fins (generated)."),
    Entry("naca0012", "NACA 0012", _S, "Symmetric section for tails and aerobatic wings (generated)."),
    Entry("naca0015", "NACA 0015", _S, "Thick symmetric section for aerobatic wings (generated)."),
    Entry("e168", "Eppler E168", _S, "Symmetric section for tails and aerobatic models."),
    Entry("e169", "Eppler E169", _S, "Thicker symmetric section."),
    Entry("e472", "Eppler E472", _S, "Symmetric section, thickest well forward."),
    Entry("sd8020", "SD8020", _S, "Symmetric section for tails and aerobatic gliders."),
    Entry("ht12", "HT12", _S, "Thin tail section (5 %)."),
    Entry("ht13", "HT13", _S, "Tail section (6.5 %)."),
    Entry("ht14", "HT14", _S, "Tail section (7.5 %)."),
    Entry("naca64a010", "NACA 64A010", _S, "Laminar-flow symmetric section for fins and fast models."),
    Entry("s1223", "Selig S1223", _H, "Very high lift, for heavy-lift and slow models."),
    Entry("s1210", "Selig S1210", _H, "High-lift section."),
    Entry("e423", "Eppler E423", _H, "Deeply cambered high-lift section."),
    Entry("fx63137", "Wortmann FX 63-137", _H, "High-lift section for slow and human-powered aircraft."),
    Entry("e61", "Eppler E61", _H, "Thin, deeply cambered section for indoor and slow flyers."),
    Entry("dae11", "DAE11", _H, "Human-powered aircraft section (MIT Daedalus)."),
    Entry("dae21", "DAE21", _H, "Human-powered aircraft section, tip (MIT Daedalus)."),
    Entry("mh114", "MH 114", _H, "High-lift section for human-powered aircraft and sailplanes."),
    Entry("naca23012", "NACA 23012", _F, "Classic five-digit section of many full-size aircraft."),
    Entry("naca23015", "NACA 23015", _F, "Thicker five-digit section for scale models."),
    Entry("ls417", "NASA LS(1)-0417", _F, "GA(W)-1 general-aviation section."),
)
_BY_ID = {e.id: e for e in ENTRIES}
_NACA4 = re.compile(r"naca\d{4}")


def key(spec: str) -> str:
    """Normalise a user-typed id: ``"SD 7037"``, ``"sd-7037"`` -> ``"sd7037"``."""
    return re.sub(r"[\s_-]+", "", spec.strip().lower())


def entry(spec: str) -> Entry | None:
    return _BY_ID.get(key(spec))


@functools.cache
def get(airfoil_id: str) -> Airfoil:
    """The airfoil with this id (raises ``AirfoilError`` for an unknown id)."""
    item = entry(airfoil_id)
    if item is None:
        raise AirfoilError(f"no airfoil {airfoil_id!r} in the database (run `openhowi airfoils` for the list)")
    if _NACA4.fullmatch(item.id):
        foil = naca4(item.id[4:])
    else:
        text = (resources.files("openhowi") / "airfoils" / f"{item.id}.dat").read_text(encoding="utf-8")
        foil = parse_dat(text, default_name=item.name)
    return Airfoil(item.name, foil.points)


class _Surface:
    """y(x) along one surface of a normalised airfoil."""

    def __init__(self, points: list[Point]):
        pts = sorted(points)
        self.xs = [p[0] for p in pts]
        self.ys = [p[1] for p in pts]

    def __call__(self, x: float) -> float:
        xs, ys = self.xs, self.ys
        for i in range(len(xs) - 1):
            if xs[i] <= x <= xs[i + 1]:
                span = xs[i + 1] - xs[i]
                return ys[i] if span <= 0 else ys[i] + (ys[i + 1] - ys[i]) * (x - xs[i]) / span
        return ys[0] if x < xs[0] else ys[-1]


def properties(foil: Airfoil) -> dict[str, Any]:
    """Thickness, camber (both in % of chord, with their positions) and whether the camber line is reflexed."""
    f = foil.normalized()
    upper, lower = _Surface(f.upper()), _Surface(f.lower())
    te_y = (f.points[0][1] + f.points[-1][1]) / 2  # the chord line runs from the leading edge to here

    def mean(x: float) -> float:
        return (upper(x) + lower(x)) / 2 - te_y * x

    xs = [i / 200 for i in range(1, 200)]
    thickness = max((upper(x) - lower(x), x) for x in xs)
    camber = max((mean(x), x) for x in xs)
    c80, c95 = mean(0.80), mean(0.95)
    return {
        "thickness": round(thickness[0] * 100, 1),
        "thickness_at": round(thickness[1] * 100),
        "camber": round(max(camber[0], 0.0) * 100, 1),
        "camber_at": round(camber[1] * 100) if camber[0] > 0.0005 else 0,
        "reflex": c95 - c80 > 0.0005,
    }


def outline(foil: Airfoil, n_per_surface: int = 30) -> list[list[float]]:
    """A light outline (unit chord) for thumbnails."""
    f = foil.normalized()
    fractions = cosine_spacing(n_per_surface)
    points = resample(f.upper(), fractions) + resample(f.lower(), fractions)[1:]
    return [[round(x, 4), round(y, 4)] for x, y in points]


@functools.cache
def _catalog() -> tuple[dict[str, Any], ...]:
    rows = []
    for item in ENTRIES:
        foil = get(item.id)
        rows.append(
            {
                "id": item.id,
                "name": item.name,
                "category": item.category,
                "note": item.note,
                "generated": bool(_NACA4.fullmatch(item.id)),
                **properties(foil),
                "outline": outline(foil),
            }
        )
    return tuple(rows)


def catalog(with_outlines: bool = True) -> list[dict[str, Any]]:
    """Every entry with its measured properties, in catalogue order."""
    if with_outlines:
        return [dict(row) for row in _catalog()]
    return [{k: v for k, v in row.items() if k != "outline"} for row in _catalog()]


def search(query: str = "", category: str | None = None) -> list[dict[str, Any]]:
    """Entries whose id, name or note contain every word of ``query``."""
    words = query.lower().split()
    out = []
    for row in catalog(with_outlines=False):
        if category and row["category"] != category:
            continue
        haystack = f"{row['id']} {row['name']} {row['note']} {row['category']}".lower()
        if all(w in haystack or key(w) in row["id"] for w in words):
            out.append(row)
    return out
