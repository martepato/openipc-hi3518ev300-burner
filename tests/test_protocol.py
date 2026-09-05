"""Frame layouts, asserted against bytes captured from a real HiBurn flash.

``tests/fixtures/captured_frames.json`` holds frames lifted verbatim from a
USBPcap recording of HiBurn 5.3 flashing a Hi3518EV300. Every builder here is
checked to produce the same bytes the vendor tool put on the wire.
"""

import json
from pathlib import Path

import pytest

from hisiburn import protocol

CAPTURED = json.loads((Path(__file__).parent / "fixtures" / "captured_frames.json").read_text())
STAGE1 = CAPTURED["stage1"]
STAGE2 = CAPTURED["stage2"]


def hexb(text: str) -> bytes:
    return bytes.fromhex(text)


# --- captured descriptors ---------------------------------------------------


def test_captured_device_is_the_id_we_look_for():
    from hisiburn.usbdev import PRODUCT_ID, VENDOR_ID

    descriptor = hexb(STAGE2["descriptor"]["device"])
    assert int.from_bytes(descriptor[8:10], "little") == VENDOR_ID
    assert int.from_bytes(descriptor[10:12], "little") == PRODUCT_ID


def test_captured_interface_is_vendor_specific_with_two_bulk_endpoints():
    config = hexb(STAGE2["descriptor"]["configuration"])
    interface = config[9:18]
    assert interface[5] == 0xFF, "bInterfaceClass must be vendor-specific"
    assert interface[4] == 2, "two endpoints"
    ep_in, ep_out = config[18:25], config[25:32]
    assert (ep_in[2], ep_out[2]) == (0x81, 0x01)
    assert ep_in[3] == ep_out[3] == 0x02, "both bulk"
    assert int.from_bytes(ep_out[4:6], "little") == protocol.MAX_PACKET


# --- frames shared by both stages ------------------------------------------


def test_head_frame_matches_every_captured_header():
    for head in STAGE1["heads"] + STAGE2["heads"]:
        assert protocol.head_frame(head["length"], head["address"]) == hexb(head["frame"])


def test_head_frame_is_nine_bytes_big_endian():
    frame = protocol.head_frame(0x10000, 0x41000000)
    assert len(frame) == 9
    assert frame[0] == protocol.OP_HEAD
    assert frame[1:5] == (0x10000).to_bytes(4, "big")
    assert frame[5:9] == (0x41000000).to_bytes(4, "big")


def test_head_frame_refuses_the_channel_open_shape():
    with pytest.raises(ValueError, match="channel-open"):
        protocol.head_frame(0x1234, 0x1234)


def test_open_frame_matches_the_captured_one():
    captured = hexb(STAGE1["open_frame"])
    token = int.from_bytes(captured[1:5], "big")
    assert protocol.open_frame(token) == captured


def test_open_frame_repeats_its_token():
    frame = protocol.open_frame(0xCB10A08B)
    assert frame[0] == protocol.OP_HEAD
    assert frame[1:5] == frame[5:9]
    assert len(frame) == 9


def test_start_frame_matches_the_captured_ones():
    for captured_hex in STAGE2["start_frames"]:
        captured = hexb(captured_hex)
        token = int.from_bytes(captured[1:5], "big")
        assert protocol.start_frame(token) == captured


def test_start_frame_shape():
    frame = protocol.start_frame(0x6A9B2567)
    assert frame[0] == protocol.OP_START
    assert frame[1:5] == frame[5:9]
    assert len(frame) == 9


def test_tail_frame_is_a_bare_opcode():
    assert protocol.tail_frame() == hexb(STAGE1["tail_frame"])
    assert protocol.tail_frame() == hexb(STAGE2["tail_frame"])
    assert protocol.tail_frame() == b"\xed"


# --- boot ROM data framing --------------------------------------------------


def test_data_frame_matches_the_captured_ddr_frame():
    assert protocol.data_frame(hexb(STAGE1["ddr_blob"])) == hexb(STAGE1["ddr_data_frame"])


def test_data_frame_has_no_sequence_or_checksum():
    # An earlier reading of the vendor headers assumed SEQ/~SEQ and a CRC here.
    # The capture shows the payload starting immediately after the opcode.
    payload = b"\xa5" * 64
    frame = protocol.data_frame(payload)
    assert frame == b"\xda" + payload
    assert len(frame) == len(payload) + 1


def test_captured_data_frames_never_exceed_one_max_packet():
    assert STAGE1["max_data_payload"] == protocol.MAX_DATA_PAYLOAD == 511


def test_data_frame_rejects_an_oversized_payload():
    with pytest.raises(ValueError, match="frame limit"):
        protocol.data_frame(b"\x00" * (protocol.MAX_DATA_PAYLOAD + 1))


def test_data_frame_rejects_an_empty_payload():
    with pytest.raises(ValueError, match="empty"):
        protocol.data_frame(b"")


def test_chunk_splits_at_the_frame_limit():
    pieces = protocol.chunk(b"x" * 1200)
    assert [len(p) for p in pieces] == [511, 511, 178]


def test_chunking_reproduces_the_captured_frame_sizes():
    # The SPL upload: 0x6000 bytes became 48 full frames and a 48-byte tail.
    sizes = [len(p) for p in protocol.chunk(b"\x00" * 0x6000)]
    assert sizes == [511] * 48 + [48]
    assert sizes == STAGE1["data_frame_payload_sizes"][1 : 1 + len(sizes)]


# --- burn agent commands ----------------------------------------------------


def test_command_frame_matches_every_captured_command():
    for entry in STAGE2["commands"]:
        assert protocol.command_frame(entry["text"]) == hexb(entry["frame"])


def test_command_frame_carries_a_big_endian_length_not_a_sequence():
    frame = protocol.command_frame("sf probe 0")
    assert frame[0] == protocol.OP_CMD
    assert int.from_bytes(frame[1:3], "big") == len("sf probe 0")
    assert frame[3:] == b"sf probe 0"


def test_command_frame_is_not_nul_terminated():
    # The device zeroes its receive buffer before every packet, so the
    # terminator is already in place; HiBurn sends none.
    assert not protocol.command_frame("reset").endswith(b"\x00")


def test_command_frame_rejects_embedded_nul():
    with pytest.raises(ValueError, match="NUL"):
        protocol.command_frame("sf probe\x000")


def test_captured_commands_are_the_ones_we_generate():
    texts = [entry["text"] for entry in STAGE2["commands"]]
    assert "getinfo version" in texts
    assert "sf probe 0" in texts
    assert "mw.b 0x41000000 0xFF 0x10000" in texts
    assert "sf write 0x41000000 0x40000 0x10000" in texts
    assert "reset" in texts


# --- responses --------------------------------------------------------------


def test_captured_ack_is_two_bytes():
    # usb3_bulk_in_transfer sends strlen(s) + 1, so an ACK arrives NUL-padded.
    ack = hexb(STAGE1["ack"])
    assert ack[0] == protocol.ACK
    assert len(ack) == 2


def test_parses_the_captured_responses():
    parsed = [
        protocol.parse_command_response("x", hexb(h))
        for h in STAGE2["responses"]
        if protocol.response_is_complete(hexb(h))
    ]
    assert parsed, "capture should contain at least one complete reply"
    assert all(result.ok for result in parsed)
    assert any("U-Boot 2016.11" in result.output for result in parsed)


def test_leading_space_from_the_device_is_stripped():
    result = protocol.parse_command_response("getinfo bootmode", b" spi\n[EOT](OK)\r\n\x00")
    assert result.output == "spi"


def test_parse_error_response():
    result = protocol.parse_command_response("bogus", b"Unknown command\r\n[EOT](ERROR)\r\n")
    assert not result.ok
    assert "Unknown command" in result.output


def test_parse_rejects_a_truncated_response():
    with pytest.raises(protocol.IncompleteResponse):
        protocol.parse_command_response("sf erase 0x0 0x40000", b"Erasing at 0x10000")


def test_response_completeness_check():
    assert not protocol.response_is_complete(b"Erasing at 0x10000 -- 25%")
    assert protocol.response_is_complete(b"Erasing done[EOT](OK)\r\n")
    assert not protocol.response_is_complete(b"\x00[EOT](OK)")


def test_zero_length_reply_is_not_complete():
    # Long-running commands make the device answer with empty packets until
    # the command finishes; those must not be mistaken for a reply.
    assert not protocol.response_is_complete(b"")


def test_carriage_returns_become_separate_lines():
    # The device separates progress steps with bare CRs so a terminal
    # overwrites them in place. Kept raw, they also overwrite log prefixes and
    # each other, which made a real run's debug output unreadable.
    raw = (
        b" \rErasing at 0x10000 --  25% complete."
        b"\rErasing at 0x20000 --  50% complete."
        b"\rErasing at 0x40000 -- 100% complete."
        b"\nSF: 262144 bytes @ 0x0 Erased: OK\n[EOT](OK)\r\n\x00"
    )
    result = protocol.parse_command_response("sf erase 0x0 0x40000", raw)
    assert result.ok
    assert result.output.splitlines() == [
        "Erasing at 0x10000 --  25% complete.",
        "Erasing at 0x20000 --  50% complete.",
        "Erasing at 0x40000 -- 100% complete.",
        "SF: 262144 bytes @ 0x0 Erased: OK",
    ]
    assert "\r" not in result.output


def test_output_truncated_by_the_device_buffer_still_parses():
    # The agent's reply buffer is 200 bytes, so long progress output is cut
    # mid-word -- visible in HiBurn's own logs as "Written: [EOT](OK)".
    raw = b" device 0 offset 0x0, size 0x40000\r\nSF: 262144 bytes @ 0x0 Written: [EOT](OK)\r\n\x00"
    result = protocol.parse_command_response("sf write 0x41000000 0x0 0x40000", raw)
    assert result.ok
    assert result.output.endswith("Written:")
