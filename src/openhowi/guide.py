# SPDX-License-Identifier: GPL-3.0-or-later
"""A printable cutting guide for a fuselage: every cut with its drawings and steps, then the templates.

It is one self-contained HTML page (no scripts), laid out for A4 paper: open
it in a browser and print it. Templates are drawn in millimetres, so printed
at 100 % they come out full size.
"""

from __future__ import annotations

import copy
from html import escape
from typing import Any

from . import __version__
from .project import SliceError, build_fuselage, slice_project
from .template import former_svgs, fuselage_plans_svg, jig_plate_svg, scale_bar_svg

CSS = """
@page { size: A4; margin: 10mm; }
* { box-sizing: border-box; }
body { font: 11pt/1.4 Helvetica, Arial, sans-serif; color: #222; margin: 0 auto; max-width: 190mm; padding: 8mm 0; }
h1 { font-size: 18pt; margin: 0 0 2mm; }
h2 { font-size: 13pt; margin: 6mm 0 2mm; border-bottom: 1px solid #ccc; padding-bottom: 1mm; }
h3 { font-size: 11pt; margin: 3mm 0 1mm; }
.lead { color: #555; margin: 0 0 4mm; }
table.summary { border-collapse: collapse; margin: 2mm 0 4mm; }
table.summary td { padding: 1mm 4mm 1mm 0; vertical-align: top; }
table.summary td:first-child { color: #666; }
ol.notes { padding-left: 6mm; margin: 2mm 0; }
ol.notes li { margin-bottom: 1.5mm; }
.steps { display: grid; grid-template-columns: 1fr 1fr; gap: 4mm; margin: 3mm 0; }
.step { break-inside: avoid; border: 1px solid #ddd; border-radius: 2mm; padding: 2mm; }
.step svg { width: 100%; height: auto; display: block; }
.step ul { margin: 1mm 0 0; padding-left: 5mm; font-size: 10pt; }
.step.wide { grid-column: 1 / -1; }
.warn { background: #fff4e6; color: #8a4b08; border-radius: 2mm; padding: 2mm 3mm; margin: 2mm 0; font-size: 10pt; }
.template { break-before: page; }
.template svg { display: block; max-width: none; }
.fit svg { width: 100%; height: auto; }
.formers { display: flex; flex-wrap: wrap; gap: 4mm; align-items: flex-start; }
.formers > div { break-inside: avoid; }
.check { margin: 1mm 0 3mm; color: #555; font-size: 10pt; }
@media screen { body { padding: 10mm; background: #f4f4f4; } .sheet { background: #fff; padding: 8mm; margin-bottom: 6mm;
  box-shadow: 0 1px 4px rgb(0 0 0 / 15%); } }
"""


def _steps_html(steps: list[dict[str, Any]]) -> str:
    out = []
    for step in steps:
        notes = step.get("notes") or []
        wide = " wide" if not step.get("svg") else ""
        title = f"<h3>{escape(str(step['title']))}</h3>" if step.get("title") else ""
        items = "".join(f"<li>{escape(str(n))}</li>" for n in notes)
        out.append(
            f'<div class="step{wide}">{title}{step.get("svg") or ""}{f"<ul>{items}</ul>" if items else ""}</div>'
        )
    return f'<div class="steps">{"".join(out)}</div>' if out else ""


def cutting_guide_html(project: dict[str, Any]) -> tuple[str, str]:
    """File name and HTML of the printable guide for a fuselage project."""
    part = project.get("part") or {}
    if not isinstance(part, dict) or part.get("type") != "fuselage":
        raise SliceError("the cutting guide is for fuselages")
    title = str(project.get("title") or "Fuselage")
    fuse, _ = build_fuselage(part)
    first = slice_project(project).data
    jobs = first.get("jobs") or [{"label": "Cut"}]
    stats = first["stats"]["fuselage"]
    method = part.get("method", "two_view")

    sections = []
    total = 0.0
    jig = None
    for number in range(1, len(jobs) + 1):
        each = copy.deepcopy(project)
        each["part"]["job"] = number
        data = first if number == first.get("job", 1) else slice_project(each).data
        total += data["stats"]["time_s"]
        jig = jig or data.get("jig")
        block = data["block"]
        steps = data.get("steps") or []
        narrated = any(step.get("notes") for step in steps)
        notes = "" if narrated else "".join(f"<li>{escape(n)}</li>" for n in data.get("notes") or [])
        warnings = "".join(
            f'<div class="warn">{escape(w)}</div>' for w in data.get("warnings") or [] if "Wire power is blank" not in w
        )
        size = f"{block['x1'] - block['x0']:.0f} × {block['y1'] - block['y0']:.0f} × {block['z1'] - block['z0']:.0f} mm"
        minutes, seconds = divmod(round(data["stats"]["time_s"]), 60)
        sections.append(
            f'<section class="sheet"><h2>Cut {number} of {len(jobs)}: {escape(jobs[number - 1]["label"])}</h2>'
            f'<p class="lead">About {minutes} min {seconds:02d} s · foam block {size} · G-code: cut {number} '
            "(Export all cuts)</p>"
            f"{warnings}{f'<ol class=notes>{notes}</ol>' if notes else ''}{_steps_html(steps)}</section>"
        )

    skin = float(part.get("skin") or 0.0)
    spacing = float(part.get("segment_length") or 150.0)
    views_svg = fuselage_plans_svg(fuse, title, skin=skin, spacing=spacing, show_formers=False)
    formers = former_svgs(fuse, skin=skin, spacing=spacing)
    templates = []
    if jig:
        too_big = 2 * jig["apothem"] > 184
        templates.append(
            '<section class="template sheet"><h2>Jig plates (1:1)</h2><p class="check">Print at 100 % (actual size). '
            "Check the plate's size across the flats with a ruler before you cut it.</p>"
            + (
                '<div class="warn">This plate is larger than an A4 page: print the plans SVG (Preview → Plans &amp; '
                "formers), which includes it, with your printer's tiling or poster option.</div>"
                if too_big
                else ""
            )
            + jig_plate_svg(int(jig["sides"]), jig["apothem"], jig["half"], jig["thickness"])
            + "</section>"
        )
    if formers:
        skin_note = f" Each is drawn {skin:g} mm inside the skin (dashed)." if skin > 0 else ""
        templates.append(
            '<section class="template sheet"><h2>Formers (1:1)</h2><p class="check">Print at 100 % (actual size) and '
            f"check the 50 mm bar.{skin_note}</p>{scale_bar_svg()}"
            f'<div class="formers">{"".join(f"<div>{svg}</div>" for svg in formers)}</div></section>'
        )
    templates.append(
        '<section class="template sheet"><h2>Side and top views (reduced)</h2><p class="check">Scaled to fit the '
        "page. For full-size views, print the SVG from Preview → Plans &amp; formers with your printer's tiling or "
        'poster option.</p><div class="fit">' + views_svg + "</div></section>"
    )

    how = {
        "two_view": "two views: the side view, then (turned onto its side) the top view; round the corners by hand",
        "turned": f"turned: {part.get('facets', 8)} passes round the fuselage, turning the block between them",
        "segments": "lengthwise segments with rounded sections, glued together",
    }.get(method, method)
    minutes, seconds = divmod(round(total), 60)
    rows = [
        ("Fuselage", f"{stats['length']:.0f} mm long, {stats['width']:.0f} mm wide, {stats['height']:.0f} mm high"),
        ("Volume", f"{stats['volume_l']:.2f} l of foam in the finished fuselage"),
        ("Cut as", how),
        ("Cuts", f"{len(jobs)} ({minutes} min {seconds:02d} s of cutting in all)"),
        ("Machine", str(project.get("machine_preset") or "custom")),
        ("Foam", str(project.get("material_preset") or "custom")),
    ]
    if fuse.saddles:
        rows.append(("Wing saddle", f"{len(fuse.saddles)}: drawn in the plans"))
    summary = "".join(f"<tr><td>{escape(k)}</td><td>{escape(v)}</td></tr>" for k, v in rows)
    html = (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{escape(title)}: cutting guide</title>'
        f"<style>{CSS}</style></head><body>"
        f'<section class="sheet"><h1>{escape(title)}: cutting guide</h1>'
        '<p class="lead">Print this page with your browser (Ctrl+P / ⌘P) at 100 % scale. The cuts are in order; '
        "each says where the foam goes and, between cuts, how to turn it.</p>"
        f'<table class="summary">{summary}</table>'
        "<p>Before cutting for real, run every file once on the simulator, or on the machine with the wire cold. "
        "Cut in a ventilated room and never leave the hot wire unattended.</p></section>"
        + "".join(sections)
        + "".join(templates)
        + f'<p class="check">open_HOWI {__version__}</p></body></html>'
    )
    return f"{title}-cutting-guide.html", html
