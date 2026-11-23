# Bristle Nodes on the uConsole

The uConsole side: settings, adopting nodes, the bridge, Bluetooth, alerts, possible skimmers,
updates, a second core. Hardware and power: `HARDWARE.md`. Flashing: `FLASHING.md`. The exact
bench steps with their result tables: `BENCH_TESTS.md`.

**Nothing here has been confirmed on the uConsole or on a board yet.** It is what the code is
written to do; the bench list is where it gets proven.

## What you need

- WDG-Bristle 1.1 installed (`INSTALL_WDG_BRISTLE.md` inside the game folder). Its installer sets
  up everything Bristle needs: the Python packages (`pyserial`, `bleak`, `esptool`, `cryptography`)
  are in the game's `requirements.txt`, and your user is put in the `dialout` and `bluetooth` groups.
- Boards flashed with `v1-build5-dev`, or blank boards (the game can flash them). A board still on build 4 links and scans too; update it from the Nodes screen.
- A **powered** USB hub. Nodes are never powered from the uConsole's AIO board.

## Settings

Everything has a default. To change one, copy the example and edit it:

```
cd ~/wdg-bristle && cp -n bristle.conf.example bristle.conf && nano bristle.conf
```

`bristle.conf.example` explains every setting. The ones you are most likely to touch:

| Setting | Default | What it does |
|---|---|---|
| `region` | `U` | `U` = US channels, `E` = EU channels |
| `tie_to_kismet` | `on` | nodes start and stop with a Kismet wardrive |
| `hud`, `hud_key` | `on`, `F8` | the small line on the map and its key |
| `ble_adapter`, `ble_link`, `ble_max_links` | `hci0`, `on`, `5` | the ONE Bluetooth adapter kept for Bristle, whether nodes are linked over it, and how many at once |
| `ble_scan` | `active` | `active`: the normal scan; nodes also ask each device for its scan response, where many names are. `passive`: they only listen (fewer names) |
| `control_channel` | `1` | where nodes meet the bridge (ESP-NOW) |
| `repeat_ap_s`, `repeat_ble_s` | 300, 30 | how long before a node reports the same device again (sent to every node) |
| `alert_...`, `kismet_alerts` | all on | which nearby alerts are shown |
| `follow_...` | 500 m, 60 min, 3 places, 10 min | when a device counts as following you |
| `ignore_macs` | empty | your own devices: never alerted on |
| `skimmer_alert_score`, `skimmer_alert_rssi` | 60, -70 | when a possible skimmer raises an alert (it is logged either way) |
| `hunt_adapter` | empty | a Bluetooth adapter hunt mode may listen with |
| `exclude_ports`, `auto_probe`, `extra_usb_ids` | empty, `off`, empty | which serial ports Bristle may touch |

The owner key is not in this file. The game makes it on the first start and adds
`BRISTLE_OWNER_KEY=...` to `secrets.conf`. It is never shown. Loot backups include `secrets.conf`.

## Adopting nodes

**PLUGINS > Bristle: Nodes.**

1. Plug a board into the powered hub.
   - A board running Bristle firmware announces itself. The game hears that (it only listens),
     says hello, and the board shows up as a `NEW` row.
   - Any other ESP32 board is a `NEW PORT` row. Nothing is sent to it. **X** marks it "not a
     node" for good (your JanOS board). **W** writes Bristle firmware to it (a blank board).
     **A** says hello anyway.
2. **A** on the `NEW` row adopts it: it gets the next node number and your owner key.
3. From then on the board is recognised on any port and comes online by itself.

Adoption is over USB only. A board with no owner that is seen over Bluetooth is reported
("plug it into USB to adopt it") and left alone.

A board that belongs to someone else shows as `OTHER OWNER` and is ignored. To take over a board
you own that has another key on it, hold its BOOT button for 5 s, then adopt it.

### Keys on the Nodes screen

| Key | Does |
|---|---|
| **S** | start / stop scanning |
| **A** | say hello to a `NEW PORT`; adopt a `NEW` board |
| **I** | blink the node's LED for 10 s |
| **P** | pin the node to a band (auto, 2.4, 5, 5 low, 5 high, Bluetooth); the rest split around it |
| **B** | block / unblock |
| **X** | mark a port "not a node" |
| **W** | write firmware to the board on USB (`FLASHING.md`, A) |
| **U** twice | update the selected node over its link (`FLASHING.md`, C). **U** twice again stops it |
| **G** twice | make the selected USB node the ESP-NOW bridge, or a node again |
| **K** | export / import the owner key and Node Memory (a second core, below) |
| **F** twice | forget the node |
| **R** twice | renumber 1..N |
| **H**, or **F8** on the map | the small map line on / off |

The title bar shows the Bluetooth link (`BLE hci0: 2/5 linked`, and `LIMIT: n waiting` when more
nodes are in range than the limit) and the bridge.

## The three links

| | What you do |
|---|---|
| USB | Plug the node into the hub. |
| ESP-NOW | Adopt one board, select it, press **G** twice: it restarts as the bridge and stays on the hub. Every adopted node that is not on USB finds it by itself. |
| Bluetooth LE | Nothing: an adopted node that is not on USB advertises, and the core links it on `ble_adapter`. |

USB wins. A node the core has on USB spends no radio time on the other two.

## Start, stop, finds

1. Nodes on the powered hub (or in range), GPS fix on the map.
2. Start a Kismet wardrive (the nodes start with it), or press **S** on the Nodes screen.
3. Watch the totals line: finds per minute, `no GPS` (finds that could not be placed) and
   `on nodes` (finds still waiting on the boards) should stay near 0.
4. Stop. The drive's `wardriving.csv` goes into the WiGLE upload queue like a Kismet drive's.

A find is placed where the uConsole was when the node saw it. The GPS track is kept on disk for a
day (`loot/bristle/gps_track.csv`), so finds a node made before the game was restarted are still
placed, and a correction of the uConsole's clock mid-drive does not lose finds. A find with no GPS
position near its time is counted as "no GPS" and not written. Nothing is written at 0,0.

The channel-coverage strip says what Kismet is doing. If one of Kismet's adapters stays on a few
channels, the nodes give those channels their shortest dwell and the line says so; if Kismet hops
everything evenly, nothing changes and the line says that.

## Nearby alerts (PLUGINS > Bristle: Alerts)

Alerts show as a box at the top of the map, a line in the small map display, a sound and a line in
the terminal, and each is written to the drive's `bristle_alerts.csv`.

| Alert | When |
|---|---|
| Following | a device seen at 3 places at least 500 m apart within an hour (settings above) |
| Tracker | an AirTag / Tile / SmartTag type tracker |
| Axon, Flock | a device from that maker |
| Pentest | another wardriving or pentest device |
| Other rig | a Bristle board that is not yours |
| Possible skimmer | high score and a strong signal (next section) |
| Kismet | Kismet's own alerts: deauth floods, an access point copying another's name, and the rest of `kismet_alerts` |

**ENTER** on the Alerts screen switches a kind on or off; **O** switches the sound. The same
device does not alert again for 5 to 15 minutes. What the alerts look for is in
`plugins/_bristle/signatures.yaml`; every value there has its source next to it, and
`docs/SIGNATURES.md` lists what was left out.

Your own devices: nodes in Node Memory are never alerted on. Put the MACs of your Piglets,
Biscuits and phone in `ignore_macs`.

## Possible skimmers (PLUGINS > Skimmer suspects)

Listening only: nothing ever connects to, pairs with or requests anything from a suspect device.
What is known about one comes from ordinary Bluetooth scanning, the kind every phone does.

**What it finds and what it cannot** (also behind **?** on the screen): Bluetooth serial modules
that look factory-fresh (a default module name, the "Uncategorized" device class, a maker prefix
seen on recovered skimmers). **Not found:** a renamed module, a skimmer that reports over the
cellular network, a skimmer with no radio. Ordinary products use the same modules. A hit is a
*possible* skimmer, and no hit never means a pump is safe.

Where the data comes from: Kismet's Bluetooth source (tick `hci1` or the UB500 in Kismet's adapter
picker; never the adapter kept for Bristle) for Bluetooth Classic and LE, and the nodes' Bluetooth
LE finds. Class of Device comes from Kismet, for Classic devices, if your Kismet is new enough to
report it; the screen says when it does not.

- Every scored sighting is logged to `loot/skimmers/sightings.csv`; the list is
  `loot/skimmers/suspects.json`. They are kept by the installer, are inside loot backups, and are
  never uploaded anywhere.
- The estimated spot is the signal-weighted centre of the sightings, with a radius. It gets better
  with every pass, most of all from different directions.

| Key | Does |
|---|---|
| **H** | hunt mode for the selected suspect |
| **M** | put a marker at its estimated spot on the map |
| **R** / **X** twice / **U** | mark it reported / a false alarm (it goes on the ignore list) / remove the mark |
| **N** | notes for the report |
| **E** | export the report: `loot/skimmers/reports/possible-skimmer-<address>-<time>.txt` |
| **?** | what this can and cannot find |

**Hunt mode.** Far away: distance and which way to the estimated spot. Once a radio hears it: a
signal meter, a beep that gets faster as the signal grows, and the strongest reading so far, saved
with its position. **ESC** ends the hunt.
- Readings come from Kismet, from every node (they listen for that one address for up to 15
  minutes), and, if `hunt_adapter` is set, from a passive scan on that adapter. An adapter Kismet
  uses is borrowed for the hunt and given back when it ends.
- A Bluetooth LE device is read from every advertisement. A Bluetooth Classic module (HC-05 type)
  only shows up when Kismet's ordinary discovery finds it, so it updates more slowly.

## Updating nodes

`FLASHING.md`: **W** writes firmware to a board on USB, **U** updates a linked node over the air.

## A second core

To run the same nodes from another WDG-Bristle install:

1. On this one: Nodes screen, **K**, **E**, type a passphrase twice (8 characters or more). The
   file `bristle-core-export.json` is written into the install folder. It holds the owner key and
   Node Memory, sealed with the passphrase (scrypt + AES-256-GCM). The key is never shown.
2. Copy that file into the other install's folder.
3. On the other one: Nodes screen, **K**, **M**, type the passphrase. Node Memory is merged; if
   that install had a different owner key it is replaced, and the old key stays in its
   `secrets.conf` as `BRISTLE_OWNER_KEY_PREVIOUS`. **Restart the game** there.

Only one core runs the fleet at a time. A node that one core is running answers "busy" to the
other over the air; that core shows the node as `BUSY`, leaves it alone, and asks again every 30 s.
A node on a core's USB cable always belongs to that core.

## Where things are kept

| What | Where |
|---|---|
| Node Memory, Block list, "not a node" list | `loot/bristle/nodes.json` |
| What each node has already delivered | `loot/bristle/acks.json` |
| ESP-NOW frame counters | `loot/bristle/espnow.json` |
| The GPS track of the last day | `loot/bristle/gps_track.csv`, `gps_epochs.json` |
| Skimmer suspects, sightings, reports | `loot/skimmers/` |
| Alerts of a drive | the session folder, `bristle_alerts.csv` |
| Settings | `bristle.conf` |
| Owner key | `secrets.conf` (`BRISTLE_OWNER_KEY`) |
| Node firmware | `bristle/firmware/releases/` in the install folder |
| Finds | the session's `wardriving.csv` and `bt_devices.csv`, like every other source |

`loot/` is kept by the installer and is inside every loot backup. `bristle.conf` and `secrets.conf`
are kept by the installer too.
