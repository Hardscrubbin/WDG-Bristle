"""[merge-v3] Loot backup / restore to one file (after WDGoNG 0.10.0).

The engine (plugins/_loot_backup.py), the launch hook in watchdogs/__main__.py
and the screen (plugins/ng_loot_backup.py), against upstream's real LootManager in a
temporary folder. The "secrets.conf" here is a synthetic file; the real one in
the build folder is never read, and nothing outside the temporary folder is
written.
"""

import ast
import io
import json
import os
import sys
import tarfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from watchdogs import plugin_loader
from plugins import _loot_backup as engine
import plugins.ng_loot_backup as ng_backup

import test_v3_loot_kismet as lk
import test_v3_ng_plugins as ng
from test_scan_session_map import loot_at

ROOT = Path(__file__).resolve().parents[1]
SECRET = "synthetic-not-a-real-key-7f3a"
W, H = 640, 360
FRAME_LIMIT = 0.5


def tree(folder):
    """{relative path: bytes} of every file under ``folder``."""
    folder = Path(folder)
    return {path.relative_to(folder).as_posix(): path.read_bytes()
            for path in sorted(folder.rglob("*")) if path.is_file()}


def members(path):
    with tarfile.open(path, "r:gz") as archive:
        return [member.name for member in archive if not member.isdir()]


class DataCase(lk.LootCase):
    """An installation folder with loot, a whitelist, secrets and a map cache."""

    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        self.home.mkdir()
        with loot_at(str(self.root), "2026-01-01T10:00:00", lk.gps()) as loot:
            loot.save_wardriving_network(lk.net(lk.W1, ssid="First"), observed_at=lk.T0)
            loot.save_bt_device(lk.B1, -60, "Tag", False, False)
            loot.save_xp(111)
            self.session = Path(loot.session_dir).name
        self.loot_dir = self.root / "loot"
        logs = self.loot_dir / "kismet_logs"
        logs.mkdir()
        (logs / "Kismet-20260101.kismet").write_bytes(b"K" * 50_000)
        (logs / "Kismet-20260101.kismet-journal").write_bytes(b"J" * 1000)
        (logs / "wigle_upload_queue.json").write_text("[]")
        (self.root / "whitelist.json").write_text('{"mine": ["02:00:00:00:99:01"]}')
        (self.root / "secrets.conf").write_text("WDG_WIGLE_TOKEN=%s\n" % SECRET)
        (self.root / "maps").mkdir()
        (self.root / "maps" / "tile.png").write_bytes(b"P" * 2000)

    def backup(self, **kwargs):
        return engine.create(str(self.root), str(self.home), **kwargs)

    def play_on(self, stamp="2026-02-02T10:00:00", xp=999):
        """A later run of the game: more loot, more XP, a changed whitelist."""
        with loot_at(str(self.root), stamp, lk.gps()) as loot:
            loot.save_wardriving_network(lk.net(lk.W2, ssid="Later"), observed_at=lk.T0 + 50)
            loot.save_xp(xp)
        (self.root / "whitelist.json").write_text('{"mine": []}')

    def xp_now(self):
        with loot_at(str(self.root), "2027-01-01T10:00:00", lk.gps()) as loot:
            return loot.load_xp()


class BackupTests(DataCase):
    def test_full_backup_holds_everything_the_player_owns_and_nothing_else(self):
        summary = self.backup(build="0.9.21.3+hardscrubbin-v3", now=1767261600)
        path = Path(summary["path"])
        self.assertEqual(path.parent, self.home)
        self.assertRegex(path.name, r"^wdg-bristle-bak-\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d\.tar\.gz$")
        names = members(path)
        self.assertEqual(names[0], "wdg-backup.json")                 # description first
        expected = {"loot/" + name for name in tree(self.loot_dir)} | {"whitelist.json", "secrets.conf"}
        self.assertEqual(set(names[1:]), expected)
        self.assertIn("loot/loot_db.json", names)
        self.assertIn("loot/%s/wardriving.csv" % self.session, names)
        self.assertIn("loot/kismet_logs/Kismet-20260101.kismet", names)
        self.assertFalse(any(name.startswith("maps") for name in names))   # tile cache: not backed up
        self.assertEqual([p.name for p in self.home.iterdir()], [path.name])   # no .part left
        self.assertEqual((summary["files"], summary["mode"], summary["has_secrets"], summary["skipped"]),
                         (len(expected), "full", True, []))
        with tarfile.open(path, "r:gz") as archive:
            manifest = json.loads(archive.extractfile("wdg-backup.json").read())
            self.assertEqual(archive.extractfile("loot/loot_db.json").read(),
                             (self.loot_dir / "loot_db.json").read_bytes())
        self.assertEqual((manifest["kind"], manifest["format"], manifest["mode"], manifest["build"]),
                         ("wdg-bristle-backup", 1, "full", "0.9.21.3+hardscrubbin-v3"))
        self.assertEqual(manifest["contains"], ["loot", "secrets.conf", "whitelist.json"])

    def test_lite_backup_leaves_out_only_kismets_databases(self):
        full, lite = set(members(self.backup(now=1)["path"])), set(members(self.backup(lite=True, now=99)["path"]))
        self.assertEqual(full - lite, {"loot/kismet_logs/Kismet-20260101.kismet",
                                       "loot/kismet_logs/Kismet-20260101.kismet-journal"})
        self.assertIn("loot/kismet_logs/wigle_upload_queue.json", lite)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_backup_file_is_readable_by_its_owner_only(self):
        mode = os.stat(self.backup()["path"]).st_mode & 0o777
        self.assertEqual(mode, 0o600)                                  # it contains secrets.conf

    def test_links_are_never_followed_or_stored(self):
        outside = self.root / "outside.txt"
        outside.write_text("not loot")
        try:
            os.symlink(outside, self.loot_dir / self.session / "link.txt")
            os.symlink(self.root / "maps", self.loot_dir / "linked-folder")
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are not available here")
        names = members(self.backup()["path"])
        self.assertFalse(any("link" in name for name in names))

    def test_a_file_that_vanishes_during_the_backup_is_skipped_not_fatal(self):
        gone = str(self.loot_dir / self.session / "wardriving.csv")
        real_open = open

        def flaky_open(path, *args, **kwargs):
            if str(path) == gone and "rb" in args:
                raise FileNotFoundError(path)
            return real_open(path, *args, **kwargs)

        with patch.object(engine, "open", flaky_open, create=True):
            summary = self.backup()
        self.assertEqual(summary["skipped"], ["loot/%s/wardriving.csv" % self.session])
        self.assertNotIn("loot/%s/wardriving.csv" % self.session, members(summary["path"]))
        self.assertEqual(engine.inspect(summary["path"])["xp"], 111)   # still a valid backup

    def test_a_file_cut_short_while_being_copied_cannot_break_the_archive(self):
        reader = engine._ExactReader(io.BytesIO(b"abc"), 10)
        self.assertEqual(reader.read(4) + reader.read(100) + reader.read(5), b"abc" + bytes(7))
        self.assertEqual(reader.padded, 7)

    def test_refusals_write_nothing(self):
        with patch.object(engine.shutil, "disk_usage", return_value=SimpleNamespace(free=10)):
            with self.assertRaisesRegex(engine.BackupError, "Not enough free space"):
                self.backup()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(engine.BackupError, "cancelled"):
            self.backup(cancel=cancel)
        empty = self.root / "empty-install"
        empty.mkdir()
        with self.assertRaisesRegex(engine.BackupError, "no loot"):
            engine.create(str(empty), str(self.home))
        self.assertEqual(list(self.home.iterdir()), [])

    def test_progress_is_reported_per_file(self):
        seen = []
        summary = self.backup(progress=lambda *values: seen.append(values))
        self.assertEqual(seen[-1][0], seen[-1][1])
        self.assertEqual(seen[-1][2], summary["bytes"])
        self.assertEqual([values[0] for values in seen], list(range(1, len(seen) + 1)))

    def test_backups_are_listed_newest_first(self):
        first = self.backup(now=1767261600)["path"]
        second = self.backup(now=1767261700)["path"]
        (self.home / "notes.tar.gz").write_bytes(b"x")
        (self.home / "wdg-bristle-bak-unfinished.tar.gz.part").write_bytes(b"x")
        self.assertEqual([item["path"] for item in engine.list_backups(str(self.home))], [second, first])
        self.assertEqual(engine.list_backups(str(self.home / "missing")), [])


def forged(path, entries, manifest=True, database=True):
    """Hand-made archive. entries: (name, tar type, payload or link target)."""
    with tarfile.open(path, "w:gz") as archive:
        def add(name, data, kind=tarfile.REGTYPE, link=""):
            info = tarfile.TarInfo(name)
            info.type, info.linkname = kind, link
            info.size = len(data) if kind == tarfile.REGTYPE else 0
            archive.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
        if manifest is True:
            manifest = dict(kind="wdg-bristle-backup", format=1, mode="full")
        if manifest:
            add("wdg-backup.json", json.dumps(manifest).encode())
        if database is True:
            database = json.dumps(dict(version=2, sessions={}, xp=5)).encode()
        if database:
            add("loot/loot_db.json", database)
        for name, kind, payload in entries:
            if kind == tarfile.REGTYPE:
                add(name, payload)
            else:
                add(name, b"", kind, payload)
    return str(path)


class InspectTests(DataCase):
    def test_a_real_backup_is_described(self):
        summary = engine.inspect(self.backup()["path"])
        self.assertEqual((summary["xp"], summary["sessions"], summary["mode"]), (111, 1, "full"))
        self.assertEqual((summary["has_secrets"], summary["has_whitelist"]), (True, True))
        self.assertEqual(summary["files"], len(tree(self.loot_dir)) + 2)

    def test_archives_that_could_write_outside_the_data_are_refused(self):
        reg, evil = tarfile.REGTYPE, b"evil"
        cases = {
            "parent path": [("../evil.txt", reg, evil)],
            "absolute path": [("/tmp/evil.txt", reg, evil)],
            "climbs out of loot": [("loot/../../evil.txt", reg, evil)],
            "deep climb": [("loot/a/b/../../../../evil.txt", reg, evil)],
            "dot segment": [("loot/./x", reg, evil)],
            "backslash path": [("loot\\..\\..\\evil.txt", reg, evil)],
            "unexpected top level": [("plugins/kismet_wardrive.py", reg, evil)],
            "file posing as loot": [("loot", reg, evil)],
            "below a single file": [("secrets.conf/x", reg, evil)],
            "program code": [("watchdogs/app.py", reg, evil)],
            "symbolic link": [("loot/link", tarfile.SYMTYPE, "/etc/passwd")],
            "hard link": [("loot/hard", tarfile.LNKTYPE, "loot/loot_db.json")],
            "device node": [("loot/dev", tarfile.CHRTYPE, "")],
            "fifo": [("loot/fifo", tarfile.FIFOTYPE, "")],
            "same file twice": [("loot/a.txt", reg, b"1"), ("loot/a.txt", reg, b"2")],
        }
        for label, entries in cases.items():
            with self.subTest(case=label):
                path = forged(self.home / "forged.tar.gz", entries)
                with self.assertRaises(engine.BackupError):
                    engine.inspect(path)

    def test_archives_this_game_could_not_open_are_refused(self):
        good_db = json.dumps(dict(version=2, sessions={})).encode()
        cases = {
            "someone else's archive": dict(manifest=dict(kind="wdgo-backup", format=1)),
            "newer format": dict(manifest=dict(kind="wdg-bristle-backup", format=2)),
            "no database": dict(database=False),
            "damaged database": dict(database=b"{not json"),
            "newer database": dict(database=json.dumps(dict(version=3, sessions={})).encode()),
            "database without sessions": dict(database=json.dumps(dict(version=2)).encode()),
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(engine.BackupError):
                    engine.inspect(forged(self.home / "forged.tar.gz", [], **kwargs))
        self.assertEqual(engine.inspect(forged(self.home / "ok.tar.gz", [], database=good_db))["xp"], None)
        self.assertFalse(engine.inspect(forged(self.home / "ok.tar.gz", [], database=good_db))["plain"])

    def test_a_plain_archive_of_another_builds_data_is_accepted_after_the_same_checks(self):
        # e.g. tar czf ~/wdg-v2-backup-<time>.tar.gz -C ~/wdg-builds/wdg-bristle-v2 loot whitelist.json secrets.conf
        old_db = json.dumps(dict(version=1, sessions={"2026-11-09_10-00-00": {}}, xp=4321)).encode()
        entries = [("loot/2026-11-09_10-00-00/wardriving.csv", tarfile.REGTYPE, b"WigleWifi-1.4\n"),
                   ("whitelist.json", tarfile.REGTYPE, b"{}"), ("secrets.conf", tarfile.REGTYPE, b"K=v\n")]
        path = forged(self.home / "wdg-v2-backup-2026-11-09_12-00-00.tar.gz", entries,
                      manifest=False, database=old_db)
        summary = engine.inspect(path)
        self.assertEqual((summary["plain"], summary["xp"], summary["sessions"], summary["mode"]),
                         (True, 4321, 1, "plain"))
        self.assertEqual((summary["has_secrets"], summary["has_whitelist"]), (True, True))
        own = self.backup(now=1767261600)["path"]
        listed = [item["name"] for item in engine.list_backups(str(self.home))]
        self.assertEqual(listed, [os.path.basename(own), "wdg-v2-backup-2026-11-09_12-00-00.tar.gz"])
        for label, kwargs in {"no database": dict(database=False),
                              "unsafe member": dict(entries=[("loot/../x", tarfile.REGTYPE, b"e")]),
                              "program code": dict(entries=[("plugins/x.py", tarfile.REGTYPE, b"e")])}.items():
            with self.subTest(case=label), self.assertRaises(engine.BackupError):
                engine.inspect(forged(self.home / "bad.tar.gz", kwargs.get("entries", []), manifest=False,
                                      database=kwargs.get("database", old_db)))
        engine.stage(str(self.root), path)
        self.assertEqual(engine.apply_pending(str(self.root), now=1770000000), "restored")
        self.assertEqual(json.loads((self.loot_dir / "loot_db.json").read_text())["xp"], 4321)
        self.assertTrue((self.root / ("loot.pre-restore-" + engine.stamp(1770000000))).is_dir())
        plain = self.home / "plain.tar.gz"
        plain.write_bytes(b"this is not an archive")
        with self.assertRaises(engine.BackupError):
            engine.inspect(str(plain))
        whole = Path(self.backup()["path"]).read_bytes()
        cut = self.home / "cut.tar.gz"
        cut.write_bytes(whole[:len(whole) // 2])
        with self.assertRaises(engine.BackupError):
            engine.inspect(str(cut))


class RestoreTests(DataCase):
    def test_nothing_staged_is_one_check_and_changes_nothing(self):
        before = tree(self.root)
        self.assertEqual(engine.apply_pending(str(self.root)), "none")
        self.assertEqual(tree(self.root), before)

    def test_round_trip_through_the_real_loot_manager(self):
        saved = tree(self.loot_dir)
        backup = self.backup()["path"]
        self.play_on(xp=999)                                           # the game carries on
        newer = tree(self.loot_dir)
        self.assertNotEqual(newer, saved)
        self.assertEqual(self.xp_now(), 999)
        newer = tree(self.loot_dir)                                    # (xp_now opened a session)

        request = engine.stage(str(self.root), backup)                 # in-game: staged only
        self.assertEqual(request["summary"]["xp"], 111)
        self.assertEqual(tree(self.loot_dir), newer)                   # nothing replaced yet
        self.assertEqual(engine.pending(str(self.root))["path"], backup)

        said = []
        self.assertEqual(engine.apply_pending(str(self.root), log=said.append, now=1770000000), "restored")
        mark = engine.stamp(1770000000)
        self.assertEqual(tree(self.loot_dir), saved)                   # exactly the backup
        self.assertEqual(tree(self.root / ("loot.pre-restore-" + mark)), newer)   # replaced data kept
        self.assertEqual((self.root / "whitelist.json").read_text(), '{"mine": ["02:00:00:00:99:01"]}')
        self.assertEqual((self.root / ("whitelist.json.pre-restore-" + mark)).read_text(), '{"mine": []}')
        self.assertTrue((self.root / ("secrets.conf.pre-restore-" + mark)).is_file())
        self.assertIsNone(engine.pending(str(self.root)))              # applied once
        result = engine.last_result(str(self.root))
        self.assertEqual((result["status"], result["xp"], result["sessions"]), ("restored", 111, 1))
        self.assertIn("loot.pre-restore-" + mark, result["message"])
        self.assertTrue(any("Restored" in line for line in said))
        self.assertEqual(engine.apply_pending(str(self.root)), "none")
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".wdg-restore-staging")], [])
        self.assertEqual(self.xp_now(), 111)                           # upstream opens the restored save

    def test_what_the_backup_does_not_hold_is_left_installed(self):
        (self.root / "whitelist.json").unlink()
        (self.root / "secrets.conf").unlink()
        backup = self.backup()["path"]                                 # loot only
        (self.root / "whitelist.json").write_text("current whitelist")
        (self.root / "secrets.conf").write_text("CURRENT=1\n")
        engine.stage(str(self.root), backup)
        self.assertEqual(engine.apply_pending(str(self.root)), "restored")
        self.assertEqual((self.root / "whitelist.json").read_text(), "current whitelist")
        self.assertEqual((self.root / "secrets.conf").read_text(), "CURRENT=1\n")
        self.assertEqual([p.name for p in self.root.glob("*.json.pre-restore-*")], [])

    def test_secrets_are_restored_to_the_folder_the_game_reads_them_from(self):
        backup = self.backup()["path"]
        project = self.root / "project-root"                           # loot_path != project root
        project.mkdir()
        engine.stage(str(self.root), backup)
        (self.root / "secrets.conf").write_text("ELSEWHERE=1\n")
        self.assertEqual(engine.apply_pending(str(self.root), secrets_dir=str(project)), "restored")
        self.assertIn(SECRET, (project / "secrets.conf").read_text())
        self.assertEqual((self.root / "secrets.conf").read_text(), "ELSEWHERE=1\n")

    def test_two_restores_keep_two_copies(self):
        backup = self.backup()["path"]
        for when in (1770000000, 1770000100):
            engine.stage(str(self.root), backup)
            self.assertEqual(engine.apply_pending(str(self.root), now=when), "restored")
        kept = sorted(p.name for p in self.root.glob("loot.pre-restore-*"))
        self.assertEqual(kept, ["loot.pre-restore-" + engine.stamp(1770000000),
                                "loot.pre-restore-" + engine.stamp(1770000100)])
        self.assertNotIn("pruned", engine.last_result(str(self.root)))

    def test_only_the_newest_two_pre_restore_copies_survive_a_restore(self):
        backup = self.backup()["path"]
        lookalikes = ["loot.pre-restore-keepme", "loot.pre-restore-2020-01-01_00-00-00.bak",
                      "loot.pre-restore-", "myloot.pre-restore-2020-01-01_00-00-00"]
        for name in lookalikes:                                        # not ours: never touched
            (self.root / name).mkdir()
            (self.root / name / "mine.txt").write_text("keep")
        times = (1770000000, 1770000100, 1770000200, 1770000300)
        said = []
        for when in times:
            engine.stage(str(self.root), backup)
            self.assertEqual(engine.apply_pending(str(self.root), log=said.append, now=when), "restored")
        marks = [engine.stamp(when) for when in times]
        for item in ("loot", "whitelist.json", "secrets.conf"):
            kept = sorted(p.name for p in self.root.iterdir()
                          if p.name.startswith(item + ".pre-restore-") and p.name not in lookalikes)
            self.assertEqual(kept, [item + ".pre-restore-" + mark for mark in marks[-2:]], item)
        for name in lookalikes:
            self.assertEqual((self.root / name / "mine.txt").read_text(), "keep")
        result = engine.last_result(str(self.root))
        self.assertEqual(sorted(result["pruned"]),
                         sorted(item + ".pre-restore-" + marks[1]
                                for item in ("loot", "whitelist.json", "secrets.conf")))
        self.assertIn("Deleted 3 older pre-restore copies (only the newest 2 are kept)", result["message"])
        self.assertTrue(any("Deleted 3 older" in line for line in said))
        self.assertEqual(self.xp_now(), 111)

    def test_a_failed_restore_prunes_nothing(self):
        backup = Path(self.backup()["path"])
        for when in (1770000000, 1770000100):
            engine.stage(str(self.root), str(backup))
            self.assertEqual(engine.apply_pending(str(self.root), now=when), "restored")
        old = self.root / "loot.pre-restore-2001-01-01_00-00-00"       # a third, older copy
        old.mkdir()
        (old / "loot_db.json").write_text("{}")
        engine.stage(str(self.root), str(backup))
        with open(backup, "ab") as fh:
            fh.write(b"tampered")
        self.assertEqual(engine.apply_pending(str(self.root), now=1770000200), "failed")
        self.assertEqual(len(list(self.root.glob("loot.pre-restore-*"))), 3)
        self.assertTrue((old / "loot_db.json").is_file())

    def test_a_copy_that_cannot_be_deleted_does_not_fail_the_restore(self):
        backup = self.backup()["path"]
        for when in (1770000000, 1770000100):
            engine.stage(str(self.root), backup)
            engine.apply_pending(str(self.root), now=when)
        engine.stage(str(self.root), backup)
        with patch.object(engine.shutil, "rmtree", side_effect=OSError("busy")):
            self.assertEqual(engine.apply_pending(str(self.root), now=1770000200), "restored")
        self.assertEqual(len(list(self.root.glob("loot.pre-restore-*"))), 3)     # left, not half-deleted
        self.assertEqual(engine.last_result(str(self.root))["status"], "restored")

    def failed_apply_leaves_everything(self, **kwargs):
        before = tree(self.root)
        before.pop(".wdg-restore-request.json", None)
        before.pop(".wdg-restore-result.json", None)                   # from an earlier attempt
        self.assertEqual(engine.apply_pending(str(self.root), **kwargs), "failed")
        after = tree(self.root)
        result = json.loads(after.pop(".wdg-restore-result.json"))
        self.assertEqual(after, before)                                # byte for byte
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(engine.pending(str(self.root)))              # no loop at every start
        self.assertEqual(self.xp_now(), 111)
        return result

    def test_a_backup_changed_or_removed_after_staging_is_not_applied(self):
        backup = Path(self.backup()["path"])
        engine.stage(str(self.root), str(backup))
        with open(backup, "ab") as fh:
            fh.write(b"tampered")
        self.assertIn("changed after", self.failed_apply_leaves_everything()["message"])
        kept = self.home / "moved-away.tar.gz"
        engine.stage(str(self.root), str(backup))
        os.replace(backup, kept)
        self.assertIn("no longer there", self.failed_apply_leaves_everything()["message"])

    def test_a_forged_archive_is_refused_at_apply_even_if_a_request_points_at_it(self):
        bad = forged(self.home / "forged.tar.gz", [("loot/../../escaped.txt", tarfile.REGTYPE, b"evil")])
        info = os.stat(bad)
        (self.root / ".wdg-restore-request.json").write_text(json.dumps(
            dict(path=bad, size=info.st_size, mtime_ns=info.st_mtime_ns)))
        self.failed_apply_leaves_everything()
        self.assertFalse((self.root.parent / "escaped.txt").exists())
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_a_damaged_request_or_a_full_disk_is_a_clean_failure(self):
        (self.root / ".wdg-restore-request.json").write_text("{broken")
        self.failed_apply_leaves_everything()
        engine.stage(str(self.root), self.backup()["path"])
        with patch.object(engine.shutil, "disk_usage", return_value=SimpleNamespace(free=10)):
            self.assertIn("Not enough free space", self.failed_apply_leaves_everything()["message"])

    def test_a_failure_in_the_middle_of_the_swap_puts_everything_back(self):
        engine.stage(str(self.root), self.backup()["path"])
        self.play_on(xp=999)
        with loot_at(str(self.root), "2026-03-03T10:00:00", lk.gps()) as loot:
            loot.save_xp(111)                                          # so xp_now() below agrees
        real, calls = os.rename, []

        def rename(source, target):
            calls.append((os.path.basename(str(source)), os.path.basename(str(target))))
            if len(calls) == 4:                                        # loot swapped, whitelist half-way
                raise OSError("synthetic I/O error")
            return real(source, target)

        before = tree(self.root)
        before.pop(".wdg-restore-request.json")
        with patch.object(engine.os, "rename", rename):
            self.assertEqual(engine.apply_pending(str(self.root), now=1770000000), "failed")
        self.assertEqual(len(calls), 7)                                # 3 done + 1 failed + 3 undone
        after = tree(self.root)
        result = json.loads(after.pop(".wdg-restore-result.json"))
        self.assertEqual(after, before)                                # loot AND whitelist as they were
        self.assertIn("synthetic I/O error", result["message"])
        self.assertEqual(list(self.root.glob("*pre-restore*")), [])
        self.assertEqual(list(self.root.glob(".wdg-restore-staging*")), [])

    def test_the_engine_deletes_only_what_it_documents(self):
        source = (ROOT / "plugins" / "_loot_backup.py").read_text(encoding="utf-8")
        code = ast.parse(source)
        calls = [ast.unparse(node.func) for node in ast.walk(code) if isinstance(node, ast.Call)]
        self.assertEqual(calls.count("shutil.rmtree"), 2)              # its unpack folder; an old undo copy
        # .part, request file (x2), an old undo copy, an old rolling loot_db copy
        self.assertEqual(calls.count("os.unlink"), 5)
        for forbidden in ("os.remove", "os.removedirs", "shutil.move", "Path.unlink"):
            self.assertNotIn(forbidden, calls)
        imports = {alias.name for node in ast.walk(code) if isinstance(node, ast.Import)
                   for alias in node.names}
        self.assertEqual(imports, {"io", "json", "os", "re", "shutil", "tarfile", "time"})   # stdlib only
        self.assertFalse(any(isinstance(node, ast.ImportFrom) for node in ast.walk(code)))


class LaunchHookTests(DataCase):
    def setUp(self):
        super().setUp()
        (self.root / "secrets.conf").unlink()            # the hook restores secrets into the real
        (self.root / "whitelist.json").unlink()          # build folder: keep them out of this test
        import watchdogs.__main__ as launcher
        self.launcher = launcher

    def test_launch_applies_a_staged_restore_before_the_game_exists(self):
        backup = self.backup()["path"]
        self.play_on(xp=999)
        (self.root / "whitelist.json").unlink()
        engine.stage(str(self.root), backup)
        real_secrets = sorted(p.name for p in ROOT.glob("secrets.conf*"))
        with patch("builtins.print") as printed:
            self.assertEqual(self.launcher._apply_staged_restore(str(self.root)), "restored")
        self.assertTrue(any("Restored" in str(call.args[0]) for call in printed.call_args_list))
        self.assertEqual(self.xp_now(), 111)
        self.assertEqual(sorted(p.name for p in ROOT.glob("secrets.conf*")), real_secrets)

    def test_launch_with_nothing_staged_does_nothing_and_says_nothing(self):
        before = tree(self.root)
        with patch("builtins.print") as printed:
            self.assertEqual(self.launcher._apply_staged_restore(str(self.root)), "none")
        printed.assert_not_called()
        self.assertEqual(tree(self.root), before)

    def test_hook_passes_the_loot_folder_and_the_project_root_and_never_raises(self):
        with patch.object(engine, "apply_pending", return_value="none") as apply:
            self.launcher._apply_staged_restore(None)
            self.launcher._apply_staged_restore("/some/loot/path")
        self.assertEqual([call.args[0] for call in apply.call_args_list], [str(ROOT), "/some/loot/path"])
        self.assertEqual({call.kwargs["secrets_dir"] for call in apply.call_args_list}, {str(ROOT)})
        with patch.object(engine, "apply_pending", side_effect=RuntimeError("boom")), \
                self.assertLogs(level="ERROR"):
            self.assertEqual(self.launcher._apply_staged_restore(None), "failed")

    def test_main_calls_the_hook_once_just_before_the_game_is_created(self):
        text = (ROOT / "watchdogs" / "__main__.py").read_text(encoding="utf-8")
        body = text.split("def main():", 1)[1]
        self.assertEqual(body.count("_apply_staged_restore(loot_path)"), 1)
        self.assertLess(body.index("loot_path = arg"), body.index("_apply_staged_restore(loot_path)"))
        self.assertLess(body.index("_apply_staged_restore(loot_path)"), body.index("WatchDogsGame(serial_port"))
        self.assertLess(body.index("_recover_game_update()"), body.index("_apply_staged_restore(loot_path)"))


class ScreenCase(DataCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(ng_backup.NgLootBackup, "_secrets_dir", staticmethod(lambda: str(self.root)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = SimpleNamespace(_app_dir=str(self.root), _plugins=[], msg=Mock(), _term_add=Mock(),
                                   _esc_consumed_frame=-1)
        self.plugin = ng_backup.NgLootBackup()
        self.plugin._backup_dir = str(self.home)
        self.plugin.on_load(self.app)

    def finish(self):
        thread = self.plugin._thread
        thread.join(20.0)
        self.assertFalse(thread.is_alive())
        self.plugin.on_update()

    def hud(self):
        return [call.args[0] for call in self.app.msg.call_args_list]

    def terminal(self):
        return [call.args[0] for call in self.app._term_add.call_args_list]

    def press(self, canvas, key):
        canvas.pressed = {key}
        self.plugin.on_update()
        canvas.pressed = set()


class ScreenTests(ScreenCase):
    def test_backup_from_the_screen_and_the_key_never_appears_anywhere(self):
        canvas = ng.Canvas()
        with ng.with_pyxel(canvas):
            self.plugin.open_overlay()
            self.finish()
            self.plugin.draw(0, 0, W, H)
            self.press(canvas, "KEY_B")
            self.finish()
            canvas.new_frame()
            self.plugin.draw(0, 0, W, H)
        self.assertEqual(canvas.overlaps(), [])
        backup, = engine.list_backups(str(self.home))
        self.assertIn("secrets.conf", members(backup["path"]))
        shown = " | ".join(canvas.texts)
        self.assertIn("LOOT BACKUP / RESTORE", shown)
        self.assertIn("> " + backup["name"], shown)
        self.assertIn("Backup saved: ~/" + backup["name"], shown)
        self.assertIn("A backup includes secrets.conf (your API keys). Keep the file private.", shown)
        self.assertIn("[DATA] Backup saved: " + backup["name"], self.hud())
        self.assertTrue(any("includes secrets.conf" in line for line in self.terminal()))
        for text in canvas.texts + self.hud() + self.terminal():
            self.assertNotIn(SECRET, text)                             # says "included", never the value

    def test_full_backup_waits_for_kismet_but_the_lite_one_does_not(self):
        self.app._plugins = [SimpleNamespace(kismet_active=lambda: True)]
        canvas = ng.Canvas()
        with ng.with_pyxel(canvas):
            self.plugin._overlay_active = True
            self.press(canvas, "KEY_B")
            self.assertIsNone(self.plugin._thread)                     # nothing started
            self.assertIn("Stop Kismet first", self.hud()[-1])
            self.press(canvas, "KEY_L")
            self.finish()
        names = members(engine.list_backups(str(self.home))[0]["path"])
        self.assertFalse(any(name.endswith(".kismet") for name in names))

    def test_restore_is_checked_confirmed_and_only_staged(self):
        backup = self.backup(now=1767261600)["path"]
        self.play_on(xp=999)
        before = tree(self.loot_dir)
        canvas = ng.Canvas()
        with ng.with_pyxel(canvas):
            self.plugin.open_overlay()
            self.finish()
            self.press(canvas, "KEY_R")                                # checked in the background
            self.finish()
            self.assertEqual(self.plugin._confirm["summary"]["xp"], 111)
            canvas.new_frame()
            self.plugin.draw(0, 0, W, H)
            self.assertEqual(canvas.overlaps(), [])
            self.assertIn("RESTORE THIS BACKUP?", canvas.texts)
            self.assertTrue(any(text.startswith("XP 111, 1 session(s)") for text in canvas.texts))
            self.assertTrue(any("Only the newest 2 such copies are kept: this restore DELETES 0 older"
                                in text for text in canvas.texts))
            self.assertTrue(any(text.startswith("0 on disk (loot.pre-restore-<time>). A restore keeps the "
                                                "newest 2") for text in canvas.texts))
            self.press(canvas, "KEY_B")                                # other keys wait for Y / N
            self.assertEqual(len(engine.list_backups(str(self.home))), 1)
            self.press(canvas, "KEY_N")
            self.assertIsNone(self.plugin._confirm)
            self.assertIsNone(engine.pending(str(self.root)))
            self.press(canvas, "KEY_R")
            self.finish()
            self.press(canvas, "KEY_Y")
            self.finish()
            canvas.new_frame()
            self.plugin.draw(0, 0, W, H)
        self.assertEqual(engine.pending(str(self.root))["path"], backup)
        self.assertEqual(tree(self.loot_dir), before)                  # the running game's loot: untouched
        self.assertIn("[DATA] Restore staged: quit and start the game again to apply it.", self.hud())
        self.assertTrue(any("applied at the next start" in text for text in canvas.texts))
        with ng.with_pyxel(canvas):
            self.press(canvas, "KEY_X")                                # changed my mind
        self.assertIsNone(engine.pending(str(self.root)))
        self.assertEqual(engine.apply_pending(str(self.root)), "none")

    def test_a_bad_backup_file_is_explained_and_never_staged(self):
        forged(self.home / "wdg-bristle-bak-2026-01-01_00-00-00.tar.gz",
               [("../evil.txt", tarfile.REGTYPE, b"evil")])
        canvas = ng.Canvas()
        with ng.with_pyxel(canvas):
            self.plugin.open_overlay()
            self.finish()
            self.press(canvas, "KEY_R")
            self.finish()
            self.press(canvas, "KEY_Y")
            self.plugin.draw(0, 0, W, H)
        self.assertIsNone(self.plugin._confirm)
        self.assertIsNone(engine.pending(str(self.root)))
        self.assertTrue(any("Unsafe or unexpected path" in line for line in self.hud()))

    def test_game_loop_keeps_ticking_while_a_backup_runs(self):
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        real = engine.create

        def slow_create(*args, **kwargs):
            entered.set()
            release.wait(60.0)
            return real(*args, **kwargs)

        canvas = ng.Canvas()
        with patch.object(engine, "create", slow_create), ng.with_pyxel(canvas):
            self.plugin._overlay_active = True
            started = time.perf_counter()
            self.press(canvas, "KEY_B")
            self.assertLess(time.perf_counter() - started, FRAME_LIMIT)
            self.assertTrue(entered.wait(10.0))
            slowest = 0.0
            for _ in range(200):
                began = time.perf_counter()
                self.plugin.on_update()
                self.plugin.draw(0, 0, W, H)
                slowest = max(slowest, time.perf_counter() - began)
            self.assertLess(slowest, FRAME_LIMIT)
            self.assertTrue(self.plugin._thread.is_alive() and self.plugin._thread.daemon)
            self.assertFalse(self.plugin.start_backup(lite=True))      # one job at a time
            self.assertIn("working", canvas.texts)
            release.set()
            self.finish()
        self.assertEqual(len(engine.list_backups(str(self.home))), 1)

    def test_quitting_during_a_backup_cancels_it_and_leaves_no_partial_file(self):
        entered = threading.Event()
        real = engine.create

        def create_until_cancelled(*args, **kwargs):
            entered.set()
            kwargs["cancel"].wait(30.0)                                # ...the game quits meanwhile
            return real(*args, **kwargs)

        with patch.object(engine, "create", create_until_cancelled):
            self.assertTrue(self.plugin.start_backup(lite=True))
            self.assertTrue(entered.wait(10.0))
            self.plugin.on_unload()
        self.assertFalse(self.plugin._thread.is_alive())
        self.assertEqual(list(self.home.iterdir()), [])                # no archive, no .part

    def test_many_backups_scroll_and_the_progress_bar_fits(self):
        for index in range(20):
            (self.home / ("wdg-bristle-bak-2026-01-%02d_10-00-00.tar.gz" % (index + 1))).write_bytes(b"x" * 1000)
        canvas = ng.Canvas()
        with ng.with_pyxel(canvas):
            self.plugin.open_overlay()
            self.finish()
            canvas.pressed = {"KEY_DOWN"}
            for _ in range(40):
                self.plugin.on_update()
            self.plugin._progress = (3, 10, 1_500_000, 5_000_000)
            self.plugin._staged = dict(path=str(self.home / "wdg-bristle-bak-2026-01-20_10-00-00.tar.gz"))
            self.plugin._last = dict(message="Restore not applied: " + "x" * 400)
            canvas.new_frame()
            self.plugin.draw(0, 0, W, H)
        self.assertEqual(canvas.overlaps(), [])
        self.assertEqual(self.plugin._sel, 19)
        self.assertIn("> wdg-bristle-bak-2026-01-01_10-00-00.tar.gz", canvas.texts)   # oldest, last row
        self.assertIn("3 / 10 files, 1.5 MB of 5.0 MB", canvas.texts)

    def test_escape_closes_and_a_restore_applied_at_launch_is_reported(self):
        canvas = ng.Canvas()
        with ng.with_pyxel(canvas):
            self.plugin._overlay_active = True
            self.press(canvas, "KEY_ESCAPE")
        self.assertFalse(self.plugin.overlay_active)
        self.assertEqual(self.app._esc_consumed_frame, ng.Canvas.frame_count)
        engine.stage(str(self.root), self.backup()["path"])
        engine.apply_pending(str(self.root), secrets_dir=str(self.root))
        fresh = ng_backup.NgLootBackup()                               # the next launch
        fresh.on_load(self.app)
        self.app._term_add.reset_mock()
        with ng.with_pyxel(canvas):
            fresh.on_update()
        self.assertTrue(any(line.startswith("[DATA] Restored wdg-bristle-bak-") for line in self.terminal()))

    def test_loader_finds_the_plugin_and_not_the_engine(self):
        kinds = [type(plugin).__name__ for plugin in plugin_loader.discover_plugins()]
        self.assertEqual(kinds.count("NgLootBackup"), 1)
        item, = ng_backup.NgLootBackup().menu_items()
        self.assertEqual((item.key, item.label, item.action), ("b", "Loot: Backup / Restore", "open_overlay"))
        self.assertIs(ng_backup.NgLootBackup.BACKGROUND_TICK, True)
        notice = (ROOT / "plugins" / "_ng" / "NOTICE").read_text(encoding="utf-8")
        self.assertIn("plugins/ng_loot_backup.py, plugins/_loot_backup.py", notice)
        for name in ("ng_loot_backup.py", "_loot_backup.py"):
            text = (ROOT / "plugins" / name).read_text(encoding="utf-8")
            self.assertIn("Required Notice: Copyright (c) 2025-2026 LOCOSP", text)


if __name__ == "__main__":
    unittest.main()
