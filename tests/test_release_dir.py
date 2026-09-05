"""End-to-end against a replica of a real build's output directory.

Mirrors what `tools/build-image.sh` in martepato/openipc-hi3518ev300-wifi-setup
writes to `output/release/`: the four images under their real names, the
partition table, and the digest files.
"""

import hashlib
from pathlib import Path

import pytest

from hisiburn.cli import main
from hisiburn.flash import PlanError, build_plan, find_checksums, verify_checksums
from hisiburn.hitool_xml import layout_from_xml

FIXTURES = Path(__file__).parent / "fixtures" / "release"

#: Real sizes from a build of that repo, so padding lands on HiBurn's numbers.
IMAGES = {
    "u-boot-hi3518ev300-universal.bin": 236099,
    "env.bin": 65536,
    "uImage.hi3518ev300": 1908952,
    "rootfs.squashfs.hi3518ev300": 5693440,
}


@pytest.fixture
def release(tmp_path):
    """A directory shaped exactly like `output/release/`."""
    for name, size in IMAGES.items():
        (tmp_path / name).write_bytes(name.encode()[:1] * size)
    for table in ("usb-burn.xml", "usb-burn-rootfs-only.xml"):
        (tmp_path / table).write_bytes((FIXTURES / table).read_bytes())

    for digest_file, algorithm in (("md5sums.txt", hashlib.md5),
                                   ("sha256sums.txt", hashlib.sha256)):
        lines = []
        for name in IMAGES:
            digest = algorithm((tmp_path / name).read_bytes()).hexdigest()
            lines.append(f"{digest}  {name}")
        (tmp_path / digest_file).write_text("\n".join(lines) + "\n")
    return tmp_path


def test_flash_finds_everything_with_no_arguments_but_the_directory(capsys, release):
    assert main(["flash", "-d", str(release), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "usb-burn.xml" in out, "should pick up the build's own partition table"
    assert "u-boot-hi3518ev300-universal.bin" in out
    assert "missing" not in out


def test_the_uboot_filename_that_used_to_be_missed_is_found(release):
    # The built-in layout names OpenIPC's real filename now; the old
    # `u-boot.bin` guess made every flash fail before it started.
    from hisiburn.layout import get_layout

    plan = build_plan(get_layout("mjsxj02hl-16m"), release)
    fastboot = next(job for job in plan.jobs if job.name == "fastboot")
    assert fastboot.image_path.name == "u-boot-hi3518ev300-universal.bin"


def test_a_legacy_u_boot_bin_name_still_works(release):
    from hisiburn.layout import get_layout

    (release / "u-boot-hi3518ev300-universal.bin").rename(release / "u-boot.bin")
    plan = build_plan(get_layout("mjsxj02hl-16m"), release)
    assert next(j for j in plan.jobs if j.name == "fastboot").image_path.name == "u-boot.bin"


def test_write_lengths_match_the_captured_hiburn_session(release):
    plan = build_plan(layout_from_xml(release / "usb-burn.xml"), release)
    lengths = {job.name: job.write_length for job in plan.jobs if not job.erase_only}
    assert lengths == {
        "fastboot": 0x40000,
        "env": 0x10000,
        "kernel": 0x1E0000,
        "rootfs": 0x570000,
    }


def test_rootfs_only_table_flashes_just_the_rootfs(capsys, release):
    assert main([
        "flash", "-d", str(release),
        "--layout-file", str(release / "usb-burn-rootfs-only.xml"), "--dry-run",
    ]) == 0
    out = capsys.readouterr().out
    assert "sf erase 0x350000 0xa00000" in out
    assert "0x50000" not in out, "kernel must not be touched"


def test_checksums_are_verified(release):
    plan = build_plan(layout_from_xml(release / "usb-burn.xml"), release)
    steps = []
    verify_checksums(plan, release, steps.append)
    assert any("match sha256sums.txt" in line for line in steps)


def test_a_corrupt_image_is_caught_before_anything_is_erased(release):
    plan = build_plan(layout_from_xml(release / "usb-burn.xml"), release)
    path = release / "uImage.hi3518ev300"
    path.write_bytes(b"\x00" + path.read_bytes()[1:])
    with pytest.raises(PlanError, match="uImage.hi3518ev300"):
        verify_checksums(plan, release, lambda _: None)


def test_sha256_is_preferred_over_md5(release):
    assert find_checksums(release)[0].name == "sha256sums.txt"
    (release / "sha256sums.txt").unlink()
    assert find_checksums(release)[0].name == "md5sums.txt"


def test_no_digest_file_is_not_an_error(release):
    for name in ("md5sums.txt", "sha256sums.txt"):
        (release / name).unlink()
    plan = build_plan(layout_from_xml(release / "usb-burn.xml"), release)
    steps = []
    verify_checksums(plan, release, steps.append)
    assert any("no checksum file" in line for line in steps)


def test_no_verify_skips_the_check(capsys, release):
    path = release / "uImage.hi3518ev300"
    path.write_bytes(b"\x00" + path.read_bytes()[1:])
    assert main(["flash", "-d", str(release), "--dry-run", "--no-verify"]) == 0


def test_a_corrupt_image_stops_the_cli(capsys, release):
    path = release / "rootfs.squashfs.hi3518ev300"
    path.write_bytes(b"\x00" + path.read_bytes()[1:])
    assert main(["flash", "-d", str(release), "--dry-run"]) == 1
    assert "does not match" in capsys.readouterr().err


def test_missing_image_names_what_the_directory_holds(capsys, release):
    (release / "env.bin").unlink()
    assert main(["flash", "-d", str(release), "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "env.bin" in err
    assert "uImage.hi3518ev300" in err, "should list what is actually there"
