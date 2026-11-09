# SPDX-License-Identifier: GPL-3.0-or-later
"""A simulated 4-axis GRBL 1.1 controller.

It speaks the serial protocol closely enough to try the whole app (and run the
tests) without a machine: ``ok`` once a line enters the planner, ``error:``
codes, status reports, feed hold / resume / soft reset, ``M0`` program pauses, jogging, ``G10 L20``
work offsets and timed motion (optionally sped up).
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

BANNER = b"Grbl 1.1h ['$' for help]\r\n"
RAPID_RATE = 1000.0  # mm/min for G0 and homing
_WORD = re.compile(r"([A-Z])([-+]?(?:\d+\.?\d*|\.\d+))")


@dataclass
class _Block:
    target: list[float] | None  # machine position at the end, None for a dwell
    duration: float  # seconds at 1x
    feed: float
    jog: bool = False
    wire: bool | None = None
    pause: bool = False
    """M0: hold here until cycle start."""


class SimulatedGrbl:
    """Implements the :class:`openhowi.sender.Transport` interface."""

    def __init__(
        self,
        axis_letters: str = "XYUV",
        speedup: float = 1.0,
        planner_size: int = 15,
        rx_size: int = 128,
        read_timeout: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.axes = axis_letters.upper()
        self.speedup = speedup
        self.planner_size = planner_size
        self.rx_size = rx_size
        self.read_timeout = read_timeout
        self._clock = clock
        self._cv = threading.Condition()
        self._out: deque[bytes] = deque()
        self._partial = bytearray()
        self._waiting: deque[str] = deque()  # complete lines still in the serial buffer
        self._planner: deque[_Block] = deque()
        self._block_t0 = 0.0
        self._hold_t0: float | None = None
        self.mpos = [0.0] * 4
        self.wco = [0.0] * 4
        self.state = "Idle"
        self.wire = False
        self.overflowed = False
        self.received: list[str] = []
        self._reset_modal()
        self._out.append(BANNER)

    # Transport interface -----------------------------------------------------

    def write(self, data: bytes) -> int:
        with self._cv:
            for byte in data:
                ch = bytes([byte])
                if ch == b"?":
                    self._advance()
                    self._out.append(self._status_report())
                elif ch == b"!":
                    self._feed_hold()
                elif ch == b"~":
                    self._cycle_start()
                elif ch == b"\x18":
                    self._soft_reset()
                elif ch == b"\x85":
                    self._jog_cancel()
                elif ch in (b"\n", b"\r"):  # GRBL ends a line at either, and answers "ok" to empty lines
                    line = self._partial.decode("ascii", "replace").strip()
                    self._partial.clear()
                    self._waiting.append(line)
                else:
                    self._partial += ch
            if sum(len(x) + 1 for x in self._waiting) + len(self._partial) > self.rx_size:
                self.overflowed = True
            self._advance()
            self._cv.notify_all()
        return len(data)

    def readline(self) -> bytes:
        with self._cv:
            deadline = self._clock() + self.read_timeout
            while True:
                self._advance()
                if self._out:
                    return self._out.popleft()
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return b""
                self._cv.wait(min(remaining, 0.01))

    def close(self) -> None:
        pass

    # Status -----------------------------------------------------------------

    @property
    def wpos(self) -> list[float]:
        return [m - w for m, w in zip(self.current_position(), self.wco)]

    def current_position(self) -> list[float]:
        if not self._planner or self._planner[0].target is None:
            return list(self.mpos)
        block = self._planner[0]
        now = self._hold_t0 if self._hold_t0 is not None else self._clock()
        frac = 1.0 if block.duration <= 0 else min(1.0, (now - self._block_t0) * self.speedup / block.duration)
        assert block.target is not None
        return [a + (b - a) * frac for a, b in zip(self.mpos, block.target)]

    def _status_report(self) -> bytes:
        pos = ",".join(f"{v:.3f}" for v in self.current_position())
        wco = ",".join(f"{v:.3f}" for v in self.wco)
        feed = self._planner[0].feed if self._planner else 0.0
        return f"<{self.state}|MPos:{pos}|FS:{feed:.0f},0|WCO:{wco}>\r\n".encode()

    # Real-time commands -----------------------------------------------------

    def _feed_hold(self) -> None:
        if self.state in ("Run", "Jog") and self._hold_t0 is None:
            if self.state == "Jog":
                self._jog_cancel()
                return
            self._hold_t0 = self._clock()
            self.state = "Hold"

    def _cycle_start(self) -> None:
        if self.state == "Hold" and self._hold_t0 is not None:
            now = self._clock()
            self._block_t0 += now - self._hold_t0
            self._hold_t0 = None
            self.state = "Run" if self._planner else "Idle"

    def _soft_reset(self) -> None:
        moving = self.state in ("Run", "Jog")
        self.mpos = self.current_position()
        self._planner.clear()
        self._waiting.clear()
        self._partial.clear()
        self._hold_t0 = None
        self.wire = False
        self._reset_modal()
        self.state = "Alarm" if moving else ("Alarm" if self.state == "Alarm" else "Idle")
        if moving:
            self._out.append(b"ALARM:3\r\n")
        self._out.append(b"\r\n" + BANNER)
        if self.state == "Alarm":
            self._out.append(b"[MSG:'$H'|'$X' to unlock]\r\n")

    def _jog_cancel(self) -> None:
        if self.state == "Jog":
            self.mpos = self.current_position()
            self._planner.clear()
            self.state = "Idle"

    # Motion -----------------------------------------------------------------

    def _reset_modal(self) -> None:
        self.absolute = True
        self.inverse_time = False
        self.feed = 0.0

    def _advance(self) -> None:
        if self._hold_t0 is None:
            now = self._clock()
            while self._planner:
                block = self._planner[0]
                if block.pause:  # M0: everything before it has finished; hold until cycle start
                    self._planner.popleft()
                    self._hold_t0 = self._block_t0
                    self.state = "Hold"
                    break
                end = self._block_t0 + block.duration / self.speedup
                if now < end:
                    break
                if block.target is not None:
                    self.mpos = list(block.target)
                if block.wire is not None:
                    self.wire = block.wire
                self._planner.popleft()
                self._block_t0 = end
            if not self._planner and self.state in ("Run", "Jog"):
                self.state = "Idle"
        while self._waiting and len(self._planner) < self.planner_size and self.state != "Hold":
            self._execute(self._waiting.popleft())

    def _queue(self, block: _Block) -> None:
        if not self._planner:
            self._block_t0 = self._clock()
        self._planner.append(block)
        if self.state == "Idle":
            self.state = "Jog" if block.jog else "Run"

    def _end_position(self) -> list[float]:
        for block in reversed(self._planner):
            if block.target is not None:
                return list(block.target)
        return list(self.mpos)

    def _execute(self, line: str) -> None:
        self.received.append(line)
        reply = self._process(line.upper().replace(" ", ""))
        self._out.append(reply.encode() + b"\r\n")

    def _process(self, line: str) -> str:
        if not line:
            return "ok"
        if self.state == "Alarm" and line not in ("$X", "$H") and not line.startswith("$$"):
            return "error:9"
        if line == "$X":
            if self.state == "Alarm":
                self.state = "Idle"
                self._out.append(b"[MSG:Caution: Unlocked]\r\n")
            return "ok"
        if line == "$H":
            self.state = "Idle"
            self._queue(_Block([0.0] * 4, self._distance([0.0] * 4) / RAPID_RATE * 60.0, RAPID_RATE))
            return "ok"
        if line.startswith("$J="):
            return self._motion(line[3:], jog=True)
        if line.startswith("$"):
            return "ok"
        return self._motion(line, jog=False)

    def _distance(self, target: list[float]) -> float:
        return math.dist(self._end_position(), target)

    def _motion(self, line: str, jog: bool) -> str:
        words = _WORD.findall(line)
        if "".join(w[0] + w[1] for w in words) != line:
            return "error:1"  # stray characters
        g = [float(v) for k, v in words if k == "G"]
        m = [float(v) for k, v in words if k == "M"]
        vals = {k: float(v) for k, v in words if k not in "GM"}
        absolute = self.absolute
        for code in g:
            if code == 90:
                absolute = True
            elif code == 91:
                absolute = False
            elif code == 93 and not jog:
                self.inverse_time = True
            elif code == 94:
                self.inverse_time = False
            elif code not in (0, 1, 4, 10, 17, 20, 21, 54):
                return "error:20"
        if not jog:
            self.absolute = absolute
        if "F" in vals and not (self.inverse_time and not jog):
            self.feed = vals["F"]

        for code in m:
            if code == 3:
                self._queue_wire(True)
            elif code == 5:
                self._queue_wire(False)
            elif code in (2, 30):
                self._queue_wire(False)
                self._reset_modal()
            elif code == 0:
                self._queue(_Block(None, 0.0, 0.0, pause=True))
            else:
                return "error:20"

        if 10 in g:  # G10 L20 P1: set work offset so the current position reads as the given values
            if vals.get("L") != 20:
                return "error:20"
            end = self._end_position()
            for i, letter in enumerate(self.axes):
                if letter in vals:
                    self.wco[i] = end[i] - vals[letter]
            return "ok"
        if 4 in g:
            self._queue(_Block(None, vals.get("P", 0.0), 0.0))
            return "ok"

        if not any(letter in vals for letter in self.axes):
            return "ok"
        end = self._end_position()
        target = list(end)
        for i, letter in enumerate(self.axes):
            if letter in vals:
                target[i] = vals[letter] + self.wco[i] if absolute else end[i] + vals[letter]
        dist = math.dist(end, target)
        if 0 in g and not jog:
            duration, feed = dist / RAPID_RATE * 60.0, RAPID_RATE
        elif self.inverse_time and not jog:
            if "F" not in vals or vals["F"] <= 0:
                return "error:22"
            duration, feed = 60.0 / vals["F"], dist * vals["F"]
        else:
            feed = vals["F"] if jog and "F" in vals else self.feed
            if feed <= 0:
                return "error:22"
            duration = dist / feed * 60.0
        if dist > 0:
            self._queue(_Block(target, duration, feed, jog=jog))
        return "ok"

    def _queue_wire(self, on: bool) -> None:
        if self._planner:  # takes effect after the queued motion, like GRBL's synchronized M3/M5
            self._queue(_Block(None, 0.0, 0.0, wire=on))
        else:
            self.wire = on
