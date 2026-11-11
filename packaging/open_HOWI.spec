# SPDX-License-Identifier: GPL-3.0-or-later
# PyInstaller recipe for the packaged app: `pyinstaller packaging/open_HOWI.spec` after `pip install .`
# (and, on Linux, `pip install "pywebview[qt]"` for the app window).
# Builds dist/open_HOWI/ with two programs: open_HOWI (the app, in its own window) and openhowi (the
# command line); on macOS also dist/open_HOWI.app.
import importlib.util
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

import openhowi

here = Path(SPECPATH)
mac = sys.platform == "darwin"
icon = str(here / ("icon.icns" if mac else "icon.ico"))
# Shown as the company in the programs' file details on Windows (as in windows/open_HOWI.iss). The
# "Publisher" in Windows' SmartScreen warning comes only from a code signature.
PUBLISHER = "Carlo Cecchetti"
COPYRIGHT = f"Copyright (C) 2026 {PUBLISHER}. GPL-3.0-or-later"


def file_details(description, filename):
    """The Windows version resource: what Properties > Details and Task Manager show."""
    if sys.platform != "win32":
        return None
    from PyInstaller.utils.win32 import versioninfo as v

    numbers = (tuple(int(n) for n in re.findall(r"\d+", openhowi.__version__)[:3]) + (0, 0, 0))[:3] + (0,)
    strings = {
        "CompanyName": PUBLISHER,
        "FileDescription": description,
        "FileVersion": openhowi.__version__,
        "InternalName": filename,
        "LegalCopyright": COPYRIGHT,
        "OriginalFilename": f"{filename}.exe",
        "ProductName": "open_HOWI",
        "ProductVersion": openhowi.__version__,
    }
    return v.VSVersionInfo(
        ffi=v.FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            v.StringFileInfo([v.StringTable("040904B0", [v.StringStruct(k, s) for k, s in strings.items()])]),
            v.VarFileInfo([v.VarStruct("Translation", [0x0409, 1200])]),
        ],
    )


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
    version=file_details("open_HOWI", "open_HOWI"),
)
cli_exe = EXE(
    PYZ(cli.pure),
    cli.scripts,
    [],
    exclude_binaries=True,
    name="openhowi",
    console=True,
    icon=icon,
    version=file_details("open_HOWI command line", "openhowi"),
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
            "NSHumanReadableCopyright": COPYRIGHT,
        },
    )
