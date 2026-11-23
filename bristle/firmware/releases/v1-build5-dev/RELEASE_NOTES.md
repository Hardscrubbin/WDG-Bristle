# Bristle node v1, build 5 (dev)

Built 2026-11-23 on the Windows PC with `tools\build_firmware.ps1`: arduino-cli 1.2.2, ESP32 core
3.3.10, NimBLE-Arduino 2.5.1, CPU clock 80 MHz. Every target compiled with no warning in Bristle's
own files.
**Not yet run on hardware.** No build of this firmware has run on a board. The bench list is
`docs/BENCH_TESTS.md`; start at checkpoint A.

| Folder | Board | Build options | App size |
|---|---|---|---|
| `xiao_c5` | Seeed XIAO ESP32-C5 | XIAO_ESP32C5, CDCOnBoot=cdc, CPUFreq=80, 8 MB | 1,490,256 B (57% of a slot) |
| `devkit_c5` | ESP32-C5 boards with native USB | esp32c5, CDCOnBoot=cdc, CPUFreq=80, 8 MB | 1,506,784 B (57%) |
| `devkit_c5_uart` | ESP32-C5 boards that only have a CH343/UART USB port | esp32c5, CDCOnBoot=default, CPUFreq=80, 8 MB | 1,512,352 B (58%) |

Flash layout (all, the same as builds 2 to 4): NVS, otadata, two **2.5 MB** OTA app slots, a
**2.875 MB** LittleFS find queue, coredump (`firmware/bristle_node/partitions_bristle_8mb.csv`).

- `bristle-<board>-FULL.bin`: flash at **0x0** on a first install, **with erase**. Works in the Seeed
  XIAO web flasher and esptool. This is the only image that puts the flash layout on the board.
- `bristle-<board>-app.bin`: app only. It is what an update over the air sends, and what goes at
  **0x10000** for an update by cable (clear otadata first: `docs/FLASHING.md`).

Builds 1 to 4 are superseded: don't flash or publish them. Build 5 is build 4 with the one fix
below; the protocol, the flash layout and everything a core sees are the same, so a core that
worked with build 4 works with build 5 and the other way round.

## Fixed in build 5

- **A replayed ESP-NOW frame no longer moves the node's bridge address.** A node sends its frames
  to the bridge it last heard. Up to build 4 it also took the sender of an OLD frame for the bridge
  (a frame signed with the owner key whose counter had already been used) when it answered that
  frame with a sync. Anybody who had recorded one frame from the core could play it back from
  their own address, and the node then sent everything to that address until it gave the bridge
  up for lost, 20 s later. Nothing in the old frame was acted on and no find was lost (the queue
  holds them), but the node was cut off for that time, and playing the frame again every 20 s
  kept it cut off.
  Build 5 sends the sync answer to the sender of the old frame and to nobody else's benefit: the
  bridge address only changes when a frame passes the counter check. A core whose counter fell
  behind still catches up the same way as before.
  (`Link_EspNow.cpp`, `EspNowLink::handle` and `sendFrame`. Found on a PC by the stand-in
  simulations in the game's `tools/sim`, where a scripted node written from this source showed
  it, then confirmed by reading the source. It has not been seen on a board, and the fix has been
  compiled, not run.)
