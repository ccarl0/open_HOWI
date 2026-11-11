# SPDX-License-Identifier: GPL-3.0-or-later
# PyInstaller recipe for the packaged app: `pyinstaller packaging/open_HOWI.spec` after `pip install .`
# (and, on Linux, `pip install "pywebview[qt]"` for the app window).
# Builds dist/open_HOWI/ with two programs: open_HOWI (the app, in its own window) and openhowi (the
# command line); on macOS also dist/open_HOWI.app.
import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

import openhowi

here = Path(SPECPATH)
mac = sys.platform == "darwin"
icon = str(here / ("icon.icns" if mac else "icon.ico"))
ours = collect_submodules("openhowi") + collect_submodules("serial")
window = []
if importlib.util.find_spec("webview"):
    window = collect_submodules("webview.platforms", filter=lambda name: name.split(".")[-1] in {
        "win32": ("winforms", "edgechromium"), "darwin": ("cocoa",)}.get(sys.platform, ("qt",)))
excluded = ["tkinter", "unittest", "pydoc_data", "test"]

app = Analysis(
    [str(here / "launcher.py")],
    datas=collect_data_files("openhowi") + collect_data_files("webview"),  # browser app, airfoils, pywebview's js
    hiddenimports=ours + window,
    excludes=excluded,
)
cli = Analysis(
    [str(here / "cli.py")],
    datas=collect_data_files("openhowi"),
    hiddenimports=ours,
    excludes=excluded + ["webview", "PyQt6", "PySide6", "clr", "pythonnet", "objc", "AppKit", "WebKit"],
)
app_exe = EXE(
    PYZ(app.pure),
    app.scripts,
    [],
    exclude_binaries=True,
    name="open_HOWI",
    console=False,  # its own window; no console window behind it
    icon=icon,
)
cli_exe = EXE(
    PYZ(cli.pure),
    cli.scripts,
    [],
    exclude_binaries=True,
    name="openhowi",
    console=True,
    icon=icon,
)
coll = COLLECT(app_exe, cli_exe, app.binaries, app.datas, cli.binaries, cli.datas, name="open_HOWI")
if mac:
    bundle = BUNDLE(
        coll,
        name="open_HOWI.app",
        icon=icon,
        bundle_identifier="io.github.ccarl0.openhowi",
        version=openhowi.__version__,
        info_plist={
            "CFBundleName": "open_HOWI",
            "CFBundleDisplayName": "open_HOWI",
            "CFBundleShortVersionString": openhowi.__version__,
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "GPL-3.0-or-later",
        },
    )
