# Building the agent U-Boot

Everything this tool does runs through a U-Boot loaded into RAM by the boot
ROM. It is **never written to flash**, so a bad build costs a power cycle and
nothing else — which makes it a safe thing to iterate on.

OpenIPC's released `u-boot-<soc>-universal.bin` works for flashing. Two things
in it make *backups* impractical, and one of them costs data:

| | consequence |
|---|---|
| `usbtftp` not compiled in | no bulk device-to-host path; a read-back falls back to hex dumps at 32 bytes per round trip — **hours** for a 16 MiB chip |
| `set_default_env()` calls `saveenv()` unconditionally | the U-Boot writes a default environment to flash **merely by starting**, destroying an erase block of whatever image sits at its `CONFIG_ENV_OFFSET` |

Both are fixed by one build:

```sh
./tools/build-agent-uboot.sh
hisiburn inspect output/u-boot-hi3518ev300-agent.bin
```

```
verdict: a U-Boot image — U-Boot 2016.11-g131d3f2-dirty
capabilities:
  yes  usbtftp      bulk flash read-back — fast backup
  yes  crc32        on-device checksums — verify
  ...
```

Then use it like any other loader:

```sh
hisiburn backup mycamera.bin --uboot output/u-boot-hi3518ev300-agent.bin
```

## What the script changes

Four edits to OpenIPC's `u-boot-hi3516ev200` tree, all of them small:

1. **`cmd/Makefile`** — `usbtftp.o` is gated on `CONFIG_CMD_USB`, which would
   also drag in the USB *host* stack this board has no use for. Build it
   directly instead.

2. **`cmd/usbtftp.c`, the `CONFIG_MMC` guard** — the flash-read half of the
   upload command sits inside `#ifndef CONFIG_MMC`, and this board sets
   `CONFIG_MMC`, so it would compile to a stub that prints "can not support
   emmc now". The real dependency is SPI flash, so the guard becomes
   `#ifdef CONFIG_CMD_SF`.

3. **`cmd/usbtftp.c`, the frame size** — the vendor sends 200 bytes per
   request, which is what makes even the bulk path slow (~20 minutes for
   16 MiB). The host reads the frame size out of the head frame, so raising it
   needs no protocol change on either side; 16 KiB brings a whole chip under a
   minute. The receive buffer is enlarged to match.

4. **`common/env_common.c`** — the `saveenv()` at the end of
   `set_default_env()` is removed. This is a HiSilicon modification; mainline
   U-Boot does not save there. An agent that writes flash just by starting is
   worse than useless for reading it.

A fifth edit is incidental: `usb_stop()` belongs to the USB host stack that
step 1 deliberately leaves out, so its call is dropped. The PHY is already
quiesced by the `phy_hiusb_init()` above it.

## Is it safe to run an unofficial U-Boot?

It only ever executes from RAM. The boot ROM is in mask ROM and cannot be
overwritten; the flash is untouched unless you ask for a write. If a build is
wrong, the camera does not come back as a burn agent and you power-cycle.

The one thing worth checking before trusting a build is that it reads back what
is really there, which `backup` does for you: every chunk is checksummed on the
device with `crc32` and compared against what arrived over USB.
