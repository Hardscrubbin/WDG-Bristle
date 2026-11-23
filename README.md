# WDG-Bristle

WDG-Bristle is a build of **Watch Dogs Go** for the ClockworkPi uConsole: a wardriving game and
field tool with a retro 640x360 interface. It adds Kismet wardriving, two GPS receivers through
gpsd, ADS-B, and **Bristle Nodes**, a fleet of small ESP32-C5 scanning boards that report to the
uConsole.

**Built on [Watch Dogs Go by LOCOSP](https://github.com/LOCOSP/WatchDogsGo).** The game, its
artwork and most of what you see are LOCOSP's work, under LOCOSP's [LICENSE](LICENSE) (MIT). This
repository is a fork of it. Other credits are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

> **Status.** WDG-Bristle 1.1. Early. The Bristle Nodes firmware and everything it adds to the game
> have been compiled and unit-tested, and have **not yet run on real boards**: the bench list in
> [`bristle/docs/BENCH_TESTS.md`](bristle/docs/BENCH_TESTS.md) is still to be worked through. Expect
> rough edges, and do not rely on them yet.

## What it does

The full list, with what's new in WDG-Bristle and where everything comes from, is in
**[FEATURES.md](FEATURES.md)**.

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
- **Bristle Nodes.** ESP32-C5 boards scan Wi-Fi (2.4 and 5 GHz) and Bluetooth on the channels the
  uConsole gives them. Each board is adopted by hand, locked to its owner with a key, and keeps its
  finds in flash until the uConsole confirms them. The uConsole places every find with its own GPS.
  Three links, one protocol: USB on a powered hub, ESP-NOW through one board acting as a bridge
  (signed frames; the way to tens of nodes), and Bluetooth LE. Firmware updates go over any of the
  three and roll back by themselves if the new image does not come up. Protocol, firmware and tools
  are in [`bristle/`](bristle/).
- **Nearby alerts.** A popup, a sound and a log line when the radios hear a device from a
  user-editable signatures file (trackers, some makers' devices, other rigs), or one that keeps
  turning up at places far apart. Kismet's own alerts are shown the same way. Every value in the
  signatures file names its published source: [`bristle/docs/SIGNATURES.md`](bristle/docs/SIGNATURES.md).
- **Possible skimmers, listening only.** Bluetooth serial modules that look factory-fresh (the
  signs found on card skimmers pulled out of gas pumps) are scored, logged with a position, and can
  be walked up to with a signal meter. Such a device is never connected to, paired with or asked
  for anything. It finds some
  skimmers and misses others (a renamed module, a cellular one, one with no radio): a hit is a
  possible skimmer, and no hit never means a pump is safe.

## Kismet only?

You can use WDG-Bristle just for Kismet wardriving. Bristle Nodes, alerts and the rest are optional.

1. Plug in your Wi-Fi card(s) (and a Bluetooth adapter if you want Bluetooth in Kismet), then start
   the game.
2. **TAB > PLUGINS > Kismet: Configure & Start** opens the adapter picker:
   - UP/DOWN to move, SPACE/ENTER to tick a card
   - M for monitor mode
   - S to turn the channel split on or off (two cards: one takes 2.4 GHz, the other 5/6 GHz)
   - R to rescan, ESC to cancel
3. Networks and Bluetooth devices go into your loot (WiGLE-format CSV), XP and map, placed with the
   uConsole's GPS.

### K is the Kismet kill switch

Press **K** anywhere in the game while Kismet is running (on the map, the loot screen and so on):

- **Stops the Kismet run** and saves everything captured so far. Networks still waiting for a fix
  are saved at the last good GPS position from that run.
- **Turns monitor mode off.** Every Wi-Fi card the game put into monitor mode goes back to managed
  mode and is handed back to NetworkManager, so normal Wi-Fi works again without a reboot. Cards
  that were already in monitor mode before you started are left as they were.
- **Stops Bristle Nodes too** if they're set to follow Kismet (`tie_to_kismet = on` in
  `bristle.conf`, the default).
- Does nothing when Kismet isn't running. It's ignored while you're typing in a text box, in the
  TAB menu or in another plugin's screen, so a stray "k" can't end a run.

You can also stop from **TAB > PLUGINS > Kismet: Stop**.

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

1. Plug a board into a powered USB hub on the uConsole. In the game: **PLUGINS > Bristle: Nodes**.
   A blank board can be flashed from that screen (**W**); or flash it from a PC first:
   [`bristle/docs/FLASHING.md`](bristle/docs/FLASHING.md). Images are in `bristle/firmware/releases/`.
2. A Bristle board announces itself and is listed. Any other serial device is left alone: nothing
   is written to a port to find out what is on it.
3. Press **A** to adopt it, then **S** to scan.

The uConsole side is described in [`bristle/docs/INSTALL.md`](bristle/docs/INSTALL.md), hardware
and power in [`bristle/docs/HARDWARE.md`](bristle/docs/HARDWARE.md), the protocol in
[`bristle/docs/PROTOCOL.md`](bristle/docs/PROTOCOL.md). The bench steps the firmware is to be
tested against are in [`bristle/docs/BENCH_TESTS.md`](bristle/docs/BENCH_TESTS.md). What it would
take to move Bristle to another game base: [docs/PORTING_NG.md](docs/PORTING_NG.md).

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
- **`plugins/_ng/`** holds LOCOSP's WDGoNG files, unchanged, under the PolyForm Noncommercial
  licence: the notice and the licence text are in that folder
  ([NOTICE](plugins/_ng/NOTICE), [LICENSE-WDGoNG.txt](plugins/_ng/LICENSE-WDGoNG.txt)). The MIT
  licences above do not cover those files.
- **Third-party parts** (fonts, map data, installed packages, and what is compiled into the node
  firmware images) keep their own licences. They are listed in
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and
  [bristle/firmware/THIRD_PARTY_NOTICES.md](bristle/firmware/THIRD_PARTY_NOTICES.md).
