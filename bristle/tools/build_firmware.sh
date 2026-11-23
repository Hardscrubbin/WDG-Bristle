#!/usr/bin/env bash
# Build every Bristle node target and write a release folder (Linux/macOS twin of build_firmware.ps1).
#   tools/build_firmware.sh [release-name]
# Needs arduino-cli with esp32:esp32@3.3.10 and NimBLE-Arduino 2.5.1, and Python with PyYAML.
# Regenerates the protocol files first and fails if a test vector doesn't match. Every target is built
# with partitions_bristle_8mb.csv and the build fails if the app does not fit a 2.5 MB slot.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLI="${ARDUINO_CLI:-arduino-cli}"
BUILD=$(grep -E '^#define BRISTLE_BUILD' "$ROOT/firmware/bristle_node/Boards.h" | awk '{print $3}')
NAME="${1:-v1-build${BUILD}-dev}"
OUT="$ROOT/firmware/releases/$NAME"
TMP="$(mktemp -d)"
SLOT_BYTES=2621440   # app0/app1 in partitions_bristle_8mb.csv

python3 -I "$ROOT/tools/gen_proto.py"

# The Arduino build uses a file called partitions.csv next to the sketch: build from a copy that has it.
STAGE="$TMP/bristle_node"
mkdir -p "$STAGE"
cp "$ROOT"/firmware/bristle_node/*.ino "$ROOT"/firmware/bristle_node/*.h "$ROOT"/firmware/bristle_node/*.cpp "$STAGE/"
cp "$ROOT/firmware/bristle_node/partitions_bristle_8mb.csv" "$STAGE/partitions.csv"

# No path of this machine goes into the images (assert and log messages carry __FILE__): every
# compile rewrites the folders. GCC uses the LAST option that matches, so the general rule is first.
maps() {  # build dir
  local out="" pair
  for pair in "$HOME=/user" "${TOOLCHAIN_ROOT:-$HOME/.arduino15}=/toolchain" "$TMP=/build" "$1/sketch=/src"; do
    case "$pair" in *" "*) echo "cannot rewrite a path with a space in it: $pair" >&2; exit 1;; esac
    out="$out -ffile-prefix-map=$pair -fmacro-prefix-map=$pair"
  done
  echo "$out"
}

build() {  # fqbn  folder
  local flags; flags="$(maps "$TMP/build-$2")"
  "$CLI" compile --clean --warnings all -b "$1" --build-property "upload.maximum_size=$SLOT_BYTES" \
    --build-property "compiler.c.extra_flags=$flags" --build-property "compiler.cpp.extra_flags=$flags" \
    --build-property "compiler.S.extra_flags=$flags" \
    --build-path "$TMP/build-$2" --output-dir "$TMP/out-$2" "$STAGE"
  if grep -a -q -F -e "$HOME" "$TMP/out-$2/bristle_node.ino.merged.bin" "$TMP/out-$2/bristle_node.ino.bin"; then
    echo "a path of this machine is inside the $2 image (the path rewriting did not cover it)" >&2; exit 1
  fi
  mkdir -p "$OUT/$2"
  cp "$TMP/out-$2/bristle_node.ino.merged.bin" "$OUT/$2/bristle-$2-FULL.bin"   # flash at 0x0
  cp "$TMP/out-$2/bristle_node.ino.bin"        "$OUT/$2/bristle-$2-app.bin"    # app only, 0x10000
}
# CPUFreq=80: the same clock the Piglet builds use; plenty for scanning, and it runs cooler.
build "esp32:esp32:XIAO_ESP32C5:CDCOnBoot=cdc,CPUFreq=80,FlashSize=8M" xiao_c5
build "esp32:esp32:esp32c5:CDCOnBoot=cdc,CPUFreq=80,FlashSize=8M"      devkit_c5
# Boards whose only USB port is a CH343/UART bridge (no native USB): CDCOnBoot=default.
build "esp32:esp32:esp32c5:CDCOnBoot=default,CPUFreq=80,FlashSize=8M"  devkit_c5_uart

(cd "$OUT" && sha256sum */*.bin > SHA256SUMS)
# The notices travel with the images: what is compiled into them, and the licence texts.
cp "$ROOT/firmware/THIRD_PARTY_NOTICES.md" "$OUT/"
rm -rf "$OUT/licenses" && cp -r "$ROOT/firmware/licenses" "$OUT/licenses"
rm -rf "$TMP"
echo "release written to $OUT"
