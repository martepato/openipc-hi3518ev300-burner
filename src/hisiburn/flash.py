"""Turning a layout plus a directory of images into a flashing session.

The command sequence mirrors what HiBurn does, because that sequence is known
to work on these cameras: pad the staging buffer, push the image into RAM,
probe the flash, erase the whole partition, then write the padded image.

Everything that can fail cheaply is checked before anything is erased. Once
the first ``sf erase`` goes out, the camera cannot boot again until the write
finishes, so a missing file or an oversized rootfs has to be caught up front.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from hisiburn.agent import BurnAgent
from hisiburn.layout import FlashLayout, LayoutError, Partition, round_up

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
            return f"{self.name}: erase 0x{self.partition.size:X} at 0x{self.partition.offset:X}"
        return (
            f"{self.name}: {self.image_path.name} ({self.image_size} bytes) "
            f"-> 0x{self.partition.offset:X}, writing 0x{self.write_length:X}"
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
                yield f"mw.b 0x{staging:X} 0xFF 0x{job.write_length:X}"
                yield f"<upload {job.image_path.name}: {job.image_size} bytes -> 0x{staging:X}>"
            yield "sf probe 0"
            yield f"sf erase 0x{job.partition.offset:X} 0x{job.partition.size:X}"
            if not job.erase_only:
                yield (
                    f"sf write 0x{staging:X} 0x{job.partition.offset:X} "
                    f"0x{job.write_length:X}"
                )
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
        unknown = only - {p.name for p in layout.partitions}
        if unknown:
            raise PlanError(
                f"layout {layout.name!r} has no partition(s): {', '.join(sorted(unknown))}"
            )

    jobs: list[PartitionJob] = []
    missing: list[str] = []

    for partition in layout.partitions:
        if only and partition.name not in only:
            continue

        source: Path | None = None
        if partition.name in overrides:
            source = overrides[partition.name]
        elif partition.image:
            source = directory / partition.image

        if source is None:
            jobs.append(PartitionJob(partition=partition, image_path=None, image_size=0))
            continue

        if not source.is_file():
            missing.append(f"{partition.name}: {source}")
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
        raise PlanError(
            "missing image file(s):\n  "
            + "\n  ".join(missing)
            + f"\nLooked in {directory}. Use --image NAME=PATH to point at a file "
            "with a different name."
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
            on_step(f"{prefix}: padding staging buffer to 0x{job.write_length:X}")
            agent.memset(staging, 0xFF, job.write_length)

            on_step(f"{prefix}: uploading {job.image_path.name} ({job.image_size} bytes)")
            agent.upload(job.image_path.read_bytes(), staging, on_progress=on_progress)

        agent.flash_probe()

        on_step(
            f"{prefix}: erasing 0x{job.partition.size:X} at 0x{job.partition.offset:X}"
        )
        agent.flash_erase(job.partition.offset, job.partition.size)

        if not job.erase_only:
            on_step(
                f"{prefix}: writing 0x{job.write_length:X} to 0x{job.partition.offset:X}"
            )
            agent.flash_write(staging, job.partition.offset, job.write_length)

        on_step(f"{prefix}: done")

    if reset:
        on_step("resetting the camera")
        agent.reset()
