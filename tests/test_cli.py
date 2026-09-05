"""End-to-end CLI paths that need no hardware."""

import pytest

from hisiburn.cli import DEFAULT_WAIT, main


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


# --- argument placement -----------------------------------------------------
#
# argparse only accepts an option where it is defined. Options declared once on
# the top-level parser are rejected after the subcommand, which is how most
# people type them -- and how this project's own README documented `probe
# --verbose`. Both placements must work.


@pytest.mark.parametrize(
    "argv",
    [
        ["-v", "layouts"],
        ["layouts", "-v"],
        ["layouts", "--verbose"],
        ["--verbose", "layouts"],
    ],
)
def test_verbose_is_accepted_on_either_side_of_the_subcommand(argv):
    assert main(argv) == 0


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["-v", "probe"], {"verbose": True, "wait": DEFAULT_WAIT, "pid": None}),
        (["probe", "-v"], {"verbose": True, "wait": DEFAULT_WAIT, "pid": None}),
        (["probe"], {"verbose": False, "wait": DEFAULT_WAIT, "pid": None}),
        (["--wait", "5", "probe"], {"verbose": False, "wait": 5.0, "pid": None}),
        (["probe", "--wait", "5"], {"verbose": False, "wait": 5.0, "pid": None}),
        (["--pid", "0xd001", "info"], {"verbose": False, "wait": DEFAULT_WAIT, "pid": 0xD001}),
        (["info", "--pid", "0xd001"], {"verbose": False, "wait": DEFAULT_WAIT, "pid": 0xD001}),
        (["-v", "--wait", "3", "flash"], {"verbose": True, "wait": 3.0, "pid": None}),
        (["flash", "--verbose", "--wait", "3"], {"verbose": True, "wait": 3.0, "pid": None}),
        (["-v", "boot", "-f", "x.bin", "--wait", "9"],
         {"verbose": True, "wait": 9.0, "pid": None}),
    ],
)
def test_shared_options_parse_the_same_in_both_positions(argv, expected):
    from hisiburn.cli import SHARED_DEFAULTS, build_parser

    args = build_parser().parse_args(argv)
    for name, default in SHARED_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)
    assert {name: getattr(args, name) for name in expected} == expected


def test_a_value_given_before_the_subcommand_survives_it():
    # `parents=` shares one action object across parsers, so a default set on
    # the top-level parser would also land on the subparser copies and wipe
    # this out. Defaults are applied after parsing to avoid exactly that.
    from hisiburn.cli import build_parser

    args = build_parser().parse_args(["-v", "--wait", "7", "probe"])
    assert args.verbose is True
    assert args.wait == 7.0


def test_subcommands_that_touch_no_device_reject_device_options():
    from hisiburn.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["layouts", "--pid", "0xd001"])


# --- missing libusb ---------------------------------------------------------


def test_missing_libusb_is_explained_rather_than_traced(capsys, monkeypatch):
    import usb.core

    def no_backend(*args, **kwargs):
        raise usb.core.NoBackendError("No backend available")

    monkeypatch.setattr(usb.core, "find", no_backend)
    assert main(["probe"]) == 1
    err = capsys.readouterr().err
    assert "brew install libusb" in err
    assert "not a problem with the camera" in err


def test_probe_reports_nothing_found(capsys, monkeypatch):
    monkeypatch.setattr("hisiburn.cli.list_devices", lambda: [])
    # --wait 0 opts out of the default wait so the test does not sleep.
    assert main(["probe", "--wait", "0"]) == 1
    out = capsys.readouterr().out
    assert "No HiSilicon USB device found" in out
    assert "hold the reset button" in out


def test_device_commands_wait_by_default():
    # Getting a camera into download mode takes both hands and a few seconds,
    # which nobody can do before the command starts.
    from hisiburn.cli import build_parser

    for argv in (["probe"], ["info"], ["flash"], ["boot", "-f", "x.bin"]):
        args = build_parser().parse_args(argv)
        assert not hasattr(args, "wait") or args.wait == DEFAULT_WAIT
    assert DEFAULT_WAIT == 30.0


def test_probe_waits_for_a_device_to_appear(capsys, monkeypatch):
    from hisiburn.usbdev import FoundDevice

    device = FoundDevice(
        vendor_id=0x12D1, product_id=0xD001, bus=20, address=7,
        manufacturer="Hisilicon", product="HiUSBBurn",
    )
    calls = {"n": 0}

    def appears_on_third_scan():
        calls["n"] += 1
        return [device] if calls["n"] >= 3 else []

    monkeypatch.setattr("hisiburn.cli.list_devices", appears_on_third_scan)
    monkeypatch.setattr("hisiburn.cli.time.sleep", lambda _: None)
    assert main(["probe", "--wait", "5"]) == 0
    assert "12d1:d001" in capsys.readouterr().out


def test_probe_filters_by_pid(capsys, monkeypatch):
    from hisiburn.usbdev import FoundDevice

    devices = [
        FoundDevice(0x12D1, 0xD001, 20, 7, "Hisilicon", "HiUSBBurn"),
        FoundDevice(0x12D1, 0x1234, 20, 8, "Hisilicon", "Something else"),
    ]
    monkeypatch.setattr("hisiburn.cli.list_devices", lambda: devices)
    assert main(["probe", "--pid", "0xd001"]) == 0
    out = capsys.readouterr().out
    assert "12d1:d001" in out
    assert "12d1:1234" not in out


# --- probe diagnosis --------------------------------------------------------


def _pipe_for_probe(pipe, monkeypatch):
    """Point probe --verbose at a fake pipe instead of real hardware."""
    from hisiburn.usbdev import FoundDevice

    device = FoundDevice(0x12D1, 0xD001, 2, 6, "Hislicon", "HiUSBBurn")
    pipe.info = device
    pipe.ep_out = type("E", (), {"bEndpointAddress": 0x01, "wMaxPacketSize": 512})()
    pipe.ep_in = type("E", (), {"bEndpointAddress": 0x81, "wMaxPacketSize": 512})()
    monkeypatch.setattr("hisiburn.cli.list_devices", lambda: [device])
    monkeypatch.setattr("hisiburn.cli.find_device", lambda pid: object())
    monkeypatch.setattr("hisiburn.cli.BulkPipe", lambda dev: pipe)
    return pipe


def test_probe_identifies_a_boot_rom(capsys, pipe, monkeypatch):
    _pipe_for_probe(pipe, monkeypatch)
    pipe.queue_timeout(2)  # boot ROM sends no greeting, on either attempt
    pipe.queue_ack()  # but it does acknowledge an OPEN frame
    assert main(["probe", "-v"]) == 0
    out = capsys.readouterr().out
    assert "greeting: none" in out
    assert "session open (FE): acknowledged" in out
    assert "boot ROM, waiting for a download" in out


def test_probe_never_sends_a_command_to_a_boot_rom(pipe, monkeypatch):
    _pipe_for_probe(pipe, monkeypatch)
    pipe.queue_timeout(2)
    pipe.queue_ack()
    main(["probe", "-v"])
    assert not any(frame[:1] == b"\xab" for frame in pipe.writes)


def test_probe_identifies_a_burn_agent(capsys, pipe, monkeypatch):
    _pipe_for_probe(pipe, monkeypatch)
    pipe.be_an_agent()
    pipe.queue_command_ok("version: U-Boot 2016.11-g131d3f2\r\n")
    assert main(["probe", "-v"]) == 0
    out = capsys.readouterr().out
    assert "start download process." in out
    assert "U-Boot 2016.11-g131d3f2" in out
    assert "burn agent, ready to flash" in out


def test_probe_reports_a_device_that_answers_nothing(capsys, pipe, monkeypatch):
    _pipe_for_probe(pipe, monkeypatch)
    pipe.queue_timeout(3)  # silent to the greeting reads and the OPEN frame
    assert main(["probe", "-v"]) == 0
    out = capsys.readouterr().out
    assert "session open (FE): no reply" in out
    assert "Power-cycle" in out


# --- bringing the agent up automatically ------------------------------------


def _agent_pipe(monkeypatch, stages):
    """Serve a sequence of fake pipes from BulkPipe/find_device.

    Each entry is a FakePipe; they are handed out in order as the code opens
    the device, re-enumerates, and opens it again.
    """
    from hisiburn.usbdev import FoundDevice

    handed = iter(stages)
    device = FoundDevice(0x12D1, 0xD001, 2, 6, "Hislicon", "HiUSBBurn")
    monkeypatch.setattr("hisiburn.cli.find_device", lambda *a, **k: object())
    monkeypatch.setattr("hisiburn.cli.wait_for_device", lambda *a, **k: object())
    monkeypatch.setattr("hisiburn.cli.BulkPipe", lambda dev: next(handed))
    for pipe in stages:
        pipe.info = device
    return stages


def _queue_erase_only_flash(pipe):
    """Replies for `--only rootfs_data`: chip check, then probe and erase."""
    pipe.queue_command_ok()  # verify_flash_chip: sf probe 0
    pipe.queue_command_ok('Block:64KB Chip:16MB*1 \r\nName:"EN25QH128A"\r\n')
    pipe.queue_command_ok()  # run_plan: sf probe 0
    pipe.queue_command_ok()  # run_plan: sf erase
    # `reset` gets no reply, which is what a rebooting camera does.


def test_flash_starts_the_agent_itself_when_it_finds_the_boot_rom(
    capsys, release_dir, monkeypatch
):
    """The image stage 1 needs is the one being flashed; don't make users do it."""
    from conftest import FakePipe

    bootrom, agent_pipe = FakePipe(), FakePipe()
    _agent_pipe(monkeypatch, [bootrom, agent_pipe])

    # Boot ROM: no banner, on either attempt.
    bootrom.queue_timeout(2)
    # Stage 1: an ACK for the session open, then a header and tail per image.
    bootrom.queue_ack(7)
    # The agent that comes up afterwards.
    agent_pipe.be_an_agent()
    _queue_erase_only_flash(agent_pipe)

    code = main(["flash", "-d", str(release_dir), "-y", "--only", "rootfs_data"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Boot ROM is listening" in out
    assert "u-boot-hi3518ev300-universal.bin" in out
    assert "start download process." in out
    # Stage 1 really ran: three images went across as DATA frames.
    assert any(frame[:1] == b"\xda" for frame in bootrom.writes)


def test_flash_says_what_to_do_when_no_uboot_can_be_found(
    capsys, release_dir, monkeypatch
):
    from conftest import FakePipe

    bootrom = FakePipe()
    _agent_pipe(monkeypatch, [bootrom])
    bootrom.queue_timeout(2)
    (release_dir / "u-boot-hi3518ev300-universal.bin").unlink()

    assert main([
        "flash", "-d", str(release_dir), "-y", "--only", "rootfs_data", "--no-verify",
    ]) == 1
    assert "--uboot PATH" in capsys.readouterr().err


def test_flash_does_not_boot_when_the_agent_is_already_up(capsys, release_dir, monkeypatch):
    from conftest import FakePipe

    agent_pipe = FakePipe()
    _agent_pipe(monkeypatch, [agent_pipe])
    agent_pipe.be_an_agent()
    _queue_erase_only_flash(agent_pipe)

    assert main(["flash", "-d", str(release_dir), "-y", "--only", "rootfs_data"]) == 0
    out = capsys.readouterr().out
    assert "Boot ROM is listening" not in out
    assert not any(frame[:1] == b"\xda" for frame in agent_pipe.writes), "no stage 1"


# --- restore finding a loader for stage 1 -----------------------------------


def _dump_with_bootloader(tmp_path, name="factory.bin"):
    """A 16 MiB dump whose boot slot holds a usable bootloader."""
    import random
    import struct

    random.seed(11)
    image = bytearray(b"\xff" * (16 * 1024 * 1024))
    image[0:32] = bytes.fromhex("150500ea" + "feffffea" * 7)
    header = b"\x1f\x8b\x08\x08" + struct.pack("<I", 0) + b"\x00\x03u-boot.bin\x00"
    image[0x47B0 : 0x47B0 + len(header)] = header
    start = 0x47B0 + len(header)
    image[start:0x3A000] = bytes(random.getrandbits(8) for _ in range(0x3A000 - start))

    uimage = bytearray(64)
    uimage[0:4] = b"\x27\x05\x19\x56"
    struct.pack_into(">II", uimage, 12, 1024, 0x40008000)
    uimage[28], uimage[29], uimage[30] = 5, 2, 2
    image[0x40000 : 0x40000 + 64] = bytes(uimage)

    for offset in (0x230000, 0x600000, 0x9D0000):
        block = bytearray(96)
        block[0:4] = b"hsqs"
        struct.pack_into("<I", block, 4, 10)
        struct.pack_into("<I", block, 12, 131072)
        struct.pack_into("<Q", block, 40, 4096)
        image[offset : offset + 96] = bytes(block)

    path = tmp_path / name
    path.write_bytes(bytes(image))
    return path


def test_restore_uses_the_bootloader_inside_the_dump(capsys, tmp_path, monkeypatch):
    """A lone dump has no firmware directory, but it carries its own loader."""
    from conftest import FakePipe

    dump = _dump_with_bootloader(tmp_path)
    bootrom, agent_pipe = FakePipe(), FakePipe()
    _agent_pipe(monkeypatch, [bootrom, agent_pipe])

    bootrom.queue_timeout(2)  # boot ROM: no banner
    bootrom.queue_ack(7)  # session open, then a header and tail per image
    agent_pipe.be_an_agent()
    agent_pipe.queue_command_ok('Block:64KB Chip:16MB*1 \r\nName:"EN25QH128A"\r\n')
    agent_pipe.queue_command_ok()  # run_restore's sf probe 0
    for _ in range(4):  # 16 MiB in 4 MiB chunks
        agent_pipe.queue_command_ok()  # sf erase
        agent_pipe.queue_command_ok()  # mw.b
        agent_pipe.queue_ack(3)  # upload
        agent_pipe.queue_command_ok()  # sf write

    assert main(["restore", str(dump), "-y", "--no-reset"]) == 0
    out = capsys.readouterr().out
    assert "using the bootloader from the image itself" in out
    assert any(frame[:1] == b"\xda" for frame in bootrom.writes), "stage 1 ran"


def test_restore_prefers_a_uboot_beside_the_image(capsys, tmp_path, monkeypatch):
    from conftest import FakePipe

    dump = _dump_with_bootloader(tmp_path)
    (tmp_path / "u-boot-hi3518ev300-universal.bin").write_bytes(b"\xa5" * 0x39A43)

    bootrom = FakePipe()
    _agent_pipe(monkeypatch, [bootrom])
    bootrom.queue_timeout(2)
    bootrom.queue(b"\x55")  # refuse stage 1, so the run stops after the choice

    main(["restore", str(dump), "-y", "--no-reset"])
    out = capsys.readouterr().out
    assert "u-boot-hi3518ev300-universal.bin" in out
    assert "from the image itself" not in out


def test_restore_explicit_uboot_wins(capsys, tmp_path, monkeypatch):
    from conftest import FakePipe

    dump = _dump_with_bootloader(tmp_path)
    (tmp_path / "u-boot-hi3518ev300-universal.bin").write_bytes(b"\xa5" * 0x39A43)
    chosen = tmp_path / "my-uboot.bin"
    chosen.write_bytes(b"\x5a" * 0x39A43)

    bootrom = FakePipe()
    _agent_pipe(monkeypatch, [bootrom])
    bootrom.queue_timeout(2)
    bootrom.queue(b"\x55")

    main(["restore", str(dump), "-y", "--no-reset", "--uboot", str(chosen)])
    assert "my-uboot.bin" in capsys.readouterr().out


def test_uboot_comes_from_the_environment_when_not_given(tmp_path, monkeypatch):
    from hisiburn.cli import UBOOT_ENV_VAR, resolve_uboot

    image = tmp_path / "u-boot-hi3518ev300-universal.bin"
    image.write_bytes(b"\xa5" * 1024)
    monkeypatch.setenv(UBOOT_ENV_VAR, str(image))
    resolved = resolve_uboot(None)
    assert resolved is not None and resolved.label == image.name


def test_an_explicit_uboot_beats_the_environment(tmp_path, monkeypatch):
    from hisiburn.cli import UBOOT_ENV_VAR, resolve_uboot

    (tmp_path / "env-uboot.bin").write_bytes(b"\x00" * 16)
    chosen = tmp_path / "u-boot.bin"
    chosen.write_bytes(b"\x11" * 16)
    monkeypatch.setenv(UBOOT_ENV_VAR, str(tmp_path / "env-uboot.bin"))
    assert resolve_uboot(str(chosen)).label == "u-boot.bin"


def test_uboot_is_found_in_the_working_directory(tmp_path, monkeypatch):
    from hisiburn.cli import UBOOT_ENV_VAR, resolve_uboot

    monkeypatch.delenv(UBOOT_ENV_VAR, raising=False)
    (tmp_path / "u-boot-hi3518ev300-universal.bin").write_bytes(b"\xa5" * 1024)
    monkeypatch.chdir(tmp_path)
    assert resolve_uboot(None) is not None


def test_no_uboot_anywhere_is_not_an_error_here(tmp_path, monkeypatch):
    from hisiburn.cli import UBOOT_ENV_VAR, resolve_uboot

    monkeypatch.delenv(UBOOT_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    assert resolve_uboot(None) is None


def test_the_missing_uboot_message_names_the_env_var(capsys, pipe, monkeypatch, tmp_path):
    from hisiburn.cli import UBOOT_ENV_VAR

    monkeypatch.delenv(UBOOT_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)
    _pipe_for_probe(pipe, monkeypatch)
    pipe.queue_timeout(4)
    assert main(["run", "usbtftp"]) == 1
    assert UBOOT_ENV_VAR in capsys.readouterr().err
