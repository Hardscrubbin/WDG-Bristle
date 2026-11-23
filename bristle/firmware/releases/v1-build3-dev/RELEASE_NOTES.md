# Bristle node v1, build 3 (dev)

Built 2026-11-22 on the Windows PC with `tools\build_firmware.ps1`: arduino-cli 1.2.2, ESP32 core
3.3.10, NimBLE-Arduino 2.5.1, CPU clock 80 MHz.
**Not yet run on hardware.** First real test is checkpoint A (`docs/BENCH_TESTS.md`).

| Folder | Board | Build options | App size |
|---|---|---|---|
| `xiao_c5` | Seeed XIAO ESP32-C5 | XIAO_ESP32C5, CDCOnBoot=cdc, CPUFreq=80, 8 MB | 1,445,376 B (55% of a slot) |
| `devkit_c5` | ESP32-C5 boards with native USB | esp32c5, CDCOnBoot=cdc, CPUFreq=80, 8 MB | 1,461,920 B (56%) |
| `devkit_c5_uart` | ESP32-C5 boards that only have a CH343/UART USB port | esp32c5, CDCOnBoot=default, CPUFreq=80, 8 MB | 1,467,472 B (56%) |

Flash layout (all, the same as build 2): NVS, otadata, two **2.5 MB** OTA app slots, a **2.875 MB**
LittleFS find queue, coredump (`firmware/bristle_node/partitions_bristle_8mb.csv`).

- `bristle-<board>-FULL.bin`: flash at **0x0** on a first install, **with erase**. Works in the Seeed
  XIAO web flasher and esptool. This is the only image that puts the flash layout on the board.
- `bristle-<board>-app.bin`: app only, at **0x10000**, for later updates of a board that already runs
  build 2 or later (keeps settings, owner and queue).

Do not flash `v1-build1-dev`: it has the old layout, and its find stream could not work (see below).
`v1-build2-dev` is this firmware without the change below: use build 3.

## Changed since build 2

- No behaviour change: the source is build 2's, with the build number raised to 3.
- The images no longer carry folder names of the PC that built them. Assert and log messages in
  NimBLE and the Arduino core hold the source file's path; build 2 was compiled without rewriting
  it, so those strings named the builder's user folder. The build script now rewrites the
  toolchain, sketch and user folders at compile time (`-ffile-prefix-map`, `-fmacro-prefix-map`),
  builds from scratch every time, and fails if a path of the build machine is still in an image.
  What is left in the images are Espressif's own build paths (`/IDF/...`, `/builds/idf/...`).
- `THIRD_PARTY_NOTICES.md` and `licenses/` are in this folder, next to the images.

## Changed since build 1

- New flash layout (above). The node checks it at boot: `status` says `flash layout ok` or `WRONG`.
- Firmware split into the planned files: `Link`, `Link_Usb`, `Owner`, `Console`, `Node`, `Clock`,
  next to `Queue`, `Radio`, `Store`, `Codec`, `Crypto`.
- Queue: a find made before the clock is set is written to flash with the boot number and the
  uptime, and gets its time when the core sets the clock in that boot (build 1 held 400 in RAM).
- Queue: the backlog in the heartbeat is exact. Drop counters survive a reboot. `queue` on the
  console shows how many dropped finds had no clock.
- Fixed: after any reboot the sequence numbers have a gap, and build 1 reported the gap as its
  oldest find, so the core's ack could never pass it and delivery stalled for good.
- Fixed: build 1 waited for 260 free bytes in a 256-byte USB transmit buffer before sending a find,
  so no find would ever have been sent. The buffer is 4 KB now, and nothing on the node waits for a
  host that isn't reading.
- Fixed: a full channel plan did not fit one protocol line. The plan now has a short form (`d=`).
- An owned node takes its clock only from its authenticated owner.
- A node that hears nothing from the core for 20 s closes the link and keeps its finds.
- The clock survives a reset (not a power cut), if the chip keeps its time through one. To see at
  checkpoint A: `status` after pressing RESET.
- Console: `identify <s>` (and `ident`), `status` shows clock, flash layout and lines not sent.
  `selftest` has 10 checks.

## In this build

- USB link: handshake, owner lock (two-way HMAC proofs), USB-only adoption, NUM/PLAN/RUN/ACK/PING/
  IDENT/REPEAT/FORGET, heartbeat, time sync with TREQ.
- Scanning: Wi-Fi 2.4 + 5 GHz on the channel plan (per-channel dwell), BLE bursts between sweeps,
  repeat suppression (AP 5 min, BLE 30 s).
- Queue: RAM + LittleFS segments, saved every 15 s and on stop, deleted only when acked, resend
  after a reconnect or a stalled ack, oldest dropped (and counted) when flash runs low.
- Console (`help`): status, queue, owner, forget, identify, boot, selftest, version.

## Not in this build

ESP-NOW bridge and node link, Bluetooth LE link to the core, over-the-air updates. See `docs/PLAN.md`.

## Checked on the PC

- All three targets compile with `--warnings all`; no warnings in Bristle's own files.
- Each app image: `esptool image-info` reports ESP32-C5, checksum valid, validation hash valid.
- Each FULL image is 8,388,608 bytes; the partition table read back from offset 0x8000 matches
  `partitions_bristle_8mb.csv` entry for entry; the app inside it (offset 0x10000) is byte-identical
  to the separate app image.
- `SHA256SUMS` matches the files.
- No image contains the build machine's user folder (the build script's check and
  `tools/public_check.py` of the game build, which reads the strings of every binary).
- The spec's 8 test vectors and the core library's 52 unit tests pass.

Nothing here says the firmware behaves on a board. Only checkpoint A does.
