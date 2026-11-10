import base64
import socket
import threading
import urllib.error
import urllib.request

import pytest

from openhowi import desktop
from openhowi.controller import MachineController
from openhowi.presets import PresetStore
from openhowi.server import CSP, CSP_WINDOW, ApiError, App, Server, lan_addresses, make_server


class FakeWindow:
    """Stands in for a pywebview window: records calls, answers file dialogs from a list."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.calls = []
        self.on_top = False
        self.destroyed = False

    def create_file_dialog(self, kind, **kw):
        self.calls.append((kind, kw))
        return self.answers.pop(0) if self.answers else None

    def run_js(self, script):
        self.calls.append(("js", script))

    def destroy(self):
        self.destroyed = True

    def restore(self):
        self.calls.append("restore")

    def show(self):
        self.calls.append("show")


@pytest.fixture
def shell(tmp_path, monkeypatch):
    webview = pytest.importorskip("webview")
    monkeypatch.setattr(desktop, "_config_dir", lambda: tmp_path)
    server = make_server("127.0.0.1", 0, App(PresetStore(tmp_path), MachineController()))
    win = desktop.AppWindow(server, "http://localhost:1/")
    win.window = FakeWindow()
    yield win, webview
    server.server_close()


def test_native_file_dialogs(shell, tmp_path):
    win, webview = shell
    api = desktop.WindowApi(win)
    project = tmp_path / "wing.howi.json"
    project.write_text('{"version": 1}')
    win.window.answers = [[str(project)], None, str(tmp_path / "out.gcode"), (str(tmp_path / "f.stl"),)]
    assert api.open_file("project") == {"name": "wing.howi.json", "text": '{"version": 1}'}
    kind, options = win.window.calls[0]
    assert kind == webview.FileDialog.OPEN and "howi.json" in options["file_types"][0]
    assert api.open_file("drawing") is None
    assert api.save_file("cut.gcode", "G93\n") == str(tmp_path / "out.gcode")
    assert (tmp_path / "out.gcode").read_text() == "G93\n"
    assert win.window.calls[2][1]["save_filename"] == "cut.gcode"
    assert api.save_file("f.stl", base64.b64encode(b"solid").decode(), True) == str(tmp_path / "f.stl")
    assert (tmp_path / "f.stl").read_bytes() == b"solid"


def test_closing_while_a_cut_runs_asks_first(shell, monkeypatch):
    win, _ = shell
    controller = win.app.controller
    assert win._closing() is True  # nothing running: closes
    monkeypatch.setattr(type(controller), "job_active", property(lambda self: True))
    stopped = []
    monkeypatch.setattr(controller, "stop", lambda: stopped.append(True))
    assert win._closing() is False  # kept open; the page asks
    for _ in range(50):
        if any(c[0] == "js" for c in win.window.calls if isinstance(c, tuple)):
            break
        threading.Event().wait(0.02)
    assert any("confirm-quit" in c[1] for c in win.window.calls if isinstance(c, tuple) and c[0] == "js")
    win.quit(force=True)
    assert stopped == [True] and win.window.destroyed and win._closing() is True


def test_window_geometry_is_remembered(shell, tmp_path):
    win, _ = shell
    win.geometry.update(width=1300, height=850, x=40, y=30)
    win._closed()
    again = desktop.AppWindow(win.server, "http://localhost:1/")
    assert again.geometry == {"width": 1300, "height": 850, "x": 40, "y": 30}


def test_menu_and_focus(shell):
    win, _ = shell
    titles = [m.title for m in win.menu()]
    assert titles == ["File", "View", "Help"]
    file_menu = win.menu()[0]
    actions = [getattr(i, "title", "-") for i in file_menu.items]
    assert "Open project…" in actions and "Quit" in actions
    win.focus()
    assert "restore" in win.window.calls and win.window.on_top


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url, headers=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_only_the_app_window_page_may_run_its_bridge(tmp_path):
    app = App(PresetStore(tmp_path), MachineController())
    server = Server(("127.0.0.1", free_port()), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        assert get(base)[1]["Content-Security-Policy"] == CSP
        app.shell_token = "secret-token"
        assert get(base + "?shell=wrong")[1]["Content-Security-Policy"] == CSP
        assert get(base + "?shell=secret-token")[1]["Content-Security-Policy"] == CSP_WINDOW
        assert "'unsafe-eval'" in CSP_WINDOW and "'unsafe-eval'" not in CSP
    finally:
        server.shutdown()
        server.server_close()


def test_password_for_other_computers(tmp_path, monkeypatch):
    app = App(PresetStore(tmp_path), MachineController())
    server = Server(("127.0.0.1", free_port()), app, password="foam")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert get(base + "/api/info")[0] == 200  # this computer itself is never asked
        monkeypatch.setattr("openhowi.server.LOOPBACK", ())  # pretend the request comes from elsewhere
        status, headers, _ = get(base + "/api/info")
        assert status == 401 and headers["WWW-Authenticate"].startswith("Basic")
        good = "Basic " + base64.b64encode(b"anyone:foam").decode()
        bad = "Basic " + base64.b64encode(b"anyone:wrong").decode()
        assert get(base + "/api/info", {"Authorization": bad})[0] == 401
        assert get(base + "/api/info", {"Authorization": good})[0] == 200
        assert get(base + "/", {"Authorization": good})[0] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_network_mode_reports_its_addresses(tmp_path):
    server = make_server("0.0.0.0", 0, App(PresetStore(tmp_path), MachineController()), password="x")
    try:
        remote = server.app.handle("GET", "/api/info", {})["remote"]
        assert remote["password"] is True and all(u.startswith("http://") for u in remote["addresses"])
        name = socket.gethostname().lower()
        if name and "." not in name:
            assert f"{name}.local" in server.allow_hosts
    finally:
        server.server_close()
    assert all(":8765/" in u for u in lan_addresses(8765))
    local = make_server("127.0.0.1", 0, App(PresetStore(tmp_path), MachineController()))
    assert local.app.remote is None
    local.server_close()
    with pytest.raises(ApiError, match="no app window"):
        local.app.handle("POST", "/api/app/focus", {})
