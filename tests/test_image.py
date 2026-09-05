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


def _jffs2_node(nodetype: int, body: bytes = b"") -> bytes:
    """One JFFS2 node with a valid header and header CRC.

    The CRC is not decoration: the scanner checks it, because magic plus type
    alone still matches noise on a 16 MiB image.
    """
    from hisiburn.image import jffs2_header_crc

    node = bytearray(12 + len(body))
    struct.pack_into("<HHI", node, 0, 0x1985, nodetype, len(node))
    struct.pack_into("<I", node, 8, jffs2_header_crc(bytes(node[:8])))
    node[12:] = body
    # totlen is the unpadded length, but the next node still has to start on a
    # four-byte boundary or the scanner will not look at it.
    return bytes(node) + bytes(-len(node) % 4)


def _jffs2_nodes(count: int = 6) -> bytes:
    """A run of JFFS2 nodes: a cleanmarker followed by inodes."""
    out = bytearray(_jffs2_node(0x2003))
    for _ in range(count - 1):
        out += _jffs2_node(0xE002, bytes(52))
    return bytes(out)


def _jffs2_file(name: bytes, content: bytes, ino: int, version: int = 1) -> bytes:
    """A dirent naming ``ino`` plus the data node holding its contents."""
    dirent = bytearray(28 + len(name))
    struct.pack_into("<III", dirent, 0, 1, version, ino)  # pino, version, ino
    dirent[16] = len(name)  # nsize
    dirent[17] = 8  # DT_REG
    dirent[28:] = name

    inode = bytearray(56 + len(content))
    struct.pack_into("<II", inode, 0, ino, version)  # ino, version
    struct.pack_into("<III", inode, 32, 0, len(content), len(content))  # off, csize, dsize
    inode[44] = 0  # compression: none
    inode[56:] = content

    return _jffs2_node(0xE001, bytes(dirent)) + _jffs2_node(0xE002, bytes(inode))


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
    import random

    random.seed(7)
    image = bytearray(b"\xff" * (16 * 1024 * 1024))
    image[0:32] = bytes.fromhex("150500ea" + "feffffea" * 7)
    gzip_header = b"\x1f\x8b\x08\x08" + struct.pack("<I", 1575442370) + b"\x00\x03u-boot.bin\x00"
    image[0x47B0 : 0x47B0 + len(gzip_header)] = gzip_header
    # A real boot slot is dense: a compressed U-Boot fills most of it.
    payload_start = 0x47B0 + len(gzip_header)
    image[payload_start:0x3A000] = bytes(
        random.getrandbits(8) for _ in range(0x3A000 - payload_start)
    )
    image[0x40000 : 0x40000 + 64] = _uimage("Linux-4.9.37", 1916698 - 64)
    for offset, inodes, used in (
        (0x230000, 495, 3513074),
        (0x600000, 120, 3772690),
        (0x9D0000, 3, 1038625),
        (0xBC0000, 1, 162),
    ):
        image[offset : offset + 96] = _squashfs(inodes, used)
    image[0xF90000 : 0xF90000 + len(_jffs2_nodes())] = _jffs2_nodes()

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


def test_a_run_of_jffs2_nodes_is_one_finding(factory_dump):
    jffs2 = [f for f in inspect_image(factory_dump).findings if f.kind == "jffs2"]
    assert len(jffs2) == 1, "a node run is one region, not one finding per node"
    assert jffs2[0].offset == 0xF90000
    assert jffs2[0].detail["nodes"] == 6


def test_the_two_byte_jffs2_magic_alone_is_not_enough(tmp_path):
    # 0x1985 turns up about once per 64 KiB of random data. Matching on it
    # alone reported dozens of phantom filesystems inside a real kernel and
    # squashfs payloads, which wrecked the inferred layout.
    import os

    path = tmp_path / "random.bin"
    path.write_bytes(os.urandom(4 * 1024 * 1024))
    assert not [f for f in inspect_image(path).findings if f.kind == "jffs2"]


def test_a_structure_inside_another_is_not_a_partition_start(tmp_path):
    # A squashfs payload can contain anything, including bytes that look like
    # a header. Only structures beyond the previous one's declared extent
    # start a partition.
    image = bytearray(b"\xff" * (4 * 1024 * 1024))
    image[0:32] = bytes.fromhex("150500ea" + "feffffea" * 7)
    image[0x100 : 0x100 + 14] = b"\x1f\x8b\x08\x08" + b"\x00" * 6 + b"boot\x00"
    image[0x100000 : 0x100000 + 96] = _squashfs(10, 0x200000)
    image[0x180000 : 0x180000 + 96] = _squashfs(10, 4096)  # inside the first
    path = tmp_path / "nested.bin"
    path.write_bytes(bytes(image))
    extents = inspect_image(path).boundaries()
    assert [offset for offset, _, _ in extents] == [0, 0x100000]


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


# --- comparing two images ---------------------------------------------------


def test_identical_images_compare_clean(factory_dump, tmp_path):
    from hisiburn.image import compare_images, describe_comparison

    copy = tmp_path / "copy.bin"
    copy.write_bytes(factory_dump.read_bytes())
    regions, blocks = compare_images(factory_dump, copy)
    assert regions == []
    assert blocks == 16 * 1024 * 1024 // ERASE_BLOCK
    assert "identical" in describe_comparison(factory_dump, copy)


def test_a_difference_is_reported_at_erase_block_granularity(factory_dump, tmp_path):
    from hisiburn.image import compare_images

    variant = bytearray(factory_dump.read_bytes())
    variant[0x38000] ^= 0xFF  # a single byte
    path = tmp_path / "variant.bin"
    path.write_bytes(bytes(variant))

    regions, _ = compare_images(factory_dump, path)
    # Flash erases in blocks, so one changed byte means one changed block.
    assert len(regions) == 1
    assert regions[0].offset == 0x30000
    assert regions[0].length == ERASE_BLOCK


def test_adjacent_differing_blocks_merge_into_one_region(factory_dump, tmp_path):
    from hisiburn.image import compare_images

    variant = bytearray(factory_dump.read_bytes())
    variant[0x30000:0x50000] = b"\x00" * 0x20000
    path = tmp_path / "variant.bin"
    path.write_bytes(bytes(variant))

    regions, _ = compare_images(factory_dump, path)
    assert len(regions) == 1
    assert (regions[0].offset, regions[0].length) == (0x30000, 0x20000)


def test_a_difference_is_attributed_to_its_partition(factory_dump, tmp_path):
    from hisiburn.image import describe_comparison

    variant = bytearray(factory_dump.read_bytes())
    variant[0x38000:0x38100] = b"SIGNATURE-BLOCK-" * 16
    path = tmp_path / "signed.bin"
    path.write_bytes(bytes(variant))

    text = describe_comparison(path, factory_dump)
    assert "1 differing region" in text
    assert "bootloader at 0x0000000" in text
    assert "0x0030000..0x0040000" in text


def test_differing_sizes_are_called_out(factory_dump, tmp_path):
    from hisiburn.image import describe_comparison

    short = tmp_path / "short.bin"
    short.write_bytes(factory_dump.read_bytes()[: 8 * 1024 * 1024])
    assert "sizes differ" in describe_comparison(factory_dump, short)


# --- supplying stage 1 from the dump itself ---------------------------------


def test_a_dump_can_supply_its_own_bootloader(factory_dump):
    from hisiburn.image import bootloader_from_dump

    data = factory_dump.read_bytes()
    blob = bootloader_from_dump(inspect_image(factory_dump), data)
    assert blob is not None
    assert blob == data[: len(blob)], "must be the boot slot verbatim"
    assert len(blob) >= 0x6000, "must cover the SPL window stage 1 slices"
    assert len(blob) <= 0x40000, "must not run past the boot partition"


def test_the_trailing_erased_padding_is_trimmed(factory_dump):
    from hisiburn.image import bootloader_from_dump

    data = factory_dump.read_bytes()
    blob = bootloader_from_dump(inspect_image(factory_dump), data)
    # Shorter than the whole slot, because the tail is erased flash.
    assert len(blob) < 0x40000
    # But not trimmed so hard that real bytes were lost.
    assert len(blob) >= len(data[:0x40000].rstrip(b"\xff"))


def test_no_bootloader_from_something_that_is_not_a_dump(tmp_path):
    from hisiburn.image import bootloader_from_dump

    path = tmp_path / "uImage"
    path.write_bytes(_uimage("Linux-4.9.37", 1024) + b"\x00" * 1024)
    data = path.read_bytes()
    assert bootloader_from_dump(inspect_image(path), data) is None


def test_an_almost_empty_boot_slot_is_refused(tmp_path):
    # Too small to slice an SPL out of: better to say so than to hand stage 1
    # something that cannot work.
    import random

    random.seed(3)
    image = bytearray(b"\xff" * (16 * 1024 * 1024))
    image[0:32] = bytes.fromhex("150500ea" + "feffffea" * 7)
    image[0x100 : 0x100 + 14] = b"\x1f\x8b\x08\x08" + b"\x00" * 6 + b"u\x00"
    image[0x40000 : 0x40000 + 64] = _uimage("Linux-4.9.37", 1024)
    image[0x230000 : 0x230000 + 96] = _squashfs(10, 1024)
    image[0x600000 : 0x600000 + 96] = _squashfs(10, 1024)
    path = tmp_path / "hollow.bin"
    path.write_bytes(bytes(image))

    from hisiburn.image import bootloader_from_dump

    assert bootloader_from_dump(inspect_image(path), path.read_bytes()) is None


# --- naming what is in a differing block ------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\xff" * 64, "erased flash"),
        (b"\x00" * 64, "zeroed"),
        (b"\x27\x05\x19\x56" + b"\x00" * 60, "uImage header"),
        (b"hsqs" + b"\x00" * 60, "squashfs superblock"),
        (struct.pack("<HHI", 0x1985, 0xE002, 64) + b"\x00" * 52, "JFFS2 inode node"),
        (b"\x00" * 4 + b"nothing here at all" + b"\xf1" * 41, "unrecognised content"),
    ],
)
def test_classify_block(data, expected):
    from hisiburn.image import classify_block

    assert classify_block(data) == expected


def test_a_uboot_environment_is_recognised():
    # A CRC32 followed by NUL-separated key=value text -- exactly what the
    # agent U-Boot writes at its env offset when it cannot load one.
    from hisiburn.image import classify_block

    block = b"\xd0\xbf\x03\xb7" + b"arch=arm\x00baseaddr=0x42000000\x00board=hi3518ev300\x00"
    assert classify_block(block.ljust(64, b"\x00")) == "U-Boot environment"


def test_text_without_an_assignment_is_not_an_environment():
    from hisiburn.image import classify_block

    assert classify_block(b"\x00" * 4 + b"just some plain text here" * 2) != "U-Boot environment"


def test_capabilities_come_from_the_image(tmp_path):
    """Which is the only safe place to get them: the device cannot be asked."""
    import gzip

    from hisiburn.image import inspect_uboot

    payload = (
        b"U-Boot 2016.11-g131d3f2\x00"
        b"usbtftp\x00download or upload image using USB protocol\x00"
        b"checksum calculation\x00memory display\x00memory write\x00"
        b"getinfo\x00start download process.\x00"
    )
    image = b"\x15\x05\x00\xea" + b"\xff" * 60 + gzip.compress(payload)
    report = inspect_uboot(image)
    assert report is not None
    assert report["version"].startswith("U-Boot 2016.11")
    assert report["compressed"]
    assert all(found for found, _ in report["capabilities"].values())


def test_a_build_without_usbtftp_is_reported_as_such(tmp_path):
    import gzip

    from hisiburn.image import inspect_uboot

    payload = b"U-Boot 2016.11-g131d3f2\x00checksum calculation\x00memory display\x00"
    image = b"\x15\x05\x00\xea" + b"\xff" * 60 + gzip.compress(payload)
    report = inspect_uboot(image)
    assert not report["capabilities"]["usbtftp"][0]
    assert report["capabilities"]["crc32"][0]


def test_a_non_uboot_file_reports_nothing(tmp_path):
    from hisiburn.image import inspect_uboot

    assert inspect_uboot(b"\x00" * 4096) is None


# --- reading a firmware version out of a dump -------------------------------


def _settings(files: list[tuple[bytes, bytes, int, int]]) -> bytes:
    """A settings partition: a cleanmarker, then a dirent+inode per file."""
    out = bytearray(_jffs2_node(0x2003))
    for name, content, ino, version in files:
        out += _jffs2_file(name, content, ino, version)
    return bytes(out)


def test_the_firmware_version_comes_out_of_os_release():
    from hisiburn.image import firmware_version

    report = firmware_version(
        _settings([(b"os-release", b"ISA_VERSION=4.5.6_0168\n", 8, 1)])
    )
    assert report is not None
    assert report.version == "4.5.6_0168"
    assert report.source == "os-release"


def test_app_ver_is_read_when_os_release_is_missing():
    from hisiburn.image import firmware_version

    report = firmware_version(
        _settings([(b"app.ver", b"[VER]\nappver=4.0.5_0105\n", 39, 2)])
    )
    assert report is not None
    assert report.version == "4.0.5_0105"
    assert report.source == "app.ver"


def test_the_newest_node_wins_not_the_last_one_in_the_file():
    """The point of parsing JFFS2 rather than grepping for a version string.

    A log-structured filesystem keeps every write, so a dump holds every
    version the file ever had — the real dumps behind this carry `4.0.4_0073`
    and two dozen copies of `4.0.5_0105` whatever is actually installed. Only
    the node version counters say which one is live, and they need not agree
    with the order the nodes were written down in.
    """
    from hisiburn.image import firmware_version

    report = firmware_version(
        _settings([
            (b"os-release", b"ISA_VERSION=4.0.4_0073\n", 8, 1),
            (b"os-release", b"ISA_VERSION=4.5.6_0168\n", 8, 9),  # the live one
            (b"os-release", b"ISA_VERSION=4.0.5_0105\n", 8, 4),  # written last
        ])
    )
    assert report is not None
    assert report.version == "4.5.6_0168"


def test_an_unlinked_name_is_not_reported():
    from hisiburn.image import firmware_version

    version = firmware_version(
        _settings([
            (b"os-release", b"ISA_VERSION=4.0.5_0105\n", 8, 1),
            (b"os-release", b"", 0, 7),  # ino 0: the name was deleted
        ])
    )
    assert version is None


def test_the_model_is_read_but_the_camera_credentials_are_not():
    """`.product_config` holds a cloud auth key and P2P id beside the model.

    Only the two harmless keys are read, so no summary of a dump can put
    someone's credentials into a terminal or a pasted bug report.
    """
    from hisiburn.image import firmware_version

    report = firmware_version(
        _settings([
            (b"os-release", b"ISA_VERSION=4.5.6_0168\n", 8, 1),
            (
                b".product_config",
                b"PRODUCT_TYPE=hlc6\nPRODUCT_MODEL=isa.camera.hlc6\n"
                b"KEY=s3cret\nCONFIG_INFO_AUTHKEY=alsosecret\n",
                9,
                1,
            ),
        ])
    )
    assert report is not None
    assert report.model == "isa.camera.hlc6"
    assert report.vendor == "hlc6"
    rendered = str(report)
    assert "s3cret" not in rendered and "alsosecret" not in rendered


def test_an_image_with_no_settings_partition_reports_no_version():
    from hisiburn.image import firmware_version

    assert firmware_version(b"\xff" * 4096) is None


def test_a_dump_shows_its_firmware_version_in_the_report(tmp_path):
    """So `inspect` and the `restore` preamble both say what it is."""
    image = bytearray(b"\xff" * (16 * 1024 * 1024))
    image[0x47B0 : 0x47B0 + 3] = b"\x1f\x8b\x08"
    image[0x40000 : 0x40000 + 64] = _uimage("Linux-4.9.37", 1_916_762)
    image[0x230000 : 0x230000 + 96] = _squashfs(495, 3_513_074)
    settings = _settings([(b"os-release", b"ISA_VERSION=4.5.6_0168\n", 8, 1)])
    image[0xF90000 : 0xF90000 + len(settings)] = settings

    path = tmp_path / "dump.bin"
    path.write_bytes(bytes(image))

    report = inspect_image(path)
    assert report.firmware is not None
    assert report.firmware.version == "4.5.6_0168"
    assert "firmware: 4.5.6_0168" in report.describe()
