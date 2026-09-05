"""Reading HiTool partition tables, as real build systems emit them.

The fixtures are byte-identical to what `tools/build-image.sh` in
martepato/openipc-hi3518ev300-wifi-setup writes next to its images.
"""

from pathlib import Path

import pytest

from hisiburn.hitool_xml import (
    XmlParseError,
    find_partition_table,
    layout_from_xml,
    parse_size,
)
from hisiburn.layout import get_layout

RELEASE = Path(__file__).parent / "fixtures" / "release"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0K", 0),
        ("256K", 0x40000),
        ("3072K", 0x300000),
        ("13632K", 0xD50000),
        ("16M", 0x1000000),
        ("0x40000", 0x40000),
        ("65536", 0x10000),
        (" 256 K ", 0x40000),
    ],
)
def test_parse_size(text, expected):
    assert parse_size(text) == expected


def test_parse_size_rejects_nonsense():
    with pytest.raises(XmlParseError, match="as a size"):
        parse_size("bananas")


def test_gb2312_declaration_is_handled():
    # HiTool writes encoding="GB2312". Python's expat refuses any multi-byte
    # encoding outright, so this must not go through ElementTree.parse().
    assert b'encoding="GB2312"' in (RELEASE / "usb-burn.xml").read_bytes()
    assert layout_from_xml(RELEASE / "usb-burn.xml").partitions


def test_release_table_matches_the_builtin_layout():
    # Independent confirmation: the build's own table and the layout
    # transcribed from a HiBurn session describe the same chip.
    from_xml = layout_from_xml(RELEASE / "usb-burn.xml")
    builtin = get_layout("mjsxj02hl-16m")
    assert [(p.name, p.offset, p.size) for p in from_xml.partitions] == [
        (p.name, p.offset, p.size) for p in builtin.partitions
    ]
    assert from_xml.flash_size == builtin.flash_size


def test_release_table_names_the_real_image_files():
    layout = layout_from_xml(RELEASE / "usb-burn.xml")
    assert layout.get("fastboot").image == "u-boot-hi3518ev300-universal.bin"
    assert layout.get("kernel").image == "uImage.hi3518ev300"
    assert layout.get("rootfs").image == "rootfs.squashfs.hi3518ev300"


def test_empty_select_file_means_erase_only():
    assert layout_from_xml(RELEASE / "usb-burn.xml").get("rootfs_data").image is None


def test_partial_table_still_infers_the_whole_chip():
    # A rootfs-only table stops at 13632K. Taking that as the chip size would
    # make the flash-size check reject a real 16 MiB part.
    layout = layout_from_xml(RELEASE / "usb-burn-rootfs-only.xml")
    assert [p.name for p in layout.partitions] == ["rootfs"]
    assert layout.flash_size == 16 * 1024 * 1024


def test_bootloader_from_a_table_is_not_taken_from_ram():
    # A table that names a file for the slot means that file, not whatever
    # stage 1 happened to leave at the staging address.
    assert not layout_from_xml(RELEASE / "usb-burn.xml").get("fastboot").from_staged_uboot


def test_find_partition_table_prefers_the_conventional_name():
    assert find_partition_table(RELEASE).name == "usb-burn.xml"


def test_find_partition_table_returns_none_when_there_is_none(tmp_path):
    assert find_partition_table(tmp_path) is None


def test_find_partition_table_declines_to_guess_between_several(tmp_path):
    for name in ("a-table.xml", "b-table.xml"):
        (tmp_path / name).write_bytes((RELEASE / "usb-burn.xml").read_bytes())
    assert find_partition_table(tmp_path) is None


def test_unrelated_xml_is_rejected(tmp_path):
    path = tmp_path / "notes.xml"
    path.write_text('<?xml version="1.0"?><notes><note>hello</note></notes>')
    with pytest.raises(XmlParseError, match="no <Part> elements"):
        layout_from_xml(path)
    assert find_partition_table(tmp_path) is None


def test_malformed_xml_is_reported(tmp_path):
    path = tmp_path / "broken.xml"
    path.write_text("<Partition_Info><Part ")
    with pytest.raises(XmlParseError, match="not valid XML"):
        layout_from_xml(path)


def test_part_without_a_name_is_rejected(tmp_path):
    path = tmp_path / "nameless.xml"
    path.write_text('<Partition_Info><Part Start="0K" Length="256K"/></Partition_Info>')
    with pytest.raises(XmlParseError, match="no PartitionName"):
        layout_from_xml(path)
