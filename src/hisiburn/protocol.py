"""Wire formats for the two protocols a HiSilicon camera speaks over USB.

Flashing happens in two stages, and each stage speaks a *different* protocol
on the same pair of bulk endpoints:

**Stage 1 — boot ROM.** With the reset button held at power-on the mask ROM
enumerates and accepts CRC-checked frames that write into SRAM/DDR. Used to
land a RAM-init blob and a U-Boot image, then start them.

**Stage 2 — burn agent.** The U-Boot that stage 1 started re-enumerates and
runs HiSilicon's ``usbtftp`` agent: a much simpler, CRC-free protocol that
pushes raw bytes into RAM and runs U-Boot console commands.

Both layouts are transcribed from the vendor's own GPL sources shipped in
OpenIPC's ``u-boot-hi3516ev200`` tree — stage 1 from the frame documentation
in ``drivers/usb/gadget/hiudc3/usb3_prot.h``, stage 2 from the device-side
handler ``usb3_handle_protocol()`` in ``usb3_prot.c``.
"""

from __future__ import annotations

from dataclasses import dataclass

from hisiburn.crc import append_crc

# --- Shared responses -------------------------------------------------------

ACK = 0xAA
NAK = 0x55

# --- Stage 1: boot ROM frame types -----------------------------------------

FRAME_FILE = 0xFE
FRAME_DATA = 0xDA
FRAME_EOT = 0xED
FRAME_INQUIRE = 0xCD

#: Value of the FILE frame's type byte for the RAM-init (DDR setup) blob.
FILE_RAMINIT = 1
#: Value of the FILE frame's type byte for the U-Boot image.
FILE_USB = 2

#: Largest DATA payload the boot ROM accepts.
MAX_DATA_LEN = 1024

# --- Stage 2: burn agent opcodes -------------------------------------------

USTART = 0xFA
UHEAD = 0xFE
UDATA = 0xDA
UTAIL = 0xED
UCMD = 0xAB
UREQ = 0xFB

#: The agent copies its reply into a fixed 200-byte buffer before sending it,
#: so no single command response can exceed this.
AGENT_TX_BUF = 200

#: Terminator the agent appends to every command response.
EOT_OK = b"[EOT](OK)"
EOT_ERROR = b"[EOT](ERROR)"


def _seq_pair(seq: int) -> bytes:
    """Sequence byte followed by its complement, as every framed message uses."""
    seq &= 0xFF
    return bytes((seq, (~seq) & 0xFF))


# --- Stage 1 frame builders -------------------------------------------------


def bootrom_file_frame(file_type: int, length: int, address: int, seq: int = 0) -> bytes:
    """Build the 14-byte FILE frame announcing an upload.

    Layout: ``TYPE(1) SEQ(1) ~SEQ(1) FILE(1) LENGTH(4) ADDRESS(4) CRC(2)``.
    """
    body = (
        bytes((FRAME_FILE,))
        + _seq_pair(seq)
        + bytes((file_type & 0xFF,))
        + length.to_bytes(4, "big")
        + address.to_bytes(4, "big")
    )
    return append_crc(body)


def bootrom_data_frame(payload: bytes, seq: int) -> bytes:
    """Build a DATA frame carrying up to :data:`MAX_DATA_LEN` payload bytes."""
    if len(payload) > MAX_DATA_LEN:
        raise ValueError(f"payload {len(payload)} exceeds {MAX_DATA_LEN}-byte frame limit")
    return append_crc(bytes((FRAME_DATA,)) + _seq_pair(seq) + payload)


def bootrom_eot_frame(seq: int) -> bytes:
    """Build the 5-byte EOT frame closing an upload."""
    return append_crc(bytes((FRAME_EOT,)) + _seq_pair(seq))


def chunk(data: bytes, size: int = MAX_DATA_LEN) -> list[bytes]:
    """Split ``data`` into transmission-sized pieces."""
    return [data[i : i + size] for i in range(0, len(data), size)]


# --- Stage 2 frame builders -------------------------------------------------


def agent_start_frame() -> bytes:
    """Probe frame; a live agent answers :data:`ACK`."""
    return bytes((USTART,))


def agent_head_frame(length: int, address: int) -> bytes:
    """Announce ``length`` raw bytes to be DMA'd to ``address``.

    Layout: ``0xFE LENGTH(4) ADDRESS(4)``. No sequence bytes and no checksum —
    the agent reads the two words straight out of the packet.

    The device treats ``address == length`` as a channel-open handshake rather
    than a transfer, so :func:`agent_open_frame` exists for that case and this
    function refuses to build one by accident.
    """
    if length == address:
        raise ValueError(
            "length == address is the agent's channel-open form, not a transfer; "
            "use agent_open_frame()"
        )
    return bytes((UHEAD,)) + length.to_bytes(4, "big") + address.to_bytes(4, "big")


def agent_open_frame(token: int = 1) -> bytes:
    """Open the agent's output channel.

    Sending a HEAD frame whose length and address are equal makes the device
    mark itself connected and start echoing console output, instead of
    entering the raw-data receive path.
    """
    return bytes((UHEAD,)) + token.to_bytes(4, "big") + token.to_bytes(4, "big")


def agent_command_frame(command: str, seq: int = 0) -> bytes:
    """Build a console-command frame.

    Layout: ``0xAB SEQ(1) ~SEQ(1)`` then the NUL-terminated command text. The
    device runs everything from offset 3 onward through U-Boot's
    ``run_command()``.
    """
    encoded = command.encode("ascii")
    if b"\x00" in encoded:
        raise ValueError("command must not contain NUL bytes")
    return bytes((UCMD,)) + _seq_pair(seq) + encoded + b"\x00"


def agent_tail_frame() -> bytes:
    """Close a raw-data transfer. The agent NAKs if bytes are still outstanding."""
    return bytes((UTAIL,))


def agent_request_frame() -> bytes:
    """Ask the agent for the next frame of an in-progress flash read-back."""
    return bytes((UREQ,))


# --- Stage 2 response parsing ----------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one U-Boot console command run through the agent."""

    command: str
    output: str
    ok: bool

    def __bool__(self) -> bool:
        return self.ok


def parse_command_response(command: str, raw: bytes) -> CommandResult:
    """Split an agent reply into console output and its ``[EOT]`` verdict.

    The agent NUL-terminates its replies and pads the rest of the packet, so
    everything from the first NUL is dropped before looking for the marker.
    """
    body = raw.split(b"\x00", 1)[0]
    if EOT_OK in body:
        ok, marker = True, EOT_OK
    elif EOT_ERROR in body:
        ok, marker = False, EOT_ERROR
    else:
        raise IncompleteResponse(
            f"no [EOT] marker in {len(raw)}-byte reply to {command!r}: {body[:80]!r}"
        )
    output = body.split(marker, 1)[0].decode("utf-8", errors="replace").strip()
    return CommandResult(command=command, output=output, ok=ok)


def response_is_complete(raw: bytes) -> bool:
    """Whether ``raw`` already carries an ``[EOT]`` marker."""
    body = raw.split(b"\x00", 1)[0]
    return EOT_OK in body or EOT_ERROR in body


class IncompleteResponse(Exception):
    """Raised when a reply arrived without its terminating ``[EOT]`` marker."""
