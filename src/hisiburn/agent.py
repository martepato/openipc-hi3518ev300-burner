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
import time
from collections.abc import Callable

from hisiburn import protocol
from hisiburn.usbdev import BulkPipe, UsbError

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]

#: Bytes per bulk write while streaming an image. Large enough to keep the
#: pipe busy, small enough for responsive progress reporting.
STREAM_CHUNK = 64 * 1024

#: Commands that only talk to the agent return almost immediately.
DEFAULT_COMMAND_TIMEOUT_MS = 10_000

#: NOR erase and write run at roughly a megabyte per second in the worst case;
#: budget generously so a slow chip is not mistaken for a hang.
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


def erase_command(offset: int, length: int) -> str:
    return f"sf erase 0x{offset:x} 0x{length:x}"


def write_command(address: int, offset: int, length: int) -> str:
    return f"sf write 0x{address:x} 0x{offset:x} 0x{length:x}"


class AgentError(Exception):
    """The agent rejected a frame or answered in a way we cannot continue from."""


class CommandFailed(AgentError):
    """A U-Boot command ran and reported failure.

    This is close to fatal for the session: on the ``[EOT](ERROR)`` path the
    device-side handler returns without re-arming its OUT endpoint, so it stops
    accepting further commands. Recovering means re-entering download mode.
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

    def is_agent(self) -> bool:
        """Whether a U-Boot burn agent — not the boot ROM — is on the other end.

        Both stages present the same USB descriptors, so the only way to tell
        them apart is to run a command only U-Boot implements and see whether
        an ``[EOT]`` comes back.
        """
        try:
            return self.try_command("getinfo version", timeout_ms=3000).ok
        except (AgentError, UsbError, protocol.IncompleteResponse) as exc:
            log.debug("agent probe failed: %s", exc)
            return False

    def read_greeting(self, timeout_ms: int = 1500) -> str | None:
        """Consume the unsolicited "start download process." the agent sends.

        It arrives once, right after the loaded U-Boot enumerates. Reading it
        keeps it from being mistaken for the first command's reply.
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
        log.debug("result %s: %s", "OK" if result.ok else "ERROR", result.output)
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

    def reset(self) -> None:
        """Reboot the camera. The device drops off the bus, which is success."""
        try:
            self.try_command("reset", timeout_ms=3000)
        except (AgentError, UsbError) as exc:
            log.debug("no reply to reset, which is expected: %s", exc)
