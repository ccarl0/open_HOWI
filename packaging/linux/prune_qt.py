# SPDX-License-Identifier: GPL-3.0-or-later
"""Leave out the parts of Qt the app window does not use (3D, multimedia, QML controls, translations).

python packaging/linux/prune_qt.py dist/open_HOWI
"""

import shutil
import sys
from pathlib import Path

UNUSED = (
    "Qt3D",
    "Quick3D",
    "QuickControls2",
    "QuickDialogs2",
    "QuickEffects",
    "QuickParticles",
    "QuickShapes",
    "QuickTest",
    "QuickTimeline",
    "QuickVectorImage",
    "QuickLayouts",
    "QuickTemplates2",
    "Multimedia",
    "RemoteObjects",
    "Sensors",
    "SerialPort",
    "Bluetooth",
    "Nfc",
    "Pdf",
    "Charts",
    "DataVisualization",
    "Graphs",
    "Lottie",
    "Scxml",
    "SpatialAudio",
    "TextToSpeech",
    "VirtualKeyboard",
    "Designer",
    "Help",
    "Svg",
    "Test",
    "Sql",
    "StateMachine",
    "ShaderTools",
    "Location",
    "Protobuf",
    "Grpc",
    "HttpServer",
    "WebSockets",
    "WebView",
    "LabsAnimation",
    "LabsFolderListModel",
    "LabsQmlModels",
    "LabsSettings",
    "LabsSharedImage",
    "LabsWavefrontMesh",
    "PositioningQuick",
    "SensorsQuick",
    "QmlLocalStorage",
    "QmlXmlListModel",
    "Quick3DXr",
)


def main(folder: str) -> None:
    qt = Path(folder) / "_internal" / "PyQt6" / "Qt6"
    if not qt.is_dir():
        print("no Qt in this build")
        return
    before = sum(f.stat().st_size for f in qt.rglob("*") if f.is_file())
    for lib in (qt / "lib").glob("libQt6*"):
        if any(lib.name.startswith(f"libQt6{name}") for name in UNUSED):
            lib.unlink()
    shutil.rmtree(qt / "qml", ignore_errors=True)
    for item in (qt / "translations").glob("*.qm"):  # keep qtwebengine_locales: the web engine needs it
        item.unlink()
    for name in UNUSED:
        for module in (qt.parent).glob(f"Qt{name}*.so"):
            module.unlink()
    after = sum(f.stat().st_size for f in qt.rglob("*") if f.is_file())
    print(f"Qt: {before / 1e6:.0f} MB -> {after / 1e6:.0f} MB")


if __name__ == "__main__":
    main(sys.argv[1])
