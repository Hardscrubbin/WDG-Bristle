"""ADS-B tracks, receiver coverage and KML export -- ported from WDGoNG 0.10.0.

Required Notice: Copyright (c) 2025-2026 LOCOSP
PolyForm Noncommercial License 1.0.0 (see plugins/_ng/NOTICE). This build is
for personal, noncommercial use.

What comes from where
---------------------
* plugins/_ng/adsb_tracks.py is WDGoNG's file, unmodified: it reads a loot
  session's ADS-B log, works out the receiver position and the range per
  10 degree sector, and writes the KML.
* This plugin is the upstream side: it follows wdgong_ui/backend.py (_adsb_rx_log,
  adsb_tracks, adsb_export_kml) and draws WDGoNG's raylib "tracks" view as a
  640x360, 16 colour pyxel screen in upstream's overlay style.

How it hooks into upstream's ADS-B receive path (no app.py or sdr_manager edit)
------------------------------------------------------------------------
upstream's SDRManager already writes the track log WDGoNG reads: one row of
loot/<session>/adsb_aircraft.csv for every position message, with the columns
WDGoNG expects (plus squawk). So nothing is added to the receive path itself.
The one thing WDGoNG records that upstream does not is where the *receiver* was:
while upstream's ADS-B radar runs and GPS has a fresh measured position, this plugin
appends it to loot/<session>/adsb_receiver.csv every 30 s (from its background
tick), which is what range and coverage are measured from.

Nothing here blocks the game loop: reading a session's log (it reaches
megabytes) and writing the KML run on a worker thread; the screen shows
"Reading..." meanwhile.

PLUGINS menu
------------
* ADS-B: Tracks & Coverage      -- the screen (R reload, E export, ESC back)
* ADS-B: Export KML (session)   -- writes loot/<session>/adsb_tracks.kml
"""

import collections
import math
import os
import threading
import time

from plugins.plugin_base import PluginBase, PluginMenuItem
from plugins._ng import adsb_tracks

RX_LOG_INTERVAL = 30.0        # seconds between receiver positions (as WDGoNG)
MAX_DRAW_POINTS = 4000        # track points drawn per frame, across all aircraft
PLOT = (332, 22, 300, 314)    # x, y, w, h of the map panel on the 640x360 screen
LIST_ROWS = 24
FONT_W = 5                    # upstream draws every pyxel.text() with Spleen 5x8 (app.py)

# pyxel's default 16 colours, used the way upstream's own screens use them.
C_BG, C_BAR, C_TITLE, C_TEXT, C_DIM = 0, 1, 3, 7, 13
C_OK, C_WARN, C_ERR, C_BORDER = 11, 10, 8, 5
C_COVER = 13                  # grey: not one of the altitude colours


def altitude_colour(alt_ft) -> int:
    """WDGoNG's altitude scale (low warm, high cool) on the pyxel palette."""
    if alt_ft < 10000:
        return 9      # orange
    if alt_ft < 20000:
        return 10     # yellow
    if alt_ft < 30000:
        return 11     # green
    if alt_ft < 40000:
        return 12     # cyan
    return 14         # pink


def build_view(data, plot=PLOT, max_points=MAX_DRAW_POINTS) -> dict:
    """Turn adsb_tracks.load() output into ready-to-draw screen coordinates.

    Runs on the worker thread so draw() only replays lines. Tracks are thinned
    evenly to at most ``max_points`` points in total (a long session holds up
    to 200,000); the first and last point of every track are always kept.
    Returns {"lines": [(colour, [(x, y), ...])], "ring": [(x, y), ...],
    "receiver": (x, y) or None, "rows": [(label, icao, n, lo, hi, colour)]}.
    """
    px, py, pw, ph = plot
    view = {"lines": [], "ring": [], "receiver": None, "rows": []}
    tracks = data.get("tracks") or {}
    for icao, track in tracks.items():
        pts = track["pts"]
        if not pts:
            continue
        alts = [p[3] for p in pts]
        view["rows"].append((track["callsign"] or icao, icao, len(pts), min(alts), max(alts),
                             altitude_colour(max(alts))))
    view["rows"].sort(key=lambda row: (-row[2], row[1]))
    bounds = data.get("bounds")
    if not bounds:
        return view
    min_lat, min_lon, max_lat, max_lon = bounds
    receiver = data.get("receiver")
    ring_geo = []
    if receiver:
        # same geometry as adsb_tracks.write_kml(): one vertex per sector
        sectors = adsb_tracks.SECTORS
        for s in range(sectors + 1):
            d = data["coverage"][s % sectors]
            brg = math.radians((s % sectors + 0.5) * 360 / sectors)
            dlat = d / 111.32 * math.cos(brg)
            dlon = d / (111.32 * max(0.01, math.cos(math.radians(receiver[0])))) * math.sin(brg)
            ring_geo.append((receiver[0] + dlat, receiver[1] + dlon))
        min_lat = min([min_lat] + [p[0] for p in ring_geo])
        max_lat = max([max_lat] + [p[0] for p in ring_geo])
        min_lon = min([min_lon] + [p[1] for p in ring_geo])
        max_lon = max([max_lon] + [p[1] for p in ring_geo])
    mid_lat, mid_lon = (min_lat + max_lat) / 2.0, (min_lon + max_lon) / 2.0
    kx = max(0.01, math.cos(math.radians(mid_lat)))
    span_x = max((max_lon - min_lon) * kx, 1e-6)
    span_y = max(max_lat - min_lat, 1e-6)
    scale = min((pw - 16) / span_x, (ph - 16) / span_y)
    cx, cy = px + pw / 2.0, py + ph / 2.0

    def to_xy(lat, lon):
        return (int(round(cx + (lon - mid_lon) * kx * scale)),
                int(round(cy - (lat - mid_lat) * scale)))

    total = sum(len(track["pts"]) for track in tracks.values())
    stride = max(1, int(math.ceil(total / float(max(1, max_points)))))
    for icao, track in sorted(tracks.items()):
        pts = track["pts"]
        if not pts:
            continue
        kept = pts[::stride]
        if kept[-1] is not pts[-1]:
            kept.append(pts[-1])
        line = []
        for p in kept:
            xy = to_xy(p[1], p[2])
            if not line or line[-1] != xy:
                line.append(xy)
        view["lines"].append((altitude_colour(max(p[3] for p in pts)), line))
    if receiver:
        view["receiver"] = to_xy(receiver[0], receiver[1])
        if data.get("max_km", 0) > 0:
            view["ring"] = [to_xy(lat, lon) for lat, lon in ring_geo]
    return view


class NgAdsbTracks(PluginBase):
    NAME = "ADS-B Tracks + KML (WDGoNG)"
    VERSION = "1.0"
    AUTHOR = "LOCOSP (WDGoNG 0.10.0); plugin port for WDG-Bristle"

    # Ticked with its screen closed (app.py: _tick_background_plugins), so the
    # receiver position is logged for as long as the ADS-B radar runs.
    BACKGROUND_TICK = True

    def __init__(self):
        super().__init__()
        self.has_overlay = True
        self._overlay_active = False
        self._events = collections.deque()   # (hud, colour, terminal) from the worker
        self._busy = False                   # one read/export at a time
        self._busy_lock = threading.Lock()
        self._thread = None
        self._status = "Press R to read this session's ADS-B log."
        self._status_colour = C_DIM
        self._data = None                    # adsb_tracks.load() result
        self._view = None                    # build_view() result
        self._session_name = ""
        self._kml_path = ""
        self._scroll = 0
        self._rx_next = 0.0
        self._rx_rows = 0
        self._rx_error_said = False

    # ------------------------------------------------------------------
    # Menu / overlay plumbing
    # ------------------------------------------------------------------

    def menu_items(self) -> list[PluginMenuItem]:
        return [
            PluginMenuItem("a", "ADS-B: Tracks & Coverage", "open_overlay"),
            PluginMenuItem("x", "ADS-B: Export KML (session)", "export_kml"),
        ]

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    def open_overlay(self):
        self._overlay_active = True
        self._scroll = 0
        self.reload()

    def _session_dir(self) -> str:
        loot = getattr(self.app, "loot", None)
        if loot is None or not getattr(loot, "active", False):
            return ""
        try:
            return str(loot.session_dir)
        except Exception:
            return ""

    def _current_fix(self):
        """A fresh measured GPS position (lat, lon), or None. Never a cached one."""
        gps = getattr(self.app, "gps", None)
        sample = getattr(gps, "wardrive_sample", None)
        if callable(sample):
            try:
                value = sample()
            except Exception:
                value = None
            if value:
                return float(value["lat"]), float(value["lon"])
            return None
        fix = getattr(gps, "fix", None)       # a GPS object without the sample API
        if getattr(self.app, "gps_fix", False) and getattr(fix, "valid", False):
            lat, lon = getattr(fix, "latitude", 0.0), getattr(fix, "longitude", 0.0)
            if lat or lon:
                return float(lat), float(lon)
        return None

    def _adsb_running(self) -> bool:
        sdr = getattr(self.app, "_sdr", None)
        return bool(sdr is not None and getattr(sdr, "running", False)
                    and "adsb" in str(getattr(sdr, "mode", "")))

    # ------------------------------------------------------------------
    # Receiver position log (WDGoNG backend._adsb_rx_log)
    # ------------------------------------------------------------------

    def _log_receiver(self, now=None) -> bool:
        """While upstream's ADS-B radar runs with a GPS fix, note where the receiver
        is every 30 s in the session's adsb_receiver.csv. One short append."""
        now = time.time() if now is None else now
        if now < self._rx_next:
            return False
        self._rx_next = now + RX_LOG_INTERVAL
        if not self._adsb_running():
            return False
        session = self._session_dir()
        fix = self._current_fix()
        if not session or fix is None:
            return False
        path = os.path.join(session, adsb_tracks.RX_FILE)
        try:
            new = not os.path.exists(path)
            with open(path, "a", encoding="utf-8") as fh:
                if new:
                    fh.write("timestamp,lat,lon\n")
                fh.write("%d,%.6f,%.6f\n" % (int(now), fix[0], fix[1]))
        except OSError as exc:
            if not self._rx_error_said:       # say it once, not every 30 s
                self._rx_error_said = True
                self.term(f"[ADS-B] cannot write {adsb_tracks.RX_FILE}: {exc}")
            return False
        self._rx_rows += 1
        return True

    # ------------------------------------------------------------------
    # Worker: read the session log / write the KML
    # ------------------------------------------------------------------

    def _start(self, target, *args, status=None) -> bool:
        """Run ``target`` on the worker thread. ``status`` ("Reading...") is
        shown from now until the worker sets its own.

        [merge-v3] It is set HERE, before the worker exists. It used to be set
        by the caller after this returned; a worker that was already finished
        by then (no log to read: nothing to do) had its result overwritten,
        and the screen said "Reading the ADS-B log..." until the next reload."""
        with self._busy_lock:
            if self._busy:
                return False
            self._busy = True
            if status:
                self._set_status(*status)
        thread = threading.Thread(target=self._job, args=(target, args),
                                  name="ng-adsb-tracks", daemon=True)
        self._thread = thread
        try:
            thread.start()
        except RuntimeError:
            with self._busy_lock:
                self._busy = False
            return False
        return True

    def _job(self, target, args):
        try:
            target(*args)
        except Exception as exc:
            self._post("[ADS-B] tracks error (see terminal)", C_ERR, f"[ADS-B] tracks error: {exc}")
            self._set_status(f"Error: {exc}", C_ERR)
        finally:
            with self._busy_lock:
                self._busy = False

    def _post(self, hud=None, colour=C_TEXT, term=None):
        self._events.append((hud, colour, term))

    def _set_status(self, text, colour=C_DIM):
        self._status, self._status_colour = text, colour

    def _drain_events(self):
        while self._events:
            hud, colour, term = self._events.popleft()
            if term:
                self.term(term)
            if hud:
                self.msg(hud, colour)

    def _load(self, session, fix):
        """Worker thread. Returns the loaded data, or None when there is no log."""
        if not os.path.isfile(os.path.join(session, adsb_tracks.AC_FILE)):
            self._data = self._view = None
            self._set_status("No ADS-B log in this session yet (start SDR > ADS-B Radar).", C_WARN)
            return None
        data = adsb_tracks.load(session, current_fix=fix)
        view = build_view(data)
        self._session_name = os.path.basename(os.path.normpath(session))
        self._data, self._view = data, view       # swapped in whole, read by draw()
        if not data["n_aircraft"]:
            self._set_status("The ADS-B log has no aircraft positions yet.", C_WARN)
        else:
            self._set_status("%d aircraft, %d positions read." % (data["n_aircraft"], data["n_points"]),
                             C_OK)
        return data

    def reload(self):
        """Read the current session's tracks in the background."""
        session = self._session_dir()
        if not session:
            self._set_status("No loot session is open.", C_WARN)
            return
        # a False return = a read/export is already running; its status stays
        self._start(self._load, session, self._current_fix(), status=("Reading the ADS-B log...", C_TITLE))

    def _export(self, session, fix):
        data = self._load(session, fix)
        if data is None:
            self._post("[ADS-B] No ADS-B log in this session yet", C_WARN)
            return
        if not data["n_aircraft"]:
            self._post("[ADS-B] No aircraft positions to export yet", C_WARN)
            return
        path = adsb_tracks.write_kml(session, data)
        self._kml_path = path
        name = "%s/%s" % (self._session_name, os.path.basename(path))
        self._set_status("KML saved: loot/%s" % name, C_OK)
        self._post("[ADS-B] KML saved: %s" % name, C_OK,
                   "[ADS-B] KML saved: %s (%d aircraft, %d positions, max range %.0f km)"
                   % (path, data["n_aircraft"], data["n_points"], data["max_km"]))

    def export_kml(self):
        """PLUGINS menu / E key: write loot/<session>/adsb_tracks.kml."""
        session = self._session_dir()
        if not session:
            self.msg("[ADS-B] No loot session is open", C_WARN)
            return
        if not self._start(self._export, session, self._current_fix(), status=("Writing the KML...", C_TITLE)):
            self.msg("[ADS-B] Still reading the log -- try again in a moment", C_WARN)
            return
        self.msg("[ADS-B] Exporting KML in the background...", C_TEXT)

    # ------------------------------------------------------------------
    # Per-frame update
    # ------------------------------------------------------------------

    def on_update(self) -> None:
        self._drain_events()
        self._log_receiver()
        if not self._overlay_active:
            return
        import pyxel
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._overlay_active = False
            if self.app:
                self.app._esc_consumed_frame = pyxel.frame_count
            return
        rows = len(self._view["rows"]) if self._view else 0
        if pyxel.btnp(pyxel.KEY_UP, 12, 2) and self._scroll > 0:
            self._scroll -= 1
        if pyxel.btnp(pyxel.KEY_DOWN, 12, 2) and self._scroll < max(0, rows - LIST_ROWS):
            self._scroll += 1
        if pyxel.btnp(pyxel.KEY_R):
            self.reload()
        if pyxel.btnp(pyxel.KEY_E):
            self.export_kml()

    # ------------------------------------------------------------------
    # Draw (640x360, pyxel 16 colours, upstream overlay style)
    # ------------------------------------------------------------------

    def draw(self, x: int, y: int, w: int, h: int) -> None:
        if not self._overlay_active:
            return
        import pyxel
        pyxel.rect(0, 0, w, h, C_BG)
        pyxel.rect(0, 0, w, 12, C_BAR)
        live = "RADAR ON" if self._adsb_running() else "radar off"
        pyxel.text(4, 3, f"ADS-B TRACKS - {self._session_name or 'this session'}", C_TITLE)
        pyxel.text(w - 4 - FONT_W * len(live), 3, live, C_OK if live == "RADAR ON" else C_DIM)

        data, view = self._data, self._view
        px, py, pw, ph = PLOT
        pyxel.rectb(px, py, pw, ph, C_BORDER)
        ty = 18
        pyxel.text(6, ty, self._status[:64], self._status_colour)
        ty += 12
        if data:
            source = {"gps-log": "GPS log of this session", "wardriving": "wardriving fixes",
                      "gps-now": "GPS now", "estimate": "estimated from the aircraft",
                      "": "unknown"}.get(data["rx_source"], data["rx_source"])
            for label, value in (
                    ("Aircraft", str(data["n_aircraft"])),
                    ("Positions", str(data["n_points"])),
                    ("Max range", "%.0f km  (%s)" % (data["max_km"], data["max_icao"] or "-")),
                    ("Receiver", source),
                    ("RX log", "%d position(s) added this run" % self._rx_rows),
                    ("KML", os.path.basename(self._kml_path) if self._kml_path else "not exported (E)")):
                pyxel.text(6, ty, label, C_DIM)
                pyxel.text(62, ty, value[:52], C_TEXT)
                ty += 8
            ty += 6
            pyxel.text(6, ty, "CALLSIGN  ICAO     PTS   ALT ft (min-max)", C_DIM)
            ty += 8
            rows = view["rows"] if view else []
            for label, icao, count, low, high, colour in rows[self._scroll:self._scroll + LIST_ROWS]:
                pyxel.text(6, ty, "%-9s %-6s %5d   %d-%d" % (label[:9], icao[:6], count, low, high), colour)
                ty += 8
            if len(rows) > LIST_ROWS:
                pyxel.text(6, ty + 2, "%d-%d of %d" % (self._scroll + 1,
                                                       min(len(rows), self._scroll + LIST_ROWS),
                                                       len(rows)), C_DIM)
        if view:
            pyxel.clip(px + 1, py + 1, pw - 2, ph - 2)
            ring = view["ring"]
            for a, b in zip(ring, ring[1:]):
                pyxel.line(a[0], a[1], b[0], b[1], C_COVER)
            for colour, line in view["lines"]:
                if len(line) == 1:
                    pyxel.pset(line[0][0], line[0][1], colour)
                for a, b in zip(line, line[1:]):
                    pyxel.line(a[0], a[1], b[0], b[1], colour)
            if view["receiver"]:
                rx, ry = view["receiver"]
                pyxel.line(rx - 4, ry, rx + 4, ry, C_TEXT)
                pyxel.line(rx, ry - 4, rx, ry + 4, C_TEXT)
            pyxel.clip()
            legend = (("<10k", 9), ("<20k", 10), ("<30k", 11), ("<40k", 12), ("40k+", 14))
            lx = px + 4
            for text, colour in legend:
                pyxel.text(lx, py + ph - 10, text, colour)
                lx += FONT_W * len(text) + 6
            pyxel.text(lx + 2, py + ph - 10, "ft  + receiver  - coverage", C_DIM)
        elif not self._busy:
            pyxel.text(px + 60, py + ph // 2, "no tracks to draw", C_DIM)
        if self._busy:
            pyxel.text(px + 8, py + 8, "working...", C_WARN)
        pyxel.text(4, h - 12, "[R] Reload  [E] Export KML  [UP/DOWN] Scroll  [ESC] Back", C_DIM)
