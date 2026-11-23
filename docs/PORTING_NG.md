# Porting Bristle to another base (raylib / WDGoNG)

Kept current with the code. Last update: WDG-Bristle 1.2.1.

Bristle is built so that moving it to a different game base means rewriting three thin files and
a handful of small edits, not the logic. This page lists every place Bristle touches the drawing library
(pyxel) or the WDG app, and what each needs on a raylib-based base such as WDGoNG.

**Licences.** Bristle's code is MIT (`LICENSE-HARDSCRUBBIN`). WDGoNG is under the PolyForm
Noncommercial licence. Nothing from WDGoNG is copied into Bristle, and a port must not copy any
either: write the three thin files against the new base's own public interfaces. The files this
build does carry from WDGoNG are LOCOSP's, unchanged, in `plugins/_ng/` with their notice and
licence text; Bristle does not import them.

## The layers

| Layer | Files | Imports pyxel? | Touches the app? | In a port |
|---|---|---|---|---|
| Core library | `plugins/_bristle/`: `codec`, `crypto`, `proto`, `plan`, `registry`, `delivery`, `session`, `fleet`, `gpsplace`, `espnow`, `link_usb`, `link_espnow`, `link_ble`, `ota`, `nearby`, `alerts`, `skimmer`, `second_core`, `signatures.yaml` | no | no | unchanged (it is copied from the Bristle Nodes folder by `tools/sync_wdg.py`) |
| WDG-side logic | `service`, `ingest`, `nearby_engine`, `flasher`, `kismet_tap`, `hunt_scan`, `settings`, `wdg_ports`, `mapfocus` (1.2: the zoom and the confidence radius for "open a suspect on the map", as plain numbers) | no | no (`flasher` imports the game's `watchdogs.esp32_flash.run_esptool` if it is there and falls back to plain `subprocess`) | unchanged, except the two notes under "Other ties" |
| Host adapter | `host.py` | one call (`frame_count`, for a map marker) | **yes: the only file that does** | **rewrite** |
| Screens | `screen.py`, `screen_more.py` | only through the `pyxel` object handed to their draw functions | no | **rewrite the draw functions**; the text-building functions stay |
| Plugin | `plugins/bristle_core.py` | yes: keys, sounds, drawing calls | through `host` and the plugin base class | **rewrite** |

A unit test (`tests/test_bristle_more.py`, `test_logic_modules_never_import_the_drawing_library_or_the_game`)
fails if a logic file imports pyxel, imports from `watchdogs`, or reaches for `self.app`.

## Every pyxel call site

### `plugins/bristle_core.py`

| Call | Where | What it is for | On raylib |
|---|---|---|---|
| `pyxel.btnp(key)` and `pyxel.btnp(key, hold, repeat)` | `on_update`, `_keys`, `_dialog_keys`, `_alert_keys`, `_suspect_keys`, `_hunt_keys` | key presses; the three-argument form repeats while UP/DOWN is held | `IsKeyPressed(key)`; for repeat, `IsKeyPressedRepeat(key)` or a timer |
| Key names: `KEY_A B E F G H I K M N O P R S U W X`, `KEY_UP DOWN LEFT RIGHT`, `KEY_RETURN`, `KEY_ESCAPE`, `KEY_SLASH`, `KEY_F1`, and the HUD key (`KEY_F8` by default, looked up by name from `bristle.conf`) | the same functions | which key does what | the base's key constants; keep the name lookup for the HUD key |
| `pyxel.frame_count` | `_close`, `_dialog_keys`, `_hunt_keys` | written to `app._esc_consumed_frame` so the game does not also act on the ESC that closed the screen | whatever the base uses to swallow a key press for one frame |
| `pyxel.sounds[slot].set(notes, tone, volume, effect, speed)` and `pyxel.play(3, slot, loop=False)` | `_sound` | five short synthesised sounds (`notice`, `warning`, `follow`, `skimmer`, `beep`) on sound slots 52 to 55, channel 3 | load or synthesise five short sounds once; `PlaySound`. The names and when they play do not change |
| `pyxel.rect`, `pyxel.text` | `draw` (only the "Bristle did not start" screen) | | as below |
| `pyxel.btnp(KEY_ESCAPE)`, `btnp(KEY_UP / DOWN / LEFT / RIGHT)` | `_focus_keys` (1.2) | while the map is held on a suspect: ESC or an arrow key lets it follow the player again | the same keys on the new base |

### `plugins/_bristle/screen.py` and `screen_more.py`

Every draw function takes the drawing library as its first argument (`pyxel`) and uses four calls:

| Call | Used for | On raylib |
|---|---|---|
| `text(x, y, string, colour)` | all text, in the game's 5-pixel-wide font | `DrawTextEx` with a fixed-width font; the layout assumes 5 px per character at 640x360 (126 characters across) |
| `rect(x, y, w, h, colour)` | backgrounds, the selection bar, the hunt signal meter | `DrawRectangle` |
| `rectb(x, y, w, h, colour)` | box outlines (HUD line, popup, dialogs) | `DrawRectangleLines` |
| `line(x1, y1, x2, y2, colour)` | rules under headers | `DrawLine` |

Colours are pyxel palette numbers, named once at the top of `screen.py` (`C_BG`, `C_BAR`,
`C_TITLE`, `C_TEXT`, `C_DIM`, `C_OK`, `C_WARN`, `C_ERR`, `C_BORDER`): map those nine names to the
new base's colours.

`screen_more.draw_focus()` (1.2) draws a suspect opened on the map: the confidence radius as line
pieces it is handed (already cut to the map area by `mapfocus.segments`), a small cross on the spot,
and one line of text bottom right. It uses the same four calls.

`screen_more.typed()` reads `pyxel.input_text` (the characters typed this frame) and
`btnp(KEY_BACKSPACE, hold, repeat)` for the one-line text input (passphrase, notes, place):
`GetCharPressed()` in a loop on raylib.

The functions that build what is shown (`node_rows`, `totals_text`, `coverage`, `hud_lines`,
`links_text`, `job_text`, `suspect_rows`, `suspect_detail`, `alert_switch_rows`, `alert_log_rows`,
`hunt_view`, `dialog_lines`) return plain strings and numbers and are unit-tested. A port keeps them.

### `plugins/_bristle/host.py`

| Call | Where | On raylib |
|---|---|---|
| `pyxel.frame_count` | `marker`, to give a new map marker its spawn frame | the base's frame counter |

## Every WDG app hook (all in `host.py` unless noted)

| Hook on the app | Used by | What Bristle needs from it | On an NG base |
|---|---|---|---|
| `app.gps.wardrive_sample()` | `gps_sample` | the fresh, measured position: lat, lon, altitude, hdop and the monotonic time it was measured (`observed_at`) | the same five values from the base's GPS source. The measured-at time must be on the monotonic clock |
| `app.loot` | `has_loot` | whether loot can be written | |
| `app.loot.session_path` | `session_dir`, `wardriving_csv` | the current session folder (for the alert log and the WiGLE queue) | |
| `app.loot.save_wardriving_network(net, observed_at=, position=, positioned_only=, append_new=)` | `save_wifi` | write one Wi-Fi find at a given time and position, with the base's own dedup | the base's writer for a positioned Wi-Fi find |
| `watchdogs.app_state.Network` | `save_wifi` | the record type that writer takes | the base's record type |
| `app.loot.save_wardriving_bt(mac, rssi, name, observed_at=, position=, positioned_only=, append_new=)` | `save_bt` | the same for a Bluetooth find | |
| `app.loot.save_bt_device(mac, rssi, name, False, False, append_new=True)` | `save_bt_device` | the known-Bluetooth-devices list | |
| `app._known_wifi`, `app._known_ble` (sets), `app.gain_xp(n)` | `credit` | XP: 15 / 10 for a device new to the game, 1 for a known one | the base's XP call and "seen before" test |
| `app.state.networks` (list) | `list_network` | the SCAN list | |
| `watchdogs.app.WifiNetwork`, `watchdogs.app.BleDevice`, `app.wifi_networks`, `app.ble_devices` | `marker` | a marker on the map at a position | the base's map-marker call |
| `app._record_scan_map_observation(kind, id, label, rssi, channel, position=)` | `marker` (1.2) | the game draws a marker only for what it has on record as heard this session at a position; this files it | whatever makes a marker visible on the base |
| `app.proj` (`zoom`, `geo_to_screen(lat, lon)`), `watchdogs.app.ZOOM_LEVELS`, `W`, `MAP_H` | `map_view` (1.2) | the zoom levels, the size of the map and its own position-to-screen conversion | the base's map view |
| `app._map_focus` (a `(lat, lon)` or `None`), `app.proj.zoom` | `map_focus`, `map_release` (1.2) | hold the map on a spot at a zoom, and let it follow the player again at the zoom it had | the base's camera |
| `app.msg(text, colour)` | `message` | a short message on the HUD | |
| `app.player_lat`, `app.player_lon` | `position` | where the player is, when there is no fresh fix | |
| `app._muted` | `muted` | the game's mute switch | |
| `app.serial.device` | `game_port` | the serial port the game's own ESP32 link holds, so Bristle never opens it | |
| `app._plugins` | `kismet` | finding the Kismet plugin | the base's plugin registry |
| Kismet plugin: `kismet_active()`, `queue_wigle_upload(path)` | `kismet_active`, `queue_wigle` | whether Kismet is capturing; the public "queue this file for WiGLE" | the same two calls on the base's Kismet integration |
| `app.state.wardriving_running` | `set_wardriving` | the game's "a wardrive is on" flag, set while nodes scan | |
| `app._esc_consumed_frame` | `plugins/bristle_core.py` | swallow the ESC that closed a Bristle screen | |
| `app._term_add(text, raw=True)` | `PluginBase.term`, used by `plugins/bristle_core.py` | a line in the game's terminal | |
| `app.loot._base` | `plugins/bristle_core.py`, `on_load` | the loot folder (falls back to `<install>/loot`) | |

### What the base's plugin system must offer (`plugins/bristle_core.py`)

| Thing | Used for |
|---|---|
| `PluginBase` with `on_load(app)`, `on_unload()`, `on_update()` every frame, `draw(x, y, w, h)`, `menu_items()` | the plugin itself |
| `BACKGROUND_TICK = True` honoured | `on_update` runs with the screen closed: nodes keep delivering, alerts keep coming |
| `has_overlay` and an `overlay_active` property | a full-screen Bristle screen |
| `draw_map_overlay(pyxel, w, hud_top, term_y)` called while the map is drawn | the small activity line (bottom left, above the terminal) and the alert popup (under the top bar) |
| Three menu entries: `Bristle: Nodes`, `Bristle: Alerts`, `Skimmer suspects` | |

## The `[bristle]` edits in the game's own files

Each is tagged `[bristle]` in a comment. A port needs the same things from the new base.

| File | Edit | Why | On an NG base |
|---|---|---|---|
| `watchdogs/serial_manager.py`, `detect_esp32_port()` | skip ports in `wdg_ports.node_devices()` and ports where `wdg_ports.is_bristle_board()` is true | the game must never take a Bristle board as its own ESP32 | the same two filters wherever the base picks its ESP32 port |
| `plugins/kismet_wardrive.py`, `poll_networks()` | one line: `_bristle_tap.bt_device(d, mac, name, rssi)` for each Bluetooth device | nearby alerts and skimmer suspects read Kismet's Bluetooth devices, with Class of Device | call `kismet_tap.bt_device` with Kismet's device record from the base's Kismet poll |
| `plugins/kismet_wardrive.py`, `_poll_worker()` | one line: `_bristle_tap.poll(self.engine, KISMET_URL)` | Kismet's alerts and source list, fetched on Kismet's own thread | call `kismet_tap.poll` with an object that has `_get`, `_post`, `_auth`, `datasources()` and `running` |
| `plugins/kismet_wardrive.py` | `queue_wigle_upload()`, a public name for the queue function | Bristle queues a nodes-only drive for WiGLE without reaching into private names | |
| `plugins/hardware_check.py` | one line for the fleet, and the "kept for Bristle" row for the Bluetooth adapter | | optional |
| `watchdogs/app.py`, `update()` (1.2) | the camera aims at `_map_focus` when a plugin has set it, at the player otherwise | "open a suspect on the map" has to hold the map somewhere the player is not | the base's camera needs a "look here" hook |
| `watchdogs/app.py`, `_draw_player()` (1.2) | while `_map_focus` is set the player figure is drawn at the player's position (not at all when that is off the map) instead of in the middle of the screen | the figure would otherwise stand on the suspect | a base that draws the player by position needs nothing |
| `plugins/kismet_wardrive.py`, `datasources()` (1.2) | a source the plugin holds on one channel carries `lock` | the node planner counts a locked channel as covered by Kismet (`plan.kismet_heavy_channels`) | pass `lock`, or `hopping` False with `channel`, in the source list handed to `kismet_tap.poll` |

### Other `[bristle]` edits in 1.2 and 1.2.1 that are not part of Bristle Nodes

They are the game's own features, tagged the same way so they can be found. A port of the nodes
does not need them.

| File | What | Logic lives in |
|---|---|---|
| `plugins/kismet_wardrive.py` | the per-card channel lock (picker keys, the lock in the launch, putting it in force on the poll thread) and the per-card plan lines in the picker | `plugins/_kismet_extras.py` (`plan_cards`, `resolve_locks`, `picker_plan`, `parse_iw_channels`: no pyxel, no I/O) |
| `plugins/_w5_hardware.py` | `phy_info()`: the text of `iw phy` for one card, read only | |
| `plugins/kismet_wardrive.py` (1.2.1) | the K kill switch is marked: `draw_map_overlay()` draws `K: stop Kismet` while Kismet captures (its only pyxel calls: one `rect`, one `rectb`, one `text`, through the object the game passes in), one terminal line when a drive starts, the menu label, and the list of cards at stop in `_restore_adapters` | `plugins/_kismet_extras.py`, `stop_report()` (the wording: no pyxel, no I/O) |
| `plugins/_w5_hardware.py` (1.2.1) | `is_monitor()`: whether a card is in monitor mode, from `iw dev <card> info`, read only | |
| `tests/test_v3_sim.py` (1.2.1) | the list of simulated features gains `bristle` | `tools/sim/fake_bristle.py`, `tools/sim/scenarios_bristle.py` (stand-ins: they import the core library and the service, never pyxel or the game) |
| `watchdogs/app.py`, the Firmware screen and `_flash_do` / `_run_flash` | the note on disk around a firmware write, **F** for an install that did not finish, the page that says what state the board is in | `watchdogs/c5_resume.py` (no pyxel, no game object) |
| `install.sh`, `tools/install_helper.py`, `watchdogs/legacy_names.py` | the first install finds earlier installs under the home folder and copies the player's data | `tools/install_helper.py` (`find_old_installs`, `copy_data`, `bring_data`) |

Also Bristle's, outside `plugins/`: `bristle.conf.example`, the `BRISTLE_OWNER_KEY` placeholder in
`secrets.conf.example`, and the test files `tests/test_bristle_*.py`.

## Other ties to know about

- `flasher._run` uses `watchdogs.esp32_flash.run_esptool` when it can be imported (a supervised
  esptool run with timeouts) and plain `subprocess` otherwise. No change needed for a port.
- `wdg_ports.node_devices()` reads `loot/bristle/nodes.json` relative to the install folder,
  because it runs before any plugin is loaded. If the new base keeps loot elsewhere, pass `root`.
- Files Bristle keeps: `loot/bristle/` (Node Memory, ack points, frame counters, the GPS track, the
  core id), `loot/skimmers/`, `bristle_alerts.csv` in the session folder, `bristle.conf` and one
  line in `secrets.conf` in the install folder. A base with a different data layout only has to
  hand `BristleService` its install folder and its loot folder.
- Threads: Bristle starts its own (`bristle-service`, `bristle-usb-scan`, one `bristle-usb-<port>`
  per wired node, `bristle-ble`, and short-lived ones for a flash or a hunt). The base only has to
  call `on_update()` on its main thread.

## Porting checklist

1. Copy `plugins/_bristle/` and the tests. Run the tests: everything that does not need the game
   must pass before any screen exists.
2. Write `host.py` for the new base (the table above is its whole contract).
3. Write the draw functions of `screen.py` and `screen_more.py` against the new drawing calls.
4. Write the plugin file: keys, the five sounds, the three menu entries, the map overlay.
5. Make the edits in the base's own files, tagged `[bristle]`.
6. Run the bench list (`bristle/docs/BENCH_TESTS.md`).
