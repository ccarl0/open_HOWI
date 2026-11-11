# SPDX-License-Identifier: GPL-3.0-or-later
"""The desktop app: open_HOWI in its own window, with a menu and the system's file dialogs.

The window shows the same interface as the browser version, served from inside this
process; it needs pywebview and the web engine the system already has (Edge WebView2 on
Windows, WebKit on macOS, Qt WebEngine or WebKitGTK on Linux). Without one it opens in the
browser instead. Only one copy runs: starting it again brings the window to the front.
With arguments (``open_HOWI slice wing.howi.json``) it works as the ``openhowi`` command.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

DEFAULT_PORT = 8765
DOCS_URL = "https://github.com/ccarl0/open_HOWI#readme"
ISSUES_URL = "https://github.com/ccarl0/open_HOWI/issues"

# The file dialogs' filters. pywebview refuses a description with anything but letters, digits,
# "_" and spaces in it (no "G-code"): the dialog then fails instead of opening.
FILE_TYPES: dict[str, tuple[str, ...]] = {
    "project": ("open_HOWI project (*.howi.json;*.json)", "All files (*.*)"),
    "airfoil": ("Airfoil (*.dat;*.txt)", "All files (*.*)"),
    "drawing": ("Drawing (*.svg;*.dxf)", "All files (*.*)"),
    ".gcode": ("G code (*.gcode;*.nc)", "All files (*.*)"),
    ".json": ("open_HOWI project (*.howi.json;*.json)",),
    ".svg": ("SVG drawing (*.svg)",),
    ".stl": ("3D model (*.stl)",),
    ".html": ("Web page (*.html)",),
    ".dat": ("Airfoil (*.dat)",),
    ".webm": ("Video (*.webm)",),
    ".mp4": ("Video (*.mp4)",),
}


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through a proxy


def running_instance(port: int = DEFAULT_PORT) -> bool:
    """True when open_HOWI already answers on this computer at ``port``."""
    try:
        with _opener().open(f"http://127.0.0.1:{port}/api/info", timeout=1.5) as response:
            info = json.load(response)
    except (OSError, ValueError):
        return False
    return isinstance(info, dict) and "default_project" in info


def focus_instance(port: int = DEFAULT_PORT) -> bool:
    """Ask a running copy to bring its window to the front. False when it has no window."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/app/focus", data=b"{}", headers={"Content-Type": "application/json"}
    )
    try:
        with _opener().open(request, timeout=3) as response:
            return bool(json.load(response).get("ok"))
    except (OSError, ValueError):
        return False


def _config_dir() -> Path:
    from .presets import config_dir

    return config_dir()


class WindowApi:
    """What the page in the app window can ask for (``window.pywebview.api`` in JavaScript)."""

    def __init__(self, shell: AppWindow) -> None:
        self._shell = shell

    def open_file(self, kind: str = "") -> dict[str, str] | None:
        """Ask for a file to open; returns its name and text, or None when cancelled."""
        import webview

        paths = self._shell.window.create_file_dialog(
            webview.FileDialog.OPEN, file_types=FILE_TYPES.get(kind, ("All files (*.*)",))
        )
        if not paths:
            return None
        path = Path(paths[0] if not isinstance(paths, str) else paths)
        return {"name": path.name, "text": path.read_text(encoding="utf-8", errors="replace")}

    def save_file(self, name: str, data: str, binary: bool = False) -> str | None:
        """Ask where to save ``data`` (base64 when ``binary``); returns the path, or None when cancelled."""
        import webview

        suffix = Path(name).suffix.lower()
        paths = self._shell.window.create_file_dialog(
            webview.FileDialog.SAVE, save_filename=name, file_types=FILE_TYPES.get(suffix, ())
        )
        if not paths:
            return None
        path = Path(paths if isinstance(paths, str) else paths[0])
        if binary:
            path.write_bytes(base64.b64decode(data))
        else:
            path.write_text(data, encoding="utf-8")
        return str(path)

    def open_page(self, name: str, html: str) -> str:
        """Show a page (e.g. the printable cutting guide) in the web browser, where it prints best."""
        folder = Path(tempfile.gettempdir()) / "open_HOWI"
        folder.mkdir(exist_ok=True)
        path = folder / (Path(name).name or "page.html")
        path.write_text(html, encoding="utf-8")
        webbrowser.open(path.as_uri())
        return str(path)

    def open_url(self, url: str) -> None:
        if url.startswith(("https://", "http://")):
            webbrowser.open(url)

    def quit(self, force: bool = False) -> None:
        """Quit; ``force`` stops a running cut first (the page has asked)."""
        self._shell.quit(force)


class AppWindow:
    """The app window round the local server."""

    def __init__(self, server: Any, url: str, remember: bool = True) -> None:
        self.server = server
        self.app = server.app
        self.url = url
        self.window: Any = None
        self.quitting = False
        self.remember = remember  # the window's size and place, from one start to the next
        self.geometry_file = _config_dir() / "window.json"
        self.geometry = self._load_geometry() if remember else {}

    def _load_geometry(self) -> dict[str, int]:
        try:
            data = json.loads(self.geometry_file.read_text())
            return {k: int(v) for k, v in data.items() if k in ("width", "height", "x", "y")}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _save_geometry(self) -> None:
        try:
            self.geometry_file.parent.mkdir(parents=True, exist_ok=True)
            self.geometry_file.write_text(json.dumps(self.geometry))
        except OSError:
            pass

    def menu(self) -> list[Any]:
        from webview.menu import Menu, MenuAction, MenuSeparator

        def act(name: str) -> Any:
            return lambda: self.page(name)

        return [
            Menu("File", [
                MenuAction("New project", act("new")),
                MenuAction("Open project…", act("open")),
                MenuAction("Save project…", act("save")),
                MenuSeparator(),
                MenuAction("Export G-code…", act("export")),
                MenuAction("Export all cuts…", act("export-all")),
                MenuSeparator(),
                MenuAction("Kerf test…", act("kerf")),
                MenuSeparator(),
                MenuAction("Quit", act("quit")),
            ]),
            Menu("View", [
                MenuAction("Prepare", act("tab:prepare")),
                MenuAction("Preview", act("tab:preview")),
                MenuAction("Device", act("tab:device")),
                MenuSeparator(),
                MenuAction("Isometric", act("view:iso")),
                MenuAction("Along the wire", act("view:profile")),
                MenuAction("Top", act("view:top")),
                MenuAction("Side", act("view:side")),
                MenuSeparator(),
                MenuAction("Timelapse", act("timelapse")),
                MenuAction("Save timelapse video…", act("video")),
            ]),
            Menu("Help", [
                MenuAction("Documentation", lambda: webbrowser.open(DOCS_URL)),
                MenuAction("Report a problem", lambda: webbrowser.open(ISSUES_URL)),
                MenuSeparator(),
                MenuAction("About open_HOWI", act("about")),
            ]),
        ]  # fmt: skip

    def page(self, action: str) -> None:
        """Run a menu action in the page (off the GUI thread: the call waits for the page)."""
        script = f"window.openhowiMenu && window.openhowiMenu({json.dumps(action)})"
        threading.Thread(target=lambda: self.window.run_js(script), daemon=True).start()

    def focus(self) -> None:
        window = self.window
        if window is None:
            return
        window.restore()
        window.show()
        window.on_top = True  # brings it in front of other windows, then lets them over it again
        threading.Timer(0.5, lambda: setattr(window, "on_top", False)).start()

    def quit(self, force: bool = False) -> None:
        controller = self.app.controller
        if controller.job_active and force:
            try:
                controller.stop()  # feed hold and reset: motion stops, the wire switches off
            except Exception as exc:  # noqa: BLE001 - quitting anyway
                print(f"open_HOWI: could not stop the job: {exc}")
        self.quitting = True
        if self.window is not None:
            self.window.destroy()

    def _closing(self) -> bool:
        """Runs on the GUI thread when the window is asked to close; False keeps it open."""
        if self.quitting or not self.app.controller.job_active:
            return True
        self.page("confirm-quit")  # a cut is running: the page asks before stopping it
        return False

    def _closed(self) -> None:
        if self.remember:
            self._save_geometry()
        controller = self.app.controller
        if controller.connected:
            controller.disconnect()
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def run(self, check: Any = None, storage: Path | None = None) -> None:
        """Open the window and return when it closes; ``check`` runs in a thread once it is open
        (the self-test), ``storage`` is where the web engine keeps its data."""
        import secrets

        import webview

        geometry = {"width": 1440, "height": 900} | self.geometry
        self.app.shell_token = secrets.token_urlsafe(16)
        self.window = webview.create_window(
            "open_HOWI", f"{self.url}?shell={self.app.shell_token}", js_api=WindowApi(self), min_size=(960, 620),
            background_color="#e9edf2",
            menu=self.menu(), **geometry,
        )  # fmt: skip
        self.window.events.closing += self._closing
        self.window.events.closed += self._closed
        self.window.events.resized += lambda width, height: self.geometry.update(width=width, height=height)
        self.window.events.moved += lambda x, y: self.geometry.update(x=x, y=y)
        self.app.window = True
        self.app.quit = lambda: self.quit(False)
        self.app.focus = self.focus
        storage = storage or _config_dir() / "webview"
        webview.start(check, gui=_gui(), private_mode=False, storage_path=str(storage), icon=_icon())


def _icon() -> str | None:
    """The window's icon. Windows takes it from open_HOWI.exe instead: its icon loader reads only
    .ico files, and given the PNG it fails and the window never opens."""
    if sys.platform == "win32":
        return None
    return str(Path(__file__).parent / "web" / "icon-256.png")


def _gui() -> str | None:
    """The web engine to use. On Windows only Edge WebView2 runs this interface: without it (pywebview
    would quietly use Internet Explorer's engine) the browser is used instead."""
    if sys.platform == "win32":
        from webview.platforms import winforms

        if winforms.renderer != "edgechromium":
            raise RuntimeError("the Microsoft Edge WebView2 runtime is not installed")
        return "edgechromium"
    if sys.platform == "darwin":
        return "cocoa"
    import importlib.util

    return "qt" if importlib.util.find_spec("PyQt6") or importlib.util.find_spec("PySide6") else None


def run_app() -> int:
    """Start the server and show it in the app window, or in the browser when there is no window."""
    from .server import local_url, make_server, serve

    try:
        server = make_server("127.0.0.1", DEFAULT_PORT)
    except OSError:  # the usual port is taken by another program: any free one will do
        server = make_server("127.0.0.1", 0)
    url = local_url(server)
    use_browser = bool(os.environ.get("OPENHOWI_BROWSER"))
    if not use_browser:
        try:
            import webview  # noqa: F401
        except Exception as exc:  # noqa: BLE001 - any import problem: fall back to the browser
            print(f"open_HOWI: no app window ({exc}); opening it in the browser")
            use_browser = True
    if use_browser:
        server.server_close()
        serve("127.0.0.1", server.server_address[1], desktop=True)
        return 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shell = AppWindow(server, url)
    try:
        shell.run()
    except Exception as exc:  # noqa: BLE001 - no GUI toolkit for pywebview: use the browser
        print(f"open_HOWI: could not open the app window ({exc}); opening it in the browser")
        server.app.window = False
        server.app.focus = None
        server.app.quit = lambda: threading.Timer(0.3, server.shutdown).start()
        webbrowser.open(url)
        try:
            thread.join()
        except KeyboardInterrupt:
            server.shutdown()
    finally:
        server.server_close()
    return 0


def self_test(args: list[str]) -> int:
    """For the installers' checks: can this build open its window? ``--self-test [window] [--report FILE]``."""
    report: list[str] = []
    ok = True
    try:
        from importlib.metadata import version

        import webview  # noqa: F401 - part of the check: the window library loads

        try:
            report.append(f"pywebview {version('pywebview')}: ok")
        except Exception:  # noqa: BLE001 - frozen builds may lack the metadata
            report.append("pywebview: ok")
        if "window" in args:
            from .server import local_url, make_server

            # The very window the app opens (menu, icon, bridge, storage), so what stops it shows here
            server = make_server("127.0.0.1", 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            shell = AppWindow(server, local_url(server), remember=False)
            loaded: dict[str, bool] = {}

            def check() -> None:
                for _ in range(120):
                    try:
                        if shell.window.evaluate_js("Boolean(document.querySelector('#summary b'))"):
                            loaded["ok"] = True
                            break
                    except Exception:  # noqa: BLE001, S110 - not ready yet
                        pass
                    time.sleep(0.5)
                shell.quit()

            with tempfile.TemporaryDirectory(prefix="open_HOWI-self-test-", ignore_cleanup_errors=True) as storage:
                shell.run(check, storage=Path(storage))
            ok = bool(loaded.get("ok"))
            report.append("window: the app loaded in it" if ok else "window: the app did not load")
    except Exception as exc:  # noqa: BLE001 - report it
        ok = False
        report.append(f"failed: {exc.__class__.__name__}: {exc}")
    text = "\n".join(report)
    if "--report" in args:
        Path(args[args.index("--report") + 1]).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--self-test":
        return self_test(argv[1:])
    if argv:
        from .cli import main as cli

        return cli(argv)
    url = f"http://localhost:{DEFAULT_PORT}/"
    if running_instance():
        if not focus_instance():
            print(f"open_HOWI is already running: opening {url}")
            webbrowser.open(url)
        return 0
    log = _log_to_file()
    try:
        return run_app()
    except Exception as exc:
        import traceback

        if not log:
            raise
        traceback.print_exc()  # into the log, and a message box: there is no console to show it in
        _alert(f"open_HOWI could not start:\n\n{exc}\n\nThe details are in {log}")
        return 1


def _log_to_file() -> Path | None:
    """The app without a console (the Windows build: nowhere to print) keeps what it would print, and
    the web engine's messages, in open_HOWI.log next to its settings. Returns that file."""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    path = _config_dir() / "open_HOWI.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(path, "w", encoding="utf-8", buffering=1)  # noqa: SIM115 - open as long as the app
    except OSError:
        return None
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream
    print(f"open_HOWI {_version()} on {sys.platform}, {time.strftime('%Y-%m-%d %H:%M:%S')}")
    return path


def _version() -> str:
    from . import __version__

    return __version__


def _alert(text: str) -> None:
    """A message box where there is no console (Windows)."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "open_HOWI", 0x10)  # MB_ICONERROR


if __name__ == "__main__":
    sys.exit(main())
