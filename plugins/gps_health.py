"""[merge-v3] PLUGINS > GPS: Health -- one row per GPS receiver, live.

The rig has two receivers behind gpsd: the AIO GP-02 on a UART and a USB
dongle. This screen shows what each one reports (fix, satellites used / seen,
average and best SNR of the satellites in use, HDOP, age of the last report,
antenna status) with per-satellite SNR bars side by side, and marks the one
the game is following. The choice and the alerts are made in
watchdogs/gps_multi.py; this plugin only shows them.

It also runs with the screen closed (BACKGROUND_TICK): source switches and
alerts are put on the HUD and in the terminal once each. Antenna SHORT is the
loud one -- a shorted active-antenna cable can kill a module.

Antenna status is only what the receiver itself says in an NMEA TXT sentence.
A receiver that sends none reads "not reported", never "OK".

Read-only: nothing here switches a receiver, a rail or gpsd.
"""
import collections
import time

from plugins.plugin_base import PluginBase, PluginMenuItem

FONT_W = 5                      # upstream draws every pyxel.text() with Spleen 5x8 (app.py)
C_BG, C_BAR, C_TITLE, C_TEXT, C_DIM = 0, 1, 3, 7, 13
C_OK, C_WARN, C_ERR, C_BORDER, C_WEAK = 11, 10, 8, 5, 9
LEVEL_COLOUR = dict(info=C_TEXT, ok=C_OK, warn=C_WARN, alarm=C_ERR)
LOG_ROWS = 5
MAX_DEVICE_ROWS = 3
BAR_TOP, BAR_HEIGHT, BAR_MAX_DB = 150, 96, 50.0
# column x positions of the receiver table (5 px per character)
COL_MARK, COL_NAME, COL_FIX, COL_SATS, COL_AVG, COL_BEST, COL_HDOP, COL_AGE, COL_ANT = (
    6, 26, 166, 206, 256, 306, 346, 386, 436)


def fix_text(row) -> str:
    if row["silent"] or not row["reporting"]:
        return "SILENT"
    return {3: "3D FIX", 2: "2D FIX"}.get(row["mode"], "NO FIX")


def fmt_db(value) -> str:
    return "-" if value is None else "%.0f dB" % value


def fmt_age(value) -> str:
    if value is None:
        return "never"
    return "%.1f s" % value if value < 100 else "%d s" % value


class GpsHealth(PluginBase):
    NAME = "GPS Health"
    VERSION = "1.0"
    AUTHOR = "WDG-Bristle"

    BACKGROUND_TICK = True      # alerts reach the HUD with the screen closed

    def __init__(self):
        super().__init__()
        self.has_overlay = True
        self._overlay_active = False
        self._log = collections.deque(maxlen=LOG_ROWS)      # (clock text, level, text)
        self._health = None
        self._health_at = 0.0

    def menu_items(self) -> list[PluginMenuItem]:
        return [PluginMenuItem("g", "GPS: Health", "open_overlay")]

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    def open_overlay(self):
        self._overlay_active = True
        self._health_at = 0.0

    # ------------------------------------------------------------------
    def _gps(self):
        return getattr(self.app, "gps", None)

    def _drain(self) -> None:
        drain = getattr(self._gps(), "drain_health_events", None)
        if not callable(drain):
            return
        try:
            events = drain()
        except Exception:
            return
        for level, text in events:
            self._log.append((time.strftime("%H:%M:%S"), level, text))
            self.term(text)
            if level == "alarm":
                self.msg(text[:120], C_ERR)
                self.term("[GPS] !!! " + text.split("] ", 1)[-1].upper())
                sound = getattr(self.app, "_play_svc_sound", None)
                if callable(sound):
                    try:
                        sound("gps-antenna")
                    except Exception:
                        pass
            elif level in ("warn", "ok") or "switched to" in text:
                self.msg(text[:120], LEVEL_COLOUR.get(level, C_TEXT))

    def snapshot(self):
        """The GPS manager's health snapshot, refreshed at most 4 times a second."""
        now = time.monotonic()
        if self._health is None or now - self._health_at >= 0.25:
            health = getattr(self._gps(), "health", None)
            try:
                self._health = health() if callable(health) else None
            except Exception:
                self._health = None
            self._health_at = now
        return self._health

    def on_update(self) -> None:
        self._drain()
        if not self._overlay_active:
            return
        import pyxel
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._overlay_active = False
            if self.app:
                self.app._esc_consumed_frame = pyxel.frame_count

    # ------------------------------------------------------------------
    # Draw (640x360, pyxel 16 colours, upstream overlay style)
    # ------------------------------------------------------------------

    def draw(self, x: int, y: int, w: int, h: int) -> None:
        if not self._overlay_active:
            return
        import pyxel
        pyxel.rect(0, 0, w, h, C_BG)
        pyxel.rect(0, 0, w, 12, C_BAR)
        pyxel.text(4, 3, "GPS HEALTH", C_TITLE)
        health = self.snapshot()
        if health is None:
            pyxel.text(6, 20, "This build's GPS manager has no health data.", C_WARN)
            pyxel.text(4, h - 12, "[ESC] Back", C_DIM)
            return
        source = health["source"]
        right = "source: %s" % (health["endpoint"] if source == "gpsd" else source)
        pyxel.text(w - 4 - FONT_W * len(right[:60]), 3, right[:60], C_OK if source == "gpsd" else C_WARN)

        devices = health["devices"][:MAX_DEVICE_ROWS]
        in_use = next((row for row in devices if row["in_use"]), None)
        line = ("In use: %s    source switches this run: %d"
                % (in_use["label"] if in_use else "none yet", health["switches"]))
        pyxel.text(6, 18, line[:124], C_TEXT)

        ty = 32
        for cx, title in ((COL_NAME, "RECEIVER"), (COL_FIX, "FIX"), (COL_SATS, "USED/SEEN"),
                          (COL_AVG, "AVG SNR"), (COL_BEST, "BEST"), (COL_HDOP, "HDOP"),
                          (COL_AGE, "AGE"), (COL_ANT, "ANTENNA")):
            pyxel.text(cx, ty, title, C_DIM)
        ty += 10
        if not devices:
            text = {"gpsd": "gpsd is connected but no receiver has reported yet.",
                    "serial": "GPS is read straight from a serial port (no gpsd): per-receiver health needs gpsd.",
                    "waiting for gpsd": "gpsd is not answering. The game checks for it every 5 s.",
                    }.get(source, "GPS is off (SYSTEM > GPS).")
            pyxel.text(COL_NAME, ty, text[:120], C_WARN)
        for row in devices:
            fix = fix_text(row)
            fix_colour = C_OK if fix == "3D FIX" else C_WARN if fix == "2D FIX" else C_ERR
            antenna = row["antenna"]
            ant_colour = (C_ERR if antenna == "SHORT" else C_WARN if antenna == "OPEN"
                          else C_OK if antenna == "OK" else C_DIM)
            if row["in_use"]:
                pyxel.text(COL_MARK, ty, ">>>", C_OK)
            pyxel.text(COL_NAME, ty, row["label"][:27], C_TEXT if row["in_use"] else C_DIM)
            pyxel.text(COL_FIX, ty, fix, fix_colour)
            pyxel.text(COL_SATS, ty, "%d/%d" % (row["used"], row["seen"]), C_TEXT)
            pyxel.text(COL_AVG, ty, fmt_db(row["avg_snr"]), C_TEXT)
            pyxel.text(COL_BEST, ty, fmt_db(row["best_snr"]), C_TEXT)
            pyxel.text(COL_HDOP, ty, "-" if row["hdop"] is None else "%.1f" % row["hdop"], C_TEXT)
            pyxel.text(COL_AGE, ty, fmt_age(row["age"]), C_TEXT if row["reporting"] else C_ERR)
            pyxel.text(COL_ANT, ty, antenna, ant_colour)
            pyxel.text(COL_NAME, ty + 9, row["path"][:120], C_DIM)
            ty += 22

        self._draw_bars(pyxel, devices[:2], w)

        ly = h - 26 - LOG_ROWS * 9
        pyxel.text(6, ly - 10, "SWITCHES AND ALERTS (newest last)", C_DIM)
        if not self._log:
            pyxel.text(6, ly, "none since the game started", C_DIM)
        for clock, level, text in self._log:
            pyxel.text(6, ly, ("%s %s" % (clock, text))[:124], LEVEL_COLOUR.get(level, C_TEXT))
            ly += 9
        pyxel.text(4, h - 22, "Antenna = what the receiver itself reports (NMEA TXT). "
                              "'not reported' means it sends no antenna status.", C_DIM)
        pyxel.text(4, h - 12, "[ESC] Back    >>> = receiver in use    green bar = satellite in use, "
                              "grey = seen only", C_DIM)

    def _draw_bars(self, pyxel, devices, w) -> None:
        """Per-satellite SNR bars, the two receivers side by side."""
        panel_w = (w - 18) // 2
        for index, row in enumerate(devices):
            px = 6 + index * (panel_w + 6)
            base = BAR_TOP + BAR_HEIGHT
            pyxel.text(px, BAR_TOP - 22, ("%s%s" % ("> " if row["in_use"] else "", row["label"]))[:40],
                       C_TEXT if row["in_use"] else C_DIM)
            pyxel.text(px, BAR_TOP - 12, "SNR per satellite (dB)", C_DIM)
            pyxel.rectb(px, BAR_TOP - 2, panel_w, BAR_HEIGHT + 4, C_BORDER)
            for level in (20, 40):
                gy = base - int(BAR_HEIGHT * level / BAR_MAX_DB)
                pyxel.line(px + 1, gy, px + panel_w - 2, gy, C_BAR)
                pyxel.text(px + panel_w - 2 - FONT_W * 2, gy - 8, str(level), C_DIM)
            sats = row["sats"]
            if not sats:
                pyxel.text(px + 6, BAR_TOP + 8, "no satellite list reported", C_DIM)
                continue
            usable = panel_w - 8 - FONT_W * 2 - 4
            pitch = max(4, min(16, usable // len(sats)))
            labels = pitch >= 16
            for n, sat in enumerate(sats[:usable // pitch]):
                bx = px + 4 + n * pitch
                snr = sat["snr"] or 0.0
                height = max(1, int(BAR_HEIGHT * min(snr, BAR_MAX_DB) / BAR_MAX_DB))
                colour = C_DIM if not sat["used"] else C_WEAK if snr < 20 else C_OK
                pyxel.rect(bx, base - height, pitch - 2, height, colour)
                if labels and isinstance(sat["prn"], int) and 0 <= sat["prn"] <= 999:
                    pyxel.text(bx, base + 4, str(sat["prn"]), C_DIM)
