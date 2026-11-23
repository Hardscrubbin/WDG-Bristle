"""PLUGINS > WDGWars: Pair / Status -- pair this device by code and show the
live portal state (rank, team, territory, contracts).

After WDGoNG 0.10.0's "WDGWars Pair" and game-state HUD (wdgong_ui/backend.py:
pair_start, _pair_worker, _fetch_gs_worker; wdgong_ui/main.py).
Required Notice: Copyright (c) 2025-2026 LOCOSP
PolyForm Noncommercial License 1.0.0 (see plugins/_ng/NOTICE). This build is
for personal, noncommercial use.

ONE portal client. This plugin has no HTTP code, no key storage and no
endpoint list of its own. Everything goes through upstream's Wars Sync plugin
(plugins/wardrive_upload.py), which already carries the portal calls upstream added
in 0.9.21 -- request_pair(), poll_pair(), adopt_api_key(), fetch_game_state()
-- with upstream's User-Agent, key handling and shutdown rules. So there is one
client, one API key (WARDRIVE_API_KEY in secrets.conf, written by upstream's own
adopt_api_key) and, for the game state, one polling loop:

  * one request at a time, never while Wars Sync is uploading and never while
    a pairing is in progress;
  * at most one game-state read per STATE_INTERVAL (60 s); after a failure the
    wait doubles up to 10 minutes. Failures are silent (the screen shows
    "offline"); nothing is written to the terminal while offline;
  * all of it on a worker thread: the game loop never waits for the portal.

Position. The portal needs a position to list territory near the player. upstream's
rule is that the game-state call sends coordinates only when the caller passes
them, and the only place the player has agreed to position-based portal
requests is SYSTEM > Auto Map + Turf. So a position (a fresh measured GPS
sample, rounded by upstream to 6 places) is sent only while that switch is ON;
otherwise the read is made without coordinates and the screen says how to get
the territory list.

The API key is never shown, logged or copied anywhere by this plugin.
"""
import collections
import math
import threading
import time

from plugins.plugin_base import PluginBase, PluginMenuItem

FONT_W = 5                      # upstream draws every pyxel.text() with Spleen 5x8 (app.py)
C_BG, C_BAR, C_TITLE, C_TEXT, C_DIM = 0, 1, 3, 7, 13
C_OK, C_WARN, C_ERR, C_BORDER = 11, 10, 8, 5
STATE_INTERVAL = 60.0           # seconds between game-state reads
STATE_FIRST_DELAY = 12.0        # after launch: let the game and Wars Sync settle
STATE_MAX_BACKOFF = 600.0
STATE_STALE = 300.0             # older than this the chip says "old"
PAIR_POLL_SECONDS = 3.0
PAIR_DEFAULT_LIFETIME = 600
PAIR_LABEL = "WDG-Bristle (uConsole)"
MAX_ROWS = 6


def clean(value, limit=40) -> str:
    """Server text made safe for the 5x8 font: printable ASCII, one line."""
    text = "" if value is None else str(value)
    text = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in text.replace("\n", " "))
    return text[:limit]


def as_int(value, default=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    return int(value)


def summarize(state) -> dict:
    """The parts of a /api/game-state reply this plugin shows, read
    defensively: any field may be missing or of another type."""
    state = state if isinstance(state, dict) else {}
    me = state.get("me") if isinstance(state.get("me"), dict) else {}
    team = me.get("team")
    if isinstance(team, dict):
        team_name = clean(team.get("name") or team.get("tag") or "", 24)
    else:
        team_name = clean(team or me.get("gang") or "", 24)
    rank = state.get("uconsole_rank") if isinstance(state.get("uconsole_rank"), dict) else {}
    contracts = [c for c in (state.get("contracts") or []) if isinstance(c, dict)] \
        if isinstance(state.get("contracts"), list) else []
    decay = state.get("decay_nearby")
    tiles = [t for t in decay if isinstance(t, dict)] if isinstance(decay, list) else None
    return dict(
        username=clean(me.get("username"), 24), level=as_int(me.get("level")), team=team_name,
        rank=as_int(rank.get("position")), rank_of=as_int(rank.get("of")),
        contracts=[dict(title=clean(c.get("title") or c.get("name") or c.get("type") or "contract", 44),
                        detail=clean(c.get("description") or c.get("status") or "", 60))
                   for c in contracts[:MAX_ROWS]],
        contract_count=len(contracts),
        tiles=None if tiles is None else len(tiles),
        critical=0 if tiles is None else sum(1 for t in tiles if t.get("critical")),
        vulnerable=0 if tiles is None else sum(1 for t in tiles if t.get("vulnerable")))


class NgWdgWars(PluginBase):
    NAME = "WDGWars Pair + Status (WDGoNG)"
    VERSION = "1.0"
    AUTHOR = "LOCOSP (WDGoNG 0.10.0 feature); plugin for WDG-Bristle"

    BACKGROUND_TICK = True      # the chip on the map stays live with the screen closed

    def __init__(self):
        super().__init__()
        self.has_overlay = True
        self._overlay_active = False
        self._events = collections.deque()      # (hud or None, colour, terminal or None)
        self._lock = threading.Lock()
        self._busy = False                      # one portal request of ours at a time
        self._summary = None                    # summarize() of the last good reply
        self._summary_at = 0.0                  # time.monotonic() of that reply
        self._with_position = False
        self._failures = 0
        self._next_state = time.monotonic() + STATE_FIRST_DELAY
        self._announced = False
        self._status, self._status_colour = "", C_DIM
        self._pair = None                       # dict(code, expires, state) while pairing
        self._pair_cancel = threading.Event()
        self._confirm_repair = False
        self._chip = True
        self._closing = False

    def menu_items(self) -> list[PluginMenuItem]:
        return [PluginMenuItem("p", "WDGWars: Pair / Status", "open_overlay")]

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    def open_overlay(self):
        self._overlay_active = True
        self._confirm_repair = False
        self._next_state = min(self._next_state, time.monotonic() + 1.0)

    # ------------------------------------------------------------------
    # The one portal client: upstream's Wars Sync plugin
    # ------------------------------------------------------------------

    def _portal(self):
        for plugin in getattr(self.app, "_plugins", ()) or ():
            if plugin is not self and all(callable(getattr(plugin, name, None)) for name in
                                          ("fetch_game_state", "request_pair", "poll_pair", "adopt_api_key")):
                return plugin
        return None

    @staticmethod
    def _configured(portal) -> bool:
        return bool(portal is not None and getattr(portal, "_api_key", "") and getattr(portal, "_api_url", ""))

    def _position(self):
        """(lat, lon) to send with the game-state read, or (None, None).
        Only while the player has Auto Map + Turf switched on."""
        if not getattr(getattr(self.app, "_auto_map", None), "enabled", False):
            return None, None
        sample = getattr(getattr(self.app, "gps", None), "wardrive_sample", None)
        try:
            fix = sample() if callable(sample) else None
        except Exception:
            fix = None
        if not fix:
            return None, None
        return fix.get("lat"), fix.get("lon")

    def _start(self, target, *args, status=None) -> bool:
        """[merge-v3] ``status`` is set here, before the worker exists: set by
        the caller afterwards, it could overwrite the result of a worker that
        had already finished (an instant "no pairing code" when offline)."""
        with self._lock:
            if self._busy or self._closing:
                return False
            self._busy = True
            if status:
                self._set_status(*status)
        thread = threading.Thread(target=self._job, args=(target, args), name="ng-wdgwars", daemon=True)
        try:
            thread.start()
        except RuntimeError:
            self._busy = False
            return False
        return True

    def _job(self, target, args) -> None:
        try:
            target(*args)
        except Exception as exc:
            self._set_status("WDGWars: %s" % type(exc).__name__, C_ERR)
        finally:
            self._busy = False

    def _set_status(self, text, colour=C_DIM) -> None:
        self._status, self._status_colour = text, colour

    # -- game state ------------------------------------------------------
    def _state_worker(self, portal, lat, lon) -> None:
        state = portal.fetch_game_state(lat, lon) if lat is not None else portal.fetch_game_state()
        now = time.monotonic()
        if not state:
            self._failures += 1
            self._next_state = now + min(STATE_MAX_BACKOFF, STATE_INTERVAL * (2 ** min(self._failures, 4)))
            self._set_status("WDGWars is not answering (offline?). Trying again later; nothing is lost.", C_WARN)
            return
        summary = summarize(state)
        self._summary, self._summary_at, self._with_position = summary, now, lat is not None
        self._failures = 0
        self._next_state = now + STATE_INTERVAL
        self._set_status("Portal state read.", C_OK)
        if not self._announced:
            self._announced = True
            rank = ("uConsole rank #%d of %d" % (summary["rank"], summary["rank_of"] or 0)
                    if summary["rank"] else "not ranked yet")
            self._events.append((None, C_TEXT, "[WDGWars] %s lvl %s -- %s" % (
                summary["username"] or "?", summary["level"] if summary["level"] is not None else "?", rank)))

    def _tick_state(self, now) -> None:
        if now < self._next_state or self._pair is not None or self._busy:
            return
        portal = self._portal()
        if not self._configured(portal):
            self._next_state = now + STATE_INTERVAL
            return
        if getattr(portal, "_uploading", False):
            self._next_state = now + 10.0          # Wars Sync is uploading: its turn
            return
        lat, lon = self._position()
        self._next_state = now + STATE_INTERVAL    # moved on by the worker's result
        self._start(self._state_worker, portal, lat, lon)

    # -- pairing ---------------------------------------------------------
    def start_pairing(self) -> bool:
        portal = self._portal()
        if portal is None:
            self._set_status("The Wars Sync plugin is not loaded, so there is no portal client.", C_ERR)
            return False
        if self._pair is not None or self._busy:
            self._set_status("Busy -- wait a moment.", C_WARN)
            return False
        if not getattr(portal, "_api_url", ""):
            self._set_status("No WDGWars server is set (Wars Sync > server URL).", C_ERR)
            return False
        self._pair_cancel.clear()
        self._pair = dict(code="", expires=0.0, state="requesting")
        if not self._start(self._pair_worker, portal, status=("Asking WDGWars for a pairing code...", C_TITLE)):
            self._pair = None
            return False
        return True

    def cancel_pairing(self) -> None:
        if self._pair is not None:
            self._pair_cancel.set()
            self._set_status("Pairing cancelled. Nothing was changed.", C_DIM)

    def _pair_worker(self, portal) -> None:
        try:
            reply = portal.request_pair(PAIR_LABEL)
            code = clean((reply or {}).get("code"), 16)
            secret = (reply or {}).get("poll")
            if not code or not isinstance(secret, str) or not secret:
                self._set_status("WDGWars gave no pairing code (offline, or pairing is not available).", C_ERR)
                return
            lifetime = as_int((reply or {}).get("expires_in"), PAIR_DEFAULT_LIFETIME) or PAIR_DEFAULT_LIFETIME
            lifetime = max(30, min(lifetime, 3600))
            self._pair = dict(code=code, expires=time.monotonic() + lifetime, state="waiting")
            self._events.append((None, C_TEXT, "[WDGWars] pairing code %s -- enter it at the portal "
                                               "(profile > API keys); it expires in %d s" % (code, lifetime)))
            self._set_status("Enter the code at the portal. Waiting for it to be accepted...", C_TITLE)
            while not self._pair_cancel.wait(PAIR_POLL_SECONDS):
                if time.monotonic() >= self._pair["expires"]:
                    self._set_status("The pairing code expired. Press P for a new one.", C_WARN)
                    return
                answer = portal.poll_pair(code, secret)
                key = answer.get("api_key") if isinstance(answer, dict) else None
                if key:
                    try:
                        portal.adopt_api_key(key)      # upstream validates and stores it; never shown here
                    except (ValueError, RuntimeError, OSError) as exc:
                        self._set_status("The portal's key was not accepted: %s" % clean(exc, 60), C_ERR)
                        return
                    name = clean(answer.get("username"), 24) or "your account"
                    self._set_status("Paired as %s. The key is saved in secrets.conf." % name, C_OK)
                    self._events.append(("[WDGWars] Paired as %s" % name, C_OK,
                                         "[WDGWars] paired as %s; API key saved (not shown)" % name))
                    self._summary, self._announced, self._failures = None, False, 0
                    self._next_state = time.monotonic() + 3.0
                    return
                if isinstance(answer, dict) and answer.get("error") == "bad_request":
                    self._set_status("The portal no longer knows this code (expired). Press P for a new one.",
                                     C_WARN)
                    return
        finally:
            self._pair = None

    # ------------------------------------------------------------------
    # Per-frame update
    # ------------------------------------------------------------------

    def _drain(self) -> None:
        while self._events:
            hud, colour, terminal = self._events.popleft()
            if terminal:
                self.term(terminal)
            if hud:
                self.msg(hud, colour)

    def on_update(self) -> None:
        self._drain()
        self._tick_state(time.monotonic())
        if not self._overlay_active:
            return
        import pyxel
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            if self._pair is not None:
                self.cancel_pairing()
            self._overlay_active = False
            self._confirm_repair = False
            if self.app:
                self.app._esc_consumed_frame = pyxel.frame_count
            return
        if self._confirm_repair:
            if pyxel.btnp(pyxel.KEY_Y):
                self._confirm_repair = False
                self.start_pairing()
            elif pyxel.btnp(pyxel.KEY_N):
                self._confirm_repair = False
                self._set_status("Pairing not started. The current key stays.", C_DIM)
            return
        if pyxel.btnp(pyxel.KEY_P):
            if self._pair is not None:
                return
            if self._configured(self._portal()):
                self._confirm_repair = True       # a key is already set: ask first
            else:
                self.start_pairing()
        elif pyxel.btnp(pyxel.KEY_X):
            self.cancel_pairing()
        elif pyxel.btnp(pyxel.KEY_R):
            self._next_state = 0.0
        elif pyxel.btnp(pyxel.KEY_H):
            self._chip = not self._chip

    def on_unload(self) -> None:
        self._closing = True
        self._pair_cancel.set()

    # ------------------------------------------------------------------
    # Map chip (drawn by app.py's plugin map-overlay hook)
    # ------------------------------------------------------------------

    def chip_text(self, now=None):
        """One line for the map chip, or None when there is nothing to show."""
        summary = self._summary
        if not self._chip or summary is None:
            return None
        now = time.monotonic() if now is None else now
        parts = ["WDGWars"]
        if summary["rank"]:
            parts.append("#%d" % summary["rank"])
        if summary["team"]:
            parts.append(summary["team"][:14])
        if summary["tiles"] is not None:
            risk = summary["critical"] + summary["vulnerable"]
            parts.append("turf %d%s" % (summary["tiles"], " (%d at risk)" % risk if risk else ""))
        if now - self._summary_at > STATE_STALE:
            parts.append("old")
        return " ".join(parts)[:52]

    def draw_map_overlay(self, pyxel, w, hud_top, term_y) -> None:
        """Top of the map, left of the radar. Called every frame the map is
        visible; draws nothing until a portal state has been read."""
        text = self.chip_text()
        if not text:
            return
        summary = self._summary
        width = len(text) * FONT_W + 6
        x = w - 56 - width
        y = hud_top + 3
        risk = summary["critical"] + summary["vulnerable"]
        pyxel.rect(x, y, width, 11, C_BG)
        pyxel.rectb(x, y, width, 11, C_WARN if risk else C_BORDER)
        pyxel.text(x + 3, y + 2, text, C_WARN if risk else C_TITLE)

    # ------------------------------------------------------------------
    # Draw (640x360, pyxel 16 colours, upstream overlay style)
    # ------------------------------------------------------------------

    def draw(self, x: int, y: int, w: int, h: int) -> None:
        if not self._overlay_active:
            return
        import pyxel
        pyxel.rect(0, 0, w, h, C_BG)
        pyxel.rect(0, 0, w, 12, C_BAR)
        pyxel.text(4, 3, "WDGWARS: PAIR / STATUS", C_TITLE)
        if self._busy:
            pyxel.text(w - 4 - FONT_W * 7, 3, "working", C_WARN)
        pyxel.text(6, 18, self._status[:124], self._status_colour)

        portal = self._portal()
        configured = self._configured(portal)
        summary = self._summary
        now = time.monotonic()
        ty = 34
        pyxel.text(6, ty, "ACCOUNT", C_DIM)
        pyxel.line(6, ty + 9, w - 7, ty + 9, C_BORDER)
        ty += 13
        server = clean(str(getattr(portal, "_api_url", "") or "").split("/api/")[0].replace("https://", ""), 60)
        rows = [("Portal client", "the game's Wars Sync (the only one)" if portal is not None else
                 "NOT LOADED -- enable plugins/wardrive_upload.py", C_TEXT if portal is not None else C_ERR),
                ("Server", server or "not set", C_TEXT if server else C_WARN),
                ("API key", "set" if configured else "not set -- press P to pair this device by code",
                 C_OK if configured else C_WARN)]
        if summary is not None:
            age = now - self._summary_at
            rows += [("Player", "%s   level %s" % (summary["username"] or "?",
                                                    summary["level"] if summary["level"] is not None else "?"),
                      C_TEXT),
                     ("Team", summary["team"] or "none", C_TEXT),
                     ("uConsole rank", ("#%d of %d" % (summary["rank"], summary["rank_of"] or 0))
                      if summary["rank"] else "not ranked yet", C_TEXT),
                     ("Read", "%d s ago%s" % (age, "  (the portal has not answered since)"
                                              if self._failures else ""), C_WARN if self._failures else C_DIM)]
        elif configured:
            rows.append(("State", "not read yet" if not self._failures else
                         "the portal is not answering (offline?)", C_WARN if self._failures else C_DIM))
        for label, value, colour in rows:
            pyxel.text(6, ty, label, C_DIM)
            pyxel.text(86, ty, value[:106], colour)
            ty += 9

        ty += 6
        pyxel.text(6, ty, "TERRITORY NEAR YOU", C_DIM)
        pyxel.line(6, ty + 9, w - 7, ty + 9, C_BORDER)
        ty += 13
        if summary is None:
            pyxel.text(6, ty, "-", C_DIM)
        elif summary["tiles"] is None:
            hint = ("The portal sent no territory list." if self._with_position else
                    "Needs your position. It is only sent while SYSTEM > Auto Map + Turf is ON (and GPS has a fix).")
            pyxel.text(6, ty, hint, C_DIM)
        else:
            risk = summary["critical"] + summary["vulnerable"]
            pyxel.text(6, ty, "%d tile(s) held by your team; %d critical, %d vulnerable"
                       % (summary["tiles"], summary["critical"], summary["vulnerable"]),
                       C_WARN if risk else C_OK)
        ty += 15
        pyxel.text(6, ty, "CONTRACTS", C_DIM)
        pyxel.line(6, ty + 9, w - 7, ty + 9, C_BORDER)
        ty += 13
        if summary is None or not summary["contract_count"]:
            pyxel.text(6, ty, "none" if summary is not None else "-", C_DIM)
            ty += 9
        else:
            for contract in summary["contracts"]:
                pyxel.text(6, ty, contract["title"], C_TEXT)
                pyxel.text(236, ty, contract["detail"], C_DIM)
                ty += 9
            extra = summary["contract_count"] - len(summary["contracts"])
            if extra > 0:
                pyxel.text(6, ty, "... and %d more on the portal" % extra, C_DIM)

        pair = self._pair
        if pair is not None or self._confirm_repair:
            bx, by, bw, bh = 110, 232, 420, 70
            pyxel.rect(bx, by, bw, bh, C_BG)
            pyxel.rectb(bx, by, bw, bh, C_WARN)
            if self._confirm_repair:
                pyxel.text(bx + 8, by + 8, "A KEY IS ALREADY SET", C_WARN)
                pyxel.text(bx + 8, by + 22, "Pairing again replaces it with the key of the account that", C_TEXT)
                pyxel.text(bx + 8, by + 31, "accepts the new code. Uploads then go to that account.", C_TEXT)
                pyxel.text(bx + 8, by + 50, "[Y] Pair again    [N] Keep the current key", C_WARN)
            elif pair["state"] == "requesting":
                pyxel.text(bx + 8, by + 8, "PAIRING", C_WARN)
                pyxel.text(bx + 8, by + 26, "Asking the portal for a code...", C_TEXT)
            else:
                left = max(0, int(pair["expires"] - now))
                pyxel.text(bx + 8, by + 8, "PAIRING CODE", C_WARN)
                pyxel.text(bx + 120, by + 8, pair["code"], C_OK)
                pyxel.text(bx + 8, by + 24, "Log in at the portal, open profile > API keys > pair a device,", C_TEXT)
                pyxel.text(bx + 8, by + 33, "and type this code. This screen picks the key up by itself.", C_TEXT)
                pyxel.text(bx + 8, by + 50, "Expires in %d s      [X] Cancel" % left, C_WARN)

        pyxel.text(4, h - 32, "Uses the game's Wars Sync as the only portal client: one key, one request at a time, "
                              "one state read a minute.", C_DIM)
        pyxel.text(4, h - 22, "The API key is never shown. The chip on the map: %s." %
                   ("on" if self._chip else "off"), C_DIM)
        pyxel.text(4, h - 12, "[P] Pair this device  [R] Read now  [H] Map chip on/off  [ESC] Back", C_DIM)
