"""Shared doubles for tests that exercise the protocol without hardware."""

from __future__ import annotations

from collections import deque

import pytest

from hisiburn.usbdev import UsbError


class FakePipe:
    """Stands in for :class:`hisiburn.usbdev.BulkPipe`.

    Records every outbound frame and hands back queued replies, so protocol
    exchanges can be asserted byte for byte.
    """

    def __init__(self, replies: list[bytes] | None = None, max_packet_size: int = 512):
        self.writes: list[bytes] = []
        self.replies: deque[bytes] = deque(replies or [])
        self.max_packet_size = max_packet_size
        self.closed = False

    def queue(self, *replies: bytes) -> None:
        self.replies.extend(replies)

    def queue_ack(self, count: int = 1) -> None:
        # The device sends strlen(s) + 1 bytes, so a bare ACK arrives padded.
        self.replies.extend([b"\xaa\x00"] * count)

    def queue_command_ok(self, output: str = "") -> None:
        self.replies.append(f"{output}[EOT](OK)\r\n".encode() + b"\x00")

    def queue_command_error(self, output: str = "") -> None:
        self.replies.append(f"{output}[EOT](ERROR)\r\n".encode() + b"\x00")

    def write(self, data: bytes, timeout_ms: int | None = None) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def read(self, length: int | None = None, timeout_ms: int | None = None) -> bytes:
        if not self.replies:
            # A real pipe raises UsbError when the device says nothing, and
            # some callers legitimately expect that (`reset` reboots the
            # camera mid-command), so model the timeout rather than asserting.
            raise UsbError("no reply queued: the device would have timed out here")
        return self.replies.popleft()

    def read_byte(self, timeout_ms: int | None = None) -> int:
        data = self.read(timeout_ms=timeout_ms)
        if not data:
            raise AssertionError("queued an empty reply where a status byte was expected")
        return data[0]

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def pipe() -> FakePipe:
    return FakePipe()


@pytest.fixture
def fixture_log(request) -> object:
    from pathlib import Path

    return Path(request.path).parent / "fixtures" / "hiburn_session.log"
