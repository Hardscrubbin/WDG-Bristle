# WDG-Bristle features

What WDG-Bristle adds on top of [Watch Dogs Go](https://github.com/LOCOSP/WatchDogsGo), and where
each part comes from.

**Status labels**
- **1.0**: since the first release.
- **1.1**: new in 1.1, the current release.

Everything marked Bristle is new and still being bench-tested on real hardware, so expect rough
edges.

---

## Built for WDG-Bristle

### Bristle Nodes: an ESP32-C5 scanning fleet

Small ESP32-C5 boards that scan alongside the uConsole and report back to it. Run 2 or 3 in the
car, or 40+: the design scales.

| Feature | What it does | Status |
|---|---|---|
| **Wi-Fi + Bluetooth scanning** | Each node scans 2.4 GHz and 5 GHz Wi-Fi and Bluetooth LE on the channels the uConsole gives it. | 1.0 |
| **Automatic channel split** | The uConsole shares the channels out across nodes. 5 GHz only goes to boards that can do it, one node takes Bluetooth when there are two or more, and the split rebalances as nodes come and go. Pin any node to a band by hand. US and EU channel sets. | 1.0 |
| **Kismet-aware planning** | Channels Kismet already covers heavily get the nodes' shortest dwell, so the fleet spends its time where Kismet isn't. | 1.1 |
| **Adopt by hand, never automatically** | A new board is listed and left alone until you press Adopt. Other ESP32 gadgets on the same hub are never grabbed as nodes. | 1.0 |
| **Node Memory and Block list** | Adopted nodes keep their number through reboots, reflashes and reconnects. Blocked devices are ignored for good. | 1.0 |
| **Owner lock** | Each node is locked to your uConsole with a 16-byte key and an HMAC-SHA256 challenge on every connection. Someone else's rig can't take over or command your nodes. Free a node by holding BOOT for 5 seconds. | 1.0 |
| **Nothing lost, nothing doubled** | Finds are numbered, kept in flash on the node until the uConsole confirms them, and resent after a dropout or reboot. Duplicates are dropped. When flash fills, the oldest finds go first and are counted. | 1.0 |
| **Placed with the uConsole's GPS** | Nodes need no GPS. Each find is placed by its timestamp against the uConsole's GPS track. A find with no fix near its time is counted, never written at a guessed spot. Never 0,0. | 1.0 |
| **GPS track kept across restarts** | Finds a node made while the game was restarting can still be placed. | 1.1 |
| **Three ways to connect** | USB on a powered hub (1.0). ESP-NOW through one bridge board, for range and 40+ nodes (1.1). Bluetooth LE for a node placed away from the uConsole, with the link limit shown (1.1). | 1.0 / 1.1 |
| **Signed ESP-NOW frames** | Every frame carries an HMAC tag and a replay counter that survives reboots. A node spends roughly 1–4% of its scan time talking. That figure is calculated, not yet measured. | 1.1 |
| **Over-the-air updates with rollback** | Update a node over USB, ESP-NOW or Bluetooth. The image is checked with SHA-256 and kept only if it passes its self-test and checks in with its owner. Otherwise the node goes back to the old firmware. | 1.1 |
| **Flash new boards from the uConsole** | Plug a blank board in and the game flashes it, with a full erase the first time. | 1.1 |
| **Second core** | Export your owner key and Node Memory under a passphrase (scrypt + AES-256-GCM) and import them on another WDG-Bristle install. A node already busy with one core is left alone by the other. | 1.1 |
| **Nodes screen and activity HUD** | One row per node: link, channels, queue, temperature, uptime, firmware. Plus fleet totals, finds per minute, and a channel-coverage strip for nodes and Kismet. A small HUD on the map toggles with F8. | 1.0 |
| **Bench tool** | Drive real nodes over USB from a terminal, with no game needed. It never writes to a port until you tell it to. | 1.0 |
| **Open protocol** | One spec file generates both the Python and the firmware side, with test vectors checked on both. See [`bristle/docs/PROTOCOL.md`](bristle/docs/PROTOCOL.md). | 1.0 |

### Nearby alerts

| Feature | What it does | Status |
|---|---|---|
| **Following detection** | Flags a Bluetooth or Wi-Fi device seen again and again at different places over time. Your own nodes and an ignore list are left out. You set the thresholds. | 1.1 |
| **Signature alerts** | Recognises Axon body cameras, Flock Safety cameras, common item trackers, wardriving and pentest gear, and other Bristle rigs. Every signature value comes from a published source, recorded beside it in [`SIGNATURES.md`](bristle/docs/SIGNATURES.md). | 1.1 |
| **Kismet's own defensive alerts** | Deauth floods, access points copying another network's name, and similar, shown through the same alert system. | 1.1 |
| **Popup, sound, HUD line and a per-drive log** | Each alert type has its own switch, and repeats are rate-limited. | 1.1 |

### Card skimmer detection and hunt

Many gas-pump and terminal skimmers send stolen card data out over cheap Bluetooth serial modules.
WDG-Bristle watches for them during a normal wardrive and helps you find one to report.

| Feature | What it does | Status |
|---|---|---|
| **Detection while you drive** | Checks Classic Bluetooth (through Kismet) and Bluetooth LE (through the nodes) against known skimmer modules. It scores each device on module name, device class, MAC prefix and signal strength, never the name alone. Based on published research (Bhaskar et al., USENIX Security 2019). | 1.1 |
| **Suspects log** | Every sighting is logged with time, GPS position, signal strength and which radio heard it. It stays on your device and is never uploaded. | 1.1 |
| **Location estimate** | A signal-weighted estimate of where each suspect is, with a confidence radius that tightens with each pass. | 1.1 |
| **Suspects screen** | List, score, last seen, distance from you. Mark each suspect "reported" or "false alarm". | 1.1 |
| **Hunt mode** | An arrow and distance to the estimated spot, then a live signal meter with a beep that speeds up as you get closer, and a "strongest so far" marker. | 1.1 |
| **Report export** | A plain summary to hand to the station, the police or your state's fuel-pump inspectors. | 1.1 |

**Listen only.** WDG-Bristle never connects to, pairs with or sends anything to a suspected
skimmer. It says "possible skimmer" and never calls a pump safe.

**What it can't catch:** renamed modules, cellular skimmers and skimmers with no radio. If in
doubt, pay inside or use tap-to-pay.

### Wardriving and the uConsole

| Feature | What it does | Status |
|---|---|---|
| **Kismet wardrive bridge** | Start and stop Kismet from the game. Wi-Fi networks and Bluetooth devices go into the game's loot (WiGLE-format CSV), XP and map. Uses one or two Wi-Fi adapters split by band, restarts Kismet after a crash, watches the adapters, and uploads to WiGLE and WDGWars. Nodes can start and stop with it. | 1.0 |
| **Dual GPS through gpsd** | An on-board receiver and a USB receiver at the same time. The best fix wins (3D fix, then satellites, then accuracy), with a delay so it doesn't flip back and forth. Positions are never mixed between receivers. Kismet gets only the chosen receiver. | 1.0 |
| **GPS Health screen** | Which receiver is in use and why, satellites, fix quality, and antenna status where the receiver reports it. | 1.0 |
| **Hardware Check** | One screen covering the board, the AIO v2 power rails, both GPS receivers, the SDR, LoRa, Wi-Fi and Bluetooth adapters, Bristle's USB ports and its reserved Bluetooth adapter. | 1.0 |
| **Installer and self-test** | Prints a found / not-found table of your hardware, points gpsd at the receivers it can actually hear, and keeps your loot, maps and keys on every reinstall. `selftest.sh` runs the test suite on the uConsole. | 1.0 |

---

## Brought in from LOCOSP's WatchDogsGo NG

Ported from [WatchDogsGo NG](https://github.com/LOCOSP/WDGoNG) by LOCOSP. These are his features,
running as plugins here. The NG source files used are in [`plugins/_ng/`](plugins/_ng/) under
NG's PolyForm Noncommercial licence.

- **ADS-B tracks**: aircraft tracks and coverage from an RTL-SDR, with KML export.
- **433 MHz**: capture to Flipper `.sub` files, and a signal-strength hunter.
- **Loot backup and restore.**
- **Session log.**
- **WDGWars pairing and status.**
- **Offline basemap layer.**

## From Watch Dogs Go itself

The game: LOCOSP's open-world hacking RPG for the uConsole and ESP32-C5, with its world, artwork,
XP, menus and tooling. See [Watch Dogs Go](https://github.com/LOCOSP/WatchDogsGo). Other features
inherited from earlier community builds are credited in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

---

## Coming later

- A port to the NG base. Bristle's logic is already kept separate from the drawing code to make
  this easier.
- Bench-test results for every Bristle feature.
- Bluetooth Classic coverage on the nodes themselves needs a different chip. Today it comes from
  Kismet on the uConsole.

## Use it responsibly

Survey only signals you're allowed to receive, and test only equipment you own. Report suspected
skimmers. Don't open or tamper with anyone's equipment. Radio and privacy laws differ from place
to place, so know yours.
