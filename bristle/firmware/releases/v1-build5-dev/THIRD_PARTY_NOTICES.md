# Third-party notices: Bristle node firmware

This file covers the node images in `releases/<release>/<board>/` (`bristle-<board>-FULL.bin` and
`bristle-<board>-app.bin`). Each image is the Bristle node sketch (`bristle_node/`) linked together
with the software listed below. The licence texts named in the last column are in `licenses/`,
copied byte for byte from the place named in "How each licence was checked".

The node sketch itself is MIT, Copyright (c) 2026 Hardscrubbin (the `LICENSE` file of the Bristle
Nodes folder).

## What the images are built from

| | Version |
|---|---|
| Arduino core for ESP32 (`esp32:esp32`) | 3.3.10 |
| ESP-IDF (inside the core's prebuilt `esp32c5-libs`) | v5.5.4 |
| Mbed TLS (inside ESP-IDF) | 3.6.5 |
| NimBLE-Arduino | 2.5.1 |
| Compiler and C library | Espressif `riscv32-esp-elf` GCC 14.2.0 with newlib |

The list below was read from the linker map of the `xiao_c5` image of build 2: every library archive
the linker took code from. Build 3 is the same source and libraries. Build 4 is built with the same core and libraries and links more of them (ESP-NOW, `libespnow.a`, which is part of Espressif's Wi-Fi libraries in ESP-IDF; the update functions of `app_update`; the server side of NimBLE): the map file of its XIAO image lists 68 archives, all from the components in the table. Build 5 is build 4 with a few changed lines in one of Bristle's own files, built with the same core, libraries and settings. The other two targets are built from the same sketch, core and libraries
with a different board setting.

## Components in the image

| Component | What it is | Licence | Copyright line in its licence file | Text |
|---|---|---|---|---|
| Arduino core for ESP32, with its Network, WiFi, FS, LittleFS and Preferences libraries | the Arduino layer | LGPL-2.1 | none of its own (the file is the licence text) | `arduino-esp32-LICENSE.md` |
| ESP-IDF | Espressif's SDK: drivers, system, storage, networking glue | Apache-2.0 | "All original source code in this repository is Copyright (C) 2015-2023 Espressif Systems" (`COPYRIGHT.rst`) | `Apache-2.0.txt`, `esp-idf-COPYRIGHT.rst` |
| Espressif Wi-Fi, PHY, coexistence and Bluetooth controller libraries | radio code, shipped by Espressif as binaries | Apache-2.0 | none in the file | `Apache-2.0.txt` |
| NimBLE-Arduino | Bluetooth LE library | Apache-2.0 | "Copyright 2020-2026 Ryan Powell and esp-nimble-cpp, NimBLE-Arduino contributors" (NOTICE, in full below) | `NimBLE-Arduino-LICENSE.txt`, `NimBLE-Arduino-NOTICE.txt` |
| Apache Mynewt NimBLE (inside NimBLE-Arduino) | Bluetooth LE host | Apache-2.0, with parts under 3-clause BSD and one unrestricted CRC32 table (see its licence file) | "Copyright 2015-2022 The Apache Software Foundation", "Modifications Copyright 2017-2022 Espressif Systems (Shanghai) CO., LTD." | `nimble-LICENSE.txt`, `nimble-NOTICE.txt` |
| TinyCrypt (inside NimBLE) | small cryptography library | 3-clause BSD | "Copyright (c) 2017, Intel Corporation. All rights reserved." | `tinycrypt-LICENSE.txt` |
| Mbed TLS | cryptography and TLS (Bristle uses its SHA-256 and HMAC) | Apache-2.0 OR GPL-2.0-or-later. **Taken here under Apache-2.0.** | none in the file | `mbedtls-LICENSE.txt` |
| FreeRTOS kernel | the real-time kernel | MIT | none in the file; ESP-IDF's `COPYRIGHT.rst` names Amazon.com, Inc. | `FreeRTOS-Kernel-LICENSE.md` |
| lwIP (Espressif's copy) | TCP/IP stack | 3-clause BSD | "Copyright (c) 2001, 2002 Swedish Institute of Computer Science." | `lwip-COPYING.txt` |
| wpa_supplicant | Wi-Fi security | BSD | "Copyright (c) 2002-2022, Jouni Malinen and contributors" | `wpa_supplicant-COPYING.txt` |
| cJSON | JSON parser | MIT | "Copyright (c) 2009-2017 Dave Gamble and cJSON contributors" | `cJSON-LICENSE.txt` |
| protobuf-c | Protocol Buffers runtime | 2-clause BSD | "Copyright (c) 2008-2025, Dave Benson and the protobuf-c authors." | `protobuf-c-LICENSE.txt` |
| esp_littlefs 1.22.1 | LittleFS port for ESP-IDF (the find queue's file system) | MIT | "Copyright 2020 Brian Pugh" | `esp_littlefs-LICENSE.txt` |
| littlefs (inside esp_littlefs) | the file system itself | 3-clause BSD | "Copyright (c) 2022, The littlefs authors." "Copyright (c) 2017, Arm Limited. All rights reserved." | `littlefs-LICENSE.md` |
| network_provisioning 1.0.2 | Espressif component the core links in | Apache-2.0 | see the file | `network_provisioning-LICENSE.txt` |
| esp_diagnostics 1.2.1 | Espressif component the core links in | Apache-2.0 | none in the file | `esp-insights-LICENSE.txt` |
| TLSF allocator | heap allocator inside ESP-IDF | 3-clause BSD | "Copyright (C) 2006-2016 Matthew Conte" | (no licence file found, see below) |
| newlib | the C library | a collection of BSD-style and similar permissive licences | many; all are in the file | `newlib-COPYING.NEWLIB.txt` |
| libgcc and libstdc++ | compiler runtime and C++ library | GPL-3.0-or-later with the GCC Runtime Library Exception 3.1 | "Copyright (C) 2009 Free Software Foundation, Inc." (the exception) | `gcc-COPYING3.txt`, `gcc-COPYING.RUNTIME.txt` |

ESP-IDF itself contains more third-party code than the rows above (for example FreeBSD net80211
inside the Wi-Fi library, and Fast PBKDF2 inside wpa_supplicant). Espressif's own list, with the
licence of each, is `licenses/esp-idf-COPYRIGHT.rst`.

### NimBLE-Arduino NOTICE

```
esp-nimble-cpp
NimBLE-Arduino
Copyright 2020-2026 Ryan Powell <ryan@nable-embedded.io> and
esp-nimble-cpp, NimBLE-Arduino contributors.

The Initial Developer of some parts of this library, which are copied from,
derived from, or inspired by is, esp32-snippets, Copyright 2017 Neil Kolban.

If this library is used for commercial purposes, it is requested that the user consider
making a donation and/or sponsoring this project to support it's ongoing development.
```

### Apache Mynewt NimBLE NOTICE

```
Apache Mynewt NimBLE
Copyright 2015-2022 The Apache Software Foundation
Modifications Copyright 2017-2022 Espressif Systems (Shanghai) CO., LTD.

This product includes software developed at
The Apache Software Foundation (http://www.apache.org/).

Portions of this software were developed at
Runtime Inc, copyright 2015.
```

## Rebuilding the image (LGPL-2.1)

The Arduino core is under the LGPL, and the image links it statically. The complete source of the
node sketch is in `bristle_node/`, and `tools/build_firmware.ps1` / `tools/build_firmware.sh` rebuild
every image from it. That lets anyone change the core and link the firmware again. The core's own
source is at <https://github.com/espressif/arduino-esp32> (tag `3.3.10`).

## How each licence was checked

Checked on 2026-11-22.

| Text in `licenses/` | Copied from |
|---|---|
| `arduino-esp32-LICENSE.md` | `LICENSE.md` of `espressif/arduino-esp32` at tag `3.3.10` (the installed core package has no licence file of its own) |
| `Apache-2.0.txt` | `LICENSE` of `espressif/esp-idf` at tag `v5.5.4`. The `LICENSE` files of `espressif/esp32-wifi-lib`, `esp-phy-lib`, `esp-coex-lib` and `esp32c5-bt-lib` are the same file, byte for byte |
| `esp-idf-COPYRIGHT.rst` | `docs/en/COPYRIGHT.rst` of `espressif/esp-idf` at tag `v5.5.4` |
| `FreeRTOS-Kernel-LICENSE.md`, `wpa_supplicant-COPYING.txt` | the files of those names under `components/` of `espressif/esp-idf` at tag `v5.5.4` |
| `lwip-COPYING.txt` | `COPYING` of `espressif/esp-lwip`, branch `2.1.3-esp` |
| `mbedtls-LICENSE.txt` | `LICENSE` of `Mbed-TLS/mbedtls` at tag `v3.6.5` |
| `NimBLE-Arduino-LICENSE.txt`, `NimBLE-Arduino-NOTICE.txt`, `nimble-LICENSE.txt`, `nimble-NOTICE.txt`, `tinycrypt-LICENSE.txt` | the installed NimBLE-Arduino 2.5.1 library the images were built with (`LICENSE`, `NOTICE`, `src/nimble/LICENSE`, `src/nimble/NOTICE`, `src/nimble/ext/tinycrypt/LICENSE`) |
| `esp_littlefs-LICENSE.txt` | `LICENSE` of `joltwallet/esp_littlefs` at tag `v1.22.1` |
| `newlib-COPYING.NEWLIB.txt`, `gcc-COPYING3.txt`, `gcc-COPYING.RUNTIME.txt` | `share/licenses/` of the installed compiler package the images were built with |
| `cJSON-LICENSE.txt`, `protobuf-c-LICENSE.txt`, `littlefs-LICENSE.md`, `network_provisioning-LICENSE.txt`, `esp-insights-LICENSE.txt` | the `LICENSE` file on the main branch of `DaveGamble/cJSON`, `protobuf-c/protobuf-c`, `littlefs-project/littlefs`, `espressif/idf-extra-components` (`network_provisioning/`) and `espressif/esp-insights` |

## What was not checked against a licence file

- **The exact revision** of cJSON, protobuf-c, littlefs, network_provisioning, esp_diagnostics and
  the four Espressif radio libraries. Their licence files were read on the main branch, not at the
  revision ESP-IDF v5.5.4 builds from.
- **TLSF.** Its repository has no licence file. The licence and copyright line above are from the
  header of `tlsf.c` in `espressif/tlsf` (`SPDX-License-Identifier: BSD-3-Clause`) and from
  `esp-idf-COPYRIGHT.rst`.
- **wpa_supplicant's licence text.** Its `COPYING` names the BSD licence and points to a README for
  the terms. That README was not read.
- **Code inside ESP-IDF's prebuilt libraries** beyond the rows above. For those this file relies on
  Espressif's `COPYRIGHT.rst`.
- **The component list for `devkit_c5` and `devkit_c5_uart`.** It was read from the `xiao_c5` link
  map only.
