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

Reading the whole 16 MiB chip takes about a minute, against roughly two hours
through hex dumps.

## What the script changes

Enabling `usbtftp` is the easy part. It does not work as HiSilicon ships it —
each of the edits below fixes something that either wedges the device or makes
it crawl, and the command is dead on arrival without them.

### Making it exist

1. **`cmd/Makefile`** — `usbtftp.o` is gated on `CONFIG_CMD_USB`, which would
   also drag in the USB *host* stack this board has no use for. Build it
   directly instead.

2. **`cmd/usbtftp.c`, the `CONFIG_MMC` guard** — the flash-read half of the
   upload command sits inside `#ifndef CONFIG_MMC`, and this board sets
   `CONFIG_MMC`, so it would compile to a stub that prints "can not support
   emmc now". The real dependency is SPI flash, so the guard becomes
   `#ifdef CONFIG_CMD_SF`.

   Incidentally, `usb_stop()` belongs to the host stack that step 1
   deliberately leaves out, so its call is dropped. The PHY is already quiesced
   by the `phy_hiusb_init()` above it.

### Not writing flash just by booting

3. **`common/env_common.c`** — the `saveenv()` at the end of
   `set_default_env()` is removed. This is a HiSilicon modification; mainline
   U-Boot does not save there. An agent that writes flash just by starting is
   worse than useless for reading it.

### Not tearing down the link the command arrived on

4. **`cmd/usbtftp.c`, the `udc_request()` call** — this is the one that stops
   the command working at all. `do_usbtftp_upload()` ends by calling
   `udc_request()`, which allocates a *second* `usb3_device_t` and re-runs
   `phy_hiusb_init()` / `usb3_common_init()` / `usb3_init()` on it —
   reinitialising the controller underneath the very session that carried the
   command. The host's next transfer fails with `EIO` before a single byte of
   flash comes back.

   Nothing needs to happen there. The callback registered a few lines earlier
   is already served by the `usb3_handle_protocol()` loop that is running, so
   the command returns instead, and a new `usbtftp_release()` — called from
   `usbtftp end` — frees what it set up. `usbtftp end` also loses its guard:
   it refused to run unless a session was open, which is precisely when you
   need it most.

   Because the command now returns, it replies like any other command. The
   host has to read that reply before pumping request frames.

### Not crawling

5. **`drivers/usb/gadget/hiudc3/usb3.h` and two callers** — the vendor sends
   200 bytes per request, which is what makes even the bulk path slow (about
   20 minutes for 16 MiB). The host reads the frame size out of the head frame,
   so raising it needs no protocol change on either side; 16 KiB brings a whole
   chip under a minute.

   The catch is that the bulk IN buffer is a hard-coded `malloc(512)` in
   `usb3_drv.c` and a `memset(..., 512)` in `usb3_prot.c`, neither of which the
   frame builder knows about. Raising the frame size alone writes 16 KiB into a
   512-byte heap allocation. Both now derive from one constant.

6. **`include/configs/hi-common.h`** — `usbtftp`'s flash read `malloc()`s the
   entire range it is asked for, so U-Boot's heap, not the protocol, is what
   bounds a transfer. The arena goes from 4 MiB to 8 MiB, and the host reads in
   1 MiB chunks. (`hi-common.h` is included *after* the per-SoC header, so its
   `CONFIG_SYS_MALLOC_LEN` is the effective one — the value in
   `hi3518ev300.h` is dead.)

### Not wedging on the first mistake

7. **`drivers/usb/gadget/hiudc3/usb3_prot.c`** — when a command returns
   non-zero, the vendor's reply path sends a bare `[EOT](ERROR)` and then does
   *not* re-arm the bulk OUT endpoint. The device stops accepting commands
   entirely, and the console output that would have explained the failure is
   thrown away with it. So one mistyped command, or one `sf` error, costs a
   power cycle and tells you nothing.

   Both verdicts now take the same path: append the marker, re-arm OUT, send
   the output. A failed command becomes an ordinary error to handle.

## Checking a build

`hisiburn inspect <u-boot.bin>` reads the capabilities out of the binary, and
that is the only sound way to get them: a running U-Boot cannot be asked what
commands it has. `help <name>` returns failure whether or not the command
exists, and printing its help overruns a fixed 200-byte buffer on the device.
See [PROTOCOL.md](PROTOCOL.md#never-ask-the-device-what-commands-it-has).

So `backup` decides which path to use from the image passed to `--uboot`. With
no `--uboot`, the running U-Boot is an unknown and the slow path is used.

## Is it safe to run an unofficial U-Boot?

It only ever executes from RAM. The boot ROM is in mask ROM and cannot be
overwritten; the flash is untouched unless you ask for a write. If a build is
wrong, the camera does not come back as a burn agent and you power-cycle.

The one thing worth checking before trusting a build is that it reads back what
is really there, which `backup` does for you: every chunk is checksummed on the
device with `crc32` and compared against what arrived over USB.
