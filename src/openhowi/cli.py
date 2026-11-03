# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line interface: ``openhowi wing``, ``openhowi send`` and ``openhowi naca``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .airfoil import AirfoilError, naca4
from .config import ConfigError, load_wing_job
from .gcode import generate_gcode, job_minutes, segments
from .preview import toolpath_svg
from .sender import DEFAULT_RX_BUFFER, GrblAlarm, GrblError, GrblStreamer, SerialTransport, clean_line
from .toolpath import PlacementError, build_wing_toolpath


def _err(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def cmd_naca(args: argparse.Namespace) -> int:
    try:
        airfoil = naca4(args.code, n=args.points)
    except AirfoilError as exc:
        return _err(str(exc))
    if args.output:
        Path(args.output).write_text(airfoil.to_dat(), encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        sys.stdout.write(airfoil.to_dat())
    return 0


def cmd_wing(args: argparse.Namespace) -> int:
    try:
        job = load_wing_job(args.config)
        toolpath = build_wing_toolpath(job.wing, job.machine, job.placement)
    except (ConfigError, PlacementError, AirfoilError, ValueError) as exc:
        return _err(str(exc))

    wing, machine = job.wing, job.machine
    assert wing.tip_airfoil is not None
    comments = [
        job.title,
        (
            f"root {wing.root_airfoil.name} chord {wing.root_chord:g} mm, "
            f"tip {wing.tip_airfoil.name} chord {wing.tip_chord:g} mm, span {wing.span:g} mm"
        ),
        f"sweep {wing.sweep:g} mm, washout {wing.washout:g} deg, kerf root {wing.root_kerf:g} / tip {wing.tip_kerf:g} mm",
    ]
    spec = wing.core_spec()
    built_up = spec.sheeted or spec.le_stock > 0 or spec.te_stock > 0 or spec.slot_top or spec.slot_bottom
    if built_up:
        comments.append(
            f"core: sheeting top {spec.sheeting_top:g} / bottom {spec.sheeting_bottom:g} mm, "
            f"LE stock {spec.le_stock:g} mm, TE stock {spec.te_stock:g} mm, spar slots {spec.spar_width:g} mm wide, "
            f"top {spec.spar_depth_top:g} / bottom {spec.spar_depth_bottom:g} mm deep at {spec.spar_position:g} %"
        )
    gcode = generate_gcode(toolpath, machine, comments)
    output = Path(args.output or Path(args.config).with_suffix(".gcode").name)
    output.write_text(gcode, encoding="utf-8")

    a = machine.axis_letters
    cuts = [seg for seg in segments(toolpath, machine) if seg.cutting]
    cut_xy = sum(seg.d_xy for seg in cuts)
    cut_uv = sum(seg.d_uv for seg in cuts)
    mins, secs = divmod(round(job_minutes(toolpath, machine) * 60), 60)
    print(job.title)
    print(f"  cut length   {a[0]}{a[1]} {cut_xy:.0f} mm, {a[2]}{a[3]} {cut_uv:.0f} mm")
    for name, (xmin, ymin, xmax, ymax) in toolpath.extents().items():
        print(f"  {name} extents   X {xmin:.1f}..{xmax:.1f} mm, Y {ymin:.1f}..{ymax:.1f} mm")
    print(f"  est. time    {mins} min {secs:02d} s at {machine.cut_feed:g} mm/min")
    if built_up:
        cut = wing.cores()[2]
        print(f"  core ends    {cut['root_te']:.1f} mm (root) / {cut['tip_te']:.1f} mm (tip) before the trailing edge")
    print(f"Wrote {output}")

    if args.svg:
        Path(args.svg).write_text(toolpath_svg(toolpath, machine, job.title), encoding="utf-8")
        print(f"Wrote {args.svg}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    try:
        lines = Path(args.file).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return _err(str(exc))
    program = [c for c in (clean_line(line) for line in lines) if c]
    if args.dry_run:
        print(f"{len(program)} lines, {sum(len(c) + 1 for c in program)} bytes would be sent")
        return 0

    def show(msg: str) -> None:
        print(f"\n< {msg}", file=sys.stderr)

    def progress(done: int, total: int) -> None:
        print(f"\r{done}/{total} lines", end="", file=sys.stderr, flush=True)

    try:
        transport = SerialTransport(args.port, args.baud)
    except (RuntimeError, OSError, ValueError) as exc:  # pyserial missing, port busy or bad settings
        return _err(str(exc))

    with transport:
        streamer = GrblStreamer(transport, rx_buffer_size=args.rx_buffer, on_message=show)
        try:
            streamer.wake()
            if args.unlock:
                streamer.command("$X")
            streamer.stream(program, progress)
            print("\nAll lines sent, waiting for the machine to finish...", file=sys.stderr)
            streamer.wait_until_idle()
        except KeyboardInterrupt:
            streamer.abort()
            print("\nAborted: feed hold and reset sent (wire heat off).", file=sys.stderr)
            return 130
        except (GrblError, GrblAlarm) as exc:
            streamer.abort()
            return _err(f"{exc}; machine stopped")
    print("Done.", file=sys.stderr)
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .server import serve

    try:
        serve(args.host, args.port, open_browser=not args.no_browser, allow_hosts=tuple(args.allow_host))
    except OSError as exc:
        return _err(f"cannot listen on {args.host}:{args.port}: {exc}")
    return 0


def cmd_slice(args: argparse.Namespace) -> int:
    import json

    from .project import SliceError, slice_project

    try:
        project = json.loads(Path(args.project).read_text(encoding="utf-8"))
        result = slice_project(project)
    except (OSError, json.JSONDecodeError, SliceError) as exc:
        return _err(str(exc))
    output = Path(args.output or Path(args.project).name.removesuffix(".json").removesuffix(".howi") + ".gcode")
    return _report(result, output)


def _report(result: Any, output: Path) -> int:
    output.write_text(result.gcode, encoding="utf-8")
    stats = result.data["stats"]
    mins, secs = divmod(round(stats["time_s"]), 60)
    if "drawing" in stats:
        d = stats["drawing"]
        print(f"{d['shapes']} shape(s), {d['holes']} hole(s), {d['size'][0]:g} x {d['size'][1]:g} mm")
    print(
        f"Estimated time {mins} min {secs:02d} s, kerf root {stats['kerf']['root']:.2f} / tip {stats['kerf']['tip']:.2f} mm"
    )
    for warning in result.data["warnings"]:
        print(f"warning: {warning}")
    print(f"Wrote {output}")
    return 0


def cmd_shape(args: argparse.Namespace) -> int:
    import copy

    from .presets import PresetStore
    from .project import DEFAULT_PARTS, DEFAULT_PLACEMENTS, SliceError, default_project, slice_project
    from .shapes import format_for

    path = Path(args.drawing)
    try:
        fmt = format_for(path.name)
        data = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        return _err(str(exc))

    project = default_project()
    store = PresetStore()
    for kind, name in (("machine", args.machine), ("material", args.material)):
        if not name:
            continue
        presets = store.list(kind)
        match = next((preset for preset in presets if preset["name"] == name), None)
        if match is None:
            names = ", ".join(repr(preset["name"]) for preset in presets)
            return _err(f"no {kind} preset named {name!r}; choose one of {names}")
        project[kind] = copy.deepcopy(match["values"])
        project[f"{kind}_preset"] = name
    project["title"] = path.stem
    project["part"] = copy.deepcopy(DEFAULT_PARTS["shape"]) | {
        "source": {"name": path.name, "format": fmt, "data": data},
        "span": args.span,
        "scale": args.scale,
        "rotate": args.rotate,
        "mirror": args.mirror,
        "tip_scale": args.tip_scale,
    }
    placement = {"root_offset": args.root_offset, "x_margin": args.x_margin, "y_offset": args.y_offset}
    project["placement"] = copy.deepcopy(DEFAULT_PLACEMENTS["shape"]) | {
        k: v for k, v in placement.items() if v is not None
    }
    try:
        result = slice_project(project)
    except SliceError as exc:
        return _err(str(exc))
    return _report(result, Path(args.output or path.with_suffix(".gcode").name))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openhowi", description="Open-source CAM and machine control for 4-axis hot-wire foam cutters."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("gui", help="start the app (opens in your browser)")
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this computer only)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    p.add_argument(
        "--allow-host", action="append", default=[], help="extra host name the app may be reached by (repeatable)"
    )
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("slice", help="slice a project saved from the app (.howi.json) to G-code")
    p.add_argument("project")
    p.add_argument("-o", "--output", help="G-code file to write (default: <project name>.gcode)")
    p.set_defaults(func=cmd_slice)

    p = sub.add_parser("shape", help="cut the closed outlines of an SVG or DXF drawing")
    p.add_argument("drawing", help="an .svg or .dxf file")
    p.add_argument("-o", "--output", help="G-code file to write (default: <drawing name>.gcode)")
    p.add_argument("--span", type=float, default=100.0, help="length of the part along the wire, mm (default 100)")
    p.add_argument("--scale", type=float, default=100.0, help="scale the drawing, %% (default 100)")
    p.add_argument("--rotate", type=float, default=0.0, help="rotate the drawing, degrees")
    p.add_argument("--mirror", action="store_true", help="mirror the drawing left-right")
    p.add_argument("--tip-scale", type=float, default=100.0, help="size of the tip face, %% (taper)")
    p.add_argument("--root-offset", type=float, help="distance of the part from the XY carriage, mm")
    p.add_argument("--x-margin", type=float, help="machine X of the rear of the cut, mm")
    p.add_argument("--y-offset", type=float, help="machine Y of the bottom of the shape, mm")
    p.add_argument("--machine", help="machine preset name (default: the first built-in one)")
    p.add_argument("--material", help="material preset name (default: the first built-in one)")
    p.set_defaults(func=cmd_shape)

    p = sub.add_parser("wing", help="generate G-code for a wing panel described in a TOML file")
    p.add_argument("config", help="job file, see examples/tapered_wing.toml")
    p.add_argument("-o", "--output", help="G-code file to write (default: <config name>.gcode)")
    p.add_argument("--svg", help="also write an SVG preview of both carriage paths")
    p.set_defaults(func=cmd_wing)

    p = sub.add_parser("send", help="stream a G-code file to a GRBL controller over USB")
    p.add_argument("file")
    p.add_argument("-p", "--port", help="serial port, e.g. /dev/ttyACM0 or COM3")
    p.add_argument("-b", "--baud", type=int, default=115200)
    p.add_argument("--rx-buffer", type=int, default=DEFAULT_RX_BUFFER, help="controller receive buffer in bytes")
    p.add_argument("--unlock", action="store_true", help="send $X first to clear GRBL's alarm lock")
    p.add_argument("--dry-run", action="store_true", help="check the file without opening the port")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("naca", help="write a NACA 4-digit airfoil as a Selig .dat file")
    p.add_argument("code", help="e.g. 2412")
    p.add_argument("-n", "--points", type=int, default=100, help="points per surface")
    p.add_argument("-o", "--output", help="file to write (default: stdout)")
    p.set_defaults(func=cmd_naca)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "send" and not args.dry_run and not args.port:
        parser.error("send: --port is required unless --dry-run is given")
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
