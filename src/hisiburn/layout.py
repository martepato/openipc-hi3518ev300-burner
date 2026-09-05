"""Flash layouts: which image goes where, and how much to erase first.

A layout is the same information HiBurn reads out of a HiTool partition-table
XML — one entry per partition, with the offset it starts at, how much space it
owns, and the image that fills it.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

#: Names used for the bootloader slot. U-Boot's own mtdparts calls it "boot",
#: HiTool partition tables call it "fastboot".
BOOTLOADER_NAMES = frozenset({"boot", "fastboot", "uboot", "u-boot"})

#: NOR erase granularity on these cameras. Writes are padded up to it because
#: `sf write` cannot leave a partial block half-programmed.
ERASE_BLOCK = 64 * 1024


def round_up(value: int, multiple: int = ERASE_BLOCK) -> int:
    """Round ``value`` up to the next whole ``multiple``."""
    return int(math.ceil(value / multiple)) * multiple


class LayoutError(Exception):
    """A layout is internally inconsistent or does not fit its flash chip."""


@dataclass(frozen=True)
class Partition:
    """One region of the SPI NOR chip.

    ``image`` names the file that fills it. A partition with no image is
    erased and left blank — that is how HiBurn treats ``rootfs_data``.
    """

    name: str
    offset: int
    size: int
    image: str | None = None
    #: Other filenames that may hold this image. OpenIPC's U-Boot ships as
    #: `u-boot-<soc>-universal.bin`, but plenty of guides and older builds call
    #: it `u-boot.bin`, and either should just work.
    alternatives: tuple[str, ...] = ()
    #: Set for the bootloader partition, which HiBurn writes from the U-Boot
    #: already staged in RAM rather than from a separate download.
    from_staged_uboot: bool = False

    @property
    def candidates(self) -> tuple[str, ...]:
        """Filenames to look for, most canonical first."""
        return ((self.image,) if self.image else ()) + self.alternatives

    @property
    def end(self) -> int:
        return self.offset + self.size

    def __str__(self) -> str:
        source = self.image or ("staged U-Boot" if self.from_staged_uboot else "erase only")
        return f"{self.name:<12} 0x{self.offset:08x}  {self.size // 1024:>6} KiB  {source}"


@dataclass(frozen=True)
class FlashLayout:
    """An ordered, non-overlapping partition table for one flash chip."""

    name: str
    flash_size: int
    partitions: tuple[Partition, ...]
    #: Where images are staged in DRAM before being written to flash.
    staging_address: int = 0x41000000
    notes: str = ""

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        previous: Partition | None = None
        for partition in sorted(self.partitions, key=lambda p: p.offset):
            if partition.size <= 0:
                raise LayoutError(f"partition {partition.name} has a non-positive size")
            if previous is not None and partition.offset < previous.end:
                raise LayoutError(
                    f"partition {partition.name} at 0x{partition.offset:X} overlaps "
                    f"{previous.name} which ends at 0x{previous.end:X}"
                )
            if partition.end > self.flash_size:
                raise LayoutError(
                    f"partition {partition.name} ends at 0x{partition.end:X}, past the "
                    f"{self.flash_size // (1024 * 1024)} MiB flash chip"
                )
            previous = partition

    #: Names people use interchangeably for the same region. U-Boot's own
    #: mtdparts calls the bootloader slot "boot"; HiTool tables call it
    #: "fastboot".
    ALIASES = {"boot": "fastboot", "fastboot": "boot", "uboot": "fastboot"}

    def get(self, name: str) -> Partition:
        wanted = {name, self.ALIASES.get(name, name)}
        for partition in self.partitions:
            if partition.name in wanted:
                return partition
        raise LayoutError(
            f"no partition named {name!r} in layout {self.name!r} "
            f"(it has: {', '.join(p.name for p in self.partitions)})"
        )

    def resolve_names(self, names: set[str]) -> set[str]:
        """Map user-supplied partition names onto this layout's own spelling."""
        return {self.get(name).name for name in names}

    def check_fits(self, name: str, image_size: int) -> None:
        """Reject an image too big for its partition, before anything is erased."""
        partition = self.get(name)
        padded = round_up(image_size)
        if padded > partition.size:
            raise LayoutError(
                f"{name}: image is {image_size} bytes ({padded} after padding to a "
                f"{ERASE_BLOCK // 1024} KiB block) but the partition holds only "
                f"{partition.size} bytes"
            )

    def describe(self) -> str:
        lines = [f"{self.name} — {self.flash_size // (1024 * 1024)} MiB NOR"]
        if self.notes:
            lines.append(f"  {self.notes}")
        lines.extend(f"  {partition}" for partition in self.partitions)
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "flash_size": self.flash_size,
                "staging_address": self.staging_address,
                "notes": self.notes,
                "partitions": [
                    {
                        "name": p.name,
                        "offset": p.offset,
                        "size": p.size,
                        "image": p.image,
                        "alternatives": list(p.alternatives),
                        "from_staged_uboot": p.from_staged_uboot,
                    }
                    for p in self.partitions
                ],
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> FlashLayout:
        raw = json.loads(text)
        return cls(
            name=raw["name"],
            flash_size=raw["flash_size"],
            staging_address=raw.get("staging_address", 0x41000000),
            notes=raw.get("notes", ""),
            partitions=tuple(
                Partition(
                    name=p["name"],
                    offset=p["offset"],
                    size=p["size"],
                    image=p.get("image"),
                    alternatives=tuple(p.get("alternatives", ())),
                    from_staged_uboot=p.get("from_staged_uboot", False),
                )
                for p in raw["partitions"]
            ),
        )

    @classmethod
    def load(cls, path: Path) -> FlashLayout:
        return cls.from_json(Path(path).read_text())


#: The layout HiBurn used on a Xiaomi MJSXJ02HL (Hi3518EV300, 16 MiB
#: EN25QH128A), transcribed from a successful HiTool session.
MJSXJ02HL_16M = FlashLayout(
    name="mjsxj02hl-16m",
    flash_size=16 * 1024 * 1024,
    notes="Xiaomi MJSXJ02HL / Hi3518EV300, 16 MiB NOR (EN25QH128A)",
    partitions=(
        Partition(
            "fastboot", 0x000000, 0x040000,
            image="u-boot-hi3518ev300-universal.bin",
            alternatives=("u-boot.bin", "u-boot-hi3518ev300.bin"),
            from_staged_uboot=True,
        ),
        Partition("env", 0x040000, 0x010000, image="env.bin"),
        Partition("kernel", 0x050000, 0x300000, image="uImage.hi3518ev300"),
        Partition("rootfs", 0x350000, 0xA00000, image="rootfs.squashfs.hi3518ev300"),
        Partition("rootfs_data", 0xD50000, 0x2B0000),
    ),
)

BUILTIN_LAYOUTS: dict[str, FlashLayout] = {
    MJSXJ02HL_16M.name: MJSXJ02HL_16M,
}


def get_layout(name: str) -> FlashLayout:
    try:
        return BUILTIN_LAYOUTS[name]
    except KeyError:
        known = ", ".join(sorted(BUILTIN_LAYOUTS)) or "none"
        raise LayoutError(
            f"unknown layout {name!r} (built in: {known}). Pass a JSON layout with "
            "--layout-file, or derive one from a HiBurn log with `hisiburn from-log`."
        ) from None
