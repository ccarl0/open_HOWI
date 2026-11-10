# SPDX-License-Identifier: GPL-3.0-or-later
# PyInstaller recipe for the packaged app: `pyinstaller packaging/open_HOWI.spec` after `pip install .`
# Builds dist/open_HOWI/ (a folder with the open_HOWI program) and, on macOS, dist/open_HOWI.app.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

import openhowi

here = Path(SPECPATH)
mac = sys.platform == "darwin"
icon = str(here / ("icon.icns" if mac else "icon.ico"))

a = Analysis(
    [str(here / "launcher.py")],
    datas=collect_data_files("openhowi"),  # the browser app (web/) and the airfoil database
    hiddenimports=collect_submodules("openhowi") + collect_submodules("serial"),
    excludes=["tkinter", "unittest", "pydoc_data", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="open_HOWI",
    console=not mac,  # Windows/Linux: the console window shows it is running; closing it stops the app
    icon=icon,
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="open_HOWI")
if mac:
    app = BUNDLE(
        coll,
        name="open_HOWI.app",
        icon=icon,
        bundle_identifier="io.github.ccarl0.openhowi",
        version=openhowi.__version__,
        info_plist={
            "CFBundleName": "open_HOWI",
            "CFBundleDisplayName": "open_HOWI",
            "CFBundleShortVersionString": openhowi.__version__,
            "LSUIElement": True,  # no Dock icon: it lives in the browser; quit from the app's Quit button
            "NSHumanReadableCopyright": "GPL-3.0-or-later",
        },
    )
