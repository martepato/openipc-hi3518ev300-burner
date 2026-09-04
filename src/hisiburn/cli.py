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
from hisiburn.flash import PlanError, build_plan, run_plan, verify_flash_chip
from hisiburn.hitool_log import LogParseError, layout_from_log, parse_sessions
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
    device = wait_for_device(product_id, timeout=wait) if wait else find_device(product_id)
    return BulkPipe(device)


def connect_agent(product_id: int | None, wait: float) -> tuple[BulkPipe, BurnAgent]:
    """Open the camera and confirm a burn agent — not the boot ROM — is on it."""
    pipe = open_pipe(product_id, wait)
    agent = BurnAgent(pipe)
    agent.read_greeting()
    if not agent.is_agent():
        pipe.close()
        raise CliError(
            "the device is attached but no burn agent answered. Both stages share "
            "the same USB id, so this is most likely the boot ROM still waiting "
            "for a download — run `hisiburn boot -f u-boot.bin` first."
        )
    return pipe, agent


# --- commands ---------------------------------------------------------------


def cmd_probe(args: argparse.Namespace) -> int:
    def scan() -> list:
        found = list_devices()
        if args.pid is not None:
            found = [d for d in found if d.product_id == args.pid]
        return found

    devices = scan()
    if not devices and args.wait:
        # The download-mode window is short, so let the user start the scan and
        # then do the unplug/hold-reset/replug dance.
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
        if not args.wait:
            print()
            print("Tip: `hisiburn probe --wait 30` starts scanning first, so you can")
            print("run it and then do the unplug/hold-reset/replug dance.")
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
    pipe, agent = connect_agent(args.pid, args.wait)
    try:
        for topic in ("version", "bootmode", "spi"):
            value = " ".join(agent.get_info(topic).split())
            print(f"{topic:<10} {value}")
    finally:
        pipe.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    pipe, agent = connect_agent(args.pid, args.wait)
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
    profile = PROFILES.get(args.chip)
    if profile is None:
        raise CliError(f"unknown chip {args.chip!r} (known: {', '.join(sorted(PROFILES))})")

    data = uboot.read_bytes()
    print(f"Loading {uboot.name} ({len(data)} bytes) via the boot ROM...")

    pipe = open_pipe(args.pid, args.wait)
    previous = pipe.info.location
    try:
        BootRom(pipe).boot_uboot(data, profile, on_progress=ProgressBar("upload"))
    finally:
        pipe.close()

    # The loaded U-Boot comes back under the same USB id, so the handoff shows
    # up as a re-enumeration at a new address rather than a new product id.
    print("U-Boot started. Waiting for it to re-enumerate...")
    try:
        device = wait_for_device(args.pid, timeout=20.0, exclude=previous)
    except DeviceNotFound as exc:
        raise CliError(
            f"{exc}\nThe images went across but nothing came back. Check that this "
            "U-Boot was built with HiSilicon's usbtftp support."
        ) from exc

    with BulkPipe(device) as pipe:
        agent = BurnAgent(pipe)
        greeting = agent.read_greeting()
        if greeting:
            print(f"  {greeting}")
        if agent.is_agent():
            print(f"Agent ready: {agent.get_info('version')}")
        else:
            print("Device re-enumerated but is not answering as a burn agent.")
            return 1
    return 0


def _resolve_layout(args: argparse.Namespace) -> FlashLayout:
    if args.layout_file:
        return FlashLayout.load(Path(args.layout_file))
    if args.from_log:
        return layout_from_log(Path(args.from_log))
    return get_layout(args.layout)


def _parse_overrides(values: list[str] | None) -> dict[str, Path]:
    overrides: dict[str, Path] = {}
    for value in values or []:
        name, separator, path = value.partition("=")
        if not separator:
            raise CliError(f"--image expects NAME=PATH, got {value!r}")
        overrides[name] = Path(path)
    return overrides


def cmd_flash(args: argparse.Namespace) -> int:
    layout = _resolve_layout(args)
    plan = build_plan(
        layout,
        Path(args.dir),
        only=set(args.only) if args.only else None,
        overrides=_parse_overrides(args.image),
    )

    print(plan.describe())

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

    pipe, agent = connect_agent(args.pid, args.wait)
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
SHARED_DEFAULTS = {"verbose": False, "pid": None, "wait": 0.0}


def _shared_options() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
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
        help="wait this long for the device to appear instead of failing immediately",
    )
    return general, device


def build_parser() -> argparse.ArgumentParser:
    general, device = _shared_options()

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
        "info", parents=[general, device], help="query a running burn agent"
    )
    info.set_defaults(func=cmd_info)

    run = sub.add_parser(
        "run", parents=[general, device],
        help="run U-Boot console commands through the agent",
    )
    run.add_argument("command", nargs="+", help="commands to run in order")
    run.add_argument("--timeout", type=int, default=30, help="seconds per command")
    run.set_defaults(func=cmd_run)

    boot = sub.add_parser(
        "boot", parents=[general, device], help="load and start U-Boot via the boot ROM"
    )
    boot.add_argument("-f", "--file", required=True, help="U-Boot binary")
    boot.add_argument("-c", "--chip", default="hi3518ev300", help="chip profile")
    boot.set_defaults(func=cmd_boot)

    flash = sub.add_parser(
        "flash", parents=[general, device],
        help="write a firmware set to the camera's flash",
    )
    flash.add_argument("-d", "--dir", default=".", help="directory holding the images")
    flash.add_argument("-l", "--layout", default="mjsxj02hl-16m", help="built-in layout name")
    flash.add_argument("--layout-file", help="JSON layout produced by `hisiburn from-log -o`")
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
