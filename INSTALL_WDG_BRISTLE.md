# WDG-Bristle: install guide for the uConsole

Every grey block can be copied and pasted whole into a terminal on the uConsole.
Run everything as your normal user. The installer asks for `sudo` itself.

**What this is.** WDG-Bristle 1.2.1: Watch Dogs Go (base 0.9.21.3) with the Kismet wardriving bridge,
dual-GPS through gpsd with a health screen, a Hardware Check screen, Kismet on two Wi-Fi adapters,
ADS-B tracks + KML, 433 capture to Flipper `.sub`, 433 Hunter, loot backup/restore, session log,
WDGWars pairing + status, an offline-basemap layer, and **Bristle Nodes**: a fleet of ESP32-C5
scanning boards with this uConsole as the core, over USB, an ESP-NOW bridge or Bluetooth, with
firmware updates, nearby alerts and a list of possible skimmers (section 9).

**What has been proven, and what has not.** The build this one is made from passed its self-test
and its installer on the uConsole. WDG-Bristle itself (the rename, the Bristle Nodes plugin and
everything new in 1.1 and 1.2) has only been built and unit-tested on a Windows PC: nothing in it has
run on the uConsole yet, and the node firmware has not run on a board yet. The PC cannot run the shell
scripts at all (it has no bash). Use `./selftest.sh --tests-only` for the unit tests. The bench list that proves it is `bristle/docs/BENCH_TESTS.md`.

**Coming from an earlier install?** Unzip this anywhere and run `./install.sh`. On a first install
it looks for earlier installs in every folder under your home, copies your loot, maps, `secrets.conf`,
`whitelist.json`, firmware cache, Kismet adapter choice and upload record from the one with the
newest data into `~/wdg-bristle`, lists what it found and what it copied, removes the old desktop
icon, and leaves the old folder exactly as it is: that folder is your rollback. Updates are by hand from now on: a new `wdg-bristle.zip`, unzipped over
this folder. SYSTEM > Update Game only says so; it installs nothing.

---

## 1. Copy the zip to the uConsole

Pick ONE of these.

**A. USB stick.** Copy `wdg-bristle.zip` onto the stick on the PC, plug it into the uConsole, then:

```bash
ls /media/$USER/
```

```bash
cp /media/$USER/*/wdg-bristle.zip ~/
```

**B. Over the network (scp), from the PC.** Replace `USER` and `UCONSOLE-IP`:

```powershell
scp "C:\path\to\wdg-bristle.zip" USER@UCONSOLE-IP:~/
```

Check the copy arrived whole. The number must match the SHA256 in the build report:

```bash
sha256sum ~/wdg-bristle.zip
```

## 2. Unzip it where you want the game to live

The zip holds one folder, `wdg-bristle`. The game is installed in place, in that folder.
This example puts it in your home folder:

```bash
cd ~ && unzip -o wdg-bristle.zip && cd ~/wdg-bristle && ls
```

Updating later is the same command: unzip the new zip over the old folder. The zip contains no
`loot/`, `maps/`, `secrets.conf` or `whitelist.json`, so yours are not overwritten.

**First time under the name WDG-Bristle:** run `./install.sh` (step 3) even if the earlier build is
installed. It is what brings your data over and makes the new icon.

## 3. Run the installer

Plug in everything you want detected first (USB GPS dongle, Alfa, AC1200, UB500).

```bash
cd ~/wdg-bristle && chmod +x install.sh && ./install.sh
```

What it does, in order:

1. Prints a **found / not found** table of your hardware (board CM4/CM5, AIO v2 rails, both GPS
   receivers, RTL-SDR, LoRa, Wi-Fi adapters, Bluetooth, ESP32). For this look it switches the AIO
   GPS / SDR / USB rails on and puts them back, and stops gpsd for a few seconds.
2. Keeps your data (`loot/`, `maps/`, `secrets.conf`, `whitelist.json`). On a first install it brings
   your data over from an earlier install instead (see "Bring an earlier install's loot and XP over").
3. Installs missing packages: `kismet gpsd gpsd-clients rtl-sdr rtl-433` and helpers.
4. Runs the game's own `setup.sh`: Python venv in `.venv`, Python packages, dump1090 (built from source if
   missing), HackerGadgets `aiov2_ctl`. The first run takes several minutes.
5. Adds you to the `dialout`, `kismet`, `gpio`... groups that exist.
6. Points **gpsd** at the GPS receivers it actually read NMEA from, with `-n`. The old
   `/etc/default/gpsd` is backed up first as `/etc/default/gpsd.wdg-backup-<date>`.
7. Blacklists the DVB-T TV driver so the RTL-SDR is free for SDR programs.
8. Creates `secrets.conf` from the example only if you have none, writes the launcher and the
   **"Watch Dogs Go - WDG-Bristle"** icon (desktop + applications menu), marked trusted.
9. Prints the next steps.

Options:

| Command | Effect |
|---|---|
| `./install.sh --no-rails` | Do not switch AIO rails during the hardware look |
| `./install.sh --no-gpsd` | Leave `/etc/default/gpsd` alone |
| `./install.sh --no-apt` | Install no system packages |
| `./install.sh --passwordless-launch` | Let the icon start the game without the sudo password |
| `./install.sh --clean` | **Wipe** loot, maps, secrets and whitelist first. Asks you to type `DELETE` |

If it says you were added to new groups, log out and back in once.

## 4. Put your keys in `secrets.conf`

```bash
nano ~/wdg-bristle/secrets.conf
```

Add the lines for the services you use, **without** a leading `#`. Leave out what you do not use.
The values below are placeholders, not keys.

| Line | Where the value comes from | Used for |
|---|---|---|
| `WDG_WIGLE_NAME=AIDxxxxxxxx` | wigle.net > Account > "API Name" | PLUGINS > Kismet: Upload to WiGLE.net |
| `WDG_WIGLE_TOKEN=xxxxxxxx` | wigle.net > Account > "API Token" | same |
| `WARDRIVE_API_KEY=64-hex-characters` | wdgwars.pl profile, or pair in game (below) | Wars Sync, White Rabbit, turf, WDGWars status |
| `STADIA_API_KEY=xxxxxxxx` | stadiamaps.com | SYSTEM > Download Map, Auto Map + Turf |
| `MAPTILER_API_KEY=xxxxxxxx` | maptiler.com (instead of Stadia) | same |
| `WDG_WPASEC_KEY=xxxxxxxx` | wpa-sec.stanev.org | WPA-sec upload / download |
| `WDG_KISMET_API_KEY=xxxxxxxx` | Kismet web UI > Settings > API Keys | optional. A read-only key blocks the channel split |

You can skip `WARDRIVE_API_KEY` and pair instead: in game, **PLUGINS > WDGWars: Pair / Status > P**,
then type the code shown at the portal. The key is saved for you and never displayed.

If the installer brought your data over from an earlier install, your keys are already here. To
copy them from another install by hand instead of retyping them (this only reads the old file; put
that install's folder in place of `OLD-FOLDER`):

```bash
grep -E '^(WDG_|WARDRIVE_|STADIA_|MAPTILER_|TILE_|DISABLED_)' OLD-FOLDER/secrets.conf >> ~/wdg-bristle/secrets.conf && chmod 600 ~/wdg-bristle/secrets.conf
```

## 5. Kismet login (once per machine)

Skip this if Kismet already has a login on this uConsole (an earlier install used the same one).

```bash
kismet
```

Open `http://localhost:2501` in the browser, set a user and password, then press Ctrl+C in the terminal.

## 6. Run the self-test

With the game closed:

```bash
cd ~/wdg-bristle && ./selftest.sh --tests-only
```

`--tests-only` runs the whole test suite and skips the simulation scenarios, which have not been
re-checked for this build yet.

It runs the whole test suite and every simulation scenario on the uConsole itself and prints
**PASS / FAIL per feature**. About 10 minutes. It needs no radio, no GPS fix and no network, and it
works in temporary folders: your loot and keys are not touched.

The result is the same whether gpsd is running or stopped. The tests bring their own stand-in gpsd
and do not contact the real one; you do not need to stop anything first.

| Command | Effect |
|---|---|
| `./selftest.sh --tests-only` | The test suite alone, no simulation scenarios (use this for now) |

<!-- private:end -->

If it ends in `RESULT: FAIL`, send me the log file it names (`~/.watchdogs/selftest/selftest-<time>.log`).
The summary table is at the very top of that file and again at the bottom. Lines saying
`No space left on device` come from the disk-full simulations and are expected; the log keeps the
first 3 per test or scenario and counts the rest.


## 7. Start the game

Click **Watch Dogs Go - WDG-Bristle** on the desktop or in the menu (Games), or:

```bash
~/wdg-bristle/run.sh
```

The game runs as root (radios, GPIO). Its terminal window asks for your sudo password unless sudo is
passwordless or you used `--passwordless-launch`.

## 8. First-run checks

Do these in order. Each one says what you should see.

1. **TAB > PLUGINS > Hardware Check.** Board says `CM5`. Power says `ok`. The four AIO rails show
   ON/OFF and what answered. Your Wi-Fi adapters show `monitor:yes`.
2. **PLUGINS > GPS: Health.** Two rows: `AIO GP-02 (UART)` and your USB dongle. One is marked `>>>`.
   Antenna reads `OK` / `OPEN` / `SHORT`, or `not reported` if the receiver sends no antenna status.
3. **Outside, wait for a fix.** Both rows go to `3D FIX`. The bars show per-satellite SNR.
4. **Unplug the dongle.** Within about 10 s the terminal says it stopped reporting, and the game
   follows the GP-02. Plug it back in: `is reporting again`.
5. **PLUGINS > Kismet: Self-Test**, then **Kismet: Configure & Start.** Tick the Alfa (and the AC1200
   if you use it). With two Wi-Fi adapters the picker shows `Channel split ON`. Start.
6. **Watch the terminal** for `Channels split between the adapters` (two adapters), for
   `Two GPS receivers: Kismet reads only the one in use`, and for
   `Kismet running. Press K anywhere to stop and turn monitor mode off`. While Kismet captures, the
   map shows a small `K: stop Kismet` box at its left edge.
7. **Drive or walk a few minutes**, then press **K** on the map to stop. The game must not freeze.
   The terminal lists which Wi-Fi cards went `Back in managed mode` and which were
   `Left as they were`. A card it could not switch back is named with `STILL IN MONITOR MODE` and
   the command to do it by hand.
8. **PLUGINS > Kismet: Upload to WiGLE.net.** `Uploaded to WiGLE.net`; check your wigle.net uploads.
9. **SYSTEM > SDR on**, then **ADDONS > ADS-B Radar**; later **PLUGINS > ADS-B: Tracks & Coverage > E**
   writes `adsb_tracks.kml`. Stop ADS-B, then try **PLUGINS > 433: Hunter > S** with a remote.
10. **PLUGINS > Loot: Backup / Restore > L.** A backup appears in your home folder.

---

## 9. Bristle Nodes (ESP32-C5 scanning boards)

Everything for the nodes is in the `bristle/` folder inside this install: the firmware images
(`bristle/firmware/releases/`) and the guides (`bristle/docs/`). Plug the boards into a **powered**
USB hub on the uConsole (never power them from the AIO board).

1. Start the game. The first time, it adds one line to `secrets.conf`: `BRISTLE_OWNER_KEY=...`.
   That key is what your nodes answer to. It is never shown. It is inside every loot backup.
2. **PLUGINS > Bristle: Nodes.** A board running Bristle firmware announces itself and shows as
   `NEW`. Any other ESP32 board shows as `NEW PORT` and nothing is sent to it: press **W** to write
   Bristle firmware to a blank board, or **X** if it is not a node (your JanOS board): it is never
   spoken to again.
3. Press **A** on the `NEW` row to adopt the board. It gets a number and keeps it for good.
4. Press **S** to start scanning, **S** again to stop. With `tie_to_kismet = on` the nodes start
   and stop with a Kismet wardrive. Finds go into the same `wardriving.csv`, XP and map as
   everything else, placed where the uConsole was when the node saw them.
5. **F8** on the map switches the small Bristle line (bottom left) on and off.

More nodes than the hub has ports, or nodes around the car: make one board the bridge (**G** on the
Nodes screen) and power the others from anything. Nodes out of the bridge's reach link over
Bluetooth by themselves. **U** updates a node's firmware over whatever link it is on.

Two more screens: **PLUGINS > Bristle: Alerts** (what the radios noticed, and the on/off switch
for each kind) and **PLUGINS > Skimmer suspects** (possible card skimmers: listening only; press
**?** there for what it can and cannot find).

Settings: copy `bristle.conf.example` to `bristle.conf` and edit it. Node Memory, the Block list
and the GPS track are in `loot/bristle/`, suspects in `loot/skimmers/`, so the installer keeps them
and loot backups include them. `bristle.conf` is kept by the installer too.

The guides, in `bristle/docs/`: `INSTALL.md` (every screen and key), `HARDWARE.md` (hubs, power,
the bridge), `FLASHING.md`, `BOARDS.md`, `PROTOCOL.md`, `SIGNATURES.md`, and `BENCH_TESTS.md`, the
one list of bench steps (checkpoints A to H) with what to plug in and what to expect.

## Troubleshooting

### GPS not found

```bash
systemctl status gpsd --no-pager; cat /etc/default/gpsd; gpspipe -w -n 10
```

- `DEVICES=""` or a receiver missing: plug the dongle in and run `./install.sh` again. The installer
  only lists receivers it really read NMEA from.
- The GP-02 is missing: its rail (GPIO 27) is off until the game starts, and the installer switches it
  on only while it looks. If the table said `no NMEA`, check the antenna and that the UART is not a
  serial console: `grep -o 'console=[^ ]*' /proc/cmdline`. If it lists `serial0` or `ttyAMA0`, turn
  the serial console off (`sudo raspi-config` > Interface Options > Serial Port: login shell **No**,
  hardware **Yes**), reboot, and run `./install.sh` again.
- In game it says `gpsd is not running; checking for it every 5 s`: start it with
  `sudo systemctl start gpsd.socket gpsd`. The game connects by itself.
- Antenna `SHORT` on the Health screen: power the GPS rail off (SYSTEM > GPS) and check the cable now.
- To let the game read a serial GPS itself, without gpsd, start it with
  `WDG_GPS_DEVICE=/dev/ttyAMA0 ./run.sh`. Then stop gpsd first, or both will read the same port.

### Kismet sees no adapter

- Hardware Check shows `monitor:NO` or `MONITOR DRIVER NOT INSTALLED` for the AC1200: it needs a
  monitor-capable driver (`rtl8812au` / `88XXau`, or the kernel's `rtw88_8812au`). `iw dev` must list it.
- `AC1200 on USB but NO INTERFACE`: no driver is bound at all. Same fix.
- The AC1200 is on the AIO's internal USB-C port and missing: that port only has power while the USB
  rail (GPIO 23) is on. Opening the Kismet picker switches it on and rescans by itself after a few
  seconds; **R** rescans by hand.
- **HackerGadgets published a statement about an AIO v2 internal USB-C power-supply issue. Check your
  board against that statement before you run a Wi-Fi card from that port.** I have not read the
  statement and this build does nothing to detect affected boards.
- `Kismet refused the login (HTTP 401)`: set Kismet's login (step 5) or `WDG_KISMET_API_KEY`.
- `Channel split not applied (HTTP 403...)`: you use a read-only API key. Capture still works; both
  adapters hop every channel.
- An adapter stays in monitor mode after a crash: `sudo iw dev wlan1 set type managed`, then
  `sudo nmcli device set wlan1 managed yes`.

### SDR busy

- One RTL-SDR does one job. Stop ADS-B / 433 Scanner / 433 Capture / Emergency Radio before starting
  another, or before **433: Hunter**. Hardware Check shows who has it.
- `usb_claim_interface error -6` outside the game: another program holds it:
  `sudo systemctl stop dump1090-mutability dump1090-fa 2>/dev/null; pgrep -a 'dump1090|rtl_'`.
- `rtl_test` finds nothing: SDR rail off (SYSTEM > SDR), or the TV driver grabbed it. Check
  `lsmod | grep dvb_usb_rtl28xxu`; if it prints a line, reboot once (the blacklist applies at boot).

### Under-voltage

- A blinking `UV` on the top HUD, or `UNDER-VOLTAGE NOW` on Hardware Check: the supply is sagging.
  Check `vcgencmd get_throttled` (anything but `throttled=0x0` means it happened).
- Radios are the usual cause: two Wi-Fi adapters + SDR + LoRa at once. Use fresh cells, a short thick
  USB cable, and switch off rails you are not using (SYSTEM > LoRa / SDR).

### AIO rail off

```bash
for p in 27 16 7 23; do pinctrl get $p; done
```

`hi` = on. GPS 27, LoRa 16, SDR 7, internal USB 23. They are off after boot. The game switches GPS on
at start when gpsd owns the GP-02, and the others from SYSTEM. Hardware Check can toggle GPS / SDR /
USB with **G / S / U**. By hand: `sudo pinctrl set 27 op dh` (on), `sudo pinctrl set 27 op dl` (off).

### Bring an earlier install's loot and XP over (copy, never move)

**The installer does this by itself on a first install** (a folder with no `loot/` and no
`secrets.conf` yet). It looks in every folder under your home, up to four folders deep, for a folder
with the name of an earlier install that holds a `loot/` folder or a `secrets.conf`, and it also looks
next to this folder. It prints every one it finds, with the date of its newest data, copies from the
one with the newest data, and prints each thing it copied:

- `loot/`, `maps/`, `secrets.conf`, `whitelist.json`, `firmware_cache/`, `kismet_adapters.json` and the
  two upload-record files in `plugins/`.
- The old folder is only read. Nothing in it is moved, changed or deleted: it is your rollback.
- Something that is already in this folder is never overwritten.
- The same list is kept in `copied-from-earlier-install.txt` in this folder.

If it found more than one, the others are listed as "Not used". Data from two installs is never mixed.
To start from a different one, or from a folder with any other name (a copy on a USB stick, say),
unzip a fresh `wdg-bristle` folder and name it:

```bash
cd ~/wdg-bristle && ./install.sh --from=OLD-FOLDER
```

**By hand, through a backup file**, if you would rather choose what comes over. Close both games.
Make one archive of the old install's data (this reads the old folder and writes only into your home
folder; put that install's folder in place of `OLD-FOLDER`):

```bash
cd OLD-FOLDER && sudo tar czf ~/wdg-old-backup-$(date +%F_%H-%M-%S).tar.gz $(ls -d loot whitelist.json secrets.conf 2>/dev/null) && sudo chown $USER ~/wdg-old-backup-*.tar.gz && chmod 600 ~/wdg-old-backup-*.tar.gz
```

Then in WDG-Bristle: **PLUGINS > Loot: Backup / Restore**, select the `wdg-old-backup-...` file, press
**R**, check the XP and session count it shows, press **Y**, quit, start again. The data WDG-Bristle had
before is kept as `loot.pre-restore-<time>` (the newest 2 such copies are kept; older ones are deleted
at a restore). The archive contains the old `secrets.conf`, so it replaces the keys here with the old
ones.

`~/.watchdogs` (radio and mesh settings, logs) is shared by every build already.

### Roll back to the earlier install

The earlier install was not touched. Click its own icon, or run its own `run.sh` from its folder.

One thing changed system-wide: gpsd now serves **two** receivers. A build from before the two-receiver
support reads gpsd but does not tell receivers apart, so with both attached its position can jump
between them. For such a build, either unplug one receiver, or put the old gpsd settings back:

```bash
ls /etc/default/gpsd.wdg-backup-*
```

```bash
sudo cp /etc/default/gpsd.wdg-backup-REPLACE-WITH-THE-DATE /etc/default/gpsd && sudo systemctl restart gpsd.socket gpsd
```

Running `./install.sh` in the WDG-Bristle folder sets gpsd up for two receivers again.
### Remove WDG-Bristle

```bash
cd ~/wdg-bristle && ./uninstall.sh
```

Removes the icon and menu entry. Your loot stays. `--venv` also removes `.venv`; `--purge` deletes
loot, maps and keys after you type `DELETE`.

---

## Notes

- **Updates are by hand.** SYSTEM > Update Game installs nothing in this build: it says so and stops.
  Update by unzipping a new `wdg-bristle.zip` over this folder (or `git pull`), then `./install.sh`.
- **Offline world basemap.** The drawing code is in, the map data is not: WDGoNG's `basemap.mbtiles`
  (about 29 MB) was not in the WDGoNG archive this build was made from. If you have a WDGoNG install,
  copy `wdgong_ui/assets/map/basemap.mbtiles` to `~/wdg-bristle/maps/basemap.mbtiles` and restart.
  Without it the coastline is drawn as before. It has only been drawn with test tiles so far.
- **ESP32-C5 (the game's own companion board, not a Bristle node).** Plug it in and Hardware Check
  shows it. The game's Wi-Fi/BLE Sniff and WARDRIVING SOLO need JanOS with the SOLO interface on it.
  Nothing is ever flashed by itself: SYSTEM > Firmware writes only when you start it there. If a write
  is cut off, that screen says what state the board is in and offers to finish it (**F**).
- **Not built** (ask if you want them): Cluster Wardrive (needs Marauder V8 nodes, a fleet key and
  newer bridge firmware), the EXTRAS hub and sandbox, host Wi-Fi scanning (`host_wifi.py`: Kismet
  owns your adapters), `radio_arbiter` / `radio_detect`.
