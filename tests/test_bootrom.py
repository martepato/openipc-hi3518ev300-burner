"""Boot ROM staging, checked against the captured HiBurn session."""

import json
from pathlib import Path

import pytest

from hisiburn import protocol
from hisiburn.bootrom import (
    HI3518EV300,
    START_MAGIC,
    START_MAGIC_OFFSET,
    BootRom,
    BootRomError,
    ChipProfile,
)

CAPTURED = json.loads((Path(__file__).parent / "fixtures" / "captured_frames.json").read_text())
STAGE1 = CAPTURED["stage1"]


def test_ddr_stub_is_the_captured_one():
    assert HI3518EV300.ddr_init == bytes.fromhex(STAGE1["ddr_blob"])


def test_ddr_stub_carries_the_download_magic():
    # The stub stores this to REG_START_FLAG (0x1202013C). download_boot() in
    # the U-Boot it loads checks for exactly this before entering the download
    # loop, so a stub without it boots the camera normally instead.
    word = HI3518EV300.ddr_init[START_MAGIC_OFFSET : START_MAGIC_OFFSET + 4]
    assert int.from_bytes(word, "little") == START_MAGIC == 0x444F574E
    assert word == b"NWOD", "little-endian 'DOWN'"


def test_profile_rejects_a_stub_without_the_download_magic():
    # A UART-derived profile has a different word here; catching that at
    # construction beats a camera that silently boots instead of flashing.
    stub = bytearray(HI3518EV300.ddr_init)
    stub[START_MAGIC_OFFSET : START_MAGIC_OFFSET + 4] = (0x12345678).to_bytes(4, "little")
    with pytest.raises(ValueError, match="START_MAGIC"):
        ChipProfile(
            name="bad", ddr_init=bytes(stub), ddr_address=0, spl_address=0,
            spl_size=0x6000, uboot_address=0,
        )


def test_profile_rejects_a_wrong_sized_stub():
    with pytest.raises(ValueError, match="64 bytes"):
        ChipProfile(
            name="bad", ddr_init=b"\x00" * 32, ddr_address=0, spl_address=0,
            spl_size=0x6000, uboot_address=0,
        )


def test_profile_addresses_match_the_capture():
    lengths_and_addresses = [(h["length"], h["address"]) for h in STAGE1["heads"]]
    assert lengths_and_addresses[0] == (len(HI3518EV300.ddr_init), HI3518EV300.ddr_address)
    assert lengths_and_addresses[1] == (HI3518EV300.spl_size, HI3518EV300.spl_address)
    assert lengths_and_addresses[2][1] == HI3518EV300.uboot_address


def test_open_session_sends_the_open_frame(pipe):
    pipe.queue_ack()
    BootRom(pipe).open_session(0xCB10A08B)
    assert pipe.writes == [bytes.fromhex(STAGE1["open_frame"])]


def test_send_image_frames_a_transfer(pipe):
    pipe.queue_ack(2)  # header and tail only
    BootRom(pipe).send_image(b"\xa5" * 300, 0x04013000)

    head, data, tail = pipe.writes
    assert head == protocol.head_frame(300, 0x04013000)
    assert data == b"\xda" + b"\xa5" * 300
    assert tail == b"\xed"


def test_data_frames_are_not_individually_acknowledged(pipe):
    # The capture shows exactly two replies per image: one for the header and
    # one for the tail. Waiting on each DATA frame would deadlock.
    pipe.queue_ack(2)
    BootRom(pipe).send_image(b"\x00" * (511 * 5), 0x41000000)
    assert len(pipe.replies) == 0
    assert sum(1 for w in pipe.writes if w[0] == protocol.OP_DATA) == 5


def test_send_image_splits_at_the_frame_limit(pipe):
    pipe.queue_ack(2)
    BootRom(pipe).send_image(b"\x00" * 0x6000, 0x04010500)
    payloads = [len(w) - 1 for w in pipe.writes if w[0] == protocol.OP_DATA]
    assert payloads == STAGE1["data_frame_payload_sizes"][1 : 1 + len(payloads)]


def test_send_image_rejects_an_empty_image(pipe):
    with pytest.raises(ValueError, match="empty image"):
        BootRom(pipe).send_image(b"", 0x41000000)


def test_a_naked_header_is_resent(pipe):
    pipe.queue(b"\x55")
    pipe.queue_ack(2)
    BootRom(pipe).send_image(b"\x00" * 16, 0x41000000)
    headers = [w for w in pipe.writes if w[0] == protocol.OP_HEAD]
    assert len(headers) == 2 and headers[0] == headers[1]


def test_giving_up_reports_the_last_answer(pipe):
    pipe.queue(*[b"\x55"] * 32)
    with pytest.raises(BootRomError, match="0x55"):
        BootRom(pipe).send_image(b"\x00" * 16, 0x41000000)


def test_boot_uboot_reproduces_the_captured_sequence(pipe):
    uboot = bytes(range(256)) * 923  # 236,288 bytes, past the SPL window
    pipe.queue_ack(200)
    BootRom(pipe).boot_uboot(uboot, HI3518EV300)

    headers = [w for w in pipe.writes if w[0] == protocol.OP_HEAD and len(w) == 9]
    opens = [h for h in headers if h[1:5] == h[5:9]]
    transfers = [
        (int.from_bytes(h[1:5], "big"), int.from_bytes(h[5:9], "big"))
        for h in headers
        if h[1:5] != h[5:9]
    ]

    assert len(opens) == 1, "one session-open frame, sent first"
    assert pipe.writes[0] == opens[0]
    assert transfers == [
        (64, 0x04013000),
        (0x6000, 0x04010500),
        (len(uboot), 0x41000000),
    ]


def test_spl_is_the_front_of_the_uboot_image(pipe):
    # Verified against the capture: the SPL upload is byte-identical to the
    # first 0x6000 bytes of the U-Boot image sent afterwards.
    uboot = bytes(range(256)) * 923
    pipe.queue_ack(200)
    BootRom(pipe).boot_uboot(uboot, HI3518EV300)

    images, current = {}, None
    for frame in pipe.writes:
        if frame[0] == protocol.OP_HEAD and frame[1:5] != frame[5:9]:
            current = int.from_bytes(frame[5:9], "big")
            images[current] = bytearray()
        elif frame[0] == protocol.OP_DATA and current is not None:
            images[current] += frame[1:]

    assert bytes(images[HI3518EV300.spl_address]) == uboot[: HI3518EV300.spl_size]
    assert bytes(images[HI3518EV300.uboot_address]) == uboot
    assert bytes(images[HI3518EV300.ddr_address]) == HI3518EV300.ddr_init


def test_boot_uboot_rejects_an_image_shorter_than_the_spl_window(pipe):
    with pytest.raises(BootRomError, match="shorter than"):
        BootRom(pipe).boot_uboot(b"\x00" * 0x1000, HI3518EV300)
