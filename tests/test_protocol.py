"""Frame layouts, checked against the field offsets the device reads."""

import pytest

from hisiburn import protocol
from hisiburn.crc import check_crc


def test_bootrom_file_frame_layout():
    frame = protocol.bootrom_file_frame(protocol.FILE_RAMINIT, 0x40, 0x04013000)
    assert len(frame) == 14
    assert frame[0] == protocol.FRAME_FILE
    assert (frame[1], frame[2]) == (0x00, 0xFF)  # SEQ and its complement
    assert frame[3] == protocol.FILE_RAMINIT
    assert frame[4:8] == (0x40).to_bytes(4, "big")
    assert frame[8:12] == (0x04013000).to_bytes(4, "big")
    assert check_crc(frame)


def test_bootrom_file_frame_matches_defib_head_magic():
    # OpenIPC's defib drives the same boot ROM over UART and opens a transfer
    # with FE 00 FF 01. That is this frame with sequence 0 and file type 1, so
    # a change to either field would break compatibility with a known-good
    # implementation.
    frame = protocol.bootrom_file_frame(protocol.FILE_RAMINIT, 0x40, 0x04013000)
    assert frame[0:4] == bytes.fromhex("fe00ff01")


def test_bootrom_data_frame_layout():
    frame = protocol.bootrom_data_frame(b"\xaa" * 16, seq=3)
    assert frame[0] == protocol.FRAME_DATA
    assert (frame[1], frame[2]) == (0x03, 0xFC)
    assert frame[3:19] == b"\xaa" * 16
    assert check_crc(frame)


def test_bootrom_data_frame_rejects_oversized_payload():
    with pytest.raises(ValueError, match="frame limit"):
        protocol.bootrom_data_frame(b"\x00" * (protocol.MAX_DATA_LEN + 1), seq=1)


def test_bootrom_eot_frame_layout():
    frame = protocol.bootrom_eot_frame(seq=7)
    assert len(frame) == 5
    assert frame[0] == protocol.FRAME_EOT
    assert (frame[1], frame[2]) == (0x07, 0xF8)
    assert check_crc(frame)


def test_sequence_wraps_at_a_byte():
    frame = protocol.bootrom_eot_frame(seq=256)
    assert (frame[1], frame[2]) == (0x00, 0xFF)


def test_chunk_splits_to_the_frame_limit():
    pieces = protocol.chunk(b"x" * 2500)
    assert [len(p) for p in pieces] == [1024, 1024, 452]


def test_agent_head_frame_layout():
    # The device reads length from bytes 1..4 and address from bytes 5..8.
    frame = protocol.agent_head_frame(0x10000, 0x41000000)
    assert frame[0] == protocol.UHEAD
    assert frame[1:5] == (0x10000).to_bytes(4, "big")
    assert frame[5:9] == (0x41000000).to_bytes(4, "big")
    assert len(frame) == 9


def test_agent_head_frame_refuses_the_channel_open_form():
    # length == address makes the device open its output channel instead of
    # receiving data, which would silently swallow the upload.
    with pytest.raises(ValueError, match="channel-open"):
        protocol.agent_head_frame(0x1234, 0x1234)


def test_agent_open_frame_uses_equal_length_and_address():
    frame = protocol.agent_open_frame()
    assert frame[0] == protocol.UHEAD
    assert frame[1:5] == frame[5:9]


def test_agent_command_frame_puts_text_at_offset_three():
    # The device runs everything from buf+3 through run_command().
    frame = protocol.agent_command_frame("sf probe 0")
    assert frame[0] == protocol.UCMD
    assert frame[3:] == b"sf probe 0\x00"


def test_agent_command_frame_rejects_embedded_nul():
    with pytest.raises(ValueError, match="NUL"):
        protocol.agent_command_frame("sf probe\x000")


def test_parse_success_response():
    raw = b"version: U-Boot 2016.11-g131d3f2\r\n[EOT](OK)\r\n\x00\x00\x00"
    result = protocol.parse_command_response("getinfo version", raw)
    assert result.ok
    assert result.output == "version: U-Boot 2016.11-g131d3f2"
    assert bool(result) is True


def test_parse_error_response():
    result = protocol.parse_command_response("bogus", b"Unknown command\r\n[EOT](ERROR)\r\n")
    assert not result.ok
    assert "Unknown command" in result.output


def test_parse_response_with_no_output():
    result = protocol.parse_command_response("sf probe 0", b" [EOT](OK)\r\n\x00")
    assert result.ok
    assert result.output == ""


def test_parse_rejects_a_truncated_response():
    with pytest.raises(protocol.IncompleteResponse):
        protocol.parse_command_response("sf erase 0x0 0x40000", b"Erasing at 0x10000")


def test_response_completeness_check():
    assert not protocol.response_is_complete(b"Erasing at 0x10000 -- 25%")
    assert protocol.response_is_complete(b"Erasing done[EOT](OK)\r\n")
    # Padding past the terminator must not be mistaken for more output.
    assert not protocol.response_is_complete(b"\x00[EOT](OK)")
