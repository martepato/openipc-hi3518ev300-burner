"""Wire formats for the two protocols a HiSilicon camera speaks over USB.

Both stages of a flash — the boot ROM that loads U-Boot into RAM, and the
U-Boot burn agent that writes flash — talk over the same pair of bulk
endpoints and share most of their framing. Neither carries a checksum or a
sequence number; the frames below are the whole protocol.

Every layout here is transcribed from a USBPcap capture of a successful
HiBurn 5.3 flash of a Hi3518EV300, and is asserted byte-for-byte against
those captured frames in ``tests/test_protocol.py``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# --- Opcodes ----------------------------------------------------------------

OP_START = 0xFA
OP_HEAD = 0xFE
OP_DATA = 0xDA
OP_TAIL = 0xED
OP_CMD = 0xAB
OP_REQ = 0xFB

# --- Responses --------------------------------------------------------------

ACK = 0xAA
NAK = 0x55

# --- Sizes ------------------------------------------------------------------

#: Bulk max packet size the device reports at high speed.
MAX_PACKET = 512

#: Largest payload a boot ROM DATA frame carries: one max packet, less the
#: opcode byte. HiBurn never sends a larger one.
MAX_DATA_PAYLOAD = MAX_PACKET - 1

#: The agent copies each reply into a fixed 200-byte buffer before sending it,
#: which is why long console output arrives truncated.
AGENT_TX_BUF = 200

EOT_OK = b"[EOT](OK)"
EOT_ERROR = b"[EOT](ERROR)"


def _token(value: int | None = None) -> bytes:
    """A four-byte nonce, repeated by the frames that carry one.

    The device ignores it entirely — the handler only looks at the opcode and
    at whether the two words are equal. HiBurn sends a timestamp, so this does
    too, purely so captures line up.
    """
    if value is None:
        value = int(time.time()) & 0xFFFFFFFF
    return (value & 0xFFFFFFFF).to_bytes(4, "big")


# --- Frames shared by both stages ------------------------------------------


def head_frame(length: int, address: int) -> bytes:
    """Announce ``length`` bytes bound for ``address``.

    Layout: ``FE <length:4> <address:4>``, big-endian, nine bytes.

    The device treats ``length == address`` as the channel-open handshake
    rather than a transfer, so that case is refused here and has its own
    builder.
    """
    if length == address:
        raise ValueError(
            "length == address is the channel-open form, not a transfer; use open_frame()"
        )
    return bytes((OP_HEAD,)) + length.to_bytes(4, "big") + address.to_bytes(4, "big")


def open_frame(token: int | None = None) -> bytes:
    """Open a session with the boot ROM: ``FE <token> <token>``.

    Sending both words equal makes the device mark itself connected instead of
    entering its receive path. HiBurn sends this once, before the first image.
    """
    payload = _token(token)
    return bytes((OP_HEAD,)) + payload + payload


def start_frame(token: int | None = None) -> bytes:
    """Sync with the burn agent: ``FA <token> <token>``.

    The device just re-arms its OUT endpoint and ACKs. HiBurn sends one before
    every upload, and it doubles as a liveness probe.
    """
    payload = _token(token)
    return bytes((OP_START,)) + payload + payload


def tail_frame() -> bytes:
    """Close a transfer. A bare ``ED``; the device NAKs if bytes are outstanding."""
    return bytes((OP_TAIL,))


# --- Boot ROM data framing --------------------------------------------------


def data_frame(payload: bytes) -> bytes:
    """Wrap up to :data:`MAX_DATA_PAYLOAD` bytes as ``DA <payload>``.

    Only the boot ROM wants this framing. The burn agent takes its uploads as
    a raw byte stream with no per-frame opcode at all.
    """
    if len(payload) > MAX_DATA_PAYLOAD:
        raise ValueError(
            f"payload {len(payload)} exceeds the {MAX_DATA_PAYLOAD}-byte frame limit"
        )
    if not payload:
        raise ValueError("refusing to build an empty DATA frame")
    return bytes((OP_DATA,)) + payload


def chunk(data: bytes, size: int = MAX_DATA_PAYLOAD) -> list[bytes]:
    """Split ``data`` into transmission-sized pieces."""
    return [data[i : i + size] for i in range(0, len(data), size)]


# --- Burn agent commands ----------------------------------------------------


def command_frame(command: str) -> bytes:
    """Build a console-command frame: ``AB <length:2> <command>``.

    The length is the big-endian byte count of the command text, and the text
    is *not* NUL-terminated — the device zeroes its receive buffer before each
    packet, so the terminator is already there.
    """
    encoded = command.encode("ascii")
    if b"\x00" in encoded:
        raise ValueError("command must not contain NUL bytes")
    if len(encoded) > 0xFFFF:
        raise ValueError(f"command of {len(encoded)} bytes does not fit the length field")
    return bytes((OP_CMD,)) + len(encoded).to_bytes(2, "big") + encoded


def request_frame() -> bytes:
    """Ask the agent for the next frame of an in-progress flash read-back."""
    return bytes((OP_REQ,))


# --- Response parsing -------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one U-Boot console command run through the agent."""

    command: str
    output: str
    ok: bool

    def __bool__(self) -> bool:
        return self.ok


class IncompleteResponse(Exception):
    """A reply arrived without its terminating ``[EOT]`` marker."""


def _body(raw: bytes) -> bytes:
    """Strip the NUL terminator and any packet padding after it."""
    return raw.split(b"\x00", 1)[0]


def parse_command_response(command: str, raw: bytes) -> CommandResult:
    """Split an agent reply into console output and its ``[EOT]`` verdict."""
    body = _body(raw)
    if EOT_OK in body:
        ok, marker = True, EOT_OK
    elif EOT_ERROR in body:
        ok, marker = False, EOT_ERROR
    else:
        raise IncompleteResponse(
            f"no [EOT] marker in {len(raw)}-byte reply to {command!r}: {body[:80]!r}"
        )
    # The device prefixes every reply with a space of its own making, and
    # separates progress steps with bare carriage returns so a terminal
    # overwrites them in place. Left as-is those CRs also overwrite whatever
    # else is on the line -- log prefixes included -- so turn each step into
    # its own line and keep them all.
    text = body.split(marker, 1)[0].decode("utf-8", errors="replace")
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\r")]
    output = "\n".join(line for line in "\n".join(lines).split("\n") if line.strip())
    return CommandResult(command=command, output=output.strip(), ok=ok)


def response_is_complete(raw: bytes) -> bool:
    """Whether ``raw`` already carries an ``[EOT]`` marker."""
    body = _body(raw)
    return EOT_OK in body or EOT_ERROR in body
