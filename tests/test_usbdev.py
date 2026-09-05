"""Pipe behaviour that only shows up against real hardware."""

import pytest
import usb.core

from hisiburn.usbdev import BulkPipe, UsbError

STALL = usb.core.USBError("Pipe error", errno=32)  # EPIPE
TIMEOUT = usb.core.USBError("Operation timed out", errno=60)  # ETIMEDOUT


class FakeEndpoint:
    def __init__(self, address: int, fail: Exception | None):
        self.bEndpointAddress = address
        self.wMaxPacketSize = 512
        self._fail = fail

    def write(self, data, timeout):
        if self._fail:
            raise self._fail
        return len(data)

    def read(self, size, timeout):
        if self._fail:
            raise self._fail
        return b"\xaa\x00"


class FakeDevice:
    def __init__(self):
        self.cleared: list[int] = []

    def clear_halt(self, address):
        self.cleared.append(address)


def make_pipe(fail: Exception | None = None) -> BulkPipe:
    """A BulkPipe with its enumeration skipped, so only transfers are tested."""
    pipe = BulkPipe.__new__(BulkPipe)
    pipe.device = FakeDevice()
    pipe.timeout_ms = 1000
    pipe.ep_out = FakeEndpoint(0x01, fail)
    pipe.ep_in = FakeEndpoint(0x81, fail)
    return pipe


def test_a_stalled_write_clears_the_halt_before_raising():
    pipe = make_pipe(STALL)
    with pytest.raises(UsbError, match="bulk write"):
        pipe.write(b"\xfa")
    assert pipe.device.cleared == [0x01, 0x81]


def test_a_stalled_read_clears_the_halt_before_raising():
    pipe = make_pipe(STALL)
    with pytest.raises(UsbError, match="bulk read"):
        pipe.read()
    assert pipe.device.cleared == [0x01, 0x81]


def test_a_timeout_leaves_the_endpoints_alone():
    # Clearing a halt also resets the endpoint's data toggle. Doing that to a
    # healthy endpoint desynchronises host and device, and the next frames go
    # unanswered -- which is exactly what a timeout does NOT warrant. The boot
    # ROM times out by design on any opcode it does not implement.
    pipe = make_pipe(TIMEOUT)
    with pytest.raises(UsbError, match="bulk read"):
        pipe.read()
    assert pipe.device.cleared == []


def test_stall_detection():
    from hisiburn.usbdev import is_stall

    assert is_stall(STALL)
    assert not is_stall(TIMEOUT)


def test_successful_transfers_do_not_touch_the_halt():
    pipe = make_pipe()
    assert pipe.write(b"\xfe") == 1
    assert pipe.read() == b"\xaa\x00"
    assert pipe.device.cleared == []


def test_read_byte_takes_the_first_byte_of_a_padded_reply():
    # The device sends strlen(s) + 1 bytes, so a bare ACK arrives as AA 00.
    assert make_pipe().read_byte() == 0xAA
