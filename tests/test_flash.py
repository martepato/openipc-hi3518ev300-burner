"""Plan building and execution, including the checks that run before any erase."""

import pytest

from hisiburn.agent import BurnAgent
from hisiburn.flash import PlanError, build_plan, run_plan, verify_flash_chip
from hisiburn.layout import get_layout

LAYOUT = get_layout("mjsxj02hl-16m")


@pytest.fixture
def firmware(tmp_path):
    """A directory holding a plausible image for every partition."""
    (tmp_path / "u-boot.bin").write_bytes(b"\x00" * 0x30000)
    (tmp_path / "env.bin").write_bytes(b"\x00" * 0x10000)
    (tmp_path / "uImage.hi3518ev300").write_bytes(b"\x00" * 1908952)
    (tmp_path / "rootfs.squashfs.hi3518ev300").write_bytes(b"\x00" * 5689344)
    return tmp_path


def test_plan_covers_every_partition(firmware):
    plan = build_plan(LAYOUT, firmware)
    assert [job.name for job in plan.jobs] == [p.name for p in LAYOUT.partitions]


def test_write_length_is_padded_to_an_erase_block(firmware):
    plan = build_plan(LAYOUT, firmware)
    by_name = {job.name: job for job in plan.jobs}
    # The same values a real HiBurn session wrote for these image sizes.
    assert by_name["kernel"].write_length == 0x1E0000
    assert by_name["rootfs"].write_length == 0x570000
    assert by_name["env"].write_length == 0x10000


def test_partition_with_no_image_is_erase_only(firmware):
    plan = build_plan(LAYOUT, firmware)
    assert next(job for job in plan.jobs if job.name == "rootfs_data").erase_only


def test_missing_image_is_reported_before_anything_runs(tmp_path):
    (tmp_path / "env.bin").write_bytes(b"\x00" * 0x10000)
    with pytest.raises(PlanError, match="missing image"):
        build_plan(LAYOUT, tmp_path)


def test_oversized_image_is_refused(firmware):
    (firmware / "rootfs.squashfs.hi3518ev300").write_bytes(b"\x00" * (11 * 1024 * 1024))
    with pytest.raises(PlanError, match="partition holds only"):
        build_plan(LAYOUT, firmware)


def test_empty_image_is_refused(firmware):
    (firmware / "env.bin").write_bytes(b"")
    with pytest.raises(PlanError, match="is empty"):
        build_plan(LAYOUT, firmware)


def test_only_restricts_the_plan(firmware):
    plan = build_plan(LAYOUT, firmware, only={"env", "kernel"})
    assert [job.name for job in plan.jobs] == ["env", "kernel"]


def test_only_rejects_an_unknown_partition(firmware):
    with pytest.raises(PlanError, match="no partition"):
        build_plan(LAYOUT, firmware, only={"nope"})


def test_override_points_a_partition_at_another_file(firmware, tmp_path):
    other = tmp_path / "custom-env.bin"
    other.write_bytes(b"\x01" * 0x10000)
    plan = build_plan(LAYOUT, firmware, only={"env"}, overrides={"env": other})
    assert plan.jobs[0].image_path == other


def test_dry_run_commands_match_the_hiburn_sequence(firmware):
    plan = build_plan(LAYOUT, firmware, only={"env"})
    commands = list(plan.commands())
    assert commands[0] == "mw.b 0x41000000 0xFF 0x10000"
    assert commands[1].startswith("<upload env.bin")
    assert commands[2:] == [
        "sf probe 0",
        "sf erase 0x40000 0x10000",
        "sf write 0x41000000 0x40000 0x10000",
        "reset",
    ]


def test_erase_only_partition_emits_no_write(firmware):
    plan = build_plan(LAYOUT, firmware, only={"rootfs_data"})
    commands = list(plan.commands())
    assert commands == ["sf probe 0", "sf erase 0xd50000 0x2b0000", "reset"]


def _record_session(pipe, plan, **kwargs):
    """Run a plan against a fake pipe, returning the command text it sent."""
    agent = BurnAgent(pipe)
    run_plan(agent, plan, on_step=lambda _: None, **kwargs)
    return [
        frame[3:].rstrip(b"\x00").decode()
        for frame in pipe.writes
        if frame[:1] == b"\xab"
    ]


def test_run_plan_issues_the_expected_commands(firmware, pipe):
    plan = build_plan(LAYOUT, firmware, only={"env"})
    pipe.queue_command_ok()  # mw.b
    pipe.queue_ack(3)  # upload sync, header and tail
    pipe.queue_command_ok()  # sf probe
    pipe.queue_command_ok()  # sf erase
    pipe.queue_command_ok()  # sf write

    assert _record_session(pipe, plan, reset=False) == [
        "mw.b 0x41000000 0xFF 0x10000",
        "sf probe 0",
        "sf erase 0x40000 0x10000",
        "sf write 0x41000000 0x40000 0x10000",
    ]


def test_run_plan_uploads_the_image_bytes(firmware, pipe):
    plan = build_plan(LAYOUT, firmware, only={"env"})
    pipe.queue_command_ok()
    pipe.queue_ack(3)
    for _ in range(3):
        pipe.queue_command_ok()

    run_plan(BurnAgent(pipe), plan, on_step=lambda _: None, reset=False)
    uploaded = b"".join(frame for frame in pipe.writes if len(frame) > 512)
    assert len(uploaded) == 0x10000


def test_verify_flash_chip_accepts_a_matching_chip(pipe):
    pipe.queue_command_ok()  # sf probe
    pipe.queue_command_ok('Block:64KB Chip:16MB*1 \r\nName:"EN25QH128A"\r\n')
    verify_flash_chip(BurnAgent(pipe), LAYOUT, on_step=lambda _: None)


def test_verify_flash_chip_refuses_a_size_mismatch(pipe):
    pipe.queue_command_ok()
    pipe.queue_command_ok('Block:64KB Chip:8MB*1 \r\nName:"W25Q64"\r\n')
    with pytest.raises(PlanError, match="8 MiB flash chip"):
        verify_flash_chip(BurnAgent(pipe), LAYOUT, on_step=lambda _: None)


def test_verify_flash_chip_continues_when_the_size_is_unreadable(pipe):
    pipe.queue_command_ok()
    pipe.queue_command_ok("something unexpected\r\n")
    warnings = []
    verify_flash_chip(BurnAgent(pipe), LAYOUT, on_step=warnings.append)
    assert any("could not read the flash size" in line for line in warnings)
