# SPDX-License-Identifier: GPL-3.0-or-later
"""Local web server for the open_HOWI app (``openhowi gui``).

The UI runs in the browser; slicing and the serial connection run here.
By default it only listens on 127.0.0.1. Because this server can move a
machine and switch a hot wire on, it also refuses requests whose Host header
is not this machine (DNS rebinding) and only accepts JSON writes from its own
origin (other websites cannot drive it through your browser).
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
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
    default_project,
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


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class App:
    """The JSON API, independent of HTTP so it is easy to test."""

    def __init__(self, store: PresetStore | None = None, controller: MachineController | None = None):
        self.store = store or PresetStore()
        self.controller = controller or MachineController()
        self.routes: dict[tuple[str, str], Callable[[dict[str, Any]], Any]] = {
            ("GET", "/api/info"): self.info,
            ("GET", "/api/presets"): self.presets,
            ("POST", "/api/presets/save"): self.save_preset,
            ("POST", "/api/presets/delete"): self.delete_preset,
            ("GET", "/api/airfoils"): self.airfoils,
            ("GET", "/api/airfoils/file"): self.airfoil_file,
            ("POST", "/api/airfoils"): self.add_airfoil,
            ("POST", "/api/airfoil/shape"): self.airfoil_shape,
            ("POST", "/api/shapes/inspect"): self.inspect_shape,
            ("POST", "/api/slice"): self.slice,
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

    def info(self, _: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": __version__,
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
        return {"naca": NACA_CODES, "library": self.store.airfoils()}

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


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: App, allow_hosts: tuple[str, ...] = ()):
        super().__init__(address, Handler)
        self.app = app
        self.allow_hosts = {h.lower() for h in allow_hosts}
        self.lan = address[0] not in ("127.0.0.1", "localhost", "::1")


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

    def _dispatch(self, method: str) -> None:
        if not self._host_allowed():
            self._send_json({"error": "unknown host"}, 403)
            return
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            if method == "GET":
                self._static(url.path)
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

    def _static(self, path: str) -> None:
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
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(body)


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    allow_hosts: tuple[str, ...] = (),
    app: App | None = None,
) -> None:
    app = app or App()
    server = Server((host, port), app, allow_hosts)
    url = f"http://{'localhost' if host in ('127.0.0.1', '::1') else host}:{server.server_address[1]}/"
    print(f"open_HOWI {__version__} running at {url}  (Ctrl-C to quit)")
    if server.lan:
        print("WARNING: listening on the network. Anyone who can reach this address can move the machine.")
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


__all__ = ["ApiError", "App", "Server", "serve"]
