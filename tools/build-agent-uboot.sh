#!/bin/bash
#
# Build the U-Boot this tool loads as its flashing agent.
#
# OpenIPC's released U-Boot works for flashing, but not for backing up:
#
#   * `usbtftp` is not compiled in, so there is no bulk device-to-host path and
#     a read-back falls back to hex dumps -- hours for a 16 MiB chip.
#   * set_default_env() ends with an unconditional saveenv(), so the U-Boot
#     writes a default environment to flash merely by starting, destroying an
#     erase block of whatever image is at its CONFIG_ENV_OFFSET.
#
# So this builds one with usbtftp enabled and that write removed. HiSilicon's
# own usbtftp does not work as shipped either -- see the patches below, each of
# which fixes something that wedges the device or makes it crawl.
#
# The result is never written to flash -- the boot ROM loads it into RAM -- so
# a bad build costs a power cycle, nothing more.
#
#   ./tools/build-agent-uboot.sh [output-dir]

set -euo pipefail

OUT=${1:-$(pwd)/output}
SOC=${SOC:-hi3518ev300}
WORK=$OUT/uboot-build
UPSTREAM=https://github.com/openipc/firmware/releases/download
TOOLCHAIN_URL=$UPSTREAM/toolchain/toolchain.hisilicon-hi3516ev200.tgz
UBOOT_REPO=https://github.com/OpenIPC/u-boot-hi3516ev200

say() { printf '\n==> %s\n' "$*"; }
for tool in curl git make python3 tar; do
    command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 1; }
done

mkdir -p "$WORK"

say "Fetching the toolchain"
[ -s "$WORK/toolchain.tgz" ] || curl -sSL --retry 3 -o "$WORK/toolchain.tgz" "$TOOLCHAIN_URL"
[ -d "$WORK/tc" ] || { mkdir -p "$WORK/tc"; tar xzf "$WORK/toolchain.tgz" -C "$WORK/tc"; }
TCBIN=$(dirname "$(find "$WORK/tc" -name 'arm-openipc-linux-musleabi-gcc' | head -1)")
export PATH="$TCBIN:$PATH" ARCH=arm CROSS_COMPILE=arm-openipc-linux-musleabi-

say "Fetching U-Boot"
[ -d "$WORK/u-boot" ] || git clone --depth 1 "$UBOOT_REPO" "$WORK/u-boot"

say "Patching"
python3 - "$WORK/u-boot" <<'PATCHEOF'
import pathlib, sys
root = pathlib.Path(sys.argv[1])


def edit(relative, replacements):
    """Apply literal replacements, preserving the file's line endings.

    usb3.h is CRLF and the rest of the tree is LF, so the patch text below is
    written with plain newlines and translated per file. Normalising a whole
    file's endings instead would compile just as well but makes its diff
    unreadable, and these patches are meant to be reviewed.
    """
    path = root / relative
    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode()
    for old, new in replacements:
        old = old.replace("\n", newline)
        new = new.replace("\n", newline)
        if new and new in text:
            continue                 # already applied
        if not new and old not in text:
            continue                 # a removal, already gone
        assert old in text, f"{relative}: could not find {old[:60]!r}"
        text = text.replace(old, new)
    path.write_bytes(text.encode())


# --- make usbtftp exist at all ---------------------------------------------

# Build usbtftp without CONFIG_CMD_USB, which would also pull in the USB host
# stack this board has no use for.
edit("cmd/Makefile", [("obj-$(CONFIG_CMD_USB) += usbtftp.o", "obj-y += usbtftp.o")])

edit("cmd/usbtftp.c", [
    # Its flash-read half sits behind #ifndef CONFIG_MMC, and this board sets
    # CONFIG_MMC. The real dependency is SPI flash.
    ("#ifndef CONFIG_MMC", "#ifdef CONFIG_CMD_SF"),
    # usb_stop() belongs to the host stack, which is not built here.
    ("\textern int usb_stop(void);\n", ""),
    ("\tusb_stop();\n",
     "\t/* host-stack call dropped; the PHY is already quiesced */\n"),
])


# --- do not write flash just by booting ------------------------------------

edit("common/env_common.c", [(
    "\tgd->flags |= GD_FLG_ENV_DEFAULT;\n\tsaveenv();\n}",
    "\tgd->flags |= GD_FLG_ENV_DEFAULT;\n"
    "\t/* No saveenv(): this U-Boot runs from RAM as a flashing agent, and\n"
    "\t * writing a default environment would destroy a block of the image\n"
    "\t * the host is about to read or has just written. */\n}",
)])


# --- 200-byte frames become 16 KiB -----------------------------------------
#
# Frame size is what makes a vendor read-back slow: 200 bytes per USB round
# trip is twenty-odd minutes for a 16 MiB chip over a link that could do it in
# seconds. The host learns the size from the head frame, so raising it needs no
# protocol change -- but the bulk IN buffer is a hard-coded 512 bytes in two
# other files, and a 16 KiB frame written into that corrupts the heap.

edit("drivers/usb/gadget/hiudc3/usb3.h", [(
    "#define HUAWEI_VENDOR_ID        0x12D1",
    "/* Bytes per usbtftp upload frame, and the bulk IN buffer that has to hold\n"
    " * one of them. The vendor fixed these at 200 and 512. */\n"
    "#define USBTFTP_FRAME_LEN       16384\n"
    "#define USB3_BULK_IN_BUF_SIZE   (USBTFTP_FRAME_LEN + 64)\n"
    "\n"
    "#define HUAWEI_VENDOR_ID        0x12D1",
)])

# usbtftp.c reaches usb3.h through usb3_drv.h, so both constants are in scope.
edit("cmd/usbtftp.c", [
    ("#define FRAME_LENGTH (200)", "#define FRAME_LENGTH (USBTFTP_FRAME_LEN)"),
    ("req->bufdma = (uint8_t *)malloc(512);",
     "req->bufdma = (uint8_t *)malloc(USB3_BULK_IN_BUF_SIZE);"),
])
edit("drivers/usb/gadget/hiudc3/usb3_drv.c", [
    ("req->bufdma = (uint8_t *)malloc(512);",
     "req->bufdma = (uint8_t *)malloc(USB3_BULK_IN_BUF_SIZE);"),
])
edit("drivers/usb/gadget/hiudc3/usb3_prot.c", [
    ("memset(req->bufdma, 0, 512);",
     "memset(req->bufdma, 0, USB3_BULK_IN_BUF_SIZE);"),
])

# usbtftp's flash read malloc()s the whole chunk it is asked for, so the heap
# is what bounds how much a backup moves per round trip. hi-common.h is
# included after the per-SoC header, so its define is the effective one.
edit("include/configs/hi-common.h", [(
    "#define CONFIG_SYS_MALLOC_LEN       (32 * SZ_128K)",
    "/* Raised for usbtftp, whose flash read malloc()s its whole chunk. 8 MiB\n"
    " * sits at the top of DRAM, clear of the 0x41000000 staging buffer even\n"
    " * when that holds a full 16 MiB image. */\n"
    "#define CONFIG_SYS_MALLOC_LEN       SZ_8M",
)])


# --- do not tear down the USB link the command arrived on ------------------
#
# do_usbtftp_upload() ends by calling udc_request(), which builds a *second*
# usb3_device_t and re-runs phy_hiusb_init() / usb3_common_init() /
# usb3_init() -- reinitialising the controller underneath the very session
# that carried the command, so the host's next transfer fails with EIO. The
# callback registered just above is already served by the usb3_handle_protocol
# loop that is running, so the command has nothing left to do but return.

edit("cmd/usbtftp.c", [(
    "\tusb_open_flag = 1;\n"
    "\n"
    "\t(void)udc_request();\n"
    "\n"
    "\tframe_count = 0;\n"
    "\n"
    "\tSetUSB3CallBackFunc(NULL);\n",

    "\tusb_open_flag = 1;\n"
    "\n"
    "\t/* Deliberately not udc_request(): it reinitialises the USB controller\n"
    "\t * underneath the session this command arrived on. The running\n"
    "\t * usb3_handle_protocol() loop already serves UREQ frames from the\n"
    "\t * callback set above, so return and let it. `usbtftp end` tears the\n"
    "\t * session down. */\n"
    "\treturn 0;\n",
), (
    # ... which means something else has to release what the command set up.
    "\t/* cmd is one byte */\n"
    "\t*bufflen = *bufflen + 1;\n"
    "\treturn 0;\n"
    "}\n"
    "#endif\n",

    "\t/* cmd is one byte */\n"
    "\t*bufflen = *bufflen + 1;\n"
    "\treturn 0;\n"
    "}\n"
    "\n"
    "static void usbtftp_release(void)\n"
    "{\n"
    "\ttypedef int (*USB3_HANDLE_REQUEST)(uint8_t * const buff, unsigned int * bufflen);\n"
    "\textern void SetUSB3CallBackFunc(USB3_HANDLE_REQUEST func);\n"
    "\n"
    "\tSetUSB3CallBackFunc(NULL);\n"
    "\tframe_count = 0;\n"
    "#ifndef CONFIG_DM_SPI_FLASH\n"
    "\tif (spiflash) {\n"
    "\t\tspi_flash_free(spiflash);\n"
    "\t}\n"
    "#endif\n"
    "\tspiflash = NULL;\n"
    "\tif (membuf) {\n"
    "\t\tfree(membuf);\n"
    "\t\tmembuf = NULL;\n"
    "\t}\n"
    "\tmem_len = 0;\n"
    "}\n"
    "#endif\n",
), (
    # `usbtftp end` is now the only way out of an upload session, so it has to
    # work whatever state that session is in. The vendor rejected it unless
    # usb_open_flag was set -- which is exactly when you most need it.
    "\tif (strncmp(argv[1], \"end\", 3) == 0) {\n"
    "\t\tif (0 == usb_open_flag) {\n"
    "\t\t\tgoto usage;\n"
    "\t\t}\n"
    "\n"
    "\t\tusb_open_flag = 0;\n"
    "\t\tprintf(\"usbtftp end\\n\");\n"
    "\t\tgoto done;\n"
    "\t}\n",

    "\tif (strncmp(argv[1], \"end\", 3) == 0) {\n"
    "\t\tusb_open_flag = 0;\n"
    "#ifdef CONFIG_CMD_SF\n"
    "\t\tusbtftp_release();\n"
    "#endif\n"
    "\t\tgoto done;\n"
    "\t}\n",
)])


# --- a failed command must not wedge the device ----------------------------
#
# The reply path for a command that returns non-zero sends a bare marker and,
# critically, never re-arms the bulk OUT endpoint -- so a single failed command
# leaves the device deaf until a power cycle, having also thrown away the
# console output that would have said why it failed.

edit("drivers/usb/gadget/hiudc3/usb3_prot.c", [(
    "void usb3_handle_protocol(void *dev)",

    "/* strcat() into a fixed buffer with no bounds check is how the vendor\n"
    " * builds every reply; keep the verdict even when output has filled it. */\n"
    "static void append_state(const char *marker)\n"
    "{\n"
    "\tsize_t used = strlen(tx_state);\n"
    "\n"
    "\tif (used + strlen(marker) >= sizeof(tx_state)) {\n"
    "\t\tused = 0;\n"
    "\t}\n"
    "\tstrcpy(tx_state + used, marker);\n"
    "}\n"
    "\n"
    "void usb3_handle_protocol(void *dev)",
), (
    "\t\tret = run_command(buf+3,0);\n"
    "\t\tif (ret) {\n"
    "\t\t\tusb3_bulk_in_transfer(dev, \"[EOT](ERROR)\\r\\n\");\n"
    "\t\t} else {\n"
    "\t\t\tstrcat(tx_state, \"[EOT](OK)\\r\\n\");\n"
    "\t\t\tusb3_bulk_out_transfer_cmd(pcd);\n"
    "\t\t\tusb3_bulk_in_transfer(dev, tx_state);\n"
    "\t\t\tmemset(tx_state, 0, 200);\n"
    "\t\t}",

    "\t\tret = run_command(buf+3,0);\n"
    "\t\t/* Both verdicts take the same shape: append the marker, re-arm the\n"
    "\t\t * OUT endpoint, then send. */\n"
    "\t\tappend_state(ret ? \"[EOT](ERROR)\\r\\n\" : \"[EOT](OK)\\r\\n\");\n"
    "\t\tusb3_bulk_out_transfer_cmd(pcd);\n"
    "\t\tusb3_bulk_in_transfer(dev, tx_state);\n"
    "\t\tmemset(tx_state, 0, sizeof(tx_state));",
)])

print("    patches applied")
PATCHEOF

say "Building for $SOC"
cd "$WORK/u-boot"
cp "config-$SOC" .config
cp "reg_info_$SOC.bin" .reg
make -j"$(nproc)" >/dev/null
make u-boot-z.bin >/dev/null

mkdir -p "$OUT"
cp "u-boot-$SOC.bin" "$OUT/u-boot-$SOC-agent.bin"
say "Done -- $OUT/u-boot-$SOC-agent.bin"
ls -l "$OUT/u-boot-$SOC-agent.bin"
echo
echo "Check it with:  hisiburn inspect $OUT/u-boot-$SOC-agent.bin"
