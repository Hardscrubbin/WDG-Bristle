# WDG-Bristle

WDG-Bristle is a build of **Watch Dogs Go** for the ClockworkPi uConsole: a wardriving game and
field tool with a retro 640x360 interface. It adds Kismet wardriving, two GPS receivers through
gpsd, ADS-B, and **Bristle Nodes**, a fleet of small ESP32-C5 scanning boards that report to the
uConsole.

**Built on [Watch Dogs Go by LOCOSP](https://github.com/LOCOSP/WatchDogsGo).** The game, its
artwork and most of what you see are LOCOSP's work, under LOCOSP's [LICENSE](LICENSE) (MIT). This
repository is a fork of it. Other credits are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

> **Status.** Early. The Bristle Nodes firmware and its integration into the game are new and are
> being bench-tested on real boards: expect rough edges, and do not rely on them yet.

## What it does

- **Kismet wardriving.** Start and stop Kismet from the game. Networks and Bluetooth devices go
  into the game's loot (WiGLE-format CSV), XP and map, placed with the uConsole's GPS. One or two
  Wi-Fi adapters, split by band. Uploads to WiGLE and WDGWars.
- **Dual GPS through gpsd.** An on-board receiver and a USB receiver at the same time, with a
  health screen that shows which one is in use and why.
- **ADS-B.** Aircraft tracks and coverage from an RTL-SDR, with KML export.
- **433 MHz.** Capture to Flipper `.sub` files and a signal-strength hunter.
- **Hardware Check.** One screen for the board, power, the add-on board's rails, GPS, SDR, Wi-Fi
  and Bluetooth adapters.
- **Loot backup and restore**, a session log, WDGWars pairing and status, an offline basemap layer.
- **Bristle Nodes.** ESP32-C5 boards on a powered USB hub scan Wi-Fi (2.4 and 5 GHz) and Bluetooth
  on the channels the uConsole gives them. Each board is adopted by hand, locked to its owner with
  a key, and keeps its finds in flash until the uConsole confirms them. The uConsole places every
  find with its own GPS. Protocol, firmware and tools are in [`bristle/`](bristle/).

## Hardware

| Part | Notes |
|---|---|
| ClockworkPi uConsole (CM4 or CM5) | Debian-based system; developed on DragonOS |
| GPS | Any receiver gpsd can read. Two are supported at once. |
| Wi-Fi adapter with monitor mode | For Kismet. Developed with an mt7921u adapter. |
| RTL-SDR | For ADS-B and 433 MHz |
| HackerGadgets AIO v2 add-on board (optional) | RTL-SDR, LoRa, GPS and RTC in the uConsole; the game switches its power rails |
| ESP32-C5 boards with 8 MB flash (optional) | Bristle Nodes: Seeed XIAO ESP32-C5 or a C5 dev board, on a **powered** USB hub |

## Install

On the uConsole, as your normal user:

```bash
git clone https://github.com/Hardscrubbin/WDG-Bristle.git ~/wdg-bristle
cd ~/wdg-bristle
./install.sh
./selftest.sh --tests-only
```

`install.sh` asks for `sudo` where it needs it and says what it changes: system packages, the
game's Python environment, gpsd's device list, your user's groups, and a desktop entry. Your data
(`loot/`, `maps/`, `secrets.conf`, `whitelist.json`) is never touched by a reinstall.

Then put your keys in `secrets.conf` (WiGLE, WDGWars, map tiles) and start the game from the
desktop icon. The full guide, with troubleshooting, is [INSTALL_WDG_BRISTLE.md](INSTALL_WDG_BRISTLE.md).

There is no in-game updater in this build: update with `git pull` and `./install.sh`.

### Bristle Nodes

1. Flash the boards: [`bristle/docs/FLASHING.md`](bristle/docs/FLASHING.md). Images are in
   `bristle/firmware/releases/`.
2. Plug them into a powered USB hub on the uConsole.
3. In the game: **PLUGINS > Bristle: Nodes**. A new board is listed and left alone until you press
   **A**. Adopt it, then press **S** to scan.

The protocol is described in [`bristle/docs/PROTOCOL.md`](bristle/docs/PROTOCOL.md); the bench
steps the firmware is being tested against are in [`bristle/docs/BENCH_TESTS.md`](bristle/docs/BENCH_TESTS.md).

## Not in this repository

This build does not ship firmware for the game's own ESP32 companion board. SYSTEM > Firmware
says "No bundled firmware in this build" and flashes nothing.

## Use it responsibly

This is a tool for surveying radio signals you are allowed to receive, and for testing equipment
you own. Radio and privacy laws differ from place to place: know yours.

## Tests

```bash
.venv/bin/python3 -B -m unittest discover -s tests
.venv/bin/python3 -B -m unittest discover -s bristle/tests
python3 -B tools/public_check.py
```

## Licence

- **LOCOSP's game and the upstream code** are under LOCOSP's MIT licence: [LICENSE](LICENSE).
- **The additions in this repository** (Bristle Nodes, the Kismet bridge and the WDG-Bristle
  changes) are MIT, Copyright (c) 2026 Hardscrubbin: [LICENSE-HARDSCRUBBIN](LICENSE-HARDSCRUBBIN).
  The same text is in [bristle/LICENSE](bristle/LICENSE).
- **Third-party parts** (fonts, map data, installed packages, and what is compiled into the node
  firmware images) keep their own licences. They are listed in
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and
  [bristle/firmware/THIRD_PARTY_NOTICES.md](bristle/firmware/THIRD_PARTY_NOTICES.md).
