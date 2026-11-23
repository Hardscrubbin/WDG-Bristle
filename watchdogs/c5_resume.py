# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""[bristle] An interrupted C5/SOLO firmware migration: what state the board is in, and finishing it.

A migration reads the board twice in full (the device backups), then writes one application image
at 0x20000. Until WDG-Bristle 1.2 nothing on disk said how far a migration had got, so after a
power cut or a crash the game could not tell "the board was never written" from "the write stopped
half way", and a board whose application was half written could not be written again from the
game: it no longer answers, and the installer only starts from a fresh answer.

This module keeps a small journal next to the device backup and reads it back:

    write-started     the write of the application image began
    write-complete    esptool finished the write; the board had not answered yet
    confirmed         the board answered with the new version
    closed            the board answered with another version (it was put back by hand, or the
                      write never reached it): nothing is left to finish

``find`` turns what is on disk into one of five plain states (``Pending.kind``):

    before-write        only reads were done. The board was not changed.
    cut-off             a leftover download folder of a run that was cut off, and no write on
                        record for it.
    write-interrupted   the write began and did not finish. The board will not start its firmware
                        until the image is written again. Only the application area was touched.
    write-unconfirmed   the write finished; the board's answer was never seen.
    no-record           a complete device backup with no journal (made by a build before 1.2, or
                        the game stopped between the backup and the write).

``check_resume`` is the gate for finishing the write from the Firmware screen: the same approved
image, the same board (by its USB serial number), and the device backup still intact. The write
itself is the installer's own, with its read-only checks of the board first.

No drawing and no game object here. Nothing in this module talks to a board.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import legacy_names

JOURNAL = "write-journal.json"
BACKUP = "backup.json"
STEPS_FILE = "HOW-TO-FINISH.txt"
ANSWERED = "board-answered.json"             # in the recovery folder: when the board last reported a version
STARTED, WRITTEN, CONFIRMED, CLOSED = "write-started", "write-complete", "confirmed", "closed"
BEFORE_WRITE, CUT_OFF, INTERRUPTED, UNCONFIRMED, NO_RECORD = (
    "before-write", "cut-off", "write-interrupted", "write-unconfirmed", "no-record")
INSTALL_FOLDER = "c5-install"                # the journal of an ordinary install (it has no device backup)
APP_OFFSET = "0x20000"
WIDTH = 80                                   # characters across the Firmware box


@dataclass(frozen=True)
class Pending:
    kind: str
    directory: Path                          # the device backup's folder (or the leftover found)
    when: float = 0.0
    package_version: str = ""
    image_sha256: str = ""
    binding: dict = field(default_factory=dict)      # vid, pid, serial_number, location
    rollback_name: str = ""
    rollback_bytes: int = 0
    rollback_sha256: str = ""

    @property
    def resumable(self) -> bool:
        """True when finishing the write from the game can be offered: a write is on record, or
        there is a complete device backup made for one."""
        return self.kind in (INTERRUPTED, UNCONFIRMED) or (self.kind == NO_RECORD and bool(self.rollback_name))

    @property
    def urgent(self) -> bool:
        """True when the board is known to need attention (a write is on record and unfinished)."""
        return self.kind in (INTERRUPTED, UNCONFIRMED)


def _load(path) -> dict:
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read(1 << 20).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(directory, state: str, package=None, identity=None, now=None) -> bool:
    """Write the journal entry for ``state`` into the backup folder. Returns False, and changes
    nothing, when it cannot be written; it never raises (the migration does not depend on it)."""
    if state not in (STARTED, WRITTEN, CONFIRMED, CLOSED) or not directory:
        return False
    try:
        directory = Path(directory)
        if not directory.is_dir():
            return False
        now = time.time() if now is None else now
        entry = _load(directory / JOURNAL)
        if state == STARTED:
            if entry.get("state") not in (STARTED, WRITTEN):
                entry = {}                   # the write before this one was settled: a new record
            entry = {"schema": 1, "started_at": entry.get("started_at") or now,
                     "attempts": int(entry.get("attempts") or 0) + 1,
                     "target_package": getattr(package, "version", "") or entry.get("target_package", ""),
                     "target_image_sha256": getattr(package, "image_sha256", "") or entry.get("target_image_sha256", ""),
                     "usb_binding": entry.get("usb_binding") or {}}
            if identity is not None:
                entry["usb_binding"] = {"vid": identity.vid, "pid": identity.pid,
                                        "serial_number": identity.serial_number, "location": identity.location}
        elif not entry:
            if state != CLOSED:
                return False                 # nothing was started in this folder
            entry = {"schema": 1}            # a device backup from before the journal existed
        entry["state"], entry["updated_at"] = state, now
        temporary = directory / (JOURNAL + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write((json.dumps(entry, indent=2) + "\n").encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / JOURNAL)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _migration_folders(firmware_cache) -> list:
    """Device-backup folders, newest first, in this build's recovery folder (an earlier install's
    is copied there once by legacy_names.recovery_dir)."""
    root = Path(firmware_cache) / legacy_names.RECOVERY_DIR
    try:
        folders = [path for path in root.iterdir() if path.is_dir() and not path.is_symlink()]
    except OSError:
        return []
    def changed(path):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(folders, key=changed, reverse=True)


def _from_folder(folder: Path):
    backup, journal = _load(folder / BACKUP), _load(folder / JOURNAL)
    try:
        when = folder.stat().st_mtime
    except OSError:
        when = 0.0
    if backup.get("status") != "complete" and not journal:
        try:
            has_reads = any(name.startswith("full-flash-") for name in os.listdir(folder))
        except OSError:
            has_reads = False
        return Pending(BEFORE_WRITE, folder, when) if has_reads else None
    rollback = backup.get("application_rollback") if isinstance(backup.get("application_rollback"), dict) else {}
    if backup.get("status") != "complete":
        rollback = {}                        # an ordinary install: a journal, no device backup
    binding = journal.get("usb_binding") or backup.get("usb_binding")
    common = dict(directory=folder, when=float(journal.get("updated_at") or when),
                  package_version=str(journal.get("target_package") or backup.get("target_package") or ""),
                  image_sha256=str(journal.get("target_image_sha256") or backup.get("target_image_sha256") or ""),
                  binding=binding if isinstance(binding, dict) else {},
                  rollback_name=str(rollback.get("path") or ""), rollback_bytes=int(rollback.get("bytes") or 0),
                  rollback_sha256=str(rollback.get("sha256") or ""))
    state = journal.get("state")
    if state in (CONFIRMED, CLOSED):
        return float(journal.get("updated_at") or when)      # settled, and when
    if state == STARTED:
        return Pending(INTERRUPTED, **common)
    if state == WRITTEN:
        return Pending(UNCONFIRMED, **common)
    return Pending(NO_RECORD, **common) if backup.get("status") == "complete" else None


def board_answered(firmware_cache, version: str) -> str:
    """The board reported ``version``. An unfinished write on record is closed: "confirmed" when
    that is the version that was being written, "closed" when it is another one (the board runs,
    so nothing is left to finish). Returns the state written, or "" when there was nothing open."""
    result = ""
    for folder in _migration_folders(firmware_cache):
        journal = _load(folder / JOURNAL)
        if journal.get("state") in (STARTED, WRITTEN):
            state = CONFIRMED if version and version == journal.get("target_package") else CLOSED
            result = state if record(folder, state) else ""
            break
        if not journal and _load(folder / BACKUP).get("status") == "complete":
            result = CLOSED if record(folder, CLOSED) else ""    # a backup with no record: the board runs
            break
        if journal:
            break                            # the newest one is settled already
    # What has no journal of its own (a leftover download folder, device reads that were cut off)
    # cannot be marked itself; the time of this answer is kept instead, and find() ignores
    # anything of that kind older than it.
    try:
        if Path(firmware_cache).is_dir():
            root = Path(firmware_cache) / legacy_names.RECOVERY_DIR
            root.mkdir(mode=0o700, exist_ok=True)
            with open(root / ANSWERED, "w", encoding="utf-8") as output:
                json.dump({"at": time.time(), "version": str(version)}, output)
    except OSError:
        pass
    return result


def find(firmware_cache, running_workspace=None):
    """The newest unfinished migration under ``firmware_cache``, or None.

    Only the newest device backup counts: an older one is history. A leftover download folder of a
    run that was cut off (flash-*) with no backup newer than it means the run stopped before the
    backups were complete. ``running_workspace`` is this process's own download folder, if a job
    is running now: it is not a leftover."""
    newest, settled = None, 0.0
    for folder in _migration_folders(firmware_cache):
        newest = _from_folder(folder)
        if newest is not None:
            break
    if isinstance(newest, float):            # the newest write on record was settled then
        newest, settled = None, newest
    answered = _load(Path(firmware_cache) / legacy_names.RECOVERY_DIR / ANSWERED).get("at")
    if isinstance(answered, (int, float)):
        settled = max(settled, float(answered))
    if newest is not None and newest.kind in (BEFORE_WRITE, NO_RECORD) and newest.when <= settled:
        newest = None                        # the board has answered since: nothing to say about it
    if newest is None or newest.kind == NO_RECORD:
        try:
            leftovers = [path for path in Path(firmware_cache).iterdir()
                         if path.is_dir() and path.name.startswith("flash-")
                         and (running_workspace is None or path != Path(running_workspace))]
        except OSError:
            leftovers = []
        for path in leftovers:
            try:
                when = path.stat().st_mtime
            except OSError:
                continue
            if when > settled and (newest is None or when > newest.when):
                newest = Pending(CUT_OFF, path, when)
    return newest


def check_resume(pending, package, identity, firmware_error=ValueError) -> None:
    """Raise ``firmware_error`` unless finishing this write is the write that was confirmed before:
    the same approved image, the same board, and its device backup still whole."""
    if not isinstance(pending, Pending) or not pending.resumable:
        raise firmware_error("There is no interrupted write to finish")
    if (not pending.package_version or package is None or package.version != pending.package_version
            or package.image_sha256 != pending.image_sha256):
        raise firmware_error("The interrupted write was for another firmware image; no write")
    serial = str(pending.binding.get("serial_number") or "")
    if (identity is None or not serial or identity.serial_number != serial
            or identity.vid != pending.binding.get("vid") or identity.pid != pending.binding.get("pid")):
        raise firmware_error("This is not the board the interrupted write was for (USB serial differs); no write")
    if not pending.rollback_name:
        return                               # an ordinary install: it never had a device backup
    rollback = pending.directory / pending.rollback_name
    try:
        if (pending.rollback_name != os.path.basename(pending.rollback_name) or rollback.is_symlink()
                or rollback.stat().st_size != pending.rollback_bytes
                or hashlib.sha256(rollback.read_bytes()).hexdigest() != pending.rollback_sha256):
            raise firmware_error("The saved copy of the board's original application is not intact; no write")
    except OSError as exc:
        raise firmware_error("The saved copy of the board's original application cannot be read; no write") from exc


def _wrap(text: str, width: int = WIDTH) -> list:
    words, lines, line = text.split(), [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = word if not line else line + " " + word
    return lines + ([line] if line else [])


def state_lines(pending, board_version: str = "") -> list:
    """What state the board is in, in plain words, for the Firmware screen: [(text, is_heading)]."""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(pending.when)) if pending.when else "an earlier run"
    if pending.kind == BEFORE_WRITE:
        head = "A FIRMWARE INSTALL STOPPED BEFORE IT WROTE ANYTHING (%s)" % when
        body = ["The board was only read. Nothing on it was changed: it still has the firmware it had.",
                "To go on: unplug the board and plug it in again, press R, then M (migrate) or ENTER as before. "
                "The device backups are read again from the start."]
    elif pending.kind == CUT_OFF:
        head = "AN EARLIER FIRMWARE INSTALL WAS CUT OFF (%s)" % when
        body = ["Its download folder was left behind and no write is on record for it: as far as this build "
                "can tell, the board was only read. If the board answers (a version is shown on the Firmware "
                "screen), nothing needs doing.",
                "If the board does not answer: unplug it, plug it in again and press R. If it still does not, "
                "the steps to write it by hand are in OUTPUT."]
    elif pending.kind == INTERRUPTED:
        head = "A FIRMWARE WRITE DID NOT FINISH (%s)" % when
        body = ["The board's application area (from 0x20000) is partly written. The board will not start its "
                "firmware until the image is written again. Its bootloader, partition table and settings were "
                "not touched%s." % (", and its original application is saved in the device backup"
                                    if pending.rollback_name else ""),
                "To finish: plug the board in and press F. If the board is not found, hold its BOOT button "
                "while you plug it in, then press F."]
    elif pending.kind == UNCONFIRMED:
        head = "A FIRMWARE WRITE FINISHED, BUT THE BOARD NEVER ANSWERED (%s)" % when
        body = ["The whole image was written. The game stopped before the board reported its version.",
                "Plug the board in and press R. If it reports JanOS %s, nothing more is needed. If it does "
                "not answer, press F to write the image again." % (pending.package_version or "(the new version)")]
    else:
        head = "A DEVICE BACKUP HAS NO RECORD OF HOW ITS MIGRATION ENDED (%s)" % when
        body = ["It was made by a build before WDG-Bristle 1.2, or the game stopped between the backup and "
                "the write. If the board answers (a version is shown above), nothing needs doing.",
                "If the board does not answer, press F to write the approved image to it again."]
    if board_version and pending.kind not in (BEFORE_WRITE, CUT_OFF):
        body.insert(0, "The board answers now (JanOS %s): it starts its firmware." % board_version)
    lines = [(head, True)]
    for paragraph in body:
        lines += [(line, False) for line in _wrap(paragraph)]
    return lines


def summary_line(pending) -> str:
    """One line for the Firmware screen's main page."""
    return {BEFORE_WRITE: "An install stopped before it wrote anything: the board is unchanged. F: details",
            CUT_OFF: "An earlier install was cut off; no write is on record. F: details",
            INTERRUPTED: "A FIRMWARE WRITE DID NOT FINISH: the board will not start. Press F to finish it",
            UNCONFIRMED: "A write finished but the board never answered. Press R, or F for details",
            NO_RECORD: "A device backup has no record of how its migration ended. F: details"}.get(pending.kind, "")


def hand_steps(pending, install_dir, image_name: str = "", python: str = ".venv/bin/python3", port: str = "PORT") -> list:
    """The exact steps to finish (or undo) by hand, as plain lines. Paths are given from the install
    folder so each command fits a terminal line."""
    install_dir = Path(install_dir)
    try:
        backup = pending.directory.relative_to(install_dir).as_posix()
    except ValueError:
        backup = str(pending.directory)
    image = "firmware/wardriving-solo/%s" % image_name if image_name else "firmware/wardriving-solo/<the .bin there>"
    if pending.kind == BEFORE_WRITE:
        return ["Nothing needs doing by hand: the board was not written. Unplug it, plug it in, start again."]
    steps = ["By hand, in a terminal on the uConsole (the game closed):",
             "1. Unplug the board. Hold its BOOT button, plug it in, let go.",
             "2. Find its port:  ls /dev/ttyACM*     (use it in place of %s below)" % port,
             "3. cd %s" % install_dir.as_posix(),
             "4. Name the tool once, with your port in it:",
             '   T="%s -m esptool --chip esp32c5 -p %s --no-stub"' % (python, port),
             "5. To FINISH the update (writes the approved image, application only):",
             "   $T write-flash %s %s" % (APP_OFFSET, image)]
    if pending.rollback_name:
        steps += ["   OR, to go BACK to what the board had (its saved application, %d bytes):" % pending.rollback_bytes,
                  "   $T write-flash %s %s/%s" % (APP_OFFSET, backup, pending.rollback_name)]
    steps += ["6. Unplug the board and plug it in again. Start the game: SYSTEM > Firmware, press R.",
              "Never erase the whole flash for this: only the application at %s is written." % APP_OFFSET]
    return steps


def write_steps_file(pending, lines) -> str:
    """Put the by-hand steps next to the device backup (HOW-TO-FINISH.txt). Returns the path, or
    "" if it could not be written."""
    try:
        path = Path(pending.directory) / STEPS_FILE
        if not Path(pending.directory).is_dir():
            return ""
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write("\n".join(lines) + "\n")
        return str(path)
    except OSError:
        return ""
