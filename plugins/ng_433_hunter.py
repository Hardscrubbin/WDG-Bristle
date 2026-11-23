"""PLUGINS > 433: Hunter -- fox-hunt a 433 MHz remote with the RTL-SDR.

After WDGoNG 0.10.0's "433 Hunter" / WDG Key Hunter (watchdogs/sdr_manager.py:
start_key_hunt, _key_hunt_reader, keyhunt_learn, keyhunt_reset_target, and the
KEY_HUNT_* constants, calibrated there with real remotes on 2026-11-19).
Required Notice: Copyright (c) 2025-2026 LOCOSP
PolyForm Noncommercial License 1.0.0 (see plugins/_ng/NOTICE). This build is
for personal, noncommercial use.

Receive-only. rtl_power sweeps 433.4-434.6 MHz once a second. Every bin keeps
its own rolling background, so a constant carrier or a neighbour's weather
sensor becomes "normal"; a remote being pressed is a sudden rise above that in
a narrow window around the remote's frequency. The size of the rise is the
"hot" value: the bar fills and the beep gets faster as you get closer. When
the whole band rises at once the remote is right next to the antenna.

Keys   S start / stop     K learn (press the remote once: its frequency
       becomes the target)     J back to the default 434.415 MHz     ESC back
       (the hunt stops: the RTL-SDR is free again for ADS-B / 433 Scanner).

The RTL-SDR can do one job at a time. The hunt refuses to start while the game
is using it (ADS-B Radar, 433 Scanner, 433 Capture, Emergency Radio), and the
game's own SDR entries will find the device busy while a hunt runs.
The learned target is kept in loot/keyhunt_target.json. Each sweep is logged
to <session>/key_hunt.csv in WDGoNG's column layout.
"""
import collections
import json
import os
import shutil
import statistics
import subprocess
import threading
import time
from pathlib import Path

from plugins.plugin_base import PluginBase, PluginMenuItem

FONT_W = 5                      # upstream draws every pyxel.text() with Spleen 5x8 (app.py)
C_BG, C_BAR, C_TITLE, C_TEXT, C_DIM = 0, 1, 3, 7, 13
C_OK, C_WARN, C_ERR, C_BORDER, C_HOT = 11, 10, 8, 5, 9

# WDGoNG's KEY_HUNT_* values, unchanged
HUNT_LO, HUNT_HI, HUNT_BIN = 433_400_000, 434_600_000, 8_000
HUNT_GAIN = "40"
HUNT_THR = 18.0                 # dB over the bin's own background to call a burst
HUNT_HOT_SPAN = 25.0            # dB above the threshold mapped onto 0..1 "hot"
HUNT_TARGET = 434_415_000
HUNT_TARGET_HALF = 20_000
HUNT_BG_ROWS = 20
HUNT_OVERLOAD = 10.0            # median rise of the whole band: the remote is next to you
HUNT_LEARN_MIN = 20.0
HISTORY = 120                   # sweeps of "hot" kept for the strip chart
LOG_HEADER = ("timestamp,peak_freq_mhz,rssi_db,floor_db,jump_db,thr_db,burst,hot,lat,lon,"
              "target_mhz,overload,other_mhz,other_db\n")


class HuntEngine:
    """The detection maths of WDGoNG's _key_hunt_reader, one rtl_power row at
    a time. No I/O."""

    def __init__(self, target_hz=HUNT_TARGET):
        self.target_hz = float(target_hz)
        self.learning = False
        self.learned = None             # set once when a new target was learned
        self.sweeps = self.hits = 0
        self.rssi = self.floor = -999.0
        self.jump = self.hot = 0.0
        self.best = -999.0
        self.peak_mhz = self.other_mhz = self.other_db = 0.0
        self.burst = self.overload = False
        self.last_burst = 0.0
        self.freqs, self.rise = [], []
        self._hist = None

    def feed(self, line: str, now=None) -> bool:
        """Apply one rtl_power CSV row. False when the line is not a row."""
        parts = [item.strip() for item in line.split(",")]
        if len(parts) < 8:
            return False
        try:
            low, step = float(parts[2]), float(parts[4])
            values = [float(item) for item in parts[6:]]
        except ValueError:
            return False
        if step <= 0 or any(value != value for value in values):
            return False
        count = len(values)
        freqs = [low + step * (index + 0.5) for index in range(count)]
        if self._hist is None or len(self._hist) != count:
            self._hist = [collections.deque(maxlen=HUNT_BG_ROWS) for _ in range(count)]
        self.sweeps += 1
        if len(self._hist[0]) < 3:                      # first seconds: learn the background
            for index in range(count):
                self._hist[index].append(values[index])
            self.freqs, self.rise = freqs, [0.0] * count
            return True
        background = [statistics.median(history) for history in self._hist]
        rise = [values[index] - background[index] for index in range(count)]
        window = [i for i in range(count) if abs(freqs[i] - self.target_hz) <= HUNT_TARGET_HALF]
        inside = set(window)
        outside = [i for i in range(count) if i not in inside]
        target = max(window, key=lambda i: rise[i]) if window else max(range(count), key=lambda i: rise[i])
        other = max(outside, key=lambda i: rise[i]) if outside else target
        band = statistics.median(rise)
        self.overload = band > HUNT_OVERLOAD
        self.rssi, self.floor, self.jump = values[target], background[target], rise[target]
        self.peak_mhz = freqs[target] / 1e6
        self.other_mhz, self.other_db = freqs[other] / 1e6, rise[other]
        if self.learning:
            strongest = max(range(count), key=lambda i: rise[i])
            if rise[strongest] >= HUNT_LEARN_MIN and not self.overload:
                self.target_hz = freqs[strongest]
                self.learning = False
                self.learned = self.target_hz
        was = self.burst
        self.burst = self.jump > HUNT_THR or self.overload
        self.best = max(self.best, self.jump)
        if self.overload:
            self.hot = 1.0
        elif self.burst:
            self.hot = max(0.0, min(1.0, (self.jump - HUNT_THR) / HUNT_HOT_SPAN))
        else:
            self.hot = 0.0
        if self.burst:
            self.last_burst = time.time() if now is None else now
            if not was:
                self.hits += 1
        # only quiet rows feed the background, so a held remote never becomes "normal"
        if not self.burst and band < 3.0:
            for index in range(count):
                if rise[index] < 6.0:
                    self._hist[index].append(values[index])
        self.freqs, self.rise = freqs, rise
        return True


class Ng433Hunter(PluginBase):
    NAME = "433 Hunter (WDGoNG)"
    VERSION = "1.0"
    AUTHOR = "LOCOSP (WDGoNG 0.10.0 feature); plugin for WDG-Bristle"

    BACKGROUND_TICK = True      # a hunt that ended by itself is reported with the screen closed

    def __init__(self):
        super().__init__()
        self.has_overlay = True
        self._overlay_active = False
        self.engine = HuntEngine()
        self._proc = None
        self._thread = None
        self._stop = threading.Event()
        self._events = collections.deque()      # (hud or None, colour, terminal or None)
        self._history = collections.deque(maxlen=HISTORY)
        self._status, self._status_colour = "Press S to start. Nothing is transmitted.", C_DIM
        self._log = None
        self._beep_at = 0

    def menu_items(self) -> list[PluginMenuItem]:
        return [PluginMenuItem("f", "433: Hunter (fox hunt)", "open_overlay")]

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    @property
    def hunting(self) -> bool:
        return self._proc is not None

    def sdr_user(self) -> str:
        """For Hardware Check: who has the RTL-SDR."""
        return "433 Hunter" if self.hunting else ""

    def open_overlay(self):
        self._overlay_active = True

    # ------------------------------------------------------------------
    def _target_path(self) -> Path:
        base = getattr(self.app, "_app_dir", "") or str(Path(__file__).resolve().parent.parent)
        return Path(base) / "loot" / "keyhunt_target.json"

    def load_target(self) -> float:
        try:
            value = int(json.loads(self._target_path().read_text()).get("target_hz", 0))
            if HUNT_LO < value < HUNT_HI:
                return float(value)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return float(HUNT_TARGET)

    def save_target(self, hz) -> None:
        try:
            path = self._target_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"target_hz": int(hz), "learned": time.strftime("%Y-%m-%d %H:%M:%S")}))
        except OSError:
            pass

    def command(self) -> list:
        return ["rtl_power", "-f", "%d:%d:%d" % (HUNT_LO, HUNT_HI, HUNT_BIN), "-i", "1", "-g", HUNT_GAIN, "-"]

    def _set_status(self, text, colour=C_DIM) -> None:
        self._status, self._status_colour = text, colour

    def start(self) -> bool:
        if self.hunting:
            return True
        sdr = getattr(self.app, "_sdr", None)
        if getattr(sdr, "running", False):
            self._set_status("The RTL-SDR is busy (%s). Stop that first (ADDONS), then press S."
                             % (getattr(sdr, "mode", "") or "in use"), C_WARN)
            self.msg("[433] RTL-SDR busy -- stop ADS-B / 433 first", C_WARN)
            return False
        if getattr(self.app, "_aio_available", False) and not getattr(self.app, "_sdr_enabled", True):
            self._set_status("The SDR rail is off. Switch it on in SYSTEM > SDR, then press S.", C_WARN)
            return False
        command = self.command()
        if shutil.which(command[0]) is None:
            self._set_status("rtl_power is not installed (sudo apt install rtl-sdr).", C_ERR)
            return False
        try:
            self._proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                          start_new_session=True)
        except OSError as exc:
            self._proc = None
            self._set_status("rtl_power could not start: %s" % exc, C_ERR)
            return False
        self.engine = HuntEngine(self.load_target())
        self._history.clear()
        self._stop.clear()
        self._open_log()
        self._thread = threading.Thread(target=self._reader, args=(self._proc,), name="ng-433-hunter",
                                        daemon=True)
        # [merge-v3] before the reader starts, not after: an rtl_power that
        # exits at once (dongle busy or unplugged) is reported by the reader,
        # and "Hunting..." set afterwards would have covered that up
        self._set_status("Hunting at %.3f MHz. Learning the background for a few seconds..."
                         % (self.engine.target_hz / 1e6), C_OK)
        self._thread.start()
        self.term("[433] Hunter started (receive-only, target %.3f MHz)" % (self.engine.target_hz / 1e6))
        return True

    def stop(self, quiet=False) -> None:
        proc, self._proc = self._proc, None
        self._stop.set()
        if proc is not None:
            for action in (proc.terminate, proc.kill):
                try:
                    action()
                    proc.wait(timeout=2)
                    break
                except (OSError, subprocess.TimeoutExpired):
                    continue
            if not quiet:
                self._set_status("Stopped. %d burst(s) this hunt. The RTL-SDR is free again."
                                 % self.engine.hits, C_DIM)
                self.term("[433] Hunter stopped (%d burst(s))" % self.engine.hits)
        log, self._log = self._log, None
        if log is not None:
            try:
                log.close()
            except OSError:
                pass

    def _open_log(self) -> None:
        loot = getattr(self.app, "loot", None)
        folder = getattr(loot, "session_dir", None) if getattr(loot, "active", True) else None
        if not folder:
            return
        try:
            self._log = open(os.path.join(folder, "key_hunt.csv"), "a", encoding="utf-8")
            if self._log.tell() == 0:
                self._log.write(LOG_HEADER)
        except OSError:
            self._log = None

    def _write_log(self, engine) -> None:
        log = self._log
        if log is None:
            return
        lat = getattr(self.app, "player_lat", 0.0) if getattr(self.app, "gps_fix", False) else 0.0
        lon = getattr(self.app, "player_lon", 0.0) if getattr(self.app, "gps_fix", False) else 0.0
        try:
            log.write("%s,%.4f,%.1f,%.1f,%.1f,%.1f,%d,%.2f,%.6f,%.6f,%.4f,%d,%.4f,%.1f\n" % (
                time.strftime("%Y-%m-%dT%H:%M:%S"), engine.peak_mhz, engine.rssi, engine.floor, engine.jump,
                HUNT_THR, int(engine.burst), engine.hot, lat, lon, engine.target_hz / 1e6,
                int(engine.overload), engine.other_mhz, engine.other_db))
            log.flush()
        except (OSError, ValueError):
            self._log = None            # disk full / closed: stop logging, keep hunting

    def _reader(self, proc) -> None:
        engine = self.engine
        try:
            for line in proc.stdout:
                if self._stop.is_set():
                    break
                if not engine.feed(line):
                    continue
                self._history.append(engine.hot)
                if engine.learned is not None:
                    learned, engine.learned = engine.learned, None
                    self.save_target(learned)
                    self._events.append(("[433] Learned the remote at %.3f MHz" % (learned / 1e6), C_OK,
                                         "[433] Hunter learned the remote at %.3f MHz" % (learned / 1e6)))
                self._write_log(engine)
        except (OSError, ValueError):
            pass
        if not self._stop.is_set() and self._proc is proc:
            # rtl_power ended by itself: dongle unplugged, rail switched off, device busy
            self._proc = None
            self._events.append(("[433] Hunter stopped: rtl_power ended (SDR unplugged or busy?)", C_WARN,
                                 "[433] rtl_power ended by itself after %d sweep(s) -- SDR unplugged, "
                                 "switched off, or in use by another program" % engine.sweeps))
            self._set_status("rtl_power ended by itself (SDR unplugged, off or busy). Press S to try again.",
                             C_WARN)

    # ------------------------------------------------------------------
    def on_update(self) -> None:
        while self._events:
            hud, colour, terminal = self._events.popleft()
            if terminal:
                self.term(terminal)
            if hud:
                self.msg(hud, colour)
        if not self._overlay_active:
            return
        import pyxel
        engine = self.engine
        if self.hunting and engine.hot > 0.0:
            gap = max(3, int(30 - engine.hot * 27))            # closer = faster
            if pyxel.frame_count - self._beep_at >= gap:
                self._beep_at = pyxel.frame_count
                beep = getattr(self.app, "_play_subghz_beep", None)
                if callable(beep):
                    try:
                        beep()
                    except Exception:
                        pass
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._overlay_active = False
            if self.hunting:
                self.stop()
            if self.app:
                self.app._esc_consumed_frame = pyxel.frame_count
            return
        if pyxel.btnp(pyxel.KEY_S):
            if self.hunting:
                self.stop()
            else:
                self.start()
        elif pyxel.btnp(pyxel.KEY_K) and self.hunting:
            engine.learning = True
            self._set_status("Press the remote now, once: the strongest new signal becomes the target.", C_WARN)
        elif pyxel.btnp(pyxel.KEY_J):
            engine.target_hz, engine.learning = float(HUNT_TARGET), False
            self.save_target(HUNT_TARGET)
            self._set_status("Target back to the default %.3f MHz." % (HUNT_TARGET / 1e6), C_DIM)

    def on_unload(self) -> None:
        self.stop(quiet=True)

    # ------------------------------------------------------------------
    # Draw (640x360, pyxel 16 colours, upstream overlay style)
    # ------------------------------------------------------------------

    def draw(self, x: int, y: int, w: int, h: int) -> None:
        if not self._overlay_active:
            return
        import pyxel
        engine = self.engine
        pyxel.rect(0, 0, w, h, C_BG)
        pyxel.rect(0, 0, w, 12, C_BAR)
        pyxel.text(4, 3, "433 HUNTER", C_TITLE)
        state = "HUNTING" if self.hunting else "stopped"
        pyxel.text(w - 4 - FONT_W * len(state), 3, state, C_OK if self.hunting else C_DIM)
        pyxel.text(6, 18, self._status[:124], self._status_colour)

        # the big bar
        bx, by, bw, bh = 6, 36, w - 12, 30
        pyxel.rectb(bx, by, bw, bh, C_BORDER)
        fill = int((bw - 2) * engine.hot)
        if fill > 0:
            pyxel.rect(bx + 1, by + 1, fill, bh - 2, C_ERR if engine.overload else C_HOT if engine.hot > 0.6 else C_WARN)
        label = ("RIGHT HERE" if engine.overload else "%d%%" % int(engine.hot * 100) if engine.burst
                 else "no signal" if self.hunting else "-")
        pyxel.text(bx + bw // 2 - FONT_W * len(label) // 2, by + 11, label, C_TEXT)

        ty = 76
        age = time.time() - engine.last_burst if engine.last_burst else None
        rows = [("Target", "%.3f MHz%s" % (engine.target_hz / 1e6, "   LEARNING: press the remote" if engine.learning else "")),
                ("Signal", "%+.1f dB over background (threshold %.0f)   best this hunt %s"
                 % (engine.jump, HUNT_THR, "%+.1f dB" % engine.best if engine.best > -900 else "-")),
                ("Bursts", "%d   last %s   sweeps %d" % (engine.hits, "%.0f s ago" % age if age is not None else "-",
                                                          engine.sweeps)),
                ("Elsewhere", "strongest other signal %+.1f dB at %.3f MHz" % (engine.other_db, engine.other_mhz)
                 if engine.other_mhz else "-")]
        for label, value in rows:
            pyxel.text(6, ty, label, C_DIM)
            pyxel.text(66, ty, value[:112], C_WARN if label == "Target" and engine.learning else C_TEXT)
            ty += 9

        # spectrum: rise over background per bin, target window marked
        sx, sy, sw, sh = 6, 128, w - 12, 90
        pyxel.text(sx, sy - 10, "SPECTRUM 433.4 - 434.6 MHz (dB over each bin's own background)", C_DIM)
        pyxel.rectb(sx, sy, sw, sh, C_BORDER)
        freqs, rise = engine.freqs, engine.rise
        if freqs:
            span = HUNT_HI - HUNT_LO
            left = sx + 1 + int((engine.target_hz - HUNT_TARGET_HALF - HUNT_LO) / span * (sw - 2))
            right = sx + 1 + int((engine.target_hz + HUNT_TARGET_HALF - HUNT_LO) / span * (sw - 2))
            left, right = max(sx + 1, left), min(sx + sw - 2, right)
            if right > left:
                pyxel.rect(left, sy + 1, right - left, sh - 2, C_BAR)
            threshold = sy + sh - 2 - int((sh - 4) * HUNT_THR / 45.0)
            pyxel.line(sx + 1, threshold, sx + sw - 2, threshold, C_BORDER)
            width = max(1, (sw - 2) // len(freqs))
            for index, value in enumerate(rise):
                px = sx + 1 + int((freqs[index] - HUNT_LO) / span * (sw - 2))
                if not sx < px < sx + sw - width:
                    continue
                height = int((sh - 4) * max(0.0, min(45.0, value)) / 45.0)
                if height > 0:
                    pyxel.rect(px, sy + sh - 2 - height, width, height,
                               C_OK if value > HUNT_THR and left <= px <= right else C_WARN if value > HUNT_THR
                               else C_DIM)
        else:
            pyxel.text(sx + 6, sy + 8, "no sweep yet", C_DIM)

        # strip chart of "hot"
        hx, hy, hw, hh = 6, 240, w - 12, 60
        pyxel.text(hx, hy - 10, "LAST 2 MINUTES (higher = closer)", C_DIM)
        pyxel.rectb(hx, hy, hw, hh, C_BORDER)
        step = (hw - 4) / HISTORY
        previous = None
        for index, value in enumerate(self._history):
            px = hx + 2 + int(index * step)
            py = hy + hh - 3 - int((hh - 6) * value)
            if previous is not None:
                pyxel.line(previous[0], previous[1], px, py, C_HOT if value > 0 else C_DIM)
            previous = (px, py)

        pyxel.text(4, h - 22, "Receive-only. One job per RTL-SDR: stop ADS-B / 433 Scanner / Capture before a hunt.",
                   C_DIM)
        pyxel.text(4, h - 12, "[S] Start/Stop  [K] Learn the remote  [J] Default target  [ESC] Back (stops the hunt)",
                   C_DIM)
