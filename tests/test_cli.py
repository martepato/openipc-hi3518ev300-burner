"""End-to-end CLI paths that need no hardware."""

import pytest

from hisiburn.cli import main


@pytest.fixture
def firmware(tmp_path):
    (tmp_path / "u-boot.bin").write_bytes(b"\x00" * 0x30000)
    (tmp_path / "env.bin").write_bytes(b"\x00" * 0x10000)
    (tmp_path / "uImage.hi3518ev300").write_bytes(b"\x00" * 1908952)
    (tmp_path / "rootfs.squashfs.hi3518ev300").write_bytes(b"\x00" * 5689344)
    return tmp_path


def test_layouts_lists_the_builtin(capsys):
    assert main(["layouts"]) == 0
    assert "mjsxj02hl-16m" in capsys.readouterr().out


def test_from_log_prints_a_layout(capsys, fixture_log):
    assert main(["from-log", str(fixture_log)]) == 0
    output = capsys.readouterr().out
    assert "rootfs_data" in output
    assert '"offset": 13959168' in output  # 0xD50000, in the JSON dump


def test_from_log_writes_json_that_flash_can_load(capsys, fixture_log, tmp_path, firmware):
    layout_path = tmp_path / "layout.json"
    assert main(["from-log", str(fixture_log), "-o", str(layout_path)]) == 0
    assert main(
        ["flash", "--layout-file", str(layout_path), "-d", str(firmware), "--dry-run"]
    ) == 0
    assert "sf erase 0xd50000 0x2b0000" in capsys.readouterr().out


def test_from_log_list_shows_sessions(capsys, fixture_log):
    assert main(["from-log", str(fixture_log), "--list"]) == 0
    assert "1 session(s)" in capsys.readouterr().out


def test_dry_run_touches_no_device(capsys, firmware):
    assert main(["flash", "-d", str(firmware), "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "mw.b 0x41000000 0xFF 0x10000" in output
    assert output.rstrip().endswith("reset")


def test_missing_image_exits_with_an_error(capsys, tmp_path):
    assert main(["flash", "-d", str(tmp_path), "--dry-run"]) == 1
    assert "missing image" in capsys.readouterr().err


def test_unknown_layout_exits_with_an_error(capsys, firmware):
    assert main(["flash", "-l", "nope", "-d", str(firmware), "--dry-run"]) == 1
    assert "unknown layout" in capsys.readouterr().err


def test_bad_image_override_is_reported(capsys, firmware):
    assert main(["flash", "-d", str(firmware), "--image", "oops", "--dry-run"]) == 1
    assert "NAME=PATH" in capsys.readouterr().err


def test_boot_rejects_a_missing_file(capsys, tmp_path):
    assert main(["boot", "-f", str(tmp_path / "nope.bin")]) == 1
    assert "not found" in capsys.readouterr().err


def test_boot_rejects_an_unknown_chip(capsys, firmware):
    assert main(["boot", "-f", str(firmware / "u-boot.bin"), "-c", "hi9999"]) == 1
    assert "unknown chip" in capsys.readouterr().err
