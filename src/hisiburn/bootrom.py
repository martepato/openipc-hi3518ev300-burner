"""Client for the boot ROM — the stage that gets a U-Boot running in RAM.

With the reset button held at power-on, the camera enumerates as a HiUSBBurn
device and accepts three images:

1. a 64-byte DDR-init stub into SRAM, which brings the memory controller up
   *and* sets the flag that makes the U-Boot it loads enter download mode;
2. the first 0x6000 bytes of the U-Boot binary — its SPL — also into SRAM;
3. the whole U-Boot image into DRAM at 0x41000000, which starts on EOT.

Every write in this stage lands in volatile memory. A wrong frame is ignored
or NAKed; nothing reaches flash, so a failed attempt costs a power cycle.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from hisiburn import protocol
from hisiburn.usbdev import BulkPipe, UsbError

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]

FRAME_RETRIES = 8
FRAME_TIMEOUT_MS = 3000

#: ``START_MAGIC`` from the vendor's ``arch/arm/include/asm/arch-hi3518ev300/
#: platform.h`` — ASCII "DOWN". The DDR stub stores it to ``REG_START_FLAG``
#: (``SYS_CTRL_REG_BASE + REG_SC_GEN1`` = 0x1202013C), and ``download_boot()``
#: in the U-Boot it loads checks for exactly this value before entering the
#: download loop. Without it the loaded U-Boot boots normally and no burn
#: agent ever appears.
START_MAGIC = 0x444F574E

#: Offset of that word inside the 64-byte stub. It sits in the literal pool
#: the stub's `ldr` instructions read from.
START_MAGIC_OFFSET = 52


class BootRomError(Exception):
    """The boot ROM refused a frame or stopped answering."""


@dataclass(frozen=True)
class ChipProfile:
    """Load addresses and the DDR-init stub for one SoC."""

    name: str
    ddr_init: bytes
    ddr_address: int
    spl_address: int
    spl_size: int
    uboot_address: int

    def __post_init__(self) -> None:
        if len(self.ddr_init) != 64:
            raise ValueError(f"DDR init stub must be 64 bytes, got {len(self.ddr_init)}")
        magic = int.from_bytes(
            self.ddr_init[START_MAGIC_OFFSET : START_MAGIC_OFFSET + 4], "little"
        )
        if magic != START_MAGIC:
            raise ValueError(
                f"DDR stub for {self.name!r} carries 0x{magic:08X} at offset "
                f"{START_MAGIC_OFFSET}, not START_MAGIC 0x{START_MAGIC:08X}. A stub "
                "without it leaves the loaded U-Boot booting normally instead of "
                "entering USB download mode — profiles taken from UART tools "
                "commonly have a different value here."
            )


HI3518EV300 = ChipProfile(
    name="hi3518ev300",
    # Captured from a HiBurn USB session. The tail is a literal pool: the stub
    # stores START_MAGIC to REG_START_FLAG and a second word to REG_SC_GEN2,
    # then parks its return address in REG_SC_GEN3.
    ddr_init=bytes.fromhex(
        "04e02de524009fe524109fe5001080e520009fe520109fe5041080e400e080e5"
        "04f09de4efbeaddeefbeaddeefbeadde3c0102124e574f4440010212756a697a"
    ),
    ddr_address=0x04013000,
    spl_address=0x04010500,
    spl_size=0x6000,
    uboot_address=0x41000000,
)

PROFILES = {
    "hi3518ev300": HI3518EV300,
    # The EV200/EV300 camera SoCs share a boot ROM and these load addresses.
    "hi3516ev200": HI3518EV300,
    "hi3516ev300": HI3518EV300,
}


class BootRom:
    """Drives the boot ROM over an open :class:`BulkPipe`."""

    def __init__(self, pipe: BulkPipe):
        self.pipe = pipe

    def _send_acked(self, frame: bytes, label: str) -> None:
        """Send a frame the device answers, resending while it NAKs."""
        last_status: int | None = None
        for attempt in range(FRAME_RETRIES):
            try:
                self.pipe.write(frame, timeout_ms=FRAME_TIMEOUT_MS)
                status = self.pipe.read_byte(timeout_ms=FRAME_TIMEOUT_MS)
            except UsbError as exc:
                log.debug("%s attempt %d failed: %s", label, attempt + 1, exc)
                continue
            if status == protocol.ACK:
                return
            last_status = status
            log.debug("%s attempt %d answered 0x%02x", label, attempt + 1, status)
        detail = f"last answer 0x{last_status:02x}" if last_status is not None else "no answer"
        raise BootRomError(f"boot ROM never acknowledged {label} ({detail})")

    def open_session(self, token: int | None = None) -> None:
        """Handshake before the first image."""
        self._send_acked(protocol.open_frame(token), "session open")

    def send_image(
        self,
        data: bytes,
        address: int,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        """Write ``data`` to ``address``.

        Only the header and the tail are acknowledged; the DATA frames between
        them stream out back to back, which is what makes this stage fast.
        """
        if not data:
            raise ValueError("refusing to send an empty image")

        log.debug("boot ROM: %d bytes to 0x%08X", len(data), address)
        self._send_acked(
            protocol.head_frame(len(data), address), f"header for 0x{address:08X}"
        )

        sent = 0
        for piece in protocol.chunk(data):
            self.pipe.write(protocol.data_frame(piece), timeout_ms=FRAME_TIMEOUT_MS)
            sent += len(piece)
            if on_progress:
                on_progress(sent, len(data))

        self._send_acked(protocol.tail_frame(), f"tail for 0x{address:08X}")

    def boot_uboot(
        self,
        uboot: bytes,
        profile: ChipProfile = HI3518EV300,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        """Run the three-image sequence that leaves U-Boot executing."""
        if len(uboot) < profile.spl_size:
            raise BootRomError(
                f"U-Boot image is {len(uboot)} bytes, shorter than the "
                f"{profile.spl_size}-byte SPL window this chip loads first"
            )

        self.open_session()

        log.info("stage 1/3: DDR init, %d bytes to 0x%08X",
                 len(profile.ddr_init), profile.ddr_address)
        self.send_image(profile.ddr_init, profile.ddr_address)

        log.info("stage 2/3: SPL, %d bytes to 0x%08X", profile.spl_size, profile.spl_address)
        self.send_image(uboot[: profile.spl_size], profile.spl_address, on_progress)

        log.info("stage 3/3: U-Boot, %d bytes to 0x%08X", len(uboot), profile.uboot_address)
        self.send_image(uboot, profile.uboot_address, on_progress)
