"""Identify what a firmware .bin actually is before writing it anywhere.

A file handed round as "the factory firmware" is usually one of two things: a
raw dump of a whole flash chip, or a single partition image. They are written
completely differently, and getting it wrong overwrites the wrong regions, so
the distinction is worth establishing from the bytes rather than the filename.

Nothing here needs binwalk; the handful of signatures these cameras use are
short enough to find directly.
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

#: NOR erase granularity: every real partition edge is a multiple of this.
ERASE_BLOCK = 64 * 1024

#: Sizes a NOR chip on these cameras plausibly has.
KNOWN_CHIP_SIZES = tuple(1024 * 1024 * (1 << n) for n in range(0, 6))  # 1..32 MiB

UIMAGE_MAGIC = b"\x27\x05\x19\x56"

#: U-Boot legacy image types. The distinction that matters here is between a
#: kernel, which is one partition's contents, and a firmware image, which is a
#: packaged update the bootloader unpacks and applies itself.
UIMAGE_TYPES = {
    1: "standalone program", 2: "OS kernel", 3: "ramdisk", 4: "multi-file",
    5: "firmware update package", 6: "script", 7: "filesystem", 8: "flat device tree",
}
UIMAGE_OS = {0: "invalid", 5: "Linux", 17: "U-Boot"}
UIMAGE_COMPRESSION = {0: "uncompressed", 1: "gzip", 2: "bzip2", 3: "lzma", 4: "lzo"}
SQUASHFS_MAGIC = b"hsqs"
JFFS2_MAGIC = b"\x85\x19"
GZIP_MAGIC = b"\x1f\x8b\x08"
UBI_MAGIC = b"UBI#"


@dataclass
class Finding:
    """One recognisable structure inside the image."""

    offset: int
    kind: str
    description: str
    size: int | None = None
    #: Structure-specific fields other code needs to reason about.
    detail: dict = field(default_factory=dict)

    @property
    def end(self) -> int | None:
        return None if self.size is None else self.offset + self.size

    def __str__(self) -> str:
        size = f"{self.size:>10,}" if self.size is not None else " " * 10
        return f"0x{self.offset:08X} {self.kind:<10} {size}  {self.description}"


@dataclass
class ImageReport:
    """What a firmware file looks like from the inside."""

    path: Path
    size: int
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_chip_sized(self) -> bool:
        return self.size in KNOWN_CHIP_SIZES

    @property
    def starts_with_bootloader(self) -> bool:
        """Whether something bootloader-shaped sits at offset 0.

        A whole-chip dump begins with the boot partition; a lone kernel or
        rootfs image begins with its own header instead.
        """
        if not self.findings:
            return False
        first = self.findings[0]
        if first.offset == 0 and first.kind in {"uImage", "squashfs", "jffs2"}:
            return False
        return any(f.kind == "gzip" and f.offset < ERASE_BLOCK * 4 for f in self.findings)

    @property
    def is_full_dump(self) -> bool:
        """Whether this looks like a raw dump of an entire flash chip."""
        if self.packaged_update is not None:
            return False
        return self.is_chip_sized and self.starts_with_bootloader and len(self.findings) > 2

    @property
    def packaged_update(self) -> Finding | None:
        """A vendor update package rather than anything flashable directly.

        Xiaomi's SD-card recovery images are a U-Boot legacy image of type
        "firmware": the bootloader reads one off the card, unpacks it and
        applies it itself. Writing one to flash would just put the wrapper
        where a bootloader belongs.
        """
        for finding in self.findings:
            if finding.offset == 0 and finding.detail.get("type_id") == 5:
                return finding
        return None

    @property
    def verdict(self) -> str:
        packaged = self.packaged_update
        if packaged is not None:
            name = packaged.detail.get("name", "")
            return (
                "a U-Boot firmware update package"
                + (f' ("{name}")' if name else "")
                + " — meant for the camera's own updater (the SD-card recovery "
                "procedure), not for writing to flash"
            )
        if self.is_full_dump:
            mib = self.size // (1024 * 1024)
            return f"full {mib} MiB flash dump — write it verbatim from offset 0"
        if self.is_chip_sized and len(self.findings) > 2:
            return (
                "chip-sized, but nothing bootloader-shaped at offset 0 — "
                "check before treating it as a full dump"
            )
        if len(self.findings) == 1 and self.findings[0].offset == 0:
            return f"a single {self.findings[0].kind} image, not a whole-chip dump"
        return "unrecognised layout — inspect it by hand before flashing"

    def boundaries(self) -> list[tuple[int, int, str]]:
        """Infer partition extents: (offset, length, what lives there).

        Each structure is taken to start a partition that runs to the next one,
        which is how these layouts are actually built — every edge lands on an
        erase block and the regions tile the chip with no gaps.
        """
        starts: list[tuple[int, str]] = []
        for finding in self.findings:
            edge = finding.offset - (finding.offset % ERASE_BLOCK)
            label = finding.kind
            if finding.kind == "gzip" and edge == 0:
                label = "bootloader"
            if starts and starts[-1][0] == edge:
                continue
            starts.append((edge, label))

        extents = []
        for index, (offset, label) in enumerate(starts):
            end = starts[index + 1][0] if index + 1 < len(starts) else self.size
            extents.append((offset, end - offset, label))
        return extents

    def describe(self) -> str:
        lines = [
            f"{self.path.name}: {self.size:,} bytes "
            f"({self.size / 1024 / 1024:.2f} MiB)",
            f"verdict: {self.verdict}",
            "",
            "contents:",
        ]
        lines += [f"  {finding}" for finding in self.findings] or ["  (nothing recognised)"]
        if self.is_full_dump:
            lines += ["", "inferred partition extents:"]
            for offset, length, label in self.boundaries():
                lines.append(
                    f"  0x{offset:08X} {length // 1024:>6} KiB  {label}"
                )
        if self.notes:
            lines += ["", *(f"note: {note}" for note in self.notes)]
        return "\n".join(lines)


def _uimage(data: bytes, offset: int) -> Finding:
    header = data[offset : offset + 64]
    if len(header) < 64:
        return Finding(offset, "uImage", "truncated header")
    size, load = struct.unpack_from(">II", header, 12)
    stored_crc = struct.unpack_from(">I", header, 4)[0]
    os_id, _arch, type_id, compression = struct.unpack_from(">BBBB", header, 28)
    name = header[32:64].split(b"\x00", 1)[0].decode("ascii", "replace")

    # The header CRC is computed with its own field zeroed.
    checkable = bytearray(header)
    checkable[4:8] = b"\x00\x00\x00\x00"
    crc_ok = zlib.crc32(bytes(checkable)) & 0xFFFFFFFF == stored_crc

    kind = UIMAGE_TYPES.get(type_id, f"type {type_id}")
    parts = [f'"{name}"', kind]
    if os_id in UIMAGE_OS:
        parts.append(UIMAGE_OS[os_id])
    if compression:
        parts.append(UIMAGE_COMPRESSION.get(compression, f"compression {compression}"))
    if load:
        parts.append(f"load 0x{load:08X}")
    if not crc_ok:
        parts.append("HEADER CRC MISMATCH")

    return Finding(
        offset=offset,
        kind="uImage",
        size=size + 64,
        description=", ".join(parts),
        detail={"type_id": type_id, "crc_ok": crc_ok, "name": name},
    )


def _squashfs(data: bytes, offset: int) -> Finding | None:
    if offset + 48 > len(data):
        return None
    inodes = struct.unpack_from("<I", data, offset + 4)[0]
    block_size = struct.unpack_from("<I", data, offset + 12)[0]
    bytes_used = struct.unpack_from("<Q", data, offset + 40)[0]
    # A plausible superblock, not four bytes that happen to spell hsqs.
    if not (0 < bytes_used <= len(data) and block_size in {1 << n for n in range(12, 21)}):
        return None
    return Finding(
        offset=offset,
        kind="squashfs",
        size=bytes_used,
        description=f"{inodes} inodes, {block_size // 1024} KiB blocks",
    )


def _gzip(data: bytes, offset: int) -> Finding:
    name = ""
    if offset + 10 < len(data) and data[offset + 3] & 0x08:  # FNAME
        end = data.find(b"\x00", offset + 10, offset + 10 + 256)
        if end > 0:
            name = data[offset + 10 : end].decode("ascii", "replace")
    return Finding(
        offset=offset,
        kind="gzip",
        description=f'compressed "{name}"' if name else "compressed data",
    )


def inspect_image(path: Path) -> ImageReport:
    """Scan a firmware file for the structures these cameras use."""
    path = Path(path)
    data = path.read_bytes()
    report = ImageReport(path=path, size=len(data))

    for match in re.finditer(re.escape(UIMAGE_MAGIC), data):
        report.findings.append(_uimage(data, match.start()))
    for match in re.finditer(re.escape(SQUASHFS_MAGIC), data):
        finding = _squashfs(data, match.start())
        if finding is not None:
            report.findings.append(finding)
    for match in re.finditer(re.escape(UBI_MAGIC), data):
        report.findings.append(Finding(match.start(), "ubi", "UBI volume"))

    # gzip appears inside many payloads; only the bootloader's own copy, near
    # the start, tells us anything about the layout.
    for match in re.finditer(re.escape(GZIP_MAGIC), data[: ERASE_BLOCK * 4]):
        report.findings.append(_gzip(data, match.start()))

    # JFFS2 is a stream of small nodes, so report only the first of each run.
    last_jffs2 = -ERASE_BLOCK
    for match in re.finditer(re.escape(JFFS2_MAGIC), data):
        offset = match.start()
        if offset - last_jffs2 >= ERASE_BLOCK and offset % 4 == 0:
            report.findings.append(Finding(offset, "jffs2", "filesystem"))
            last_jffs2 = offset

    report.findings.sort(key=lambda f: f.offset)

    if not report.is_chip_sized:
        nearest = min(KNOWN_CHIP_SIZES, key=lambda s: abs(s - report.size))
        report.notes.append(
            f"{report.size:,} bytes is not a whole chip size; nearest is "
            f"{nearest // (1024 * 1024)} MiB ({nearest:,})"
        )
    for finding in report.findings:
        if finding.kind in {"uImage", "squashfs"} and finding.offset % ERASE_BLOCK:
            report.notes.append(
                f"the {finding.kind} at 0x{finding.offset:X} is not on a "
                f"{ERASE_BLOCK // 1024} KiB erase block, so it is embedded in a "
                "partition rather than starting one"
            )
    return report
