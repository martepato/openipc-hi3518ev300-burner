#!/bin/bash
#
# Build the U-Boot this tool loads as its flashing agent.
#
# OpenIPC's released U-Boot works for flashing, but two things in it make
# backups impractical:
#
#   * `usbtftp` is not compiled in, so there is no bulk device-to-host path and
#     a read-back falls back to hex dumps -- hours for a 16 MiB chip.
#   * set_default_env() ends with an unconditional saveenv(), so the U-Boot
#     writes a default environment to flash merely by starting, destroying an
#     erase block of whatever image is at its CONFIG_ENV_OFFSET.
#
# This builds one with usbtftp enabled and that write removed. It is never
# written to flash -- the boot ROM loads it into RAM -- so a bad build costs a
# power cycle, nothing more.
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
python3 - "$WORK/u-boot" <<'PYEOF'
import pathlib, sys
root = pathlib.Path(sys.argv[1])

def edit(relative, replacements):
    path = root / relative
    text = path.read_text()
    for old, new in replacements:
        if new in text:      # already applied
            continue
        assert old in text, f"{relative}: could not find {old[:60]!r}"
        text = text.replace(old, new)
    path.write_text(text)

# Build usbtftp without CONFIG_CMD_USB, which would also pull in the USB host
# stack this board has no use for.
edit("cmd/Makefile", [("obj-$(CONFIG_CMD_USB) += usbtftp.o", "obj-y += usbtftp.o")])

edit("cmd/usbtftp.c", [
    # Its flash-read half sits behind #ifndef CONFIG_MMC, and this board sets
    # CONFIG_MMC. The real dependency is SPI flash.
    ("#ifndef CONFIG_MMC", "#ifdef CONFIG_CMD_SF"),
    # 200-byte frames are what make a vendor read-back slow. The host learns
    # the size from the head frame, so raising it needs no protocol change.
    ('#include "common.h"',
     '#include "common.h"\n\n#define USBTFTP_FRAME_LEN 16384'),
    ("#define FRAME_LENGTH (200)", "#define FRAME_LENGTH (USBTFTP_FRAME_LEN)"),
    ("req->bufdma = (uint8_t *)malloc(512);",
     "req->bufdma = (uint8_t *)malloc(USBTFTP_FRAME_LEN + 64);"),
    # usb_stop() belongs to the host stack, which is not built here.
    ("\textern int usb_stop(void);\n", ""),
    ("\tusb_stop();\n", "\t/* host-stack call dropped; the PHY is already quiesced */\n"),
])

# HiSilicon's set_default_env() ends with an unconditional saveenv(), so this
# U-Boot writes flash just by starting when it cannot read an environment.
edit("common/env_common.c", [(
    "\tgd->flags |= GD_FLG_ENV_DEFAULT;\n\tsaveenv();\n}",
    "\tgd->flags |= GD_FLG_ENV_DEFAULT;\n"
    "\t/* No saveenv(): this U-Boot runs from RAM as a flashing agent, and\n"
    "\t * writing a default environment would destroy a block of the image\n"
    "\t * the host is about to read or has just written. */\n}",
)])
print("    patches applied")
PYEOF

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
