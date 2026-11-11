# SPDX-License-Identifier: GPL-3.0-or-later
"""Background machine controller for the GUI's Device tab.

One I/O thread owns the serial connection: it polls the status, streams job
lines with GRBL's character-counting protocol, sends console/jog commands and
parses every reply. The GUI calls the public methods from other threads.
"""

from __future__ import annotations

import itertools
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from .sender import DEFAULT_RX_BUFFER, SerialTransport, Transport, clean_line
from .simulator import SimulatedGrbl

SIM_PORTS = {"sim": 1.0, "sim:10": 10.0}
FEED_HOLD, CYCLE_START, SOFT_RESET, JOG_CANCEL, STATUS = b"!", b"~", b"\x18", b"\x85", b"?"


class ControllerError(RuntimeError):
    pass


def list_ports() -> list[dict[str, str]]:
    ports = []
    try:
        from serial.tools import list_ports as lp  # type: ignore[import-not-found]

        for p in lp.comports():
            label = p.device if not p.description or p.description == "n/a" else f"{p.device} — {p.description}"
            ports.append({"id": p.device, "label": label})
    except ImportError:
        pass
    ports.append({"id": "sim", "label": "Simulator (real time)"})
    ports.append({"id": "sim:10", "label": "Simulator (10× speed)"})
    return ports


def open_transport(port: str, baud: int, axis_letters: str = "XYUV") -> Transport:
    if port in SIM_PORTS:
        return SimulatedGrbl(axis_letters=axis_letters, speedup=SIM_PORTS[port])
    return SerialTransport(port, baud, timeout=0.05)


def parse_status(msg: str) -> dict[str, Any]:
    """Parse ``<Idle|MPos:...|FS:...|WCO:...>`` (GRBL 1.1) or ``<Idle,MPos:...>`` (0.9)."""
    body = msg.strip()[1:-1]
    parts = re.split(r"\|", body) if "|" in body else [body]
    out: dict[str, Any] = {"state": re.split(r"[:,]", parts[0])[0]}
    for part in parts[1:] if len(parts) > 1 else []:
        key, _, value = part.partition(":")
        nums = []
        for v in value.split(","):
            try:
                nums.append(float(v))
            except ValueError:
                pass
        if key in ("MPos", "WPos", "WCO"):
            out[key.lower()] = nums
        elif key in ("FS", "F") and nums:
            out["feed"] = nums[0]
    return out


class MachineController:
    def __init__(
        self,
        opener: Callable[[str, int, str], Transport] = open_transport,
        poll_interval: float = 0.2,
        rx_buffer: int = DEFAULT_RX_BUFFER,
        log_size: int = 500,
    ):
        self._opener = opener
        self.poll_interval = poll_interval
        self.rx_buffer = rx_buffer
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._transport: Transport | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._log: deque[dict[str, Any]] = deque(maxlen=log_size)
        self._log_ids = itertools.count(1)
        self._reset_state()

    def _reset_state(self) -> None:
        self.port: str | None = None
        self.axis_letters = "XYUV"
        self.ready = False
        self.status: dict[str, Any] = {"state": "Disconnected"}
        self._wco = [0.0, 0.0, 0.0, 0.0]
        self._pending: deque[tuple[int, str]] = deque()  # (bytes, "job" | "cmd")
        self._in_flight = 0
        self._commands: deque[str] = deque()
        self._job_lines: list[str] = []
        self._pauses: list[str] = []
        """What each M0 in the job is for, in order."""
        self._next_pause = 0
        self.job: dict[str, Any] | None = None

    # Public API (any thread) ---------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._transport is not None

    def connect(self, port: str, baud: int = 115200, axis_letters: str = "XYUV") -> None:
        with self._lock:
            if self._transport is not None:
                raise ControllerError(f"already connected to {self.port}")
            transport = self._opener(port, baud, axis_letters)
            self._reset_state()
            self._transport, self.port, self.axis_letters = transport, port, axis_letters.upper()
            self.status = {"state": "Connecting"}
            self._running = True
            self._log_line("info", f"connected to {port} at {baud} baud")
            self._thread = threading.Thread(target=self._run, name="openhowi-io", daemon=True)
            self._thread.start()
        # No "\r\n\r\n" wake-up: GRBL answers "ok" to empty lines, and stray oks would be
        # miscounted against job lines. It announces itself (or answers "?") once it has booted.

    def disconnect(self) -> None:
        with self._lock:
            transport, thread = self._transport, self._thread
            self._running = False
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            if transport is not None:
                close = getattr(transport, "close", None)
                if close:
                    close()
            if self.job and self.job["state"] in ("running", "paused", "finishing"):
                self.job.update(state="error", error="disconnected")
            self._transport = None
            self.status = {"state": "Disconnected"}
            self.ready = False
            self._log_line("info", "disconnected")

    def snapshot(self, since: int = 0) -> dict[str, Any]:
        with self._lock:
            status = dict(self.status)
            if "mpos" in status and "wpos" not in status:
                status["wpos"] = [m - w for m, w in zip(status["mpos"], self._wco)]
            job = dict(self.job) if self.job else None
            if job and job.get("started"):
                job["elapsed"] = round((job.get("finished") or time.time()) - job["started"], 1)
            return {
                "connected": self.connected,
                "port": self.port,
                "ready": self.ready,
                "axis_letters": self.axis_letters,
                "status": status,
                "job": job,
                "log": [e for e in self._log if e["id"] > since],
            }

    def command(self, line: str) -> None:
        """Queue a console command (refused while a job is streaming)."""
        line = clean_line(line)
        if not line:
            return
        with self._lock:
            self._require_connected()
            if self._job_active():
                raise ControllerError("a job is running; pause or stop it first")
            self._commands.append(line)
            self._log_line("tx", line)

    def jog(self, moves: dict[str, float], feed: float) -> None:
        words = " ".join(f"{k.upper()}{v:.3f}" for k, v in moves.items() if v)
        if not words:
            return
        if feed <= 0:
            raise ControllerError("jog feed must be > 0")
        self.command(f"$J=G91 G21 {words} F{feed:.0f}")

    def jog_cancel(self) -> None:
        self._realtime(JOG_CANCEL)

    def home(self) -> None:
        self.command("$H")

    def unlock(self) -> None:
        self.command("$X")

    def set_zero(self) -> None:
        self.command("G10 L20 P1 " + " ".join(f"{a}0" for a in self.axis_letters))

    def go_to_zero(self, feed: float) -> None:
        self.command("G90 G94 G1 " + " ".join(f"{a}0" for a in self.axis_letters) + f" F{feed:.0f}")

    def wire(self, on: bool, power: float | None = None) -> None:
        self.command(("M3" + (f" S{power:g}" if power is not None else "")) if on else "M5")

    def start_job(self, gcode: str, name: str = "job") -> None:
        lines = [c for c in (clean_line(x) for x in gcode.splitlines()) if c]
        pauses, note = [], ""
        for raw in gcode.splitlines():  # "(PAUSE: ...)" comments say what each M0 is for
            text = raw.strip()
            if text.upper().startswith("(PAUSE:"):
                note = text[7:].rstrip(")").strip()
            elif clean_line(text) in ("M0", "M00"):
                pauses.append(note or "Program pause: resume when ready.")
                note = ""
        if not lines:
            raise ControllerError("the G-code is empty")
        too_long = next((x for x in lines if len(x) + 1 > self.rx_buffer), None)
        if too_long:
            raise ControllerError(f"line too long for the controller buffer: {too_long}")
        with self._lock:
            self._require_connected()
            if not self.ready:
                raise ControllerError("the controller has not answered yet")
            if self._job_active():
                raise ControllerError("a job is already running")
            if self.status.get("state") not in ("Idle", None):
                raise ControllerError(f"the machine must be Idle to start (it is {self.status.get('state')})")
            self._job_lines = lines
            self._pauses, self._next_pause = pauses, 0
            self.job = {"name": name, "state": "running", "total": len(lines), "sent": 0, "acked": 0,
                        "started": time.time(), "finished": None, "error": None, "message": None,
                        "pauses": len(pauses)}  # fmt: skip
            self._log_line("info", f"job started: {name} ({len(lines)} lines)")

    def pause(self) -> None:
        with self._lock:
            if not self.job or self.job["state"] not in ("running", "finishing"):
                raise ControllerError("no running job")
            self.job["state"] = "paused"
        self._realtime(FEED_HOLD)

    def resume(self) -> None:
        with self._lock:
            if not self.job or self.job["state"] != "paused":
                raise ControllerError("the job is not paused")
            self.job["state"] = "running" if self.job["acked"] < self.job["total"] else "finishing"
            if self.job.get("message"):
                self._next_pause += 1
                self.job["message"] = None
        self._realtime(CYCLE_START)

    def stop(self) -> None:
        """Feed hold, then soft reset: stops motion, keeps the position, switches the wire off."""
        self._require_connected()
        self._realtime(FEED_HOLD)
        time.sleep(0.4)
        with self._lock:
            self._realtime(SOFT_RESET)
            self._pending.clear()
            self._in_flight = 0
            self._commands.clear()
            if self.job and self.job["state"] in ("running", "paused", "finishing"):
                self.job.update(state="stopped", finished=time.time())
                self._log_line("info", "job stopped")

    # Internals -------------------------------------------------------------------

    def _require_connected(self) -> None:
        if self._transport is None:
            raise ControllerError("not connected")

    def _job_active(self) -> bool:
        return bool(self.job and self.job["state"] in ("running", "paused", "finishing"))

    @property
    def job_active(self) -> bool:
        """A job is running, paused or finishing."""
        return self._job_active()

    def _log_line(self, kind: str, text: str) -> None:
        self._log.append({"id": next(self._log_ids), "t": round(time.time(), 2), "kind": kind, "text": text})

    def _write(self, data: bytes) -> None:
        transport = self._transport
        if transport is None:
            return
        with self._write_lock:
            transport.write(data)

    def _realtime(self, byte: bytes) -> None:
        self._require_connected()
        self._write(byte)

    def _fill(self) -> None:
        """Send queued commands, then job lines, while they fit in the controller's buffer."""
        with self._lock:
            if not self.ready:
                return
            while self._commands:
                data = (self._commands[0] + "\n").encode("ascii")
                if self._pending and self._in_flight + len(data) > self.rx_buffer:
                    return
                self._commands.popleft()
                self._send(data, "cmd")
            job = self.job
            while job and job["state"] in ("running", "paused") and job["sent"] < job["total"]:
                data = (self._job_lines[job["sent"]] + "\n").encode("ascii")
                if self._pending and self._in_flight + len(data) > self.rx_buffer:
                    return
                job["sent"] += 1
                self._send(data, "job")

    def _send(self, data: bytes, source: str) -> None:
        self._pending.append((len(data), source))
        self._in_flight += len(data)
        self._write(data)

    def _run(self) -> None:
        next_poll = 0.0
        while self._running:
            try:
                now = time.monotonic()
                if now >= next_poll:
                    self._write(STATUS)
                    next_poll = now + self.poll_interval
                self._fill()
                transport = self._transport
                if transport is None:
                    break
                raw = transport.readline()
                if raw:
                    self._handle(raw.decode("ascii", "replace").strip())
            except OSError as exc:  # serial port unplugged
                with self._lock:
                    self._log_line("error", f"connection lost: {exc}")
                self._running = False
                threading.Thread(target=self.disconnect, daemon=True).start()
                return

    def _handle(self, msg: str) -> None:
        if not msg:
            return
        with self._lock:
            if msg.startswith("<"):
                self.status = parse_status(msg)
                if "wco" in self.status:
                    self._wco = self.status["wco"]
                if not self.ready:
                    self.ready = True
                job = self.job
                if job and job["state"] == "finishing" and self.status["state"] == "Idle":
                    job.update(state="done", finished=time.time())
                    self._log_line("info", "job finished")
                elif (
                    job
                    and job["state"] in ("running", "finishing")
                    and self.status["state"] == "Hold"
                    and self._next_pause < len(self._pauses)
                ):  # the job's own M0: tell the operator what to do
                    message = self._pauses[self._next_pause]
                    job.update(state="paused", message=message)
                    self._log_line("info", f"paused: {message}")
                return
            if msg == "ok" or msg.startswith("error"):
                size, source = self._pending.popleft() if self._pending else (0, "cmd")
                self._in_flight -= size
                job = self.job
                if source == "job" and job:
                    job["acked"] += 1
                    if msg != "ok":
                        line = self._job_lines[job["acked"] - 1]
                        job.update(state="error", error=f"{msg} on line {job['acked']}: {line}", finished=time.time())
                        self._log_line("error", job["error"])
                        threading.Thread(target=self._safe_stop, daemon=True).start()
                    elif job["acked"] >= job["total"] and job["state"] == "running":
                        job["state"] = "finishing"
                elif msg != "ok":
                    self._log_line("error", msg)
                return
            if msg.startswith(("Grbl", "ALARM")):
                if msg.startswith("Grbl"):
                    self.ready = True
                self._pending.clear()
                self._in_flight = 0
                job = self.job
                if job and job["state"] in ("running", "paused", "finishing"):
                    job.update(state="error", error=f"controller reported {msg}", finished=time.time())
                self._log_line("error" if msg.startswith("ALARM") else "rx", msg)
                return
            self._log_line("rx", msg)

    def _safe_stop(self) -> None:
        try:
            self._realtime(FEED_HOLD)
            time.sleep(0.4)
            self._realtime(SOFT_RESET)
        except ControllerError:
            pass
