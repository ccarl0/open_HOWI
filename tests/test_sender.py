from collections import deque

import pytest

from openhowi.sender import GrblAlarm, GrblError, GrblStreamer, clean_line


class FakeGrbl:
    """Minimal GRBL model: buffers complete lines and acknowledges one per read."""

    def __init__(self, rx_size=128, errors=(), alarm_after=None, states=("Idle",)):
        self.rx_size = rx_size
        self.errors = set(errors)
        self.alarm_after = alarm_after
        self.states = deque(states)
        self.unacked: deque[bytes] = deque()
        self.replies: deque[bytes] = deque()
        self.received: list[str] = []
        self.realtime: list[bytes] = []
        self.max_in_flight = 0
        self.empty_reads = 0

    def write(self, data: bytes):
        if data in (b"?", b"!", b"\x18"):
            self.realtime.append(data)
            if data == b"?":
                state = self.states.popleft() if len(self.states) > 1 else self.states[0]
                self.replies.append(f"<{state}|MPos:0.000,0.000,0.000,0.000|FS:0,0>\r\n".encode())
            return len(data)
        if data.strip() == b"":
            self.replies.append(b"Grbl 1.1h ['$' for help]\r\n")
            return len(data)
        assert data.endswith(b"\n")
        self.unacked.append(data)
        self.max_in_flight = max(self.max_in_flight, sum(len(d) for d in self.unacked))
        assert self.max_in_flight <= self.rx_size, "receive buffer overflow"
        return len(data)

    def readline(self) -> bytes:
        if self.replies:
            return self.replies.popleft()
        if self.unacked:
            line = self.unacked.popleft().decode().strip()
            self.received.append(line)
            if self.alarm_after is not None and len(self.received) > self.alarm_after:
                return b"ALARM:2\r\n"
            if line in self.errors:
                return b"error:20\r\n"
            return b"ok\r\n"
        self.empty_reads += 1
        if self.empty_reads > 1000:
            raise RuntimeError("streamer is waiting for a reply that will never come")
        return b""


def _streamer(fake, **kw):
    return GrblStreamer(fake, sleep=lambda s: None, **kw)


def test_clean_line():
    assert clean_line("G1 X1.5 Y2 (move) ; trailing") == "G1X1.5Y2"
    assert clean_line("(only a comment)") == ""
    assert clean_line("%") == ""
    assert clean_line("g93") == "G93"


def test_stream_respects_buffer_and_order():
    fake = FakeGrbl()
    lines = ["(header)"] + [f"G1 X{i}.123 Y{i}.456 U{i}.789 V{i}.012 F1.23456" for i in range(200)]
    progress = []
    sent = _streamer(fake).stream(lines, progress=lambda done, total: progress.append((done, total)))
    assert sent == 200
    assert fake.received == [clean_line(line) for line in lines[1:]]
    assert progress[-1] == (200, 200)
    assert fake.max_in_flight > 100  # buffer is actually kept full, not one line at a time
    assert fake.max_in_flight <= 127


def test_stream_raises_on_error_line():
    fake = FakeGrbl(errors={"G1X2"})
    with pytest.raises(GrblError, match="G1X2"):
        _streamer(fake).stream(["G1 X1", "G1 X2", "G1 X3"])


def test_stream_raises_on_alarm():
    fake = FakeGrbl(alarm_after=1)
    with pytest.raises(GrblAlarm):
        _streamer(fake).stream(["G1 X1", "G1 X2"])


def test_messages_are_passed_through():
    fake = FakeGrbl()
    seen = []
    streamer = _streamer(fake, on_message=seen.append)
    assert streamer.wake() == ["Grbl 1.1h ['$' for help]"]
    fake.replies.append(b"[MSG:Caution: Unlocked]\r\n")
    streamer.command("$X")
    assert "[MSG:Caution: Unlocked]" in seen


def test_wait_until_idle_polls_status():
    fake = FakeGrbl(states=("Run", "Run", "Idle"))
    _streamer(fake).wait_until_idle()
    assert fake.realtime.count(b"?") == 3


def test_wait_until_idle_raises_on_alarm_state():
    with pytest.raises(GrblAlarm):
        _streamer(FakeGrbl(states=("Alarm",))).wait_until_idle()


def test_abort_sends_feed_hold_then_reset():
    fake = FakeGrbl()
    _streamer(fake).abort()
    assert fake.realtime == [b"!", b"\x18"]


def test_line_longer_than_buffer_is_rejected():
    with pytest.raises(GrblError):
        _streamer(FakeGrbl(), rx_buffer_size=10).stream(["G1 X100 Y100 U100 V100"])
