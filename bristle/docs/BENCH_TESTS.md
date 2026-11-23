# Bristle bench tests

Real boards only. Write results under each checkpoint (date, boards, pass/fail, notes).

## Checkpoint A: node firmware on real boards

Release: `firmware/releases/v1-build3-dev/`. Boards: one XIAO ESP32-C5 and one non-XIAO C5.
Nothing here has run on a board before: every line below is "expected", not "seen".

You need: both boards, two USB-C **data** cables, the PC (for A1), the uConsole and a powered USB
hub (for A2).

### A1. Flash and boot (each board, from the PC)

1. Flash the FULL image at **0x0 with erase** (`docs/FLASHING.md`). Use the folder for the board:
   `xiao_c5` for the XIAO; `devkit_c5` for a C5 board on its native USB port; `devkit_c5_uart` for a
   board whose only USB port is a CH343/UART chip.
2. Open the console at 115200 and press RESET. Expect one line, with no WARNING in it:
   `# Bristle node build 3 (<board>), hw <12 hex>, no owner yet`
   (On a native-USB board RESET drops the port for a moment. Reconnect the terminal; if the line
   went by, type `version`.)
3. Type `selftest`. Expect 10 `PASS` lines (crc16, encode, encode2, plan_hash, plan_short,
   owner_id, node_proof, core_proof, frame, roundtrip) and then `# SELFTEST PASS`.
4. Type `status`. Expect, among the lines:
   - `# flash layout ok (app slot 2560 KB, queue 2944 KB)`
   - `# clock not set (finds are stored by uptime until the core sets it)`
5. Type `queue`. Expect `holding 0 finds` and a flash total of about 2944 KB.
6. Type `identify 5`. The LED should blink for 5 s. If it stays lit when it should be off, the
   polarity in `Boards.h` is backwards for this board: note it.
7. Type `boot` (expect `reads 1`). Hold BOOT and type `boot` again (expect `reads 0`). If it
   doesn't change, the BOOT pin in `Boards.h` is wrong for this board: note it.

### A2. Adopt and scan with the bench tool (uConsole)

Close Watch Dogs Go first, so it isn't holding a serial port.

```
# on the uConsole, in the copied Bristle folder
sudo apt install python3-serial
python3 -I tools/bristle_bench.py
```

The bench tool never writes to a new serial port by itself. It lists each ESP32 port and waits.

1. Plug both boards into the powered hub. Each shows up as
   `port N: /dev/ttyACM… (303a:1001, serial …): new port: not spoken to yet`.
2. If your JanOS board is plugged in too, mark it once: `notnode N`. It is remembered
   (`~/.bristle-bench/nodes.json`) and never spoken to again. Type `ports` to see every port and
   what was decided about it.
3. `probe N` for each Bristle board. Expect `*** new_node: hw … on port N`. Then `adopt N` for
   each. Expect `*** node 1 online` and `*** node 2 online`.
   Watch for a line `node console: Bristle node build 3 …` right after `saying hello`. That is the
   board's boot banner: it would mean opening the port reset the board. It should not happen;
   tell me if it does (and on which board).
4. `start`. Finds stream in (`[1] AP …`, `[2] BT …`). Let it run 5 minutes, then `status`:
   `backlog` near 0, `dropped=0`, `repeats=0`.
5. **Outage, nodes stay powered.** Pull the hub's cable out of the uConsole (the hub keeps the
   boards powered). Wait 2 minutes. Plug it back. Both nodes come back online by themselves and
   their backlog arrives. In the CSV (`~/.bristle-bench/finds-….csv`) the finds from those 2 minutes
   carry times from those 2 minutes, and no (node, seq) pair appears twice.
6. **Reboot mid-scan.** Press RESET on node 2. It comes back with the same node number and keeps
   scanning. `status` a minute later: `dropped=0`.
7. **Finds made before the clock is set.** Type `detach` (the tool leaves, the nodes keep
   scanning). Press RESET on node 1. Wait 1 minute. Start the tool again. Node 1 comes online and
   hands over what it found in that minute. In the CSV those finds carry times spread over that
   minute, not all the moment the tool started. On node 1's console `status` should show
   `clock … (set by the core)`.
8. `stop`, then `quit`. Start the tool again: both nodes come back online without `probe` or
   `adopt`.
9. Plug in a Biscuit or a Piglet. It must not become a node: at most it is listed as a new port.
   If you `probe` it anyway, expect `nothing answered 3 hellos` and it is left alone.
10. On one board's console type `queue`, and write down the `dropped` line.

### Results

| Step | XIAO | Other C5 | Notes |
|---|---|---|---|
| A1 boot banner (build 3, no WARNING) | | | |
| A1 selftest (10 PASS) | | | |
| A1 flash layout ok | | | |
| A1 LED (identify) | | | |
| A1 BOOT pin | | | |
| A2 new ports listed, not touched | | | |
| A2 opening the port does not reset the board | | | |
| A2 JanOS board marked, left alone | | | |
| A2 adopt | | | |
| A2 scan 5 min | | | |
| A2 outage with power kept | | | |
| A2 reboot mid-scan | | | |
| A2 finds from before the clock | | | |
| A2 restart bench, no re-adopt | | | |
| A2 Biscuit/Piglet not a node | | | |

Also useful to send back: one `status` and one `queue` output per board, and the chip temperature
from the bench `status` line after 5 minutes of scanning.

## Checkpoint R: WDG-Bristle installed from the zip

Package: `WDG Main\dist\wdg-bristle.zip` (top folder `wdg-bristle`). Nothing in it has run on the
uConsole. Your current install (the earlier build's folder) is not changed by any step here: it stays as the
rollback.

```
# on the uConsole, with the zip copied to your home folder
cd ~ && sha256sum wdg-bristle.zip
unzip -o wdg-bristle.zip && cd ~/wdg-bristle && ./install.sh
./selftest.sh --tests-only
```

1. `install.sh`, step "2/9 Your data": expect `Found your earlier install: <its folder>`,
   then `copied over:` for loot, maps, secrets.conf and whitelist.json, then
   `Your loot, keys and settings are here now.` Step 8: `removed the old icon: ...` twice.
2. `./selftest.sh --tests-only`: expect `RESULT: PASS`. (The simulation half is skipped on purpose
   in this pass: see BACKTRACK R2.)
3. Desktop: one icon, named **WDG-Bristle** (teal, "WDG BRISTLE"). The old "V3 MAX" icon is gone.
4. Start it from the icon. Window title `WDG-Bristle`. Intro: `by LOCOSP`, then `WDG-Bristle 1.0`.
   Terminal: `[SYS] WDG-Bristle 1.0 (base 0.9.21.3)`.
5. Your data: level and XP as before; old sessions in the loot list; WDGWars pairing still valid
   (PLUGINS > WDGWars: Pair / Status); WiGLE key still accepted (Wars Sync).
6. SYSTEM > Update Game: `Updates: managed manually`, `Source: none (install a new wdg-bristle.zip)`.
   It must not show a GitHub source, and nothing is fetched.
7. SYSTEM > Firmware C5/SOLO with the network off, press R: `Approved C5 package: JanOS 1.7.0 + SOLO`
   and `Source: shipped with this build. Updates: managed manually`. Do not flash: this is a look.
8. PLUGINS > Loot: Backup / Restore: the backups you made with the earlier build are still listed; a new
   backup is named `wdg-bristle-bak-<time>.tar.gz`.
9. As before: a Kismet wardrive (start, finds, stop), GPS: Health shows both receivers, ADS-B,
   a WiGLE upload and a WDGWars sync.

| Step | Result | Notes |
|---|---|---|
| R1 data copied over, old folder untouched | | |
| R2 selftest --tests-only PASS | | |
| R3 one icon | | |
| R4 name on window, intro, version line | | |
| R5 loot, keys, settings all there | | |
| R6 Update Game says managed manually | | |
| R7 firmware window, shipped image, no network | | |
| R8 old backups listed, new name | | |
| R9 Kismet, both GPS, ADS-B, uploads | | |

## Checkpoint B: nodes in WDG-Bristle over USB

Needs checkpoint A first (two boards flashed with build 3 and `selftest` PASS on each). Boards on
the powered hub, hub on the uConsole.

1. Before the first start: `ls -l ~/wdg-bristle/secrets.conf`. Start the game. Terminal:
   `[BRISTLE] A new owner key was added to secrets.conf (it is never shown)`. Quit, then
   `ls -l` again: same owner, mode `-rw-------`, and `grep -c BRISTLE_OWNER_KEY secrets.conf` says 1.
   Every earlier key line is still there.
   (If you adopted the boards with the bench tool at checkpoint A, expect instead
   `Took over the bench tool's key and 2 node(s)`, and the nodes come online by themselves.)
2. PLUGINS > Bristle: Nodes. Each ESP32 port the game does not know shows as a `NEW PORT` row.
   Nothing has been sent to them. Select your JanOS board's row and press **X**: it is marked
   "not a node" for good.
3. Select a Bristle board's `NEW PORT` row, press **A**: it becomes a `NEW` row. Press **A**
   again: `Node 1 online`. Same for the second board.
4. Press **S**. Rows show `scanning`; the totals line counts finds per minute; new networks appear
   in the terminal as `[WiFi] <name> Ch:<n> (node <n>)` and as markers on the map where you were.
5. `tail -n 5 ~/wdg-bristle/loot/<today's session>/wardriving.csv`: rows with real coordinates,
   none at 0,0.
6. Press **S** to stop. With `tie_to_kismet = on` (the default): start a Kismet wardrive, the nodes
   start with it; stop Kismet, the nodes stop.
7. While scanning: pull the hub's cable from the uConsole for a minute and plug it back. Both nodes
   come back online, their backlog arrives, the "on nodes" count goes back to 0.
8. While scanning: press RESET on one node. It comes back with the same number.
9. Plug in a Biscuit or a Piglet. It never becomes a node: at most a `NEW PORT` row. Press **A**
   on it: `did not answer: not a Bristle node`. Then block or mark it: **X**.
10. **I** on a node: its LED blinks 10 s. **P**: the pin cycles and the channel column follows.
    **R** twice: renumber. (**F** twice forgets a node: try it last, then adopt it again.)
11. On the map, press **F8**: the activity line (bottom left) goes away; again: it is back. Quit
    and start the game: it remembered.
12. PLUGINS > Hardware Check: a `Bristle nodes: 2 online of 2` line, and `hci0 ... kept for Bristle`.
    In the Kismet adapter picker tick `hci0`: Hardware Check now shows
    `KEPT FOR BRISTLE BUT KISMET IS SET TO USE IT`. Untick it again.
13. Quit the game mid-scan and start it again: both nodes come back online without adopting, and
    wardriving.csv has no network twice.
14. PLUGINS > Loot: Backup: make a backup; `tar tzf ~/wdg-bristle-bak-*.tar.gz | grep bristle`
    lists `loot/bristle/nodes.json`.

| Step | Result | Notes |
|---|---|---|
| B1 owner key added, secrets.conf intact | | |
| B2 new ports listed, JanOS board marked | | |
| B3 adopt both boards | | |
| B4 scan: rows, terminal, map markers | | |
| B5 wardriving.csv rows with coordinates | | |
| B6 start/stop with Kismet | | |
| B7 unplug / replug | | |
| B8 node reboot mid-scan | | |
| B9 Biscuit / Piglet never a node | | |
| B10 identify, pin, renumber, forget | | |
| B11 HUD toggle remembered | | |
| B12 Hardware Check rows and the hci0 warning | | |
| B13 restart: nodes back, no doubles | | |
| B14 Node Memory inside the loot backup | | |
