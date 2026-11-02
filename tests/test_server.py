import http.client
import json
import threading

import pytest

from openhowi.controller import MachineController
from openhowi.presets import PresetStore
from openhowi.project import default_project
from openhowi.server import ApiError, App, Server
from openhowi.simulator import SimulatedGrbl


@pytest.fixture
def app(tmp_path):
    controller = MachineController(opener=lambda port, baud, axes: SimulatedGrbl(axes, speedup=500.0))
    application = App(PresetStore(tmp_path), controller)
    yield application
    if controller.connected:
        controller.disconnect()


@pytest.fixture
def server(app):
    srv = Server(("127.0.0.1", 0), app)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Host": f"localhost:{port}"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    conn.request(method, path, body=None if body is None else json.dumps(body), headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    ctype = resp.getheader("Content-Type", "")
    conn.close()
    return resp.status, (json.loads(data) if ctype.startswith("application/json") else data), resp


def test_info_and_slice(app):
    info = app.handle("GET", "/api/info", {})
    assert {"machine", "material", "part", "placement"} <= set(info["schema"])
    result = app.handle("POST", "/api/slice", {"project": info["default_project"]})
    assert result["ok"] and result["gcode"].startswith("(open_HOWI")


def test_slice_error_is_an_api_error(app):
    project = default_project()
    project["placement"]["root_offset"] = 5000
    with pytest.raises(ApiError, match="does not fit"):
        app.handle("POST", "/api/slice", {"project": project})


def test_unknown_route(app):
    with pytest.raises(ApiError) as exc:
        app.handle("GET", "/api/nope", {})
    assert exc.value.status == 404


def test_presets_and_airfoils_round_trip(app):
    values = default_project()["machine"] | {"tower_distance": 1234.0}
    presets = app.handle("POST", "/api/presets/save", {"kind": "machine", "name": "Shop cutter", "values": values})
    assert any(p["name"] == "Shop cutter" and not p["builtin"] for p in presets["machine"])
    dat = "MY FOIL\n1 0\n0.5 0.05\n0 0\n0.5 -0.03\n1 0\n"
    added = app.handle("POST", "/api/airfoils", {"filename": "my.dat", "dat": dat})
    assert added["added"]["name"] == "MY FOIL"
    assert app.handle("GET", "/api/airfoils/file", {"file": "my.dat"})["dat"] == dat
    with pytest.raises(ApiError):
        app.handle("POST", "/api/airfoils", {"filename": "bad.dat", "dat": "nonsense"})


def test_kerf_test_endpoint(app):
    p = default_project()
    r = app.handle("POST", "/api/calibration/kerf", {"machine": p["machine"], "material": p["material"]})
    assert "Kerf test" in r["gcode"] and "calipers" in r["instructions"]


def test_device_endpoints(app):
    app.handle("POST", "/api/device/connect", {"port": "sim", "baud": 115200, "axis_letters": "XYUV"})
    with pytest.raises(ApiError, match="among"):
        app.handle("POST", "/api/device/jog", {"moves": {"Z": 1}, "feed": 100})
    with pytest.raises(ApiError, match="baud"):
        app.handle("POST", "/api/device/connect", {"port": "sim", "baud": "fast"})
    with pytest.raises(ApiError, match="already connected"):
        app.handle("POST", "/api/device/connect", {"port": "sim"})
    assert app.handle("GET", "/api/device/state", {"since": "0"})["connected"] is True
    app.handle("POST", "/api/device/disconnect", {})
    assert app.handle("GET", "/api/device/state", {})["connected"] is False


def test_http_serves_the_app(server):
    status, body, resp = request(server, "GET", "/")
    assert status == 200 and b"open_HOWI" in body
    assert "default-src 'self'" in resp.getheader("Content-Security-Policy")
    status, body, _ = request(server, "GET", "/vendor/three/three.module.min.js")
    assert status == 200 and body.startswith(b"/**")
    status, _, _ = request(server, "GET", "/../pyproject.toml")
    assert status == 404


def test_http_rejects_foreign_hosts_and_origins(server):
    status, body, _ = request(server, "GET", "/api/info", headers={"Host": "attacker.example"})
    assert status == 403 and body["error"] == "unknown host"
    status, body, _ = request(server, "POST", "/api/device/stop", {}, headers={"Origin": "http://attacker.example"})
    assert status == 403 and "cross-origin" in body["error"]
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=10)
    conn.request(
        "POST", "/api/device/stop", body="{}", headers={"Host": f"localhost:{server}", "Content-Type": "text/plain"}
    )
    assert conn.getresponse().status == 403
    conn.close()


def test_http_same_origin_write(server):
    origin = {"Origin": f"http://localhost:{server}"}
    status, body, _ = request(server, "POST", "/api/slice", {"project": default_project()}, headers=origin)
    assert status == 200 and body["ok"]
    status, body, _ = request(server, "POST", "/api/device/stop", {}, headers=origin)
    assert status == 400 and body["error"] == "not connected"
