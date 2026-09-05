"""Identifying what a firmware .bin is, before anything writes it."""

import struct

import pytest

from hisiburn.image import ERASE_BLOCK, inspect_image


def _uimage(name: str, data_size: int, load: int = 0x40008000) -> bytes:
    header = bytearray(64)
    header[0:4] = b"\x27\x05\x19\x56"
    struct.pack_into(">II", header, 12, data_size, load)
    struct.pack_into(">I", header, 16, load)
    header[28], header[29], header[30], header[31] = 5, 2, 2, 0  # Linux, ARM, kernel
    header[32 : 32 + len(name)] = name.encode()
    return bytes(header)


def _squashfs(inodes: int, bytes_used: int, block_size: int = 131072) -> bytes:
    block = bytearray(96)
    block[0:4] = b"hsqs"
    struct.pack_into("<I", block, 4, inodes)
    struct.pack_into("<I", block, 12, block_size)
    struct.pack_into("<Q", block, 40, bytes_used)
    return bytes(block)


@pytest.fixture
def factory_dump(tmp_path):
    """A 16 MiB image shaped like a real MJSXJ02HL factory dump.

    Offsets and sizes are those binwalk reported for one, so the inference
    here is checked against a layout that exists rather than an invented one.
    """
    image = bytearray(b"\xff" * (16 * 1024 * 1024))
    image[0:32] = bytes.fromhex("150500ea" + "feffffea" * 7)
    gzip_header = b"\x1f\x8b\x08\x08" + struct.pack("<I", 1575442370) + b"\x00\x03u-boot.bin\x00"
    image[0x47B0 : 0x47B0 + len(gzip_header)] = gzip_header
    image[0x40000 : 0x40000 + 64] = _uimage("Linux-4.9.37", 1916698 - 64)
    for offset, inodes, used in (
        (0x230000, 495, 3513074),
        (0x600000, 120, 3772690),
        (0x9D0000, 3, 1038625),
        (0xBC0000, 1, 162),
    ):
        image[offset : offset + 96] = _squashfs(inodes, used)
    image[0xF90000 : 0xF90000 + 12] = b"\x85\x19\x02\xe0" + b"\x00" * 8

    path = tmp_path / "factory.bin"
    path.write_bytes(bytes(image))
    return path


def test_a_full_dump_is_recognised(factory_dump):
    report = inspect_image(factory_dump)
    assert report.is_chip_sized
    assert report.starts_with_bootloader
    assert report.is_full_dump
    assert "full 16 MiB flash dump" in report.verdict


def test_every_structure_is_found(factory_dump):
    kinds = [f.kind for f in inspect_image(factory_dump).findings]
    assert kinds == ["gzip", "uImage", "squashfs", "squashfs", "squashfs", "squashfs", "jffs2"]


def test_structure_details_are_read_correctly(factory_dump):
    findings = {f.offset: f for f in inspect_image(factory_dump).findings}
    assert 'compressed "u-boot.bin"' in findings[0x47B0].description
    assert findings[0x40000].size == 1916698
    assert "Linux-4.9.37" in findings[0x40000].description
    assert findings[0x230000].size == 3513074
    assert "495 inodes" in findings[0x230000].description


def test_inferred_extents_tile_the_whole_chip(factory_dump):
    report = inspect_image(factory_dump)
    extents = report.boundaries()
    assert extents[0][0] == 0
    for (offset, length, _), (next_offset, _, _) in zip(extents, extents[1:], strict=False):
        assert offset + length == next_offset, "extents must not gap or overlap"
    last_offset, last_length, _ = extents[-1]
    assert last_offset + last_length == report.size
    assert [length // 1024 for _, length, _ in extents] == [256, 1984, 3904, 3904, 1984, 3904, 448]
    assert sum(length for _, length, _ in extents) == 16 * 1024 * 1024


def test_every_extent_lands_on_an_erase_block(factory_dump):
    for offset, length, _ in inspect_image(factory_dump).boundaries():
        assert offset % ERASE_BLOCK == 0
        assert length % ERASE_BLOCK == 0


def test_a_bare_kernel_is_not_mistaken_for_a_dump(tmp_path):
    path = tmp_path / "uImage.bin"
    path.write_bytes(_uimage("Linux-4.9.37", 1024) + b"\x00" * 1024)
    report = inspect_image(path)
    assert not report.is_full_dump
    assert "single uImage image" in report.verdict


def test_a_bare_rootfs_is_not_mistaken_for_a_dump(tmp_path):
    path = tmp_path / "rootfs.squashfs"
    path.write_bytes(_squashfs(100, 4096) + b"\x00" * 4000)
    report = inspect_image(path)
    assert not report.is_full_dump
    assert "single squashfs image" in report.verdict


def test_a_chip_sized_file_without_a_bootloader_is_flagged_not_assumed(tmp_path):
    # Exactly chip-sized, several filesystems, but nothing bootloader-shaped at
    # the start: worth a human look rather than a verbatim write from 0.
    image = bytearray(b"\xff" * (8 * 1024 * 1024))
    image[0:96] = _squashfs(10, 1024)
    image[0x400000 : 0x400000 + 96] = _squashfs(10, 1024)
    image[0x600000 : 0x600000 + 96] = _squashfs(10, 1024)
    path = tmp_path / "odd.bin"
    path.write_bytes(bytes(image))
    report = inspect_image(path)
    assert not report.is_full_dump
    assert "check before treating it as a full dump" in report.verdict


def test_an_odd_size_is_noted(tmp_path):
    path = tmp_path / "truncated.bin"
    path.write_bytes(b"\xff" * (16 * 1024 * 1024 - 4096))
    report = inspect_image(path)
    assert not report.is_chip_sized
    assert any("not a whole chip size" in note for note in report.notes)
    assert any("16 MiB" in note for note in report.notes)


def test_four_bytes_spelling_hsqs_are_not_a_filesystem(tmp_path):
    path = tmp_path / "noise.bin"
    path.write_bytes(b"hsqs" + b"\x00" * 200)
    assert not inspect_image(path).findings


def test_an_embedded_structure_is_noted_as_embedded(tmp_path):
    # A squashfs part-way into a partition is payload, not a partition start.
    image = bytearray(b"\xff" * (4 * 1024 * 1024))
    image[0x12345 : 0x12345 + 96] = _squashfs(10, 1024)
    path = tmp_path / "embedded.bin"
    path.write_bytes(bytes(image))
    assert any("not on a 64 KiB erase block" in n for n in inspect_image(path).notes)


# --- vendor update packages -------------------------------------------------


def _firmware_package(name: str, data_size: int) -> bytes:
    """A U-Boot legacy image of type "firmware", as Xiaomi's SD recovery uses."""
    import zlib

    header = bytearray(64)
    header[0:4] = b"\x27\x05\x19\x56"
    struct.pack_into(">I", header, 12, data_size)
    header[28], header[29], header[30], header[31] = 5, 2, 5, 0  # Linux, ARM, firmware
    header[32 : 32 + len(name)] = name.encode()
    struct.pack_into(">I", header, 4, zlib.crc32(bytes(header)) & 0xFFFFFFFF)
    return bytes(header)


@pytest.fixture
def sd_card_package(tmp_path):
    path = tmp_path / "hlc6.bin"
    path.write_bytes(_firmware_package("hlc6", 11067392) + b"\x5a" * 4096)
    return path


def test_a_firmware_package_is_not_a_flash_image(sd_card_package):
    report = inspect_image(sd_card_package)
    assert report.packaged_update is not None
    assert not report.is_full_dump
    assert "firmware update package" in report.verdict
    assert "SD-card recovery" in report.verdict


def test_the_package_name_is_reported(sd_card_package):
    assert '"hlc6"' in inspect_image(sd_card_package).verdict


def test_a_kernel_uimage_is_not_treated_as_a_package(tmp_path):
    path = tmp_path / "uImage"
    path.write_bytes(_uimage("Linux-4.9.37", 1024) + b"\x00" * 1024)
    report = inspect_image(path)
    assert report.packaged_update is None
    assert "OS kernel" in report.findings[0].description


def test_uimage_header_crc_is_checked(tmp_path):
    good = bytearray(_firmware_package("hlc6", 1024))
    path = tmp_path / "good.bin"
    path.write_bytes(bytes(good) + b"\x00" * 1024)
    assert "CRC MISMATCH" not in inspect_image(path).findings[0].description

    good[40] ^= 0xFF  # corrupt the name, leaving the stored CRC stale
    bad = tmp_path / "bad.bin"
    bad.write_bytes(bytes(good) + b"\x00" * 1024)
    assert "HEADER CRC MISMATCH" in inspect_image(bad).findings[0].description


def test_a_chip_sized_package_is_still_a_package(tmp_path):
    # Size alone must not promote a wrapper to a flash image.
    image = bytearray(b"\xff" * (16 * 1024 * 1024))
    image[0:64] = _firmware_package("hlc6", 11067392)
    path = tmp_path / "packaged.bin"
    path.write_bytes(bytes(image))
    report = inspect_image(path)
    assert report.is_chip_sized
    assert not report.is_full_dump
