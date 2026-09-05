"""Read HiTool/HiBurn partition-table XML into a flash layout.

A HiTool project ships its partition table as XML, and build systems that
target these cameras emit one alongside the images. It names every partition,
its offset, its length and the file that fills it — which is exactly a layout,
authored by whoever built the firmware. Reading it beats hardcoding filenames
that differ per build.

```xml
<Partition_Info ProgrammerFile="">
<Part Sel="1" PartitionName="fastboot" FlashType="spi" FileSystem="none"
      Start="0K" Length="256K" SelectFile="u-boot-hi3518ev300-universal.bin"/>
</Partition_Info>
```
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree

from hisiburn.layout import FlashLayout, Partition

#: Filenames a build is likely to use for its partition table.
CONVENTIONAL_NAMES = ("usb-burn.xml", "partition.xml", "burn.xml")

_SIZE = re.compile(r"^\s*(0x[0-9a-fA-F]+|\d+)\s*([KMG]?)B?\s*$", re.IGNORECASE)
_UNITS = {"": 1, "K": 1024, "M": 1024 * 1024, "G": 1024 * 1024 * 1024}

_DECLARATION = re.compile(rb"^\s*<\?xml[^>]*\?>")
_ENCODING = re.compile(rb'encoding\s*=\s*["\']([\w.-]+)["\']', re.IGNORECASE)


class XmlParseError(Exception):
    """The file is not a partition table we can read."""


def parse_size(text: str) -> int:
    """Parse HiTool's ``"256K"`` / ``"0x40000"`` / ``"65536"`` size notation."""
    match = _SIZE.match(text or "")
    if not match:
        raise XmlParseError(f"cannot read {text!r} as a size")
    value, unit = match.groups()
    return int(value, 16 if value.lower().startswith("0x") else 10) * _UNITS[unit.upper()]


def _chip_size_for(highest_end: int) -> int:
    """Round up to a plausible NOR chip size.

    A partial table — a rootfs-only one, say — does not reach the end of the
    chip, so the largest offset in it understates the part. NOR sizes are
    powers of two, so rounding up recovers the real one and keeps the
    chip-size check meaningful instead of spuriously failing.
    """
    size = 1024 * 1024
    while size < highest_end:
        size *= 2
    return size


def _read_xml(path: Path) -> ElementTree.Element:
    """Parse an XML file, tolerating the encodings HiTool actually writes.

    HiTool tables declare ``encoding="GB2312"``. Python's expat parser refuses
    any multi-byte encoding outright, so the file has to be decoded here and
    handed over as text — with its declaration removed, because ElementTree
    will not take a string that still carries one.
    """
    raw = path.read_bytes()

    declared = _ENCODING.search(raw[:200])
    encodings = [declared.group(1).decode("ascii", "replace")] if declared else []
    encodings += ["utf-8", "latin-1"]

    text: str | None = None
    for encoding in encodings:
        try:
            text = _DECLARATION.sub(b"", raw, count=1).decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:  # pragma: no cover - latin-1 decodes any byte string
        raise XmlParseError(f"{path}: could not decode as any of {encodings}")

    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise XmlParseError(f"{path} is not valid XML: {exc}") from exc


def layout_from_xml(path: Path, name: str | None = None) -> FlashLayout:
    """Build a layout from a HiTool partition-table XML."""
    path = Path(path)
    try:
        root = _read_xml(path)
    except OSError as exc:
        raise XmlParseError(f"cannot read {path}: {exc}") from exc

    entries = root.findall(".//Part")
    if not entries:
        raise XmlParseError(f"{path} has no <Part> elements — is it a partition table?")

    partitions = []
    for entry in entries:
        partition_name = entry.get("PartitionName")
        if not partition_name:
            raise XmlParseError(f"{path}: a <Part> element has no PartitionName")
        image = (entry.get("SelectFile") or "").strip() or None
        partitions.append(
            Partition(
                name=partition_name,
                offset=parse_size(entry.get("Start", "0")),
                size=parse_size(entry.get("Length", "0")),
                image=image,
                # A table that names a file for the bootloader slot means it,
                # so nothing here is written from whatever is left in RAM.
                from_staged_uboot=False,
            )
        )

    highest_end = max(p.end for p in partitions)
    return FlashLayout(
        name=name or path.stem,
        flash_size=_chip_size_for(highest_end),
        notes=f"partition table from {path.name}",
        partitions=tuple(partitions),
    )


def find_partition_table(directory: Path) -> Path | None:
    """Look for a partition table in a firmware directory."""
    directory = Path(directory)
    for candidate in CONVENTIONAL_NAMES:
        path = directory / candidate
        if path.is_file():
            return path
    # Fall back to any XML that parses as a partition table, but only if there
    # is exactly one — guessing between several would be worse than asking.
    tables = []
    for path in sorted(directory.glob("*.xml")):
        try:
            layout_from_xml(path)
        except (XmlParseError, OSError):
            continue
        tables.append(path)
    return tables[0] if len(tables) == 1 else None
