"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from hisiburn import version_string
from hisiburn.agent import AgentError, BurnAgent
from hisiburn.bootrom import PROFILES, BootRom, BootRomError
from hisiburn.flash import (
    BACKUP_CHUNK,
    BACKUP_CHUNK_BULK,
    RESTORE_CHUNK,
    VERIFY_CHUNK,
    PlanError,
    build_plan,
    find_uboot,
    run_backup,
    run_plan,
    run_restore,
    verify_against_image,
    verify_checksums,
    verify_flash_chip,
)
from hisiburn.hitool_log import LogParseError, layout_from_log, parse_sessions
from hisiburn.hitool_xml import XmlParseError, find_partition_table, layout_from_xml
from hisiburn.image import describe_comparison, inspect_image, inspect_uboot
from hisiburn.layout import BUILTIN_LAYOUTS, FlashLayout, LayoutError, get_layout
from hisiburn.usbdev import (
    BackendMissing,
    BulkPipe,
    DeviceNotFound,
    UsbError,
    find_device,
    list_devices,
    wait_for_device,
)

log = logging.getLogger("hisiburn")

#: Used only when nothing better is available: no --layout given, and no
#: partition table alongside the images.
DEFAULT_LAYOUT = "mjsxj02hl-16m"

#: Only one SoC family is supported so far; the flag exists for when that
#: changes, not because anyone needs to pass it today.
DEFAULT_CHIP = "hi3518ev300"

#: Every device command waits by default. Getting a camera into download mode
#: means unplugging it, holding reset and plugging back in, which no one can do
#: before the command starts. Waiting is the normal case; `--wait 0` opts out.
DEFAULT_WAIT = 30.0


class CliError(Exception):
    """Anything the user can fix, reported without a traceback."""


# --- output helpers ---------------------------------------------------------


def step(message: str) -> None:
    print(f"  {message}", flush=True)


class ProgressBar:
    """A single-line transfer meter that stays quiet when output is piped."""

    def __init__(self, label: str, enabled: bool = True):
        self.label = label
        self.enabled = enabled and sys.stdout.isatty()
        self._last = 0.0

    def __call__(self, sent: int, total: int) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if sent < total and now - self._last < 0.1:
            return
        self._last = now
        share = sent / total if total else 1.0
        filled = int(share * 32)
        bar = "#" * filled + "-" * (32 - filled)
        end = "\n" if sent >= total else ""
        print(
            f"\r  {self.label} [{bar}] {share * 100:5.1f}%  {sent // 1024} KiB",
            end=end,
            flush=True,
        )


# --- device helpers ---------------------------------------------------------


def open_pipe(product_id: int | None, wait: float) -> BulkPipe:
    if not wait:
        return BulkPipe(find_device(product_id))
    try:
        device = find_device(product_id)
    except DeviceNotFound:
        print(
            f"Waiting up to {wait:.0f}s for the camera — plug it in now, holding reset..."
        )
        device = wait_for_device(product_id, timeout=wait)
    return BulkPipe(device)


def start_agent(
    pipe: BulkPipe,
    uboot: bytes,
    profile: object,
    product_id: int | None,
) -> tuple[BulkPipe, BurnAgent]:
    """Run stage 1 on ``pipe``, then return a pipe onto the agent it starts.

    The camera re-enumerates when U-Boot takes over, so the pipe handed back is
    a new one — the caller's original is closed here.
    """
    previous = pipe.info.location
    try:
        BootRom(pipe).boot_uboot(uboot, profile, on_progress=ProgressBar("u-boot"))
    finally:
        pipe.close()

    # The loaded U-Boot comes back under the same USB id, so the handoff shows
    # up as a re-enumeration at a new address rather than a new product id.
    print("U-Boot started. Waiting for it to re-enumerate...")
    try:
        device = wait_for_device(product_id, timeout=20.0, exclude=previous)
    except DeviceNotFound:
        # A host can hand the device back its old address, which the exclusion
        # above would then skip forever. Fall back to whatever is attached.
        log.debug("no device at a new address; retrying without the exclusion")
        try:
            device = wait_for_device(product_id, timeout=5.0)
        except DeviceNotFound as exc:
            raise CliError(
                f"{exc}\nThe images went across but nothing came back. Check that "
                "this U-Boot was built with HiSilicon's usbtftp support."
            ) from exc

    new_pipe = BulkPipe(device)
    agent = BurnAgent(new_pipe)
    greeting = agent.wait_for_greeting()
    if greeting is None:
        new_pipe.close()
        raise CliError("the device re-enumerated but is not answering as a burn agent")
    print(f"  {greeting}")
    return new_pipe, agent


def _profile_for(chip: str) -> object:
    profile = PROFILES.get(chip)
    if profile is None:
        raise CliError(f"unknown chip {chip!r} (known: {', '.join(sorted(PROFILES))})")
    return profile


@dataclass(frozen=True)
class UbootImage:
    """A U-Boot to run on the boot ROM, and where it came from."""

    data: bytes
    label: str


#: Set this to a U-Boot once per shell and the commands that have no firmware
#: directory to search stop needing --uboot every time.
UBOOT_ENV_VAR = "HISIBURN_UBOOT"


def load_uboot(path: Path) -> UbootImage:
    if not path.is_file():
        raise CliError(f"U-Boot image not found: {path}")
    return UbootImage(path.read_bytes(), path.name)


def resolve_uboot(explicit: str | None, *directories: Path) -> UbootImage | None:
    """Find a U-Boot for stage 1, for commands pointed at no firmware set.

    `flash` has a directory full of images to look in; `info`, `run` and
    `verify` do not, so they fall back to an environment variable and the
    working directory rather than demanding --uboot on every invocation.
    """
    if explicit:
        return load_uboot(Path(explicit))
    from_env = os.environ.get(UBOOT_ENV_VAR)
    if from_env:
        return load_uboot(Path(from_env))
    for directory in (*directories, Path.cwd()):
        found = find_uboot(directory)
        if found is not None:
            return load_uboot(found)
    return None


def connect_agent(
    product_id: int | None,
    wait: float,
    uboot: UbootImage | None = None,
    chip: str = DEFAULT_CHIP,
) -> tuple[BulkPipe, BurnAgent]:
    """Open the camera and return a pipe onto a running burn agent.

    A camera fresh out of the reset-button dance is in its boot ROM, which
    cannot flash anything. Given a U-Boot image, stage 1 is run here rather
    than making the caller do it by hand — the image a firmware set installs
    is the same one the boot ROM needs, so there is nothing extra to supply.
    """
    pipe = open_pipe(product_id, wait)
    agent = BurnAgent(pipe)
    if agent.is_agent():
        return pipe, agent

    if uboot is None:
        pipe.close()
        raise CliError(
            "the device is attached but no burn agent answered. Both stages share "
            "the same USB id, so this is most likely the boot ROM still waiting "
            "for a download, and no U-Boot image was found to send it.\n"
            "Pass --uboot PATH, or set "
            f"{UBOOT_ENV_VAR}=/path/to/u-boot-<soc>-universal.bin once per shell. "
            "Any U-Boot for this SoC will do; it only runs from RAM."
        )

    print(
        f"Boot ROM is listening — loading {uboot.label} "
        f"({len(uboot.data)} bytes) first..."
    )
    return start_agent(pipe, uboot.data, _profile_for(chip), product_id)


# --- commands ---------------------------------------------------------------


def cmd_probe(args: argparse.Namespace) -> int:
    def scan() -> list:
        found = list_devices()
        if args.pid is not None:
            found = [d for d in found if d.product_id == args.pid]
        return found

    devices = scan()
    if not devices and args.wait:
        # The download-mode window is short, so the scan starts first and the
        # user does the unplug/hold-reset/replug dance while it runs.
        print(f"Waiting up to {args.wait:.0f}s — plug the camera in now (holding reset)...")
        deadline = time.monotonic() + args.wait
        while not devices and time.monotonic() < deadline:
            time.sleep(0.2)
            devices = scan()

    if not devices:
        print("No HiSilicon USB device found (vendor 12d1).")
        print()
        print("To put the camera into download mode:")
        print("  1. unplug it")
        print("  2. hold the reset button down")
        print("  3. plug the USB cable back in, still holding")
        print("  4. keep holding for a couple of seconds, then run this again")
        print()
        print("macOS needs no driver for this — if nothing appears, the camera is")
        print("not entering download mode rather than being unsupported.")
        return 1

    print(f"Found {len(devices)} HiSilicon device(s):")
    for device in devices:
        print(f"  {device}")

    if not args.verbose:
        return 0

    print()
    for device in devices:
        try:
            with BulkPipe(find_device(device.product_id)) as pipe:
                _describe_pipe(pipe)
        except (UsbError, DeviceNotFound) as exc:
            print(f"  could not open: {exc}")
    return 0


def _describe_pipe(pipe: BulkPipe) -> None:
    """Print endpoints and work out which stage is answering."""
    info = pipe.info
    print(f"{info.vendor_id:04x}:{info.product_id:04x} endpoints:")
    print(
        f"  bulk OUT 0x{pipe.ep_out.bEndpointAddress:02x} "
        f"maxpacket {pipe.ep_out.wMaxPacketSize}"
    )
    print(
        f"  bulk IN  0x{pipe.ep_in.bEndpointAddress:02x} "
        f"maxpacket {pipe.ep_in.wMaxPacketSize}"
    )

    agent = BurnAgent(pipe)

    # The banner is the only safe way to tell the stages apart: a command the
    # boot ROM does not implement gets no reply and leaves its OUT endpoint
    # un-armed, so probing with one would break the very session being probed.
    greeting = agent.wait_for_greeting()
    if greeting is not None:
        print(f"  greeting: {greeting!r}")
        try:
            print(f"  getinfo version: {agent.get_info('version')}")
        except (AgentError, UsbError) as exc:
            log.debug("getinfo failed on a device that greeted us: %s", exc)
        print("  -> burn agent, ready to flash")
        return

    print("  greeting: none (the burn agent always sends one)")
    if agent.ping():
        print("  session open (FE): acknowledged")
        print("  -> boot ROM, waiting for a download")
        print("     `hisiburn flash -d <firmware-dir>` will load U-Boot for you")
    else:
        print("  session open (FE): no reply")
        print("  -> nothing is answering. Power-cycle into download mode and retry.")


def cmd_info(args: argparse.Namespace) -> int:
    uboot = resolve_uboot(args.uboot)
    pipe, agent = connect_agent(args.pid, args.wait, uboot, args.chip)
    try:
        for topic in ("version", "bootmode", "spi"):
            value = " ".join(agent.get_info(topic).split())
            print(f"{topic:<10} {value}")
        # Capabilities come from the U-Boot image, not the device: a running
        # U-Boot cannot be asked what commands it has without wedging it.
        if uboot is not None:
            capabilities = inspect_uboot(uboot.data)
            if capabilities:
                found = capabilities["capabilities"]["usbtftp"][0]
                print(f"{'usbtftp':<10} {'yes' if found else 'no'} (from {uboot.label})")
                if not found:
                    print(f"{'':<10} flash read-back will use the slow path;")
                    print(f"{'':<10} see docs/AGENT-UBOOT.md")
    finally:
        pipe.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    pipe, agent = connect_agent(
        args.pid, args.wait, resolve_uboot(args.uboot), args.chip
    )
    try:
        for command in args.command:
            result = agent.try_command(command, timeout_ms=args.timeout * 1000)
            print(f"$ {command}")
            if result.output:
                print(result.output)
            print("OK" if result.ok else "ERROR")
            if not result.ok:
                return 1
    finally:
        pipe.close()
    return 0


def cmd_boot(args: argparse.Namespace) -> int:
    uboot = Path(args.file)
    if not uboot.is_file():
        raise CliError(f"U-Boot image not found: {uboot}")
    profile = _profile_for(args.chip)

    data = uboot.read_bytes()
    print(f"Loading {uboot.name} ({len(data)} bytes) via the boot ROM...")

    pipe = open_pipe(args.pid, args.wait)
    agent_pipe, agent = start_agent(pipe, data, profile, args.pid)
    with agent_pipe:
        print(f"Agent ready: {agent.get_info('version')}")
    return 0


def _resolve_layout(args: argparse.Namespace) -> tuple[FlashLayout, str]:
    """Pick a layout and say where it came from.

    A partition table shipped next to the images beats any built-in guess: it
    was written by whoever built that firmware and names its actual files.
    """
    if args.layout_file:
        path = Path(args.layout_file)
        if path.suffix.lower() == ".xml":
            return layout_from_xml(path), f"{path}"
        return FlashLayout.load(path), f"{path}"
    if args.from_log:
        return layout_from_log(Path(args.from_log)), f"HiBurn log {args.from_log}"
    if args.layout:
        return get_layout(args.layout), f"built-in layout {args.layout!r}"

    table = find_partition_table(Path(args.dir))
    if table is not None:
        return layout_from_xml(table), f"{table.name} in {args.dir}"
    return get_layout(DEFAULT_LAYOUT), f"built-in layout {DEFAULT_LAYOUT!r}"


def _parse_overrides(values: list[str] | None) -> dict[str, Path]:
    overrides: dict[str, Path] = {}
    for value in values or []:
        name, separator, path = value.partition("=")
        if not separator:
            raise CliError(f"--image expects NAME=PATH, got {value!r}")
        overrides[name] = Path(path)
    return overrides


def cmd_flash(args: argparse.Namespace) -> int:
    layout, source = _resolve_layout(args)
    print(f"Layout: {source}")
    plan = build_plan(
        layout,
        Path(args.dir),
        only=layout.resolve_names(set(args.only)) if args.only else None,
        overrides=_parse_overrides(args.image),
    )

    print(plan.describe())
    if not args.no_verify:
        verify_checksums(plan, Path(args.dir), step)

    if args.dry_run:
        print("\nCommands that would be sent:")
        for command in plan.commands():
            print(f"  {command}")
        return 0

    if not args.yes:
        print("\nThis erases the listed partitions. The camera cannot boot until it finishes.")
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    found = Path(args.uboot) if args.uboot else find_uboot(Path(args.dir), layout)
    uboot = load_uboot(found) if found is not None else None

    pipe, agent = connect_agent(args.pid, args.wait, uboot=uboot, chip=args.chip)
    try:
        print("\nFlashing:")
        verify_flash_chip(agent, layout, step)
        run_plan(
            agent,
            plan,
            on_step=step,
            on_progress=ProgressBar("upload"),
            reset=not args.no_reset,
        )
    finally:
        pipe.close()

    print("\nDone. The camera should reboot into the new firmware.")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    path = Path(args.image)
    if not path.is_file():
        raise CliError(f"image not found: {path}")

    if args.compare:
        other = Path(args.compare)
        if not other.is_file():
            raise CliError(f"image not found: {other}")
        print(describe_comparison(path, other))
        return 0

    # A whole-chip dump begins with a bootloader, so asking "is there a U-Boot
    # in this file" says nothing about what the file is — every dump answers
    # yes. Only a file that holds a bootloader and nothing else is a U-Boot.
    report = inspect_image(path)
    uboot = inspect_uboot(path.read_bytes()) if report.is_bootloader_only else None
    if uboot is None:
        print(report.describe())
        return 0

    print(f"{path.name}: {path.stat().st_size:,} bytes")
    print(f"verdict: a U-Boot image — {uboot['version']}")
    if uboot["compressed"]:
        print("         (SPL plus a gzip-compressed U-Boot; searched inflated)")
    print()
    print("capabilities:")
    for name, (found, description) in uboot["capabilities"].items():
        mark = "yes" if found else "NO "
        print(f"  {mark}  {name:<12} {description}")
    if not uboot["capabilities"]["usbtftp"][0]:
        print()
        print("Without usbtftp there is no bulk read-back, so `hisiburn backup`")
        print("falls back to md.b — correct and checksummed, but about two hours")
        print("for 16 MiB. See docs/AGENT-UBOOT.md.")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    path = Path(args.image)
    if not path.is_file():
        raise CliError(f"image not found: {path}")

    report = inspect_image(path)
    print(report.describe())
    print()

    if not report.is_full_dump and not args.force:
        raise CliError(
            "this does not look like a whole-chip dump, and restore writes an "
            "image verbatim from offset 0. Use `hisiburn flash` for a firmware "
            "set with a partition table, or --force if you are sure."
        )

    data = path.read_bytes()
    if args.dry_run:
        for offset in range(0, len(data), args.chunk):
            end = min(offset + args.chunk, len(data))
            print(f"  erase and write 0x{offset:07X}..0x{end:07X}")
        print("  reset")
        return 0

    if not args.yes:
        installing = (
            f" — installing {report.firmware.version}" if report.firmware else ""
        )
        print(
            f"This overwrites the ENTIRE {len(data) / 1024 / 1024:.0f} MiB chip, "
            f"bootloader included{installing}."
        )
        print(
            "Recovery if it goes wrong: hold reset while plugging in, then run "
            "`hisiburn flash` again — the boot ROM is in mask ROM and cannot be "
            "overwritten."
        )
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1

    pipe, agent = connect_agent(
        args.pid,
        args.wait,
        uboot=uboot_for_image(args.uboot, path, data, args.chip),
        chip=args.chip,
    )
    try:
        print("\nRestoring:")
        info = agent.get_info("spi")
        step(f"flash: {' '.join(info.split())}")
        import re as _re

        match = _re.search(r"Chip:(\d+)MB", info)
        if match:
            reported = int(match.group(1)) * 1024 * 1024
            if reported != len(data) and not args.force:
                raise PlanError(
                    f"the camera has a {reported // (1024 * 1024)} MiB chip but the "
                    f"image is {len(data) / 1024 / 1024:.2f} MiB. Restoring it would "
                    "leave the chip half-written; --force overrides."
                )
        run_restore(
            agent, data, staging=args.staging, on_step=step,
            on_progress=ProgressBar("upload"), chunk_size=args.chunk,
            reset=False,
        )

        mismatches = []
        if args.verify:
            # Verifying here, in the same session, is the only reading that
            # can be trusted: the agent U-Boot writes its own environment to
            # flash when it starts, so a later session damages the image
            # before it can measure it.
            print("\nVerifying, in the same session:")
            mismatches = verify_against_image(
                agent, data, staging=args.staging, on_step=step,
                chunk_size=args.chunk,
            )
        if not args.no_reset:
            step("resetting the camera")
            agent.reset()
    finally:
        pipe.close()

    if mismatches:
        print(f"\n{len(mismatches)} region(s) did not verify:")
        for mismatch in mismatches:
            print(f"  {mismatch}")
        return 1
    if args.verify:
        print("\nVerified: flash matches the image byte for byte.")
    print("\nDone. The camera should reboot into the restored firmware.")
    return 0


def uboot_for_image(
    explicit: str | None, image: Path, data: bytes, chip: str
) -> UbootImage | None:
    """Find a U-Boot for a command pointed at a single image file.

    `resolve_uboot`'s sources first — ``--uboot``, the environment variable, a
    loader beside the image or in the working directory — and then the image
    itself. That last source is what makes a whole-chip dump self-sufficient:
    it carries at offset 0 the bootloader the camera it came from was running,
    and stage 1 only ever runs a loader out of RAM, so the copy inside the dump
    serves as well as any other.

    Every command handed a whole image goes through here, so a dump that can be
    restored without ``--uboot`` can be verified and peeked at without one too.
    Nothing about the image changes between those commands; only which of them
    bothered to look would have.
    """
    found = resolve_uboot(explicit, Path(image).parent)
    if found is not None:
        return found

    from hisiburn.image import bootloader_from_dump

    embedded = bootloader_from_dump(
        inspect_image(Path(image)), data, minimum=_profile_for(chip).spl_size
    )
    if embedded is None:
        return None
    print(
        f"No separate U-Boot found; using the bootloader from the image itself "
        f"({len(embedded):,} bytes at offset 0)."
    )
    print(
        "  If the camera does not come back as a burn agent, pass --uboot with "
        "a U-Boot known to work on this SoC."
    )
    return UbootImage(embedded, "bootloader from the image")


def _hexdump(data: bytes, base: int, other: bytes | None = None) -> str:
    lines = []
    for position in range(0, len(data), 16):
        row = data[position : position + 16]
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        marker = ""
        if other is not None:
            expected = other[position : position + 16]
            marker = "  " if expected == row else "  <- differs"
        lines.append(
            f"  0x{base + position:08X}  {row.hex(' '):<47}  |{text:<16}|{marker}"
        )
    return "\n".join(lines)


def cmd_peek(args: argparse.Namespace) -> int:
    """Show the actual bytes at a flash offset, for narrowing a mismatch."""
    expected: bytes | None = None
    uboot: UbootImage | None = None
    if args.image:
        path = Path(args.image)
        if not path.is_file():
            raise CliError(f"image not found: {path}")
        blob = path.read_bytes()
        expected = blob[args.offset : args.offset + args.length]
        # A peek is usually narrowing a verify mismatch, so it is pointed at
        # the same image and can take a loader from it the same way.
        uboot = uboot_for_image(args.uboot, path, blob, args.chip)
    else:
        uboot = resolve_uboot(args.uboot)

    pipe, agent = connect_agent(args.pid, args.wait, uboot, args.chip)
    try:
        agent.flash_probe()
        agent.flash_read(args.staging, args.offset, max(args.length, 0x1000))
        data = agent.read_memory(args.staging, args.length)
    finally:
        pipe.close()

    print(f"\ncamera, flash 0x{args.offset:08X}:")
    print(_hexdump(data, args.offset, expected))
    if expected is not None:
        print(f"\n{Path(args.image).name}, same offset:")
        print(_hexdump(expected, args.offset, data))
        print()
        print("identical" if expected == data else "these differ")
    return 0


class Meter:
    """Progress with a rate and an estimate, for the operations that take minutes."""

    def __init__(self, label: str):
        self.label = label
        self.enabled = sys.stdout.isatty()
        self.started = time.monotonic()
        self._last = 0.0

    def __call__(self, done: int, total: int) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if done < total and now - self._last < 0.5:
            return
        self._last = now
        elapsed = max(now - self.started, 1e-6)
        rate = done / elapsed
        remaining = (total - done) / rate if rate else 0
        share = done / total if total else 1.0
        bar = "#" * int(share * 24) + "-" * (24 - int(share * 24))
        print(
            f"\r  {self.label} [{bar}] {share * 100:5.1f}%  "
            f"{done // 1024:>6} KiB  {rate / 1024:5.1f} KiB/s  "
            f"{remaining / 60:4.1f} min left ",
            end="\n" if done >= total else "",
            flush=True,
        )


#: Seconds per `md.b` round trip on the fallback path, measured end to end:
#: a 16 MiB chip took about two hours, which is 524,288 round trips. It is
#: host-dependent — the same exchange cost about 2 ms in a Windows capture —
#: so this is the macOS figure the estimate is shown to macOS users with.
DUMP_ROUND_TRIP_S = 0.0137


def cmd_backup(args: argparse.Namespace) -> int:
    destination = Path(args.output)
    resume_from = 0
    if destination.exists() and args.resume:
        resume_from = destination.stat().st_size - (
            destination.stat().st_size % args.chunk
        )
        print(f"Resuming {destination.name} at 0x{resume_from:X}")
    elif destination.exists() and not args.force:
        raise CliError(
            f"{destination} exists. Use --resume to continue it, or --force to "
            "start over."
        )

    uboot = resolve_uboot(args.uboot)
    pipe, agent = connect_agent(args.pid, args.wait, uboot, args.chip)
    try:
        info = agent.get_info("spi")
        step(f"flash: {' '.join(info.split())}")
        match = re.search(r"Chip:(\d+)MB", info)
        chip_size = int(match.group(1)) * 1024 * 1024 if match else None

        length = args.length
        if length is None:
            if chip_size is None:
                raise CliError(
                    "could not read the chip size; pass --length to say how much "
                    "to read"
                )
            length = chip_size - args.offset

        # Read the capability from the image we loaded, never from the device:
        # there is no safe way to ask a running U-Boot what commands it has.
        capabilities = inspect_uboot(uboot.data) if uboot else None
        has_usbtftp = bool(
            capabilities and capabilities["capabilities"]["usbtftp"][0]
        )
        bulk = has_usbtftp and not args.no_bulk
        chunk = args.chunk or (BACKUP_CHUNK_BULK if bulk else BACKUP_CHUNK)
        if bulk:
            step(f"{uboot.label} has usbtftp — using the bulk read path")
        else:
            why = (
                "no --uboot was given, so the running U-Boot's capabilities are "
                "unknown"
                if capabilities is None
                else f"{uboot.label} has no usbtftp"
            )
            minutes = -(-length // 32) * DUMP_ROUND_TRIP_S / 60
            print(
                f"{why}, so the read falls back to hex dumps:\n32 bytes per round "
                f"trip, about {minutes:.0f} minutes for {length:,} bytes. An agent "
                f"U-Boot reads the\nsame flash in well under a minute — see "
                "docs/AGENT-UBOOT.md."
            )

        mode = "r+b" if resume_from else "wb"
        with destination.open(mode) as handle:
            handle.seek(resume_from)
            written = run_backup(
                agent, handle, offset=args.offset, length=length,
                staging=args.staging, on_step=step,
                on_progress=Meter("read"), chunk_size=chunk,
                resume_from=resume_from, bulk=bulk,
            )
    finally:
        pipe.close()

    print(f"\nWrote {written:,} bytes to {destination}")
    print("Every chunk was checked against the device's own crc32.")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    path = Path(args.image)
    if not path.is_file():
        raise CliError(f"image not found: {path}")
    data = path.read_bytes()

    print(f"Verifying the camera against {path.name} ({len(data):,} bytes)")
    pipe, agent = connect_agent(
        args.pid, args.wait, uboot_for_image(args.uboot, path, data, args.chip), args.chip
    )
    try:
        print()
        mismatches = verify_against_image(
            agent, data, staging=args.staging, on_step=step,
            offset=args.offset, chunk_size=args.chunk,
            skip=args.skip, length=args.length,
        )
    finally:
        pipe.close()

    print()
    if not mismatches:
        print(f"Match: the camera's flash is byte-identical to {path.name}.")
        return 0
    print(f"{len(mismatches)} region(s) differ:")
    for mismatch in mismatches:
        print(f"  {mismatch}")
    return 1


def cmd_from_log(args: argparse.Namespace) -> int:
    path = Path(args.log)
    sessions = parse_sessions(path.read_text(errors="replace"))
    if not sessions:
        raise CliError(f"no flashing session found in {path}")

    if args.list:
        print(f"{len(sessions)} session(s) in {path}:")
        for index, session in enumerate(sessions):
            names = ", ".join(job.name or "?" for job in session.jobs)
            print(f"  [{index}] chip={session.flash_chip or '?'} partitions: {names}")
        return 0

    layout = layout_from_log(path, session_index=args.session, name=args.name)
    if args.output:
        Path(args.output).write_text(layout.to_json())
        print(f"Wrote {args.output}")
    else:
        print(layout.describe())
        print()
        print(layout.to_json())
    return 0


def cmd_layouts(args: argparse.Namespace) -> int:
    if not BUILTIN_LAYOUTS:
        print("No built-in layouts.")
        return 0
    for layout in BUILTIN_LAYOUTS.values():
        print(layout.describe())
        print()
    return 0


# --- argument parsing -------------------------------------------------------


#: Defaults for the options accepted on either side of the subcommand. They
#: are applied after parsing rather than through ``set_defaults``: ``parents=``
#: shares one action object between the top-level parser and every subparser,
#: so setting a default on the parser would also overwrite it on the copies and
#: undo the SUPPRESS that keeps them from clobbering each other.
SHARED_DEFAULTS = {"verbose": False, "pid": None, "wait": DEFAULT_WAIT}


def _shared_options() -> tuple[
    argparse.ArgumentParser, argparse.ArgumentParser, argparse.ArgumentParser
]:
    """Options accepted on either side of the subcommand.

    Each is attached to the top-level parser *and* to every subcommand that
    wants it, so both `hisiburn -v probe` and `hisiburn probe -v` work. The
    copies use SUPPRESS defaults: without that, a subparser's default would
    overwrite a value already given before the subcommand.
    """
    general = argparse.ArgumentParser(add_help=False)
    general.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
        help="log protocol detail",
    )

    device = argparse.ArgumentParser(add_help=False)
    device.add_argument(
        "--pid", type=lambda value: int(value, 0), default=argparse.SUPPRESS,
        help="override the USB product id to open",
    )
    device.add_argument(
        "--wait", type=float, default=argparse.SUPPRESS, metavar="SECONDS",
        help=f"seconds to wait for the camera to appear (default {DEFAULT_WAIT:.0f}; "
             "0 fails immediately)",
    )
    # Commands that can start the burn agent themselves when they find the
    # boot ROM instead.
    booting = argparse.ArgumentParser(add_help=False)
    booting.add_argument(
        "--uboot", metavar="PATH",
        help="U-Boot to load if the camera is still in its boot ROM "
             "(default: $HISIBURN_UBOOT, a U-Boot beside the image or in the "
             "working directory, or the one inside a whole-chip dump)",
    )
    booting.add_argument(
        "-c", "--chip", default=DEFAULT_CHIP,
        help=f"chip profile for the boot ROM stage (default {DEFAULT_CHIP})",
    )
    return general, device, booting


def build_parser() -> argparse.ArgumentParser:
    general, device, booting = _shared_options()

    parser = argparse.ArgumentParser(
        prog="hisiburn",
        parents=[general, device],
        description=(
            "Flash HiSilicon Hi3518EV300 cameras over USB from macOS or Linux. "
            "An open replacement for HiTool/HiBurn that needs no driver install."
        ),
    )
    parser.add_argument("--version", action="version", version=version_string())

    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser(
        "probe", parents=[general, device], help="list attached HiSilicon devices"
    )
    probe.set_defaults(func=cmd_probe)

    info = sub.add_parser(
        "info", parents=[general, device, booting], help="query a running burn agent"
    )
    info.set_defaults(func=cmd_info)

    run = sub.add_parser(
        "run", parents=[general, device, booting],
        help="run U-Boot console commands through the agent",
    )
    run.add_argument("command", nargs="+", help="commands to run in order")
    run.add_argument("--timeout", type=int, default=30, help="seconds per command")
    run.set_defaults(func=cmd_run)

    boot = sub.add_parser(
        "boot", parents=[general, device],
        help="load and start U-Boot via the boot ROM (flash does this for you)",
    )
    boot.add_argument("-f", "--file", required=True, help="U-Boot binary")
    boot.add_argument("-c", "--chip", default=DEFAULT_CHIP, help="chip profile")
    boot.set_defaults(func=cmd_boot)

    flash = sub.add_parser(
        "flash", parents=[general, device, booting],
        help="write a firmware set to the camera's flash",
    )
    flash.add_argument("-d", "--dir", default=".", help="directory holding the images")
    flash.add_argument(
        "-l", "--layout",
        help=f"built-in layout name (default: the partition table found next to "
             f"the images, else {DEFAULT_LAYOUT!r})",
    )
    flash.add_argument(
        "--layout-file",
        help="partition table to use: a HiTool .xml, or JSON from `from-log -o`",
    )
    flash.add_argument("--from-log", help="derive the layout from a HiBurn log")
    flash.add_argument(
        "--only", nargs="+", metavar="NAME", help="flash only these partitions"
    )
    flash.add_argument(
        "--image",
        action="append",
        metavar="NAME=PATH",
        help="use a specific file for a partition",
    )
    flash.add_argument("--dry-run", action="store_true", help="print commands, touch nothing")
    flash.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    flash.add_argument("--no-reset", action="store_true", help="leave the camera halted")
    flash.add_argument(
        "--no-verify", action="store_true",
        help="skip checking the images against a shipped md5sums/sha256sums file",
    )
    flash.set_defaults(func=cmd_flash)

    inspect = sub.add_parser(
        "inspect", parents=[general],
        help="say what a firmware .bin actually is, without writing anything",
    )
    inspect.add_argument("image", help="firmware file to examine")
    inspect.add_argument(
        "--compare", metavar="OTHER",
        help="diff against another image, by erase block, instead of describing this one",
    )
    inspect.set_defaults(func=cmd_inspect)

    restore = sub.add_parser(
        "restore", parents=[general, device, booting],
        help="write a whole-chip dump back verbatim",
    )
    restore.add_argument("image", help="full flash image")
    restore.add_argument(
        "--chunk", type=lambda v: int(v, 0), default=RESTORE_CHUNK,
        metavar="BYTES", help="how much to stage at a time (default 4 MiB)",
    )
    restore.add_argument(
        "--staging", type=lambda v: int(v, 0), default=0x41000000,
        metavar="ADDR", help="DRAM address to stage through",
    )
    restore.add_argument(
        "--verify", action="store_true",
        help="check the write in the same session, before anything reboots "
             "(the only reading that can be trusted — see the README)",
    )
    restore.add_argument("--dry-run", action="store_true", help="print the plan only")
    restore.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    restore.add_argument("--no-reset", action="store_true", help="leave the camera halted")
    restore.add_argument(
        "--force", action="store_true",
        help="write even if the image is not a recognisable whole-chip dump, "
             "or does not match the chip size",
    )
    restore.set_defaults(func=cmd_restore)

    peek = sub.add_parser(
        "peek", parents=[general, device, booting],
        help="show the bytes at a flash offset, optionally against a local image",
    )
    peek.add_argument("offset", type=lambda v: int(v, 0), help="flash offset")
    peek.add_argument(
        "-n", "--length", type=lambda v: int(v, 0), default=64, metavar="BYTES",
        help="how many bytes to show (default 64; this path is slow, keep it small)",
    )
    peek.add_argument("--image", help="local image to compare the same offset against")
    peek.add_argument(
        "--staging", type=lambda v: int(v, 0), default=0x41000000, metavar="ADDR",
        help="DRAM address to read through",
    )
    peek.set_defaults(func=cmd_peek)

    backup = sub.add_parser(
        "backup", parents=[general, device, booting],
        help="read flash back to a file (slow: no bulk path without usbtftp)",
    )
    backup.add_argument("output", help="file to write")
    backup.add_argument(
        "--offset", type=lambda v: int(v, 0), default=0, metavar="ADDR",
        help="flash offset to start at (default 0)",
    )
    backup.add_argument(
        "--length", type=lambda v: int(v, 0), metavar="BYTES",
        help="how much to read (default: to the end of the chip)",
    )
    backup.add_argument(
        "--chunk", type=lambda v: int(v, 0), metavar="BYTES",
        help="how much to read and checksum at a time "
             "(default 1 MiB over usbtftp, 64 KiB without it)",
    )
    backup.add_argument(
        "--no-bulk", action="store_true",
        help="force the slow hex-dump path even where usbtftp is available",
    )
    backup.add_argument(
        "--staging", type=lambda v: int(v, 0), default=0x41000000, metavar="ADDR",
        help="DRAM address to read through",
    )
    backup.add_argument("--resume", action="store_true", help="continue an interrupted run")
    backup.add_argument("--force", action="store_true", help="overwrite an existing file")
    backup.set_defaults(func=cmd_backup)

    verify = sub.add_parser(
        "verify", parents=[general, device, booting],
        help="check the camera's flash against a local image, by checksum",
    )
    verify.add_argument("image", help="image the flash should match")
    verify.add_argument(
        "--offset", type=lambda v: int(v, 0), default=0, metavar="ADDR",
        help="flash offset the image starts at (default 0)",
    )
    verify.add_argument(
        "--chunk", type=lambda v: int(v, 0), default=VERIFY_CHUNK, metavar="BYTES",
        help="how much to check at a time (default 4 MiB)",
    )
    verify.add_argument(
        "--staging", type=lambda v: int(v, 0), default=0x41000000, metavar="ADDR",
        help="DRAM address to read through",
    )
    verify.add_argument(
        "--skip", type=lambda v: int(v, 0), default=0, metavar="BYTES",
        help="ignore this much from the start of the image (and of flash)",
    )
    verify.add_argument(
        "--length", type=lambda v: int(v, 0), metavar="BYTES",
        help="verify only this many bytes, so one region can be checked alone",
    )
    verify.set_defaults(func=cmd_verify)

    from_log = sub.add_parser(
        "from-log", parents=[general],
        help="recover a flash layout from a HiBurn log",
    )
    from_log.add_argument("log", help="HiTool/HiBurn log file")
    from_log.add_argument("--session", type=int, default=-1, help="which session (default: last)")
    from_log.add_argument("--name", default="from-log", help="name for the layout")
    from_log.add_argument("-o", "--output", help="write the layout as JSON")
    from_log.add_argument("--list", action="store_true", help="list sessions and stop")
    from_log.set_defaults(func=cmd_from_log)

    layouts = sub.add_parser(
        "layouts", parents=[general], help="show the built-in flash layouts"
    )
    layouts.set_defaults(func=cmd_layouts)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for name, default in SHARED_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        return args.func(args)
    except (
        BackendMissing,
        CliError,
        XmlParseError,
        PlanError,
        LayoutError,
        LogParseError,
        DeviceNotFound,
        AgentError,
        BootRomError,
        UsbError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
