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


# --- finding the U-Boot to run before flashing ------------------------------


def test_uboot_is_found_from_the_layouts_bootloader_entry(release):
    from hisiburn.flash import find_uboot

    layout = layout_from_xml(release / "usb-burn.xml")
    assert find_uboot(release, layout).name == "u-boot-hi3518ev300-universal.bin"


def test_uboot_is_found_without_a_layout(release):
    from hisiburn.flash import find_uboot

    assert find_uboot(release).name == "u-boot-hi3518ev300-universal.bin"


def test_uboot_is_found_for_a_rootfs_only_layout(release):
    # The rootfs-only table names no bootloader, but the image is still there
    # and stage 1 still needs one.
    from hisiburn.flash import find_uboot

    layout = layout_from_xml(release / "usb-burn-rootfs-only.xml")
    assert find_uboot(release, layout).name == "u-boot-hi3518ev300-universal.bin"


def test_no_uboot_in_the_directory(tmp_path):
    from hisiburn.flash import find_uboot

    assert find_uboot(tmp_path) is None


def test_ambiguous_uboot_candidates_are_not_guessed_between(release):
    # Two plausible images and no layout entry to choose by: better to say
    # nothing than to flash the wrong bootloader.
    from hisiburn.flash import find_uboot

    (release / "u-boot-old.bin").write_bytes(b"\x00" * 1024)
    assert find_uboot(release) is None


# --- whole-chip restore -----------------------------------------------------


def _queue_restore(pipe, chunks: int) -> None:
    """Replies for a restore: one probe, then erase/fill/upload/write per chunk."""
    pipe.queue_command_ok()  # sf probe 0, once at the start
    for _ in range(chunks):
        pipe.queue_command_ok()  # sf erase
        pipe.queue_command_ok()  # mw.b
        pipe.queue_ack(3)  # upload sync, header, tail
        pipe.queue_command_ok()  # sf write


def test_restore_writes_the_image_in_erase_block_aligned_chunks(pipe):
    from hisiburn.agent import BurnAgent
    from hisiburn.flash import run_restore

    data = bytes(range(256)) * 4096  # 1 MiB
    chunk = 256 * 1024
    _queue_restore(pipe, chunks=len(data) // chunk)

    run_restore(BurnAgent(pipe), data, staging=0x41000000,
                on_step=lambda _: None, chunk_size=chunk, reset=False)

    commands = [f[3:].decode() for f in pipe.writes if f[:1] == b"\xab"]
    erases = [c for c in commands if c.startswith("sf erase")]
    writes = [c for c in commands if c.startswith("sf write")]
    assert erases == [f"sf erase 0x{o:x} 0x{chunk:x}" for o in range(0, len(data), chunk)]
    assert writes == [
        f"sf write 0x41000000 0x{o:x} 0x{chunk:x}" for o in range(0, len(data), chunk)
    ]


def test_restore_sends_the_image_bytes_unchanged(pipe):
    from hisiburn.agent import BurnAgent
    from hisiburn.flash import run_restore

    data = bytes(range(256)) * 1024  # 256 KiB, one chunk
    _queue_restore(pipe, chunks=1)

    run_restore(BurnAgent(pipe), data, staging=0x41000000,
                on_step=lambda _: None, chunk_size=len(data), reset=False)
    uploaded = b"".join(f for f in pipe.writes if len(f) > 512)
    assert uploaded == data


def test_restore_pads_a_short_final_chunk_to_an_erase_block(pipe):
    from hisiburn.agent import BurnAgent
    from hisiburn.flash import run_restore

    data = b"\xa5" * (64 * 1024 + 100)  # one block plus a bit
    _queue_restore(pipe, chunks=2)

    run_restore(BurnAgent(pipe), data, staging=0x41000000,
                on_step=lambda _: None, chunk_size=64 * 1024, reset=False)
    uploaded = b"".join(f for f in pipe.writes if len(f) > 512)
    # Padded up with 0xFF, which is what an erased chip reads as.
    assert len(uploaded) == 128 * 1024
    assert uploaded[: len(data)] == data
    assert set(uploaded[len(data):]) == {0xFF}


def test_restore_rejects_a_misaligned_chunk_size(pipe):
    from hisiburn.agent import BurnAgent
    from hisiburn.flash import run_restore

    with pytest.raises(PlanError, match="erase block"):
        run_restore(BurnAgent(pipe), b"x" * 1024, staging=0x41000000,
                    on_step=lambda _: None, chunk_size=1000)


def test_restore_rejects_an_empty_image(pipe):
    from hisiburn.agent import BurnAgent
    from hisiburn.flash import run_restore

    with pytest.raises(PlanError, match="empty image"):
        run_restore(BurnAgent(pipe), b"", staging=0x41000000, on_step=lambda _: None)


# --- verifying flash without reading it back --------------------------------


def test_verify_reports_a_match(pipe):
    import zlib

    from hisiburn.agent import BurnAgent
    from hisiburn.flash import verify_against_image

    data = bytes(range(256)) * 4096  # 1 MiB
    pipe.queue_command_ok()  # sf probe
    pipe.queue_command_ok()  # sf read
    pipe.queue_command_ok(f"crc32 for 41000000 ... ==> {zlib.crc32(data):08x}\r\n")

    steps = []
    assert verify_against_image(BurnAgent(pipe), data, 0x41000000, steps.append,
                                chunk_size=len(data)) == []
    assert any(": ok" in line for line in steps)


def test_verify_reports_a_mismatch_with_both_checksums(pipe):
    from hisiburn.agent import BurnAgent
    from hisiburn.flash import verify_against_image

    data = b"\xa5" * 4096
    pipe.queue_command_ok()
    pipe.queue_command_ok()
    pipe.queue_command_ok("crc32 for 41000000 ... ==> deadbeef\r\n")

    mismatches = verify_against_image(BurnAgent(pipe), data, 0x41000000,
                                      lambda _: None, chunk_size=len(data))
    assert len(mismatches) == 1
    assert mismatches[0].actual == 0xDEADBEEF
    assert mismatches[0].offset == 0


def test_verify_sends_read_then_crc_per_chunk(pipe):
    import zlib

    from hisiburn.agent import BurnAgent
    from hisiburn.flash import verify_against_image

    data = b"\x5a" * 8192
    chunk = 4096
    pipe.queue_command_ok()
    for index in range(2):
        piece = data[index * chunk : (index + 1) * chunk]
        pipe.queue_command_ok()
        pipe.queue_command_ok(f"crc32 for x ... ==> {zlib.crc32(piece):08x}\r\n")

    verify_against_image(BurnAgent(pipe), data, 0x41000000, lambda _: None,
                         chunk_size=chunk)
    sent = [f[3:].decode() for f in pipe.writes if f[:1] == b"\xab"]
    assert sent == [
        "sf probe 0",
        "sf read 0x41000000 0x0 0x1000",
        "crc32 0x41000000 0x1000",
        "sf read 0x41000000 0x1000 0x1000",
        "crc32 0x41000000 0x1000",
    ]


def test_verify_honours_a_flash_offset(pipe):
    import zlib

    from hisiburn.agent import BurnAgent
    from hisiburn.flash import verify_against_image

    data = b"\x11" * 4096
    pipe.queue_command_ok()
    pipe.queue_command_ok()
    pipe.queue_command_ok(f"crc32 for x ... ==> {zlib.crc32(data):08x}\r\n")

    verify_against_image(BurnAgent(pipe), data, 0x41000000, lambda _: None,
                         offset=0x350000, chunk_size=len(data))
    sent = [f[3:].decode() for f in pipe.writes if f[:1] == b"\xab"]
    assert "sf read 0x41000000 0x350000 0x1000" in sent


def test_a_uboot_without_crc32_is_explained(pipe):
    from hisiburn.agent import AgentError, BurnAgent

    pipe.queue_command_ok("Unknown command 'crc32' - try 'help'\r\n")
    with pytest.raises(AgentError, match="CONFIG_CMD_CRC32"):
        BurnAgent(pipe).crc32(0x41000000, 4096)


def test_verify_can_check_one_region_alone(pipe):
    """Narrowing a mismatch means checking a slice, not the whole chip."""
    import zlib

    from hisiburn.agent import BurnAgent
    from hisiburn.flash import verify_against_image

    data = bytes(range(256)) * 4096  # 1 MiB
    kernel = data[0x40000:0x50000]
    pipe.queue_command_ok()
    pipe.queue_command_ok()
    pipe.queue_command_ok(f"crc32 for x ... ==> {zlib.crc32(kernel):08x}\r\n")

    assert verify_against_image(
        BurnAgent(pipe), data, 0x41000000, lambda _: None,
        skip=0x40000, length=0x10000, chunk_size=0x10000,
    ) == []
    sent = [f[3:].decode() for f in pipe.writes if f[:1] == b"\xab"]
    assert sent == [
        "sf probe 0",
        "sf read 0x41000000 0x40000 0x10000",
        "crc32 0x41000000 0x10000",
    ]


def test_verify_slice_offsets_flash_and_image_together(pipe):
    import zlib

    from hisiburn.agent import BurnAgent
    from hisiburn.flash import verify_against_image

    data = b"\x00" * 0x10000 + b"\xa5" * 0x10000
    tail = data[0x10000:]
    pipe.queue_command_ok()
    pipe.queue_command_ok()
    pipe.queue_command_ok(f"crc32 for x ... ==> {zlib.crc32(tail):08x}\r\n")

    # A mismatch here would mean the image slice and the flash slice had
    # drifted apart, which is the whole hazard of a --skip flag.
    assert verify_against_image(
        BurnAgent(pipe), data, 0x41000000, lambda _: None,
        skip=0x10000, chunk_size=0x10000,
    ) == []


def test_verify_rejects_a_skip_past_the_end(pipe):
    from hisiburn.agent import BurnAgent
    from hisiburn.flash import verify_against_image

    with pytest.raises(PlanError, match="past the end"):
        verify_against_image(BurnAgent(pipe), b"x" * 1024, 0x41000000,
                             lambda _: None, skip=4096)


# --- reading bytes back without usbtftp -------------------------------------


def test_memory_dump_parsing_ignores_the_ascii_column():
    from hisiburn.agent import parse_memory_dump

    # The ASCII column can contain things that look like hex bytes, so the
    # parser anchors on the address prefix rather than scanning for pairs.
    text = (
        "41000000: 27 05 19 56 63 fd 39 c1 5e 98 76 aa 00 1d 40 5a    '..Vc.9.^.v...@Z\r\n"
        "41000010: 61 62 63 64 65 66 30 31 32 33 34 35 36 37 38 39    abcdef0123456789\r\n"
    )
    data = parse_memory_dump(text)
    assert len(data) == 32
    assert data[:4].hex() == "27051956"
    assert data[16:32] == b"abcdef0123456789"


def test_read_memory_stitches_several_dumps(pipe):
    from hisiburn.agent import BurnAgent

    def dump(base, payload):
        lines = []
        for i in range(0, len(payload), 16):
            row = payload[i : i + 16]
            ascii_col = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
            lines.append(f"{base + i:08x}: {row.hex(' ')}    {ascii_col}")
        return "\r\n".join(lines) + "\r\n"

    payload = bytes(range(64))
    pipe.queue_command_ok(dump(0x41000000, payload[:32]))
    pipe.queue_command_ok(dump(0x41000020, payload[32:]))

    assert BurnAgent(pipe).read_memory(0x41000000, 64) == payload
    sent = [f[3:].decode() for f in pipe.writes if f[:1] == b"\xab"]
    assert sent == ["md.b 0x41000000 0x20", "md.b 0x41000020 0x20"]


def test_read_memory_explains_a_uboot_without_md(pipe):
    from hisiburn.agent import AgentError, BurnAgent

    pipe.queue_command_ok("Unknown command 'md.b' - try 'help'\r\n")
    with pytest.raises(AgentError, match="CONFIG_CMD_MEMORY"):
        BurnAgent(pipe).read_memory(0x41000000, 16)


def test_has_command_asks_via_help(pipe):
    from hisiburn.agent import BurnAgent

    # Running the command itself tells you nothing: the device discards
    # console output on the failure path, so an unknown command and a usage
    # message both come back as a bare [EOT](ERROR).
    pipe.queue_command_ok("usbtftp - download or upload image using USB\r\n")
    assert BurnAgent(pipe).has_command("usbtftp")
    assert pipe.writes[0][3:].decode() == "help usbtftp"


def test_has_command_is_false_when_help_fails(pipe):
    from hisiburn.agent import BurnAgent

    pipe.queue_command_error()
    assert not BurnAgent(pipe).has_command("usbtftp")
