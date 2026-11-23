"""One-file backup and restore of the player's data -- the engine.

After WDGoNG 0.10.0's "Backup / Restore" (wdgong_ui/backend.py:
export_userdata, import_userdata).
Required Notice: Copyright (c) 2025-2026 LOCOSP
PolyForm Noncommercial License 1.0.0 (see plugins/_ng/NOTICE). This build is
for personal, noncommercial use.

Standard library only: watchdogs/__main__.py calls apply_pending() before any
game module opens the loot folder. The screen is plugins/ng_loot_backup.py.

What a backup holds (same idea as WDGoNG: everything the player owns)
    loot/            XP, badges, totals, every session and capture
    whitelist.json   if there is one
    secrets.conf     if there is one -- so THE BACKUP CONTAINS YOUR API KEYS.
                     It is written readable by its owner only.
Not included: maps/ (the tile cache downloads again), firmware_cache/, and
Wars Sync's upload bookkeeping in plugins/ (the server recognises what it
already has).

What is different from WDGoNG, on purpose
* WDGoNG swaps the loot folder under the running game and restarts. upstream's game
  holds loot open and saves XP and the database again on exit, so here a
  restore is only STAGED in-game (the archive is checked and a request file
  is written) and APPLIED at the next launch, before the game opens anything.
* The data being replaced is renamed to loot.pre-restore-<time> (WDGoNG keeps
  one undo copy and removes the previous one). Only the newest
  KEEP_PRE_RESTORE (2) such copies of each item are kept: when a restore has
  been applied, older ones are deleted (prune_pre_restore; the restore screen
  says so before the player confirms). Nothing is pruned by a restore that
  failed. Apart from that, the only things this module removes are files it
  created itself: an unfinished .part archive, its request file, and its own
  unpack folder after a restore that was rolled back.
* An archive is checked member by member before a single file is written: no
  absolute or ".." paths, no links or device nodes, nothing outside loot/,
  whitelist.json and secrets.conf, and loot/loot_db.json must be a database
  this game can open. A failed apply puts every renamed item back.
"""

import io
import json
import os
import re
import shutil
import tarfile
import time

KIND = "wdg-bristle-backup"
FORMAT = 1
MANIFEST = "wdg-backup.json"
PREFIX = "wdg-bristle-bak-"
SUFFIX = ".tar.gz"
REQUEST = ".wdg-restore-request.json"
RESULT = ".wdg-restore-result.json"
STAGING_PREFIX = ".wdg-restore-staging-"
PRE_RESTORE = ".pre-restore-"
# archives made elsewhere that the restore screen also lists (see inspect())
PLAIN_PREFIXES = ("wdg-v2-backup-", "wdgo-backup-")
# [bristle] Backups written before this build was renamed carry another kind and file name, and
# are listed and restored exactly like new ones. This engine imports nothing from the game, so
# whoever uses it hands those names in (plugins/ng_loot_backup.py and watchdogs/__main__.py do,
# from watchdogs/legacy_names.py).
OLD_KINDS = ()
OLD_PREFIXES = ()


def accept_old_names(kinds=(), prefixes=()) -> None:
    global OLD_KINDS, OLD_PREFIXES
    OLD_KINDS, OLD_PREFIXES = tuple(kinds), tuple(prefixes)
KEEP_PRE_RESTORE = 2          # undo copies kept per item; older ones are pruned
_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}")

LOOT = "loot"
SINGLE_FILES = ("whitelist.json", "secrets.conf")
MAX_DB_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
SPACE_MARGIN = 50 * 1024 * 1024
# "Lite" leaves out Kismet's own capture databases: usually most of the bytes,
# never read by the game, and unsafe to copy while Kismet is writing them.
LITE_SKIP_DIR = "kismet_logs"
LITE_SKIP_SUFFIXES = (".kismet", ".kismet-journal")


class BackupError(Exception):
    """A backup or restore step that cannot go ahead; the message is for the player."""


def stamp(now=None) -> str:
    return time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(now))


def backup_name(now=None) -> str:
    return PREFIX + stamp(now) + SUFFIX


def _lite_skips(rel_parts) -> bool:
    return (len(rel_parts) >= 2 and rel_parts[0] == LITE_SKIP_DIR
            and rel_parts[-1].lower().endswith(LITE_SKIP_SUFFIXES))


def plan(app_dir, secrets_dir=None, lite=False):
    """What a backup of ``app_dir`` would contain.

    Returns (entries, total_bytes) with entries = [(source path, archive name)].
    Symbolic links are never followed or stored."""
    app_dir = os.path.abspath(app_dir)
    secrets_dir = os.path.abspath(secrets_dir or app_dir)
    entries, total = [], 0
    loot = os.path.join(app_dir, LOOT)
    if os.path.isdir(loot) and not os.path.islink(loot):
        for folder, dirs, files in os.walk(loot):
            dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(folder, d)))
            for name in sorted(files):
                path = os.path.join(folder, name)
                rel = os.path.relpath(path, loot).split(os.sep)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                if lite and _lite_skips(rel):
                    continue
                try:
                    total += os.path.getsize(path)
                except OSError:
                    continue
                entries.append((path, LOOT + "/" + "/".join(rel)))
    for name, base in (("whitelist.json", app_dir), ("secrets.conf", secrets_dir)):
        path = os.path.join(base, name)
        if os.path.isfile(path) and not os.path.islink(path):
            try:
                total += os.path.getsize(path)
            except OSError:
                continue
            entries.append((path, name))
    return entries, total


class _ExactReader:
    """Reads exactly ``size`` bytes for tarfile. The game may be running: if
    a file is cut short after its header was written, the missing tail is
    zero-filled (and reported) instead of leaving a broken archive."""

    def __init__(self, source, size):
        self._source, self._left, self.padded = source, size, 0

    def read(self, count=-1):
        if self._left <= 0:
            return b""
        count = self._left if count is None or count < 0 else min(count, self._left)
        try:
            data = self._source.read(count)
        except OSError:
            data = b""
        if len(data) < count:
            self.padded += count - len(data)
            data += bytes(count - len(data))
        self._left -= len(data)
        return data


def _hand_to_login_user(path) -> None:
    """The game runs under sudo; give the backup to the user who launched it."""
    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0 and os.environ.get("SUDO_UID"):
            uid = int(os.environ["SUDO_UID"])
            os.chown(path, uid, int(os.environ.get("SUDO_GID", uid)))
    except (OSError, ValueError):
        pass


def create(app_dir, out_dir, *, secrets_dir=None, lite=False, build="", progress=None,
           cancel=None, now=None) -> dict:
    """Write <out_dir>/wdg-bristle-bak-<time>.tar.gz. Returns a summary dict.

    The game may be running: a file that disappears or cannot be read while
    it is being added is skipped and listed, never fatal. ``progress(done
    files, total files, done bytes, total bytes)`` is called from this
    thread; ``cancel`` is a threading.Event-like object."""
    entries, total = plan(app_dir, secrets_dir, lite)
    if not any(arc.startswith(LOOT + "/") for _path, arc in entries):
        raise BackupError("There is no loot to back up yet.")
    out_dir = os.path.abspath(out_dir)
    try:
        free = shutil.disk_usage(out_dir).free
    except OSError as exc:
        raise BackupError(f"Cannot use the backup folder: {exc}") from None
    if free < total + SPACE_MARGIN:
        raise BackupError("Not enough free space for the backup: %.0f MB needed, %.0f MB free."
                          % ((total + SPACE_MARGIN) / 1e6, free / 1e6))
    created = time.time() if now is None else now
    target = os.path.join(out_dir, backup_name(created))
    if os.path.exists(target):
        raise BackupError("A backup with this second's name already exists; try again.")
    part = target + ".part"
    manifest = dict(kind=KIND, format=FORMAT, created=int(created), build=str(build),
                    mode="lite" if lite else "full",
                    contains=sorted({arc.split("/", 1)[0] for _path, arc in entries}))
    skipped, done_bytes = [], 0
    try:
        handle = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(handle, "wb") as raw, \
                tarfile.open(fileobj=raw, mode="w:gz", compresslevel=3) as tar:
            data = json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8")
            info = tarfile.TarInfo(MANIFEST)
            info.size, info.mtime, info.mode = len(data), int(created), 0o600
            tar.addfile(info, io.BytesIO(data))
            for index, (path, arcname) in enumerate(entries):
                if cancel is not None and cancel.is_set():
                    raise BackupError("Backup cancelled.")
                if not _safe_name(arcname):
                    skipped.append(arcname)      # a name restore would refuse
                    continue
                try:
                    source = open(path, "rb")    # vanished / unreadable: left out cleanly
                except OSError:
                    skipped.append(arcname)
                    continue
                with source:
                    info = tar.gettarinfo(arcname=arcname, fileobj=source)
                    if info is None or not info.isreg():
                        skipped.append(arcname)
                        continue
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    reader = _ExactReader(source, info.size)
                    tar.addfile(info, reader)
                    done_bytes += info.size
                    if reader.padded:            # truncated while it was being copied
                        skipped.append(arcname + " (changed during the backup)")
                if progress is not None:
                    progress(index + 1, len(entries), done_bytes, total)
        os.replace(part, target)
    except BaseException:
        try:
            os.unlink(part)          # our own unfinished temporary file
        except OSError:
            pass
        raise
    _hand_to_login_user(target)
    return dict(path=target, files=len(entries) - len(skipped), bytes=done_bytes,
                size=os.path.getsize(target), skipped=skipped, mode=manifest["mode"],
                has_secrets="secrets.conf" in manifest["contains"])


def list_backups(out_dir) -> list:
    """Backups in ``out_dir``, newest first: [dict(path, name, size, mtime)]."""
    found = []
    try:
        names = os.listdir(out_dir)
    except OSError:
        return found
    for name in names:
        if name.startswith((PREFIX,) + OLD_PREFIXES + PLAIN_PREFIXES) and name.endswith(SUFFIX):
            path = os.path.join(out_dir, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            if os.path.isfile(path):
                found.append(dict(path=path, name=name, size=info.st_size, mtime=info.st_mtime))
    # this game's own backups first (newest first), then archives made elsewhere
    found.sort(key=lambda item: item["name"], reverse=True)
    found.sort(key=lambda item: not item["name"].startswith((PREFIX,) + OLD_PREFIXES))
    return found


def _safe_name(name) -> bool:
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        return False
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    return (parts[0] == LOOT and len(parts) > 1) or (len(parts) == 1 and parts[0] in SINGLE_FILES)


def inspect(path) -> dict:
    """Check every member of a backup without writing anything.

    Raises BackupError for an archive that must not be restored. Returns
    dict(files, bytes, sessions, xp, mode, build, created, has_secrets,
    has_whitelist)."""
    try:
        archive = tarfile.open(path, "r:gz")
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise BackupError(f"Not a readable backup file: {exc}") from None
    files = total = 0
    manifest = database = None
    seen = set()
    try:
        with archive:
            for member in archive:
                name = member.name
                if name == MANIFEST and manifest is None:
                    if not member.isreg() or member.size > MAX_MANIFEST_BYTES:
                        raise BackupError("The backup's description is damaged.")
                    try:
                        manifest = json.loads(archive.extractfile(member).read().decode("utf-8"))
                    except (ValueError, UnicodeError, AttributeError):
                        raise BackupError("The backup's description is damaged.") from None
                    continue
                if member.isdir():
                    if not (_safe_name(name) or name == LOOT):
                        raise BackupError(f"Unsafe folder in the backup: {name[:80]!r}")
                    continue
                if not member.isreg():
                    raise BackupError(f"The backup holds something that is not a plain file: {name[:80]!r}")
                if not _safe_name(name):
                    raise BackupError(f"Unsafe or unexpected path in the backup: {name[:80]!r}")
                if name in seen:
                    raise BackupError(f"The backup lists a file twice: {name[:80]!r}")
                seen.add(name)
                files += 1
                total += member.size
                if name == LOOT + "/loot_db.json":
                    if member.size > MAX_DB_BYTES:
                        raise BackupError("The backup's loot database is implausibly large.")
                    try:
                        database = json.loads(archive.extractfile(member).read().decode("utf-8"))
                    except (ValueError, UnicodeError, AttributeError):
                        raise BackupError("The backup's loot database cannot be read.") from None
    except (OSError, tarfile.TarError, EOFError) as exc:
        raise BackupError(f"The backup file is damaged: {exc}") from None
    # An archive with no description at all is a "plain" one: made by hand from
    # another build's folder (tar czf ... loot whitelist.json secrets.conf, e.g.
    # the v2 build's data) or by WDGoNG's Export. It is accepted on the strength
    # of the same member checks above plus the database check below. A
    # description that says something else, or a newer format, is still refused.
    plain = manifest is None
    if not plain:
        if not isinstance(manifest, dict) or manifest.get("kind") not in (KIND,) + OLD_KINDS:
            raise BackupError("This file was not made by Loot Backup in this game.")
        if type(manifest.get("format")) is not int or manifest["format"] > FORMAT:
            raise BackupError("This backup was made by a newer build.")
    else:
        manifest = dict(mode="plain", build="", created=None)
    if (not isinstance(database, dict) or type(database.get("version")) is not int
            or not 1 <= database["version"] <= 2 or not isinstance(database.get("sessions"), dict)):
        raise BackupError("The backup has no loot database this game can open.")
    xp = database.get("xp")
    return dict(files=files, bytes=total, sessions=len(database["sessions"]),
                xp=xp if type(xp) is int else None, mode=str(manifest.get("mode", "")),
                build=str(manifest.get("build", "")), created=manifest.get("created"),
                has_secrets="secrets.conf" in seen, has_whitelist="whitelist.json" in seen,
                plain=plain)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _write_json(path, value) -> None:
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=1, sort_keys=True)
    os.replace(temporary, path)


def pending(app_dir):
    """The staged restore request, or None."""
    return _read_json(os.path.join(app_dir, REQUEST))


def last_result(app_dir):
    return _read_json(os.path.join(app_dir, RESULT))


def stage(app_dir, backup_path, summary=None) -> dict:
    """Check ``backup_path`` and ask for it to be restored at the next launch.
    ``summary``: an inspect() result already obtained for this file (the
    archive is checked again in full when the restore is applied)."""
    info = os.stat(backup_path)
    if summary is None:
        summary = inspect(backup_path)
    request = dict(path=os.path.abspath(backup_path), size=info.st_size, mtime_ns=info.st_mtime_ns,
                   staged=int(time.time()), summary=summary)
    _write_json(os.path.join(app_dir, REQUEST), request)
    return request


def cancel_staged(app_dir) -> bool:
    try:
        os.unlink(os.path.join(app_dir, REQUEST))   # our own request file
        return True
    except OSError:
        return False


AUTO_DIR = ".autobackup"      # loot/.autobackup/loot_db-<time>.json
AUTO_KEEP = 3


def auto_copies(app_dir) -> list:
    """Paths of the rolling copies of loot_db.json, oldest first."""
    folder = os.path.join(app_dir, LOOT, AUTO_DIR)
    try:
        names = sorted(name for name in os.listdir(folder)
                       if name.startswith("loot_db-") and name.endswith(".json")
                       and _STAMP_RE.fullmatch(name[len("loot_db-"):-len(".json")]))
    except OSError:
        return []
    return [os.path.join(folder, name) for name in names]


def auto_copy_db(app_dir, now=None):
    """Keep a rolling known-good copy of loot/loot_db.json (after WDGoNG's
    periodic loot-DB safety copy). The database is copied only when it parses
    as a database this game can open and differs from the newest copy; the
    newest AUTO_KEEP copies are kept. Returns the new copy's path, or None
    when nothing was written. Never raises. Call it off the game thread."""
    source = os.path.join(app_dir, LOOT, "loot_db.json")
    try:
        with open(source, "rb") as fh:
            data = fh.read(MAX_DB_BYTES + 1)
        if len(data) > MAX_DB_BYTES:
            return None
        parsed = json.loads(data)
        if not (isinstance(parsed, dict) and isinstance(parsed.get("version"), int)
                and isinstance(parsed.get("sessions"), dict)):
            return None                       # never overwrite good copies with a damaged file
        existing = auto_copies(app_dir)
        if existing:
            with open(existing[-1], "rb") as fh:
                if fh.read(MAX_DB_BYTES + 1) == data:
                    return None
        folder = os.path.join(app_dir, LOOT, AUTO_DIR)
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, "loot_db-%s.json" % stamp(now))
        temporary = target + ".part"
        with open(temporary, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, target)
        for old in auto_copies(app_dir)[:-AUTO_KEEP]:
            try:
                os.unlink(old)                # an older copy this function made itself
            except OSError:
                pass
        return target
    except (OSError, ValueError):
        return None


def damaged_db_hint(app_dir):
    """A sentence for the launcher when loot_db.json cannot be read but a
    rolling copy exists, else None. Nothing is changed."""
    source = os.path.join(app_dir, LOOT, "loot_db.json")
    try:
        with open(source, "rb") as fh:
            json.loads(fh.read(MAX_DB_BYTES + 1))
        return None
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        pass
    copies = auto_copies(app_dir)
    if not copies:
        return None
    newest = copies[-1]
    return ("loot_db.json cannot be read. A good copy from %s exists. To use it, keep the damaged "
            "file and copy the good one over it:\n    cp '%s' '%s.damaged' && cp '%s' '%s'"
            % (os.path.basename(newest)[len("loot_db-"):-len(".json")], source, source, newest, source))


def _restore_items(app_dir, secrets_dir=None):
    """(name, folder it lives in) for everything a restore replaces."""
    app_dir = os.path.abspath(app_dir)
    secrets_dir = os.path.abspath(secrets_dir or app_dir)
    return [(LOOT, app_dir)] + [(name, app_dir if name == "whitelist.json" else secrets_dir)
                                for name in SINGLE_FILES]


def pre_restore_copies(app_dir, secrets_dir=None) -> dict:
    """{item name: [paths of its pre-restore copies, oldest first]}.

    Only names of exactly the form ``<item>.pre-restore-<YYYY-MM-DD_HH-MM-SS>``
    count, i.e. only copies apply_pending() itself set aside."""
    found = {}
    for name, base in _restore_items(app_dir, secrets_dir):
        prefix = name + PRE_RESTORE
        try:
            entries = os.listdir(base)
        except OSError:
            entries = []
        marks = sorted(entry[len(prefix):] for entry in entries
                       if entry.startswith(prefix) and _STAMP_RE.fullmatch(entry[len(prefix):]))
        found[name] = [os.path.join(base, prefix + mark) for mark in marks]
    return found


def prune_pre_restore(app_dir, secrets_dir=None, keep=KEEP_PRE_RESTORE) -> list:
    """Delete all but the newest ``keep`` pre-restore copies of each item.

    Returns the base names removed. Never raises; a copy that cannot be removed
    is left where it is. A link is unlinked, never followed."""
    removed = []
    keep = max(1, int(keep))
    for _name, paths in pre_restore_copies(app_dir, secrets_dir).items():
        for path in paths[:-keep]:
            try:
                if os.path.islink(path) or not os.path.isdir(path):
                    os.unlink(path)
                else:
                    shutil.rmtree(path)
                removed.append(os.path.basename(path))
            except OSError:
                continue
    return removed


def _extract(archive_path, staging) -> None:
    """Write the checked members under ``staging``. Names were validated by
    inspect(); they are validated again here, per file, before each write."""
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            if member.name == MANIFEST or member.isdir():
                continue
            if not member.isreg() or not _safe_name(member.name):
                raise BackupError(f"Unsafe path in the backup: {member.name[:80]!r}")
            target = os.path.join(staging, *member.name.split("/"))
            if os.path.commonpath([os.path.abspath(staging), os.path.abspath(target)]) != os.path.abspath(staging):
                raise BackupError(f"Unsafe path in the backup: {member.name[:80]!r}")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            source = archive.extractfile(member)
            with open(target, "wb") as out:
                shutil.copyfileobj(source, out, 1024 * 1024)
            try:
                os.utime(target, (member.mtime, member.mtime))
                if member.name == "secrets.conf":
                    os.chmod(target, 0o600)
            except OSError:
                pass


def apply_pending(app_dir, secrets_dir=None, log=None, now=None) -> str:
    """Apply a staged restore. Call only while nothing has the loot open.

    Returns "none" (nothing staged: a single file check), "restored" or
    "failed". Never raises. On "failed" the installation is exactly as it was
    (anything already renamed is renamed back) and the request is cleared so
    the game cannot loop on it."""
    app_dir = os.path.abspath(app_dir)
    request_path = os.path.join(app_dir, REQUEST)
    if not os.path.exists(request_path):
        return "none"
    say = log or (lambda _text: None)
    secrets_dir = os.path.abspath(secrets_dir or app_dir)
    mark = stamp(now)
    staging = os.path.join(app_dir, STAGING_PREFIX + mark)
    moved, result = [], dict(when=int(time.time() if now is None else now))
    try:
        request = _read_json(request_path)
        if request is None or not isinstance(request.get("path"), str):
            raise BackupError("The restore request is damaged.")
        source = request["path"]
        result["backup"] = os.path.basename(source)
        try:
            info = os.stat(source)
        except OSError:
            raise BackupError("The backup file is no longer there: " + source) from None
        if (info.st_size, info.st_mtime_ns) != (request.get("size"), request.get("mtime_ns")):
            raise BackupError("The backup file changed after the restore was requested.")
        summary = inspect(source)
        if shutil.disk_usage(app_dir).free < summary["bytes"] + SPACE_MARGIN:
            raise BackupError("Not enough free space to restore (%.0f MB needed)."
                              % ((summary["bytes"] + SPACE_MARGIN) / 1e6))
        say("[DATA] restoring %s ..." % os.path.basename(source))
        os.makedirs(staging)
        _extract(source, staging)
        if not os.path.isfile(os.path.join(staging, LOOT, "loot_db.json")):
            raise BackupError("The backup did not unpack completely.")
        items = _restore_items(app_dir, secrets_dir)
        kept = []
        for name, base in items:
            fresh = os.path.join(staging, name)
            if not os.path.exists(fresh):
                continue                         # not in the backup: leave what is installed
            current = os.path.join(base, name)
            aside = current + PRE_RESTORE + mark
            if os.path.lexists(aside):
                raise BackupError("A pre-restore copy with this name already exists.")
            if os.path.lexists(current):
                os.rename(current, aside)
                moved.append((aside, current))
                kept.append(os.path.basename(aside))
            os.rename(fresh, current)
            moved.append((current, fresh))
        try:
            os.rmdir(staging)                    # empty now; never removes content
        except OSError:
            pass
        result.update(status="restored", kept=kept, xp=summary["xp"], sessions=summary["sessions"],
                      message="Restored %s. Previous data kept as %s."
                              % (os.path.basename(source), ", ".join(kept) or "(nothing to keep)"))
        say("[DATA] " + result["message"])
        outcome = "restored"
        # Applied: keep the newest KEEP_PRE_RESTORE undo copies, drop older ones.
        pruned = prune_pre_restore(app_dir, secrets_dir)
        if pruned:
            result["pruned"] = pruned
            note = (" Deleted %d older pre-restore cop%s (only the newest %d are kept)."
                    % (len(pruned), "y" if len(pruned) == 1 else "ies", KEEP_PRE_RESTORE))
            result["message"] += note
            say("[DATA]" + note + " " + ", ".join(pruned))
    except Exception as exc:
        undone = True
        for now_at, back_to in reversed(moved):  # put every rename back, newest first
            try:
                os.rename(now_at, back_to)
            except OSError:
                undone = False
        message = str(exc) if isinstance(exc, BackupError) else f"{type(exc).__name__}: {exc}"
        # The unpacked copy of the backup is ours and of no further use. It is
        # only removed when every rename was put back, i.e. when it is certain
        # to hold nothing but files unpacked from the archive a moment ago.
        if undone and os.path.basename(staging).startswith(STAGING_PREFIX) and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
        elif os.path.isdir(staging):
            message += " (unpacked files left in %s)" % os.path.basename(staging)
        result.update(status="failed", message="Restore not applied: " + message)
        say("[DATA] " + result["message"] + " Your data is unchanged.")
        outcome = "failed"
    try:
        os.unlink(request_path)                  # our own request file
    except OSError:
        pass
    try:
        _write_json(os.path.join(app_dir, RESULT), result)
    except OSError:
        pass
    return outcome
