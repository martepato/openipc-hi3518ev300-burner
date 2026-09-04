"""The log parser has to reproduce a real session's partition table exactly."""

import pytest

from hisiburn.hitool_log import LogParseError, layout_from_log, parse_sessions, session_to_layout
from hisiburn.layout import get_layout

EXPECTED = [
    # name, offset, size, image
    ("boot", 0x000000, 0x040000, "u-boot.bin"),
    ("env", 0x040000, 0x010000, "env.bin"),
    ("kernel", 0x050000, 0x300000, "uImage.hi3518ev300"),
    ("rootfs", 0x350000, 0xA00000, "rootfs.squashfs.hi3518ev300"),
    ("rootfs_data", 0xD50000, 0x2B0000, None),
]


def test_session_metadata(fixture_log):
    sessions = parse_sessions(fixture_log.read_text())
    assert len(sessions) == 1
    session = sessions[0]
    assert session.flash_chip == "EN25QH128A"
    assert session.flash_size == 16 * 1024 * 1024
    assert session.uboot_version == "U-Boot 2016.11-g131d3f2"


def test_recovered_layout_matches_the_session(fixture_log):
    layout = layout_from_log(fixture_log)
    assert [(p.name, p.offset, p.size, p.image) for p in layout.partitions] == EXPECTED


def test_recovered_layout_matches_the_builtin(fixture_log):
    # The built-in table was transcribed by hand from a HiBurn session; the
    # parser reading the same session must land on the identical geometry.
    recovered = layout_from_log(fixture_log)
    builtin = get_layout("mjsxj02hl-16m")
    assert [(p.name, p.offset, p.size) for p in recovered.partitions] == [
        (p.name, p.offset, p.size) for p in builtin.partitions
    ]


def test_boot_partition_is_marked_as_written_from_ram(fixture_log):
    # The boot slot has no download of its own — HiBurn writes it from the
    # U-Boot the boot ROM already staged at 0x41000000.
    layout = layout_from_log(fixture_log)
    assert layout.get("boot").from_staged_uboot
    assert not layout.get("env").from_staged_uboot


def test_erase_only_partition_has_no_image(fixture_log):
    assert layout_from_log(fixture_log).get("rootfs_data").image is None


def test_staging_address_is_recovered(fixture_log):
    assert layout_from_log(fixture_log).staging_address == 0x41000000


def test_repeated_sessions_are_separated(fixture_log):
    doubled = fixture_log.read_text() * 2
    sessions = parse_sessions(doubled)
    assert len(sessions) == 2
    for session in sessions:
        layout = session_to_layout(session)
        assert [p.name for p in layout.partitions] == [name for name, *_ in EXPECTED]


def test_last_session_is_the_default(fixture_log, tmp_path):
    text = fixture_log.read_text()
    # Give the second session a distinguishing partition size.
    doubled = text + text.replace("sf erase 0xd50000 0x2b0000", "sf erase 0xd50000 0x2a0000")
    path = tmp_path / "two.log"
    path.write_text(doubled)
    assert layout_from_log(path).get("rootfs_data").size == 0x2A0000
    assert layout_from_log(path, session_index=0).get("rootfs_data").size == 0x2B0000


def test_unparseable_log_is_reported(tmp_path):
    path = tmp_path / "empty.log"
    path.write_text("nothing useful here\n")
    with pytest.raises(LogParseError, match="no recognisable flashing session"):
        layout_from_log(path)


def test_session_index_out_of_range(fixture_log):
    with pytest.raises(LogParseError, match="out of range"):
        layout_from_log(fixture_log, session_index=5)
