"""Loot backup / restore to one file -- after WDGoNG 0.10.0's "Backup / Restore".

Required Notice: Copyright (c) 2025-2026 LOCOSP
PolyForm Noncommercial License 1.0.0 (see plugins/_ng/NOTICE). This build is
for personal, noncommercial use.

WDGoNG's feature lives inside its raylib backend (wdgong_ui/backend.py:
export_userdata / import_userdata), so there is no NG file to copy; this
plugin and plugins/_loot_backup.py implement the same feature for upstream's game.
What a backup holds, and how restore differs from WDGoNG (staged in-game,
applied at the next launch, nothing deleted), is described in
plugins/_loot_backup.py.

PLUGINS menu:  Loot: Backup / Restore   -- the screen
    B  back up everything            L  back up without Kismet's .kismet databases
    R  restore the selected backup (then Y to confirm; applied at next start)
    X  cancel a staged restore       UP/DOWN select      ESC back

Backups are written to the home folder of the user who started the game
(~/wdg-bristle-bak-<time>.tar.gz), outside the build folder, so they survive a
reinstall or a re-merge. Every archive read or write runs on a worker thread.
"""

import collections
import os
import shutil
import threading
import time
from pathlib import Path

from plugins.plugin_base import PluginBase, PluginMenuItem
from plugins import _loot_backup as engine
from watchdogs import legacy_names as _legacy  # [bristle] backups made before the rename still list and restore

engine.accept_old_names((_legacy.OLD_BACKUP_KIND,), (_legacy.OLD_BACKUP_PREFIX,))

LIST_ROWS = 13
AUTO_FIRST = 90.0               # s after launch before the first rolling copy of loot_db.json
AUTO_INTERVAL = 600.0           # s between rolling copies
FONT_W = 5                     # upstream draws every pyxel.text() with Spleen 5x8 (app.py)
C_BG, C_BAR, C_TITLE, C_TEXT, C_DIM = 0, 1, 3, 7, 13
C_OK, C_WARN, C_ERR, C_BORDER = 11, 10, 8, 5


def _mb(count) -> str:
    return "%.1f MB" % (count / 1e6)


class NgLootBackup(PluginBase):
    NAME = "Loot Backup / Restore (WDGoNG)"
    VERSION = "1.0"
    AUTHOR = "LOCOSP (WDGoNG 0.10.0 feature); plugin for WDG-Bristle"

    BACKGROUND_TICK = True      # a finished backup is reported with the screen closed

    def __init__(self):
        super().__init__()
        self.has_overlay = True
        self._overlay_active = False
        self._events = collections.deque()      # (hud, colour, terminal) from the worker
        self._busy = False
        self._busy_lock = threading.Lock()
        self._thread = None
        self._cancel = threading.Event()
        self._status, self._status_colour = "", C_DIM
        self._progress = None                   # (files done, files, bytes done, bytes)
        self._sizes = None                      # dict(full=(files, bytes), lite=(files, bytes), free=bytes)
        self._backups = []
        self._sel = 0
        self._confirm = None                    # dict(backup=..., summary=...) awaiting Y
        self._backup_dir = None                 # tests / odd setups may set this
        self._staged = None                     # the request file's content, read off-frame
        self._last = None                       # result of the last applied restore
        self._undo = 0                          # loot.pre-restore-<time> copies on disk
        self._auto_next = time.monotonic() + AUTO_FIRST     # rolling copy of loot_db.json
        self._auto_last = ""                    # name of the newest rolling copy made this run

    # ------------------------------------------------------------------
    # Where things are
    # ------------------------------------------------------------------

    def menu_items(self) -> list[PluginMenuItem]:
        return [PluginMenuItem("b", "Loot: Backup / Restore", "open_overlay")]

    def on_load(self, app) -> None:
        self.app = app
        # A restore applied by this launch (watchdogs/__main__.py) is reported
        # once the game loop runs; nothing is drawn or logged from here.
        try:
            self._last = engine.last_result(self._app_dir())
            if self._last and time.time() - float(self._last.get("when", 0)) < 300:
                restored = self._last.get("status") == "restored"
                text = "[DATA] " + str(self._last.get("message", ""))
                self._events.append((text[:120], C_OK if restored else C_ERR, text))
        except Exception:
            self._last = None

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    def _app_dir(self) -> str:
        return str(getattr(self.app, "_app_dir", "") or Path(__file__).resolve().parent.parent)

    @staticmethod
    def _secrets_dir() -> str:
        return str(Path(__file__).resolve().parent.parent)     # secrets.conf: project root

    def backup_dir(self) -> str:
        if self._backup_dir:
            return str(self._backup_dir)
        try:
            from watchdogs.game_updates import _home
            return str(_home())
        except Exception:
            return str(Path.home())

    def _kismet_capturing(self) -> bool:
        try:
            from watchdogs import merge_build
            return merge_build.kismet_bridge_active(getattr(self.app, "_plugins", ()))
        except Exception:
            return False

    @staticmethod
    def _build() -> str:
        try:
            import watchdogs
            from watchdogs import merge_build
            return merge_build.display_version(watchdogs.__version__)
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Worker plumbing
    # ------------------------------------------------------------------

    def _start(self, target, *args, status=None) -> bool:
        """[merge-v3] ``status`` is set here, before the worker exists: set by
        the caller afterwards, it could overwrite the result of a worker that
        had already finished."""
        with self._busy_lock:
            if self._busy:
                return False
            self._busy = True
            if status:
                self._set_status(*status)
        self._cancel = threading.Event()
        thread = threading.Thread(target=self._job, args=(target, args),
                                  name="ng-loot-backup", daemon=True)
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
        except engine.BackupError as exc:
            self._set_status(str(exc), C_ERR)
            self._events.append(("[DATA] " + str(exc), C_ERR, "[DATA] " + str(exc)))
        except Exception as exc:
            self._set_status(f"Error: {exc}", C_ERR)
            self._events.append(("[DATA] Backup error (see terminal)", C_ERR, f"[DATA] error: {exc}"))
        finally:
            self._progress = None
            with self._busy_lock:
                self._busy = False

    def _set_status(self, text, colour=C_DIM):
        self._status, self._status_colour = text, colour

    def _drain_events(self):
        while self._events:
            hud, colour, term = self._events.popleft()
            if term:
                self.term(term)
            if hud:
                self.msg(hud, colour)

    # ------------------------------------------------------------------
    # Jobs (worker thread)
    # ------------------------------------------------------------------

    def _scan(self, announce=True):
        app_dir, secrets = self._app_dir(), self._secrets_dir()
        full_entries, full_bytes = engine.plan(app_dir, secrets, lite=False)
        lite_entries, lite_bytes = engine.plan(app_dir, secrets, lite=True)
        folder = self.backup_dir()
        try:
            free = shutil.disk_usage(folder).free
        except OSError:
            free = None
        self._sizes = dict(full=(len(full_entries), full_bytes),
                           lite=(len(lite_entries), lite_bytes), free=free)
        self._backups = engine.list_backups(folder)
        self._sel = min(self._sel, max(0, len(self._backups) - 1))
        self._staged, self._last = engine.pending(app_dir), engine.last_result(app_dir)
        self._undo = len(engine.pre_restore_copies(app_dir).get(engine.LOOT, ()))
        if announce:
            self._set_status("Ready. %d backup(s) in %s" % (len(self._backups), folder), C_DIM)

    def _backup(self, lite):
        self._set_status("Backing up...", C_TITLE)

        def progress(done, total, done_bytes, total_bytes):
            self._progress = (done, total, done_bytes, total_bytes)

        summary = engine.create(self._app_dir(), self.backup_dir(), secrets_dir=self._secrets_dir(),
                                lite=lite, build=self._build(), progress=progress, cancel=self._cancel)
        name = os.path.basename(summary["path"])
        text = "Backup saved: ~/%s (%s, %d files)" % (name, _mb(summary["size"]), summary["files"])
        if summary["skipped"]:
            text += ", %d skipped" % len(summary["skipped"])
        self._events.append(("[DATA] Backup saved: " + name, C_OK,
                             "[DATA] backup saved: %s (%s, %d files%s)"
                             % (summary["path"], _mb(summary["size"]), summary["files"],
                                ", includes secrets.conf" if summary["has_secrets"] else "")))
        for item in summary["skipped"][:5]:
            self._events.append((None, C_WARN, "[DATA] not in the backup: " + item))
        self._scan(announce=False)
        self._set_status(text, C_OK)

    def _inspect(self, backup):
        self._set_status("Checking %s ..." % backup["name"], C_TITLE)
        summary = engine.inspect(backup["path"])
        self._confirm = dict(backup=backup, summary=summary)
        self._set_status("Checked. Press Y to restore this backup at the next start, N to leave it.",
                         C_WARN)

    def _stage(self, confirm):
        self._staged = engine.stage(self._app_dir(), confirm["backup"]["path"],
                                    summary=confirm["summary"])
        text = "Restore staged: quit and start the game again to apply it."
        self._set_status(text, C_OK)
        self._events.append(("[DATA] " + text, C_OK,
                             "[DATA] restore of %s staged; it is applied at the next start and the "
                             "current data is kept as loot.pre-restore-<time> (newest %d copies kept, older "
                             "ones deleted then)" % (confirm["backup"]["name"], engine.KEEP_PRE_RESTORE)))

    # ------------------------------------------------------------------
    # Actions (game thread)
    # ------------------------------------------------------------------

    def open_overlay(self):
        self._overlay_active = True
        self._confirm = None
        self.rescan()

    def rescan(self):
        self._start(self._scan, status=("Looking at the loot folder...", C_TITLE))

    def start_backup(self, lite=False) -> bool:
        if self._busy:
            self.msg("[DATA] Busy -- wait for the current step to finish", C_WARN)
            return False
        if not lite and self._kismet_capturing():
            text = "Kismet is capturing: stop it first, or press L to back up without its databases."
            self._set_status(text, C_WARN)
            self.msg("[DATA] Stop Kismet first, or use L (no .kismet files)", C_WARN)
            return False
        self._confirm = None
        if not self._start(self._backup, lite):
            return False
        self.msg("[DATA] Backing up in the background...", C_TEXT)
        return True

    def request_restore(self) -> bool:
        if self._busy or not self._backups:
            return False
        self._confirm = None
        if self._kismet_capturing():
            # Kismet is writing into loot/ right now; a restore staged here
            # would be checked against data that is still changing.
            self._set_status("Kismet is capturing: stop it (PLUGINS > Kismet: Stop) before restoring.", C_WARN)
            self.msg("[DATA] Stop Kismet before restoring", C_WARN)
            return False
        return self._start(self._inspect, self._backups[self._sel])

    def confirm_restore(self) -> bool:
        confirm, self._confirm = self._confirm, None
        if confirm is None or self._busy:
            return False
        return self._start(self._stage, confirm)

    def cancel_staged(self):
        cancelled = engine.cancel_staged(self._app_dir())
        self._staged = None
        if cancelled:
            self._set_status("Staged restore cancelled. Nothing will change at the next start.", C_OK)
            self.term("[DATA] staged restore cancelled")
        else:
            self._set_status("No restore is staged.", C_DIM)

    # ------------------------------------------------------------------
    # Per-frame update
    # ------------------------------------------------------------------

    def _auto_tick(self) -> None:
        """Every AUTO_INTERVAL: a rolling known-good copy of loot_db.json, on
        its own short-lived thread (see _loot_backup.auto_copy_db)."""
        now = time.monotonic()
        if now < self._auto_next:
            return
        self._auto_next = now + AUTO_INTERVAL
        try:
            threading.Thread(target=self._auto_copy, name="ng-loot-autocopy", daemon=True).start()
        except RuntimeError:
            pass

    def _auto_copy(self) -> None:
        made = engine.auto_copy_db(self._app_dir())
        if made:
            self._auto_last = os.path.basename(made)

    def on_update(self) -> None:
        self._drain_events()
        self._auto_tick()
        if not self._overlay_active:
            return
        import pyxel
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._overlay_active = False
            self._confirm = None
            if self.app:
                self.app._esc_consumed_frame = pyxel.frame_count
            return
        if self._confirm is not None:
            if pyxel.btnp(pyxel.KEY_Y):
                self.confirm_restore()
            elif pyxel.btnp(pyxel.KEY_N):
                self._confirm = None
                self._set_status("Restore not staged.", C_DIM)
            return
        if pyxel.btnp(pyxel.KEY_UP, 12, 2) and self._sel > 0:
            self._sel -= 1
        if pyxel.btnp(pyxel.KEY_DOWN, 12, 2) and self._sel < len(self._backups) - 1:
            self._sel += 1
        if pyxel.btnp(pyxel.KEY_B):
            self.start_backup(lite=False)
        elif pyxel.btnp(pyxel.KEY_L):
            self.start_backup(lite=True)
        elif pyxel.btnp(pyxel.KEY_R):
            self.request_restore()
        elif pyxel.btnp(pyxel.KEY_X):
            self.cancel_staged()

    def on_unload(self) -> None:
        # Quitting mid-backup: let the worker stop and remove its unfinished file.
        self._cancel.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(3.0)

    # ------------------------------------------------------------------
    # Draw (640x360, pyxel 16 colours, upstream overlay style)
    # ------------------------------------------------------------------

    def draw(self, x: int, y: int, w: int, h: int) -> None:
        if not self._overlay_active:
            return
        import pyxel
        pyxel.rect(0, 0, w, h, C_BG)
        pyxel.rect(0, 0, w, 12, C_BAR)
        pyxel.text(4, 3, "LOOT BACKUP / RESTORE", C_TITLE)
        if self._busy:
            pyxel.text(w - 4 - FONT_W * 7, 3, "working", C_WARN)

        ty = 18
        pyxel.text(6, ty, self._status[:124], self._status_colour)
        ty += 10
        progress = self._progress
        if progress:
            done, total, done_bytes, total_bytes = progress
            pyxel.rectb(6, ty, 302, 7, C_BORDER)
            fill = int(300 * (done_bytes / total_bytes if total_bytes else done / max(1, total)))
            pyxel.rect(7, ty + 1, max(0, min(300, fill)), 5, C_OK)
            pyxel.text(316, ty + 1, "%d / %d files, %s of %s" % (done, total, _mb(done_bytes),
                                                                 _mb(total_bytes)), C_TEXT)
        ty += 12

        sizes = self._sizes
        staged, last = self._staged, self._last
        rows = [
            ("Your data", "%d files, %s   (without Kismet databases: %s)"
             % (sizes["full"][0], _mb(sizes["full"][1]), _mb(sizes["lite"][1])) if sizes else "..."),
            ("Saved to", "%s   (%s free)" % (self.backup_dir()[-70:],
                                             _mb(sizes["free"]) if sizes and sizes["free"] is not None else "?")),
            ("Staged", ("%s -> applied at the next start  [X] cancel"
                        % os.path.basename(str(staged.get("path", "?")))) if staged else "nothing"),
            ("Last restore", str(last.get("message", "-")) if last else "-"),
            ("Undo copies", "%d on disk (loot.pre-restore-<time>). A restore keeps the newest %d, deletes older."
             % (self._undo, engine.KEEP_PRE_RESTORE)),
            ("Safety copy", "loot_db.json every %d min into loot/%s (newest %d kept)%s"
             % (AUTO_INTERVAL // 60, engine.AUTO_DIR, engine.AUTO_KEEP,
                "; last: " + self._auto_last[8:-5] if self._auto_last else "")),
        ]
        for label, value in rows:
            pyxel.text(6, ty, label, C_DIM)
            pyxel.text(76, ty, value[:110], C_WARN if label == "Staged" and staged else C_TEXT)
            ty += 8
        ty += 6

        pyxel.text(6, ty, "BACKUPS (newest first)", C_DIM)
        pyxel.text(240, ty, "SIZE", C_DIM)
        pyxel.text(310, ty, "WRITTEN", C_DIM)
        ty += 8
        backups = list(self._backups)
        first = max(0, min(self._sel - LIST_ROWS + 1, len(backups) - LIST_ROWS))
        for index in range(first, min(len(backups), first + LIST_ROWS)):
            item = backups[index]
            selected = index == self._sel
            pyxel.text(6, ty, ("> " if selected else "  ") + item["name"][:42], C_TEXT if selected else C_DIM)
            pyxel.text(240, ty, _mb(item["size"]), C_TEXT if selected else C_DIM)
            pyxel.text(310, ty, time.strftime("%Y-%m-%d %H:%M", time.localtime(item["mtime"])),
                       C_TEXT if selected else C_DIM)
            ty += 8
        if not backups:
            pyxel.text(6, ty, "  none yet -- press B to make the first one", C_DIM)

        confirm = self._confirm
        if confirm is not None:
            summary = confirm["summary"]
            bx, by, bw, bh = 80, 196, 480, 102
            pyxel.rect(bx, by, bw, bh, C_BG)
            pyxel.rectb(bx, by, bw, bh, C_WARN)
            pyxel.text(bx + 8, by + 8, "RESTORE THIS BACKUP?", C_WARN)
            pyxel.text(bx + 8, by + 22, confirm["backup"]["name"][:90], C_TEXT)
            xp = "XP %d" % summary["xp"] if summary["xp"] is not None else "XP not recorded"
            pyxel.text(bx + 8, by + 32, "%s, %d session(s), %d files, %s%s"
                       % (xp, summary["sessions"], summary["files"], _mb(summary["bytes"]),
                          ", with secrets.conf" if summary["has_secrets"] else ""), C_TEXT)
            pyxel.text(bx + 8, by + 46, "It replaces the current loot at the NEXT START of the game."
                       if not summary.get("plain") else
                       "Made outside this game (no description); checked the same way. Applied at the NEXT START.",
                       C_TEXT)
            pyxel.text(bx + 8, by + 56, "The current data is kept as loot.pre-restore-<time>.", C_TEXT)
            doomed = max(0, self._undo + 1 - engine.KEEP_PRE_RESTORE)
            pyxel.text(bx + 8, by + 66, "Only the newest %d such copies are kept: this restore DELETES %d older one(s)."
                       % (engine.KEEP_PRE_RESTORE, doomed), C_WARN if doomed else C_TEXT)
            pyxel.text(bx + 8, by + 84, "[Y] Stage the restore    [N] No", C_WARN)

        pyxel.text(4, h - 32, "A backup includes secrets.conf (your API keys). Keep the file private.", C_WARN)
        pyxel.text(4, h - 22, "Not included: maps/ (downloads again). Restore is applied when the game starts.",
                   C_DIM)
        pyxel.text(4, h - 12, "[B] Backup  [L] Backup w/o Kismet databases  [R] Restore selected  "
                              "[X] Cancel staged  [ESC] Back", C_DIM)
