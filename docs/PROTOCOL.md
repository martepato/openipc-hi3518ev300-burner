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
device-side handler sends the marker alone and returns *without re-arming its
OUT endpoint*, so the device accepts nothing further and the console output
that would have explained the failure is discarded with it. Recovering means
power-cycling back into download mode. (The agent U-Boot built by
`tools/build-agent-uboot.sh` gives both verdicts the same path, so a failed
command there is recoverable and says why it failed.)

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

Writing flash is well served by the protocol. Reading it back is not, and the
two paths below differ by two orders of magnitude.

### Without `usbtftp`: hex dumps

A stock OpenIPC U-Boot has no device-to-host bulk path at all. `cmd/Makefile`
gates `usbtftp.o` on `CONFIG_CMD_USB`, which that config leaves unset, and the
command's flash-read half is further inside `#ifndef CONFIG_MMC`, which that
config sets — two independent reasons it is absent.

What remains is the reply to a command, copied into a fixed 200-byte buffer.
So: `md.b`, whose hex dump comes back in that buffer.

```
41000000: 27 05 19 56 63 fd 39 c1 5e 98 76 aa 00 1d 40 5a    '..Vc.9.^.v...@Z
```

At about 79 characters per 16-byte line, two lines is both the most that fits
and the most that is *safe* to ask for: `udc_puts()` appends with no bounds
check, so a third line overruns the buffer on the device. That caps a read at
**32 bytes per round trip**.

A round trip measured about 2 ms in the Windows capture, which suggested 18
minutes for a 16 MiB chip. It takes **about two hours** on macOS: the same
exchange costs roughly 7 ms there, and 500,000 of them is where the time goes.
Per-round-trip latency, not throughput, is the whole cost of this path — which
is exactly why the fix is a bigger frame rather than a faster link.

Slow, but checkable: `crc32` runs on the device over the same range, so what
the host assembled from the text can be compared against what the device
actually holds, per chunk. That turns an error-prone text channel into one
that cannot silently corrupt a backup.

### With `usbtftp`: bulk frames

`usbtftp <offset> <name> <length>` reads the range into a device-side buffer
and arms a callback; the host then drives the transfer one frame per `FB`:

```
-> AB <len:2> "usbtftp 0xf90000 backup.bin 0x70000"
<- <output> "[EOT](OK)\r\n\0"          the command returns; it does not block

-> FB
<- FE <total:4> <frame_length:4>      once, first
-> FB
<- DA <up to frame_length bytes>      repeated
   ...
-> FB
<- ED                                 no more data

-> AB <len:2> "usbtftp end"           releases the device-side buffer
<- <output> "[EOT](OK)\r\n\0"
```

The `FE` head frame is 9 bytes and carries both the total the device will send
and the size of each `DA` frame, so a host never has to guess a read size — ask
for `frame_length + 1` and let a short packet end each frame. The device sends
`ED` when the total is exhausted, and keeps re-sending `ED` for any further
`FB`, so reading through to the tail rather than stopping on a byte count is
both safe and what keeps the pipe synchronised.

Three device-side properties are worth knowing before relying on this:

* **The command `malloc()`s the whole range.** `length` is bounded by
  `CONFIG_SYS_MALLOC_LEN`, not by the protocol, and the failure mode is a
  failed command — which on a stock reply path wedges the device. Read in
  chunks sized against the arena.
* **Only one session at a time.** A second `usbtftp <off> <file> <len>` before
  `usbtftp end` is rejected outright, so the release must happen even on an
  error part-way through a read.
* **The vendor's frame length is 200 bytes**, which makes this path about 20
  minutes for a 16 MiB chip — better than hex dumps, still not good. The size
  is announced in the head frame, so raising it is a device-side change the
  host needs no knowledge of; the agent build uses 16 KiB and reads a whole
  chip in about a minute.

As shipped, though, `do_usbtftp_upload()` ends by calling `udc_request()`,
which reinitialises the USB controller underneath the session that carried the
command — the host's next transfer fails with `EIO` before any flash comes
back. The command does not work at all until that call is removed. See
[AGENT-UBOOT.md](AGENT-UBOOT.md#what-the-script-changes).

Both paths get the same `crc32` check per chunk. A fast transfer is not a
trusted one.

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

## Which firmware a dump came from

Nothing on the wire says this — it is a property of the image, and worth
recording because the obvious method is wrong.

The kernel and rootfs are compressed, and the bootloader's version string is
its own rather than the firmware's. What does say is the settings partition at
0xF90000: a JFFS2 area holding a few tiny text files the camera's updater
writes, `os-release` (`ISA_VERSION=`) and, on older firmware, `app.ver`
(`appver=`).

**Grepping for the version string does not work.** JFFS2 is log-structured: a
write appends a new node and leaves the old one in place, so a dump carries
every version a file ever had. A dump taken from a camera running 4.5.6_0168
contains two copies of `4.5.6_0168`, thirty-one of `4.0.5_0105` and one of
`4.0.4_0073` — and the live one is neither the first nor, reliably, the last.

The node headers settle it. Each dirent and inode node carries a `version`
counter that increments per write, so the newest dirent for a name gives the
live inode, and that inode's data nodes applied in version order give its
contents. A dirent whose newest version names inode 0 is a deletion.

Node headers are worth validating properly while walking them: magic (0x1985)
and node type alone still match noise about once per 64 KiB, and a false
dirent would name a false inode and yield a plausible-looking wrong answer.
The header CRC closes that — note that JFFS2 computes it over the first 8
bytes with the opposite convention to zlib's, seeding with ~0 and not
inverting the result:

```python
crc = (zlib.crc32(header[:8], 0xFFFFFFFF) ^ 0xFFFFFFFF) & 0xFFFFFFFF
```

That validated 34 of 34 nodes in a real settings partition and rejects
everything else in a 16 MiB image.

One caution about what else is in there: `device.conf` and `.product_config`
hold the camera's MAC, its cloud device id, its P2P id and its cloud auth key
alongside the model name. `hisiburn` reads the model and vendor keys and
nothing else, deliberately — a tool that summarises a dump should not be how
those end up in a scrollback.

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
| Error path stops the session | vendor source (`usb3_prot.c`), then observed on hardware: a failed command left the device deaf to every frame after it |
| Boot ROM stalls on a `FA` first frame | observed on hardware (Hi3518EV300, macOS) |
| Only the agent sends the banner | capture: agent greets after SET_CONFIGURATION, boot ROM does not |
| An unimplemented opcode un-arms the OUT endpoint | vendor source (no `else` in `usb3_handle_protocol`), and observed: a `getinfo` to the boot ROM left it silent to every later frame |
| SET_CONFIGURATION re-arms and re-greets | vendor source (`usb3_do_set_config`) |
| `usbtftp` absent from OpenIPC's build | searched the released binary's decompressed payload — ASCII and UTF-16 — after the config file suggested it; the config alone would not have been evidence |
| `usbtftp` frame layout (`FE`/`DA`/`ED` on `FB`) | vendor source (`do_upload` in `cmd/usbtftp.c`), then exercised against a rebuilt U-Boot |
| `udc_request()` breaks the live session | observed on hardware: `EIO` on the first `FB` after `sf probe` succeeded, traced to the re-init in vendor source |
| 512-byte bulk IN buffer | vendor source (`usb3_drv.c`, `usb3_prot.c`) |
| Read-back at ~7 ms per round trip on macOS | measured: a full-chip `md.b` backup took about two hours |
| Firmware version lives in the settings partition | two dumps from one camera, at known firmware versions, agreeing across `os-release` and `app.ver` |
| JFFS2 header CRC convention | 34 of 34 nodes in a real settings partition; zlib's own convention matched 0 |

The last item is worth its own note. The 2 ms round trip in the Windows capture
was extrapolated here to "about 18 minutes" for a chip that in fact took two
hours to read on macOS. A number measured on one host's USB stack does not
transfer to another's, and this document said so too late.

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
