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

#: JFFS2 node types. Requiring one of these alongside the magic turns a
#: 16-bit signature into a 32-bit one, which is the difference between
#: matching real nodes and matching noise: a bare 0x1985 lands roughly once
#: per 64 KiB of random data, so on a 16 MiB image it appears everywhere.
JFFS2_NODETYPES = {
    0xE001: "dirent", 0xE002: "inode", 0x2003: "cleanmarker",
    0x2004: "padding", 0x2006: "summary", 0xE008: "xattr", 0xE009: "xref",
}

#: A node bigger than this is not a node; JFFS2 keeps them small.
JFFS2_MAX_NODE = 1 << 20
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
        covered_until = 0
        for finding in self.findings:
            # Anything sitting inside the previous structure's own extent is
            # its payload, not the start of a new partition.
            if finding.offset < covered_until:
                continue
            edge = finding.offset - (finding.offset % ERASE_BLOCK)
            label = finding.kind
            if finding.kind == "gzip" and edge == 0:
                label = "bootloader"
            covered_until = finding.end or finding.offset
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


def _jffs2_node(data: bytes, offset: int) -> tuple[int, int] | None:
    """Validate a JFFS2 node header, returning (nodetype, total length)."""
    if offset % 4 or offset + 12 > len(data):
        return None
    magic, nodetype, totlen = struct.unpack_from("<HHI", data, offset)
    if magic != 0x1985 or nodetype not in JFFS2_NODETYPES:
        return None
    if not (12 <= totlen <= JFFS2_MAX_NODE) or offset + totlen > len(data):
        return None
    return nodetype, totlen


def _jffs2_regions(data: bytes) -> list[Finding]:
    """Find JFFS2 areas, collapsing each run of nodes into one finding."""
    nodes: list[tuple[int, int, int]] = []
    for match in re.finditer(re.escape(JFFS2_MAGIC), data):
        node = _jffs2_node(data, match.start())
        if node is not None:
            nodes.append((match.start(), node[0], node[1]))

    regions: list[Finding] = []
    for offset, nodetype, totlen in nodes:
        # Nodes run back to back, and a region is usually padded out with
        # erased flash, so anything within an erase block of the last node
        # belongs to the region already open.
        if regions and offset - (regions[-1].detail["last_node_end"]) < ERASE_BLOCK:
            region = regions[-1]
            region.detail["nodes"] += 1
            region.detail["last_node_end"] = offset + totlen
            region.detail["types"].add(JFFS2_NODETYPES[nodetype])
            region.size = region.detail["last_node_end"] - region.offset
            region.description = (
                f"{region.detail['nodes']} nodes "
                f"({', '.join(sorted(region.detail['types']))})"
            )
            continue
        regions.append(
            Finding(
                offset=offset,
                kind="jffs2",
                size=totlen,
                description=f"1 node ({JFFS2_NODETYPES[nodetype]})",
                detail={
                    "nodes": 1,
                    "last_node_end": offset + totlen,
                    "types": {JFFS2_NODETYPES[nodetype]},
                },
            )
        )
    return regions


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

    for finding in _jffs2_regions(data):
        report.findings.append(finding)

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


def bootloader_from_dump(
    report: ImageReport, data: bytes, minimum: int = 0x6000
) -> bytes | None:
    """Pull the bootloader partition out of a whole-chip dump.

    Stage 1 needs a U-Boot to run before anything can be written, and a full
    dump necessarily carries the one that camera booted — the same mini-boot
    blob (SPL plus a compressed payload) that a standalone u-boot.bin is. So a
    dump can supply its own loader instead of the caller hunting for one.
    """
    if not report.is_full_dump:
        return None
    extents = report.boundaries()
    if not extents or extents[0][2] != "bootloader" or extents[0][0] != 0:
        return None

    blob = data[: extents[0][1]]
    # The slot is padded out with erased flash; trimming it keeps the upload
    # to what is actually the image, rounded back up so nothing real is lost
    # to a bootloader that happens to end in 0xFF.
    trimmed = len(blob.rstrip(b"\xff"))
    blob = blob[: min(len(blob), round_up_to(trimmed, 4096))]

    # Stage 1 slices the SPL off the front of this, so anything smaller than
    # that window is not a bootloader — an almost-empty boot slot, most
    # likely. Refusing here gives a clear message instead of a confusing one
    # from the loader.
    if len(blob) < minimum:
        return None
    return blob


def round_up_to(value: int, multiple: int) -> int:
    return -(-value // multiple) * multiple


# --- comparing two images ---------------------------------------------------


@dataclass(frozen=True)
class DiffRegion:
    """A contiguous run of erase blocks whose contents differ."""

    offset: int
    length: int

    @property
    def end(self) -> int:
        return self.offset + self.length


def compare_images(
    first: Path, second: Path, block: int = ERASE_BLOCK
) -> tuple[list[DiffRegion], int]:
    """Compare two images at erase-block granularity.

    Returns the differing regions and the number of blocks compared. Blocks
    are the right unit: flash can only be erased that way, so a difference
    anywhere in a block means the whole block has to be written differently.
    """
    left = Path(first).read_bytes()
    right = Path(second).read_bytes()
    span = max(len(left), len(right))

    regions: list[DiffRegion] = []
    blocks = 0
    for offset in range(0, span, block):
        blocks += 1
        if left[offset : offset + block] == right[offset : offset + block]:
            continue
        if regions and regions[-1].end == offset:
            regions[-1] = DiffRegion(regions[-1].offset, regions[-1].length + block)
        else:
            regions.append(DiffRegion(offset, block))
    return regions, blocks


def describe_comparison(first: Path, second: Path) -> str:
    """A readable account of how two firmware images differ."""
    first, second = Path(first), Path(second)
    left, right = inspect_image(first), inspect_image(second)
    regions, blocks = compare_images(first, second)

    lines = [f"{first.name}", f"{second.name}", ""]
    if left.size != right.size:
        lines.append(f"sizes differ: {left.size:,} vs {right.size:,} bytes")
    else:
        lines.append(f"both {left.size:,} bytes")

    if not regions:
        lines.append("identical")
        return "\n".join(lines)

    extents = left.boundaries() or right.boundaries()

    def partition_at(offset: int) -> str:
        for start, length, label in extents:
            if start <= offset < start + length:
                return f"{label} at 0x{start:07X}"
        return "outside any recognised partition"

    differing = sum(r.length for r in regions)
    lines.append(
        f"{len(regions)} differing region(s), {differing // 1024} KiB of "
        f"{blocks * ERASE_BLOCK // 1024} KiB "
        f"({differing / (blocks * ERASE_BLOCK) * 100:.2f}% of the chip)"
    )
    lines.append("")
    for region in regions:
        lines.append(
            f"  0x{region.offset:07X}..0x{region.end:07X}  "
            f"{region.length // 1024:>5} KiB  {partition_at(region.offset)}"
        )
    return "\n".join(lines)


# --- explaining a differing block -------------------------------------------


def _looks_like_uboot_env(data: bytes) -> bool:
    """A U-Boot environment: a CRC32, then NUL-separated key=value text."""
    body = data[4:]
    if b"=" not in body:
        return False
    printable = sum(1 for b in body if 32 <= b < 127 or b == 0)
    return printable >= len(body) * 0.9


def classify_block(data: bytes) -> str:
    """Say what a block of flash looks like, to explain why it differs.

    A mismatch is only alarming once you know what replaced what — the two
    that turn up routinely are the agent U-Boot's own environment and the
    camera's settings partition, and both are recognisable on sight.
    """
    if not data:
        return "unreadable"
    if set(data) == {0xFF}:
        return "erased flash"
    if set(data) == {0x00}:
        return "zeroed"
    if data[:4] == UIMAGE_MAGIC:
        return "uImage header"
    if data[:4] == SQUASHFS_MAGIC:
        return "squashfs superblock"
    if len(data) >= 12:
        magic, nodetype, _ = struct.unpack_from("<HHI", data, 0)
        if magic == 0x1985 and nodetype in JFFS2_NODETYPES:
            return f"JFFS2 {JFFS2_NODETYPES[nodetype]} node"
    if _looks_like_uboot_env(data):
        return "U-Boot environment"
    return "unrecognised content"


# --- what a U-Boot image can do ---------------------------------------------

#: Capabilities worth knowing about before pointing this tool at a camera,
#: and a string that is present in a build that has each. Help text is used
#: where a bare command name is too short to search for reliably.
UBOOT_CAPABILITIES: tuple[tuple[str, bytes, str], ...] = (
    ("usbtftp", b"usbtftp", "bulk flash read-back — fast backup"),
    ("crc32", b"checksum calculation", "on-device checksums — verify"),
    ("md", b"memory display", "memory dump — slow backup, and peek"),
    ("mw", b"memory write", "memory write — staging a flash write"),
    ("getinfo", b"getinfo", "HiSilicon burn-agent extensions"),
    ("burn agent", b"start download process.", "the USB download loop itself"),
)

_UBOOT_VERSION = re.compile(rb"U-Boot 20\d\d\.\d\d[^\x00\n]{0,40}")


def uboot_payload(data: bytes) -> bytes | None:
    """Decompress the U-Boot inside a mini-boot image.

    These images are an SPL followed by a gzip-compressed U-Boot, so almost
    nothing interesting is visible without inflating it first.
    """
    import zlib

    for offset in range(0, min(len(data), ERASE_BLOCK * 4) - 3):
        if data[offset : offset + 3] != GZIP_MAGIC:
            continue
        try:
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data[offset:])
        except zlib.error:
            continue
    return None


def inspect_uboot(data: bytes) -> dict | None:
    """Report a U-Boot image's version and the capabilities this tool cares about.

    Answers "can this U-Boot back up flash?" from the binary itself, which is
    the only authority — a config file in a source tree need not be the one a
    release was built from.
    """
    payload = uboot_payload(data)
    haystack = (payload or b"") + data
    match = _UBOOT_VERSION.search(haystack)
    if match is None:
        return None

    present = {}
    for name, needle, description in UBOOT_CAPABILITIES:
        # The gadget keeps some strings as UTF-16, so check both encodings.
        found = needle in haystack or needle.decode().encode("utf-16-le") in haystack
        present[name] = (found, description)
    return {
        "version": match.group().decode("ascii", "replace").strip(),
        "compressed": payload is not None,
        "capabilities": present,
    }
