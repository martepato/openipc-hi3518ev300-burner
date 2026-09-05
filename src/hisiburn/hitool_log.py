"""Recover a flash layout from a HiBurn session log.

A successful HiTool run prints every console command it sent. That transcript
is a complete, device-specific partition table — offsets, sizes and image
names included — which makes an old log the most reliable source of a layout
for a camera nobody has written a profile for yet.

The parser is deliberately forgiving: it looks for the handful of lines that
carry structure and ignores everything else, so progress spinners, localised
banners and truncated tails do not derail it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from hisiburn.layout import FlashLayout, Partition

_ERASE = re.compile(r"sf\s+erase\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)")
_WRITE = re.compile(
    r"sf\s+write\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)"
)
_MEMSET = re.compile(r"mw\.b\s+(0x[0-9a-fA-F]+)\s+0x[0-9a-fA-F]+\s+(0x[0-9a-fA-F]+)")
_DOWNLOAD = re.compile(r"Downloading File\s*:\s*(\S+)")
_FILE_LENGTH = re.compile(r"fileLength\s*=\s*(\d+)")
_ADDRESS = re.compile(r"address\s*=\s*(0x[0-9a-fA-F]+)")
_BURNT = re.compile(r"Partition\s+(\S+)\s+burnt successfully")
_CHIP = re.compile(r"Chip:(\d+)MB")
_CHIP_NAME = re.compile(r'Name:"([^"]+)"')
_UBOOT = re.compile(r"version:\s*(U-Boot\s+\S+)")


class LogParseError(Exception):
    """The log contained no recognisable flashing session."""


@dataclass
class _Job:
    """One partition's worth of commands, accumulated as the log is read."""

    image: str | None = None
    image_length: int | None = None
    staging_address: int | None = None
    erase_offset: int | None = None
    erase_length: int | None = None
    write_offset: int | None = None
    write_length: int | None = None
    name: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.erase_offset is not None and self.erase_length is not None


@dataclass
class Session:
    """One flashing run found in a log."""

    jobs: list[_Job] = field(default_factory=list)
    flash_size: int | None = None
    flash_chip: str | None = None
    uboot_version: str | None = None

    @property
    def is_usable(self) -> bool:
        return any(job.is_complete for job in self.jobs)


def parse_sessions(text: str) -> list[Session]:
    """Split a log into sessions and pull the structure out of each."""
    sessions: list[Session] = []
    session = Session()
    job = _Job()

    def flush_job() -> None:
        nonlocal job
        if job.is_complete:
            session.jobs.append(job)
        job = _Job()

    def flush_session() -> None:
        nonlocal session
        flush_job()
        if session.is_usable:
            sessions.append(session)
        session = Session()

    for line in text.splitlines():
        if "Boot download completed" in line and session.is_usable:
            flush_session()

        if match := _CHIP.search(line):
            session.flash_size = int(match.group(1)) * 1024 * 1024
        if match := _CHIP_NAME.search(line):
            session.flash_chip = match.group(1)
        if match := _UBOOT.search(line):
            session.uboot_version = match.group(1)

        if match := _MEMSET.search(line):
            # A staging-buffer fill always opens a new partition's sequence.
            flush_job()
            job.staging_address = int(match.group(1), 16)
            continue

        if match := _DOWNLOAD.search(line):
            job.image = match.group(1)
            continue

        if match := _FILE_LENGTH.search(line):
            job.image_length = int(match.group(1))
            continue

        if match := _ADDRESS.search(line):
            job.staging_address = int(match.group(1), 16)
            continue

        if match := _ERASE.search(line):
            if job.erase_offset is not None:
                flush_job()
            job.erase_offset = int(match.group(1), 16)
            job.erase_length = int(match.group(2), 16)
            continue

        if match := _WRITE.search(line):
            job.staging_address = int(match.group(1), 16)
            job.write_offset = int(match.group(2), 16)
            job.write_length = int(match.group(3), 16)
            continue

        if match := _BURNT.search(line):
            job.name = match.group(1)
            flush_job()
            continue

    flush_session()
    return sessions


def _name_for(job: _Job, index: int) -> str:
    if job.name:
        return job.name
    if job.erase_offset == 0:
        # HiBurn does not print a name for the bootloader slot; HiTool tables
        # and U-Boot's own mtdparts call it "fastboot" and "boot" respectively.
        return "fastboot"
    return f"part{index}_0x{job.erase_offset:x}"


def session_to_layout(session: Session, name: str = "from-log") -> FlashLayout:
    """Turn a parsed session into a validated :class:`FlashLayout`."""
    jobs = [job for job in session.jobs if job.is_complete]
    if not jobs:
        raise LogParseError("session contained no erase commands to build a layout from")

    flash_size = session.flash_size or max(
        (job.erase_offset or 0) + (job.erase_length or 0) for job in jobs
    )

    partitions = []
    for index, job in enumerate(jobs):
        assert job.erase_offset is not None and job.erase_length is not None
        # A partition written straight from the staging buffer with nothing
        # downloaded into it first is the boot slot, filled from the U-Boot
        # the boot ROM already put in RAM.
        from_staged = job.image is None and job.write_length is not None
        partitions.append(
            Partition(
                name=_name_for(job, index),
                offset=job.erase_offset,
                size=job.erase_length,
                image=job.image or ("u-boot.bin" if from_staged else None),
                from_staged_uboot=from_staged,
            )
        )

    notes_parts = [part for part in (session.flash_chip, session.uboot_version) if part]
    return FlashLayout(
        name=name,
        flash_size=flash_size,
        staging_address=next(
            (job.staging_address for job in jobs if job.staging_address), 0x41000000
        ),
        notes="recovered from HiBurn log"
        + (f" ({', '.join(notes_parts)})" if notes_parts else ""),
        partitions=tuple(partitions),
    )


def layout_from_log(path: Path, session_index: int = -1, name: str = "from-log") -> FlashLayout:
    """Build a layout from the chosen session of a HiBurn log (default: the last)."""
    sessions = parse_sessions(Path(path).read_text(errors="replace"))
    if not sessions:
        raise LogParseError(
            f"{path} has no recognisable flashing session — expected lines like "
            "'sf erase 0x... 0x...' and 'Partition <name> burnt successfully!'"
        )
    try:
        session = sessions[session_index]
    except IndexError:
        raise LogParseError(
            f"log has {len(sessions)} session(s); index {session_index} is out of range"
        ) from None
    return session_to_layout(session, name=name)
