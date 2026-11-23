# Bristle node v1, build 4 (dev)

Built 2026-11-22 on the Windows PC with `tools\build_firmware.ps1`: arduino-cli 1.2.2, ESP32 core
3.3.10, NimBLE-Arduino 2.5.1, CPU clock 80 MHz.
**Not yet run on hardware.** No build of this firmware has run on a board. The bench list is
`docs/BENCH_TESTS.md`; start at checkpoint A.

| Folder | Board | Build options | App size |
|---|---|---|---|
| `xiao_c5` | Seeed XIAO ESP32-C5 | XIAO_ESP32C5, CDCOnBoot=cdc, CPUFreq=80, 8 MB | 1,490,240 B (57% of a slot) |
| `devkit_c5` | ESP32-C5 boards with native USB | esp32c5, CDCOnBoot=cdc, CPUFreq=80, 8 MB | 1,506,768 B (57%) |
| `devkit_c5_uart` | ESP32-C5 boards that only have a CH343/UART USB port | esp32c5, CDCOnBoot=default, CPUFreq=80, 8 MB | 1,512,320 B (58%) |

Flash layout (all, the same as builds 2 and 3): NVS, otadata, two **2.5 MB** OTA app slots, a
**2.875 MB** LittleFS find queue, coredump (`firmware/bristle_node/partitions_bristle_8mb.csv`).

- `bristle-<board>-FULL.bin`: flash at **0x0** on a first install, **with erase**. Works in the Seeed
  XIAO web flasher and esptool. This is the only image that puts the flash layout on the board.
- `bristle-<board>-app.bin`: app only. It is what an update over the air sends, and what goes at
  **0x10000** for an update by cable (clear otadata first: `docs/FLASHING.md`).

Builds 1, 2 and 3 are superseded: don't flash or publish them.

## New in build 4

- **Roles.** One sketch, two roles, set by the core over USB and kept in NVS: node (scans) and
  bridge (relays ESP-NOW frames between the core and the nodes, scans nothing).
- **ESP-NOW link.** Signed frames (HMAC-SHA256 with the owner key), a counter that survives
  restarts on both ends with a sync frame to pick up after one, fragments for long lines, a visit to
  the control channel every 1.5 s between two channel dwells, poll frames, a search for a lost
  bridge that backs off when there is none. Bridge: a mailbox per node, a peer table that makes room
  by dropping the peer unused longest.
- **Bluetooth LE link.** GATT server with Bristle's own UUIDs; the hwid and an "owned" flag in the
  advertisement; the radio is left to the link for a short slot at regular intervals while a core
  is connected.
- **Updates over the air** on every link: begin, chunk, verify (SHA-256 of the slot read back from
  flash), commit, abort, status. The new image runs on trial and the node goes back to the old one
  if it does not pass its self-test and meet its owner within 5 minutes.
- **One core at a time.** A hello over the air from a second core, while another core holds a live
  link, is answered "busy".
- **A beacon on USB.** A board no core has spoken to prints a `BCN` line once a second, so a
  program can recognise a Bristle board by listening, without writing to the port.
- **The plan says how Bluetooth is scanned:** with scan responses (the core's default) or
  receiving only. A node never connects to or pairs with what it hears.
- **More in a Bluetooth find:** the first byte of the manufacturer data, up to three 16-bit service
  UUIDs, and for another Bristle board its hwid and whether it has an owner.
- **Hunt mode's watch:** on the core's request a node listens for Bluetooth LE only and reports
  every advertisement of one address as it hears it. It sends nothing to that device.
- The board name of the UART build is `devkit_c5_uart` (it was `devkit_c5`), so an update takes the
  image for the right USB port.
- Console: `links`, `update`; `status` shows the role; `queue` shows how often flash was written;
  `selftest` has 15 checks.

## Fixed in build 4

- Bluetooth scan results were handed to the find queue on the Bluetooth stack's own task while
  loop() used the same queue: two tasks in one data structure. Results now cross to loop() through
  a queue and everything else happens there. (Found by reading the code; build 3 never ran.)
- The find stream skipped a find when the link had no room for it after all, and only sent it
  again after the 10 s resend pause. It now keeps its place and sends that find next time.
- The small state file (ack point, drop counters) was written with every save of the queue. It is
  now written at most once a minute while only the ack point moves, and at once when a drop is
  counted.

## Carried over from builds 2 and 3

USB link with the owner lock and USB-only adoption; Wi-Fi 2.4 + 5 GHz scanning on the channel
plan, Bluetooth bursts between sweeps, repeat suppression; the RAM + flash find queue with exact
backlog, drop counters that survive a restart, finds stored before the clock is set; link timeout;
BOOT 5 s to free a node; LED identify; the flash-layout check; images without the build machine's
folder names; `THIRD_PARTY_NOTICES.md` and `licenses/` next to the images.

## Not in this build

Wireless adoption (a node is adopted over USB only). Coordinated visit times for very large fleets
(nodes pick their own; see `docs/PROTOCOL.md`, "40 nodes").

## Checked on the PC

- All three targets compile with `--warnings all`; no warnings in Bristle's own files.
- Each app image: `esptool image-info` reports ESP32-C5, checksum valid, validation hash valid.
- Each FULL image is 8,388,608 bytes; the partition table read back from offset 0x8000 matches
  `partitions_bristle_8mb.csv` entry for entry; the app inside it (offset 0x10000) is byte-identical
  to the separate app image.
- `SHA256SUMS` matches the files.
- No image contains the build machine's user folder (the build script's own check).
- The spec's 10 test vectors and the core library's 146 unit tests pass. They cover the Python
  side of the frame counters, fragments, the bridge hub, the update sender and the rest; the C++
  side of the same logic is only compiled, and checked on a board by `selftest` (15 checks).

Nothing here says the firmware behaves on a board. Only the bench list does.
