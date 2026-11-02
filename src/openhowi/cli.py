# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line interface: ``openhowi wing``, ``openhowi send`` and ``openhowi naca``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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
    output.write_text(result.gcode, encoding="utf-8")
    stats = result.data["stats"]
    mins, secs = divmod(round(stats["time_s"]), 60)
    print(
        f"Estimated time {mins} min {secs:02d} s, kerf root {stats['kerf']['root']:.2f} / tip {stats['kerf']['tip']:.2f} mm"
    )
    for warning in result.data["warnings"]:
        print(f"warning: {warning}")
    print(f"Wrote {output}")
    return 0


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
