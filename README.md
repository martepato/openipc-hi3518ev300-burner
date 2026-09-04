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

Both stages are implemented against a **USBPcap capture of a real HiBurn 5.3
flash**, not guesswork. The captured frames are checked into
`tests/fixtures/captured_frames.json`, and the test suite asserts that this
tool reproduces them byte for byte — including a replay test that compares our
whole U-Boot command stream against HiBurn's, command for command.

Not yet exercised on hardware *by this tool*: someone still has to run it end
to end against a camera. Nothing in stage 1 can brick one — it only writes to
volatile memory — and stage 2 checks every image size before the first erase.

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
hisiburn probe --wait 30
```

`--wait` starts scanning first, so you can run the command and *then* do the
unplug/hold-reset/replug dance — handy, because the download-mode window is
short. All of `--verbose`, `--wait` and `--pid` work on either side of the
subcommand.

```
Found 1 HiSilicon device(s):
  12d1:d001 at bus 20 device 7 [Hisilicon HiUSBBurn] — HiUSBBurn (boot ROM or burn agent — ask it to tell them apart)
```

Both stages present the same USB id, so `probe --verbose` asks the device
which one is listening.

If nothing appears, the camera is not entering download mode — that is a
button-timing problem, not a driver problem.

### 2. Start a U-Boot in RAM

```sh
hisiburn boot -f u-boot.bin
```

This sends the DDR-init stub, the SPL and the U-Boot image. The camera then
re-enumerates at a new bus address, running the burn agent:

```sh
hisiburn info
```

```
version    version: U-Boot 2016.11-g131d3f2
bootmode   spi
spi        Block:64KB Chip:16MB*1 ID:0x1C 0x70 0x18 Name:"EN25QH128A"
```

### 3. Flash

```sh
hisiburn flash -d ./openipc-firmware --layout mjsxj02hl-16m
```

Check what it will do first — this touches nothing:

```sh
hisiburn flash -d ./openipc-firmware --dry-run
```

```
Plan for mjsxj02hl-16m (5 partitions):
  boot: u-boot.bin (196608 bytes) -> 0x0, writing 0x30000
  env: env.bin (65536 bytes) -> 0x40000, writing 0x10000
  kernel: uImage.hi3518ev300 (1908952 bytes) -> 0x50000, writing 0x1e0000
  rootfs: rootfs.squashfs.hi3518ev300 (5689344 bytes) -> 0x350000, writing 0x570000
  rootfs_data: erase 0x2B0000 at 0xD50000
  total to transfer: 7.50 MiB
```

Useful flags: `--only kernel rootfs` to flash a subset, `--image NAME=PATH` to
point a partition at a differently-named file, `--no-reset` to leave the
camera halted afterwards.

### Recovering a layout from a HiBurn log

If you have flashed this camera from Windows before, the HiTool log is a
complete partition table for it — offsets, sizes and image names. Turn it into
a layout rather than guessing:

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
