# Bristle hardware: boards, hubs, power, cables

Planning notes. The figures marked *estimate* were not measured here; the bench list
(`BENCH_TESTS.md`) has the places where you measure them.

## What you need

| Part | How many | Notes |
|---|---|---|
| ESP32-C5 boards, **8 MB flash or more** | one per node, plus one for the bridge | Seeed XIAO ESP32-C5, Espressif ESP32-C5-DevKitC-1 (8 MB), Waveshare ESP32-C5-WiFi6-KIT and the like. Per-board notes: `BOARDS.md`. |
| A **powered** USB hub | one | Nodes are never powered from the uConsole's AIO board. |
| USB-C **data** cables | one per wired node | A charge-only cable powers the board and nothing else: the port never shows up. |
| Power for nodes away from the hub | as needed | Car USB sockets or power banks. |

## Which link for which node

| Link | Use it for | Limit |
|---|---|---|
| USB | Nodes next to the uConsole. Fastest, no scan time lost, and the only way to adopt or first-flash a board. | Hub ports and cable length. |
| ESP-NOW through the bridge | Nodes around the car or the house. This is the link that scales. | About 40 nodes on one bridge (`PROTOCOL.md`, timing plan). Range is that of 2.4 GHz at 1 Mbit/s: usually better than Bluetooth. |
| Bluetooth LE | A node out of the bridge's reach, or a setup with no bridge. | 5 links by default, about 7 per adapter. Costs that node about 15% of its scan time while linked (*estimate*). |

A node can have more than one link. USB wins: a node the core has on USB makes no ESP-NOW visits
and does not advertise.

## The bridge

- One Bristle board, set to the bridge role on the Nodes screen (**G**), on the powered hub.
- Use a board with **native USB** (the XIAO). At 40 nodes the bridge passes about 20 KB/s to the
  core; a board on a 115,200 baud UART port cannot.
- It scans nothing and stores nothing. If it restarts or is unplugged, the nodes keep scanning and
  keep their finds; they find the bridge again by themselves.
- Put it where it has a clear view of the nodes: high in the car, not behind metal.

## Power

- Budget **500 mA at 5 V per node** when choosing a hub or a power bank (*estimate*: the USB 2.0
  port limit. A scanning ESP32-C5 draws much less on average, with short peaks when its radio
  transmits). Five nodes: a hub with a 2.5 A supply or better.
- A hub that sags under load shows up as nodes restarting: the Nodes screen shows short uptimes,
  and a node's console says `core went quiet`.
- Power banks: many switch off when the draw is small. If a node on a power bank goes dark after a
  while, that is the power bank's low-current cutoff, not the node. Use one with an "always on" or
  "low current" mode.
- A node that loses power keeps every find it had saved to flash (it saves at least every 15 s)
  and its node number, owner and settings. It loses its clock; what it scans after that can only
  be timed once it meets the core again in that same run (`PLAN.md`, known limits).

## Heat and enclosures

- Every heartbeat carries the chip temperature; the Nodes screen shows it (TEMP).
- The firmware runs the CPU at 80 MHz, the same as the Piglet builds, to keep heat down.
- No metal enclosures: the antenna is on the board. Leave the antenna end of the board clear.
- In a closed car in the sun the cabin gets hotter than the boards make themselves. If TEMP reads
  above about 85 C, give the node shade or air (*estimate* of a sensible limit; the chip is rated
  higher).

## Flash wear

The find queue writes to flash at least every 15 s while a node has finds that are not saved yet.
Worked out from the layout (`BOARDS.md`): about 240 small writes per hour of scanning, spread over
about 700 flash blocks, which is on the order of 30 years of continuous scanning before a block
reaches the 100,000 erase cycles such flash is typically rated for. Wear is not what limits a
node. The node's `queue` console command shows how often it wrote since it started.

## Bluetooth adapters on the uConsole

| Adapter | Use |
|---|---|
| `hci0` (CM5 built-in) | Kept for Bristle: the Bluetooth link to nodes. Never give it to Kismet. |
| `hci1` (the Alfa's) and the UB500 | Kismet. Tick one of them in Kismet's adapter picker: Bluetooth Classic and LE devices from Kismet feed the skimmer suspects list. |

PLUGINS > Hardware Check warns if Kismet is set to use the adapter kept for Bristle.

## Other ESP32 boards on the same hub

A JanOS board, a Piglet or a Biscuit plugged in next to the nodes is safe: the core never writes
to a serial port unless the board on it is one of your nodes, announced itself as a Bristle board,
or you pressed **A** on its row. Mark such a board "not a node" (**X**) once and it is never
offered again.
