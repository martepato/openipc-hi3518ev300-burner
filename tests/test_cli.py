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
    pipe.queue_timeout()  # boot ROM sends no greeting
    pipe.queue_ack()  # answers OPEN, but nothing after that
    assert main(["probe", "-v"]) == 0
    out = capsys.readouterr().out
    assert "session open (FE): acknowledged" in out
    assert "boot ROM, waiting for a download" in out


def test_probe_identifies_a_burn_agent(capsys, pipe, monkeypatch):
    _pipe_for_probe(pipe, monkeypatch)
    pipe.queue(b"start download process.\x00")
    pipe.queue_ack()
    pipe.queue_command_ok("version: U-Boot 2016.11-g131d3f2\r\n")
    assert main(["probe", "-v"]) == 0
    out = capsys.readouterr().out
    assert "start download process." in out
    assert "burn agent, ready to flash" in out


def test_probe_reports_a_device_that_answers_nothing(capsys, pipe, monkeypatch):
    _pipe_for_probe(pipe, monkeypatch)
    pipe.queue_timeout(2)  # silent to both the greeting read and the OPEN frame
    assert main(["probe", "-v"]) == 0
    out = capsys.readouterr().out
    assert "session open (FE): no reply" in out
    assert "Power-cycle" in out
