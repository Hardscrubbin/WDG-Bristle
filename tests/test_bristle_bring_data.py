# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""[bristle] WDG-Bristle 1.2 (BACKTRACK R8): on a first install the installer looks for earlier
installs in any folder under the user's home whose name is one of the old install names in
watchdogs/legacy_names.py, copies the player's data from the one with the newest data, never moves
or changes anything there, and lists what it found and what it copied.

The looking and the copying are in tools/install_helper.py and are run here on scratch folders.
install.sh itself cannot be run on the build PC; its text is checked for the call it has to make.
"""
import hashlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import install_helper  # noqa: E402
from watchdogs import legacy_names  # noqa: E402

OLD = legacy_names.OLD_INSTALL_NAME


def snapshot(folder) -> dict:
    """{relative path: (kind, sha256 or link target, mtime)} for everything under a folder."""
    seen = {}
    for base, names, files in os.walk(folder):
        for name in names + files:
            path = os.path.join(base, name)
            rel = os.path.relpath(path, folder).replace(os.sep, "/")
            info = os.lstat(path)
            if os.path.islink(path):
                seen[rel] = ("link", os.readlink(path), info.st_mtime_ns)
            elif os.path.isdir(path):
                seen[rel] = ("dir", "", info.st_mtime_ns)
            else:
                with open(path, "rb") as handle:
                    seen[rel] = ("file", hashlib.sha256(handle.read()).hexdigest(), info.st_mtime_ns)
    return seen


class Scratch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home" / "player"
        self.home.mkdir(parents=True)
        self.new = self.home / "wdg-bristle"
        (self.new / "plugins").mkdir(parents=True)
        (self.new / "plugins" / "bristle_core.py").write_text("code\n", encoding="utf-8")
        self.said = []

    def old_install(self, where, age_days=1.0, loot=True, secrets=True, extras=True):
        folder = Path(where)
        (folder / "plugins").mkdir(parents=True)
        stamp = time.time() - age_days * 86400
        if loot:
            (folder / "loot" / "2026-11-20").mkdir(parents=True)
            (folder / "loot" / "loot_db.json").write_text('{"xp": 12}', encoding="utf-8")
            (folder / "loot" / "2026-11-20" / "wardriving.csv").write_bytes(b"row\n" * 500)
            for path in (folder / "loot" / "2026-11-20", folder / "loot" / "loot_db.json", folder / "loot"):
                os.utime(path, (stamp, stamp))
        if secrets:
            (folder / "secrets.conf").write_text("WIGLE_API_NAME=demo\n", encoding="utf-8")
            os.utime(folder / "secrets.conf", (stamp, stamp))
        if extras:
            (folder / "maps").mkdir()
            (folder / "maps" / "tile.bin").write_bytes(b"\0" * 64)
            (folder / "whitelist.json").write_text("[]", encoding="utf-8")
            (folder / "firmware_cache" / legacy_names.OLD_RECOVERY_DIR).mkdir(parents=True)
            (folder / "kismet_adapters.json").write_text('{"wifi_macs": [], "ble_macs": []}', encoding="utf-8")
            (folder / "plugins" / ".wardrive_uploaded.json").write_text("{}", encoding="utf-8")
            (folder / "plugins" / "kismet_wardrive.py").write_text("old code\n", encoding="utf-8")
        return folder

    def bring(self, chosen=""):
        return install_helper.bring_data(str(self.new), str(self.home), chosen, out=self.said.append)

    def text(self):
        return "\n".join(self.said)


class Names(unittest.TestCase):
    def test_the_old_names_come_from_legacy_names(self):
        self.assertEqual(install_helper.OLD_INSTALL_NAMES, tuple(legacy_names.OLD_INSTALL_NAMES))
        self.assertIn(OLD, legacy_names.OLD_INSTALL_NAMES)
        self.assertGreaterEqual(len(legacy_names.OLD_INSTALL_NAMES), 4)
        self.assertNotIn("wdg-bristle", legacy_names.OLD_INSTALL_NAMES)

    def test_what_counts_as_an_old_install_name(self):
        for name in (OLD, OLD.upper(), OLD + "-old", OLD + " (1)", OLD + ".bak", OLD + "_2", legacy_names.OLD_DESKTOP_ID):
            with self.subTest(name=name):
                self.assertTrue(install_helper.is_old_name(name))
        for name in ("wdg-bristle", "wdg", OLD + "x", OLD[:-1], "my-" + OLD, "Documents", ""):
            with self.subTest(name=name):
                self.assertFalse(install_helper.is_old_name(name))
        self.assertFalse(install_helper.is_old_name(OLD, ()))                 # no names known: nothing matches


class Finding(Scratch):
    def test_an_install_anywhere_under_home_is_found_not_only_the_two_old_places(self):
        deep = self.old_install(self.home / "stuff" / "builds" / OLD)
        found, complete = install_helper.find_old_installs(str(self.home), str(self.new))
        self.assertTrue(complete)
        self.assertEqual([entry["path"] for entry in found], [str(deep)])
        self.assertIn("loot", found[0]["items"])
        self.assertIn("plugins/.wardrive_uploaded.json", found[0]["items"])

    def test_the_folder_next_to_this_install_is_found_even_outside_home(self):
        elsewhere = Path(self._tmp.name) / "opt"
        new = elsewhere / "wdg-bristle"
        new.mkdir(parents=True)
        beside = self.old_install(elsewhere / OLD)
        found, _complete = install_helper.find_old_installs(str(self.home), str(new))
        self.assertEqual([entry["path"] for entry in found], [str(beside)])

    def test_several_are_listed_newest_data_first(self):
        older = self.old_install(self.home / "wdg-builds" / legacy_names.OLD_INSTALL_NAMES[2], age_days=40)
        newer = self.old_install(self.home / OLD, age_days=2)
        found, _complete = install_helper.find_old_installs(str(self.home), str(self.new))
        self.assertEqual([entry["path"] for entry in found], [str(newer), str(older)])

    def test_what_is_not_an_install_with_data_is_left_out(self):
        (self.home / OLD).mkdir()                                             # the name, but no data in it
        self.old_install(self.home / ".hidden" / OLD)                         # hidden folders are not searched
        self.old_install(self.home / "a" / "b" / "c" / "d" / "e" / OLD)       # deeper than the search goes
        self.old_install(self.home / "projects" / "something-else")           # data, but not an old name
        found, complete = install_helper.find_old_installs(str(self.home), str(self.new))
        self.assertEqual(found, [])
        self.assertTrue(complete)

    def test_an_install_is_not_searched_inside(self):
        outer = self.old_install(self.home / OLD)
        self.old_install(outer / "old-copies" / OLD)                          # a copy kept inside the install
        found, _complete = install_helper.find_old_installs(str(self.home), str(self.new))
        self.assertEqual([entry["path"] for entry in found], [str(outer)])

    def test_this_install_is_never_its_own_source(self):
        here = self.old_install(self.home / OLD)                              # the new build unzipped over the old name
        found, _complete = install_helper.find_old_installs(str(self.home), str(here))
        self.assertEqual(found, [])

    def test_the_search_gives_up_at_its_limit_and_says_so(self):
        for index in range(12):
            (self.home / ("dir%02d" % index)).mkdir()
        _found, complete = install_helper.find_old_installs(str(self.home), str(self.new), limit=5)
        self.assertFalse(complete)


class Copying(Scratch):
    def test_copy_never_moves_or_changes_the_old_folder(self):
        old = self.old_install(self.home / "stuff" / OLD)
        before = snapshot(old)
        self.assertEqual(self.bring(), 0)
        self.assertEqual(snapshot(old), before)                               # byte for byte, times included
        for item in install_helper.DATA_ITEMS:
            source, copy = old / item, self.new / item
            if source.exists():
                self.assertTrue(copy.exists(), item)
        self.assertEqual((self.new / "loot" / "2026-11-20" / "wardriving.csv").read_bytes(), b"row\n" * 500)
        self.assertEqual((self.new / "secrets.conf").read_text(encoding="utf-8"), "WIGLE_API_NAME=demo\n")
        self.assertTrue((self.new / "firmware_cache" / legacy_names.OLD_RECOVERY_DIR).is_dir())
        # code is never taken from the old install
        self.assertFalse((self.new / "plugins" / "kismet_wardrive.py").exists())
        self.assertEqual((self.new / "plugins" / "bristle_core.py").read_text(encoding="utf-8"), "code\n")
        self.assertEqual([p.name for p in self.new.rglob("*" + install_helper.PARTIAL)], [])

    def test_it_lists_what_was_found_and_what_was_copied(self):
        old = self.old_install(self.home / OLD, age_days=1)
        other = self.old_install(self.home / "wdg-builds" / legacy_names.OLD_INSTALL_NAMES[2], age_days=30)
        self.assertEqual(self.bring(), 0)
        said = self.text()
        self.assertIn("Earlier installs found: 2", said)
        self.assertIn(str(old), said)
        self.assertIn(str(other), said)
        self.assertIn("Copying from %s (the one with the newest data)" % old, said)
        for item in ("loot", "maps", "secrets.conf", "whitelist.json", "firmware_cache", "kismet_adapters.json"):
            self.assertIn("copied: %s (" % item, said)
        self.assertIn("copied: loot (2 files", said)
        self.assertIn("was not changed: keep it as the backup", said)
        self.assertIn("Not used: %s" % other, said)
        self.assertIn("--from=%s" % other, said)
        record = (self.new / install_helper.COPY_RECORD).read_text(encoding="utf-8")
        self.assertIn("copied: loot (2 files", record)                        # the same list, kept in the folder
        self.assertEqual(snapshot(other)["loot/loot_db.json"][0], "file")     # the other one was only read

    def test_a_second_run_copies_nothing(self):
        old = self.old_install(self.home / OLD)
        self.bring()
        (self.new / "loot" / "loot_db.json").write_text('{"xp": 99}', encoding="utf-8")   # the player plays on
        self.said.clear()
        self.assertEqual(self.bring(), 0)
        self.assertIn("already has player data", self.text())
        self.assertEqual((self.new / "loot" / "loot_db.json").read_text(encoding="utf-8"), '{"xp": 99}')
        self.assertEqual((old / "loot" / "loot_db.json").read_text(encoding="utf-8"), '{"xp": 12}')

    def test_what_is_already_here_is_never_overwritten(self):
        old = self.old_install(self.home / OLD)
        (self.new / "maps").mkdir()
        (self.new / "maps" / "mine.bin").write_bytes(b"new")
        rows = dict((item, status) for item, status, _detail in install_helper.copy_data(str(old), str(self.new)))
        self.assertEqual(rows["maps"], "kept")
        self.assertEqual(rows["loot"], "copied")
        self.assertEqual(sorted(p.name for p in (self.new / "maps").iterdir()), ["mine.bin"])

    def test_no_earlier_install_is_said_plainly(self):
        self.assertEqual(self.bring(), 0)
        self.assertIn("No earlier install found under %s" % self.home, self.text())
        self.assertFalse((self.new / "loot").exists())

    def test_from_names_the_folder_to_copy_from(self):
        newest = self.old_install(self.home / OLD, age_days=1)
        wanted = self.old_install(self.home / "old-usb-stick-copy", age_days=90)    # any name: you named it
        (wanted / "loot" / "loot_db.json").write_text('{"xp": 7}', encoding="utf-8")
        self.assertEqual(self.bring(str(wanted)), 0)
        self.assertEqual((self.new / "loot" / "loot_db.json").read_text(encoding="utf-8"), '{"xp": 7}')
        self.assertIn("named with --from", self.text())
        self.assertIn("Not used: %s" % newest, self.text())

    def test_from_a_folder_without_data_copies_nothing_and_fails(self):
        (self.home / "empty").mkdir()
        self.assertEqual(self.bring(str(self.home / "empty")), 1)
        self.assertIn("Nothing was copied", self.text())
        self.assertFalse((self.new / "loot").exists())

    def test_a_copy_that_was_cut_off_is_started_again(self):
        old = self.old_install(self.home / OLD)
        half = self.new / ("loot" + install_helper.PARTIAL)                   # left by a run that was cut off
        half.mkdir()
        (half / "junk").write_text("half", encoding="utf-8")
        self.assertEqual(self.bring(), 0)
        self.assertFalse(half.exists())
        self.assertFalse((self.new / "loot" / "junk").exists())
        self.assertEqual(snapshot(self.new / "loot").keys(), snapshot(old / "loot").keys())

    def test_a_failed_item_is_reported_and_the_rest_still_copied(self):
        old = self.old_install(self.home / OLD)
        before = snapshot(old)
        real = install_helper._copy

        def copy(source, target):
            if os.path.basename(source) == "maps":
                raise OSError("disk full")
            return real(source, target)
        install_helper._copy = copy
        try:
            self.assertEqual(self.bring(), 1)
        finally:
            install_helper._copy = real
        said = self.text()
        self.assertIn("could not copy maps (OSError: disk full). It is untouched in %s" % old, said)
        self.assertIn("copied: loot (", said)
        self.assertFalse((self.new / "maps").exists())
        self.assertEqual(snapshot(old), before)

    def test_links_are_copied_as_links(self):
        old = self.old_install(self.home / OLD)
        try:
            os.symlink("2026-11-20", old / "loot" / "latest")
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("this machine does not let the test make a link")
        self.assertEqual(self.bring(), 0)
        self.assertTrue(os.path.islink(self.new / "loot" / "latest"))
        self.assertEqual(os.readlink(self.new / "loot" / "latest"), "2026-11-20")

    def test_the_command_line(self):
        self.old_install(self.home / OLD)
        self.assertEqual(install_helper.main(["bring-data", str(self.new)]), 2)       # the home folder is missing
        self.assertEqual(install_helper.main(["bring-data", str(self.new), str(self.home)]), 0)
        self.assertTrue((self.new / "loot" / "loot_db.json").is_file())


class Installer(unittest.TestCase):
    """install.sh cannot be run here; what it must do in step 2 is read from its text."""

    def setUp(self):
        self.text = (ROOT / "install.sh").read_text(encoding="utf-8")
        start = self.text.index('step "2/9  Your data"')
        self.step = self.text[start:self.text.index('step "3/9', start)]

    def test_the_first_install_calls_the_helper_with_the_home_folder(self):
        self.assertIn('if [ ! -e "$DIR/loot" ] && [ ! -e "$DIR/secrets.conf" ]; then', self.step)
        self.assertIn('tools/install_helper.py" bring-data "$DIR" "$HOME"', self.step)
        self.assertIn('"--from=$FROM_DIR"', self.step)
        self.assertIn("[bristle]", self.step)

    def test_the_installer_itself_moves_and_deletes_nothing_outside_clean(self):
        copy_part = self.step[self.step.index("# [bristle]"):]                # everything after the --clean branch
        for word in ("rm ", "mv ", "rsync", "cp -a"):
            self.assertNotIn(word, copy_part)

    def test_the_option_is_parsed_and_in_the_help(self):
        self.assertIn('--from=*) FROM_DIR="${arg#--from=}" ;;', self.text)
        self.assertIn('FROM_DIR=""', self.text)
        lines = self.text.splitlines()
        self.assertIn("--from=FOLDER", "\n".join(lines[:32]))
        shown = [int(n) for n in __import__("re").search(r"sed -n '(\d+),(\d+)p'", self.text).groups()]
        self.assertEqual(lines[shown[1] - 1][:5], "# ===")                    # the help ends at the header's last line
        self.assertEqual(lines[shown[1]], "set -u")

    def test_unix_line_endings(self):
        self.assertNotIn(b"\r\n", (ROOT / "install.sh").read_bytes())


if __name__ == "__main__":
    unittest.main()
