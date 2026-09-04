"""The agent client, driven against a recorded pipe instead of a camera."""

import pytest

from hisiburn import protocol
from hisiburn.agent import AgentError, BurnAgent, CommandFailed, flash_timeout_ms


def test_ping_accepts_an_ack(pipe):
    pipe.queue_ack()
    assert BurnAgent(pipe).ping()
    assert pipe.writes == [protocol.agent_start_frame()]


def test_ping_reports_silence_rather_than_raising(pipe):
    pipe.queue(b"\x55")
    assert not BurnAgent(pipe).ping()


def test_open_channel_sends_the_equal_length_form(pipe):
    pipe.queue_ack()
    BurnAgent(pipe).open_channel()
    frame = pipe.writes[0]
    assert frame[0] == protocol.UHEAD
    assert frame[1:5] == frame[5:9]


def test_open_channel_raises_on_nak(pipe):
    pipe.queue(b"\x55")
    with pytest.raises(AgentError, match="NAKed"):
        BurnAgent(pipe).open_channel()


def test_unexpected_status_byte_is_reported(pipe):
    pipe.queue(b"\x42")
    with pytest.raises(AgentError, match="0x42"):
        BurnAgent(pipe).open_channel()


def test_command_returns_output(pipe):
    pipe.queue_command_ok("version: U-Boot 2016.11-g131d3f2\r\n")
    agent = BurnAgent(pipe)
    assert agent.command("getinfo version") == "version: U-Boot 2016.11-g131d3f2"
    assert pipe.writes[0][3:] == b"getinfo version\x00"


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


def test_sequence_number_advances_per_command(pipe):
    pipe.queue_command_ok()
    pipe.queue_command_ok()
    agent = BurnAgent(pipe)
    agent.command("sf probe 0")
    agent.command("sf probe 0")
    assert pipe.writes[0][1] != pipe.writes[1][1]


def test_upload_sends_header_payload_and_tail(pipe):
    pipe.queue_ack(2)  # header, then tail
    payload = b"\xa5" * 4096
    BurnAgent(pipe).upload(payload, 0x41000000)

    header, body, tail = pipe.writes
    assert header == protocol.agent_head_frame(len(payload), 0x41000000)
    assert body == payload
    assert tail == protocol.agent_tail_frame()


def test_upload_chunks_large_images(pipe):
    pipe.queue_ack(2)
    payload = b"\x00" * (200 * 1024)
    BurnAgent(pipe).upload(payload, 0x41000000)
    body = b"".join(pipe.writes[1:-1])
    assert body == payload
    assert len(pipe.writes) > 3, "a 200 KiB image should be streamed in several writes"


def test_upload_reports_progress(pipe):
    pipe.queue_ack(2)
    seen: list[tuple[int, int]] = []
    payload = b"\x00" * (128 * 1024)
    BurnAgent(pipe).upload(payload, 0x41000000, on_progress=lambda s, t: seen.append((s, t)))
    assert seen[-1] == (len(payload), len(payload))


def test_upload_rejects_an_empty_image(pipe):
    with pytest.raises(ValueError, match="empty image"):
        BurnAgent(pipe).upload(b"", 0x41000000)


def test_upload_tail_nak_says_the_image_is_incomplete(pipe):
    pipe.queue_ack()
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

    sent = [frame[3:].rstrip(b"\x00").decode() for frame in pipe.writes]
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
