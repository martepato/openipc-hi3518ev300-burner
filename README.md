# hisiburn

Flash HiSilicon **Hi3518EV300** cameras over USB from **macOS** (and Linux).

An open-source replacement for the Windows-only HiTool/HiBurn workflow — no
Zadig, no libusbK, no driver installation of any kind.

> **Why macOS needs no driver.** The camera exposes a vendor-specific USB
> interface (`bInterfaceClass = 0xFF`). Windows has no stock driver that binds
> such an interface, which is why HiTool users have to swap in libusbK with
> Zadig. On macOS and Linux nothing claims a vendor-specific interface, so
> libusb opens it directly.

## Status

**Working.** A Xiaomi MJSXJ02HL (Hi3518EV300) has been flashed end to end with
this tool from macOS — all five partitions, from boot ROM to reboot in one
command, no drivers installed, no serial console, no Windows.

The U-Boot command stream from that run was diffed against the captured HiBurn
session: **identical from the first downloaded partition onward**, with the one
documented difference being that this tool uploads U-Boot for the bootloader
slot rather than reusing what stage 1 left in RAM.

Both stages were implemented against a **USBPcap capture of a real HiBurn 5.3
flash** rather than guesswork. The captured frames are checked into
`tests/fixtures/captured_frames.json`, and the test suite asserts this tool
reproduces them byte for byte — including a replay test comparing its whole
U-Boot command stream against HiBurn's, command for command.

Only this camera has been tested. Other Hi3518EV300 boards should work with
their own partition table; other SoCs need a profile.

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the full protocol, including
[what a source-only reading got wrong](docs/PROTOCOL.md#for-the-record-what-a-source-only-reading-got-wrong)
before the capture existed.

## Install

```sh
brew install libusb            # the pyusb backend; the only native dependency
uv tool install git+https://github.com/martepato/openipc-hi3518ev300-burner
```

Or from a clone:

```sh
pip install -e '.[dev]'
```

## Use

### 1. Get the camera into download mode

Unplug it, hold the reset button, plug the USB cable back in while still
holding, and keep holding for a couple of seconds.

```sh
hisiburn probe
```

Every command that touches the camera **waits up to 30 seconds for it by
default**, so you can start the command and *then* do the
unplug/hold-reset/replug dance. `--wait 0` fails immediately instead;
`--wait 60` gives you longer. All of `--verbose`, `--wait` and `--pid` work on
either side of the subcommand.

```
Found 1 HiSilicon device(s):
  12d1:d001 at bus 20 device 7 [Hisilicon HiUSBBurn] — HiUSBBurn (boot ROM or burn agent — ask it to tell them apart)
```

Both stages present the same USB id, so `probe --verbose` asks the device
which one is listening.

If nothing appears, the camera is not entering download mode — that is a
button-timing problem, not a driver problem.

### 2. Flash

Point it at a build's output directory. Nothing else is needed:

```sh
hisiburn flash -d ./output/release
```

**It brings the camera up itself.** A camera fresh out of the reset-button
dance is sitting in its boot ROM, which cannot flash anything — a U-Boot has
to be loaded into RAM first. The image needed for that is the same bootloader
the firmware set installs, already in the directory, so `flash` finds it and
runs that stage for you:

```
Boot ROM is listening — loading u-boot-hi3518ev300-universal.bin (236099 bytes) first...
  u-boot [################################] 100.0%  230 KiB
U-Boot started. Waiting for it to re-enumerate...
  start download process.
```

Override the image with `--uboot PATH`. If the camera is already running the
agent, this step is skipped.

Check what it will do first — this touches nothing:

```sh
hisiburn flash -d ./output/release --dry-run
```

```
Layout: usb-burn.xml in ./output/release
Plan for usb-burn (5 partitions):
  fastboot: u-boot-hi3518ev300-universal.bin (236099 bytes) -> 0x0, writing 0x40000
  env: env.bin (65536 bytes) -> 0x40000, writing 0x10000
  kernel: uImage.hi3518ev300 (1908952 bytes) -> 0x50000, writing 0x1e0000
  rootfs: rootfs.squashfs.hi3518ev300 (5693440 bytes) -> 0x350000, writing 0x570000
  rootfs_data: erase 0x2b0000 at 0xd50000
  total to transfer: 7.54 MiB
  4 image(s) match sha256sums.txt
```

**It reads the build's own partition table.** If the directory holds a HiTool
`usb-burn.xml`, that is the layout — it was written by whoever built the
firmware and names its actual files, which beats any built-in guess. A
built-in layout is used only when there is no table to read. Override with
`--layout-file` (a `.xml` or JSON) or `--layout NAME`.

**It verifies checksums first.** If the build shipped `sha256sums.txt` or
`md5sums.txt`, the images are checked against it before a single block is
erased — a truncated download is the cheapest way to brick a camera.
`--no-verify` skips it.

Useful flags: `--only kernel rootfs` to flash a subset (`boot` and `fastboot`
name the same partition), `--image NAME=PATH` to point a partition at a
specific file, `--no-reset` to leave the camera halted.

Expect about a minute: erase runs at roughly 2.7 s/MiB and write at 1.6 s/MiB
on these NOR parts, so the long silences during `sf erase` are normal.

### Driving the stages by hand

`flash` covers the normal path. The stages are separately available when you
want them — to inspect a camera without writing anything, say:

```sh
hisiburn boot -f u-boot-hi3518ev300-universal.bin   # stage 1 only
hisiburn info                                        # ask a running agent
```

```
version    version: U-Boot 2016.11-g131d3f2
bootmode   spi
spi        Block:64KB Chip:16MB*1 ID:0x1C 0x70 0x18 Name:"EN25QH128A"
```

`info` and `run` will also start the agent themselves if you give them
`--uboot PATH`.

### Working out what a firmware file is

A `.bin` handed round as "the factory firmware" can be a raw dump of a whole
flash chip or a packaged update, and they are written completely differently.
`inspect` says which, without touching the camera:

```sh
hisiburn inspect factory.bin
```

```
factory.bin: 16,777,216 bytes (16.00 MiB)
verdict: full 16 MiB flash dump — write it verbatim from offset 0

contents:
  0x000047B0 gzip                   compressed "u-boot.bin"
  0x00040000 uImage      1,916,698  "Linux-4.9.37", OS kernel, Linux, load 0x40008000
  0x00230000 squashfs    3,513,074  495 inodes, 128 KiB blocks
  ...

inferred partition extents:
  0x00000000    256 KiB  bootloader
  0x00040000   1984 KiB  uImage
  ...
```

A vendor SD-card recovery image is recognised for what it is and refused:

```
verdict: a U-Boot firmware update package ("hlc6") — meant for the camera's
own updater (the SD-card recovery procedure), not for writing to flash
```

Two dumps of the same camera can be diffed at erase-block granularity, which
is the unit that matters — a byte's difference means the whole block has to be
written differently:

```sh
hisiburn inspect signed.bin --compare unsigned.bin
```

```
both 16,777,216 bytes
1 differing region(s), 64 KiB of 16384 KiB (0.39% of the chip)

  0x0030000..0x0040000     64 KiB  bootloader at 0x0000000
```

### Restoring a whole-chip dump

```sh
hisiburn restore factory.bin
```

Writes the image verbatim from offset 0, erasing and writing a few MiB at a
time so the staging area never has to hold the whole chip. Use this rather
than `flash` for a dump: its partition layout is whatever the camera it came
from used, which is very likely not the one `flash` knows.

It refuses anything that does not look like a whole-chip dump, and refuses a
size that does not match the chip. `--force` overrides both, `--dry-run`
prints the plan.

Stage 1 still needs a U-Boot to run, and a lone image file has no firmware
directory to find one in — so a full dump supplies its own, from the
bootloader at its offset 0. `--uboot PATH` overrides that, and a U-Boot
sitting next to the image is preferred over the embedded one.

### Recovering a layout from a HiBurn log

If you have no partition table but have flashed this camera from Windows
before, the HiTool log is one — offsets, sizes and image names. Turn it into a
layout rather than guessing:

```sh
hisiburn from-log hiburn.log                    # show it
hisiburn from-log hiburn.log -o layout.json     # save it
hisiburn flash --layout-file layout.json -d ./firmware
```

This is the recommended path for any camera without a built-in layout.

### Driving U-Boot directly

The agent runs arbitrary U-Boot commands, which makes it a usable debugging
console even without a UART:

```sh
hisiburn run 'sf probe 0' 'sf read 0x41000000 0x0 0x10000' 'md 0x41000000 0x20'
```

Nothing here needs a serial console.

## Safety

- Every image is checked to exist and to fit **before** the first erase.
- The chip size the camera reports must match the layout, or the run is
  refused — an 8 MiB chip flashed with a 16 MiB layout silently loses the tail
  of the rootfs.
- Writes are padded to the 64 KiB erase block, matching HiBurn.
- A failed U-Boot command stops the agent accepting further commands; the tool
  says so rather than appearing to hang.

**Take a backup first if the camera currently works.** `hisiburn run 'sf probe 0'`
plus `sf read` can pull the existing flash out through the same channel.

## If you have a UART

If you can reach the board's serial pads, OpenIPC's
[defib](https://github.com/OpenIPC/defib) is the more mature tool and covers
120+ SoCs over UART plus TFTP. `hisiburn` exists for the case defib does not
cover: USB only, no soldering.

## Protocol

[docs/PROTOCOL.md](docs/PROTOCOL.md) documents both stages — USB ids,
endpoints, every frame layout, the ACK cadence, and the flashing command
sequence — with the provenance of each claim. As far as we can tell this is
the only public write-up of the HiUSBBurn protocol.

One finding worth pulling out, because it silently breaks anything built from
UART sources: the 64-byte DDR stub also writes `START_MAGIC` ("DOWN") to
`REG_START_FLAG`, and the U-Boot it loads refuses to enter download mode
without it. OpenIPC `defib`'s profile has a different value in those four
bytes. `ChipProfile` refuses to construct without the right one.

## Credits

The protocol was read out of GPL-licensed HiSilicon sources shipped in
[OpenIPC/u-boot-hi3516ev200](https://github.com/OpenIPC/u-boot-hi3516ev200)
(`drivers/usb/gadget/hiudc3/`, `common/download_process.c`, `cmd/usbtftp.c`),
cross-checked against [OpenIPC/defib](https://github.com/OpenIPC/defib), and
then corrected against a USBPcap capture of a real HiBurn session — which is
what turned it from a plausible reading into a verified one.

## License

MIT — see [LICENSE](LICENSE).
