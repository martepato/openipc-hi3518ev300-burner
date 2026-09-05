# The HiSilicon USB flashing protocol

This is what HiTool/HiBurn does over USB, written down.

Everything here is **verified against a USBPcap capture of a successful
HiBurn 5.3 flash** of a Hi3518EV300 (Xiaomi MJSXJ02HL), cross-read against
HiSilicon's own GPL U-Boot sources as shipped in OpenIPC's
[`u-boot-hi3516ev200`][uboot]. The frames quoted below are bytes that were
actually on the wire; `tests/fixtures/captured_frames.json` holds them and the
test suite asserts this implementation reproduces them exactly.

[uboot]: https://github.com/OpenIPC/u-boot-hi3516ev200

## Overview

Flashing is two stages. Both present **the same USB identity** and share most
of their framing, but they are different programs with different capabilities:

| Stage | Who is running | What it does |
|---|---|---|
| 1 | boot ROM | takes three images into SRAM/DDR, then starts U-Boot |
| 2 | U-Boot burn agent | raw uploads into RAM + U-Boot console commands |

Between them the device re-enumerates: same vendor and product id, new bus
address. There is no descriptor difference to key off — telling the two apart
means asking (see [Distinguishing the stages](#distinguishing-the-stages)).

## USB identity

Captured device and configuration descriptors:

```
idVendor          0x12D1   (Huawei; HiSilicon is a Huawei subsidiary)
idProduct         0xD001   — both stages
bcdUSB            0x0200
bInterfaceClass   0xFF     vendor-specific  (subclass 0xFF, protocol 0xFF)
bNumEndpoints     2        bulk IN 0x81, bulk OUT 0x01
wMaxPacketSize    512
iManufacturer     "Hislicon"     (sic - the typo is in the vendor's gadget source)
iProduct          "HiUSBBurn"
```

The misspelt manufacturer string is a useful fingerprint: it comes from
``string_manu[] = {'H','i','s','l','i','c','o','n'}`` in the vendor's
``cmd/usbtftp.c``, so seeing it confirms the device is running that gadget
code and not something else answering on the same ids.

The vendor-specific interface class is why the Windows workflow needs Zadig:
no stock Windows driver binds a `0xFF` interface, so libusbK has to be
installed against the device. macOS and Linux need nothing — no kernel driver
claims a vendor-specific interface, so libusb can open it directly.

> `usb3.h` in the vendor tree carries a commented-out `0x3609` next to the
> product id. It is not what the silicon enumerates with; the capture shows
> `0xD001` for the boot ROM as well as the agent.

## Framing

All multi-byte fields are **big-endian**. There is **no checksum and no
sequence number anywhere in the USB protocol**.

> The vendor header `usb3_prot.h` documents a `TYPE SEQ ~SEQ … CRC` layout,
> and OpenIPC's `defib` implements exactly that. **That is the UART protocol.**
> Reading it as the USB one produces frames this device ignores. The USB
> framing is the simpler thing below.

| Opcode | Name | Sent as | Answered with |
|---|---|---|---|
| `0xFE` | HEAD | `FE <length:4> <address:4>` | `AA 00` |
| `0xFE` | OPEN | `FE <token:4> <token:4>` (both words equal) | `AA 00` |
| `0xFA` | START | `FA <token:4> <token:4>` | `AA 00` |
| `0xDA` | DATA | `DA <payload ≤511>` | *nothing* |
| `0xED` | TAIL | `ED` | `AA 00`, or `55` if bytes are outstanding |
| `0xAB` | CMD | `AB <length:2> <command text>` | console output + `[EOT]` |
| `0xFB` | REQ | `FB` | next frame of a flash read-back |

Three details worth stating plainly:

- **A HEAD frame whose length equals its address is not a transfer.** The
  device reads that as "open the channel" and stays in command mode. HiBurn
  uses it once to open a boot ROM session, and the same shape with opcode
  `0xFA` to sync before each agent upload. The token is ignored by the device.
- **Replies are NUL-terminated.** The device sends `strlen(s) + 1` bytes, so a
  bare ACK arrives as `AA 00`. Parse to the first NUL.
- **`0xDA` carries no sequence and no checksum.** The payload starts at
  offset 1, and never exceeds one max packet minus the opcode: **511 bytes**.

## Stage 1 — the boot ROM

```
-> FE <token:4> <token:4>          open the session
<- AA 00
   for each of the three images:
-> FE <length:4> <address:4>       announce
<- AA 00
-> DA <≤511 bytes>   × N           streamed back to back, none acknowledged
-> ED                              close
<- AA 00
```

Exactly **seven** replies cross the wire for a whole stage-1 run: one for the
open, then a header and a tail ACK for each of three images. Waiting for an
ACK per DATA frame deadlocks.

### The three images

| # | Image | Length | Address |
|---|---|---|---|
| 1 | DDR-init stub | `0x40` (64) | `0x04013000` |
| 2 | SPL | `0x6000` (24576) | `0x04010500` |
| 3 | U-Boot | whole file | `0x41000000` |

The SPL is byte-identical to the **first `0x6000` bytes of the U-Boot image**
— confirmed by reassembling both from the capture. HiBurn sends a flat
`0x6000`; it does *not* scan for a compressed-payload boundary the way UART
tools do.

### The DDR stub is not just DDR init

The 64-byte stub is a short ARM routine plus a literal pool:

```asm
push {lr}
ldr  r0, [pc, #0x24]   ; 0x1202013C  = SYS_CTRL_REG_BASE + REG_SC_GEN1
ldr  r1, [pc, #0x24]   ; 0x444F574E  = START_MAGIC, ASCII "DOWN"
str  r1, [r0]
ldr  r0, [pc, #0x20]   ; 0x12020140  = REG_SC_GEN2
ldr  r1, [pc, #0x20]   ; 0x7A696A75
str  r1, [r0], #4
str  lr, [r0]          ; entry address into REG_SC_GEN3
pop  {pc}
```

That `START_MAGIC` write is the load-bearing part. `download_boot()` in the
U-Boot being loaded reads `REG_START_FLAG` and only enters its download loop
if it finds exactly `0x444F574E`; otherwise it boots the camera normally and
**no burn agent ever appears**. Both the register and the magic are defined in
`arch/arm/include/asm/arch-hi3518ev300/platform.h`.

This is the one field where a UART-derived profile is actively wrong: OpenIPC
`defib`'s `hi3518ev300` profile carries `0x12345678` in that slot. The stub is
otherwise byte-identical. `ChipProfile` refuses to construct without the right
magic, so the mistake fails loudly instead of producing a camera that quietly
reboots mid-flash.

## Stage 2 — the U-Boot burn agent

On enumerating, the agent sends an unsolicited greeting on bulk IN:

```
"start download process.\0"
```

### Running commands

```
-> AB <length:2> <command text>       length is strlen, text is not NUL-terminated
<- <console output> "[EOT](OK)\r\n\0"
```

The reply arrives only once the command has *finished*, so a ten-megabyte
`sf erase` simply means a long first read. While it runs the device answers
with **zero-length packets** — the capture shows eight of them during one
`sf erase 0x50000 0x300000`. A client must treat an empty read as "still
working", not as a reply.

Replies are copied into a fixed 200-byte buffer, so long console output is
truncated mid-line — an `sf write` reply commonly ends at `Written:` with its
`OK` cut off. That is cosmetic and not a failed write: HiBurn's own logs show
the identical truncation on the identical commands.

Progress steps within a reply are separated by bare carriage returns, meant to
overwrite each other on a terminal. A host that logs the text unchanged will
find them overwriting its own output too, so translate them before printing.

Every reply begins with a space the device prepends.

**A failed command ends the session.** On the `[EOT](ERROR)` path the
device-side handler returns without re-arming its OUT endpoint, so it accepts
nothing further. Recovering means power-cycling back into download mode.

### Uploading an image

```
-> FA <token:4> <token:4>      sync
<- AA 00
-> FE <length:4> <address:4>   announce; length is the exact file size
<- AA 00
-> <length raw bytes>          no framing, no opcodes, DMA'd straight to address
-> ED
<- AA 00
```

Note the asymmetry with stage 1: the **boot ROM wants `DA`-framed data, the
agent wants a raw stream**. HiBurn submits the whole image as a single bulk
transfer; splitting it host-side is equivalent, because the device counts
bytes down against the announced length and only completes on a short packet.

### Distinguishing the stages

**Read the banner. Do not send a command.**

On every SET_CONFIGURATION the burn agent emits, unprompted, on bulk IN:

```
"start download process.\0"
```

The boot ROM never does — verified in the capture, where the agent's greeting
follows its SET_CONFIGURATION and the boot ROM goes straight from its own to
the host's first OPEN frame with nothing in between. HiBurn uses this too: it
never aims a `getinfo` at a boot ROM.

The banner can be asked for again. `usb3_do_set_config()` re-sends it on each
SET_CONFIGURATION, so a missed read is recoverable by re-issuing the request
rather than being mistaken for a boot ROM.

### Never ask the device what commands it has

There is no safe way, and two separate reasons.

`help <name>` cannot answer it. `cmd_usage()` returns 1 unconditionally and
`_do_help()` ORs that into its result, so `help` reports **failure whether the
command exists or not**:

```c
if (cmdtp != NULL)
        rcode |= cmd_usage(cmdtp);   /* cmd_usage() always returns 1 */
else {
        printf("Unknown command '%s' ...");
        rcode = 1;
}
```

And trying it costs the session. `cmd_usage()` prints the name, the usage line
and the whole multi-line help, each through `udc_puts()`:

```c
void udc_puts(const char *s)
{
    if (strlen(s) > 200) return;
    else { if (usb_out_open == 1) strcat(tx_state, s); }
}
```

`tx_state` is `char[200]`. The guard checks the length of *each* string, never
the running total, so several printfs of help text **overrun the buffer on the
device** and it stops responding. Observed: `help usbtftp` against a U-Boot
that does have `usbtftp`, followed by a write timeout on the next command.

Read capabilities out of the U-Boot image instead — the host loaded it, so it
has the bytes. `hisiburn inspect <u-boot.bin>` does exactly that.

### Never send an opcode the device may not implement

`usb3_handle_protocol()` is a chain of `if`/`else if` on the opcode byte **with
no final `else`**. An unrecognised frame therefore gets no reply *and* — the
part that bites — no `usb3_bulk_out_transfer_cmd()` call, which is what re-arms
the OUT endpoint for the next frame. The device simply stops receiving.

So a `getinfo` aimed at a boot ROM does not merely time out; it ends the
session. Everything after it goes unanswered, including a perfectly correct
OPEN frame, which looks exactly like a dead or mis-framed device.

Two consequences for a host implementation:

- Discriminate with the banner, never with a command.
- SET_CONFIGURATION is the way back. `usb3_do_set_config()` calls
  `usb3_bulk_out_transfer()`, re-arming the endpoint, so a wedged device is
  recoverable without a power cycle.

And one thing not to do: `clear_halt` is for an actual stall. It also resets
the endpoint's data toggle, so issuing it after a mere timeout desynchronises
host and device and breaks the next exchange.

## Flashing sequence

Per partition, verbatim from the capture:

```
mw.b   0x41000000 0xFF <padded-length>    pre-fill the staging buffer
<upload the image to 0x41000000>
sf probe 0
sf erase <partition-offset> <partition-size>
sf write 0x41000000 <partition-offset> <padded-length>
```

`<padded-length>` is the image size rounded up to the 64 KiB erase block —
1,908,952 bytes becomes `0x1e0000`, 5,693,440 becomes `0x570000`. The `0xFF`
pre-fill means the tail of a short image is written as erased flash rather
than as leftovers from the previous partition. The erase covers the whole
partition; the write covers only the padded image.

The boot partition is special in HiBurn: it is written straight from
`0x41000000` with no upload, because stage 1 left the U-Boot image there.
`hisiburn` uploads it explicitly instead — one extra transfer, in exchange for
`--only boot` working on a camera that is already running the agent.

Finally, `reset`.

## Getting data back off the camera

The agent's only device-to-host path is the reply to a command, and that
reply is copied into a fixed 200-byte buffer. There is no bulk read unless
U-Boot was built with `usbtftp` — `cmd/Makefile` gates it on `CONFIG_CMD_USB`,
which OpenIPC's config leaves unset, and its flash-read half is further inside
`#ifndef CONFIG_MMC`, which that config sets. Two independent reasons it is
absent from a stock OpenIPC build.

What remains is `md.b`, whose hex dump comes back in that reply buffer:

```
41000000: 27 05 19 56 63 fd 39 c1 5e 98 76 aa 00 1d 40 5a    '..Vc.9.^.v...@Z
```

At about 79 characters per 16-byte line, two lines is both the most that fits
and the most that is *safe* to ask for: `udc_puts()` appends to the buffer
with no bounds check, so a third line would overrun it on the device. That
caps a read at **32 bytes per round trip**, and a round trip measured about
**2 ms** in the capture — so roughly 18 minutes for a 16 MiB chip.

Slow, but checkable: `crc32` runs on the device over the same range, so what
the host assembled from the text can be compared against what the device
actually holds, per chunk. That turns an error-prone text channel into one
that cannot silently corrupt a backup.

## The agent U-Boot writes to flash when it starts

This one costs data, so it is worth stating plainly.

HiSilicon's U-Boot carries a modified `set_default_env()` in
`common/env_common.c`, which mainline does not:

```c
gd->flags |= GD_FLG_ENV_READY;
gd->flags |= GD_FLG_ENV_DEFAULT;
saveenv();          /* unconditional */
```

So whenever this U-Boot starts and cannot load a valid environment, it
**writes a default one to flash** — at its own `CONFIG_ENV_OFFSET`, which for
OpenIPC's `u-boot-hi3518ev300-universal.bin` is 0x40000, matching the OpenIPC
16 MB layout (`256k(boot),64k(env),…`).

That is harmless when the camera's layout agrees. It is not harmless
otherwise. Restore a vendor image whose kernel begins at 0x40000, and the
next time any host tool loads this U-Boot as its agent, one 64 KiB block of
that kernel is replaced by a U-Boot environment — recognisable as a CRC32
followed by NUL-separated `key=value` text:

```
0x00040000  d0 bf 03 b7 61 72 63 68 3d 61 72 6d 00 62 61 73  |....arch=arm.bas|
```

Two consequences:

- **Verify in the same session as the write.** The environment save happens at
  U-Boot startup, before any of the tool's own writes, so what a restore
  writes is intact at the end of that session. A *later* session damages the
  image before it can read it — which makes a standalone verify of a foreign
  layout report a difference it caused itself. `restore --verify` exists for
  this.
- **A camera left in that state may not boot**, because the block holding the
  kernel's uImage header is gone. Re-restore before relying on it.

Nothing here is specific to this tool: any host tool that loads this U-Boot to
reach the flash has the same effect, HiTool included.

## Provenance

| Claim | Basis |
|---|---|
| USB ids, endpoints, descriptors | captured descriptors |
| Every frame layout | captured frames, byte-compared in the test suite |
| DATA payload limit of 511 | maximum observed across 513 captured frames |
| ACK cadence (7 replies in stage 1) | counted from the capture |
| The three stage-1 images and addresses | captured headers; payloads reassembled |
| SPL == first 0x6000 of U-Boot | both reassembled from the capture and compared |
| `START_MAGIC` semantics | captured stub, decoded against vendor `platform.h` |
| Flashing command sequence | captured command frames + HiBurn log |
| Zero-length packets during long commands | observed during `sf erase` |
| Error path stops the session | vendor source (`usb3_prot.c`), not exercised in the capture |
| Boot ROM stalls on a `FA` first frame | observed on hardware (Hi3518EV300, macOS) |
| Only the agent sends the banner | capture: agent greets after SET_CONFIGURATION, boot ROM does not |
| An unimplemented opcode un-arms the OUT endpoint | vendor source (no `else` in `usb3_handle_protocol`), and observed: a `getinfo` to the boot ROM left it silent to every later frame |
| SET_CONFIGURATION re-arms and re-greets | vendor source (`usb3_do_set_config`) |

The one item resting on source rather than observation is the last: a failed
command was never provoked in the captured run.

## For the record: what a source-only reading got wrong

The first version of this document was written from the vendor sources alone,
before a capture existed. It was right about the USB identity, the agent's
`FE`/`ED` frames, the raw upload path and the whole flashing sequence — and
wrong about five things, every one of which the capture settled:

1. **The boot ROM's product id.** Guessed `0x3609` from a commented-out
   constant; it is `0xD001`, the same as the agent.
2. **Stage-1 framing.** Read the `usb3_prot.h` comment as the USB layout —
   14-byte headers, `SEQ`/`~SEQ`, CRC-16, 1024-byte payloads. That is the UART
   protocol. USB uses 9-byte headers, no sequence, no checksum, 511-byte
   payloads.
3. **The session-open frame.** Missed entirely.
4. **ACK cadence.** Assumed every DATA frame was acknowledged; only headers
   and tails are.
5. **The DDR stub's `START_MAGIC`.** Shipped `defib`'s UART value. Four bytes,
   and without the right ones the loaded U-Boot never enters download mode.

The command frame was also `AB <seq> <~seq> <text> 00` rather than
`AB <length:2> <text>`; this firmware tolerates it, but it is not what the
vendor sends and the serial variant does read that length field.
