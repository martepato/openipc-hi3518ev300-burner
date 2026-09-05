# hisiburn

Flash, back up, restore and verify HiSilicon **Hi3518EV300** cameras over USB,
from macOS and Linux.

```sh
hisiburn flash -d ./output/release       # write a firmware build
hisiburn backup mycamera.bin             # read the whole chip to a file
hisiburn restore mycamera.bin --verify   # write it back, byte for byte
hisiburn inspect factory.bin             # say what a .bin actually is
```

Tested end to end on a Xiaomi MJSXJ02HL. Other Hi3518EV300 boards should work
with their own partition table; other SoCs need a profile.

## Requirements

**A U-Boot for your SoC.** The camera's boot ROM cannot write flash on its
own, so every operation loads a U-Boot into RAM and drives that. It runs from
RAM and is never written to flash, so any build for the SoC will do:

```sh
curl -LO https://github.com/OpenIPC/firmware/releases/download/latest/u-boot-hi3518ev300-universal.bin
```

`flash` picks it up from the firmware directory, and commands pointed at a
whole-chip dump take it from the dump itself. Otherwise pass `--uboot PATH` or
set `HISIBURN_UBOOT` once per shell.

Backups want the agent U-Boot instead — it carries HiSilicon's bulk read path,
which reads a 16 MiB chip in about a minute where the stock loader takes two
hours. One command to build; see [docs/AGENT-UBOOT.md](docs/AGENT-UBOOT.md).

**libusb** and **Python 3.10+**:

```sh
brew install libusb                # macOS
sudo apt install libusb-1.0-0      # Debian/Ubuntu
```

## Install

```sh
brew install uv
uv tool install git+https://github.com/martepato/openipc-hi3518ev300-burner
```

`pip install git+https://…` works too, as does `pip install -e '.[dev]'` from a
clone. Git installs pin a commit, so `hisiburn --version` reports which one;
`uv tool install --force` or `pip install --force-reinstall` moves it forward.

## Flashing a build

Point it at the build's output directory:

```sh
hisiburn flash -d ./output/release
```

Start the command, then unplug the camera, hold its reset button, plug the USB
cable back in while still holding, and keep holding for a couple of seconds.
Every command that touches the camera waits 30 seconds for it to appear, so
the order works out.

`flash` loads U-Boot itself, reads the build's own `usb-burn.xml` partition
table when there is one, checks the images against a shipped `sha256sums.txt`
before erasing anything, writes each partition and reboots the camera. Expect
about a minute. `--dry-run` prints the plan and touches nothing:

```
Layout: usb-burn.xml in ./output/release
Plan for usb-burn (5 partitions):
  fastboot: u-boot-hi3518ev300-universal.bin (236099 bytes) -> 0x0, writing 0x40000
  kernel: uImage.hi3518ev300 (1908952 bytes) -> 0x50000, writing 0x1e0000
  rootfs: rootfs.squashfs.hi3518ev300 (5693440 bytes) -> 0x350000, writing 0x570000
  total to transfer: 7.54 MiB
  4 image(s) match sha256sums.txt
```

## Backing up and restoring

```sh
./tools/build-agent-uboot.sh
hisiburn backup mycamera.bin --uboot output/u-boot-hi3518ev300-agent.bin
```

Reads the whole chip in about a minute. Every chunk is checksummed on the
device with `crc32` and compared against what arrived, so a dropped frame
cannot pass silently, and `--resume` continues an interrupted run.

Putting it back:

```sh
hisiburn restore mycamera.bin --verify
```

`restore` writes a dump verbatim from offset 0 — use it rather than `flash`
for a dump, whose layout is whatever the camera it came from used. It starts
the agent from the bootloader inside the dump, so `--uboot` is optional here.
`--verify` re-checks the write in the same session, which is the reading you
can trust (see [Things worth knowing](#things-worth-knowing)).

## Commands

`hisiburn <command> --help` has the full flag list. `--verbose`, `--wait` and
`--pid` work on either side of the subcommand, and every command that talks to
the camera takes `--uboot PATH` and `--chip`.

| | |
|---|---|
| `flash -d DIR` | Write a firmware set. Layout from the build's `usb-burn.xml`, else `--layout-file`, `--from-log`, `--layout NAME` or a built-in. `--only NAME…` writes a subset, `--image NAME=PATH` overrides one file, `--dry-run` prints the plan, `--no-reset` leaves the camera halted. |
| `restore IMAGE` | Write a whole-chip dump verbatim. `--verify` checks it in the same session. `--force` accepts an image that is not a recognisable dump or does not match the chip size. |
| `backup OUTPUT` | Read flash to a file. `--offset` / `--length` for one partition, `--resume` to continue, `--no-bulk` to force the hex-dump path. |
| `verify IMAGE` | Check flash against a local image by checksum. `--offset` places a partition-sized image, `--skip` and `--length` check one region. |
| `peek OFFSET` | Show the bytes at a flash offset, optionally beside `--image`. Moves tens of bytes per round trip, so keep `-n` small — it is for a header, not a partition. |
| `inspect IMAGE` | Say what a firmware file is, without touching the camera. `--compare OTHER` diffs two images by erase block. |
| `from-log LOG` | Recover a flash layout from a HiTool/HiBurn log. `-o layout.json` saves it for `flash --layout-file`. |
| `layouts` | List the built-in layouts. |
| `probe` | List attached HiSilicon devices. Both stages share a USB id, so `probe -v` asks which is listening. |
| `info` | Query a running agent: U-Boot version, boot mode, flash chip. |
| `run CMD…` | Run U-Boot console commands. A usable debugging console over USB alone. |
| `boot -f FILE` | Load and start a U-Boot via the boot ROM. The other commands do this for you. |

### verify

Each chunk is read into DRAM and checksummed **on the device**, so only the
checksum crosses USB: a whole 16 MiB chip is four round trips, and it works on
a U-Boot with no read-back path at all. A mismatch is narrowed to the erase
block and the block identified:

```
2 region(s) differ:
  0x0040000..0x0050000  U-Boot environment
  0x0F90000..0x0FA0000  JFFS2 inode node
```

Those two turn up routinely and neither means the write failed — the first is
the loader's own environment, the second the camera's settings partition. A
`uImage header` where the image expects something else is a real problem.

### inspect

A `.bin` handed round as "the factory firmware" can be a raw chip dump, a
packaged vendor update, or a lone bootloader, and they are written completely
differently — a packaged update written to flash puts a wrapper where the
bootloader belongs. `inspect` says which, and which firmware a dump came from:

```
factory.bin: 16,777,216 bytes (16.00 MiB)
verdict: full 16 MiB flash dump — write it verbatim from offset 0
firmware: 4.0.5_0105 on isa.camera.hlc6 (from os-release at 0x00F9AE74)

contents:
  0x000047B0 gzip                   compressed "u-boot.bin"
  0x00040000 uImage      1,916,698  "Linux-4.9.37", OS kernel, Linux
  0x00F90000 jffs2         327,692  38 nodes (cleanmarker, dirent, inode)

inferred partition extents:
  0x00000000    256 KiB  bootloader
  0x00040000   1984 KiB  uImage
  ...
```

Pointed at a U-Boot it reports that build's capabilities instead, which is
what decides whether a backup gets the fast path.

### from-log

If you have flashed this camera from Windows before, the HiTool log records
the offsets, sizes and image names — a partition table you already have. This
is the recommended path for a camera with no built-in layout:

```sh
hisiburn from-log hiburn.log -o layout.json
hisiburn flash --layout-file layout.json -d ./firmware
```

## Things worth knowing

**A camera that has booted will not match a dump of it.** Firmware writes its
settings partition on every boot, so that region differing is expected.

**A stock U-Boot writes to flash when it starts.** HiSilicon's
`set_default_env()` calls `saveenv()` unconditionally, so a loader that cannot
read a valid environment writes a default one at its own env offset — 0x40000
for OpenIPC's build. Restore a vendor image whose kernel starts there and the
*next* session overwrites that block before it can read it, so a standalone
verify reports a difference it caused itself. Either verify in the same
session as the write (`restore --verify`), or use the agent U-Boot, which has
that write removed.

**Firmware versions cannot be grepped for.** The settings partition is JFFS2,
which appends rather than overwrites, so a dump holds every version the file
ever had — one taken from a 4.5.6 camera contains thirty-one copies of an
older string. `inspect` reads the node version counters instead.

**`inspect` reads only the version, model and vendor** out of that partition.
The same files hold the camera's MAC, cloud device id and auth key, which are
deliberately never printed.

**Recovering from a bad flash** is holding reset while plugging in and running
`hisiburn flash` again. The boot ROM is mask ROM and always answers.

## Safety

- Images are checked to exist and to fit before the first erase.
- The chip size the camera reports must match the layout, or the run stops —
  an 8 MiB chip flashed with a 16 MiB layout silently loses the tail of the
  rootfs.
- Writes are padded to the 64 KiB erase block, matching HiBurn.
- Reads are checksummed on the device and compared against what arrived.

## If you have a UART

OpenIPC's [defib](https://github.com/OpenIPC/defib) covers 120+ SoCs over UART
plus TFTP and is the more mature tool if you can reach the board's serial pads.

## How it works

[docs/PROTOCOL.md](docs/PROTOCOL.md) documents both stages — USB ids,
endpoints, every frame layout, the ACK cadence, the flashing sequence — with
the provenance of each claim. As far as we can tell it is the only public
write-up of the HiUSBBurn protocol.

Both stages were implemented against a USBPcap capture of a real HiBurn 5.3
session. The captured frames are in `tests/fixtures/captured_frames.json` and
the suite asserts this tool reproduces them byte for byte, including a replay
test comparing its whole U-Boot command stream against HiBurn's — identical
from the first downloaded partition onward.

[docs/AGENT-UBOOT.md](docs/AGENT-UBOOT.md) covers the U-Boot to build for fast
backups, and the four faults in HiSilicon's own `usbtftp` that had to be fixed
to make it work.

One finding worth pulling out, because it silently breaks anything built from
UART sources: the 64-byte DDR stub also writes `START_MAGIC` ("DOWN") to
`REG_START_FLAG`, and the U-Boot it loads refuses to enter download mode
without it. OpenIPC `defib`'s profile has a different value in those four
bytes; `ChipProfile` refuses to construct without the right one.

## Credits

The protocol was read out of GPL-licensed HiSilicon sources shipped in
[OpenIPC/u-boot-hi3516ev200](https://github.com/OpenIPC/u-boot-hi3516ev200)
(`drivers/usb/gadget/hiudc3/`, `common/download_process.c`, `cmd/usbtftp.c`),
cross-checked against [OpenIPC/defib](https://github.com/OpenIPC/defib), and
corrected against a USBPcap capture of a real HiBurn session — which is what
turned it from a plausible reading into a verified one.

## License

MIT — see [LICENSE](LICENSE).
