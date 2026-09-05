"""Shared doubles for tests that exercise the protocol without hardware."""

from __future__ import annotations

from collections import deque

import pytest

from hisiburn.usbdev import UsbError

#: Queue this instead of bytes to make the next read behave like a silent
#: device: the real pipe raises UsbError on a timeout.
TIMEOUT = object()


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

    def queue_timeout(self, count: int = 1) -> None:
        """Queue reads that behave like a device saying nothing."""
        self.replies.extend([TIMEOUT] * count)

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
        reply = self.replies.popleft()
        if reply is TIMEOUT:
            raise UsbError("queued timeout: the device said nothing")
        return reply

    def read_byte(self, timeout_ms: int | None = None) -> int:
        data = self.read(timeout_ms=timeout_ms)
        if not data:
            raise AssertionError("queued an empty reply where a status byte was expected")
        return data[0]

    def clear_halt(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    # Dunder lookup is on the type, so these have to live on the class for
    # `with BulkPipe(...) as pipe:` to work against this double.
    def __enter__(self) -> FakePipe:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


@pytest.fixture
def pipe() -> FakePipe:
    return FakePipe()


@pytest.fixture
def fixture_log(request) -> object:
    from pathlib import Path

    return Path(request.path).parent / "fixtures" / "hiburn_session.log"


@pytest.fixture
def release_dir(tmp_path):
    """A firmware directory shaped like a real build's output/release/."""
    import hashlib
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures" / "release"
    images = {
        "u-boot-hi3518ev300-universal.bin": 236099,
        "env.bin": 65536,
        "uImage.hi3518ev300": 1908952,
        "rootfs.squashfs.hi3518ev300": 5693440,
    }
    for name, size in images.items():
        (tmp_path / name).write_bytes(name.encode()[:1] * size)
    (tmp_path / "usb-burn.xml").write_bytes((fixtures / "usb-burn.xml").read_bytes())
    (tmp_path / "sha256sums.txt").write_text(
        "".join(
            f"{hashlib.sha256((tmp_path / n).read_bytes()).hexdigest()}  {n}\n"
            for n in images
        )
    )
    return tmp_path
