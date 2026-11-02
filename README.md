# open_HOWI

**Open-source CAM and machine control for 4-axis CNC hot-wire foam cutters.**

## Why

Many hobbyists and small shops use **DevFoam** or **Profili** to make G-code for their
hot-wire foam cutters and to drive the machine. Both programs cost money. Many of these
setups also depend on an old PC with a **parallel (LPT) port** that sends step/direction
signals straight to the stepper drivers. Modern computers don't have that port, and the
old machines that do are getting harder to keep running.

open_HOWI aims to be a free alternative that:

- runs on any current computer (Linux, Windows, macOS, even a Raspberry Pi) with Python 3.11+;
- talks to the machine over **USB** through a cheap GRBL-style controller board, so the
  timing-critical step pulses come from the microcontroller, not from the PC;
- writes plain, readable G-code that other controllers (LinuxCNC, etc.) can run too.

## What works today (v0.1)

| | |
|---|---|
| Airfoils | Selig and Lednicer `.dat` files (e.g. from the UIUC database); built-in NACA 4-digit generator |
| Wing cores | Different root/tip airfoils, taper, sweep, washout (twist), tip height offset |
| 4-axis geometry | Root and tip sections are projected onto the two carriages, so the panel can sit anywhere along the wire |
| Kerf | Separate root and tip kerf compensation |
| Speed sync | Per-segment feed so both carriages finish each move together, with the farther-travelling side at your cut speed (G93 inverse time, or compensated G94) |
| Machine limits | The job is checked against your travel range before any G-code is written |
| Preview | SVG of both carriage paths |
| Machine control | Streams G-code to GRBL over USB serial (character-counting protocol), clears alarms with `$X`, and on Ctrl-C does a feed hold, then a reset (wire off) |

## How a 4-axis hot-wire cut works

```
 top view (looking down on the machine)

   XY carriage                                              UV carriage
   span = 0                                                 span = tower_distance
      │                                                          │
      ●━━━━━━━━━━━━━━━━━━━━━━━━━━ hot wire ━━━━━━━━━━━━━━━━━━━━━━━●
      │          ┌──────────────── foam ────────────────┐        │
      │          │ root section               tip section│        │
      │          └───────────────────────────────────────┘        │
      │<- root_offset ->|<------------ span ------------>|        │
```

The wire is a straight line between the two carriages. To cut a tapered panel, every pair of
matching root and tip points must lie on the wire at the same moment. open_HOWI:

1. resamples the root and tip airfoils so they have the same number of points, with the
   leading and trailing edges matched (cosine spacing, dense where curvature is high);
2. scales, sweeps and twists each section and moves it outward by half its kerf;
3. extends the line through each root/tip pair out to both carriages. These are the
   paths the XY and UV axes must follow;
4. sets the feed of each segment so both carriages arrive together. The farther-moving
   side moves at `cut_feed`; the other side moves more slowly.

In machine coordinates the trailing edge faces the origin. The wire rises at X=0 (behind the
foam), cuts in to the trailing edge, goes over the upper surface to the leading edge, back
under the lower surface, out the entry slot, and returns to the origin.

## Hardware

Any controller that accepts 4-axis G-code over USB works. A common, inexpensive setup is
an **Arduino Mega 2560 + RAMPS 1.4** (or similar) running a 4-axis GRBL 1.1 build such as
**Grbl-Mega-5X**:

- set `axis_letters` in your job file to the letters your firmware uses, e.g. `"XYUV"` or `"XYZA"`;
- if the wire's power supply is switched or PWM-controlled from the spindle output, set
  `wire_power` to the `S` value for M3 (relative to GRBL's `$30` max spindle speed);
  otherwise leave it out and switch the wire by hand;
- set steps/mm and max rates (`$100`… / `$110`…) for each axis as usual.

## Install

```bash
git clone https://github.com/ccarl0/open_HOWI.git
cd open_HOWI
pip install '.[serial]'      # pyserial is only needed for sending to a machine
```

## Quick start

```bash
# 1. Describe the job (see examples/tapered_wing.toml for every option)
openhowi wing examples/tapered_wing.toml -o wing.gcode --svg wing.svg

# 2. Check the summary (extents, cut length, estimated time) and open wing.svg

# 3. Put both carriages at the origin, then cut
openhowi send wing.gcode --port /dev/ttyACM0      # COM3 etc. on Windows
```

Other commands:

```bash
openhowi naca 2412 -o naca2412.dat   # write a NACA 4-digit airfoil
openhowi send wing.gcode --dry-run   # check a file without a machine
openhowi send wing.gcode --port /dev/ttyACM0 --unlock   # send $X first
```

## Job file reference

```toml
title = "My wing"

[machine]
tower_distance = 1000.0     # mm between the XY and UV carriages
x_travel = 500.0            # usable travel, mm
y_travel = 250.0
axis_letters = "XYUV"
cut_feed = 180.0            # mm/min for the farther-travelling carriage
travel_feed = 600.0         # mm/min through air
feed_mode = "inverse-time"  # or "units-per-minute"
wire_power = 600            # optional: S value for M3
preheat = 3.0               # seconds to dwell after M3

[wing]
root_airfoil = "naca2412"   # or "airfoils/clarky.dat" (relative to this file)
tip_airfoil = "naca2410"    # default: same as root
root_chord = 250.0
tip_chord = 160.0           # default: same as root
span = 600.0
sweep = 40.0                # tip LE this far aft of root LE
washout = 1.5               # degrees, tip nose-down
washout_pivot = 0.25        # twist axis as a fraction of chord
tip_offset_y = 0.0          # tip LE height above root LE
root_kerf = 1.0             # slot width the wire melts, mm
tip_kerf = 1.3
points_per_surface = 80

[placement]
root_side = "xy"            # "uv" cuts the opposite-hand panel
root_offset = 200.0         # root face distance from that carriage
x_margin = 25.0             # machine X of the trailing edge
y_offset = 40.0             # machine Y of the root chord line
```

**Measuring kerf:** cut a straight slot through a scrap block at your `cut_feed` and wire
heat, then measure its width. On a tapered panel the narrow end moves more slowly and melts
a wider slot, so it usually needs a larger kerf value.

**Placing the foam:** the rear face of the block must lie between X=0 and `x_margin`, so the
wire enters from outside the foam. The bottom of the core is at about `y_offset` minus the
lower-surface depth; keep the block above Y=0.

## Safety

- Hot-wire cutting expanded polystyrene and similar foams gives off fumes. Cut in a well
  ventilated area.
- The wire is hot enough to burn. Keep the power supply's current limited, and never leave
  a hot wire still inside the foam or unattended.
- Run new G-code once with the wire cold (or with `--dry-run` and the SVG preview) before
  cutting.

## Roadmap

- Graphical interface: jogging, homing, live position, preview while cutting
- DXF/SVG import for 2D shapes (fuselage formers, letters, signs)
- Spar slots, LE/TE cut-outs and sheeting allowance for wing cores
- Speed-dependent kerf model
- Rotary-table (5th axis) support

## Development

```bash
pip install -e '.[dev,serial]'
pytest
ruff check src tests && ruff format --check src tests
```

The core (`airfoil`, `geometry`, `wing`, `toolpath`, `gcode`) is pure Python with no
dependencies. `sender` needs pyserial only when talking to real hardware; its tests use a
simulated GRBL.

## License

open_HOWI is free software: you can redistribute it and/or modify it under the terms of the
[GNU General Public License](LICENSE) as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version.
