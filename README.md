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

## Requirements

**A U-Boot for your SoC.** This is the one thing you must supply. The camera's
boot ROM cannot write flash on its own — every operation here first loads a
U-Boot into RAM and drives that. It is never written to flash, so any build
for the SoC will do; OpenIPC publishes one per SoC as
`u-boot-<soc>-universal.bin`:

```sh
curl -LO https://github.com/OpenIPC/firmware/releases/download/latest/u-boot-hi3518ev300-universal.bin
```

`flash` finds it automatically in the firmware directory, and any command
pointed at a whole-chip dump — `restore`, `verify`, `peek --image` — can take
one out of the dump itself. `info`, `run` and `backup` have nowhere to look, so
pass `--uboot PATH` (or set `HISIBURN_UBOOT`).

**libusb**, the only native dependency:

```sh
brew install libusb          # macOS
sudo apt install libusb-1.0-0    # Debian/Ubuntu
```

**Python 3.10+.**

## Install

With [uv](https://docs.astral.sh/uv/) — which macOS does not ship, so install
it first:

```sh
brew install uv
uv tool install git+https://github.com/martepato/openipc-hi3518ev300-burner
```

Or with pip, into a virtualenv of your choosing:

```sh
pip install git+https://github.com/martepato/openipc-hi3518ev300-burner
```

Or from a clone, for development:

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
firmware: 4.0.5_0105 on isa.camera.hlc6 (from os-release at 0x00F9AE74)

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

**It says which firmware a dump was taken from.** `restore` prints the same
line before asking for confirmation, so two dumps of the same camera are told
apart by what they hold rather than by what you called the files.

The version comes from the settings partition, where the camera's own updater
writes it — `os-release`, or `app.ver` on older firmware. Note that grepping a
dump for a version string does *not* work: JFFS2 appends rather than
overwrites, so a dump keeps every version the file ever had. A dump taken from
a camera running 4.5.6_0168 holds two copies of `4.5.6_0168` — and thirty-one
of `4.0.5_0105`, plus one of `4.0.4_0073`, from firmware long since replaced.
The node headers carry version counters that say which is live, so those are
what get read.

Only the version, model and vendor are read out of that partition. The same
files hold the camera's MAC, its cloud device id and its cloud auth key, and
those are deliberately never printed — a summary of a dump should not be how
someone's credentials end up in a scrollback or a pasted bug report.

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
bootloader at its offset 0. This holds for every command pointed at a whole
image, `verify` and `peek --image` included: an image you can restore without
`--uboot` is one you can verify without it too.

The order is `--uboot`, then `$HISIBURN_UBOOT`, then a U-Boot beside the image
or in the working directory, then the one inside the image. Whichever wins is
named on the `Boot ROM is listening — loading …` line, which is worth reading:
a *stock* U-Boot writes its own environment to flash the moment it starts (see
below), and the copy inside a factory dump is a stock one.

`backup` is the exception, since it is pointed at a file it is about to create
rather than one that exists. It needs `--uboot`, `$HISIBURN_UBOOT`, or a U-Boot
in the working directory.

### Backing up a camera's flash

**Speed here depends entirely on the U-Boot you load — by a factor of about a
hundred.** Build the agent U-Boot once:

```sh
./tools/build-agent-uboot.sh
hisiburn backup mycamera.bin --uboot output/u-boot-hi3518ev300-agent.bin
```

```
  flash: Block:64KB Chip:16MB*1 ID:0x1C 0x70 0x18 Name:"EN25QH128A"
  u-boot-hi3518ev300-agent.bin has usbtftp — using the bulk read path
  read [########################] 100.0%  16384 KiB   285 KiB/s
```

That build has HiSilicon's `usbtftp` bulk read path enabled and working, so
the whole 16 MiB chip comes back in about a minute. See
[docs/AGENT-UBOOT.md](docs/AGENT-UBOOT.md) — it also removes the `saveenv()`
that otherwise costs you an erase block every session.

OpenIPC's released U-Boot works too, but it has no `usbtftp`, so there is no
device-to-host bulk path at all and the bytes come back as hex text in command
replies — 32 bytes per round trip, **about two hours** for a 16 MiB chip:

```sh
hisiburn backup mycamera.bin --uboot u-boot-hi3518ev300-universal.bin
```

A single partition is tolerable on that path:

```sh
hisiburn backup settings.bin --offset 0xf90000 --length 0x70000    # ~3 min
```

`hisiburn inspect <u-boot.bin>` reports which capabilities a given build has,
and `hisiburn info --uboot <u-boot.bin>` reports the same alongside what the
running agent says about itself. Capabilities always come from the image: a
running U-Boot cannot safely be asked what commands it has.

Both paths are checked the same way, and that is what makes either
trustworthy: **every chunk is checksummed on the device with `crc32` and
compared against what arrived.** A dropped frame or a misparsed dump cannot
pass silently; a failing chunk is re-read before the run gives up. Chunks are
written and flushed as they complete, so `--resume` continues an interrupted
run rather than starting over.

### Verifying what is actually on the camera

```sh
hisiburn verify mjsxj02hl_full-dump_4.0.5-0105_sign.bin
```

```
  0x0000000..0x0400000: ok (3f2a91c4)
  0x0400000..0x0800000: ok (b71de055)
  ...
Match: the camera's flash is byte-identical to mjsxj02hl_full-dump_4.0.5-0105_sign.bin.
```

Each chunk is read into DRAM and checksummed **on the device**; only the
checksum crosses USB. That makes it fast — a whole 16 MiB chip is four round
trips — and, more usefully, it works on a U-Boot with no device-to-host bulk
path at all.

A mismatch is narrowed automatically — the failing range is re-checked per
erase block, and the block is read back and identified:

```
2 region(s) differ:
  0x0040000..0x0050000  U-Boot environment
  0x0F90000..0x0FA0000  JFFS2 inode node
```

Those two are the ones that turn up routinely, and neither means the write
failed: the first is the agent U-Boot's own environment (see below), the
second is the camera's settings partition. A `uImage header` or
`squashfs superblock` where the image expects something else would be a real
problem.

Checking can also be aimed by hand:

```sh
hisiburn verify dump.bin --chunk 0x10000              # per 64 KiB erase block
hisiburn verify dump.bin --skip 0x40000 --length 0x1f0000   # just the kernel
```

`--offset` is for an image that is one partition rather than a whole chip:
it says where in flash that image belongs.

**A camera that has booted will not match a dump of it.** Firmware writes its
settings partition on every boot, so that region differing is normal.

**And the agent U-Boot itself writes to flash when it starts.** HiSilicon's
`set_default_env()` calls `saveenv()` unconditionally, so a U-Boot that cannot
load a valid environment writes a default one at its own env offset — 0x40000
for OpenIPC's build. Restore a vendor image whose kernel starts there and the
next session will overwrite that block before it can read it, making a
standalone verify report a difference it caused itself. Verify in the same
session instead:

```sh
hisiburn restore factory.bin --verify --uboot u-boot-hi3518ev300-universal.bin
```

The full mechanism is in
[docs/PROTOCOL.md](docs/PROTOCOL.md#the-agent-u-boot-writes-to-flash-when-it-starts).

### Looking at individual bytes

When `verify` flags a block, `peek` shows what is actually there — reading it
back through U-Boot's `md.b`, which needs no upload path:

```sh
hisiburn peek 0x40000 --image dump.bin --uboot u-boot-hi3518ev300-universal.bin
```

```
camera, flash 0x00040000:
  0x00040000  27 05 19 56 63 fd 39 c1 ...  |'..Vc.9.....|
dump.bin, same offset:
  0x00040000  27 05 19 56 8a 3b 11 02 ...  |'..V.;......|  <- differs
```

Keep the length small: this path moves tens of bytes per round trip. It is for
reading a header, not a partition.

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
