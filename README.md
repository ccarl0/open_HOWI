# open_HOWI

**A free, open-source "slicer" for 4-axis CNC hot-wire foam cutters**: prepare a part, preview
the cut, export G-code and run the machine from one app, the way PrusaSlicer works for 3D printers.

![Prepare view: a tapered wing panel placed between the two carriages](docs/prepare.png)

## Why

Many hobbyists and small shops use **DevFoam** or **Profili** to make G-code for their
hot-wire foam cutters and to drive the machine. Both programs cost money. Many of these
setups also depend on an old PC with a **parallel (LPT) port** that sends step/direction
signals straight to the stepper drivers. Modern computers don't have that port, and the
old machines that do are getting harder to keep running.

open_HOWI is a free alternative that:

- runs on any current computer (Linux, Windows, macOS, or a Raspberry Pi next to the machine);
- talks to the machine over **USB** through a cheap GRBL-style controller board, so the
  timing-critical step pulses come from the microcontroller, not from the PC;
- writes plain, readable G-code that other controllers (LinuxCNC, etc.) can run too.

## The app

```bash
pip install .        # Python 3.11+
openhowi gui         # opens http://localhost:8765 in your browser
```

The app runs locally. The browser is only the window; slicing and the USB connection run in
the `openhowi` process. It works offline.

| Tab | What it does |
|---|---|
| **Prepare** | Choose a **machine** and a **material** preset, then set up the part: a **wing panel** (root/tip airfoils from the built-in database, NACA or any `.dat` file, chords, span, sweep, washout, sheeting allowance, LE/TE stock, spar slots, servo channels, joiner holes and servo bays) or a **2D shape** from a DXF or SVG drawing. The part, the foam block and both carriage paths update live in 3D as you type. |
| **Preview** | Estimated time, cut lengths, kerf at each end, peak carriage speeds, the foam block you need and where to put it. Scrub or play back the cut with the timeline. Export G-code. |
| **Device** | Connect over USB (or to the built-in **simulator**), live position, jog both carriages together or separately, home, unlock, set zero, wire on/off, start/pause/resume/stop the job, console. A **STOP** button (feed hold + reset, wire off) stays in the top bar while connected. |

![Device tab: a job running on the simulator](docs/device.png)

**Presets, like a slicer's printer/filament profiles:**

- *Machine*: distance between the carriages, travel, axis letters, travel speed, feed mode, baud rate.
- *Material*: cut speed, kerf at cut speed and at half speed, wire power, preheat. The built-in
  EPS / XPS / EPP presets are only starting points. Run **Kerf test** (top bar): it cuts two
  60 × 30 mm pieces, one at full speed and one at half, and you enter the measured kerf in your
  own material preset.

Your presets and airfoil library are stored in `~/.config/openhowi` (Linux),
`~/Library/Application Support/openhowi` (macOS) or `%APPDATA%\openhowi` (Windows).
Projects save as `.howi.json` files with loaded `.dat` airfoils embedded (database airfoils by id), so you can share them.

**Try it without a machine:** pick *Simulator (10× speed)* as the port in the Device tab.

### Airfoil database

Pick **Browse airfoil database…** in an airfoil field to choose from 68 common model-aircraft
sections, grouped as trainer & sport, gliders, flying wings (reflexed), symmetric & tail, high lift,
and full-size & scale, with their thickness and camber. Search by name or use (*hand launch*,
*flying wing*). You can still load any `.dat` file (Selig or Lednicer format).

![Airfoil database: the gliders category](docs/airfoils.png)

The coordinates come from the [UIUC Airfoil Coordinates Database](https://m-selig.ae.illinois.edu/ads/coord_database.html)
(see `src/openhowi/airfoils/README.md`); the NACA 4-digit sections are generated. In job files and on
the command line an airfoil is a NACA code (`naca2412`), a database id (`sd7037`) or a `.dat` file;
`openhowi airfoils` lists the ids.

### Built-up wings: sheeting, LE/TE stock, spars, channels, joiners and servo bays

For cores that get skinned, open the wing panel's **Core**, **Spars**, **Servo channels**, **Joiners**
and **Servo bays** groups:

![Core sections with their wire paths: two spars and a servo channel in a sheeted core; three spar caps and two channels in a plain core](docs/core.png)

- **Sheeting** (top and bottom can differ, e.g. 1.5 mm balsa or 0.6 mm obeche): the core is cut
  undersize by that much, so the finished wing has the airfoil's shape. Near the trailing edge the
  airfoil gets thinner than the two skins together; the core stops where they meet, and the preview
  tells you how far that is from the trailing edge (the skins are glued together there, or onto a TE strip).
- **LE / TE stock**: cut the core back flat by this much (along the chord) to fit a leading-edge or
  trailing-edge strip.
- **Spars**: add as many as you need (*+ Add spar*). Each is a rectangular slot for a spar cap in the
  top and/or bottom surface, at a % of the chord (the tip can use a different position), measured
  from the core surface, i.e. under the skins.
- **Servo channels**: a round spanwise channel for servo leads (*+ Add servo channel*), at a % of the
  chord, centred in the core (or moved up or down), at the diameter you set. The wire cannot be
  lifted, so it reaches the channel through a straight slit from the bottom (or top) surface, goes
  round it and leaves along the slit; tape or sheet over the slit.
- **Joiners**: a straight hole through the whole panel for a joiner tube or rod (round) or a
  blade (rectangular), cut like a servo channel through a slit (*+ Add joiner hole*). By default it
  runs square to the root face, as most joiners do; set a *tip position* to make it follow a swept or
  tapered panel, and a *dihedral* for a straight joiner across a dihedral joint (the hole then runs
  down towards the tip by half the angle). The hole's section in each face is worked out from its
  axis, so a slanted tube still comes out round and true to size.
- **Servo bays** (*+ Add servo bay*): a pocket at a distance from the root, at a % of the local chord,
  with its length, width and depth. The wire can only cut what runs through the whole panel, so
  bays are cut by hand: open_HOWI checks that each bay fits, leaves foam under it and stays clear of
  spar slots, joiners and other bays, shows it in red in 3D, says whether a servo channel runs into
  it, and prints a **1:1 template** (*Preview → Servo bay template*, an SVG in mm) with the bays,
  slots and slits in plan view and the distances to measure from the root face and the front of
  the core.
- Anything that would leave less than 1 mm of foam, does not fit on the core, or overlaps another
  slot, channel or joiner is refused with a message saying which one. So is a layout where two cuts
  swap places between root and tip (for example a joiner square to the root of a swept panel
  crossing a spar slot): their cuts would cross inside the panel.
- Root and tip are built with the same number of points, so every slot, channel and edge runs
  straight from root to tip.

![Root sections with their wire paths: a joiner tube from below, a blade from above, spar caps and a servo channel; a sheeted core with an 8 mm tube under the main spar](docs/joiners.png)

![Servo bay template: the panel in plan view with the bay, spar slot and slits, and the distances to measure](docs/bay-template.png)

`examples/sheeted_wing.toml` shows the same options for the command line.

### 2D shapes from DXF or SVG

Switch the part to **2D shape** and load a drawing: fuselage formers, ribs, letters, signs, any closed outline.

![A fuselage former with three lightening holes, ready to cut](docs/shape.png)

- **What is read.** SVG: paths (every command, including arcs), rect, circle, ellipse, polygon,
  polyline, line, groups, transforms and `<use>` clones, in real units (`width="100mm"` + `viewBox`).
  DXF (ASCII): LINE, LWPOLYLINE and POLYLINE with arc segments, CIRCLE, ARC, ELLIPSE, SPLINE and
  blocks (INSERT), in the drawing's `$INSUNITS`. Text must be converted to outlines first
  (Inkscape: *Path → Object to Path*); the app tells you what it skipped.
- **Outlines and holes.** Loose lines and arcs are joined into closed outlines (gaps up to 0.2 mm by
  default). An outline inside another becomes a hole; a shape inside a hole is a separate part again.
- **Scale, rotate, mirror**, set the **length along the wire**, and optionally **taper** the shape
  towards the tip face (tip scale and shift). The app refuses tapers that are too steep for where the part sits.
- **Cut planning.** Every shape is entered from behind the block (X = 0) along a horizontal lead-in
  that avoids the other shapes. Holes are cut on the way round the outline, through the shortest
  straight "bridge" cut from the outline. The wire cannot be lifted, so each hole leaves one
  kerf-wide slit in the part to glue; the preview says how many. Holes narrower than the kerf are skipped.

Try `examples/shapes/former.svg` (one shape, three holes) and `examples/shapes/star_and_d.dxf`
(two shapes, a block insert and an outline drawn as separate lines and an arc).

**Run it on a Raspberry Pi** at the machine and use it from another computer on your network:
`openhowi gui --host 0.0.0.0 --no-browser`. Anyone who can reach that address can move the
machine, so only do this on a network you trust.

## How the cut works

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
2. scales, sweeps and twists each section and moves it outward by half its kerf. The kerf
   at each end comes from the material and how fast that end actually moves. On a tapered
   panel the narrow end moves more slowly and melts a wider slot;
3. extends the line through each root/tip pair out to both carriages. These are the paths
   the XY and UV axes must follow;
4. times each segment so the wire never moves through the foam faster than the cut speed
   at either end of the part. Both carriages arrive together; the outer one may move faster.

In machine coordinates the trailing edge faces the origin. The wire rises at X=0 (behind the
foam), cuts in to the trailing edge, goes over the upper surface (through any spar slot) to the
leading edge, back under the lower surface, up the trailing-edge face of a blunt or cut-back core,
out the entry slot, and returns to the origin. Start every job with
both carriages at the origin. Use **Set zero here** in the Device tab to choose where that is.

## Hardware

Any controller that accepts 4-axis G-code over USB works. A common, inexpensive setup is
an **Arduino Mega 2560 + RAMPS 1.4** (or similar) running a 4-axis GRBL 1.1 build such as
**Grbl-Mega-5X**:

- set the machine preset's *axis letters* to the letters your firmware uses, e.g. `XYUV` or `XYZA`.
  The firmware must report the axes in that order;
- if the wire's power supply is switched or PWM-controlled from the spindle output, set the
  material's *wire power* to the `S` value for M3 (relative to GRBL's `$30`), and turn laser
  mode off (`$32=0`) so the wire stays hot during every move. Otherwise leave it blank and
  switch the wire by hand;
- set steps/mm and max rates (`$100`… / `$110`…) for each axis as usual.

G-code uses inverse-time feed (G93) by default, because every controller reads it the same
way. Switch the machine preset to *Units per minute (G94)* only if yours lacks G93.

## Command line

```bash
openhowi slice my-wing.howi.json -o my-wing.gcode   # slice a project saved from the app
openhowi shape former.svg --span 80 --material "XPS extruded (blue/pink)"   # cut a DXF/SVG drawing
openhowi wing examples/tapered_wing.toml --svg wing.svg   # TOML job file, explicit kerf values
openhowi wing examples/sheeted_wing.toml --bay-template bays.svg   # built-up core + 1:1 servo bay template
openhowi airfoils glider                             # search the airfoil database
openhowi airfoils --export sd7037 -o sd7037.dat      # write a database airfoil as a .dat file
openhowi send wing.gcode --port /dev/ttyACM0         # stream to GRBL (COM3 etc. on Windows)
openhowi send wing.gcode --dry-run                   # check a file without a machine
openhowi naca 2412 -o naca2412.dat                   # write a NACA 4-digit airfoil
```

`examples/tapered_wing.toml` documents every option of the TOML job format.

## Safety

- Hot-wire cutting expanded polystyrene and similar foams gives off fumes. Cut in a well
  ventilated area.
- The wire is hot enough to burn. Keep the power supply's current limited, and never leave
  a hot wire still inside the foam or unattended.
- Run new G-code on the simulator, or once on the machine with the wire cold, before cutting.

## Roadmap

- Arrange several parts in one block automatically; lead-ins from below or the side when shapes block each other
- Wing-root dowel and hold-down holes on the template; cutting short joiner holes with a heated tube
- Homing/limit-aware placement and a block-size optimiser
- Rotary-table (5th axis) support
- Packaged desktop builds (one-click install on Windows/macOS)

## Development

```bash
pip install -e '.[dev]'
pytest
ruff check src tests && ruff format --check src tests
```

| Module | |
|---|---|
| `airfoil`, `airfoildb`, `geometry`, `wing`, `core` | Airfoil parsing/generation and the airfoil database, 2D geometry, wing sections with sheeting, stock, slots, channels, joiners and servo bays |
| `svgimport`, `dxfimport`, `curves`, `shapes` | Drawing import, curve flattening, outlines/holes, kerf and cut planning for 2D shapes |
| `toolpath`, `gcode` | Placement, the 4-axis projection, feed timing and G-code output |
| `material`, `presets`, `project`, `calibration`, `template` | Slicer layer: presets, kerf model, project slicing, kerf test, servo bay template |
| `simulator`, `controller`, `sender` | Simulated GRBL, the threaded device controller, the CLI streamer |
| `server`, `web/` | Local HTTP API and the browser UI (three.js r179 vendored under `web/vendor`) |

## License

open_HOWI is free software: you can redistribute it and/or modify it under the terms of the
[GNU General Public License](LICENSE) as published by the Free Software Foundation, either
version 3 of the License, or (at your option) any later version.

The bundled three.js (`src/openhowi/web/vendor/three`) is MIT licensed. The airfoil coordinates in
`src/openhowi/airfoils` come from the UIUC Airfoil Coordinates Database, by way of AeroSandbox (MIT);
see the README in that folder.
