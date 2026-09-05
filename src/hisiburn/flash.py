"""Turning a layout plus a directory of images into a flashing session.

The command sequence mirrors what HiBurn does, because that sequence is known
to work on these cameras: pad the staging buffer, push the image into RAM,
probe the flash, erase the whole partition, then write the padded image.

Everything that can fail cheaply is checked before anything is erased. Once
the first ``sf erase`` goes out, the camera cannot boot again until the write
finishes, so a missing file or an oversized rootfs has to be caught up front.
"""

from __future__ import annotations

import hashlib
import logging
import re
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from hisiburn.agent import (
    AgentError,
    BurnAgent,
    erase_command,
    memset_command,
    probe_command,
    write_command,
)
from hisiburn.image import classify_block
from hisiburn.layout import (
    BOOTLOADER_NAMES,
    ERASE_BLOCK,
    FlashLayout,
    LayoutError,
    Partition,
    round_up,
)

log = logging.getLogger(__name__)

StepCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]


class PlanError(Exception):
    """The requested flash cannot be carried out as described."""


@dataclass(frozen=True)
class PartitionJob:
    """One partition's work, fully resolved and size-checked."""

    partition: Partition
    image_path: Path | None
    image_size: int

    @property
    def name(self) -> str:
        return self.partition.name

    @property
    def write_length(self) -> int:
        """Bytes handed to ``sf write``: the image padded to an erase block."""
        return round_up(self.image_size)

    @property
    def erase_only(self) -> bool:
        return self.image_path is None

    def summary(self) -> str:
        if self.erase_only:
            return f"{self.name}: erase 0x{self.partition.size:x} at 0x{self.partition.offset:x}"
        return (
            f"{self.name}: {self.image_path.name} ({self.image_size} bytes) "
            f"-> 0x{self.partition.offset:x}, writing 0x{self.write_length:x}"
        )


@dataclass(frozen=True)
class FlashPlan:
    """A validated, ordered set of partition jobs."""

    layout: FlashLayout
    jobs: tuple[PartitionJob, ...]

    @property
    def total_bytes(self) -> int:
        return sum(job.image_size for job in self.jobs)

    def describe(self) -> str:
        lines = [f"Plan for {self.layout.name} ({len(self.jobs)} partitions):"]
        lines.extend(f"  {job.summary()}" for job in self.jobs)
        lines.append(f"  total to transfer: {self.total_bytes / 1024 / 1024:.2f} MiB")
        return "\n".join(lines)

    def commands(self) -> Iterator[str]:
        """The U-Boot commands this plan will run, for a dry run."""
        staging = self.layout.staging_address
        for job in self.jobs:
            if not job.erase_only:
                yield memset_command(staging, 0xFF, job.write_length)
                yield f"<upload {job.image_path.name}: {job.image_size} bytes -> 0x{staging:x}>"
            yield probe_command()
            yield erase_command(job.partition.offset, job.partition.size)
            if not job.erase_only:
                yield write_command(staging, job.partition.offset, job.write_length)
        yield "reset"


def build_plan(
    layout: FlashLayout,
    directory: Path,
    only: set[str] | None = None,
    overrides: dict[str, Path] | None = None,
) -> FlashPlan:
    """Resolve a layout against real files, checking every size up front.

    ``only`` restricts the run to named partitions; ``overrides`` points a
    partition at a specific file instead of the layout's default name.
    """
    directory = Path(directory)
    overrides = overrides or {}

    if only:
        try:
            # "boot" and "fastboot" name the same region; accept either.
            only = layout.resolve_names(only)
        except LayoutError as exc:
            raise PlanError(str(exc)) from exc

    jobs: list[PartitionJob] = []
    missing: list[str] = []

    for partition in layout.partitions:
        if only and partition.name not in only:
            continue

        source: Path | None = None
        if partition.name in overrides:
            source = overrides[partition.name]
        elif partition.candidates:
            # Builds disagree on filenames -- OpenIPC ships U-Boot as
            # `u-boot-<soc>-universal.bin`, older guides as `u-boot.bin`.
            found = [directory / name for name in partition.candidates]
            source = next((path for path in found if path.is_file()), found[0])

        if source is None:
            jobs.append(PartitionJob(partition=partition, image_path=None, image_size=0))
            continue

        if not source.is_file():
            names = " or ".join(partition.candidates) or source.name
            missing.append(f"{partition.name}: {names}")
            continue

        size = source.stat().st_size
        if size == 0:
            raise PlanError(f"{partition.name}: {source} is empty")
        try:
            layout.check_fits(partition.name, size)
        except LayoutError as exc:
            raise PlanError(str(exc)) from exc

        jobs.append(PartitionJob(partition=partition, image_path=source, image_size=size))

    if missing:
        available = sorted(p.name for p in directory.iterdir() if p.is_file())[:12]
        raise PlanError(
            "missing image file(s):\n  "
            + "\n  ".join(missing)
            + f"\nLooked in {directory}, which holds: "
            + (", ".join(available) if available else "nothing")
            + "\nUse --image NAME=PATH to point a partition at a specific file."
        )

    if not jobs:
        raise PlanError("nothing to do — the selection matched no partitions")

    return FlashPlan(layout=layout, jobs=tuple(jobs))


def verify_flash_chip(agent: BurnAgent, layout: FlashLayout, on_step: StepCallback) -> None:
    """Compare the chip the camera reports against the layout's assumption.

    A mismatch is refused rather than warned about: an 8 MiB chip flashed with
    a 16 MiB layout silently loses the tail of the rootfs.
    """
    agent.flash_probe()
    info = agent.get_info("spi")
    on_step(f"flash: {' '.join(info.split())}")

    import re

    match = re.search(r"Chip:(\d+)MB", info)
    if not match:
        on_step("warning: could not read the flash size; continuing on the layout's word")
        return

    reported = int(match.group(1)) * 1024 * 1024
    if reported != layout.flash_size:
        raise PlanError(
            f"camera reports a {reported // (1024 * 1024)} MiB flash chip but layout "
            f"{layout.name!r} describes {layout.flash_size // (1024 * 1024)} MiB. "
            "Pick a matching layout, or derive one from a HiBurn log of this camera."
        )


def run_plan(
    agent: BurnAgent,
    plan: FlashPlan,
    on_step: StepCallback,
    on_progress: ProgressCallback | None = None,
    reset: bool = True,
) -> None:
    """Execute a plan against a live burn agent."""
    staging = plan.layout.staging_address

    for index, job in enumerate(plan.jobs, start=1):
        prefix = f"[{index}/{len(plan.jobs)}] {job.name}"

        if not job.erase_only:
            assert job.image_path is not None
            on_step(f"{prefix}: padding staging buffer to 0x{job.write_length:x}")
            agent.memset(staging, 0xFF, job.write_length)

            on_step(f"{prefix}: uploading {job.image_path.name} ({job.image_size} bytes)")
            agent.upload(job.image_path.read_bytes(), staging, on_progress=on_progress)

        agent.flash_probe()

        on_step(
            f"{prefix}: erasing 0x{job.partition.size:x} at 0x{job.partition.offset:x}"
        )
        agent.flash_erase(job.partition.offset, job.partition.size)

        if not job.erase_only:
            on_step(
                f"{prefix}: writing 0x{job.write_length:x} to 0x{job.partition.offset:x}"
            )
            agent.flash_write(staging, job.partition.offset, job.write_length)

        on_step(f"{prefix}: done")

    if reset:
        on_step("resetting the camera")
        agent.reset()


# --- checksum verification --------------------------------------------------

#: Digest files a build might ship next to its images, strongest first.
CHECKSUM_FILES = {"sha256sums.txt": hashlib.sha256, "md5sums.txt": hashlib.md5}

_DIGEST_LINE = re.compile(r"^([0-9a-fA-F]{32,64})\s+[* ]?(.+)$")


def find_checksums(directory: Path) -> tuple[Path, object] | None:
    """The strongest digest file a firmware directory ships, if any."""
    for name, algorithm in CHECKSUM_FILES.items():
        path = Path(directory) / name
        if path.is_file():
            return path, algorithm
    return None


def verify_checksums(plan: FlashPlan, directory: Path, on_step: StepCallback) -> None:
    """Check the images against a digest file the build shipped.

    A truncated download is the cheapest possible way to brick a camera, and
    the check costs a second. Images the digest file does not mention are left
    alone -- it is a manifest, not an allowlist.
    """
    found = find_checksums(directory)
    if found is None:
        on_step("no checksum file alongside the images; skipping verification")
        return
    path, algorithm = found

    expected: dict[str, str] = {}
    for line in path.read_text(errors="replace").splitlines():
        match = _DIGEST_LINE.match(line.strip())
        if match:
            expected[match.group(2).strip()] = match.group(1).lower()

    checked, mismatched = 0, []
    for job in plan.jobs:
        if job.image_path is None:
            continue
        want = expected.get(job.image_path.name)
        if want is None:
            continue
        digest = algorithm()
        with job.image_path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != want:
            mismatched.append(job.image_path.name)
        checked += 1

    if mismatched:
        raise PlanError(
            f"{path.name} does not match: {', '.join(mismatched)}. "
            "The image is corrupt or was rebuilt without refreshing the digests; "
            "flashing it would brick the camera. Re-download, or pass --no-verify "
            "if you know the digest file is stale."
        )
    if checked:
        on_step(f"{checked} image(s) match {path.name}")
    else:
        on_step(f"{path.name} lists none of these images; skipping verification")


# --- locating the U-Boot to run before flashing -----------------------------

#: Filenames a U-Boot image plausibly has, when a layout does not name one.
UBOOT_GLOBS = ("u-boot*.bin", "uboot*.bin", "fastboot*.bin")


def find_uboot(directory: Path, layout: FlashLayout | None = None) -> Path | None:
    """Find the U-Boot image in a firmware directory.

    Stage 1 needs a U-Boot to run before anything can be flashed, and a
    firmware set almost always ships the one it is about to install. Preferring
    the layout's own bootloader entry means using the name the build declared
    rather than guessing.
    """
    directory = Path(directory)

    if layout is not None:
        for partition in layout.partitions:
            if partition.name in BOOTLOADER_NAMES or partition.offset == 0:
                for candidate in partition.candidates:
                    path = directory / candidate
                    if path.is_file():
                        return path

    for pattern in UBOOT_GLOBS:
        matches = sorted(path for path in directory.glob(pattern) if path.is_file())
        if len(matches) == 1:
            return matches[0]
        if matches:
            # Several candidates and no layout to disambiguate: picking one at
            # random is worse than saying so.
            return None
    return None


# --- whole-chip restore -----------------------------------------------------

#: How much of a full-chip image to stage in DRAM at a time. The staging area
#: sits at 0x41000000 and the boards this targets have 64 MiB, so a few MiB is
#: comfortable while a whole 16 MiB image would not be.
RESTORE_CHUNK = 4 * 1024 * 1024


def run_restore(
    agent: BurnAgent,
    data: bytes,
    staging: int,
    on_step: StepCallback,
    on_progress: ProgressCallback | None = None,
    chunk_size: int = RESTORE_CHUNK,
    reset: bool = True,
) -> None:
    """Write a whole-flash image verbatim, a chunk at a time.

    A full dump is defined by its offsets, so it goes down exactly as it is
    rather than being split across a partition table — the table it was taken
    from is very likely not the one this tool knows about.

    Each chunk is erased immediately before it is written, which keeps the
    blank window short and means an interrupted restore has only lost the
    region it was working on.
    """
    if not data:
        raise PlanError("refusing to restore an empty image")
    if chunk_size % ERASE_BLOCK:
        raise PlanError(
            f"chunk size 0x{chunk_size:X} is not a multiple of the "
            f"{ERASE_BLOCK // 1024} KiB erase block"
        )

    agent.flash_probe()
    total = len(data)
    written = 0

    for offset in range(0, total, chunk_size):
        piece = data[offset : offset + chunk_size]
        # The tail of the image may not fill a whole erase block; pad it with
        # 0xFF so what lands in flash matches an erased chip rather than
        # whatever preceded it.
        padded = round_up(len(piece))
        piece = piece.ljust(padded, b"\xff")

        label = f"0x{offset:07X}..0x{offset + padded:07X}"
        on_step(f"{label}: erasing")
        agent.flash_erase(offset, padded)

        on_step(f"{label}: staging {len(piece):,} bytes")
        agent.memset(staging, 0xFF, padded)
        agent.upload(piece, staging, on_progress=on_progress)

        on_step(f"{label}: writing")
        agent.flash_write(staging, offset, padded)

        written += len(piece)
        on_step(f"{label}: done ({written / total * 100:.0f}% of the image)")

    if reset:
        on_step("resetting the camera")
        agent.reset()


# --- verifying flash against a local image ----------------------------------

#: How much to check at a time. `sf read` lands straight in DRAM without
#: going through U-Boot's small heap, so this is bounded by memory, not by
#: CONFIG_SYS_MALLOC_LEN the way a `usbtftp` read would be.
VERIFY_CHUNK = 4 * 1024 * 1024


@dataclass(frozen=True)
class Mismatch:
    """A region whose contents on the camera differ from the local image."""

    offset: int
    length: int
    expected: int
    actual: int
    #: What the camera actually has there, when it was narrow enough to look.
    content: str = ""

    def __str__(self) -> str:
        where = f"0x{self.offset:07X}..0x{self.offset + self.length:07X}"
        if self.content:
            return f"{where}  {self.content}"
        return f"{where}  expected {self.expected:08x}, camera has {self.actual:08x}"


def verify_against_image(
    agent: BurnAgent,
    data: bytes,
    staging: int,
    on_step: StepCallback,
    offset: int = 0,
    chunk_size: int = VERIFY_CHUNK,
    skip: int = 0,
    length: int | None = None,
    narrow_to: int | None = ERASE_BLOCK,
) -> list[Mismatch]:
    """Compare flash against ``data`` by checksum, without reading it back.

    Each chunk is read into DRAM and checksummed on the device; only the
    checksum crosses USB. That makes this usable on a U-Boot with no
    device-to-host bulk path at all, and fast — a whole 16 MiB chip is four
    round trips rather than eighty thousand.
    """
    if not data:
        raise PlanError("refusing to verify against an empty image")
    if skip >= len(data):
        raise PlanError(
            f"--skip 0x{skip:X} starts past the end of a {len(data):,}-byte image"
        )

    # A slice of the image is compared against the matching slice of flash, so
    # a single region can be checked without re-reading the whole chip.
    data = data[skip : None if length is None else skip + length]
    offset += skip

    agent.flash_probe()
    mismatches: list[Mismatch] = []

    for position in range(0, len(data), chunk_size):
        piece = data[position : position + chunk_size]
        flash_offset = offset + position
        label = f"0x{flash_offset:07X}..0x{flash_offset + len(piece):07X}"

        agent.flash_read(staging, flash_offset, len(piece))
        actual = agent.crc32(staging, len(piece))
        expected = zlib.crc32(piece) & 0xFFFFFFFF

        if actual == expected:
            on_step(f"{label}: ok ({expected:08x})")
        else:
            on_step(f"{label}: MISMATCH (expected {expected:08x}, got {actual:08x})")
            mismatches.append(Mismatch(flash_offset, len(piece), expected, actual))

    if narrow_to is None or chunk_size <= narrow_to:
        return [_explain(agent, m, staging) for m in mismatches]

    # A 4 MiB mismatch says nothing useful. Re-check just the failing ranges at
    # erase-block granularity, which is cheap and localises it exactly.
    narrowed: list[Mismatch] = []
    for coarse in mismatches:
        on_step(f"narrowing 0x{coarse.offset:07X} to {narrow_to // 1024} KiB blocks")
        start = coarse.offset - offset
        narrowed += verify_against_image(
            agent, data[start : start + coarse.length], staging, lambda _: None,
            offset=coarse.offset, chunk_size=narrow_to, narrow_to=None,
        )
    return narrowed


def _explain(agent: BurnAgent, mismatch: Mismatch, staging: int) -> Mismatch:
    """Look at what the camera actually has, so the report says something."""
    try:
        agent.flash_read(staging, mismatch.offset, 0x1000)
        head = agent.read_memory(staging, 64)
    except (AgentError, PlanError) as exc:
        log.debug("could not read 0x%X to explain it: %s", mismatch.offset, exc)
        return mismatch
    return replace(mismatch, content=classify_block(head))
