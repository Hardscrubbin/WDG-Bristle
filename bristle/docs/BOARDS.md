# Bristle board notes

8 MB flash minimum, always. Every board uses the same layout,
`firmware/bristle_node/partitions_bristle_8mb.csv`:

| Partition | Offset | Size | Use |
|---|---|---|---|
| nvs | 0x9000 | 20 KB | owner key, node number, plan, run state, sequence reservation, boot number |
| otadata | 0xE000 | 8 KB | which app slot boots |
| app0 | 0x10000 | 2.5 MB (0x280000) | firmware |
| app1 | 0x290000 | 2.5 MB (0x280000) | second slot for over-the-air updates |
| spiffs (LittleFS) | 0x510000 | 2.875 MB (0x2E0000) | the find queue |
| coredump | 0x7F0000 | 64 KB | crash dumps |

Build 3 is about 1.45 MB, 55% of a slot. A stored find takes about 65 bytes, so the queue holds
roughly 40,000 finds (an estimate; checkpoint C measures it).

The layout only gets onto a board with the **FULL image at 0x0**. The firmware checks it at boot:
`status` says `flash layout ok`, or `flash layout WRONG` if the board was flashed some other way.

All targets are built for an 80 MHz CPU clock (the same as the Piglet builds).

| Board | Build folder | USB | LED | BOOT pin |
|---|---|---|---|---|
| Seeed XIAO ESP32-C5 (8 MB + PSRAM) | `xiao_c5` | Native USB (USB-JTAG/serial, 303a:1001) | GPIO27 (`LED_BUILTIN`); polarity to verify | 28, to verify |
| Espressif ESP32-C5-DevKitC-1 (8 MB) | `devkit_c5` or `devkit_c5_uart` | Native USB port and/or a UART bridge | RGB LED on GPIO27 | 28, to verify |
| Waveshare ESP32-C5-WiFi6-KIT | `devkit_c5_uart` (UART port) | CH343 (1a86:55d3); needs a driver on Windows/Mac | RGB, to verify | 28, to verify |
| Other ESP32-C5 boards with 8 MB+ | `devkit_c5` / `devkit_c5_uart` | varies | `RGB_BUILTIN` assumed | 28, to verify |

"To verify" items are checked at checkpoint A: console `identify 5` (LED) and `boot` (BOOT pin).
Fix `firmware/bristle_node/Boards.h` if a board differs.

## Which serial ports the core will speak to

A serial port is only ever written to when all of this holds:

- its USB chip is a known ESP32 interface (Espressif native USB, CH343, CH340, CP210x, or one added
  in settings), so the GPS dongle is never a candidate;
- it is not on the exclude list and not marked "not a node";
- no other program has it open;
- it belongs to a node in Node Memory, or you asked for it.

A new ESP32 port is listed and left alone until you say so. A board with native USB reports its
MAC as the USB serial number, which is the node's hardware id, so your own nodes are recognised on
any port. A CH343 board is recognised by the CH343's own serial number, remembered at adoption. A
CH340 has no serial number and can only be told apart by the port it is on.
