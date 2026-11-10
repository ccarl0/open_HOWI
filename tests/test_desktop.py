import socket
import threading

import pytest

from openhowi import desktop
from openhowi.controller import MachineController
from openhowi.presets import PresetStore
from openhowi.server import ApiError, App, Server


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_running_instance_and_quit(tmp_path):
    port = free_port()
    assert not desktop.running_instance(port)
    app = App(PresetStore(tmp_path), MachineController())
    with pytest.raises(ApiError, match="command line"):
        app.handle("POST", "/api/app/quit", {})
    assert app.handle("GET", "/api/info", {})["desktop"] is False
    server = Server(("127.0.0.1", port), app)
    app.quit = lambda: threading.Thread(target=server.shutdown).start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert desktop.running_instance(port)
        assert app.handle("GET", "/api/info", {})["desktop"] is True
        assert app.handle("POST", "/api/app/quit", {}) == {"ok": True}
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        server.server_close()
    assert not desktop.running_instance(port)


def test_packaged_app_runs_commands(tmp_path, capsys):
    assert desktop.main(["naca", "2412", "-o", str(tmp_path / "n.dat")]) == 0
    assert (tmp_path / "n.dat").read_text().startswith("NACA 2412")


def test_second_launch_opens_the_running_one(monkeypatch):
    opened = []
    monkeypatch.setattr(desktop, "running_instance", lambda port=desktop.DEFAULT_PORT: True)
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    assert desktop.main([]) == 0
    assert opened == ["http://localhost:8765/"]
