"""Client for the U-Boot burn agent — the stage that actually writes flash.

Once the boot ROM has started a U-Boot carrying HiSilicon's ``usbtftp``
support, that U-Boot exposes a bulk endpoint pair over which the host can push
raw bytes into RAM and run arbitrary U-Boot console commands. Flashing is then
just the console sequence a HiBurn log shows:

.. code-block:: text

    mw.b 0x41000000 0xFF <length>       # pre-fill the staging buffer
    <push the image into 0x41000000>
    sf probe 0
    sf erase <offset> <erase-length>
    sf write 0x41000000 <offset> <length>
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

from hisiburn import protocol
from hisiburn.usbdev import BulkPipe, UsbError

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]

#: Bytes per bulk write while streaming an image. Large enough to keep the
#: pipe busy, small enough for responsive progress reporting.
STREAM_CHUNK = 64 * 1024

#: What the burn agent announces on every SET_CONFIGURATION. The boot ROM
#: stays silent, which is what distinguishes them.
GREETING = "start download process"

#: Commands that only talk to the agent return almost immediately.
DEFAULT_COMMAND_TIMEOUT_MS = 10_000

#: Timed from a captured HiBurn session on an EN25QH128A: erase ran at about
#: 2.7 s/MiB (10 MiB in 27.3 s) and write at about 1.6 s/MiB (5.4 MiB in
#: 8.5 s). This budget keeps roughly a four-fold margin over the slower of the
#: two, so a merely slow chip is never mistaken for a hang -- a timeout in the
#: middle of a write does not stop the device, it just desynchronises us.
FLASH_MS_PER_MIB = 12_000
FLASH_TIMEOUT_FLOOR_MS = 30_000


# --- Command text -----------------------------------------------------------
#
# These are the exact strings HiBurn sends, lower-case hex included, so a dry
# run and a real run are formatted by the same code and a capture diff stays
# clean. `flash.py` renders its preview through them for the same reason.


def memset_command(address: int, value: int, length: int) -> str:
    return f"mw.b 0x{address:x} 0x{value:02X} 0x{length:x}"


def probe_command(bus: int = 0) -> str:
    return f"sf probe {bus}"


def read_command(address: int, offset: int, length: int) -> str:
    return f"sf read 0x{address:x} 0x{offset:x} 0x{length:x}"


def memory_dump_command(address: int, count: int) -> str:
    return f"md.b 0x{address:x} 0x{count:x}"


def crc32_command(address: int, length: int) -> str:
    return f"crc32 0x{address:x} 0x{length:x}"


def erase_command(offset: int, length: int) -> str:
    return f"sf erase 0x{offset:x} 0x{length:x}"


def write_command(address: int, offset: int, length: int) -> str:
    return f"sf write 0x{address:x} 0x{offset:x} 0x{length:x}"


#: `md.b` output: an address, a colon, then the bytes, then an ASCII column.
#: Anchoring on the address keeps ASCII that happens to look like hex out.
_DUMP_LINE = re.compile(r"^[0-9a-fA-F]+:\s+((?:[0-9a-fA-F]{2}[ \t]+){1,16})", re.MULTILINE)


def parse_memory_dump(text: str) -> bytes:
    """Pull the bytes out of a `md.b` hex dump."""
    out = bytearray()
    for match in _DUMP_LINE.finditer(text):
        out += bytes(int(token, 16) for token in match.group(1).split())
    return bytes(out)


class AgentError(Exception):
    """The agent rejected a frame or answered in a way we cannot continue from."""


class CommandFailed(AgentError):
    """A U-Boot command ran and reported failure.

    On a stock U-Boot this is close to fatal for the session: the vendor's
    ``[EOT](ERROR)`` path returns without re-arming the OUT endpoint, so the
    device stops accepting commands and recovering means re-entering download
    mode. It also sends the marker alone, so ``result.output`` is empty and
    there is nothing to say *why* it failed.

    The agent U-Boot built by ``tools/build-agent-uboot.sh`` fixes both: it
    re-arms either way and sends the console output with the marker, so a
    failed command there is an ordinary error to handle and carry on from.
    """

    def __init__(self, result: protocol.CommandResult):
        self.result = result
        super().__init__(
            f"U-Boot rejected {result.command!r}"
            + (f": {result.output}" if result.output else "")
            + " — the agent stops accepting commands after an error, so"
            " power-cycle into download mode before retrying"
        )


def flash_timeout_ms(length: int) -> int:
    """A timeout proportional to how much flash an operation touches."""
    return max(FLASH_TIMEOUT_FLOOR_MS, int(length / (1024 * 1024) * FLASH_MS_PER_MIB))


class BurnAgent:
    """Drives one flashing session over an open :class:`BulkPipe`."""

    def __init__(self, pipe: BulkPipe):
        self.pipe = pipe

    # --- framing helpers ---------------------------------------------------

    def _expect_ack(self, what: str, timeout_ms: int | None = None) -> None:
        status = self.pipe.read_byte(timeout_ms)
        if status == protocol.ACK:
            return
        if status == protocol.NAK:
            raise AgentError(f"device NAKed {what}")
        raise AgentError(f"device answered {what} with 0x{status:02x}, expected ACK or NAK")

    # --- session -----------------------------------------------------------

    def ping(self) -> bool:
        """Check that something is listening. Returns False rather than raising.

        Uses the OPEN frame rather than the START frame. Both stages accept
        OPEN as an opening frame — it is the first thing HiBurn sends to the
        boot ROM — whereas START was only ever observed mid-session against a
        running agent, and the boot ROM stalls its endpoint on it.

        An ACK does not prove it is the *agent*; the boot ROM answers this too.
        Use :meth:`is_agent` when the distinction matters.
        """
        try:
            self.pipe.write(protocol.open_frame())
            return self.pipe.read_byte(timeout_ms=2000) == protocol.ACK
        except (UsbError, AgentError) as exc:
            log.debug("ping failed: %s", exc)
            return False

    def is_agent(self, attempts: int = 2) -> bool:
        """Whether a U-Boot burn agent — not the boot ROM — is on the other end.

        Both stages present identical USB descriptors, so something has to be
        asked. It must not be a *command*: the gadget's frame handler has no
        fallback branch, so an opcode the device does not implement gets no
        reply and, worse, leaves the OUT endpoint un-armed — a `getinfo` aimed
        at the boot ROM stops it accepting anything further.

        The banner is the safe discriminator. The agent emits it on every
        SET_CONFIGURATION and the boot ROM never does, which is how HiBurn
        tells them apart too.
        """
        return self.wait_for_greeting(attempts) is not None

    def wait_for_greeting(self, attempts: int = 2) -> str | None:
        """Read the agent's banner, re-triggering it if the first read misses."""
        for attempt in range(attempts):
            text = self.read_greeting()
            if text and GREETING in text:
                return text
            if attempt + 1 < attempts:
                try:
                    self.pipe.reset_configuration()
                except UsbError as exc:
                    log.debug("could not re-trigger the greeting: %s", exc)
                    return None
        return None

    def read_greeting(self, timeout_ms: int = 800) -> str | None:
        """Consume the unsolicited banner the agent sends on SET_CONFIGURATION.

        Reading it also keeps it from being mistaken for the first command's
        reply.
        """
        try:
            data = self.pipe.read(timeout_ms=timeout_ms)
        except UsbError:
            return None
        text = data.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()
        return text or None

    # --- commands ----------------------------------------------------------

    def command(self, command: str, timeout_ms: int = DEFAULT_COMMAND_TIMEOUT_MS) -> str:
        """Run a U-Boot console command and return its output.

        Raises :class:`CommandFailed` if U-Boot reports a non-zero result.
        """
        result = self.try_command(command, timeout_ms)
        if not result.ok:
            raise CommandFailed(result)
        return result.output

    def try_command(
        self, command: str, timeout_ms: int = DEFAULT_COMMAND_TIMEOUT_MS
    ) -> protocol.CommandResult:
        """Run a command and report the outcome without raising on failure."""
        log.debug("send command: %s", command)
        self.pipe.write(protocol.command_frame(command))

        # The reply arrives only once the command has finished running, so a
        # slow erase simply means a long first read. Keep reading in case the
        # agent split its 200-byte buffer across packets.
        buffer = bytearray()
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise AgentError(
                    f"timed out after {timeout_ms} ms waiting for {command!r} to finish"
                    + (f" (partial reply: {bytes(buffer)[:80]!r})" if buffer else "")
                )
            buffer += self.pipe.read(timeout_ms=remaining_ms)
            if protocol.response_is_complete(bytes(buffer)):
                break

        result = protocol.parse_command_response(command, bytes(buffer))
        # Multi-line progress output would swamp the log; the last line is the
        # verdict ("Erased: OK") and the rest is a spinner.
        summary = result.output.splitlines()[-1] if result.output else ""
        log.debug("result %s: %s", "OK" if result.ok else "ERROR", summary)
        return result

    # --- bulk upload -------------------------------------------------------

    def upload(
        self,
        data: bytes,
        address: int,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        """Stream ``data`` straight into device RAM at ``address``.

        The agent DMAs the bytes to the target address with no framing and no
        checksum, so this is the fast path: announce, stream, close.
        """
        if not data:
            raise ValueError("refusing to upload an empty image")

        log.debug("upload %d bytes to 0x%08X", len(data), address)
        # HiBurn syncs with a START frame before every upload; the device just
        # re-arms its OUT endpoint and ACKs.
        self.pipe.write(protocol.start_frame())
        self._expect_ack("upload sync")

        self.pipe.write(protocol.head_frame(len(data), address))
        self._expect_ack(f"upload header for 0x{address:08X}")

        sent = 0
        for offset in range(0, len(data), STREAM_CHUNK):
            piece = data[offset : offset + STREAM_CHUNK]
            written = self.pipe.write(piece, timeout_ms=30_000)
            if written != len(piece):
                raise AgentError(
                    f"short bulk write at offset {offset}: {written} of {len(piece)} bytes"
                )
            sent += written
            if on_progress:
                on_progress(sent, len(data))

        self.pipe.write(protocol.tail_frame())
        try:
            self._expect_ack("upload tail")
        except AgentError as exc:
            raise AgentError(
                f"{exc} — the device still expected more data, so the image "
                "did not land completely"
            ) from exc

    # --- convenience wrappers ---------------------------------------------

    def get_info(self, topic: str) -> str:
        """Query the agent's ``getinfo`` extension (``version``, ``bootmode``, ``spi``)."""
        return self.command(f"getinfo {topic}")

    def memset(self, address: int, value: int, length: int) -> None:
        """Fill device RAM, used to pad the staging buffer before a short image."""
        self.command(
            memset_command(address, value, length), timeout_ms=flash_timeout_ms(length)
        )

    def flash_probe(self, bus: int = 0) -> str:
        return self.command(probe_command(bus))

    def flash_erase(self, offset: int, length: int) -> str:
        return self.command(
            erase_command(offset, length), timeout_ms=flash_timeout_ms(length)
        )

    def flash_write(self, address: int, offset: int, length: int) -> str:
        return self.command(
            write_command(address, offset, length), timeout_ms=flash_timeout_ms(length)
        )

    def flash_read(self, address: int, offset: int, length: int) -> str:
        """Read flash into DRAM. The data stays on the device."""
        return self.command(
            read_command(address, offset, length), timeout_ms=flash_timeout_ms(length)
        )

    def crc32(self, address: int, length: int) -> int:
        """CRC-32 of a DRAM range, as U-Boot computes it.

        U-Boot's crc32 is the standard one, so the result compares directly
        against :func:`zlib.crc32`. This is the only way to check flash
        contents against a local file on a U-Boot without `usbtftp`: the
        bytes never leave the device, just a checksum of them.
        """
        output = self.command(
            crc32_command(address, length), timeout_ms=flash_timeout_ms(length)
        )
        match = re.search(r"==>\s*([0-9a-fA-F]{1,8})", output)
        if not match:
            raise AgentError(
                f"could not read a checksum out of {output!r}. If U-Boot says the "
                "command is unknown, this build has CONFIG_CMD_CRC32 disabled."
            )
        return int(match.group(1), 16)

    #: Bytes per `md.b` call. The agent appends to a fixed 200-byte reply
    #: buffer with no bounds check, and U-Boot spends about 79 of those per
    #: 16-byte line, so two lines is both the most that fits and the most that
    #: is safe to ask for — a third would overrun the buffer on the device.
    DUMP_STRIDE = 32

    def read_memory(
        self, address: int, count: int, on_progress: ProgressCallback | None = None
    ) -> bytes:
        """Read DRAM back as text, through U-Boot's `md.b`.

        This is the only way to see actual bytes on a U-Boot without
        `usbtftp`: they come back as a hex dump in the command reply. It is
        far too slow for bulk transfer -- tens of bytes per round trip -- but
        exactly right for looking at a header or a few blocks.
        """
        out = bytearray()
        while len(out) < count:
            want = min(self.DUMP_STRIDE, count - len(out))
            text = self.command(memory_dump_command(address + len(out), want))
            chunk = parse_memory_dump(text)
            if not chunk:
                raise AgentError(
                    f"could not read bytes out of {text!r}. If U-Boot says the "
                    "command is unknown, this build has CONFIG_CMD_MEMORY disabled."
                )
            out += chunk[:want]
            if on_progress:
                on_progress(len(out), count)
        return bytes(out[:count])

    def usbtftp_read(
        self,
        offset: int,
        length: int,
        on_progress: ProgressCallback | None = None,
        timeout_ms: int = 30_000,
    ) -> bytes:
        """Read flash over the agent's bulk upload path.

        Present only in a U-Boot built by `tools/build-agent-uboot.sh`. The
        command reads the range into a device-side buffer and arms a callback;
        the host then pumps request frames, each answered with one bulk
        transfer. That moves a whole frame per round trip instead of the 32
        bytes a hex dump manages.

        The command returns as soon as the buffer is filled -- so it replies
        like any other, and that reply has to be read before the first request
        frame or the pipe runs a reply behind for the rest of the transfer.
        The session it opens is closed by `usbtftp end`, and the device refuses
        to start another until it is.

        `length` is bounded by U-Boot's heap, not by the protocol: the device
        malloc()s the whole range up front. `flash.BACKUP_CHUNK_BULK` is sized
        against the agent build's arena.
        """
        # A slow SPI read is the whole of this command, so budget it like one.
        try:
            self.command(
                f"usbtftp 0x{offset:x} backup.bin 0x{length:x}",
                timeout_ms=flash_timeout_ms(length),
            )
        except CommandFailed as exc:
            if "malloc" in exc.result.output:
                raise AgentError(
                    f"the device could not allocate {length:,} bytes to serve the "
                    "read. Its heap is CONFIG_SYS_MALLOC_LEN, so read in smaller "
                    "pieces (`--chunk`) or raise the arena in the agent build."
                ) from exc
            raise

        out = bytearray()
        frame_len: int | None = None
        total: int | None = None
        try:
            while True:
                self.pipe.write(protocol.request_frame())
                # Ask for exactly what the next frame should hold: a transfer
                # that happens to be a multiple of the packet size ends with no
                # short packet, and a read sized by guesswork would hang.
                if frame_len is None or total is None:
                    want = 512
                else:
                    want = min(frame_len, total - len(out)) + 1
                data = self.pipe.read(max(want, 512), timeout_ms=timeout_ms)
                if not data:
                    raise AgentError("empty frame during a usbtftp read")

                kind = data[0]
                if kind == protocol.OP_HEAD:
                    total = int.from_bytes(data[1:5], "big")
                    frame_len = int.from_bytes(data[5:9], "big")
                    log.debug("usbtftp: %d bytes in %d-byte frames", total, frame_len)
                elif kind == protocol.OP_DATA:
                    out += data[1:]
                    if on_progress and total:
                        on_progress(min(len(out), total), total)
                elif kind == protocol.OP_TAIL:
                    # Always read through to the tail rather than stopping on a
                    # byte count: leaving a frame unread desynchronises the pipe
                    # for whatever runs next.
                    break
                else:
                    raise AgentError(f"unexpected frame 0x{kind:02x} during a read")

                if total is not None and len(out) > total:
                    raise AgentError(
                        f"usbtftp sent {len(out)} bytes, more than the {total} "
                        "it announced"
                    )
        finally:
            # Always close the session, including after a failure part-way
            # through: the device frees its buffer here, and refuses to start
            # another read while one is open.
            try:
                self.try_command("usbtftp end", timeout_ms=5_000)
            except (UsbError, AgentError) as exc:
                log.debug("could not close the usbtftp session: %s", exc)

        if total is not None and len(out) != total:
            raise AgentError(
                f"usbtftp returned {len(out)} bytes, not the {total} it announced"
            )
        return bytes(out[:length])

    # There is deliberately no "does this U-Boot have command X" probe.
    # `help <name>` cannot answer it: cmd_usage() returns 1 unconditionally and
    # _do_help ORs that in, so help reports failure whether the command exists
    # or not. Worse, it prints the full multi-line help through udc_puts(),
    # which strcats into a fixed 200-byte buffer without a bounds check -- long
    # help overruns it and the device stops responding. Capabilities are read
    # from the U-Boot image instead; see hisiburn.image.inspect_uboot.

    def reset(self) -> None:
        """Reboot the camera. The device drops off the bus, which is success."""
        try:
            self.try_command("reset", timeout_ms=3000)
        except (AgentError, UsbError) as exc:
            log.debug("no reply to reset, which is expected: %s", exc)
