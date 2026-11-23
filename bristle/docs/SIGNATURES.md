# Signatures: what is in the file, where each value comes from, and what was left out

The file is `core/bristle/signatures.yaml` (in a WDG-Bristle install:
`plugins/_bristle/signatures.yaml`). You may edit it; the form is described at the top of
`nearby.py`. An entry without a `source` line is refused when the file is loaded.

Rule for this file: **every value comes from a published source that can be checked, and the
source is written in the entry.** A value that could not be checked was left out and is listed at
the bottom of this page.

## How each source was checked (2026-11-22)

Every value in the file was read in the document it is cited from. Nothing in the file rests on a
summary or a search result any more.

| Source | How it was read |
|---|---|
| IEEE OUI registry | The registry's own `oui.csv` was downloaded and searched. |
| Bluetooth SIG assigned numbers | The SIG's own `company_identifiers.yaml`, `member_uuids.yaml` and `class_of_device.yaml` were downloaded and searched. |
| Bhaskar, Bland, Levchenko, Schulman, "Please Pay Inside: Evaluating Bluetooth-based Detection of Gas Pump Skimmers", USENIX Security 2019 | The paper's PDF from usenix.org: the table of default scan profiles (section 3), the table of MAC prefixes on skimmers (section 4) and the text on device names. |
| Heinrich, Bittner, Hollick, "AirGuard - Protecting Android Users From Stalking Attacks By Apple Find My Devices", arXiv 2202.11813 | The paper's text, section 2.2.1 "BLE advertisement format": "The first two bytes are set to the company ID of Apple, the next byte is fixed to 0x12, which identifies that this advertisement is used for Find My." |
| Yu et al., "Privacy Analysis of Samsung's Crowd-Sourced Bluetooth Location Tracking System", arXiv 2210.14702 | The paper's text, section 4.5.1 "Helper-Tag Interaction": "A registered tag broadcasts OF advertisements on UUID FD5A continuously." |
| HC-08 Bluetooth UART Communication Module User Manual, software HC-08 V3.1, hardware V2.0, 2017-07-07 (www.hc01.com) | A copy of the manual (static.chipdip.ru/lib/781/DOC013781198.pdf): AT command table, command 7, `AT+NAME=xxxxxxxxxxxx`, "Revise name", default `HC-08`. |
| JDY-31 Bluetooth Module manual | A copy of the manual (alexgyver.ru/arduino/JDY-31.pdf): command 7, "broadcast name settings/query", `AT+NAME`: "Default broadcast name: JDY-31-SPP". |
| Kismet (what it reports for a Bluetooth device, its alert names, its REST routes) | Kismet's source files on its public repository (`phy_bluetooth.h/.cc`, `capture_linux_bluetooth.c`, `phy_80211.cc`, `alertracker.cc`, `datasourcetracker.cc`). |
| Piglet firmware | The Piglet source folder on this PC (`Config.h`). |

## Entries

| id | Alerts as | Matches | Source | Checked |
|---|---|---|---|---|
| `axon-oui` | Axon | MAC prefix 00:25:DF | IEEE OUI registry: Axon Enterprise, Inc. | first-hand |
| `axon-service-uuid` | Axon | BLE service UUID 0xFC81 | Bluetooth SIG member UUIDs: Axon Enterprise, Inc. | first-hand |
| `axon-company-id` | Axon | BLE company id 0x034D | Bluetooth SIG company identifiers: TASER International, Inc. | first-hand (that TASER International became Axon Enterprise in 2017 is general knowledge, not from the registry) |
| `flock-oui` | Flock | MAC prefix B4:1E:52 | IEEE OUI registry: Flock Safety | first-hand |
| `tracker-apple-findmy` | Tracker | BLE company id 0x004C with type byte 0x12 | Company id: Bluetooth SIG (Apple, Inc.). Type byte: AirGuard, arXiv 2202.11813, section 2.2.1 | first-hand |
| `tracker-tile-uuid` | Tracker | BLE service UUIDs 0xFEED, 0xFEEC, 0xFD84 | Bluetooth SIG member UUIDs: Tile, Inc. | first-hand |
| `tracker-tile-company` | Tracker | BLE company id 0x067C | Bluetooth SIG company identifiers: Tile, Inc. | first-hand |
| `tracker-chipolo` | Tracker | BLE service UUIDs 0xFE65, 0xFE33 | Bluetooth SIG member UUIDs: CHIPOLO d.o.o. | first-hand |
| `tracker-samsung-smarttag` | Tracker | BLE service UUID 0xFD5A | Assignment: Bluetooth SIG member UUIDs (Samsung Electronics Co., Ltd.). That SmartTags use it: Yu et al., arXiv 2210.14702, section 4.5.1 | first-hand |
| `pentest-piglet-setup` | Pentest | Wi-Fi SSID `Piglet-WARDRIVE` | Piglet firmware source, `Config.h` | first-hand |
| `rig-bristle-advert` | Other rig | a Bristle advertisement (service data under Bristle's UUID) | Bristle protocol spec | first-hand |
| `rig-bristle-name` | Other rig | Bluetooth name starting `BRISTLE-` | Bristle protocol spec | first-hand |
| `skimmer-name-hc` | Skimmer score +20 | name `HC-05` or `HC-06` | Bhaskar et al. 2019, section 3 table | first-hand |
| `skimmer-name-rnbt` | Skimmer score +20 | name starting `RNBT-` | Bhaskar et al. 2019 (the table prints "RBNT-*", the text "RNBT-xxxx") | first-hand |
| `skimmer-name-hc08` | Skimmer score +20 | name `HC-08` | HC-08 user manual, software V3.1, AT command 7 | first-hand |
| `skimmer-name-jdy31` | Skimmer score +20 | name `JDY-31-SPP` | JDY-31 module manual, command 7 | first-hand |
| `skimmer-class-uncategorized` | Skimmer score +25 | Class of Device major class 31 | Bhaskar et al. 2019 (all 87 skimmers "Uncategorized"); the number from Bluetooth SIG `class_of_device` | first-hand |
| `skimmer-mac-roving` | Skimmer score +35 | MAC prefix 00:06:66 | Bhaskar et al. 2019 table; IEEE OUI registry: Roving Networks | first-hand |
| `skimmer-mac-bolutek` | Skimmer score +35 | MAC prefix 98:D3:31 | Bhaskar et al. 2019 table; IEEE OUI registry: Shenzhen Bolutek Technology | first-hand |
| `skimmer-mac-paper-unregistered` | Skimmer score +30 | MAC prefixes 20:13:04, 20:17:11, 20:18:01, 20:18:04, 20:18:07, 20:18:08, 20:18:09, 20:18:10, 20:18:11, 98:D3:35 | Bhaskar et al. 2019 table, maker "Unknown" | first-hand |

A strong signal adds up to 10 to a skimmer score. The weights (20 / 25 / 30 / 35) are choices made
for this build, not values from the paper. They are set so that a name on its own stays a weak score
(listed, never alerted on), and the three signs the paper's app looked at together make a strong one.

## What a match can and cannot tell you

- A node reports a Bluetooth LE device's name, company id, the first byte of its manufacturer data
  and up to three 16-bit service UUIDs. Kismet reports name, address and, for Bluetooth Classic,
  the Class of Device (current Kismet only: an older one does not, and the suspects screen says so).
- A MAC prefix or a company id says who made the radio, not what the device is. An Axon dock and an
  Axon body camera look the same here.
- Phones, earbuds and many trackers change their Bluetooth address every few minutes. Each address
  is a new device to the "following" check. A tracker away from its owner keeps one address much
  longer, which is the case that check is for.

## Checked and dropped (after the 1.1 run)

Six values had first been taken from a search result's summary. Each was then read in the document
itself. Four stayed (the two module names, the Apple type byte, SmartTag's UUID). Two went:

| Value | What the document says | Why it is out |
|---|---|---|
| Module name `linvor` | The HC-06 manual that could be read ("HC-06 Bluetooth module", 12 pages, copy at electronicaembajadores.com/Datos/manuales/lc/lcbt/lcbtht6.pdf) gives the factory settings as "baud rate 9600N81, name HC-06, password 1234". `linvor` turned up only as a firmware version string (`linvorV1.8`) in a hobbyist's guide. | No manual was found that gives `linvor` as a default name. `HC-06` is in the file already, from the paper. |
| Pwnagotchi address DE:AD:BE:EF:DE:AD | It is real: pwngrid, the program a Pwnagotchi advertises itself with (github.com/evilsocket/pwngrid), has `SignatureAddrStr = "de:ad:be:ef:de:ad"` in `wifi/defines.go`, and `wifi/pack.go` builds a beacon frame with `Address2: SignatureAddr` and `Address3: from`. | Address2 is the transmitter address; Address3, the BSSID, is the unit's own address. A node's Wi-Fi scan reports access points by BSSID, so this build never sees the signature address and the entry could not have matched anything. Listing it would have claimed a detection that does not exist. |

## Left out (no source that could be checked)

| Wanted | Why it is not in the file |
|---|---|
| Module name `HC-03` | No document found that gives it as a default name. |
| Other JDY module names (`JDY-30`, `JDY-33`, ...) | Only the JDY-31 manual was found. |
| Flock Safety camera SSIDs or Bluetooth names | Only community lists were found, no primary source. The registered MAC prefix is in the file. |
| ESP32 Marauder (its own access point or Bluetooth name) | Not looked up in its source. |
| Biscuit wardriver | No source for it on this PC, and none looked up. |
| Pwnagotchi | See above: its address is not one a node's scan reports. It would need the nodes to report frames by transmitter address. |
| Apple's 0xFCB2 service (assigned to Apple; used by the unwanted-tracker detection scheme) | The assignment was checked; what it is used for was not, so it would be a guess as a tracker signature. |
| Google's Find My Device network tags | No value checked. |
| Other makers of serial Bluetooth modules | The paper lists only the two makers and the unregistered prefixes above. |

To add one: find the document, add an entry with its `source`, and run the unit tests
(`test_nearby.py` checks that the file loads and that every entry names a source).
