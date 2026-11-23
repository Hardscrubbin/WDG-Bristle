"""433 MHz capture to raw .cu8 + Flipper Zero RAW .sub -- ported from WDGoNG 0.10.0.

Required Notice: Copyright (c) 2025-2026 LOCOSP
PolyForm Noncommercial License 1.0.0 (see plugins/_ng/NOTICE). This build is
for personal, noncommercial use.

What comes from where
---------------------
* plugins/_ng/subghz_export.py is WDGoNG's file, unmodified: it turns one raw
  I/Q recording (.cu8) into a Flipper RAW .sub.
* This plugin is the upstream side. It follows WDGoNG's "433 Capture"
  (watchdogs/sdr_manager.py: start_433_grab, _grab_watcher) and shows it as a
  640x360, 16 colour pyxel screen in upstream's overlay style.

How it hooks into upstream's 433 MHz capture (no app.py or sdr_manager edit)
----------------------------------------------------------------------
upstream's 433 MHz scanner (SDR > 433 MHz Scanner, SDRManager.start_433) runs
rtl_433 and decodes sensors. WDGoNG's capture is the same program with
"-S all" added, which makes rtl_433 also save every detected transmission as
raw I/Q in its working directory. So while a capture is armed, this plugin
wraps the scanner's own process launcher (SDRManager._spawn_owned, on that one
instance) and, for the rtl_433 process only, adds "-S all" and sets the working
directory to loot/<session>/captures_433/<time>/. upstream still owns the process:
its decoding, sensor log, stop/cleanup and power handling are untouched, and
with no capture armed the launcher is passed through unchanged.

A watcher thread then converts each finished .cu8 into a .sub next to it. The
conversion is pure Python over every sample, so it never runs on the game
thread. If a future upstream release renames the launcher, starting a capture says
so and the scanner keeps working as stock.

[merge-v3] When a .sub is written -- and when it is not
-------------------------------------------------------
* Every capture gets a folder of its own. The name is the start time to the
  second; a second capture in the same second gets "_2", "_3"... and never
  adopts a folder that already exists (it used to, and then converted the
  previous capture's recordings a second time).
* Every capture has its own results (_Capture). A capture that is still
  converting its last files cannot write into the next one's list or counters.
* A .sub is made only from a recording that is complete and not empty: the
  .cu8 must not have changed for QUIET_SECONDS (1 s), rtl_433 must no longer
  have it open (checked through /proc on Linux; elsewhere the quiet time
  alone decides), it must hold data, and it must still be unchanged after it
  has been read. That holds after Stop as well: the watcher waits for rtl_433
  to exit and for the quiet time before it converts what is left. An empty
  or still-changing recording gets no .sub; the raw file stays and the list
  says why. So a .sub now appears about a second after its signal ended.
  What cannot be ruled out without /proc: a writer that pauses for more than
  a second in the middle of one recording. rtl_433 writes a recording in one
  go when its signal ends, so that is not expected.
* The .sub is built as <name>.sub.part and renamed when complete, so a .sub
  that exists is always a whole file.

This only records what the receiver hears and writes files. Nothing here
transmits.

PLUGINS menu
------------
* 433: Capture (.cu8 + .sub) Start/Stop
* 433: Captures                              -- the screen (C start/stop, ESC back)
"""

import collections
import os
import shutil
import threading
import time

from plugins.plugin_base import PluginBase, PluginMenuItem
from plugins._ng import subghz_export

CAPTURE_SUBDIR = "captures_433"
WATCH_INTERVAL = 0.5            # seconds between looks at the capture folder
QUIET_SECONDS = 1.0             # a recording must have been unchanged this long before it is converted
SETTLE_INTERVAL = 0.2           # after Stop: seconds between the last looks at each recording
FINAL_WAIT = 6.0                # after Stop: longest wait for rtl_433 to exit and its files to settle
PART_SUFFIX = ".part"           # a .sub in the making; renamed to .sub only when complete
MAX_FILES = 2000                # one capture stops itself after this many signals
MIN_FREE_BYTES = 200 * 1024 * 1024   # ...or when the loot volume is nearly full
LIST_ROWS = 26
FONT_W = 5                      # upstream draws every pyxel.text() with Spleen 5x8 (app.py)

C_BG, C_BAR, C_TITLE, C_TEXT, C_DIM = 0, 1, 3, 7, 13
C_OK, C_WARN, C_ERR = 11, 10, 8


def repair_raw_sub(path, pulses):
    """[merge-v3] Make a .sub written by WDGoNG's converter valid Flipper RAW
    data when it is not. Returns (path or None, number of timings).

    WDGoNG's cu8_to_pulses() folds a glitch shorter than 30 us into the run
    before it but does not join that run with the one after, which has the
    same level. On a noisy recording the file then holds two timings in a row
    with the same sign. Flipper's RAW format requires every value to change
    sign (positive = carrier on) and to start on a positive one. Here
    neighbours of the same sign are added together, a leading or trailing gap
    is dropped, and the file is written again with WDGoNG's own writer.
    WDGoNG's file (plugins/_ng/subghz_export.py) stays unmodified. A file that
    is already valid is not touched. If fewer than two timings remain there is
    no signal: the .sub this plugin just made is removed (the raw .cu8 stays)."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return path, pulses
    header = [line for line in lines if not line.startswith("RAW_Data: ")]
    values = []
    try:
        for line in lines:
            if line.startswith("RAW_Data: "):
                values.extend(int(item) for item in line[len("RAW_Data: "):].split())
        frequency = next(int(line.split(": ", 1)[1]) for line in header if line.startswith("Frequency: "))
    except (ValueError, StopIteration):
        return path, pulses
    merged = []
    for value in values:
        if value == 0:
            continue
        if merged and (merged[-1] > 0) == (value > 0):
            merged[-1] += value
        else:
            merged.append(value)
    while merged and merged[0] < 0:
        merged.pop(0)
    while merged and merged[-1] < 0:
        merged.pop()
    if merged == values:
        return path, pulses
    if len(merged) < 2:
        try:
            os.unlink(path)                    # our own output of a moment ago
        except OSError:
            pass
        return None, 0
    temporary = path + ".tmp"
    subghz_export.write_flipper_sub(merged, frequency, temporary)
    os.replace(temporary, path)
    return path, len(merged)


def new_capture_dir(parent, stamp=None):
    """[merge-v3] Create and return a folder for ONE capture under ``parent``.
    The name is the start time to the second; if that folder exists (a capture
    was already started in this second) the next free "_2", "_3"... is taken.
    os.mkdir -- not makedirs(exist_ok=True) -- so an existing folder, with
    another capture's recordings in it, can never be adopted."""
    stamp = stamp or time.strftime("%Y-%m-%d_%H-%M-%S")
    os.makedirs(parent, exist_ok=True)
    for number in range(1, 1000):
        directory = os.path.join(parent, stamp if number == 1 else "%s_%d" % (stamp, number))
        try:
            os.mkdir(directory)
            return directory
        except FileExistsError:
            continue
    raise OSError("no free capture folder name for " + stamp)


def _file_state(path):
    """(size, modification time) of a recording, or None when it cannot be read."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_size, info.st_mtime_ns


def _remove(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _open_in_process(pid, path):
    """[merge-v3] Does process ``pid`` (rtl_433) still have ``path`` open?
    True / False where the system can tell: Linux lists a process's open files
    in /proc/<pid>/fd. None where it cannot (no /proc, not our process to look
    at, already gone); the caller then relies on the quiet time alone."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    folder = "/proc/%d/fd" % pid
    try:
        names = os.listdir(folder)
        wanted = os.path.realpath(path)
    except OSError:
        return None
    for name in names:
        try:
            if os.readlink(os.path.join(folder, name)) == wanted:
                return True
        except OSError:
            continue                      # that descriptor was closed meanwhile
    return False


class _Capture:
    """[merge-v3] Everything that belongs to ONE capture. Its watcher thread
    writes only here, so a capture that is still converting its last files
    cannot touch the list, the counters or the stop reason of the next one."""

    def __init__(self, directory=""):
        self.directory = directory
        self.rows = []                        # (cu8 name, sub name, pulses, note)
        self.converted = 0                    # .sub files written
        self.skipped = 0                      # recordings that got no .sub
        self.limit = ""                       # set by the watcher: why it wants to stop
        self.stop = threading.Event()
        self.thread = None
        self.proc = None                      # the rtl_433 process recording into the folder


def _of_capture(name):
    """A plugin attribute that lives on the current capture's record."""
    return property(lambda self: getattr(self._capture, name),
                    lambda self, value: setattr(self._capture, name, value))


class NgSubGhzCapture(PluginBase):
    NAME = "433 Capture to Flipper .sub (WDGoNG)"
    VERSION = "1.0"
    AUTHOR = "LOCOSP (WDGoNG 0.10.0); plugin port for WDG-Bristle"

    # Ticked with its screen closed: results are reported and a capture ends
    # cleanly when the scanner is stopped from upstream's own SDR menu.
    BACKGROUND_TICK = True

    def __init__(self):
        super().__init__()
        self.has_overlay = True
        self._overlay_active = False
        self._events = collections.deque()    # (hud, colour, terminal) from the watcher
        self._armed = False                   # the next rtl_433 launch records raw I/Q
        self._spawned = False                 # ...and that launch has happened
        self._capture = _Capture()            # [merge-v3] the capture shown on the screen
        self._hook_owner = None               # the SDRManager our wrapper is installed on
        self._scroll = 0
        self._status = "Capture is off."
        self._status_colour = C_DIM

    # [merge-v3] These were plain attributes shared by every capture. They are
    # now views of the current capture's own record (see _Capture).
    _capture_dir = _of_capture("directory")
    _rows = _of_capture("rows")
    _converted = _of_capture("converted")
    _skipped = _of_capture("skipped")
    _limit = _of_capture("limit")
    _watch_stop = _of_capture("stop")
    _watch_thread = _of_capture("thread")

    # ------------------------------------------------------------------
    # Menu / overlay plumbing
    # ------------------------------------------------------------------

    def menu_items(self) -> list[PluginMenuItem]:
        return [
            PluginMenuItem("c", "433: Capture (.cu8 + .sub) Start/Stop", "toggle_capture"),
            PluginMenuItem("v", "433: Captures", "open_overlay"),
        ]

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    def open_overlay(self):
        self._overlay_active = True

    def capture_active(self) -> bool:
        return self._armed

    def _sdr(self):
        return getattr(self.app, "_sdr", None)

    def _scanner_running(self) -> bool:
        sdr = self._sdr()
        return bool(sdr is not None and getattr(sdr, "running", False)
                    and "433" in str(getattr(sdr, "mode", "")))

    def _set_status(self, text, colour=C_DIM):
        self._status, self._status_colour = text, colour

    def _say(self, text, colour=C_TEXT):
        self._set_status(text, colour)
        self.msg("[433] " + text, colour)
        self.term("[433] " + text)

    # ------------------------------------------------------------------
    # The hook: upstream's own rtl_433 launch, with raw recording added
    # ------------------------------------------------------------------

    def _install_hook(self) -> bool:
        sdr = self._sdr()
        original = getattr(sdr, "_spawn_owned", None)
        if sdr is None or not callable(original):
            return False
        if self._hook_owner is sdr:
            return True
        plugin = self

        def spawn_owned(name, command, **kwargs):
            capture = plugin._capture
            if name == "_rtl433_proc" and plugin._armed and capture.directory:
                command = list(command) + ["-S", "all"]
                kwargs["cwd"] = capture.directory
                plugin._spawned = True
                # [merge-v3] remembered so the watcher can wait for this
                # process to exit before it converts the last recordings
                capture.proc = original(name, command, **kwargs)
                return capture.proc
            return original(name, command, **kwargs)

        sdr._spawn_owned = spawn_owned
        self._hook_owner = sdr
        return True

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def toggle_capture(self):
        if self._armed:
            self.stop_capture()
        else:
            self.start_capture()

    def start_capture(self) -> bool:
        if self._armed:
            return True
        sdr = self._sdr()
        toggle = getattr(self.app, "_sdr_433", None)
        if sdr is None or not callable(toggle):
            self._say("This game build has no 433 MHz scanner", C_ERR)
            return False
        has_rtl433 = getattr(sdr, "has_rtl433", None)
        if callable(has_rtl433) and not has_rtl433():
            self._say("rtl_433 not installed (sudo apt install rtl-433)", C_ERR)
            return False
        loot = getattr(self.app, "loot", None)
        if loot is None or not getattr(loot, "active", False):
            self._say("No loot session is open -- nowhere to save captures", C_ERR)
            return False
        if not self._install_hook():
            self._say("The game's scanner launcher changed; capture is unavailable in this build", C_ERR)
            return False
        try:
            # [merge-v3] always a new folder: see new_capture_dir()
            directory = new_capture_dir(os.path.join(str(loot.session_dir), CAPTURE_SUBDIR))
        except OSError as exc:
            self._say(f"Cannot create the capture folder: {exc}", C_ERR)
            return False
        if self._scanner_running():
            toggle()                      # upstream's own stop; it restarts below with recording
            if self._scanner_running():
                self._discard_empty(directory)
                self._say("The 433 scanner is still stopping -- try again", C_WARN)
                return False
        # [merge-v3] a new record: the previous capture's watcher may still be
        # converting its last files and keeps writing to its own record only
        capture = _Capture(directory)
        self._capture = capture
        self._scroll = 0
        self._spawned = False
        self._armed = True
        toggle()                          # upstream's own start (power, messages, badge)
        if not (self._spawned and self._scanner_running()):
            # upstream said why on the HUD (SDR off, dongle busy, shutdown pending...)
            self._armed = False
            self._capture = _Capture()
            self._discard_empty(directory)
            self._set_status("Capture not started -- see the SDR message.", C_WARN)
            self.term("[433] Capture not started (the 433 scanner did not start)")
            return False
        capture.thread = threading.Thread(target=self._watch, args=(capture,),
                                          name="ng-433-capture", daemon=True)
        capture.thread.start()
        where = os.path.join(os.path.basename(os.path.dirname(os.path.dirname(directory))),
                             CAPTURE_SUBDIR, os.path.basename(directory))
        self._say("Capture ON -> loot/" + where.replace(os.sep, "/"), C_OK)
        return True

    def stop_capture(self, reason="") -> None:
        if not self._armed:
            return
        self._armed = False
        if self._scanner_running():
            toggle = getattr(self.app, "_sdr_433", None)
            if callable(toggle):
                toggle()                  # upstream's own stop; rtl_433 closes its last file
        self._watch_stop.set()            # the watcher converts what is left, then ends
        text = "Capture OFF"
        if reason:
            text += " (" + reason + ")"
        self._say(text + " -- conversions finish in the background",
                  C_WARN if reason else C_TEXT)

    @staticmethod
    def _discard_empty(directory):
        try:
            os.rmdir(directory)           # only ever removes a folder with nothing in it
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Watcher thread (WDGoNG sdr_manager._grab_watcher)
    # ------------------------------------------------------------------

    def _watch(self, capture):
        """Convert each finished .cu8 of ONE capture to a Flipper RAW .sub.
        A recording is taken once it is the same on two looks (rtl_433 is
        still writing otherwise). After stop is set, _finish() deals with what
        is left -- by the same rule, not by taking everything at once."""
        directory, stop = capture.directory, capture.stop
        sizes, done = {}, set()
        while not stop.is_set():
            try:
                self._sweep(directory, sizes, done, False, capture)
            except Exception as exc:      # never let the watcher die silently
                self._events.append((None, C_ERR, f"[433] capture watcher: {exc}"))
            if not capture.limit:
                if len(done) >= MAX_FILES:
                    capture.limit = f"{MAX_FILES} signals recorded"
                else:
                    try:
                        if shutil.disk_usage(directory).free < MIN_FREE_BYTES:
                            capture.limit = "loot storage is nearly full"
                    except OSError:
                        pass
            stop.wait(WATCH_INTERVAL)
        try:
            self._finish(capture, sizes, done)
        except Exception as exc:
            self._events.append((None, C_ERR, f"[433] capture watcher: {exc}"))
        self._events.append((
            None, C_TEXT,
            "[433] Capture finished: %d signal(s), %d .sub written, %d with no usable signal -> %s"
            % (len(done), capture.converted, capture.skipped, directory)))

    def _finish(self, capture, sizes, done):
        """[merge-v3] After Stop. rtl_433 writes a recording when its signal
        ends and may still be writing the last one while upstream stops it, so:
        wait until that process has gone (at most FINAL_WAIT seconds), then
        look at each remaining recording until it is the same on two looks.
        What is still empty or still changing at the end gets no .sub."""
        deadline = time.monotonic() + FINAL_WAIT
        proc = capture.proc
        while proc is not None and time.monotonic() < deadline:
            try:
                if proc.poll() is not None:
                    break
            except Exception:
                break
            time.sleep(0.05)
        # always long enough for a recording seen only now to pass the quiet time
        deadline = max(deadline, time.monotonic() + QUIET_SECONDS + 3 * SETTLE_INTERVAL)
        while True:
            pending = self._sweep(capture.directory, sizes, done, True, capture)
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(SETTLE_INTERVAL)
        for name in pending:
            state = _file_state(os.path.join(capture.directory, name))
            done.add(name)
            capture.skipped += 1
            capture.rows.append((name, "", 0, "empty recording (raw kept)" if state and state[0] == 0
                                 else "still being written at stop: no .sub (raw kept)"))

    def _sweep(self, directory, sizes, done, final, capture=None):
        """One look at the capture folder. Returns the names of the recordings
        that are not finished with yet. A recording is finished when

        * its size and time have not changed since an earlier look that is at
          least QUIET_SECONDS old (``sizes`` remembers what was seen and when),
        * and, where the system can tell (Linux), rtl_433 no longer has the
          file open: that is the exact answer, the quiet time is the fallback.

        Only then is it converted, and only if it holds data. ``final`` (the
        capture has been stopped) closes a recording that has stayed empty;
        while capturing, an empty file is left for a later look."""
        capture = capture or self._capture
        try:
            names = sorted(os.listdir(directory))
        except FileNotFoundError:
            return []
        pending = []
        for name in names:
            if not name.endswith(".cu8") or name in done:
                continue
            path = os.path.join(directory, name)
            state = _file_state(path)
            if state is None:
                continue
            now = time.monotonic()
            seen = sizes.get(name)
            if seen is None or seen[0] != state:
                sizes[name] = (state, now)    # first sight, or it changed: the quiet time starts again
                pending.append(name)
                continue
            if now - seen[1] < QUIET_SECONDS:
                pending.append(name)          # unchanged, but not for long enough yet
                continue
            if _open_in_process(getattr(capture.proc, "pid", None), path):
                pending.append(name)          # rtl_433 still has it open: not finished, however quiet
                continue
            if state[0] == 0:             # nothing in it: never handed to the converter
                if final:
                    done.add(name)
                    capture.skipped += 1
                    capture.rows.append((name, "", 0, "empty recording (raw kept)"))
                else:
                    pending.append(name)
                continue
            try:
                result = self._convert(path, state)
            except Exception as exc:
                done.add(name)
                capture.skipped += 1
                capture.rows.append((name, "", 0, f"failed: {exc}"))
                self._events.append((None, C_ERR, f"[433] {name}: .sub failed ({exc})"))
                continue
            if result is None:            # it changed while it was read: not finished after all
                sizes.pop(name, None)
                pending.append(name)
                continue
            done.add(name)
            out, pulses = result
            if out:
                capture.converted += 1
                capture.rows.append((name, os.path.basename(out), pulses, ""))
                self._events.append((None, C_OK,
                                     f"[433] captured {os.path.basename(out)} ({pulses} pulses)"))
            else:
                capture.skipped += 1
                capture.rows.append((name, "", 0, "no usable signal (raw kept)"))
        return pending

    @staticmethod
    def _convert(path, state):
        """[merge-v3] One .cu8 -> .sub, all or nothing. The Flipper file is
        built as <name>.sub.part and gets its real name only when it is
        complete AND the recording is still exactly what was read (``state``).
        Returns (.sub path or "", timings); None when the recording changed
        while it was read (nothing is left behind, it is looked at again)."""
        target = os.path.splitext(path)[0] + ".sub"
        part = target + PART_SUFFIX
        try:
            out, pulses = subghz_export.convert_cu8(path, out_path=part)
            if out:
                out, pulses = repair_raw_sub(out, pulses)       # see the function
            if _file_state(path) != state:
                _remove(part)
                return None
            if not out:
                _remove(part)
                return "", 0
            os.replace(part, target)
        except BaseException:
            _remove(part)
            raise
        return target, pulses

    # ------------------------------------------------------------------
    # Per-frame update
    # ------------------------------------------------------------------

    def _drain_events(self):
        while self._events:
            hud, colour, term = self._events.popleft()
            if term:
                self.term(term)
            if hud:
                self.msg(hud, colour)

    def on_update(self) -> None:
        self._drain_events()
        if self._armed:
            if self._limit:
                self.stop_capture(self._limit)
            elif self._spawned and not self._scanner_running():
                # stopped from upstream's SDR menu (or the scanner died): end cleanly
                self._armed = False
                self._watch_stop.set()
                self._say("Capture ended (the 433 scanner stopped)", C_WARN)
        if not self._overlay_active:
            return
        import pyxel
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._overlay_active = False
            if self.app:
                self.app._esc_consumed_frame = pyxel.frame_count
            return
        if pyxel.btnp(pyxel.KEY_C):
            self.toggle_capture()
        if pyxel.btnp(pyxel.KEY_UP, 12, 2) and self._scroll > 0:
            self._scroll -= 1
        if pyxel.btnp(pyxel.KEY_DOWN, 12, 2) and self._scroll < max(0, len(self._rows) - LIST_ROWS):
            self._scroll += 1

    def on_unload(self) -> None:
        # The game stops the SDR itself on exit; just let the watcher finish.
        self._armed = False
        self._watch_stop.set()

    # ------------------------------------------------------------------
    # Draw (640x360, pyxel 16 colours, upstream overlay style)
    # ------------------------------------------------------------------

    def draw(self, x: int, y: int, w: int, h: int) -> None:
        if not self._overlay_active:
            return
        import pyxel
        pyxel.rect(0, 0, w, h, C_BG)
        pyxel.rect(0, 0, w, 12, C_BAR)
        pyxel.text(4, 3, "433 MHz CAPTURE - raw .cu8 + Flipper RAW .sub", C_TITLE)
        state = "CAPTURING" if self._armed else "off"
        pyxel.text(w - 4 - FONT_W * len(state), 3, state, C_OK if self._armed else C_DIM)

        ty = 18
        pyxel.text(6, ty, self._status[:124], self._status_colour)
        ty += 12
        rows = list(self._rows)
        folder = self._capture_dir.replace(os.sep, "/")
        marker = folder.find("/loot/")
        shown = folder[marker + 1:] if marker >= 0 else folder
        for label, value in (
                ("Scanner", "running (rtl_433)" if self._scanner_running() else "stopped"),
                ("Folder", shown or "-"),
                ("Signals", "%d recorded, %d .sub written, %d without a usable signal"
                 % (len(rows), self._converted, self._skipped))):
            pyxel.text(6, ty, label, C_DIM)
            pyxel.text(52, ty, value[:114], C_TEXT)
            ty += 8
        ty += 6
        pyxel.text(6, ty, "RECORDING (raw I/Q)", C_DIM)
        pyxel.text(170, ty, "FLIPPER FILE", C_DIM)
        pyxel.text(334, ty, "PULSES", C_DIM)
        ty += 8
        for name, sub, pulses, note in rows[self._scroll:self._scroll + LIST_ROWS]:
            pyxel.text(6, ty, name[:31], C_TEXT)
            if sub:
                pyxel.text(170, ty, sub[:31], C_OK)
                pyxel.text(334, ty, str(pulses), C_TEXT)
            else:
                pyxel.text(170, ty, note[:92], C_WARN)
            ty += 8
        if not rows:
            pyxel.text(6, ty, "Nothing recorded yet. Press C, then trigger the 433 MHz device.", C_DIM)
        elif len(rows) > LIST_ROWS:
            pyxel.text(6, h - 34, "%d-%d of %d" % (self._scroll + 1,
                                                   min(len(rows), self._scroll + LIST_ROWS),
                                                   len(rows)), C_DIM)
        pyxel.text(4, h - 22, "Copy a .sub to the Flipper's SD card (subghz/). Receive only: nothing is"
                              " transmitted from here.", C_DIM)
        pyxel.text(4, h - 12, "[C] Start/Stop capture  [UP/DOWN] Scroll  [ESC] Back", C_DIM)
