# SPDX-License-Identifier: GPL-3.0-or-later
"""Stream G-code to a GRBL-compatible controller over USB serial.

The controller (e.g. an Arduino Mega running a 4-axis GRBL build) generates
the step pulses itself, so the PC only needs a USB port: no parallel port and
no real-time operating system. Lines are sent with GRBL's character-counting
protocol, which keeps the controller's receive buffer full for smooth motion.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Protocol, Self

DEFAULT_RX_BUFFER = 127  # GRBL's 128-byte serial buffer, minus one


class Transport(Protocol):
    def write(self, data: bytes) -> object: ...

    def readline(self) -> bytes:
        """Return one line, or ``b""`` when the read timed out."""
        ...


class GrblError(RuntimeError):
    """The controller rejected a line (``error:N``)."""


class GrblAlarm(RuntimeError):
    """The controller entered an alarm state; motion has stopped."""


_COMMENT = re.compile(r"\([^)]*\)|;.*$")


def clean_line(line: str) -> str:
    """Strip comments and whitespace; GRBL ignores both and they waste buffer space."""
    text = _COMMENT.sub("", line)
    text = "".join(text.split()).upper()
    return "" if text == "%" else text


class SerialTransport:
    """pyserial-backed transport (``pip install open-howi[serial]``)."""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 0.5):
        try:
            import serial  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError("sending G-code needs pyserial: pip install 'open-howi[serial]'") from exc
        self._serial = serial.Serial(port, baudrate, timeout=timeout)

    def write(self, data: bytes) -> object:
        return self._serial.write(data)

    def readline(self) -> bytes:
        return self._serial.readline()

    def close(self) -> None:
        self._serial.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class GrblStreamer:
    def __init__(
        self,
        transport: Transport,
        rx_buffer_size: int = DEFAULT_RX_BUFFER,
        on_message: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.transport = transport
        self.rx_buffer_size = rx_buffer_size
        self.on_message = on_message or (lambda msg: None)
        self._sleep = sleep

    def wake(self, settle: float = 2.0) -> list[str]:
        """Wake the controller (opening the port resets most Arduinos) and drain its banner."""
        self.transport.write(b"\r\n\r\n")
        self._sleep(settle)
        banner = []
        while raw := self.transport.readline():
            msg = raw.decode("ascii", "replace").strip()
            if msg:
                banner.append(msg)
                self.on_message(msg)
        return banner

    def command(self, line: str) -> None:
        """Send one line and wait for its ``ok`` (e.g. ``$X`` to clear an alarm lock)."""
        self.stream([line])

    def stream(self, lines: Iterable[str], progress: Callable[[int, int], None] | None = None) -> int:
        """Send ``lines``; returns the number of lines the controller acknowledged.

        Stops at the first ``error:`` or ``ALARM`` by raising. Lines already in
        the controller's buffer still run; call :meth:`abort` to stop them.
        """
        program = [c for c in (clean_line(line) for line in lines) if c]
        total = len(program)
        pending: deque[tuple[int, str]] = deque()
        in_flight = 0
        acked = 0

        def wait_ack() -> None:
            nonlocal in_flight, acked
            size, _ = self._read_ack(pending)
            in_flight -= size
            acked += 1
            if progress:
                progress(acked, total)

        for line in program:
            data = (line + "\n").encode("ascii")
            if len(data) > self.rx_buffer_size:
                raise GrblError(f"line longer than the controller buffer: {line!r}")
            while pending and in_flight + len(data) > self.rx_buffer_size:
                wait_ack()
            self.transport.write(data)
            pending.append((len(data), line))
            in_flight += len(data)
        while pending:
            wait_ack()
        return acked

    def _read_ack(self, pending: deque[tuple[int, str]]) -> tuple[int, str]:
        while True:
            raw = self.transport.readline()
            if not raw:
                # GRBL holds back "ok" while its planner is full; a long, slow
                # cut can take minutes per segment, so keep waiting.
                continue
            msg = raw.decode("ascii", "replace").strip()
            if not msg:
                continue
            if msg == "ok":
                return pending.popleft()
            if msg.startswith("error"):
                _, line = pending.popleft()
                raise GrblError(f"controller replied {msg} to {line!r}")
            if msg.startswith("ALARM"):
                raise GrblAlarm(msg)
            self.on_message(msg)

    def status(self) -> str | None:
        """Query the machine state (``Idle``, ``Run``, ``Hold``, ``Alarm``, ...)."""
        self.transport.write(b"?")
        while raw := self.transport.readline():
            msg = raw.decode("ascii", "replace").strip()
            if msg.startswith("<"):
                return re.split(r"[|,>:]", msg[1:], maxsplit=1)[0]
            if msg.startswith("ALARM"):
                raise GrblAlarm(msg)
            if msg:
                self.on_message(msg)
        return None

    def wait_until_idle(self, poll: float = 0.5) -> None:
        """Block until all buffered motion has finished."""
        while True:
            state = self.status()
            if state == "Idle":
                return
            if state == "Alarm":
                raise GrblAlarm("controller is in alarm state")
            self._sleep(poll)

    def abort(self) -> None:
        """Feed hold, then soft reset: stops motion, keeps position, switches the wire off."""
        self.transport.write(b"!")
        self._sleep(0.5)
        self.transport.write(b"\x18")
