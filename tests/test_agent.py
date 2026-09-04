"""The agent client, driven against a recorded pipe instead of a camera."""

import pytest

from hisiburn import protocol
from hisiburn.agent import (
    FLASH_TIMEOUT_FLOOR_MS,
    AgentError,
    BurnAgent,
    CommandFailed,
    flash_timeout_ms,
)


def test_ping_uses_the_open_frame_both_stages_accept(pipe):
    # The boot ROM stalls its endpoint on a START frame -- HiBurn never opens
    # with one. OPEN is what it actually sends first, and the agent takes it
    # too, so it is the only safe liveness probe before the stage is known.
    pipe.queue_ack()
    assert BurnAgent(pipe).ping()
    assert pipe.writes[0] == protocol.open_frame(
        int.from_bytes(pipe.writes[0][1:5], "big")
    )
    assert pipe.writes[0][0] == protocol.OP_HEAD
    assert pipe.writes[0][1:5] == pipe.writes[0][5:9]


def test_ping_reports_silence_rather_than_raising(pipe):
    pipe.queue(b"\x55")
    assert not BurnAgent(pipe).ping()


def test_is_agent_distinguishes_the_agent_from_the_boot_rom(pipe):
    # Both stages present identical USB descriptors, so the only way to tell
    # them apart is whether a U-Boot command comes back with an [EOT].
    pipe.queue_command_ok("version: U-Boot 2016.11-g131d3f2\r\n")
    assert BurnAgent(pipe).is_agent()


def test_is_agent_is_false_when_nothing_answers(pipe):
    assert not BurnAgent(pipe).is_agent()


def test_read_greeting_returns_the_download_banner(pipe):
    pipe.queue(b"start download process.\x00")
    assert BurnAgent(pipe).read_greeting() == "start download process."


def test_read_greeting_tolerates_silence(pipe):
    assert BurnAgent(pipe).read_greeting() is None


def test_upload_nak_is_reported(pipe):
    pipe.queue(b"\x55")
    with pytest.raises(AgentError, match="NAKed"):
        BurnAgent(pipe).upload(b"x" * 16, 0x41000000)


def test_unexpected_status_byte_is_reported(pipe):
    pipe.queue(b"\x42")
    with pytest.raises(AgentError, match="0x42"):
        BurnAgent(pipe).upload(b"x" * 16, 0x41000000)


def test_command_returns_output(pipe):
    pipe.queue_command_ok("version: U-Boot 2016.11-g131d3f2\r\n")
    agent = BurnAgent(pipe)
    assert agent.command("getinfo version") == "version: U-Boot 2016.11-g131d3f2"
    assert pipe.writes[0] == protocol.command_frame("getinfo version")


def test_command_failure_explains_the_stuck_agent(pipe):
    pipe.queue_command_error("Unknown command\r\n")
    with pytest.raises(CommandFailed, match="power-cycle"):
        BurnAgent(pipe).command("bogus")


def test_try_command_reports_failure_without_raising(pipe):
    pipe.queue_command_error()
    result = BurnAgent(pipe).try_command("bogus")
    assert not result.ok


def test_reply_split_across_packets_is_reassembled(pipe):
    pipe.queue(b"Erasing at 0x10000 -- 25% complete.")
    pipe.queue_command_ok("Erased: OK\r\n")
    assert "Erasing" in BurnAgent(pipe).command("sf erase 0x0 0x40000")


def test_repeated_commands_send_identical_frames(pipe):
    # The frame carries a length, not a sequence number, so the same command
    # is the same bytes every time -- as the capture shows.
    pipe.queue_command_ok()
    pipe.queue_command_ok()
    agent = BurnAgent(pipe)
    agent.command("sf probe 0")
    agent.command("sf probe 0")
    assert pipe.writes[0] == pipe.writes[1]


def test_upload_sends_sync_header_payload_and_tail(pipe):
    pipe.queue_ack(3)  # sync, header, then tail
    payload = b"\xa5" * 4096
    BurnAgent(pipe).upload(payload, 0x41000000)

    sync, header, body, tail = pipe.writes
    assert sync[0] == protocol.OP_START and len(sync) == 9
    assert header == protocol.head_frame(len(payload), 0x41000000)
    assert body == payload, "the agent takes uploads raw, with no DATA framing"
    assert tail == protocol.tail_frame()


def test_upload_chunks_large_images(pipe):
    pipe.queue_ack(3)
    payload = b"\x00" * (200 * 1024)
    BurnAgent(pipe).upload(payload, 0x41000000)
    body = b"".join(pipe.writes[2:-1])
    assert body == payload
    assert len(pipe.writes) > 3, "a 200 KiB image should be streamed in several writes"


def test_upload_reports_progress(pipe):
    pipe.queue_ack(3)
    seen: list[tuple[int, int]] = []
    payload = b"\x00" * (128 * 1024)
    BurnAgent(pipe).upload(payload, 0x41000000, on_progress=lambda s, t: seen.append((s, t)))
    assert seen[-1] == (len(payload), len(payload))


def test_upload_rejects_an_empty_image(pipe):
    with pytest.raises(ValueError, match="empty image"):
        BurnAgent(pipe).upload(b"", 0x41000000)


def test_upload_tail_nak_says_the_image_is_incomplete(pipe):
    pipe.queue_ack(2)
    pipe.queue(b"\x55")
    with pytest.raises(AgentError, match="did not land completely"):
        BurnAgent(pipe).upload(b"x" * 16, 0x41000000)


def test_convenience_wrappers_emit_hiburn_command_text(pipe):
    for _ in range(4):
        pipe.queue_command_ok()
    agent = BurnAgent(pipe)
    agent.memset(0x41000000, 0xFF, 0x10000)
    agent.flash_probe()
    agent.flash_erase(0x40000, 0x10000)
    agent.flash_write(0x41000000, 0x40000, 0x10000)

    sent = [frame[3:].decode() for frame in pipe.writes]
    assert sent == [
        "mw.b 0x41000000 0xFF 0x10000",
        "sf probe 0",
        "sf erase 0x40000 0x10000",
        "sf write 0x41000000 0x40000 0x10000",
    ]


def test_reset_tolerates_the_device_vanishing(pipe):
    # `reset` reboots the camera, so there is usually no reply at all.
    BurnAgent(pipe).reset()  # FakePipe raises on an unqueued read; must be swallowed


def test_flash_timeout_scales_with_size():
    assert flash_timeout_ms(0x10000) == 30_000  # floor
    assert flash_timeout_ms(10 * 1024 * 1024) > 30_000


def test_ping_never_opens_with_a_start_frame(pipe):
    # Regression: leading with 0xFA stalled a real boot ROM's endpoint, and on
    # macOS the stall then failed every later transfer with EIO.
    pipe.queue_ack()
    BurnAgent(pipe).ping()
    assert pipe.writes[0][0] != protocol.OP_START


def test_flash_timeouts_clear_the_measured_hardware_rates():
    # From the captured session: 10 MiB erase took 27.3 s, 5.44 MiB write took
    # 8.5 s. Timeouts must leave real headroom over both.
    assert flash_timeout_ms(10 * 1024 * 1024) > 27_300 * 2
    assert flash_timeout_ms(int(5.44 * 1024 * 1024)) > 8_500 * 2
    # A whole 16 MiB chip erase must not be capped by the floor.
    assert flash_timeout_ms(16 * 1024 * 1024) > FLASH_TIMEOUT_FLOOR_MS
