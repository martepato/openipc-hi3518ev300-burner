"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from hisiburn import __version__
from hisiburn.agent import AgentError, BurnAgent
from hisiburn.bootrom import PROFILES, BootRom, BootRomError
from hisiburn.flash import (
    PlanError,
    build_plan,
    find_uboot,
    run_plan,
    verify_checksums,
    verify_flash_chip,
)
from hisiburn.hitool_log import LogParseError, layout_from_log, parse_sessions
from hisiburn.hitool_xml import XmlParseError, find_partition_table, layout_from_xml
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
    greeting = agent.read_greeting()
    if greeting:
        print(f"  {greeting}")
    if not agent.is_agent():
        new_pipe.close()
        raise CliError("the device re-enumerated but is not answering as a burn agent")
    return new_pipe, agent


def _profile_for(chip: str) -> object:
    profile = PROFILES.get(chip)
    if profile is None:
        raise CliError(f"unknown chip {chip!r} (known: {', '.join(sorted(PROFILES))})")
    return profile


def connect_agent(
    product_id: int | None,
    wait: float,
    uboot: Path | None = None,
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
    agent.read_greeting()
    if agent.is_agent():
        return pipe, agent

    if uboot is None:
        pipe.close()
        raise CliError(
            "the device is attached but no burn agent answered. Both stages share "
            "the same USB id, so this is most likely the boot ROM still waiting "
            "for a download, and no U-Boot image was found to send it. Pass "
            "--uboot PATH, or run `hisiburn boot -f <u-boot.bin>` first."
        )

    data = uboot.read_bytes()
    print(f"Boot ROM is listening — loading {uboot.name} ({len(data)} bytes) first...")
    return start_agent(pipe, data, _profile_for(chip), product_id)


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
    greeting = agent.read_greeting()
    if greeting:
        print(f"  greeting: {greeting!r}")

    # OPEN is the one frame both stages accept before anything else. The boot
    # ROM stalls its endpoint on a START frame, so it must not be used here.
    if not agent.ping():
        print("  session open (FE): no reply")
        print("  -> nothing is answering. Power-cycle into download mode and retry.")
        return
    print("  session open (FE): acknowledged")

    # Only U-Boot implements getinfo. The boot ROM may stall on the attempt,
    # which the pipe recovers from.
    result = None
    try:
        result = agent.try_command("getinfo version", timeout_ms=3000)
    except (AgentError, UsbError) as exc:
        log.debug("getinfo probe failed: %s", exc)

    if result is not None and result.ok:
        print(f"  getinfo version: {result.output}")
        print("  -> burn agent, ready to flash")
    else:
        print("  getinfo version: no reply")
        print("  -> boot ROM, waiting for a download: hisiburn boot -f u-boot.bin")


def cmd_info(args: argparse.Namespace) -> int:
    pipe, agent = connect_agent(
        args.pid, args.wait, Path(args.uboot) if args.uboot else None, args.chip
    )
    try:
        for topic in ("version", "bootmode", "spi"):
            value = " ".join(agent.get_info(topic).split())
            print(f"{topic:<10} {value}")
    finally:
        pipe.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    pipe, agent = connect_agent(
        args.pid, args.wait, Path(args.uboot) if args.uboot else None, args.chip
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

    uboot = Path(args.uboot) if args.uboot else find_uboot(Path(args.dir), layout)
    if uboot is not None and not uboot.is_file():
        raise CliError(f"U-Boot image not found: {uboot}")

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
             "(default: the bootloader image in the firmware directory)",
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
    parser.add_argument("--version", action="version", version=f"hisiburn {__version__}")

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
