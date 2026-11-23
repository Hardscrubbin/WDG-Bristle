# Bristle board notes

8 MB flash minimum, always. Every board uses the same layout,
`firmware/bristle_node/partitions_bristle_8mb.csv`:

| Partition | Offset | Size | Use |
|---|---|---|---|
| nvs | 0x9000 | 20 KB | owner key, node number, role, plan, run state, control channel, sequence and frame-counter reservations, boot number |
| otadata | 0xE000 | 8 KB | which app slot boots |
| app0 | 0x10000 | 2.5 MB (0x280000) | firmware |
| app1 | 0x290000 | 2.5 MB (0x280000) | second slot for over-the-air updates |
| spiffs (LittleFS) | 0x510000 | 2.875 MB (0x2E0000) | the find queue |
| coredump | 0x7F0000 | 64 KB | crash dumps |

Build 5 is about 1.49 MB, 57% of a slot.

## The find queue: how much it holds, how often it writes

Worked out from the layout and the code, not measured yet (checkpoint C measures it; the node's
`queue` console command prints the counts).

| Thing | Number | Why |
|---|---|---|
| Space | 2.875 MB, 718 blocks of 4 KB; 48 KB is always kept free | `partitions_bristle_8mb.csv`, `queue.min_free_bytes` |
| One stored find | 18 bytes of header + the find (40 to 60 bytes): about 70 bytes | `Queue.h` |
| Holds | about 38,000 finds | 2.7 MB / 70 |
| Lasts, away from the core | about 7 hours in a dense city (90 finds a minute); about a day parked (25 a minute) | with the default repeat times (5 min per access point, 30 s per Bluetooth device) |
| When it is full | the oldest finds go, and are counted (`dropped`, `qd` in the heartbeat) | |
| Saved to flash | when 4 KB is waiting in RAM (about 55 finds), and at least every **15 s** | `queue.ram_flush_bytes`, `timing.queue_save_ms` |
| Lost by a power cut | at most the last 15 s of finds that were not delivered yet | |
| Cost of one save | about one 4 KB block erased and rewritten (the file system rewrites the last block of the file it adds to) | LittleFS |
| Saves per hour of scanning | at most 240 | |
| Ack point and drop counters (the small state file) | written at most once a minute while only the ack point moves; at once when a drop is counted | `timing.queue_state_save_ms` (new in build 4: it was written with every save) |
| Wear | 240 block erases an hour over about 700 blocks: each block about once every 3 hours | |
| Until a block reaches 100,000 erases | about 30 years of continuous scanning with the queue mostly empty; a few years if the queue is kept nearly full the whole time | typical rating of this kind of flash |

**Why 15 s.** Saving every 5 s would triple the writes to protect 10 more seconds of finds; every
60 s would quarter them and put a minute of finds at risk. Wear is far from being the limit either
way, so the interval stays where a power cut costs little. A node linked to the core hands most
finds over before they are 15 s old, and those are then never written to flash at all.

The layout only gets onto a board with the **FULL image at 0x0**. The firmware checks it at boot:
`status` says `flash layout ok`, or `flash layout WRONG` if the board was flashed some other way.

All targets are built for an 80 MHz CPU clock (the same as the Piglet builds).

| Board | Build folder | USB | LED | BOOT pin |
|---|---|---|---|---|
| Seeed XIAO ESP32-C5 (8 MB + PSRAM) | `xiao_c5` | Native USB (USB-JTAG/serial, 303a:1001) | GPIO27 (`LED_BUILTIN`); polarity to verify | 28, to verify |
| Espressif ESP32-C5-DevKitC-1 (8 MB) | `devkit_c5` (its native USB port) or `devkit_c5_uart` (its UART port) | Native USB port and/or a UART bridge | RGB LED on GPIO27 | 28, to verify |
| Waveshare ESP32-C5-WiFi6-KIT | `devkit_c5_uart` (UART port) | CH343 (1a86:55d3); needs a driver on Windows/Mac | RGB, to verify | 28, to verify |
| Other ESP32-C5 boards with 8 MB+ | `devkit_c5` / `devkit_c5_uart` | varies | `RGB_BUILTIN` assumed | 28, to verify |

"To verify" items are checked at checkpoint A: console `identify 5` (LED) and `boot` (BOOT pin).
Fix `firmware/bristle_node/Boards.h` if a board differs.

A board reports its build folder as its board name (`xiao_c5`, `devkit_c5`, `devkit_c5_uart`), and
an update over the air takes the image from the folder of that name. So a board keeps the kind of
USB port it was first flashed for.

**The bridge** is any of these boards in the bridge role; use one with native USB (`HARDWARE.md`).

## Which serial ports the core will speak to

A serial port is only ever written to when all of this holds:

- its USB chip is a known ESP32 interface (Espressif native USB, CH343, CH340, CP210x, or one added
  in settings), so the GPS dongle is never a candidate;
- it is not on the exclude list and not marked "not a node";
- no other program has it open;
- it belongs to a node in Node Memory, or you asked for it.

A new ESP32 port is listed and left alone until you say so, with one exception: a board that
announces itself as a Bristle board (it prints a beacon line once a second while no core has spoken
to it) is said hello to without asking. The core finds that out by listening; nothing is written
to a port to see what is on it. Adopting is still your key press. A board with native USB reports its
MAC as the USB serial number, which is the node's hardware id, so your own nodes are recognised on
any port. A CH343 board is recognised by the CH343's own serial number, remembered at adoption. A
CH340 has no serial number and can only be told apart by the port it is on.
