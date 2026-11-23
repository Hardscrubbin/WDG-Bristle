"""PLUGINS > Loot: Session Log -- browse every capture session by date and
look inside its files.

After WDGoNG 0.10.0's loot "SESSION LOG journal" (wdgong_ui/main.py loot
screen, wdgong_ui/backend.py session listing / file preview).
Required Notice: Copyright (c) 2025-2026 LOCOSP
PolyForm Noncommercial License 1.0.0 (see plugins/_ng/NOTICE). This build is
for personal, noncommercial use.

Left: the sessions under loot/, newest first, with what each one holds
(Wi-Fi, BLE, handshakes, aircraft, mesh nodes -- from the loot database when
it has the session, otherwise just the files). Right: the selected session's
files with their sizes. ENTER on a file shows its first lines.

Read-only: nothing is deleted, moved, uploaded or changed. (Deleting upload
logs stays in upstream's PLUGINS > Pending Upload Logs, with its safeguards.)
A preview may show captured credentials if the session holds any, exactly as
the files on disk do.

The folder scan and every file read run on a worker thread; the game thread
only draws the last finished result.
"""
import json
import os
import threading
import time
from pathlib import Path

from plugins.plugin_base import PluginBase, PluginMenuItem

FONT_W = 5                      # upstream draws every pyxel.text() with Spleen 5x8 (app.py)
C_BG, C_BAR, C_TITLE, C_TEXT, C_DIM = 0, 1, 3, 7, 13
C_OK, C_WARN, C_ERR, C_BORDER = 11, 10, 8, 5
ROWS = 30                       # list rows that fit
PREVIEW_BYTES = 96 * 1024       # how much of a file is read for a preview
PREVIEW_COLS = 124
MAX_SESSIONS = 2000
MAX_FILES = 400
NOT_SESSIONS = ("kismet_logs",)
COUNT_KEYS = (("wifi", "WiFi"), ("bt", "BLE"), ("hs", "HS"), ("adsb", "ADS-B"), ("meshcore", "MC"),
              ("meshtastic", "MT"), ("passwords", "PWD"))


def human(size) -> str:
    for unit, scale in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if size >= scale:
            return "%.1f %s" % (size / scale, unit)
    return "%d B" % size


def scan(loot_dir) -> list:
    """[dict(name, files=[(relative name, size)], bytes, counts)] newest first."""
    loot_dir = str(loot_dir)
    totals = {}
    try:
        with open(os.path.join(loot_dir, "loot_db.json"), "rb") as fh:
            database = json.loads(fh.read(64 * 1024 * 1024))
        if isinstance(database, dict) and isinstance(database.get("sessions"), dict):
            totals = database["sessions"]
    except (OSError, ValueError):
        totals = {}
    sessions = []
    try:
        names = sorted((n for n in os.listdir(loot_dir)
                        if not n.startswith(".") and n not in NOT_SESSIONS and ".pre-restore-" not in n
                        and os.path.isdir(os.path.join(loot_dir, n)) and not os.path.islink(os.path.join(loot_dir, n))),
                       reverse=True)[:MAX_SESSIONS]
    except OSError:
        return sessions
    for name in names:
        folder = os.path.join(loot_dir, name)
        files, size = [], 0
        for base, dirs, entries in os.walk(folder):
            dirs.sort()
            for entry in sorted(entries):
                path = os.path.join(base, entry)
                try:
                    if os.path.islink(path):
                        continue
                    length = os.path.getsize(path)
                except OSError:
                    continue
                size += length
                if len(files) < MAX_FILES:
                    files.append((os.path.relpath(path, folder).replace(os.sep, "/"), length))
        stored = totals.get(name) if isinstance(totals.get(name), dict) else {}
        counts = [(label, stored[key]) for key, label in COUNT_KEYS
                  if isinstance(stored.get(key), int) and not isinstance(stored.get(key), bool) and stored[key] > 0]
        sessions.append(dict(name=name, files=files, bytes=size, counts=counts))
    return sessions


def preview(path) -> list:
    """The first PREVIEW_BYTES of a file as display lines. Binary files give
    one line saying so."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(PREVIEW_BYTES + 1)
    except OSError as exc:
        return ["cannot read this file: %s" % exc]
    cut = len(data) > PREVIEW_BYTES
    data = data[:PREVIEW_BYTES]
    if b"\x00" in data[:4096]:
        return ["binary file (%s shown as text would be noise) -- not previewed" % human(len(data))]
    lines = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        text = "".join(ch if 32 <= ord(ch) < 127 else "?" if ch != "\t" else " " for ch in line)
        lines.append(text[:PREVIEW_COLS])
    if cut:
        lines.append("... (first %s shown)" % human(PREVIEW_BYTES))
    return lines or ["(empty file)"]


class NgSessionLog(PluginBase):
    NAME = "Loot Session Log (WDGoNG)"
    VERSION = "1.0"
    AUTHOR = "LOCOSP (WDGoNG 0.10.0 feature); plugin for WDG-Bristle"

    def __init__(self):
        super().__init__()
        self.has_overlay = True
        self._overlay_active = False
        self._sessions = None           # None = not scanned yet
        self._busy = False
        self._lock = threading.Lock()
        self._sel = self._file_sel = 0
        self._focus = "sessions"        # "sessions" | "files" | "preview"
        self._preview = []
        self._preview_name = ""
        self._scroll = 0

    def menu_items(self) -> list[PluginMenuItem]:
        return [PluginMenuItem("s", "Loot: Session Log", "open_overlay")]

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    def _loot_dir(self) -> str:
        base = getattr(self.app, "_app_dir", "") or str(Path(__file__).resolve().parent.parent)
        return os.path.join(str(base), "loot")

    def open_overlay(self):
        self._overlay_active = True
        self._focus = "sessions"
        self.rescan()

    def _start(self, target, *args) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True

        def job():
            try:
                target(*args)
            except Exception:
                pass
            finally:
                self._busy = False
        try:
            threading.Thread(target=job, name="ng-session-log", daemon=True).start()
        except RuntimeError:
            self._busy = False
            return False
        return True

    def rescan(self) -> bool:
        return self._start(self._scan)

    def _scan(self) -> None:
        sessions = scan(self._loot_dir())
        self._sessions = sessions
        self._sel = min(self._sel, max(0, len(sessions) - 1))
        self._file_sel = 0

    def _load_preview(self, session, name) -> None:
        folder = os.path.realpath(os.path.join(self._loot_dir(), session))
        path = os.path.realpath(os.path.join(folder, *name.split("/")))
        if os.path.commonpath([folder, path]) != folder:
            self._preview = ["not inside the session folder"]
        else:
            self._preview = preview(path)
        self._preview_name, self._scroll, self._focus = "%s/%s" % (session, name), 0, "preview"

    def _current(self):
        sessions = self._sessions or []
        return sessions[self._sel] if 0 <= self._sel < len(sessions) else None

    # ------------------------------------------------------------------
    def on_update(self) -> None:
        if not self._overlay_active:
            return
        import pyxel
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            if self._focus == "preview":
                self._focus = "files"
            elif self._focus == "files":
                self._focus = "sessions"
            else:
                self._overlay_active = False
            if self.app:
                self.app._esc_consumed_frame = pyxel.frame_count
            return
        session = self._current()
        up = pyxel.btnp(pyxel.KEY_UP, 12, 2)
        down = pyxel.btnp(pyxel.KEY_DOWN, 12, 2)
        page = (ROWS - 2) * (1 if pyxel.btnp(pyxel.KEY_PAGEDOWN, 12, 4) else
                             -1 if pyxel.btnp(pyxel.KEY_PAGEUP, 12, 4) else 0)
        step = (1 if down else -1 if up else 0) + page
        if self._focus == "preview":
            self._scroll = max(0, min(self._scroll + step, max(0, len(self._preview) - ROWS)))
            return
        if pyxel.btnp(pyxel.KEY_R):
            self.rescan()
            return
        if session is None:
            return
        if self._focus == "sessions":
            if step:
                self._sel = max(0, min(self._sel + step, len(self._sessions) - 1))
                self._file_sel = 0
            if pyxel.btnp(pyxel.KEY_RETURN) or pyxel.btnp(pyxel.KEY_RIGHT):
                if session["files"]:
                    self._focus = "files"
        else:
            if step:
                self._file_sel = max(0, min(self._file_sel + step, len(session["files"]) - 1))
            if pyxel.btnp(pyxel.KEY_LEFT):
                self._focus = "sessions"
            elif pyxel.btnp(pyxel.KEY_RETURN) and session["files"]:
                self._start(self._load_preview, session["name"], session["files"][self._file_sel][0])

    # ------------------------------------------------------------------
    # Draw (640x360, pyxel 16 colours, upstream overlay style)
    # ------------------------------------------------------------------

    @staticmethod
    def _window(selected, total):
        return max(0, min(selected - ROWS + 1, total - ROWS)) if total > ROWS else 0

    def draw(self, x: int, y: int, w: int, h: int) -> None:
        if not self._overlay_active:
            return
        import pyxel
        pyxel.rect(0, 0, w, h, C_BG)
        pyxel.rect(0, 0, w, 12, C_BAR)
        pyxel.text(4, 3, "LOOT: SESSION LOG", C_TITLE)
        if self._busy:
            pyxel.text(w - 4 - FONT_W * 7, 3, "reading", C_WARN)
        sessions = self._sessions
        if self._focus == "preview":
            pyxel.text(6, 16, self._preview_name[:124], C_WARN)
            ty = 28
            for line in self._preview[self._scroll:self._scroll + ROWS]:
                pyxel.text(6, ty, line, C_TEXT)
                ty += 9
            more = len(self._preview) - self._scroll - ROWS
            pyxel.text(4, h - 22, "line %d of %d%s" % (self._scroll + 1, len(self._preview),
                                                       "   (%d more below)" % more if more > 0 else ""), C_DIM)
            pyxel.text(4, h - 12, "[UP/DOWN] [PGUP/PGDN] Scroll   [ESC] Back to the files   Read-only", C_DIM)
            return
        if sessions is None:
            pyxel.text(6, 20, "Reading the loot folder...", C_WARN)
            pyxel.text(4, h - 12, "[ESC] Back", C_DIM)
            return
        pyxel.text(6, 16, "SESSIONS (%d, newest first)" % len(sessions), C_DIM)
        pyxel.text(206, 16, "FILES", C_DIM)
        pyxel.line(200, 14, 200, h - 28, C_BORDER)
        if not sessions:
            pyxel.text(6, 28, "no sessions under loot/ yet", C_DIM)
        first = self._window(self._sel, len(sessions))
        ty = 28
        for index in range(first, min(len(sessions), first + ROWS)):
            selected = index == self._sel
            marker = ">" if selected and self._focus == "sessions" else " "
            pyxel.text(6, ty, (marker + " " + sessions[index]["name"])[:28],
                       C_TEXT if selected else C_DIM)
            pyxel.text(150, ty, human(sessions[index]["bytes"])[:9], C_TEXT if selected else C_DIM)
            ty += 9
        session = self._current()
        if session is not None:
            counts = "  ".join("%s:%d" % item for item in session["counts"]) or "no totals in the loot database"
            pyxel.text(246, 16, counts[:78], C_OK if session["counts"] else C_DIM)
            files = session["files"]
            if not files:
                pyxel.text(206, 28, "no files", C_DIM)
            first = self._window(self._file_sel, len(files))
            ty = 28
            for index in range(first, min(len(files), first + ROWS)):
                name, size = files[index]
                selected = index == self._file_sel and self._focus == "files"
                pyxel.text(206, ty, ("> " if selected else "  ") + name[:66], C_TEXT if selected else C_DIM)
                pyxel.text(580, ty, human(size)[:11], C_TEXT if selected else C_DIM)
                ty += 9
        pyxel.text(4, h - 22, "Read-only. A preview shows what is in the file, captured credentials included.",
                   C_DIM)
        pyxel.text(4, h - 12, "[UP/DOWN] Select  [ENTER] Files / preview  [LEFT] Sessions  [R] Rescan  [ESC] Back",
                   C_DIM)
