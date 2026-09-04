"""Boot ROM staging, driven against a recorded pipe."""

import pytest

from hisiburn import protocol
from hisiburn.bootrom import HI3518EV300, BootRom, BootRomError, ChipProfile, detect_spl_size


def test_profile_load_addresses():
    # These match the hi3518ev300 profile OpenIPC's defib drives over UART.
    assert HI3518EV300.ddr_address == 0x04013000
    assert HI3518EV300.spl_address == 0x04010500
    assert HI3518EV300.uboot_address == 0x41000000
    assert len(HI3518EV300.ddr_init) == 64


def test_profile_rejects_a_wrong_sized_ddr_blob():
    with pytest.raises(ValueError, match="64 bytes"):
        ChipProfile(
            name="bad",
            ddr_init=b"\x00" * 32,
            ddr_address=0,
            spl_address=0,
            uboot_address=0,
            spl_max_size=0x1000,
        )


def test_send_image_frames_a_transfer(pipe):
    pipe.queue_ack(3)  # FILE, one DATA, EOT
    BootRom(pipe).send_image(b"\xa5" * 512, 0x04013000)

    file_frame, data_frame, eot_frame = pipe.writes
    assert file_frame == protocol.bootrom_file_frame(protocol.FILE_RAMINIT, 512, 0x04013000)
    assert data_frame[0] == protocol.FRAME_DATA
    assert data_frame[3:-2] == b"\xa5" * 512
    assert eot_frame[0] == protocol.FRAME_EOT


def test_send_image_splits_at_the_frame_limit(pipe):
    pipe.queue_ack(5)  # FILE, three DATA, EOT
    BootRom(pipe).send_image(b"\x00" * 2500, 0x41000000)
    payloads = [frame[3:-2] for frame in pipe.writes if frame[0] == protocol.FRAME_DATA]
    assert [len(p) for p in payloads] == [1024, 1024, 452]


def test_data_frame_sequence_increments(pipe):
    pipe.queue_ack(4)
    BootRom(pipe).send_image(b"\x00" * 2048, 0x41000000)
    sequences = [frame[1] for frame in pipe.writes if frame[0] == protocol.FRAME_DATA]
    assert sequences == [1, 2]


def test_a_naked_frame_is_resent(pipe):
    pipe.queue(b"\x55")  # NAK the header once
    pipe.queue_ack(3)
    BootRom(pipe).send_image(b"\x00" * 16, 0x41000000)
    headers = [f for f in pipe.writes if f[0] == protocol.FRAME_FILE]
    assert len(headers) == 2, "the header should have been sent twice"
    assert headers[0] == headers[1]


def test_giving_up_reports_the_last_answer(pipe):
    pipe.queue(*[b"\x55"] * 32)
    with pytest.raises(BootRomError, match="0x55"):
        BootRom(pipe).send_image(b"\x00" * 16, 0x41000000)


def test_detect_spl_size_finds_a_gzip_payload():
    uboot = bytearray(b"\x00" * 0x8000)
    uboot[0x5000:0x5003] = b"\x1f\x8b\x08"
    assert detect_spl_size(bytes(uboot), HI3518EV300) == 0x5000


def test_detect_spl_size_finds_an_lzma_payload():
    uboot = bytearray(b"\x00" * 0x8000)
    uboot[0x4800] = 0x5D
    uboot[0x4801:0x4805] = (1 << 20).to_bytes(4, "little")
    assert detect_spl_size(bytes(uboot), HI3518EV300) == 0x4800


def test_detect_spl_size_ignores_an_implausible_lzma_dictionary():
    uboot = bytearray(b"\x00" * 0x8000)
    uboot[0x4800] = 0x5D
    uboot[0x4801:0x4805] = (12345).to_bytes(4, "little")
    assert detect_spl_size(bytes(uboot), HI3518EV300) == HI3518EV300.spl_max_size


def test_detect_spl_size_falls_back_to_the_profile_maximum():
    assert detect_spl_size(b"\x00" * 0x8000, HI3518EV300) == HI3518EV300.spl_max_size


def test_boot_uboot_stages_ddr_then_spl_then_uboot(pipe):
    uboot = bytearray(b"\x00" * 0x8000)
    uboot[0x5000:0x5003] = b"\x1f\x8b\x08"
    uboot = bytes(uboot)

    pipe.queue_ack(200)
    BootRom(pipe).boot_uboot(uboot, HI3518EV300)

    headers = [f for f in pipe.writes if f[0] == protocol.FRAME_FILE and len(f) == 14]
    addresses = [int.from_bytes(f[8:12], "big") for f in headers]
    lengths = [int.from_bytes(f[4:8], "big") for f in headers]
    file_types = [f[3] for f in headers]

    assert addresses == [0x04013000, 0x04010500, 0x41000000]
    assert lengths == [64, 0x5000, len(uboot)]
    # The final image is announced as the USB payload rather than RAM init.
    assert file_types == [protocol.FILE_RAMINIT, protocol.FILE_RAMINIT, protocol.FILE_USB]
