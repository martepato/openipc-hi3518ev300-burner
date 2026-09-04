"""The checksum must match the device's byte for byte, or every frame is NAKed."""

import pytest

from hisiburn.crc import CRC_TABLE, append_crc, check_crc, crc16

# Spot values from the crc16_table[] literal in U-Boot's
# common/download_process.c (HiSilicon). If the generated table drifts from
# the device's, frames are rejected with no useful diagnostic, so pin it.
C_TABLE_SAMPLES = {
    0: 0x0000,
    1: 0x1021,
    2: 0x2042,
    15: 0xF1EF,
    16: 0x1231,
    128: 0x9188,
    200: 0x5844,
    255: 0x1EF0,
}


@pytest.mark.parametrize(("index", "expected"), sorted(C_TABLE_SAMPLES.items()))
def test_table_matches_vendor_source(index, expected):
    assert CRC_TABLE[index] == expected


def test_table_is_complete():
    assert len(CRC_TABLE) == 256


def test_crc_of_empty_input_is_the_flush_only():
    # Two zero bytes pushed through an all-zero register leave it at zero.
    assert crc16(b"") == 0


def test_crc_is_order_sensitive():
    assert crc16(b"\x01\x02") != crc16(b"\x02\x01")


def test_append_and_check_round_trip():
    frame = append_crc(b"\xfe\x00\xff\x01\x00\x00\x10\x00\x41\x00\x00\x00")
    assert len(frame) == 14
    assert check_crc(frame)


def test_check_rejects_a_corrupted_frame():
    frame = bytearray(append_crc(b"hello world"))
    frame[3] ^= 0xFF
    assert not check_crc(bytes(frame))


def test_check_rejects_a_runt():
    assert not check_crc(b"\x01\x02")
