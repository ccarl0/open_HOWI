# SPDX-License-Identifier: GPL-3.0-or-later
"""Wing planforms, and splitting a curved one into straight panels the wire can cut.

A hot wire cuts ruled surfaces, so each panel has straight leading and trailing
edges. An elliptical or otherwise curved half wing is cut as several panels whose
ends sit on the true outline, so neighbouring panels meet face to face; the
straight edges in between stay within a tolerance of the curve.

Coordinates: ``s`` runs along the span from the wing root; the leading edge
``le(s)`` is measured aft of the root's leading edge; ``height(s)`` is the rise of
the leading edge above the root's; ``twist(s)`` is nose-down, relative to the root.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .fuselage import _crossings, _linear, _pchip
from .geometry import Point

PLANFORMS = ("panel", "elliptical", "stations", "drawing")
MIN_PANEL = 20.0
"""Shortest panel the splitter makes unless told otherwise (mm)."""

Curve = Callable[[float], float]


class PlanformError(ValueError):
    """The planform cannot be built or split as asked."""


@dataclass(frozen=True)
class PanelCut:
    """One straight panel of a half wing."""

    number: int
    s0: float
    """Root end, mm from the wing root."""
    s1: float
    root_chord: float
    tip_chord: float
    sweep: float
    """Tip leading edge aft of the panel's root leading edge (mm)."""
    washout: float
    """Twist of the panel's tip relative to its root (degrees, nose-down)."""
    rise: float
    """Height of the tip leading edge above the root's (mm)."""
    error: float
    """How far the straight edges stray from the true outline (mm)."""

    @property
    def span(self) -> float:
        return self.s1 - self.s0


class Planform:
    def __init__(
        self,
        kind: str,
        span: float,
        le: Curve,
        chord: Curve,
        twist: Curve | None = None,
        height: Curve | None = None,
        knots: Sequence[float] = (),
    ) -> None:
        if span <= 0:
            raise PlanformError("the span must be > 0")
        self.kind = kind
        self.span = span
        self.le = le
        self.chord = chord
        self.twist: Curve = twist or (lambda s: 0.0)
        self.height: Curve = height or (lambda s: 0.0)
        self.knots = tuple(k for k in knots if 0 < k < span)
        """Span positions where the outline has a corner: always a panel joint."""
        for k in range(41):
            if self.chord(span * k / 40) <= 0:
                raise PlanformError("the chord must stay above 0 along the span")

    # Building --------------------------------------------------------------------------------------

    @classmethod
    def trapezoid(cls, root_chord: float, tip_chord: float, span: float, sweep: float = 0.0,
                  washout: float = 0.0, rise: float = 0.0) -> Planform:  # fmt: skip
        """A straight tapered panel: one panel unless it is split to fit the machine."""
        return cls(
            "panel", span,
            lambda s: sweep * s / span,
            lambda s: root_chord + (tip_chord - root_chord) * s / span,
            lambda s: washout * s / span,
            lambda s: rise * s / span,
        )  # fmt: skip

    @classmethod
    def elliptical(cls, root_chord: float, tip_chord: float, span: float, line: float = 25.0,
                   sweep: float = 0.0, washout: float = 0.0, rise: float = 0.0) -> Planform:  # fmt: skip
        """An elliptical half wing cut off at ``tip_chord``; the line at ``line`` % of the chord is straight
        and sits ``sweep`` mm further back at the tip than at the root."""
        if not 0 < tip_chord < root_chord:
            raise PlanformError("an elliptical wing needs a tip chord above 0 and below the root chord")
        if not 0 <= line <= 100:
            raise PlanformError("the straight line must be between 0 and 100 % of the chord")
        k = line / 100.0
        full = span / math.sqrt(1.0 - (tip_chord / root_chord) ** 2)

        def chord(s: float) -> float:
            return root_chord * math.sqrt(max(0.0, 1.0 - (s / full) ** 2))

        return cls(
            "elliptical", span,
            lambda s: k * root_chord + sweep * s / span - k * chord(s),
            chord,
            lambda s: washout * s / span,
            lambda s: rise * s / span,
        )  # fmt: skip

    @classmethod
    def from_stations(cls, rows: Sequence[tuple[float, float, float, float, float]], smooth: bool = True) -> Planform:
        """From stations (span position, chord, leading edge aft of the root's, twist, height), root first.

        ``smooth`` runs smooth curves through them; otherwise straight lines, with a joint at each station.
        """
        rows = sorted(rows)
        if len(rows) < 2:
            raise PlanformError("a planform needs at least two stations")
        s0, _, le0, tw0, h0 = rows[0]
        xs = [r[0] - s0 for r in rows]
        if any(b - a < 1.0 for a, b in itertools.pairwise(xs)):
            raise PlanformError("stations must be at least 1 mm apart along the span")
        if any(r[1] <= 0 for r in rows):
            raise PlanformError("every station needs a chord above 0")
        fit = _pchip if smooth else _linear
        return cls(
            "stations", xs[-1],
            fit(xs, [r[2] - le0 for r in rows]),
            fit(xs, [r[1] for r in rows]),
            fit(xs, [r[3] - tw0 for r in rows]),
            fit(xs, [r[4] - h0 for r in rows]),
            knots=() if smooth else xs[1:-1],
        )  # fmt: skip

    @classmethod
    def from_outline(cls, ring: Sequence[Point], min_chord: float, washout: float = 0.0, rise: float = 0.0,
                     samples: int = 400) -> tuple[Planform, list[str]]:  # fmt: skip
        """From a drawing of the half wing seen from above: root on the left, leading edge at the top.

        The wing is cut out to where its chord drops below ``min_chord``; a rounded tip beyond that is
        carved by hand.
        """
        warnings: list[str] = []
        x0, x1 = min(p[0] for p in ring), max(p[0] for p in ring)
        if x1 - x0 < 20:
            raise PlanformError("the planform drawing must be at least 20 mm long")
        ss, les, chords = [], [], []
        for k in range(samples + 1):
            f = min(max(k / samples, 1e-6), 1 - 1e-6)
            ys = _crossings(ring, x0 + f * (x1 - x0))
            if len(ys) < 2:
                continue
            chord = max(ys) - min(ys)
            if chord < min_chord:
                if ss:
                    break
                continue
            ss.append(f * (x1 - x0))
            les.append(-max(ys))
            chords.append(chord)
        if len(ss) < 3:
            raise PlanformError("could not read the planform: draw one closed outline, root on the left")
        if ss[0] > 1.0:
            raise PlanformError("the root of the planform must be its left edge, at least the tip chord deep")
        if ss[-1] < (x1 - x0) * (1 - 1e-6) - 1.0:
            warnings.append(
                f"The tip narrower than {min_chord:g} mm (the last {x1 - x0 - ss[-1]:.0f} mm) is not cut: "
                "carve it from a block by hand."
            )
        span = ss[-1]
        le0 = les[0]
        return cls(
            "drawing", span,
            _linear(ss, [v - le0 for v in les]),
            _linear(ss, chords),
            lambda s: washout * s / span,
            lambda s: rise * s / span,
        ), warnings  # fmt: skip

    # Splitting -------------------------------------------------------------------------------------

    def te(self, s: float) -> float:
        return self.le(s) + self.chord(s)

    def deviation(self, s0: float, s1: float, samples: int = 24) -> float:
        """Furthest the straight panel from s0 to s1 strays from the outline (and the height line)."""
        a = (self.le(s0), self.te(s0), self.height(s0))
        b = (self.le(s1), self.te(s1), self.height(s1))
        worst = 0.0
        for k in range(1, samples):
            t = k / samples
            s = s0 + (s1 - s0) * t
            for actual, lo, hi in zip((self.le(s), self.te(s), self.height(s)), a, b):
                worst = max(worst, abs(actual - (lo + (hi - lo) * t)))
        return worst

    def split(
        self,
        forced: Sequence[float] = (),
        count: int = 0,
        tolerance: float = 1.0,
        max_length: float = math.inf,
        min_length: float = MIN_PANEL,
    ) -> list[float]:
        """Panel joints from root to tip (0 and the span included).

        Joints are kept at the outline's corners and at ``forced`` positions. Between those, with
        ``count`` set, the panels that stray furthest from the outline are split until there are
        ``count`` of them; otherwise there are as few as keep every panel within ``tolerance``. No
        panel is longer than ``max_length``. Finally the joints slide along the span to even out the
        error.
        """
        span = self.span
        if min_length < 5:
            raise PlanformError("the shortest panel must be at least 5 mm long")
        if max_length < min_length:
            raise PlanformError(f"the longest panel must be at least as long as the shortest ({min_length:g} mm)")
        fixed = {0.0, span}
        for s in list(self.knots) + list(forced):
            if 1.0 < s < span - 1.0:
                fixed.add(float(s))
        joints = sorted(fixed)
        for a, b in itertools.pairwise(joints):
            if b - a < 1.0:
                raise PlanformError("two panel joints are less than 1 mm apart")

        def greedy(a: float, b: float, limit: float) -> list[float]:
            """Joints after a, as far apart as ``limit`` allows (the last one is b)."""
            out, s = [], a
            while b - s > 1e-6:
                if b - s <= max_length and self.deviation(s, b) <= limit:
                    out.append(b)
                    break
                lo, hi = s + min_length, min(b - min_length, s + max_length)
                if lo > hi + 1e-6:  # too short for two panels: the rest is one (halved only if too long)
                    s = (s + b) / 2 if b - s > max_length else b
                    out.append(s)
                    continue
                if self.deviation(s, lo) <= limit:
                    for _ in range(22):
                        mid = (lo + hi) / 2
                        if self.deviation(s, mid) <= limit:
                            lo = mid
                        else:
                            hi = mid
                s = lo
                out.append(s)
            return out

        segments = list(zip(sorted(fixed), sorted(fixed)[1:]))

        def total(limit: float) -> int:
            return sum(len(greedy(a, b, limit)) for a, b in segments)

        hi = tolerance
        if count:  # the smallest error level that needs no more than ``count`` panels
            lo, hi = 0.0, max(self.deviation(a, b) for a, b in segments) + 1.0
            if total(hi) <= count:
                for _ in range(18):
                    mid = (lo + hi) / 2
                    if total(mid) <= count:
                        hi = mid
                    else:
                        lo = mid
        joints = [0.0]
        for a, b in segments:
            more = greedy(a, b, hi)
            if max(self.deviation(x, y) for x, y in zip([a] + more, more)) < 0.01:
                more = [a + (b - a) * k / len(more) for k in range(1, len(more) + 1)]  # straight: equal panels
            joints += more
        if not count:
            return joints

        def bad(a: float, b: float) -> float:
            return self.deviation(a, b) + 1e-6 * (b - a)  # equal lengths where the outline is straight

        while len(joints) - 1 < count:
            panels = [(bad(a, b), k) for k, (a, b) in enumerate(itertools.pairwise(joints)) if b - a >= 2 * min_length]
            if not panels:
                break
            k = max(panels)[1]
            joints.insert(k + 1, (joints[k] + joints[k + 1]) / 2)
        for _ in range(4):  # even out neighbours: never makes the worst panel worse
            for i in range(1, len(joints) - 1):
                if joints[i] in fixed:
                    continue
                a, c = joints[i - 1], joints[i + 1]
                lo, hi = max(a + min_length, c - max_length), min(c - min_length, a + max_length)
                if lo >= hi:
                    continue
                for _ in range(24):
                    mid = (lo + hi) / 2
                    if bad(a, mid) > bad(mid, c):
                        hi = mid
                    else:
                        lo = mid
                joints[i] = (lo + hi) / 2
        return joints

    def panels(self, joints: Sequence[float]) -> list[PanelCut]:
        out = []
        for number, (a, b) in enumerate(itertools.pairwise(joints), start=1):
            out.append(
                PanelCut(number, a, b, self.chord(a), self.chord(b), self.le(b) - self.le(a),
                         self.twist(b) - self.twist(a), self.height(b) - self.height(a), self.deviation(a, b))
            )  # fmt: skip
        return out

    # Positions along straight lines --------------------------------------------------------------------

    def line_position(self, s: float, start: tuple[float, float], end: tuple[float, float]) -> float:
        """% of the chord at ``s`` of a straight line (seen from above) from ``start`` to ``end``,
        each given as (span position, % of the chord there)."""
        (sa, pa), (sb, pb) = start, end
        xa = self.le(sa) + pa / 100.0 * self.chord(sa)
        xb = self.le(sb) + pb / 100.0 * self.chord(sb)
        x = xa + (xb - xa) * (s - sa) / (sb - sa)
        return (x - self.le(s)) / self.chord(s) * 100.0

    def outline(self, n: int = 120, joints: Sequence[float] = ()) -> list[Point]:
        """The half wing seen from above as (span, -chordwise) points: leading edge, then trailing edge back."""
        ss = sorted({self.span * k / n for k in range(n + 1)} | set(joints))
        return [(s, -self.le(s)) for s in ss] + [(s, -self.te(s)) for s in reversed(ss)]
