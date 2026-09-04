# The HiSilicon USB flashing protocol

This is what HiTool/HiBurn does over USB, written down. Everything here is
derived from HiSilicon's own GPL-licensed U-Boot sources — the ones OpenIPC
ships in [`u-boot-hi3516ev200`][uboot] — cross-checked against a real HiBurn
session log and against OpenIPC's [`defib`][defib], which drives the same boot
ROM over UART.

[uboot]: https://github.com/OpenIPC/u-boot-hi3516ev200
[defib]: https://github.com/OpenIPC/defib

## Overview

Flashing is two stages, and each stage speaks a *different* protocol over the
same pair of bulk endpoints:

| Stage | Who is running | USB id | What it does |
|---|---|---|---|
| 1 | mask ROM | `12d1:3609` | accepts CRC-checked frames into SRAM/DDR; starts U-Boot |
| 2 | U-Boot burn agent | `12d1:d001` | raw DMA into RAM + U-Boot console commands |

The camera enters stage 1 when the reset button is held as USB power is
applied. Once stage 1 has loaded and started a U-Boot built with HiSilicon's
`usbtftp` support, that U-Boot re-enumerates and stage 2 begins.

## USB identity

From `drivers/usb/gadget/hiudc3/usb3_pcd.c` and `usb3.h`:

```
idVendor          0x12D1   (Huawei; HiSilicon is a Huawei subsidiary)
idProduct         0xD001   (burn agent)   — 0x3609 is the boot ROM
bInterfaceClass   0xFF     vendor-specific
bNumEndpoints     2        bulk IN 0x81, bulk OUT 0x01
wMaxPacketSize    512 (high speed) / 1024 (super speed)
iManufacturer     "Hisilicon"
iProduct          "HiUSBBurn"
```

The vendor-specific interface class is why the Windows workflow needs Zadig:
no stock Windows driver binds a `0xFF` interface, so libusbK has to be
installed against the device. macOS and Linux need nothing — no kernel driver
claims a vendor-specific interface, so libusb can open it directly.

## Checksum

Both stage-1 framing and the serial fallback use one CRC, transcribed from
`calc_crc16()` in `common/download_process.c`:

```c
for (i = 0; i < length; i++)
    crc = ((crc << 8) | packet[i]) ^ table[(crc >> 8) & 0xFF];
for (i = 0; i < 2; i++)                       /* flush */
    crc = ((crc << 8) | 0)         ^ table[(crc >> 8) & 0xFF];
```

The table is the standard CCITT one (polynomial `0x1021`), but the update step
is **not** standard CRC-16/CCITT: the message byte is shifted into the low half
of the register rather than XORed into the high half. A stock CRC routine
produces frames the device silently NAKs. See `hisiburn/crc.py`.

Checksums are transmitted big-endian, as is every multi-byte field below.

## Stage 1 — the mask ROM

Frame layouts are documented in `drivers/usb/gadget/hiudc3/usb3_prot.h`:

```
FILE FRAME: TYPE(1) SEQ(1) ~SEQ(1) FILE(1) LENGTH(4) ADDRESS(4) CRC(2)   14 bytes
DATA FRAME: TYPE(1) SEQ(1) ~SEQ(1) DATA(0..1024)     CRC(2)
EOT  FRAME: TYPE(1) SEQ(1) ~SEQ(1) CRC(2)                                 5 bytes
```

| Constant | Value |
|---|---|
| `FRAME_FILE` | `0xFE` |
| `FRAME_DATA` | `0xDA` |
| `FRAME_EOT` | `0xED` |
| `FRAME_INQUIRE` | `0xCD` |
| `FILE_RAMINIT` | `1` |
| `FILE_USB` | `2` |
| ACK / NAK | `0xAA` / `0x55` |

The device answers every frame with a single status byte. A NAK means resend.

**Cross-check.** `defib` opens a transfer to this same boot ROM over UART with
the 14-byte header `FE 00 FF 01 <length:4> <address:4> <crc:2>`. That is
exactly a FILE frame with `SEQ=0`, `~SEQ=0xFF` and `FILE=1` — so the frame
layout above is corroborated by a working implementation, and only the pipe
underneath differs.

### Staging sequence

Three images, in order — the same sequence `defib` uses over UART:

| # | Image | Address | Notes |
|---|---|---|---|
| 1 | DDR init | `0x04013000` | fixed 64-byte blob, brings the memory controller up |
| 2 | SPL | `0x04010500` | the front of the U-Boot binary, into SRAM |
| 3 | U-Boot | `0x41000000` | the whole image, into DRAM; starts on EOT |

The SPL slice must stop where U-Boot's compressed payload begins (an LZMA
`5D` + valid dictionary size, or a gzip `1F 8B 08`, rounded down to 1 KiB).
Bytes past that boundary land in SRAM the boot ROM is using for its own stack,
which hangs the chip mid-upload.

Everything in this stage lands in volatile memory. A wrong frame gets NAKed or
ignored; nothing reaches flash, so a failed attempt costs a power cycle.

## Stage 2 — the U-Boot burn agent

Implemented device-side by `usb3_handle_protocol()` in
`drivers/usb/gadget/hiudc3/usb3_prot.c`. The host writes a single opcode byte
(plus arguments) to bulk OUT; the device replies on bulk IN. There is **no
checksum and no sequence checking** in this stage.

| Opcode | Name | Host sends | Device answers |
|---|---|---|---|
| `0xFA` | START | `FA` | `AA` |
| `0xFE` | HEAD | `FE <length:4> <address:4>` | `AA` |
| `0xAB` | CMD | `AB <seq> <~seq> <command…> 00` | console output + `[EOT](OK\|ERROR)` |
| `0xED` | TAIL | `ED` | `AA`, or `55` if bytes are still outstanding |
| `0xFB` | REQ | `FB` | next frame of a flash read-back |

Three details that are easy to get wrong:

- **The command text starts at offset 3.** The device calls
  `run_command(buf + 3, 0)`; bytes 1 and 2 are the sequence pair and are not
  examined.
- **`length == address` in a HEAD frame is not a transfer.** The device reads
  it as "open the output channel", sets `usb_connected`, and stays in command
  mode. Send it once at the start of a session; never let a real transfer take
  that shape.
- **Replies are NUL-terminated.** `usb3_bulk_in_transfer()` sends
  `strlen(s) + 1` bytes, so a bare ACK arrives as `AA 00`. Parse up to the
  first NUL and ignore the rest of the packet.

### Uploading an image

```
-> FE <length:4> <address:4>     announce
<- AA
-> <length raw bytes>            DMA'd straight to address, unframed
-> ED                            close
<- AA
```

The device arms its OUT endpoint for the whole announced length (up to 16 MiB
minus one max-packet), so the host can stream in whatever chunk size it likes.

### Running commands

The reply arrives only once the command has *finished*, so a ten-megabyte
`sf erase` simply means a long first read — not a hang. Budget the timeout by
how much flash the command touches.

The agent copies its reply into a fixed 200-byte buffer, so long console
output is truncated. That is cosmetic for `sf erase`, whose progress spinner
gets cut off mid-line, and is visible in HiBurn's own logs too.

**A failed command ends the session.** On the `[EOT](ERROR)` path the device
returns without re-arming its OUT endpoint, so it accepts nothing further.
Recovering means power-cycling back into download mode.

### Flashing sequence

This is what HiBurn does per partition, and what `hisiburn flash` reproduces:

```
mw.b   0x41000000 0xFF <padded-length>    pre-fill the staging buffer
<upload the image to 0x41000000>
sf probe 0
sf erase <partition-offset> <partition-size>
sf write 0x41000000 <partition-offset> <padded-length>
```

`<padded-length>` is the image size rounded up to the 64 KiB erase block. The
pre-fill with `0xFF` means the tail of a short image is written as erased
flash rather than as leftover bytes from the previous partition.

The boot partition is special: HiBurn writes it straight from `0x41000000`
without a download first, because the U-Boot that stage 1 staged there is
already the image that belongs in flash.

## Confidence

| Claim | Basis |
|---|---|
| Stage-2 opcodes, offsets, replies | read from the device-side handler |
| Stage-2 flashing sequence | read from a real HiBurn session log |
| USB ids, endpoints, descriptors | read from the gadget's descriptor tables |
| CRC | transcribed from vendor source; table verified byte-for-byte |
| Stage-1 frame layout | vendor header, corroborated by `defib`'s working UART implementation |
| Stage-1 USB handshake | **inferred.** The frames are documented; whether the boot ROM wants an inquire or any preamble before the first FILE frame over USB is not. |

The last row is the one open question. It is also the safe one to experiment
with: stage 1 only writes to volatile memory.
