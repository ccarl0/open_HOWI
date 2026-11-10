# SPDX-License-Identifier: GPL-3.0-or-later
"""Local web server for the open_HOWI app (``openhowi gui``).

The UI runs in the browser (or the app's own window); slicing and the serial
connection run here. By default it only listens on 127.0.0.1. Because this
server can move a machine and switch a hot wire on, it also refuses requests
whose Host header is not this machine (DNS rebinding) and only accepts JSON
writes from its own origin (other websites cannot drive it through your
browser). Listening on the network (e.g. on a Raspberry Pi next to the
machine), it can ask other computers for a password.
"""

from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import mimetypes
import socket
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__, airfoildb
from .airfoil import AirfoilError
from .calibration import kerf_test
from .controller import ControllerError, MachineController, list_ports
from .presets import (
    KINDS,
    MACHINE_FIELDS,
    MATERIAL_FIELDS,
    PresetError,
    PresetStore,
    machine_from_values,
    material_from_values,
)
from .project import (
    DEFAULT_PARTS,
    DEFAULT_PLACEMENTS,
    PART_SCHEMAS,
    PART_TYPES,
    PLACEMENT_SCHEMAS,
    SliceError,
    bay_template,
    default_project,
    fuselage_plans,
    fuselage_stl,
    inspect_drawing,
    resolve_airfoil,
    slice_project,
)
from .toolpath import PlacementError

MAX_BODY = 8 * 1024 * 1024
NACA_CODES = ["0009", "0012", "0015", "2408", "2410", "2412", "2415", "4412", "4415", "6412"]
WEB_ROOT = Path(str(resources.files("openhowi") / "web"))
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)
CSP_WINDOW = CSP.replace("script-src 'self'", "script-src 'self' 'unsafe-eval'")
"""The app window's own page: pywebview builds its bridge to Python with ``new Function``."""


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class App:
    """The JSON API, independent of HTTP so it is easy to test."""

    def __init__(self, store: PresetStore | None = None, controller: MachineController | None = None):
        self.store = store or PresetStore()
        self.controller = controller or MachineController()
        self.quit: Callable[[], None] | None = None
        """Set when the app was started from a desktop shortcut: how the UI's Quit button stops it."""
        self.focus: Callable[[], None] | None = None
        """Set in the app's own window: brings it to the front (when it is started a second time)."""
        self.window = False
        """True in the app's own window (it has a menu with Quit, and native file dialogs)."""
        self.shell_token: str | None = None
        """The app window loads the page with this secret: only that page may run pywebview's bridge,
        which needs 'unsafe-eval'; browser tabs keep the strict policy."""
        self.remote: dict[str, Any] | None = None
        """Listening on the network: the addresses to reach it at, and whether it asks for a password."""
        self.routes: dict[tuple[str, str], Callable[[dict[str, Any]], Any]] = {
            ("GET", "/api/info"): self.info,
            ("GET", "/api/presets"): self.presets,
            ("POST", "/api/presets/save"): self.save_preset,
            ("POST", "/api/presets/delete"): self.delete_preset,
            ("GET", "/api/airfoils"): self.airfoils,
            ("GET", "/api/airfoils/file"): self.airfoil_file,
            ("GET", "/api/airfoils/database"): self.airfoil_database,
            ("POST", "/api/airfoils"): self.add_airfoil,
            ("POST", "/api/airfoil/shape"): self.airfoil_shape,
            ("POST", "/api/shapes/inspect"): self.inspect_shape,
            ("POST", "/api/slice"): self.slice,
            ("POST", "/api/template/bays"): self.bays_template,
            ("POST", "/api/template/fuselage"): self.fuselage_template,
            ("POST", "/api/guide/fuselage"): self.fuselage_guide,
            ("POST", "/api/export/fuselage-stl"): self.fuselage_model,
            ("POST", "/api/calibration/kerf"): self.kerf_test,
            ("GET", "/api/device/ports"): lambda q: {"ports": list_ports()},
            ("GET", "/api/device/state"): self.device_state,
            ("POST", "/api/device/connect"): self.device_connect,
            ("POST", "/api/device/disconnect"): lambda b: self.controller.disconnect(),
            ("POST", "/api/device/command"): lambda b: self.controller.command(_str(b, "line")),
            ("POST", "/api/device/jog"): self.device_jog,
            ("POST", "/api/device/jog_cancel"): lambda b: self.controller.jog_cancel(),
            ("POST", "/api/device/home"): lambda b: self.controller.home(),
            ("POST", "/api/device/unlock"): lambda b: self.controller.unlock(),
            ("POST", "/api/device/zero"): lambda b: self.controller.set_zero(),
            ("POST", "/api/device/go_zero"): lambda b: self.controller.go_to_zero(_num(b, "feed", 600.0)),
            ("POST", "/api/device/wire"): self.device_wire,
            ("POST", "/api/device/job"): lambda b: self.controller.start_job(_str(b, "gcode"), _str(b, "name", "job")),
            ("POST", "/api/device/pause"): lambda b: self.controller.pause(),
            ("POST", "/api/device/resume"): lambda b: self.controller.resume(),
            ("POST", "/api/device/stop"): lambda b: self.controller.stop(),
            ("POST", "/api/app/quit"): self.quit_app,
            ("POST", "/api/app/focus"): self.focus_app,
        }

    def handle(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        route = self.routes.get((method, path))
        if route is None:
            raise ApiError(f"no such endpoint: {method} {path}", 404)
        try:
            result = route(payload)
        except (SliceError, PresetError, AirfoilError, PlacementError, ControllerError) as exc:
            raise ApiError(str(exc)) from exc
        return {"ok": True} if result is None else result

    # Settings & slicing ----------------------------------------------------------

    def quit_app(self, _: dict[str, Any]) -> None:
        if self.quit is None:
            raise ApiError("this app was started from the command line: stop it there (Ctrl-C)")
        if self.controller.connected:
            self.controller.disconnect()
        self.quit()

    def focus_app(self, _: dict[str, Any]) -> None:
        if self.focus is None:
            raise ApiError("no app window to bring forward")
        self.focus()

    def info(self, _: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": __version__,
            "desktop": self.quit is not None,
            "window": self.window,
            "remote": self.remote,
            "schema": {
                "machine": MACHINE_FIELDS,
                "material": MATERIAL_FIELDS,
                "part": PART_SCHEMAS,
                "placement": PLACEMENT_SCHEMAS,
            },
            "part_types": PART_TYPES,
            "default_parts": DEFAULT_PARTS,
            "default_placements": DEFAULT_PLACEMENTS,
            "default_project": default_project(),
            "naca": NACA_CODES,
        }

    def presets(self, _: dict[str, Any]) -> dict[str, Any]:
        return {kind: self.store.list(kind) for kind in KINDS}

    def save_preset(self, body: dict[str, Any]) -> dict[str, Any]:
        values = body.get("values")
        if not isinstance(values, dict):
            raise ApiError("values must be an object")
        self.store.save(_str(body, "kind"), _str(body, "name"), values)
        return self.presets({})

    def delete_preset(self, body: dict[str, Any]) -> dict[str, Any]:
        self.store.delete(_str(body, "kind"), _str(body, "name"))
        return self.presets({})

    def airfoils(self, _: dict[str, Any]) -> dict[str, Any]:
        return {
            "naca": NACA_CODES,
            "library": self.store.airfoils(),
            "database": airfoildb.catalog(with_outlines=False),
        }

    def airfoil_database(self, _: dict[str, Any]) -> dict[str, Any]:
        return {"categories": list(airfoildb.CATEGORIES), "airfoils": airfoildb.catalog()}

    def airfoil_file(self, query: dict[str, Any]) -> dict[str, Any]:
        name = _str(query, "file")
        return {"file": name, "dat": self.store.airfoil_text(name)}

    def add_airfoil(self, body: dict[str, Any]) -> dict[str, Any]:
        entry = self.store.add_airfoil(_str(body, "filename"), _str(body, "dat"))
        return {"added": entry, "library": self.store.airfoils()}

    def airfoil_shape(self, body: dict[str, Any]) -> dict[str, Any]:
        foil = resolve_airfoil(body.get("source")).normalized()
        return {"name": foil.name, "points": [[round(x, 5), round(y, 5)] for x, y in foil.points]}

    def inspect_shape(self, body: dict[str, Any]) -> dict[str, Any]:
        return inspect_drawing(_str(body, "filename"), _str(body, "data"))

    def bays_template(self, body: dict[str, Any]) -> dict[str, Any]:
        name, svg = bay_template(body.get("project") or {})
        return {"filename": name, "svg": svg}

    def fuselage_template(self, body: dict[str, Any]) -> dict[str, Any]:
        name, svg = fuselage_plans(body.get("project") or {})
        return {"filename": name, "svg": svg}

    def fuselage_guide(self, body: dict[str, Any]) -> dict[str, Any]:
        from .guide import cutting_guide_html

        name, html = cutting_guide_html(body.get("project") or {})
        return {"filename": name, "html": html}

    def fuselage_model(self, body: dict[str, Any]) -> dict[str, Any]:
        import base64

        name, data = fuselage_stl(body.get("project") or {})
        return {"filename": name, "stl": base64.b64encode(data).decode("ascii")}

    def slice(self, body: dict[str, Any]) -> dict[str, Any]:
        result = slice_project(body.get("project") or {})
        return {**result.data, "gcode": result.gcode}

    def kerf_test(self, body: dict[str, Any]) -> dict[str, Any]:
        try:
            material = material_from_values(dict(body.get("material") or {}))
            machine = machine_from_values(dict(body.get("machine") or {}), material)
            gcode, instructions = kerf_test(machine)
        except ValueError as exc:  # includes MachineLimitError
            raise ApiError(str(exc)) from exc
        return {"gcode": gcode, "instructions": instructions}

    # Device -------------------------------------------------------------------------

    def device_state(self, query: dict[str, Any]) -> dict[str, Any]:
        try:
            since = int(query.get("since", 0))
        except (TypeError, ValueError):
            since = 0
        return self.controller.snapshot(since)

    def device_connect(self, body: dict[str, Any]) -> None:
        baud = body.get("baud", 115200)
        if not isinstance(baud, int) or isinstance(baud, bool):
            raise ApiError("baud must be a whole number")
        try:
            self.controller.connect(_str(body, "port"), baud, _str(body, "axis_letters", "XYUV"))
        except (OSError, ValueError, RuntimeError) as exc:  # port busy / missing, pyserial absent
            if isinstance(exc, ControllerError):
                raise
            raise ApiError(f"cannot open {body.get('port')}: {exc}") from exc

    def device_jog(self, body: dict[str, Any]) -> None:
        moves = body.get("moves")
        if not isinstance(moves, dict) or not all(
            isinstance(k, str) and len(k) == 1 and isinstance(v, (int, float)) for k, v in moves.items()
        ):
            raise ApiError("moves must map axis letters to distances")
        letters = self.controller.axis_letters
        if any(k.upper() not in letters for k in moves):
            raise ApiError(f"jog axes must be among {letters}")
        self.controller.jog({k: float(v) for k, v in moves.items()}, _num(body, "feed", 600.0))

    def device_wire(self, body: dict[str, Any]) -> None:
        power = body.get("power")
        if power is not None and (not isinstance(power, (int, float)) or power < 0):
            raise ApiError("power must be a number >= 0")
        self.controller.wire(bool(body.get("on")), power)


def _str(body: dict[str, Any], key: str, default: str | None = None) -> str:
    value = body.get(key, default)
    if not isinstance(value, str):
        raise ApiError(f"{key} must be a string")
    return value


def _num(body: dict[str, Any], key: str, default: float) -> float:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(f"{key} must be a number")
    return float(value)


LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], app: App, allow_hosts: tuple[str, ...] = (), password: str | None = None
    ):
        super().__init__(address, Handler)
        self.app = app
        self.lan = address[0] not in ("127.0.0.1", "localhost", "::1")
        hosts = {h.lower() for h in allow_hosts}
        if self.lan:  # this computer's own name is not an attacker's domain: allow it (raspberrypi.local)
            name = socket.gethostname().lower()
            hosts |= {name, f"{name}.local"} if name and "." not in name else {name}
        self.allow_hosts = hosts
        self.password = password or None
        """Asked of every computer but this one, when listening on the network."""


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = f"openhowi/{__version__}"

    def log_message(self, format: str, *args: Any) -> None:  # quiet
        pass

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    # Safety checks ------------------------------------------------------------------

    def _hostname(self) -> str:
        host = self.headers.get("Host", "")
        if host.startswith("["):
            return host[1 : host.find("]")].lower()
        return host.rsplit(":", 1)[0].lower()

    def _host_allowed(self) -> bool:
        name = self._hostname()
        if name in ("localhost", "127.0.0.1", "::1") or name in self.server.allow_hosts:
            return True
        if self.server.lan:  # IP literals only: a DNS name could be a rebinding attack
            try:
                ipaddress.ip_address(name)
                return True
            except ValueError:
                return False
        return False

    def _write_allowed(self) -> str | None:
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return "writes must be JSON"
        origin = self.headers.get("Origin")
        if origin is not None and origin != f"http://{self.headers.get('Host', '')}":
            return "cross-origin request refused"
        return None

    # Dispatch -----------------------------------------------------------------------

    def _authorised(self) -> bool:
        """With a password set, other computers must give it (HTTP basic auth; any user name)."""
        password = self.server.password
        if not password or self.client_address[0] in LOOPBACK:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(given.encode(), password.encode())

    def _dispatch(self, method: str) -> None:
        if not self._host_allowed():
            self._send_json({"error": "unknown host"}, 403)
            return
        if not self._authorised():
            body = b"open_HOWI asks for its password."
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="open_HOWI", charset="UTF-8"')
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            if method == "GET":
                self._static(url.path, parse_qs(url.query).get("shell", [""])[-1])
            else:
                self._send_json({"error": "not found"}, 404)
            return

        payload: dict[str, Any]
        if method == "GET":
            payload = {k: v[-1] for k, v in parse_qs(url.query).items()}
        else:
            problem = self._write_allowed()
            if problem:
                self._send_json({"error": problem}, 403)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send_json({"error": "request too large"}, 413)
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON"}, 400)
                return
            if not isinstance(payload, dict):
                self._send_json({"error": "expected a JSON object"}, 400)
                return
        try:
            self._send_json(self.server.app.handle(method, url.path, payload))
        except ApiError as exc:
            self._send_json({"error": str(exc)}, exc.status)
        except Exception as exc:  # noqa: BLE001 - report instead of dropping the connection
            self._send_json({"error": f"internal error: {exc.__class__.__name__}: {exc}"}, 500)

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: str, shell: str = "") -> None:
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (WEB_ROOT / rel).resolve()
        if WEB_ROOT.resolve() not in target.parents or not target.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js":
            ctype = "text/javascript"
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8" if ctype.startswith("text/") else ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        token = self.server.app.shell_token
        in_window = bool(token and shell and self.client_address[0] in LOOPBACK and hmac.compare_digest(shell, token))
        self.send_header("Content-Security-Policy", CSP_WINDOW if in_window else CSP)
        self.end_headers()
        self.wfile.write(body)


def lan_addresses(port: int) -> list[str]:
    """Addresses other computers on the network can reach this one at (best guess)."""
    ips: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))  # no packet is sent: this picks the outgoing interface
            ips.add(probe.getsockname()[0])
    except OSError:
        pass
    try:
        ips |= {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        pass
    urls = [f"http://{ip}:{port}/" for ip in sorted(ips) if not ip.startswith("127.")]
    name = socket.gethostname()
    if name and "." not in name:
        urls.append(f"http://{name.lower()}.local:{port}/")
    return urls


def make_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    app: App | None = None,
    allow_hosts: tuple[str, ...] = (),
    password: str | None = None,
) -> Server:
    """The app's server, listening but not yet serving (call ``serve_forever``)."""
    app = app or App()
    server = Server((host, port), app, allow_hosts, password)
    if server.lan:
        app.remote = {"addresses": lan_addresses(server.server_address[1]), "password": bool(server.password)}
    return server


def local_url(server: Server) -> str:
    host = server.server_address[0]
    shown = "localhost" if host in ("127.0.0.1", "::1", "0.0.0.0", "::") else host
    return f"http://{shown}:{server.server_address[1]}/"


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    allow_hosts: tuple[str, ...] = (),
    app: App | None = None,
    desktop: bool = False,
    password: str | None = None,
) -> None:
    """Run the app until Ctrl-C (or, with ``desktop``, until the UI's Quit button)."""
    server = make_server(host, port, app, allow_hosts, password)
    app = server.app
    if desktop:
        app.quit = lambda: threading.Timer(0.3, server.shutdown).start()
    url = local_url(server)
    how = "press Quit in the app or close this window to stop it" if desktop else "Ctrl-C to quit"
    print(f"open_HOWI {__version__} running at {url}  ({how})")
    if server.lan:
        assert app.remote is not None
        print("On the network at: " + "  ".join(app.remote["addresses"]))
        if server.password:
            print("Other computers are asked for the password.")
        else:
            print("WARNING: no password. Anyone who can reach these addresses can move the machine.")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.controller.connected:
            app.controller.disconnect()
        server.server_close()


__all__ = ["ApiError", "App", "Server", "lan_addresses", "local_url", "make_server", "serve"]
