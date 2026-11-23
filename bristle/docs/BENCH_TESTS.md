# Bristle bench tests: one list, checkpoints A to H

Real boards only. **Nothing below has run on a board or on the uConsole: every "expect" is what
the code is written to do, not something that was seen.** Write what you saw into the tables.

Firmware: `firmware/releases/v1-build5-dev/`. Game: `WDG Main\dist\wdg-bristle.zip` (WDG-Bristle 1.2.1).

Build 5 is build 4 with one fix (a replayed ESP-NOW frame: `RELEASE_NOTES.md` in the release
folder). If you have already flashed build 4 on a board, it works with this game as it is; update
it over the air in F2, where you then see the build number change from 4 to 5.

## The order

The checkpoints are in the order that wastes the least time, not in letter order:

| # | Checkpoint | What it proves | Time |
|---|---|---|---|
| 1 | **A** | the firmware starts, its self-test passes, the board's LED and BOOT pin are right | 20 min |
| 2 | **R** | WDG-Bristle 1.2.1 installs, brings your data over from an earlier install, and everything from before still works; the Kismet channel lock and split (R12 to R15); the K kill switch as it is marked now (R17) | 50 min |
| 3 | **B** | nodes over USB in the game: adopt, scan, finds in loot and on the map | 45 min |
| 4 | **F1** | the game flashes a blank board | 10 min |
| 5 | **D** | the ESP-NOW bridge, in the house, then in the car | 1 h |
| 6 | **E** | one node over Bluetooth, away from the uConsole | 30 min |
| 7 | **F2** | updates over the air on each link, a cut-off update, rollback | 45 min |
| 8 | **G** | nearby alerts and the skimmer test with a stand-in | 1 h, with a short drive |
| 9 | **S** | a second core (only if you have a second install) | 20 min |
| 10 | **C** | the long outage: a node on a power bank overnight | overnight |
| 11 | **H** | the full drive | a drive |

If a step fails, stop there and send me the step number, what you saw, and the terminal lines
that start with `[BRISTLE]`. Useful from a node: its `status`, `links` and `queue` console output.

To read a node's console (close the game first, or use a board the game is not linked to):
```
python3 -m serial.tools.miniterm /dev/ttyACM0 115200
```

---

## 1. Checkpoint A: the firmware on real boards

**Plug in:** one XIAO ESP32-C5 and one other C5 board, one at a time, into the **PC**, with USB-C
data cables.

| # | Do | Expect |
|---|---|---|
| A1 | Flash the FULL image at **0x0 with erase** (`FLASHING.md`, B). Folder: `xiao_c5` for the XIAO; `devkit_c5` for a C5 board on its native USB port; `devkit_c5_uart` for a board whose only USB port is a CH343/UART chip. | esptool or the web flasher ends without an error. |
| A2 | Open the console at 115200, press RESET. (A native-USB board drops the port for a moment: reconnect; if the line went by, type `version`.) | One line, with no WARNING in it: `# Bristle node build 5 (<board>), hw <12 hex>, no owner yet`. Then, once a second, a line starting `BCN:hw=`. |
| A3 | Type `selftest`. | 15 lines starting `# PASS` (crc16, encode, encode2, plan_hash, plan_short, owner_id, node_proof, core_proof, frame, frame_poll, frame_piece, frame_open, frame_tamper, base64url, roundtrip), then `# SELFTEST PASS`. The `BCN:` lines stop for a minute. |
| A4 | Type `status`. | Among the lines: `# flash layout ok (app slot 2560 KB, queue 2944 KB)`, `# clock not set (...)`, `# role node, firmware update: no update in progress`. |
| A5 | Type `queue`. | `holding 0 finds`, a flash total of about 2944 KB, and `since this boot: finds written to flash 0 times`. |
| A6 | Type `links`. | `ESP-NOW: channel 1 (no owner: nothing is sent), bridge not heard, visits 0 ...` and `Bluetooth link: advertising or resting ...`. |
| A7 | Type `identify 5`. | The LED blinks for 5 s. If it stays lit when it should be off, the polarity in `Boards.h` is backwards for this board: note it. |
| A8 | Type `boot`. Hold BOOT and type `boot` again. | `reads 1`, then `reads 0`. If it does not change, the BOOT pin in `Boards.h` is wrong for this board: note it. |

| Step | XIAO | Other C5 | Notes |
|---|---|---|---|
| A1 flash | | | |
| A2 banner (build 5, no WARNING) and BCN lines | | | |
| A3 selftest (15 PASS) | | | |
| A4 flash layout ok | | | |
| A5 queue | | | |
| A6 links | | | |
| A7 LED | | | |
| A8 BOOT pin | | | |

(The bench tool `tools/bristle_bench.py` still works for adopting and scanning without the game. It
is not needed for this list.)

---

## 2. Checkpoint R: WDG-Bristle 1.2.1 installed from the zip

**Plug in:** nothing new. Your current install is not changed by any step here: it stays as the
rollback.

```
# on the uConsole, with the zip copied to your home folder
cd ~ && sha256sum wdg-bristle.zip
unzip -o wdg-bristle.zip && cd ~/wdg-bristle && ./install.sh
./selftest.sh --tests-only
```

| # | Do | Expect |
|---|---|---|
| R1 | `sha256sum wdg-bristle.zip` | The value I gave you in the report. |
| R2 | `./install.sh` in a freshly unzipped folder (no `loot/`, no `secrets.conf` in it yet) | Step "2/9 Your data": `Earlier install(s) found: n`, one line per earlier install with the date of its newest data and what it holds (every earlier install under your home, wherever it is, not only the one next to this folder), then `Copying from <folder> (the one with the newest data)`, a `copied:` line for each of loot, maps, secrets.conf, whitelist.json and the rest, and `<folder> was not changed: keep it as the backup`. Any other earlier install is listed as `Not used`. The same lines are in `copied-from-earlier-install.txt`. In the old folder nothing has changed (`ls -la` it: same files, same dates). Step 8: the old icon is removed if there was one. On a reinstall over an existing WDG-Bristle folder nothing is looked for and nothing is copied. **Tell me if an earlier install of yours was not found, and where it is.** |
| R3 | `./selftest.sh --tests-only` | `RESULT: PASS`. |
| R4 | Look at the desktop. Start the game from the icon. | One icon, **WDG-Bristle**. Window title `WDG-Bristle`. Intro: `by LOCOSP`, then `WDG-Bristle 1.2`. Terminal: `[SYS] WDG-Bristle 1.2 (base 0.9.21.3)`. |
| R5 | Look at your data. | Level and XP as before; old sessions in the loot list; WDGWars pairing still valid; WiGLE key still accepted. |
| R6 | SYSTEM > Update Game. | `Updates: managed manually`. No GitHub source; nothing is fetched. |
| R7 | SYSTEM > Firmware C5/SOLO with the network off, press R. Do not flash. | `Approved C5 package: JanOS 1.7.0 + SOLO`, `Source: shipped with this build. Updates: managed manually`. |
| R8 | PLUGINS > Loot: Backup / Restore. | Earlier backups are listed; a new one is named `wdg-bristle-bak-<time>.tar.gz`. |
| R9 | A Kismet wardrive (start, finds, stop); GPS: Health; ADS-B; a WiGLE upload; a WDGWars sync. | As before. |
| R10 | With your JanOS board plugged in and no node: start the game. | It starts about 1.3 s later than before (the game listens on the port once to see whether the board is a Bristle board) and connects to the JanOS board as before. Nothing is written to the port for that. |
| R11 | PLUGINS menu. | Three new entries: `Bristle: Nodes`, `Bristle: Alerts`, `Skimmer suspects`. Each opens; ESC closes it. |
| R12 | Kismet adapter picker with your Wi-Fi cards ticked. Highlight one card, press **RIGHT** a few times, then **LEFT**, then **L**. | The row shows `LOCKED: channel n`, stepping through that card's own channels; **L** takes the lock off. Under the list, `CHANNEL PLAN` has one line per ticked card: its bands and channels, or `locked on channel n`. With two cards: 2.4 GHz on one, 5/6 GHz on the other, as before. With three: 2.4 GHz, 5 GHz low, 5 GHz high. |
| R13 | Lock one card to channel 6 and start the wardrive. Open Kismet's own web page (Data Sources). Stop, open the picker again. | Terminal: `<card> is locked to channel 6 (no hopping)`, then `Channels split between the adapters -- ...; <card> stays on channel 6`. In Kismet that source sits on channel 6 and does not hop; the others hop their share, without channel 6. PLUGINS > Hardware Check shows the same under KISMET. The picker opens with the lock still set (also after a restart of the game, and after unplugging and replugging the card). **This is the first time a lock meets a real card and a real Kismet: tell me what Kismet shows.** |
| R14 | With a card locked and the nodes scanning: look at the line under the coverage strip on the Nodes screen. Then unplug the locked card. | `Kismet stays mostly on 6: nodes give those channels the shortest dwell`. After the unplug: the other cards get channel 6 back (`Channels split ...` again in the terminal, no `stays on channel 6`), and within about a minute the nodes' line goes back to `Kismet hops N channels evenly`. |
| R15 | (Optional) Close the game. In `kismet_adapters.json` set `"wifi_locks"` for one card's MAC to a channel it cannot tune (`"100"` for a 2.4 GHz only card). Start, open the picker, start the wardrive. | Picker: `<card>: cannot tune channel 100: it will hop instead`. Terminal at the start: `<card> cannot tune channel 100 -- the lock is dropped for this run and it hops like the others`. The card captures and hops. |
| R16 | SYSTEM > Firmware C5/SOLO. Do not flash. | The screen as before. No line about an unfinished install (there is none). If you ever see `A FIRMWARE WRITE DID NOT FINISH` there, press **F** and send me the page and the lines it prints in OUTPUT before pressing anything else. |
| R17 | **The K kill switch, as it is marked since 1.2.1.** Start a Kismet wardrive with two Wi-Fi cards ticked, monitor mode (**M**) on for one of them only. Look at the terminal, then at the map, then at TAB > PLUGINS. Press **K** on the map. Then `iw dev` in a terminal. | Terminal at the start, once: `[KISMET] Kismet running. Press K anywhere to stop and turn monitor mode off`. On the map, at the left edge under the top bar, a small box: `K: stop Kismet`; it is gone when Kismet is not capturing. PLUGINS: `Kismet: Stop (or K anywhere)`. After **K**: `[KISMET] Back in managed mode and handed back to NetworkManager: <the card with M>` and `[KISMET] Left as they were: <the other card> (monitor mode was not switched on for it in the picker)`. `iw dev`: the first card says `type managed`, and normal Wi-Fi works again without a reboot. If a card would not switch back you get `STILL IN MONITOR MODE: <card> (...)` with the command to do it by hand: send me that line. Tell me if the box on the map sits on top of anything. |

| Step | Result | Notes |
|---|---|---|
| R1 checksum | | |
| R2 earlier installs found and listed, data copied, old folder untouched | | |
| R3 selftest --tests-only PASS | | |
| R4 one icon; name and 1.2 on window, intro, version line | | |
| R5 loot, keys, settings all there | | |
| R6 Update Game says managed manually | | |
| R7 firmware window, shipped image, no network | | |
| R8 old backups listed, new name | | |
| R9 Kismet, both GPS, ADS-B, uploads | | |
| R10 JanOS board still found; start delay | | |
| R11 the three Bristle screens open | | |
| R12 picker: lock keys, the plan per card | | |
| R13 a locked card under a real Kismet; lock remembered | | |
| R14 nodes count the locked channel as covered; unplug gives it back | | |
| R15 a channel the card cannot tune (optional) | | |
| R16 firmware screen, nothing pending | | |
| R17 K kill switch: the label, the start line, the menu entry, the cards listed at stop | | |

---

## 3. Checkpoint B: nodes in the game over USB

**Plug in:** the powered hub into the uConsole; the two boards from A into the hub; your JanOS
board too, if you use one.

| # | Do | Expect |
|---|---|---|
| B1 | Before the first start: `ls -l ~/wdg-bristle/secrets.conf`. Start the game, quit, `ls -l` again, `grep -c BRISTLE_OWNER_KEY secrets.conf`. | Terminal: `[BRISTLE] A new owner key was added to secrets.conf (it is never shown)`. Same owner, mode `-rw-------`, the count is 1, every earlier key line is still there. |
| B2 | PLUGINS > Bristle: Nodes. | Each Bristle board shows as a `NEW` row within a few seconds (it announced itself; the game said hello). The JanOS board is the game's own ESP32 as always, or a `NEW PORT` row if the game is not using it: nothing was sent to it. |
| B3 | If a `NEW PORT` row is your JanOS board: select it, press **X**. | It is marked "not a node" and never listed again. |
| B4 | Select a `NEW` row, press **A**. Same for the second board. | `Node 1 online over USB`, `Node 2 online over USB`. Rows turn green. |
| B5 | Press **S**. | Rows show `scanning`; the totals line counts finds per minute; the terminal shows `[WiFi] <name> Ch:<n> (node <n>)`; markers appear on the map where you were. |
| B6 | `tail -n 5 ~/wdg-bristle/loot/<today's session>/wardriving.csv` | Rows with real coordinates, none at 0,0. |
| B7 | Press **S** to stop. Then start a Kismet wardrive; stop it. | With `tie_to_kismet = on`: the nodes start with Kismet and stop with it. After a nodes-only run that wrote rows: `This drive's wardriving.csv is in the WiGLE upload queue`. |
| B8 | While scanning: pull the hub's cable from the uConsole for a minute, plug it back. | Both nodes come back online by themselves, their backlog arrives, "on nodes" goes back to 0. No network twice in wardriving.csv. |
| B9 | While scanning: press RESET on one node. Watch the terminal and the "on nodes" count for a minute. | Terminal: `Node n restarted or dropped the link: connecting again` (or `went quiet ... reconnecting`), then `Node n online over USB`. No line with `refused` in it. It comes back with the same number and keeps scanning. "On nodes" goes back to 0 within a minute and stays there when you stop scanning. (Before 1.2.1 an ACK that missed the restarting node was never repeated, and that count would have stayed up for good.) |
| B10 | Plug in a Biscuit or a Piglet. Press **A** on its `NEW PORT` row, then **X**. | It never becomes a node. After **A**: `did not answer: not a Bristle node`. After **X**: gone for good. |
| B11 | **I** on a node. **P** a few times. **R** twice. | Its LED blinks 10 s. The pin cycles and the channel column follows. Nodes are renumbered 1..N. |
| B12 | On the map press **F8** twice. Quit and start the game. | The small line (bottom left) goes away and comes back; the game remembered the setting. |
| B13 | PLUGINS > Hardware Check. In Kismet's adapter picker tick `hci0`, look again, untick it. | `Bristle nodes: 2 online of 2` and `hci0 ... kept for Bristle`; with `hci0` ticked: `KEPT FOR BRISTLE BUT KISMET IS SET TO USE IT`. |
| B14 | Quit the game mid-scan, start it again. | Both nodes come back online without adopting. Finds the nodes made while the game was off for that moment are placed (the GPS track is on disk), not counted as "no GPS". |
| B15 | While scanning: `sudo date -s "+2 minutes"` in a terminal, wait 10 s, then `sudo date -s "-2 minutes"`. | Each time: `The uConsole's clock changed: node clocks were set again`. The "no GPS" count does not jump. (Skip this if your clock is disciplined by something that fights `date`.) |
| B16 | Look at the channel-coverage line while Kismet captures. | `Kismet hops N channels evenly: nodes split the channels evenly`, or, if a Kismet adapter is locked to a few channels, `Kismet stays mostly on ...`. |
| B17 | PLUGINS > Loot: Backup: make a backup. `tar tzf ~/wdg-bristle-bak-*.tar.gz \| grep bristle` | `loot/bristle/nodes.json` and the other files in `loot/bristle/` are listed. |
| B18 | Last: **F** twice on node 2, then adopt it again. | `Node 2 forgotten`; the board shows as `NEW`; **A** makes it a node again. |

| Step | Result | Notes |
|---|---|---|
| B1 owner key added, secrets.conf intact | | |
| B2 Bristle boards show as NEW by themselves; JanOS board untouched | | |
| B3 JanOS board marked | | |
| B4 adopt both boards | | |
| B5 scan: rows, terminal, map markers | | |
| B6 wardriving.csv rows with coordinates | | |
| B7 start/stop with Kismet; WiGLE queue line | | |
| B8 unplug / replug | | |
| B9 node reboot mid-scan | | |
| B10 Biscuit / Piglet never a node | | |
| B11 identify, pin, renumber | | |
| B12 HUD toggle remembered | | |
| B13 Hardware Check rows and the hci0 warning | | |
| B14 restart: nodes back, backlog placed | | |
| B15 clock change mid-drive | | |
| B16 Kismet line under the coverage strip | | |
| B17 Node Memory inside the loot backup | | |
| B18 forget and adopt again | | |

---

## 4. Checkpoint F1: flash a blank board from the uConsole

**Plug in:** a third board into the hub. If it already runs Bristle, erase it first on the PC
(`esptool ... erase-flash`) so that it is really blank.

| # | Do | Expect |
|---|---|---|
| F1.1 | Nodes screen. | A `NEW PORT` row for the blank board. |
| F1.2 | Select it, press **W**. | A box: the port, `Board: < ... >`, `ERASES THE WHOLE BOARD`, `Image: v1-build5-dev`. |
| F1.3 | **LEFT/RIGHT** until the board type is right. **ENTER**. | `Flashing <port>: erasing ...`, then `writing ...` with esptool's progress; about a minute. Then `<port> now holds v1-build5-dev`. |
| F1.4 | Wait a few seconds. | The board shows up as a `NEW` row. **A** adopts it as node 3. |
| F1.5 | Select node 1 (on USB), press **W**. | The box says `Writes the app only: owner, number, settings and stored finds are kept.` **ENTER**: after the flash node 1 comes back online as node 1 without adopting. |
| F1.6 | Try **W** on the JanOS board's row, if it has one. | There is none to select if you marked it "not a node". If the game holds it as its own ESP32: `... is the game's own ESP32: not flashed`. |

| Step | Result | Notes |
|---|---|---|
| F1.1-F1.4 blank board flashed and adopted | | |
| F1.5 app-only flash keeps the node | | |
| F1.6 the JanOS board cannot be flashed by mistake | | |

---

## 5. Checkpoint D: the ESP-NOW bridge

**Plug in:** the hub with one XIAO that stays on it (it becomes the bridge). The other nodes get
power from a power bank or a car socket, **not** from the hub.

| # | Do | Expect |
|---|---|---|
| D1 | Nodes screen: select the XIAO on the hub, press **G** twice. | `Node n is restarting as the bridge`. Its row comes back with STATE `bridge` and `relays ESP-NOW: 0 node(s)`. Terminal: `Bridge online on <port>`. Title bar: `bridge: 0 node(s)`. |
| D2 | Power a node from the power bank, a metre away. | Within about 5 s: `Node 2 online over ESP-NOW`. Its row shows LINK `NOW`. The bridge row counts `1 node(s)`. |
| D3 | Press **S**. | Finds arrive from node 2 as over USB. "on nodes" stays small. |
| D4 | On node 2's console (plug its USB data cable into the PC, not the hub): `links`. | `ESP-NOW: channel 1, bridge heard, visits <growing>, last visit took <n> ms, old frames refused 0, frames not mine 0`. **Write down "last visit took"**: 15 to 40 ms is what I worked out. |
| D5 | Power the other nodes the same way. | Each comes online over ESP-NOW. Totals count them. |
| D6 | Unplug the bridge from the hub for 30 s, plug it back. | The nodes' rows go offline, then all come back by themselves within about 30 s. `dropped` stays 0; the backlog arrives. |
| D7 | Press RESET on a wireless node. | It comes back online with the same number. On its console `links` shows `old frames refused` 0 or a small number, and it is online: the frame counters picked up after the restart. |
| D8 | Quit the game, start it again. | The bridge and the nodes come back online. No node shows `old frames refused` growing without end. |
| D9 | Plug a wireless node into the hub while it is online over ESP-NOW. | Its row changes to LINK `USB`. On its console `links` says `(resting: USB is the link)` and `visits` stops growing. Unplug it: back to `NOW`. |
| D10 | Carry a node away until it drops out, then back. | It goes offline around the edge of range and comes back by itself; its finds from the time away arrive (`on nodes` drains). Note the distance. |
| D11 | In the car: bridge on the hub, nodes around the car, a 15-minute drive with Kismet. | All nodes online for the drive; finds from every node in wardriving.csv; `dropped on nodes` 0. |
| D12 | `control_channel = 11` in `bristle.conf`, restart the game. | Bridge and nodes move to channel 11 within a minute (`links` on a node says `channel 11`). Set it back to 1. |

| Step | Result | Notes |
|---|---|---|
| D1 a board becomes the bridge | | |
| D2 a node comes online over ESP-NOW (seconds) | | |
| D3 finds over ESP-NOW | | |
| D4 "last visit took" (ms) | | |
| D5 all nodes over ESP-NOW | | |
| D6 bridge unplugged and back | | |
| D7 node restart: counters pick up | | |
| D8 game restart: counters pick up | | |
| D9 USB wins | | |
| D10 range, and catching up after | | |
| D11 the drive | | |
| D12 control channel change | | |

Also useful: the bridge row's `rx`/`tx` numbers after the drive, and from one node `queue`.

---

## 6. Checkpoint E: one node over Bluetooth

**Plug in:** nothing into the hub for this node: power it from a power bank, out of the bridge's
reach, or unplug the bridge for this test. Kismet must not have `hci0` ticked.

| # | Do | Expect |
|---|---|---|
| E1 | Nodes screen title bar. | `BLE hci0: 0/5 linked`. If it says `BLE link: off (...)`, the reason is in the brackets (for example `bleak` not installed, or the adapter is missing). |
| E2 | Power the node, bridge unplugged. | Within about 20 s: `Node n online over Bluetooth`. LINK `BLE`. Title bar `1/5 linked`. |
| E3 | Press **S**; let it run 5 minutes. | Finds arrive. The node keeps scanning Wi-Fi and Bluetooth while linked. Note finds per minute against the same node on USB: I expect about 15% fewer. |
| E4 | Carry the node out of range and back. | Offline, then online again by itself; its backlog arrives. |
| E5 | Plug the bridge back in. | The node stays on Bluetooth or moves to ESP-NOW (`NOW` wins over `BLE` for delivering finds); either way it stays online and nothing is doubled. |
| E6 | Set `ble_max_links = 1`, restart the game, power two nodes out of the bridge's reach. | One links; the title bar says `1/1 linked, LIMIT: 1 waiting`, and the terminal `Bluetooth link limit reached (1)`. Set it back to 5. |
| E7 | Power a board that has **no owner** nearby (forget one first). | Terminal: `A Bristle board with no owner is in Bluetooth range (...): plug it into USB to adopt it`. It is not linked. |
| E8 | Start a Kismet wardrive with `hci1` ticked while a node is linked on `hci0`. | Both keep working. |

| Step | Result | Notes |
|---|---|---|
| E1 Bluetooth link state in the title bar | | |
| E2 node online over Bluetooth | | |
| E3 finds; scan rate while linked | | |
| E4 out of range and back | | |
| E5 with the bridge back | | |
| E6 the link limit is shown | | |
| E7 a board with no owner is only reported | | |
| E8 Kismet on hci1 at the same time | | |

---

## 7. Checkpoint F2: updates over the air

Boards flashed with build 5 get build 5 again: that proves the transfer, the check and the switch
to the other slot. A board you flashed with build 4 earlier shows the build number change from 4
to 5 in the same steps.

**Plug in:** node 1 on the hub (USB); node 2 through the bridge; node 3 over Bluetooth.

| # | Do | Expect |
|---|---|---|
| F2.1 | Select node 1 (USB), **U** twice. | `Updating node 1 to build 5 over USB`. STATE `upd n%` rising. About a minute. Then `Node 1 has the new image and is restarting`, and within a few seconds `Node 1 now runs build 5`. Its STATE may show `new build` for a moment. |
| F2.2 | On node 1's console: `update`. | `no update in progress` with no "ON TRIAL" in it: the image was confirmed. |
| F2.3 | Select node 2 (ESP-NOW), **U** twice. | The same, slower: I expect 3 to 5 minutes. **Write down the time.** The node does not scan meanwhile. |
| F2.4 | Select node 3 (Bluetooth), **U** twice. | The same; write down the time. |
| F2.5 | **Cut one off.** Start an update of node 1 over USB and pull its cable at about 50%. Plug it back. | `Node 1 update failed: the link closed during the update: the node keeps its old firmware`. Node 1 comes back online on the build it had and scans. Its console `update` says `last update dropped: its link closed`. |
| F2.6 | Start an update and press **U** twice at about 30%. | `Update stopped: node n keeps its old firmware`. |
| F2.7 | **Rollback.** Update node 2 over ESP-NOW. The moment the messages say it is restarting, quit the game and leave it off for 6 minutes. Watch node 2's console. | After the restart: `update`: `THIS BUILD IS ON TRIAL`. About 5 minutes later the console prints `new firmware not confirmed: going back to the previous build` and the board restarts. Start the game: node 2 comes online; the trial is over. |
| F2.8 | Update the bridge (select its row, **U** twice). | It updates over USB like a node and comes back as the bridge; the wireless nodes drop for a moment and return. |

| Step | Result | Time taken | Notes |
|---|---|---|---|
| F2.1 over USB | | | |
| F2.2 confirmed, not on trial | | | |
| F2.3 over ESP-NOW | | | |
| F2.4 over Bluetooth | | | |
| F2.5 cable pulled mid-update | | | |
| F2.6 stopped by hand | | | |
| F2.7 rollback when the owner never shows up | | | |
| F2.8 the bridge | | | |

---

## 8. Checkpoint G: nearby alerts and possible skimmers

**Plug in:** the fleet as you drive it. In Kismet's adapter picker tick `hci1` (or the UB500), not
`hci0`. For the skimmer test: a spare Bluetooth module renamed `HC-05`, or your phone renamed
`HC-05`, and if you have one a BLE module renamed `HC-08`. Leave it in one spot (a parked car, a
window sill) where you can drive past and then walk up to it.

### Alerts

| # | Do | Expect |
|---|---|---|
| G1 | PLUGINS > Bristle: Alerts. | The eight kinds, each `[ON ]`; `20 signatures loaded`. **ENTER** switches one; **O** the sound. Quit and start: remembered. |
| G2 | Drive a short loop with alerts on. | For anything matched: a box at the top of the map, a red `!` line in the small map display, a sound, a terminal line `[ALERT] ...`. Nothing for your own nodes. |
| G3 | After the drive: `cat ~/wdg-bristle/loot/<session>/bristle_alerts.csv` | One row per alert with time, kind, device, position. |
| G4 | Carry a second phone or a tracker with you in the car for 20 minutes over a few kilometres (set `follow_min_minutes = 5` to shorten the wait). | One `Seen at 3 places over n min` alert for it. Your own phone alerts too unless its MAC is in `ignore_macs`: add it and it stops. |
| G5 | With Kismet capturing near an access point you can restart: restart it a few times, or just watch a normal drive. | Any Kismet alert named in `kismet_alerts` shows as `Kismet: <NAME>`. (A deauth flood only shows if one happens: do not make one.) |

### Possible skimmers

| # | Do | Expect |
|---|---|---|
| G6 | Drive past the stand-in with a Kismet wardrive running. | PLUGINS > Skimmer suspects lists it: name `HC-05`, its address, score 20 to 30 (a name alone is a weak score), SEEN 1 or more, an estimated spot near where it is. No alert popup: the score is below 60. |
| G7 | Look at the lines under the list. | `Why: default module name HC-05/HC-06 ...`; `Class of Device:` a value, or `not given (this Kismet does not report Class of Device)`. **Tell me which.** |
| G8 | Drive past again from another direction. | SEEN goes up; the estimated spot moves closer to the real one and its `+-` radius shrinks. |
| G9 | `head -3 ~/wdg-bristle/loot/skimmers/sightings.csv` | The header and one row per sighting: time, lat, lon, RSSI, who heard it (`Kismet ...` or `node n`), name, address, class, score. |
| G10 | Select it, **M**. | The suspects screen closes and the map moves to the estimated spot, zoomed in so the whole confidence radius fits. A red outline shows the radius (wider than tall away from the equator), with a small cross on the spot, a marker, and a line bottom right: `POSSIBLE SKIMMER <name>: within about n m  [ESC] back to you`. Your own figure is drawn where you are, or not at all if that is off the map. **ESC** or an arrow key: the map goes back to you at the zoom it had. **Tell me whether the marker itself shows, and whether the radius is big enough to see.** |
| G11 | Select it, **H**, and walk toward it with the uConsole. | Far: `n m, head <direction>`. Near: `You are inside the estimated area`. When a radio hears it: the meter fills, the beep gets faster as you get closer, `STRONGEST SO FAR` shows a value with a position. A phone or a BLE stand-in updates every second or so; a Classic module only every several seconds (Kismet's discovery). |
| G12 | (Optional) `hunt_adapter = hci1` in bristle.conf, restart, hunt again with Kismet running. | `LISTENING WITH` says `hci1 borrowed from Kismet; listening on hci1`, and the BLE stand-in updates faster. After ESC, Kismet's Bluetooth source is capturing again (Kismet's own screen). If it says `no fast listening: ...`, send me that line. |
| G13 | **ESC** to end the hunt. **N**: type a note. **E**: type the place. | `Report written: .../loot/skimmers/reports/possible-skimmer-<address>-<time>.txt`. `cat` it: place, times, address, name, class, estimated spot, signal readings, your note, and the words "possible" and "not proof". |
| G14 | **X** twice (false alarm). Drive past again. | The row goes grey, status `false alarm`. SEEN does not go up any more and nothing alerts for it. **U** removes the mark. |
| G15 | If you have a real HC-05 module (not a phone): rename nothing, power it, hold the uConsole next to it with Kismet running. | Score 45 or more (name + class Uncategorized, + a maker prefix if it has one of the listed ones); at close range a `Possible skimmer close by` alert. |
| G16 | **?** on the suspects screen. | What it can and cannot find, including: renamed modules, cellular skimmers and skimmers with no radio are not detected. |

| Step | Result | Notes |
|---|---|---|
| G1 switches | | |
| G2 popup, map line, sound, terminal | | |
| G3 alert log | | |
| G4 following | | |
| G5 Kismet alerts | | |
| G6 stand-in listed with a position | | |
| G7 Class of Device given by Kismet? | | |
| G8 second pass improves the spot | | |
| G9 sightings.csv | | |
| G10 open on the map: centred, radius drawn, ESC back | | |
| G11 hunt on foot | | |
| G12 borrowed adapter (optional) | | |
| G13 report | | |
| G14 false alarm stops it | | |
| G15 a real module (optional) | | |
| G16 limits shown | | |

---

## 9. S: a second core (only with a second install)

**Plug in:** nothing special. You need a second WDG-Bristle install (another uConsole, or a
second folder on the same one, run one at a time).

| # | Do | Expect |
|---|---|---|
| S1 | First install, Nodes screen: **K**, **E**, a passphrase twice. | `Export written: .../bristle-core-export.json (n nodes)`. `grep -c BRISTLE ~/wdg-bristle/bristle-core-export.json` says 0: the key is not readable in it. |
| S2 | Copy the file into the second install's folder. There: **K**, **M**, a wrong passphrase. | `That did not work: wrong passphrase, or the file is damaged`. Nothing changed. |
| S3 | **K**, **M**, the right passphrase. | `Import done: n node(s) added ... RESTART THE GAME`. `grep -c BRISTLE_OWNER_KEY_PREVIOUS secrets.conf` says 1 (if that install had its own key). |
| S4 | Restart the second install with a node on its USB hub. | The node comes online there without adopting. |
| S5 | With the first core running the fleet through its bridge: start the second core with its own bridge. | On the second: rows `BUSY ... another core is running this node`, terminal `Node ... is being run by another core: left alone`. The first core's nodes are not disturbed. Stop the first core: within a minute the second takes the nodes. |

| Step | Result | Notes |
|---|---|---|
| S1 export | | |
| S2 wrong passphrase refused | | |
| S3 import | | |
| S4 nodes work on the second core | | |
| S5 busy fleet is left alone | | |

---

## 10. Checkpoint C: the long outage

**Plug in:** one adopted node on a power bank with an always-on mode, somewhere it hears networks.
Start it in the evening.

| # | Do | Expect |
|---|---|---|
| C1 | With the node online and scanning, quit the game and shut the uConsole down (or drive away). Note the time. On the node's console first: `queue`. | The node keeps scanning: its `holding n finds` grows, `dropped 0`. |
| C2 | Next morning, on its console: `queue` and `status`. **Write both down.** | `holding` in the thousands; `dropped 0` unless it filled (about 38,000 finds); `flash ... KB used`; `since this boot: finds written to flash n times (k KB)`: about 240 an hour. `clock <unix time> (set by the core)`. |
| C3 | Start the uConsole and the game. | The node comes online and hands over the night: "on nodes" starts at C2's number and drains to 0. The messages do not show `dropped on nodes` rising. |
| C4 | Look at the counts. | The finds are counted as `no GPS`, not written: the uConsole was off, so there is no position for those times. That is the rule, not a fault. `received` on the core equals what the node held (no doubles): the node's `queue` now says `holding 0`. |
| C5 | The same test with the game left running and the node carried out of range for an hour, then back. | This time the finds are **written**, at the places the uConsole was at those times (it stood still, so: where it stood). |
| C6 | Pull the node's power for a minute during the outage, then power it again. | It keeps what it had saved (at most the last 15 s are lost). What it scans after the power cut has no clock until it meets the core; when it does, those finds get their times and arrive too. |

| Step | Result | Notes |
|---|---|---|
| C1 node keeps scanning without the core | | |
| C2 `queue` in the morning (holding, dropped, flash used, writes) | | |
| C3 the night is handed over once | | |
| C4 counts add up; nothing doubled | | |
| C5 outage with the game running: finds placed | | |
| C6 power cut during the outage | | |

---

## 11. Checkpoint H: the full drive

**Plug in:** everything as you mean to drive it: the bridge and any wired nodes on the hub, the
rest on car power, GPS, the Alfa, Kismet with `hci1` ticked.

| # | Do | Expect |
|---|---|---|
| H1 | A fresh install from the zip (`unzip -o`, `./install.sh`) over the running one. | Node Memory, the Block list, `bristle.conf`, the owner key, suspects and loot are all still there afterwards. |
| H2 | Start a Kismet wardrive and drive for at least 30 minutes. | Every node online the whole drive; the small map line shows `BRISTLE n/n scan`; no frame stalls in the game. |
| H3 | During the drive: unplug the hub for a minute; restart Kismet once; power-cycle one node. | Everything comes back by itself; nothing is doubled; `dropped on nodes` stays 0. |
| H4 | After the drive: the totals line. | `no GPS` and `dropped` small or 0; "on nodes" 0. |
| H5 | Upload to WiGLE and sync WDGWars. | As before; the nodes' finds are in the same wardriving.csv as Kismet's, each network once. |
| H6 | PLUGINS > Loot: Backup. | The backup includes `loot/bristle/` and `loot/skimmers/`. |

| Step | Result | Notes |
|---|---|---|
| H1 reinstall keeps everything | | |
| H2 the drive | | |
| H3 unplug, Kismet restart, node power cycle | | |
| H4 totals | | |
| H5 uploads | | |
| H6 backup | | |

Numbers I would like back after H: nodes online, finds per minute from the nodes against Kismet's,
"last visit took" from one node, chip temperatures, and the bridge row's rx/tx.
