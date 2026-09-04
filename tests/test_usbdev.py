"""Pipe behaviour that only shows up against real hardware."""

import pytest
import usb.core

from hisiburn.usbdev import BulkPipe, UsbError


class FakeEndpoint:
    def __init__(self, address: int, fail: bool):
        self.bEndpointAddress = address
        self.wMaxPacketSize = 512
        self._fail = fail

    def write(self, data, timeout):
        if self._fail:
            raise usb.core.USBError("Input/Output Error", errno=5)
        return len(data)

    def read(self, size, timeout):
        if self._fail:
            raise usb.core.USBError("Input/Output Error", errno=5)
        return b"\xaa\x00"


class FakeDevice:
    def __init__(self):
        self.cleared: list[int] = []

    def clear_halt(self, address):
        self.cleared.append(address)


def make_pipe(fail: bool) -> BulkPipe:
    """A BulkPipe with its enumeration skipped, so only transfers are tested."""
    pipe = BulkPipe.__new__(BulkPipe)
    pipe.device = FakeDevice()
    pipe.timeout_ms = 1000
    pipe.ep_out = FakeEndpoint(0x01, fail)
    pipe.ep_in = FakeEndpoint(0x81, fail)
    return pipe


def test_a_stalled_write_clears_the_halt_before_raising():
    # A device that does not recognise a frame stalls its endpoint. Without
    # clearing it, every later transfer fails with EIO and the real cause is
    # invisible.
    pipe = make_pipe(fail=True)
    with pytest.raises(UsbError, match="bulk write"):
        pipe.write(b"\xfa")
    assert pipe.device.cleared == [0x01, 0x81]


def test_a_stalled_read_clears_the_halt_before_raising():
    pipe = make_pipe(fail=True)
    with pytest.raises(UsbError, match="bulk read"):
        pipe.read()
    assert pipe.device.cleared == [0x01, 0x81]


def test_successful_transfers_do_not_touch_the_halt():
    pipe = make_pipe(fail=False)
    assert pipe.write(b"\xfe") == 1
    assert pipe.read() == b"\xaa\x00"
    assert pipe.device.cleared == []


def test_read_byte_takes_the_first_byte_of_a_padded_reply():
    # The device sends strlen(s) + 1 bytes, so a bare ACK arrives as AA 00.
    assert make_pipe(fail=False).read_byte() == 0xAA
