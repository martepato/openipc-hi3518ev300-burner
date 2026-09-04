"""Replay the captured HiBurn session and hold our output against it.

This is the closest thing to hardware-in-the-loop that runs in CI: the frames
and command text a real HiBurn 5.3 flash put on the wire, versus what this
tool would send for the same firmware and layout.
"""

import json
from pathlib import Path

import pytest

from hisiburn.agent import BurnAgent
from hisiburn.flash import build_plan, run_plan
from hisiburn.hitool_log import layout_from_log
from hisiburn.layout import get_layout

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURED = json.loads((FIXTURES / "captured_frames.json").read_text())
HIBURN_COMMANDS = [entry["text"] for entry in CAPTURED["stage2"]["commands"]]

# Sizes of the images in the captured session, so our plan pads identically.
CAPTURED_IMAGE_SIZES = {
    "u-boot.bin": 0x39A43,
    "env.bin": 65536,
    "uImage.hi3518ev300": 1908952,
    "rootfs.squashfs.hi3518ev300": 5693440,
}


@pytest.fixture
def firmware(tmp_path):
    for name, size in CAPTURED_IMAGE_SIZES.items():
        (tmp_path / name).write_bytes(b"\x00" * size)
    return tmp_path


def flash_commands(pipe, plan) -> list[str]:
    """The command text a run of ``plan`` actually puts on the wire."""
    # Replies come off one queue in the order the run consumes them.
    for job in plan.jobs:
        if job.erase_only:
            pipe.queue_command_ok()  # sf probe
            pipe.queue_command_ok()  # sf erase
            continue
        pipe.queue_command_ok()  # mw.b
        pipe.queue_ack(3)  # upload sync, header, tail
        pipe.queue_command_ok()  # sf probe
        pipe.queue_command_ok()  # sf erase
        pipe.queue_command_ok()  # sf write
    run_plan(BurnAgent(pipe), plan, on_step=lambda _: None, reset=False)
    return [frame[3:].decode() for frame in pipe.writes if frame[:1] == b"\xab"]


def test_our_commands_match_hiburn_for_every_downloaded_partition(firmware, pipe):
    """The env, kernel, rootfs and rootfs_data sequences must match verbatim."""
    plan = build_plan(get_layout("mjsxj02hl-16m"), firmware)
    ours = flash_commands(pipe, plan)

    # HiBurn's run starts with getinfo probing, which `verify_flash_chip`
    # handles separately, and writes the boot slot from the U-Boot the boot ROM
    # left in RAM. Compare from the first partition we both download.
    start = HIBURN_COMMANDS.index("mw.b 0x41000000 0xFF 0x10000")
    theirs = HIBURN_COMMANDS[start:]
    assert ours[ours.index("mw.b 0x41000000 0xFF 0x10000"):] == theirs[: -1]


def test_boot_partition_is_the_one_deliberate_difference(firmware, pipe):
    """We upload U-Boot explicitly where HiBurn reuses what is already in RAM.

    HiBurn can skip the upload because stage 1 left the image at 0x41000000.
    Sending it again costs one transfer and makes `--only boot` work on a
    camera that is already running the agent.
    """
    plan = build_plan(get_layout("mjsxj02hl-16m"), firmware, only={"boot"})
    ours = flash_commands(pipe, plan)

    assert ours[0] == "mw.b 0x41000000 0xFF 0x40000"  # ours; HiBurn omits this
    # The flash-facing commands are identical to HiBurn's.
    assert ours[1:] == [
        "sf probe 0",
        "sf erase 0x0 0x40000",
        "sf write 0x41000000 0x0 0x40000",
    ]
    assert "sf write 0x41000000 0x0 0x40000" in HIBURN_COMMANDS
    assert "sf erase 0x0 0x40000" in HIBURN_COMMANDS


def test_write_lengths_match_hiburn_for_the_captured_image_sizes(firmware, pipe):
    """Padding an image to the erase block must land on HiBurn's numbers."""
    plan = build_plan(get_layout("mjsxj02hl-16m"), firmware)
    ours = flash_commands(pipe, plan)
    for command in ours:
        if command.startswith("sf write") and not command.endswith("0x0 0x40000"):
            assert command in HIBURN_COMMANDS, f"{command!r} is not what HiBurn sent"


def test_layout_recovered_from_the_log_reproduces_the_same_commands(firmware, pipe, fixture_log):
    """A layout derived from a log drives the same flash as the built-in one."""
    from_log = build_plan(layout_from_log(fixture_log), firmware)
    builtin = build_plan(get_layout("mjsxj02hl-16m"), firmware)
    assert list(from_log.commands()) == list(builtin.commands())


def test_stage1_and_stage2_use_the_same_staging_address(firmware):
    from hisiburn.bootrom import HI3518EV300

    plan = build_plan(get_layout("mjsxj02hl-16m"), firmware)
    assert plan.layout.staging_address == HI3518EV300.uboot_address
    for head in CAPTURED["stage2"]["heads"]:
        assert head["address"] == plan.layout.staging_address
