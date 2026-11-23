"""[bristle] PLUGINS > Bristle: Nodes -- a fleet of ESP32-C5 scanning nodes, with this uConsole as
the core.

Nodes scan Wi-Fi and Bluetooth on the channels this core gives them and hand their finds over with
a time stamp. The game places each find with its own GPS and writes it through the same loot
writers as every other source (wardriving.csv, bt_devices.csv, XP, map markers).

This step: nodes on USB. A board joins only when you press Adopt. The core speaks only to boards
that speak the Bristle protocol, pass the owner check and are in Node Memory; a new serial port is
listed and left alone until you say so.

Threads: all serial, port and file work is in plugins/_bristle/service.py's worker threads. This
file runs on the game thread and only reads values, drains queues within a time budget and draws.
"""
import collections
import time
from pathlib import Path

from plugins.plugin_base import PluginBase, PluginMenuItem

FONT_W = 5
CONFIRM_SECONDS = 3.0
MESSAGES_PER_FRAME = 3


class BristleNodes(PluginBase):
    NAME = "Bristle: Nodes"
    VERSION = "0.3"
    AUTHOR = "WDG-Bristle"
    BACKGROUND_TICK = True          # nodes keep delivering while the screen is closed

    def __init__(self):
        super().__init__()
        self.has_overlay = True
        self._overlay_active = False
        self.service = None
        self.ingest = None
        self.error = ""
        self._jobs = collections.deque()
        self._selected = 0
        self._rows = []
        self._rows_at = 0.0
        self._status = None
        self._confirm = None           # (what, hwid, until)
        self._kismet_was = False
        self._wrote_at_start = 0

    # ------------------------------------------------------------------ plugin interface
    def menu_items(self):
        return [PluginMenuItem("n", "Bristle: Nodes", "open_overlay")]

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    def open_overlay(self):
        self._overlay_active = True
        self._rows_at = 0.0

    def on_load(self, app) -> None:
        self.app = app
        try:
            from plugins._bristle import ingest, service
            root = Path(__file__).resolve().parent.parent
            loot = getattr(app, "loot", None)
            loot_base = getattr(loot, "_base", None) or (root / "loot")
            self.service = service.BristleService(root, loot_base, self._on_find, held_port=self._game_port)
            self.ingest = ingest.Ingest(app, self.service.fleet.delivery, say=self.term)
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

    def _game_port(self):
        """The serial port WDG's own ESP32 link has open (Bristle must never open it too)."""
        serial = getattr(self.app, "serial", None)
        return getattr(serial, "device", None)

    def _kismet(self):
        for plugin in getattr(self.app, "_plugins", ()):
            if plugin is not self and callable(getattr(plugin, "kismet_active", None)):
                return plugin
        return None

    # ------------------------------------------------------------------ every frame
    def on_update(self) -> None:
        svc = self.service
        if svc is None:
            if self._overlay_active:
                import pyxel
                if pyxel.btnp(pyxel.KEY_ESCAPE):
                    self._overlay_active = False
                    self.app._esc_consumed_frame = pyxel.frame_count
            return
        import pyxel
        self.ingest.feed_gps()
        self.ingest.drain()
        for _ in range(MESSAGES_PER_FRAME):       # service messages -> the game's terminal
            if not svc.terminal:
                break
            self.term(svc.terminal.popleft())
        self._follow_kismet()
        key = getattr(pyxel, "KEY_" + str(svc.conf.get("hud_key", "F8")).upper(), None)
        if key is not None and pyxel.btnp(key):
            self._toggle_hud()
        if self._overlay_active:
            self._keys(pyxel)

    def _follow_kismet(self) -> None:
        svc = self.service
        kismet = self._kismet()
        active = False
        try:
            active = bool(kismet is not None and kismet.kismet_active())
        except Exception:
            pass
        if active != self._kismet_was:
            self._kismet_was = active
            from plugins._bristle import settings
            if settings.on(svc.conf, "tie_to_kismet"):
                if active and not svc.fleet.run_mask:
                    self._start(by_tie=True)
                elif not active and svc.started_by_tie:
                    self._stop()

    def _start(self, by_tie=False) -> None:
        self._wrote_at_start = self.ingest.counts["written_wifi"] + self.ingest.counts["written_bt"]
        self.service.do(lambda: self.service.set_running(True, by_tie=by_tie))

    def _stop(self) -> None:
        svc = self.service
        svc.do(lambda: svc.set_running(False))
        # A drive that wrote rows goes into the same WiGLE upload queue a Kismet drive uses. With
        # Kismet capturing, its own stop queues the file.
        wrote = self.ingest.counts["written_wifi"] + self.ingest.counts["written_bt"] - self._wrote_at_start
        kismet = self._kismet()
        queue = getattr(kismet, "_queue_wigle_upload", None)
        session = getattr(getattr(self.app, "loot", None), "session_path", None)
        if wrote > 0 and callable(queue) and session and not self._kismet_was:
            path = str(Path(session) / "wardriving.csv")

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
        from plugins._bristle import screen
        svc = self.service
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._overlay_active = False
            self.app._esc_consumed_frame = pyxel.frame_count
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
        elif pyxel.btnp(pyxel.KEY_U):
            self._say("Firmware updates from this screen come with the OTA step. For now: docs/FLASHING.md", screen.C_DIM)
        elif pyxel.btnp(pyxel.KEY_K):
            self._say("Owner key export/import comes with the second-core step. The key is in secrets.conf.", screen.C_DIM)
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
        elif pyxel.btnp(pyxel.KEY_F):
            if self._confirmed("forget", row["hwid"]):
                svc.do(lambda: self._forget(row["hwid"], session))
                self._say("Node %d forgotten%s" % (row["number"], "" if session else " (offline: free it with its BOOT button, 5 s)"), screen.C_OK)
            else:
                self._say("Forget node %d? It loses its owner and its stored finds. Press F again" % row["number"], screen.C_WARN)
        self._rows_at = 0.0 if any(pyxel.btnp(k) for k in (pyxel.KEY_A, pyxel.KEY_B, pyxel.KEY_X, pyxel.KEY_F,
                                                           pyxel.KEY_P, pyxel.KEY_R)) else self._rows_at

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

    # ------------------------------------------------------------------ drawing
    def draw(self, x: int, y: int, w: int, h: int) -> None:
        if not self._overlay_active:
            return
        import pyxel
        from plugins._bristle import screen
        if self.service is None:
            pyxel.rect(0, 0, w, h, screen.C_BG)
            pyxel.rect(0, 0, w, 12, screen.C_BAR)
            pyxel.text(4, 3, "BRISTLE: NODES", screen.C_TITLE)
            pyxel.text(4, 22, "Bristle did not start:", screen.C_ERR)
            pyxel.text(4, 34, screen.clip(self.error), screen.C_TEXT)
            pyxel.text(4, h - 12, "[ESC] Back", screen.C_DIM)
            return
        screen.draw(pyxel, w, h, self.service, self.ingest, self._rows, self._selected, self._status,
                    self._kismet_was)

    def draw_map_overlay(self, pyxel, w, hud_top, term_y) -> None:
        """The activity line, bottom left of the map (F8 or [H] on the Nodes screen switches it)."""
        if self.service is None:
            return
        from plugins._bristle import screen, settings
        if settings.on(self.service.conf, "hud"):
            screen.draw_hud(pyxel, self.service, self.ingest, term_y)
