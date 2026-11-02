"""SVG preview of the two carriage paths."""

from __future__ import annotations

from html import escape

from .machine import Machine
from .toolpath import Toolpath

XY_COLOR = "#2563eb"
UV_COLOR = "#ea580c"


def toolpath_svg(toolpath: Toolpath, machine: Machine, title: str = "") -> str:
    w, h = machine.x_travel, machine.y_travel
    pad = 0.05 * max(w, h)
    legend = 0.12 * max(w, h)
    font = 0.03 * max(w, h)

    def points(idx: int) -> str:
        pts = [toolpath.start[idx]] + [m.xy if idx == 0 else m.uv for m in toolpath.moves]
        return " ".join(f"{x:.3f},{h - y:.3f}" for x, y in pts)

    a = machine.axis_letters
    vb = f"{-pad:.3f} {-pad - legend:.3f} {w + 2 * pad:.3f} {h + 2 * pad + legend:.3f}"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" font-family="sans-serif" font-size="{font:.3f}">
  <rect x="{-pad:.3f}" y="{-pad - legend:.3f}" width="{w + 2 * pad:.3f}" height="{h + 2 * pad + legend:.3f}" fill="#ffffff"/>
  <rect x="0" y="0" width="{w:.3f}" height="{h:.3f}" fill="#f8fafc" stroke="#94a3b8" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>
  <text x="0" y="{-legend * 0.55:.3f}" fill="#0f172a">{escape(title or "open_HOWI preview")}</text>
  <text x="0" y="{-legend * 0.15:.3f}" fill="{XY_COLOR}">{a[0]}{a[1]} carriage</text>
  <text x="{w * 0.3:.3f}" y="{-legend * 0.15:.3f}" fill="{UV_COLOR}">{a[2]}{a[3]} carriage</text>
  <polyline points="{points(0)}" fill="none" stroke="{XY_COLOR}" stroke-width="1.5" vector-effect="non-scaling-stroke"/>
  <polyline points="{points(1)}" fill="none" stroke="{UV_COLOR}" stroke-width="1.5" vector-effect="non-scaling-stroke"/>
</svg>
"""
