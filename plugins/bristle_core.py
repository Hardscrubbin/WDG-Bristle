"""[bristle] PLUGINS > Bristle: Nodes, Bristle: Alerts, Skimmer suspects.

A fleet of ESP32-C5 scanning nodes, with this uConsole as the core. Nodes scan Wi-Fi and Bluetooth
on the channels this core gives them and hand their finds over with a time stamp, over USB, over
the ESP-NOW bridge or over Bluetooth LE. The game places each find with its own GPS and writes it
through the same loot writers as every other source (wardriving.csv, bt_devices.csv, XP, map
markers). What the radios hear also feeds the nearby alerts and the list of possible skimmers.

A board joins only when you press Adopt. The core speaks only to boards that speak the Bristle
protocol, pass the owner check and are in Node Memory.

This file is thin on purpose: keys, drawing calls and sounds. The logic is in plugins/_bristle/
(none of it imports pyxel), and everything that touches the game goes through _bristle/host.py.
Threads: all serial, radio, flashing, file and network work is on worker threads
(plugins/_bristle/service.py). This file runs on the game thread and only reads values, drains
queues within a time budget, asks the worker to do things (svc.do) and draws.
"""
import collections
import time
from pathlib import Path

from plugins.plugin_base import PluginBase, PluginMenuItem

FONT_W = 5
CONFIRM_SECONDS = 3.0
MESSAGES_PER_FRAME = 3
NODES, ALERTS, SUSPECTS, HUNT = "nodes", "alerts", "suspects", "hunt"
# name -> (pyxel sound slot, notes, tone, volume, effect, speed). Slots 52-55 are not used by the game.
SOUNDS = {"notice": (52, "c3e3", "t", "4", "n", 14), "warning": (53, "a3d3a3d3", "p", "5", "n", 12),
          "follow": (53, "e3c3e3c3e3", "p", "5", "v", 12), "skimmer": (54, "g3g3c4c4", "s", "5", "n", 10),
          "beep": (55, "c4", "t", "4", "f", 6)}


class BristleNodes(PluginBase):
    NAME = "Bristle: Nodes"
    VERSION = "1.1"
    AUTHOR = "WDG-Bristle"
    BACKGROUND_TICK = True          # nodes keep delivering while the screen is closed

    def __init__(self):
        super().__init__()
        self.has_overlay = True
        self._overlay_active = False
        self._view = NODES
        self.service = None
        self.ingest = None
        self.host = None
        self.error = ""
        self._selected = 0
        self._rows = []
        self._rows_at = 0.0
        self._status = None
        self._confirm = None           # (what, id, until)
        self._dialog = None
        self._kismet_was = False
        self._marked_wardriving = False
        self._wrote_at_start = 0
        self._popup = None             # (alert, until)
        self._alert_selected = 0
        self._suspect_rows = []
        self._suspect_selected = 0
        self._suspect_help = False
        self._hunt_mac = ""
        self._last_beep = 0.0
        self._sounds_ready = set()

    # ------------------------------------------------------------------ plugin interface
    def menu_items(self):
        return [PluginMenuItem("n", "Bristle: Nodes", "open_overlay"),
                PluginMenuItem("e", "Bristle: Alerts", "open_alerts"),
                PluginMenuItem("m", "Skimmer suspects", "open_suspects")]

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    def _show(self, view) -> None:
        self._overlay_active = True
        self._view = HUNT if view == SUSPECTS and self._hunt_mac else view
        self._rows_at = 0.0
        self._status = self._dialog = None

    def open_overlay(self):
        self._show(NODES)

    def open_alerts(self):
        self._show(ALERTS)

    def open_suspects(self):
        self._show(SUSPECTS)

    def on_load(self, app) -> None:
        self.app = app
        try:
            from plugins._bristle import host, ingest, service
            self.host = host.WdgHost(app)
            root = Path(__file__).resolve().parent.parent
            loot = getattr(app, "loot", None)
            loot_base = getattr(loot, "_base", None) or (root / "loot")
            self.service = service.BristleService(root, loot_base, self._on_find, held_port=self.host.game_port)
            self.ingest = ingest.Ingest(self.host, self.service.fleet.delivery, say=self.term, service=self.service)
            self.service.start()
            if self.service.key_source == "new":
                self.term("[BRISTLE] A new owner key was added to secrets.conf (it is never shown)")
        except Exception as exc:
            # Bristle is off; the game goes on. Nothing was half-started that could touch a port.
            self.service = self.ingest = None
            self.error = str(exc) or type(exc).__name__
            self.term("[BRISTLE] Not started: %s" % self.error)

    def on_unload(self) -> None:
        if self.service is None:
            return
        try:
            if self.ingest is not None:
                self.ingest.drain(final=True)      # nothing stays half-handled
            self.service.stop()
        except Exception:
            pass

    def _on_find(self, find) -> None:              # called on a link thread
        if self.ingest is not None:
            self.ingest.add(find)

    # ------------------------------------------------------------------ every frame
    def on_update(self) -> None:
        svc = self.service
        if svc is None:
            if self._overlay_active:
                import pyxel
                if pyxel.btnp(pyxel.KEY_ESCAPE):
                    self._close(pyxel)
            return
        import pyxel
        self.ingest.feed_gps()
        self.ingest.drain()
        svc.nearby.session_dir = self.host.session_dir()
        for _ in range(MESSAGES_PER_FRAME):       # service messages -> the game's terminal
            if not svc.terminal:
                break
            self.term(svc.terminal.popleft())
        self._follow_kismet()
        self._mark_wardriving()
        self._alerts(pyxel)
        self._hunt_beep(pyxel)
        key = getattr(pyxel, "KEY_" + str(svc.conf.get("hud_key", "F8")).upper(), None)
        if key is not None and pyxel.btnp(key):
            self._toggle_hud()
        if self._overlay_active:
            if self._dialog is not None:
                self._dialog_keys(pyxel)
            elif self._view == ALERTS:
                self._alert_keys(pyxel)
            elif self._view == SUSPECTS:
                self._suspect_keys(pyxel)
            elif self._view == HUNT:
                self._hunt_keys(pyxel)
            else:
                self._keys(pyxel)

    def _close(self, pyxel) -> None:
        self._overlay_active = False
        self._dialog = None
        self.app._esc_consumed_frame = pyxel.frame_count

    def _follow_kismet(self) -> None:
        svc = self.service
        active = self.host.kismet_active(self)
        if active != self._kismet_was:
            self._kismet_was = active
            from plugins._bristle import settings
            if settings.on(svc.conf, "tie_to_kismet"):
                if active and not svc.fleet.run_mask:
                    self._start(by_tie=True)
                elif not active and svc.started_by_tie:
                    self._stop()

    def _mark_wardriving(self) -> None:
        """While the nodes scan, the game counts as wardriving (as it does for a Kismet drive)."""
        scanning = bool(self.service.fleet.run_mask)
        if scanning:
            self.host.set_wardriving(True)
            self._marked_wardriving = True
        elif self._marked_wardriving:
            self._marked_wardriving = False
            if not self._kismet_was:               # Kismet still capturing: the flag stays its own
                self.host.set_wardriving(False)

    def _start(self, by_tie=False) -> None:
        self._wrote_at_start = self.ingest.counts["written_wifi"] + self.ingest.counts["written_bt"]
        self.service.do(lambda: self.service.set_running(True, by_tie=by_tie))

    def _stop(self) -> None:
        svc = self.service
        svc.do(lambda: svc.set_running(False))
        # A drive that wrote rows goes into the same WiGLE upload queue a Kismet drive uses. With
        # Kismet capturing, its own stop queues the file.
        wrote = self.ingest.counts["written_wifi"] + self.ingest.counts["written_bt"] - self._wrote_at_start
        queue = self.host.queue_wigle(self)
        path = self.host.wardriving_csv()
        if wrote > 0 and queue is not None and path and not self._kismet_was:
            def job():
                try:
                    queue(path)
                    svc.say("[BRISTLE] This drive's wardriving.csv is in the WiGLE upload queue")
                except Exception as exc:
                    svc.say("[BRISTLE] Could not queue the WiGLE upload: %s" % exc, "warn")
            svc.do(job)

    def _toggle_hud(self) -> None:
        from plugins._bristle import settings
        now_on = not settings.on(self.service.conf, "hud")
        self.service.remember("hud", "on" if now_on else "off")
        self.msg("[BRISTLE] HUD %s" % ("on" if now_on else "off"))

    # ------------------------------------------------------------------ alerts: popup and sound
    def _sound(self, pyxel, name) -> None:
        if self.host.muted() or str(self.service.conf.get("alert_sound", "on")).lower() != "on":
            return
        slot, notes, tone, volume, effect, speed = SOUNDS[name]
        try:
            if name not in self._sounds_ready:
                pyxel.sounds[slot].set(notes, tone, volume, effect, speed)
                self._sounds_ready = {n for n in self._sounds_ready if SOUNDS[n][0] != slot} | {name}
            pyxel.play(3, slot, loop=False)
        except Exception:
            pass

    def _alerts(self, pyxel) -> None:
        from plugins._bristle import alerts, screen_more
        now = time.monotonic()
        if self._popup is not None and now >= self._popup[1]:
            self._popup = None
        center = self.service.nearby.center
        if self._popup is None and center.popups:
            alert = center.popups.popleft()
            self._popup = (alert, now + screen_more.POPUP_SECONDS)
            self.msg("[ALERT] " + alert.title, 8)
            self._sound(pyxel, alerts.TYPES.get(alert.type, ("", "notice", 0))[1])

    def _hunt_beep(self, pyxel) -> None:
        hunt = self.service.nearby.hunt
        if hunt is None:
            return
        now = time.monotonic()
        if hunt.beep_due(now, self._last_beep):
            self._last_beep = now
            self._sound(pyxel, "beep")

    # ------------------------------------------------------------------ keys on the Nodes screen
    def _say(self, text, colour=7) -> None:
        self._status = (text, colour)

    def _confirmed(self, what, hwid) -> bool:
        now = time.monotonic()
        if self._confirm and self._confirm[:2] == (what, hwid) and now < self._confirm[2]:
            self._confirm = None
            return True
        self._confirm = (what, hwid, now + CONFIRM_SECONDS)
        return False

    def _keys(self, pyxel) -> None:
        from plugins._bristle import flasher, proto, screen
        svc = self.service
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._close(pyxel)
            return
        now = time.monotonic()
        if now - self._rows_at > 0.5:
            self._rows = screen.node_rows(svc)
            self._rows_at = now
            self._selected = max(0, min(self._selected, len(self._rows) - 1))
        if pyxel.btnp(pyxel.KEY_DOWN, 12, 3):
            self._selected = min(self._selected + 1, max(0, len(self._rows) - 1))
        if pyxel.btnp(pyxel.KEY_UP, 12, 3):
            self._selected = max(self._selected - 1, 0)
        row = self._rows[self._selected] if self._rows else None
        session = row.get("session") if row else None
        if pyxel.btnp(pyxel.KEY_S):
            if svc.fleet.run_mask:
                self._stop()
            elif not svc.fleet.authed():
                self._say("No node is online: nothing to start", screen.C_WARN)
            else:
                self._start()
        elif pyxel.btnp(pyxel.KEY_H):
            self._toggle_hud()
        elif pyxel.btnp(pyxel.KEY_K):
            self._dialog = {"type": "key"}
        elif pyxel.btnp(pyxel.KEY_R):
            if self._confirmed("renumber", ""):
                svc.do(lambda: (svc.registry.renumber(), svc.fleet.replan()))
                self._say("Renumbering 1..N in the current order", screen.C_OK)
            else:
                self._say("Renumber every node 1..N (closes gaps)? Press R again to do it", screen.C_WARN)
        elif row is None:
            return
        elif pyxel.btnp(pyxel.KEY_A):
            if row["kind"] == "port":
                svc.do(lambda: svc.probe(row["key"]))
                self._say("Saying hello to %s. If it is a Bristle board it shows up as NEW: press A again" % row["port"].device)
            elif row["kind"] == "new" and session is not None:
                svc.do(lambda: self._adopt(session))
            else:
                self._say("Select a NEW row to adopt", screen.C_DIM)
        elif pyxel.btnp(pyxel.KEY_W):
            link = svc.link_of(session) if session is not None else None
            if row["kind"] == "port":
                port, first = row["port"], True
            elif link is not None:
                port, first = link.port, row["kind"] != "node"     # one of your nodes: keep its settings
            else:
                self._say("Select a NEW PORT row (a blank board on USB), or a node that is on USB", screen.C_DIM)
                return
            board = getattr(session, "board", "") if session is not None else ""
            board = board if board in flasher.BOARDS else flasher.guess_board(port.vid, port.pid)
            found = svc.firmware()
            self._dialog = {"type": "flash", "key": port.key, "device": port.device, "first": first,
                            "board": flasher.BOARDS.index(board),
                            "note": "Image: %s" % found[0].name if found else "NO FIRMWARE FOUND (bristle/firmware/releases)"}
        elif pyxel.btnp(pyxel.KEY_X):
            if row["kind"] == "port":
                svc.do(lambda: svc.not_a_node(row["key"], "marked on the Nodes screen"))
                self._say("%s is marked not a node: Bristle will never speak to it" % row["port"].device, screen.C_OK)
            elif row["kind"] == "new" and session is not None and svc.link_of(session) is not None:
                key = svc.link_of(session).port.key
                svc.do(lambda: svc.not_a_node(key, "marked on the Nodes screen"))
                self._say("That device is marked not a node", screen.C_OK)
            else:
                self._say("Select a NEW PORT row to mark it not a node", screen.C_DIM)
        elif pyxel.btnp(pyxel.KEY_B):
            hwid = row.get("hwid")
            if row["kind"] == "blocked":
                svc.do(lambda: svc.registry.unblock(hwid))
                self._say("Unblocked %s" % hwid, screen.C_OK)
            elif row["kind"] in ("new", "other") and session is not None:
                svc.do(lambda: session.block("blocked on the Nodes screen"))
                self._say("Blocked %s: it will be ignored and never offered again" % hwid, screen.C_OK)
            elif row["kind"] == "node":
                self._say("That is one of your nodes. Forget it first (F), then block it.", screen.C_WARN)
        elif row["kind"] != "node":
            return
        elif pyxel.btnp(pyxel.KEY_I):
            if session is None:
                self._say("Node %d is offline" % row["number"], screen.C_WARN)
            else:
                svc.do(lambda: session.identify(10))
                self._say("Node %d is blinking its LED for 10 s" % row["number"], screen.C_OK)
        elif pyxel.btnp(pyxel.KEY_P):
            nxt = screen.PINS[(screen.PINS.index(row["pin"]) + 1) % len(screen.PINS)] if row["pin"] in screen.PINS else None
            svc.do(lambda: (svc.registry.set_pin(row["hwid"], nxt), svc.fleet.replan()))
            self._say("Node %d: %s" % (row["number"], screen.PIN_NAMES[nxt]), screen.C_OK)
        elif pyxel.btnp(pyxel.KEY_U):
            update = svc.ota
            if update is not None and update.sender.active:
                if self._confirmed("stop-update", ""):
                    svc.do(svc.stop_ota)
                else:
                    self._say("Node %s is being updated. Press U again to stop that update." % update.number, screen.C_WARN)
            elif session is None:
                self._say("Node %d is offline: it can only be updated while it is linked" % row["number"], screen.C_WARN)
            elif self._confirmed("update", row["hwid"]):
                svc.do(lambda: svc.start_ota(row["hwid"]))
                self._say("Starting the update of node %d" % row["number"], screen.C_OK)
            else:
                found = svc.firmware()
                self._say("Update node %d to %s over its link? It keeps scanning data and settings. Press U again"
                          % (row["number"], found[0].name if found else "(NO FIRMWARE FOUND)"), screen.C_WARN)
        elif pyxel.btnp(pyxel.KEY_G):
            if session is None or session.link_type != proto.LINK_USB:
                self._say("The bridge role is set over USB: plug the board into the hub first", screen.C_WARN)
            elif self._confirmed("bridge", row["hwid"]):
                to_bridge = not session.is_bridge
                svc.do(lambda: svc.set_bridge(session, to_bridge))
            else:
                self._say("Make node %d %s? It restarts. Press G again" % (
                    row["number"], "a scanning node again" if session.is_bridge else
                    "the ESP-NOW bridge (it stops scanning and relays for the others)"), screen.C_WARN)
        elif pyxel.btnp(pyxel.KEY_F):
            if self._confirmed("forget", row["hwid"]):
                svc.do(lambda: self._forget(row["hwid"], session))
                self._say("Node %d forgotten%s" % (row["number"], "" if session else " (offline: free it with its BOOT button, 5 s)"), screen.C_OK)
            else:
                self._say("Forget node %d? It loses its owner and its stored finds. Press F again" % row["number"], screen.C_WARN)
        self._rows_at = 0.0 if any(pyxel.btnp(k) for k in (pyxel.KEY_A, pyxel.KEY_B, pyxel.KEY_X, pyxel.KEY_F,
                                                           pyxel.KEY_P, pyxel.KEY_R, pyxel.KEY_G)) else self._rows_at

    def _adopt(self, session) -> None:             # worker thread
        try:
            number = session.adopt()
            self.service.say("[BRISTLE] Adopting %s as node %d" % (session.hwid, number), "ok")
        except Exception as exc:
            self.service.say("[BRISTLE] Could not adopt: %s" % exc, "warn")

    def _forget(self, hwid, session) -> None:      # worker thread
        if session is not None:
            session.forget_on_node()
        else:
            self.service.registry.forget(hwid)
        self.service.fleet.replan()

    # ------------------------------------------------------------------ dialogs
    def _ask(self, title, hint, done, mask=False, value="") -> None:
        self._dialog = {"type": "text", "title": title, "hint": hint, "value": value, "mask": mask, "done": done}

    def _dialog_keys(self, pyxel) -> None:
        from plugins._bristle import flasher, screen, screen_more, second_core
        svc, dialog = self.service, self._dialog
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._dialog = None
            self.app._esc_consumed_frame = pyxel.frame_count
            return
        if dialog["type"] == "flash":
            if pyxel.btnp(pyxel.KEY_LEFT) or pyxel.btnp(pyxel.KEY_RIGHT):
                step = 1 if pyxel.btnp(pyxel.KEY_RIGHT) else -1
                dialog["board"] = (dialog["board"] + step) % len(flasher.BOARDS)
            elif pyxel.btnp(pyxel.KEY_RETURN):
                key, board, first = dialog["key"], flasher.BOARDS[dialog["board"]], dialog["first"]
                svc.do(lambda: svc.start_flash(key, board, first))
                self._say("Flashing %s. Do not unplug it." % dialog["device"], screen.C_WARN)
                self._dialog = None
        elif dialog["type"] == "key":
            least = second_core.MIN_PASSPHRASE
            if pyxel.btnp(pyxel.KEY_E):
                def again(first_entry):
                    def check(second_entry):
                        if second_entry != first_entry:
                            self._say("The two passphrases differ: nothing was exported", screen.C_ERR)
                        else:
                            svc.do(lambda: svc.export_core(first_entry))
                            self._say("Exporting to %s" % svc.export_path(), screen.C_OK)
                    if len(first_entry) < least:
                        self._say("The passphrase needs at least %d characters" % least, screen.C_ERR)
                    else:
                        self._ask("EXPORT: the same passphrase again", "", check, mask=True)
                self._ask("EXPORT: a passphrase for the file (at least %d characters)" % least,
                          "You type it again on the other install. It is not stored anywhere.", again, mask=True)
            elif pyxel.btnp(pyxel.KEY_M):
                def run(passphrase):
                    svc.do(lambda: svc.import_core(passphrase))
                    self._say("Importing %s" % svc.export_path(), screen.C_OK)
                self._ask("IMPORT: the passphrase of %s" % second_core.FILE_NAME,
                          "Node Memory is merged. A different owner key replaces this install's (the old one is kept).",
                          run, mask=True)
        else:
            dialog["value"] = screen_more.typed(pyxel, dialog["value"])
            if pyxel.btnp(pyxel.KEY_RETURN):
                self._dialog = None
                dialog["done"](dialog["value"])

    # ------------------------------------------------------------------ alerts screen
    def _alert_keys(self, pyxel) -> None:
        from plugins._bristle import alerts
        svc = self.service
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._close(pyxel)
            return
        names = list(alerts.TYPES)
        if pyxel.btnp(pyxel.KEY_DOWN, 12, 3):
            self._alert_selected = min(self._alert_selected + 1, len(names) - 1)
        if pyxel.btnp(pyxel.KEY_UP, 12, 3):
            self._alert_selected = max(self._alert_selected - 1, 0)
        if pyxel.btnp(pyxel.KEY_RETURN):
            name = names[self._alert_selected]
            on = not svc.nearby.center.is_on(name)
            svc.nearby.set_switch(name, on)
            svc.remember("alert_" + name, "on" if on else "off")
        elif pyxel.btnp(pyxel.KEY_O):
            on = str(svc.conf.get("alert_sound", "on")).lower() != "on"
            svc.remember("alert_sound", "on" if on else "off")

    # ------------------------------------------------------------------ suspects screen and hunt
    def _suspect_keys(self, pyxel) -> None:
        from plugins._bristle import screen, screen_more, skimmer
        svc = self.service
        engine = svc.nearby
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._close(pyxel)
            return
        if pyxel.btnp(pyxel.KEY_SLASH) or pyxel.btnp(pyxel.KEY_F1):
            self._suspect_help = not self._suspect_help
            return
        now = time.monotonic()
        if now - self._rows_at > 1.0:
            self._suspect_rows = screen_more.suspect_rows(engine.suspects, svc.here() or self.host.position())
            self._rows_at = now
            self._suspect_selected = max(0, min(self._suspect_selected, len(self._suspect_rows) - 1))
        rows = self._suspect_rows
        if pyxel.btnp(pyxel.KEY_DOWN, 12, 3):
            self._suspect_selected = min(self._suspect_selected + 1, max(0, len(rows) - 1))
        if pyxel.btnp(pyxel.KEY_UP, 12, 3):
            self._suspect_selected = max(self._suspect_selected - 1, 0)
        if not rows or self._suspect_help:
            return
        row = rows[self._suspect_selected]
        mac, suspect, est = row["mac"], row["suspect"], row["estimate"]
        if pyxel.btnp(pyxel.KEY_H):
            self._hunt_mac = mac
            self._view = HUNT
            svc.do(lambda: svc.hunt_start(mac))
        elif pyxel.btnp(pyxel.KEY_M):
            if est is None:
                self._say("No estimated spot yet: it has no sighting with a GPS position", screen.C_WARN)
            else:
                self.host.marker("bt", est.lat, est.lon, skimmer.colon(mac), "POSSIBLE SKIMMER " + (suspect.name or ""),
                                 suspect.points[-1][3] if suspect.points else -90)
                self._say("Marked on the map at %.6f, %.6f (within about %d m). Close this screen to see it."
                          % (est.lat, est.lon, round(est.radius_m)), screen.C_OK)
        elif pyxel.btnp(pyxel.KEY_R):
            svc.do(lambda: engine.mark(mac, skimmer.STATUS_REPORTED))
            self._say("Marked as reported", screen.C_OK)
        elif pyxel.btnp(pyxel.KEY_X):
            if self._confirmed("false", mac):
                svc.do(lambda: engine.mark(mac, skimmer.STATUS_FALSE))
                self._say("Marked a false alarm: this address is ignored from now on", screen.C_OK)
            else:
                self._say("False alarm? %s goes on the ignore list. Press X again" % skimmer.colon(mac), screen.C_WARN)
        elif pyxel.btnp(pyxel.KEY_U):
            svc.do(lambda: engine.mark(mac, skimmer.STATUS_NEW))
            self._say("Mark removed (and off the ignore list)", screen.C_OK)
        elif pyxel.btnp(pyxel.KEY_N):
            self._ask("NOTES for %s" % skimmer.colon(mac), "For the report: which pump, what you saw.",
                      lambda text: svc.do(lambda: engine.mark(mac, suspect.status, text)), value=suspect.notes)
        elif pyxel.btnp(pyxel.KEY_P) or pyxel.btnp(pyxel.KEY_E):
            def export(place):
                def job():
                    path = engine.export_report(mac, place)
                    svc.say("[BRISTLE] Report written: %s" % path, "ok")
                svc.do(job)
                self._say("Writing the report to loot/skimmers/reports", screen.C_OK)
            self._ask("REPORT: the place (station name and address)", "It goes at the top of the report. Empty is allowed.", export)
        self._rows_at = 0.0 if any(pyxel.btnp(k) for k in (pyxel.KEY_R, pyxel.KEY_X, pyxel.KEY_U)) else self._rows_at

    def _hunt_keys(self, pyxel) -> None:
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self.service.do(self.service.hunt_stop)
            self._hunt_mac = ""
            self._view = SUSPECTS
            self._rows_at = 0.0
            self.app._esc_consumed_frame = pyxel.frame_count

    # ------------------------------------------------------------------ drawing
    def draw(self, x: int, y: int, w: int, h: int) -> None:
        if not self._overlay_active:
            return
        import pyxel
        from plugins._bristle import screen, screen_more
        svc = self.service
        if svc is None:
            pyxel.rect(0, 0, w, h, screen.C_BG)
            pyxel.rect(0, 0, w, 12, screen.C_BAR)
            pyxel.text(4, 3, "BRISTLE: NODES", screen.C_TITLE)
            pyxel.text(4, 22, "Bristle did not start:", screen.C_ERR)
            pyxel.text(4, 34, screen.clip(self.error), screen.C_TEXT)
            pyxel.text(4, h - 12, "[ESC] Back", screen.C_DIM)
            return
        if self._view == ALERTS:
            screen_more.draw_alerts(pyxel, w, h, svc, self._alert_selected)
        elif self._view == SUSPECTS:
            screen_more.draw_suspects(pyxel, w, h, svc, self._suspect_rows, self._suspect_selected, self._status,
                                      self._suspect_help)
        elif self._view == HUNT:
            hunt = svc.nearby.hunt
            suspect = svc.nearby.suspects.suspects.get(self._hunt_mac)
            if hunt is None or suspect is None:
                pyxel.rect(0, 0, w, h, screen.C_BG)
                pyxel.text(4, 20, "Starting the hunt...", screen.C_DIM)
            else:
                view = screen_more.hunt_view(hunt, suspect.estimate(), svc.here() or self.host.position(), svc.heading())
                screen_more.draw_hunt(pyxel, w, h, svc, suspect, view)
        else:
            screen.draw(pyxel, w, h, svc, self.ingest, self._rows, self._selected, self._status, self._kismet_was)
        if self._dialog is not None:
            screen_more.draw_dialog(pyxel, w, h, self._dialog)

    def draw_map_overlay(self, pyxel, w, hud_top, term_y) -> None:
        """The activity line, bottom left of the map (F8 or [H] on the Nodes screen switches it),
        and an alert popup under the top bar while one is fresh."""
        if self.service is None:
            return
        from plugins._bristle import screen, screen_more, settings
        if settings.on(self.service.conf, "hud"):
            screen.draw_hud(pyxel, self.service, self.ingest, term_y)
        if self._popup is not None:
            screen_more.draw_popup(pyxel, w, hud_top, self._popup[0])
