import time

import pytest

from openhowi.controller import ControllerError, MachineController, parse_status
from openhowi.project import default_project, slice_project
from openhowi.simulator import SimulatedGrbl


def wait_for(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    raise AssertionError("timed out")


@pytest.fixture
def machine():
    sims = []

    def opener(port, baud, axes):
        sim = SimulatedGrbl(axis_letters=axes, speedup=500.0, read_timeout=0.01)
        sims.append(sim)
        return sim

    controller = MachineController(opener=opener, poll_interval=0.02)
    controller.connect("sim", 115200, "XYUV")
    wait_for(lambda: controller.snapshot()["status"].get("state") == "Idle")
    yield controller, sims[0]
    if controller.connected:
        controller.disconnect()


def test_parse_status_grbl_11_and_09():
    s = parse_status("<Run|MPos:1.000,2.000,3.000,4.000|FS:200,0|WCO:0.500,0,0,0>")
    assert s == {"state": "Run", "mpos": [1, 2, 3, 4], "feed": 200, "wco": [0.5, 0, 0, 0]}
    assert parse_status("<Hold:0|WPos:1,2,3,4>")["state"] == "Hold"
    assert parse_status("<Idle,MPos:1.0,2.0,3.0,4.0>")["state"] == "Idle"


def test_job_runs_to_completion(machine):
    controller, sim = machine
    gcode = slice_project(default_project()).gcode
    controller.start_job(gcode, "wing")
    wait_for(lambda: controller.snapshot()["job"]["state"] == "done", timeout=20)
    job = controller.snapshot()["job"]
    assert job["acked"] == job["total"] > 100
    assert not sim.overflowed
    assert sim.current_position() == pytest.approx([0, 0, 0, 0])
    assert len(sim.received) == job["total"]


def test_jog_zero_and_console(machine):
    controller, sim = machine
    controller.jog({"X": 10, "U": 10, "Y": 5}, 600)
    wait_for(lambda: controller.snapshot()["status"].get("wpos") == pytest.approx([10, 5, 10, 0]))
    controller.set_zero()
    wait_for(lambda: controller.snapshot()["status"].get("wpos") == pytest.approx([0, 0, 0, 0]))
    assert sim.wco == pytest.approx([10, 5, 10, 0])
    controller.wire(True, 500)
    wait_for(lambda: sim.wire)
    controller.wire(False)
    wait_for(lambda: not sim.wire)
    texts = [entry["text"] for entry in controller.snapshot()["log"]]
    assert "M3S500" in texts and "M5" in texts


def test_pause_resume_and_stop(machine):
    controller, sim = machine
    sim.speedup = 20.0
    controller.start_job(slice_project(default_project()).gcode, "wing")
    wait_for(lambda: controller.snapshot()["status"].get("state") == "Run")
    controller.pause()
    wait_for(lambda: controller.snapshot()["status"].get("state") == "Hold")
    with pytest.raises(ControllerError):
        controller.command("G0 X0")  # console is locked while a job runs
    controller.resume()
    wait_for(lambda: controller.snapshot()["status"].get("state") == "Run")
    controller.stop()
    wait_for(lambda: controller.snapshot()["status"].get("state") == "Idle")
    assert controller.snapshot()["job"]["state"] == "stopped"
    assert not sim.wire


def test_error_line_stops_the_job(machine):
    controller, _ = machine
    controller.start_job("G21\nG1 X10 Y0 U10 V0\nG1 X0 F100\n", "bad")  # G1 without a feed rate
    wait_for(lambda: controller.snapshot()["job"]["state"] == "error")
    assert "error:22" in controller.snapshot()["job"]["error"]


def test_start_requires_idle_connected_controller():
    controller = MachineController(opener=lambda *a: SimulatedGrbl())
    with pytest.raises(ControllerError, match="not connected"):
        controller.start_job("G0 X1", "x")
    with pytest.raises(ControllerError, match="empty"):
        controller.start_job("(just a comment)", "x")


def test_simulator_alarm_and_unlock():
    sim = SimulatedGrbl(read_timeout=0.01)
    sim.write(b"G1 X50 F6000\n")
    sim.write(b"\x18")  # reset while moving -> alarm
    lines = []
    while raw := sim.readline():
        lines.append(raw.strip())
    assert b"ALARM:3" in lines and sim.state == "Alarm"
    sim.write(b"G0 X1\n")
    assert sim.readline().strip() == b"error:9"
    sim.write(b"$X\n")
    while raw := sim.readline():
        lines.append(raw.strip())
    assert sim.state == "Idle"
