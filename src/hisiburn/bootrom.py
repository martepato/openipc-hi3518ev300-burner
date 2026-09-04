"""Client for the mask ROM — the stage that gets a U-Boot running in RAM.

With the reset button held at power-on, the Hi3518EV300's boot ROM enumerates
as ``12d1:3609`` and accepts CRC-checked frames that write into on-chip SRAM
and, once DDR is up, into DRAM. Three images go across:

1. a 64-byte DDR-init step into SRAM, which brings up the memory controller;
2. the SPL sliced off the front of the U-Boot binary, also into SRAM;
3. the full U-Boot image into DRAM at 0x41000000, which then starts.

The frame layout and staging sequence are the same ones OpenIPC's ``defib``
drives over UART — its 14-byte "HEAD" frame ``FE 00 FF 01 …`` is exactly this
module's FILE frame with sequence 0 and file type 1. What differs here is only
the pipe underneath: bulk endpoints instead of a serial line.

Every write in this stage lands in volatile memory. A wrong frame gets NAKed
or ignored; nothing reaches flash, so a failed attempt costs a power cycle.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from hisiburn import protocol
from hisiburn.usbdev import BulkPipe, UsbError

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], None]

#: Retries per frame. The boot ROM NAKs a frame it could not checksum, and a
#: resend usually settles it.
FRAME_RETRIES = 8

FRAME_TIMEOUT_MS = 2000


class BootRomError(Exception):
    """The boot ROM refused a frame or stopped answering."""


@dataclass(frozen=True)
class ChipProfile:
    """Load addresses and the DDR-init blob for one SoC.

    Values are those OpenIPC's ``defib`` uses for this chip family; the
    Hi3518EV300 shares its profile with the Hi3516EV200/EV300.
    """

    name: str
    ddr_init: bytes
    ddr_address: int
    spl_address: int
    uboot_address: int
    spl_max_size: int

    def __post_init__(self) -> None:
        if len(self.ddr_init) != 64:
            raise ValueError(f"DDR init blob must be 64 bytes, got {len(self.ddr_init)}")


HI3518EV300 = ChipProfile(
    name="hi3518ev300",
    ddr_init=bytes.fromhex(
        "04e02de524009fe524109fe5001080e520009fe520109fe5041080e400e080e5"
        "04f09de4efbeaddeefbeaddeefbeadde3c0102127856341240010212756a697a"
    ),
    ddr_address=0x04013000,
    spl_address=0x04010500,
    uboot_address=0x41000000,
    spl_max_size=0x6000,
)

PROFILES = {
    "hi3518ev300": HI3518EV300,
    # The EV200/EV300 camera SoCs share a boot ROM and these load addresses.
    "hi3516ev200": HI3518EV300,
    "hi3516ev300": HI3518EV300,
}


def detect_spl_size(uboot: bytes, profile: ChipProfile) -> int:
    """Find where the SPL ends and its compressed U-Boot payload begins.

    HiSilicon's mini-boot layout is: vector table, register setup, SPL code,
    then a compressed U-Boot payload. Only the code belongs in SRAM — bytes
    past the payload boundary overwrite the memory the boot ROM is using for
    its own stack, which hangs the chip mid-upload.
    """
    lzma_dictionary_sizes = {1 << n for n in range(16, 25)}
    for index in range(0x4000, min(len(uboot), 0x10000)):
        if uboot[index] == 0x5D:
            dictionary = int.from_bytes(uboot[index + 1 : index + 5], "little")
            if dictionary in lzma_dictionary_sizes:
                return index & ~0x3FF
        elif uboot[index : index + 3] == b"\x1f\x8b\x08":
            return index & ~0x3FF
    return profile.spl_max_size


class BootRom:
    """Drives the boot ROM over an open :class:`BulkPipe`."""

    def __init__(self, pipe: BulkPipe):
        self.pipe = pipe

    def _send_frame(self, frame: bytes, label: str) -> None:
        """Send one frame, resending while the device NAKs it."""
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
            log.debug("%s attempt %d got 0x%02x", label, attempt + 1, status)
        detail = f"last answer 0x{last_status:02x}" if last_status is not None else "no answer"
        raise BootRomError(f"boot ROM never acknowledged {label} ({detail})")

    def send_image(
        self,
        data: bytes,
        address: int,
        file_type: int = protocol.FILE_RAMINIT,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        """Write ``data`` to ``address`` as a FILE / DATA… / EOT sequence."""
        log.debug("boot ROM: %d bytes to 0x%08X (type %d)", len(data), address, file_type)
        self._send_frame(
            protocol.bootrom_file_frame(file_type, len(data), address),
            f"FILE header for 0x{address:08X}",
        )

        seq = 1
        sent = 0
        for piece in protocol.chunk(data):
            self._send_frame(protocol.bootrom_data_frame(piece, seq), f"DATA frame {seq}")
            seq = (seq + 1) & 0xFF
            sent += len(piece)
            if on_progress:
                on_progress(sent, len(data))

        self._send_frame(protocol.bootrom_eot_frame(seq), "EOT frame")

    def boot_uboot(
        self,
        uboot: bytes,
        profile: ChipProfile = HI3518EV300,
        on_progress: ProgressCallback | None = None,
    ) -> None:
        """Run the full three-stage sequence that leaves U-Boot executing."""
        log.info(
            "stage 1/3: DDR init (%d bytes to 0x%08X)",
            len(profile.ddr_init),
            profile.ddr_address,
        )
        self.send_image(profile.ddr_init, profile.ddr_address)

        spl_size = detect_spl_size(uboot, profile)
        log.info("stage 2/3: SPL (%d bytes to 0x%08X)", spl_size, profile.spl_address)
        self.send_image(uboot[:spl_size], profile.spl_address, on_progress=on_progress)

        log.info("stage 3/3: U-Boot (%d bytes to 0x%08X)", len(uboot), profile.uboot_address)
        self.send_image(
            uboot,
            profile.uboot_address,
            file_type=protocol.FILE_USB,
            on_progress=on_progress,
        )
