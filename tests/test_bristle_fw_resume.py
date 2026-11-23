# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""[bristle] WDG-Bristle 1.2 (BACKTRACK R9): an interrupted C5/SOLO firmware install.

Three things are tested:
  * the journal and what is read back from disk (watchdogs/c5_resume.py), on scratch folders;
  * the game's own flash methods from watchdogs/app.py, run as they are written with the stand-in
    USB, processes and files of tests/test_flash_preflight.py: the write is noted on disk, a write
    that is cut off can be finished from the Firmware screen, and only on the same board;
  * what the Firmware screen says: the board's state in plain words, and the exact steps by hand.

No board and no esptool run here. Whether a real board with a half-written application takes the
write again is a bench item.
"""
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from watchdogs import c5_firmware, c5_resume, legacy_names  # noqa: E402
from watchdogs.esp32_flash import FlashError  # noqa: E402

import test_flash_preflight as harness  # noqa: E402

PACKAGE = SimpleNamespace(version="1.7.0", image_sha256="ab" * 32, image_name="janos-1.7.0-solo-xiao-c5-app.bin")
BOARD = c5_firmware.USBIdentity(0x303A, 0x1001, "DEMO-SERIAL-1", "1-1.2")
OTHER = c5_firmware.USBIdentity(0x303A, 0x1001, "DEMO-SERIAL-2", "1-1.2")
ROLLBACK = b"\x5a" * 4096


class Scratch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.install = Path(self._tmp.name) / "wdg-bristle"
        self.cache = self.install / "firmware_cache"
        self.recovery = self.cache / legacy_names.RECOVERY_DIR
        self.recovery.mkdir(parents=True)

    def backup(self, name="c5-migration-demo", complete=True, age=0.0):
        """A device-backup folder as backup_migration_device leaves it."""
        folder = self.recovery / name
        folder.mkdir()
        (folder / "full-flash-1.bin").write_bytes(b"\0" * 16)
        if complete:
            (folder / "full-flash-2.bin").write_bytes(b"\0" * 16)
            (folder / "original-application-sectors.bin").write_bytes(ROLLBACK)
            (folder / "backup.json").write_text(json.dumps({
                "schema": 1, "status": "complete", "target_package": PACKAGE.version,
                "target_image_sha256": PACKAGE.image_sha256,
                "usb_binding": {"vid": BOARD.vid, "pid": BOARD.pid, "serial_number": BOARD.serial_number,
                                "location": BOARD.location},
                "application_rollback": {"path": "original-application-sectors.bin", "offset": 0x20000,
                                         "bytes": len(ROLLBACK), "sha256": hashlib.sha256(ROLLBACK).hexdigest()}}),
                encoding="utf-8")
        if age:
            stamp = time.time() - age
            os.utime(folder, (stamp, stamp))
        return folder

    def leftover(self, name="flash-abc123", age=0.0):
        folder = self.cache / name
        folder.mkdir()
        (folder / "c5-partition-table.bin").write_bytes(b"\0" * 8)
        if age:
            stamp = time.time() - age
            os.utime(folder, (stamp, stamp))
        return folder


class Journal(Scratch):
    def test_the_states_are_written_and_read_back(self):
        folder = self.backup()
        self.assertTrue(c5_resume.record(folder, c5_resume.STARTED, PACKAGE, BOARD, now=1000.0))
        entry = json.loads((folder / c5_resume.JOURNAL).read_text(encoding="utf-8"))
        self.assertEqual((entry["state"], entry["attempts"], entry["target_package"]), ("write-started", 1, "1.7.0"))
        self.assertEqual(entry["usb_binding"]["serial_number"], "DEMO-SERIAL-1")
        self.assertTrue(c5_resume.record(folder, c5_resume.WRITTEN, now=1010.0))
        self.assertTrue(c5_resume.record(folder, c5_resume.STARTED, PACKAGE, BOARD, now=1020.0))   # tried again
        entry = json.loads((folder / c5_resume.JOURNAL).read_text(encoding="utf-8"))
        self.assertEqual((entry["state"], entry["attempts"], entry["started_at"]), ("write-started", 2, 1000.0))
        self.assertTrue(c5_resume.record(folder, c5_resume.CONFIRMED, now=1030.0))
        self.assertTrue(c5_resume.record(folder, c5_resume.STARTED, PACKAGE, BOARD, now=2000.0))   # a later install
        entry = json.loads((folder / c5_resume.JOURNAL).read_text(encoding="utf-8"))
        self.assertEqual((entry["attempts"], entry["started_at"]), (1, 2000.0))                    # a new record
        self.assertEqual([p.name for p in folder.glob("*.tmp")], [])

    def test_a_note_that_cannot_be_written_never_raises(self):
        self.assertFalse(c5_resume.record(self.recovery / "missing", c5_resume.STARTED, PACKAGE, BOARD))
        self.assertFalse(c5_resume.record(None, c5_resume.STARTED, PACKAGE, BOARD))
        self.assertFalse(c5_resume.record(self.backup(), "nonsense"))
        fresh = self.backup("c5-migration-two")
        self.assertFalse(c5_resume.record(fresh, c5_resume.WRITTEN))          # nothing was started there
        self.assertFalse((fresh / c5_resume.JOURNAL).exists())

    def test_the_journal_file_is_private(self):
        folder = self.backup()
        c5_resume.record(folder, c5_resume.STARTED, PACKAGE, BOARD)
        if os.name == "posix":
            self.assertEqual(os.stat(folder / c5_resume.JOURNAL).st_mode & 0o077, 0)


class Finding(Scratch):
    def test_nothing_on_disk_means_nothing_pending(self):
        self.assertIsNone(c5_resume.find(self.cache))
        self.assertIsNone(c5_resume.find(self.install / "no-such-folder"))

    def test_reads_that_were_cut_off_mean_the_board_is_unchanged(self):
        self.backup(complete=False)
        pending = c5_resume.find(self.cache)
        self.assertEqual(pending.kind, c5_resume.BEFORE_WRITE)
        self.assertFalse(pending.resumable)
        self.assertFalse(pending.urgent)

    def test_a_write_that_began_and_did_not_finish(self):
        folder = self.backup()
        c5_resume.record(folder, c5_resume.STARTED, PACKAGE, BOARD)
        pending = c5_resume.find(self.cache)
        self.assertEqual(pending.kind, c5_resume.INTERRUPTED)
        self.assertTrue(pending.resumable and pending.urgent)
        self.assertEqual((pending.package_version, pending.image_sha256), (PACKAGE.version, PACKAGE.image_sha256))
        self.assertEqual(pending.binding["serial_number"], BOARD.serial_number)
        self.assertEqual(pending.rollback_bytes, len(ROLLBACK))

    def test_a_write_that_finished_without_an_answer(self):
        folder = self.backup()
        c5_resume.record(folder, c5_resume.STARTED, PACKAGE, BOARD)
        c5_resume.record(folder, c5_resume.WRITTEN)
        self.assertEqual(c5_resume.find(self.cache).kind, c5_resume.UNCONFIRMED)

    def test_a_backup_from_before_the_journal_existed(self):
        self.backup()
        pending = c5_resume.find(self.cache)
        self.assertEqual(pending.kind, c5_resume.NO_RECORD)
        self.assertTrue(pending.resumable)
        self.assertFalse(pending.urgent)

    def test_only_the_newest_backup_counts(self):
        old = self.backup("c5-migration-old", age=5000)
        c5_resume.record(old, c5_resume.STARTED, PACKAGE, BOARD)
        os.utime(old, (time.time() - 5000,) * 2)
        new = self.backup("c5-migration-new")
        c5_resume.record(new, c5_resume.STARTED, PACKAGE, BOARD)
        c5_resume.record(new, c5_resume.WRITTEN)
        c5_resume.record(new, c5_resume.CONFIRMED)
        self.assertIsNone(c5_resume.find(self.cache))                         # the newest one ended well

    def test_a_leftover_download_folder_is_a_weaker_sign(self):
        self.leftover()
        pending = c5_resume.find(self.cache)
        self.assertEqual(pending.kind, c5_resume.CUT_OFF)
        self.assertFalse(pending.resumable)
        self.assertIsNone(c5_resume.find(self.cache, running_workspace=self.cache / "flash-abc123"))   # ours, running now

    def test_a_leftover_older_than_a_finished_install_is_history(self):
        self.leftover(age=9000)
        folder = self.backup()
        c5_resume.record(folder, c5_resume.STARTED, PACKAGE, BOARD)
        c5_resume.record(folder, c5_resume.CONFIRMED)
        self.assertIsNone(c5_resume.find(self.cache))

    def test_an_ordinary_install_has_a_journal_and_no_backup(self):
        folder = self.recovery / c5_resume.INSTALL_FOLDER
        folder.mkdir()
        c5_resume.record(folder, c5_resume.STARTED, PACKAGE, BOARD)
        pending = c5_resume.find(self.cache)
        self.assertEqual(pending.kind, c5_resume.INTERRUPTED)
        self.assertTrue(pending.resumable)
        self.assertEqual(pending.rollback_name, "")


class BoardAnswers(Scratch):
    def test_the_expected_version_confirms_the_write(self):
        folder = self.backup()
        c5_resume.record(folder, c5_resume.STARTED, PACKAGE, BOARD)
        c5_resume.record(folder, c5_resume.WRITTEN)
        self.assertEqual(c5_resume.board_answered(self.cache, "1.7.0"), c5_resume.CONFIRMED)
        self.assertIsNone(c5_resume.find(self.cache))

    def test_another_version_closes_it_the_board_runs(self):
        folder = self.backup()
        c5_resume.record(folder, c5_resume.STARTED, PACKAGE, BOARD)
        self.assertEqual(c5_resume.board_answered(self.cache, "1.7.5"), c5_resume.CLOSED)   # put back by hand
        self.assertIsNone(c5_resume.find(self.cache))
        self.assertEqual(c5_resume.board_answered(self.cache, "1.7.5"), "")   # nothing open any more

    def test_weaker_signs_are_dropped_once_the_board_has_answered(self):
        self.backup(complete=False, age=50)
        self.leftover(age=40)
        self.assertIsNotNone(c5_resume.find(self.cache))
        c5_resume.board_answered(self.cache, "1.7.0")
        self.assertIsNone(c5_resume.find(self.cache))
        self.leftover("flash-later")                                          # a new one after that is said again
        os.utime(self.cache / "flash-later", (time.time() + 5,) * 2)
        self.assertEqual(c5_resume.find(self.cache).kind, c5_resume.CUT_OFF)

    def test_a_backup_without_a_record_is_closed_when_the_board_answers(self):
        folder = self.backup()
        self.assertEqual(c5_resume.board_answered(self.cache, "1.7.0"), c5_resume.CLOSED)
        self.assertIsNone(c5_resume.find(self.cache))
        self.assertTrue((folder / "backup.json").is_file())                   # the backup itself is untouched
        self.assertEqual((folder / "original-application-sectors.bin").read_bytes(), ROLLBACK)


class Gate(Scratch):
    def pending(self):
        folder = self.backup()
        c5_resume.record(folder, c5_resume.STARTED, PACKAGE, BOARD)
        return c5_resume.find(self.cache)

    def test_the_same_image_and_the_same_board_pass(self):
        c5_resume.check_resume(self.pending(), PACKAGE, BOARD, FlashError)

    def test_everything_else_is_refused(self):
        pending = self.pending()
        other_image = SimpleNamespace(version="1.7.0", image_sha256="cd" * 32)
        other_version = SimpleNamespace(version="1.8.0", image_sha256=PACKAGE.image_sha256)
        for name, args in (("another board", (pending, PACKAGE, OTHER)), ("another image", (pending, other_image, BOARD)),
                           ("another version", (pending, other_version, BOARD)), ("no board", (pending, PACKAGE, None)),
                           ("no package", (pending, None, BOARD)), ("nothing pending", (None, PACKAGE, BOARD))):
            with self.subTest(case=name):
                with self.assertRaises(FlashError):
                    c5_resume.check_resume(*args, FlashError)

    def test_a_damaged_or_missing_device_backup_is_refused(self):
        pending = self.pending()
        (pending.directory / "original-application-sectors.bin").write_bytes(b"\x00" * len(ROLLBACK))
        with self.assertRaisesRegex(FlashError, "not intact"):
            c5_resume.check_resume(pending, PACKAGE, BOARD, FlashError)
        os.remove(pending.directory / "original-application-sectors.bin")
        with self.assertRaisesRegex(FlashError, "cannot be read"):
            c5_resume.check_resume(pending, PACKAGE, BOARD, FlashError)

    def test_what_cannot_be_finished_from_the_game(self):
        self.backup(complete=False)
        with self.assertRaises(FlashError):
            c5_resume.check_resume(c5_resume.find(self.cache), PACKAGE, BOARD, FlashError)

    def test_a_board_without_a_serial_number_is_refused(self):
        folder = self.backup()
        c5_resume.record(folder, c5_resume.STARTED, PACKAGE, c5_firmware.USBIdentity(0x303A, 0x1001, "", "1-1"))
        with self.assertRaises(FlashError):
            c5_resume.check_resume(c5_resume.find(self.cache), PACKAGE, c5_firmware.USBIdentity(0x303A, 0x1001, "", "1-1"),
                                   FlashError)


class Words(Scratch):
    def every_kind(self):
        out = {}
        self.backup("a", complete=False)
        out[c5_resume.BEFORE_WRITE] = c5_resume.find(self.cache)
        folder = self.backup("b")
        os.utime(folder, (time.time() + 10,) * 2)
        out[c5_resume.NO_RECORD] = c5_resume.find(self.cache)
        c5_resume.record(folder, c5_resume.STARTED, PACKAGE, BOARD)
        out[c5_resume.INTERRUPTED] = c5_resume.find(self.cache)
        c5_resume.record(folder, c5_resume.WRITTEN)
        out[c5_resume.UNCONFIRMED] = c5_resume.find(self.cache)
        out[c5_resume.CUT_OFF] = c5_resume.Pending(c5_resume.CUT_OFF, self.cache / "flash-x", time.time())
        return out

    def test_every_state_is_said_in_plain_words_that_fit_the_box(self):
        for kind, pending in self.every_kind().items():
            with self.subTest(kind=kind):
                self.assertEqual(pending.kind, kind)
                lines = c5_resume.state_lines(pending)
                self.assertTrue(lines[0][1])                                  # a heading first
                self.assertLessEqual(len(lines), 15)                          # the rows the box has
                for text, _heading in lines:
                    self.assertLessEqual(len(text), c5_resume.WIDTH)
                self.assertLessEqual(len(c5_resume.summary_line(pending)), c5_resume.WIDTH)
                self.assertTrue(c5_resume.summary_line(pending))

    def test_the_interrupted_write_says_what_was_and_was_not_touched(self):
        interrupted = self.every_kind()[c5_resume.INTERRUPTED]
        said = " ".join(text for text, _ in c5_resume.state_lines(interrupted))
        self.assertIn("A FIRMWARE WRITE DID NOT FINISH", said)
        self.assertIn("will not start its firmware until the image is written again", said)
        self.assertIn("bootloader, partition table and settings were not touched", said)
        self.assertIn("original application is saved", said)
        self.assertIn("press F", said)
        self.assertIn("hold its BOOT button", said)
        answered = " ".join(text for text, _ in c5_resume.state_lines(interrupted, "1.7.0"))
        self.assertIn("The board answers now (JanOS 1.7.0)", answered)

    def test_reads_only_says_the_board_is_unchanged(self):
        said = " ".join(text for text, _ in c5_resume.state_lines(self.every_kind()[c5_resume.BEFORE_WRITE]))
        self.assertIn("Nothing on it was changed", said)

    def test_the_steps_by_hand_are_exact_commands(self):
        kinds = self.every_kind()
        pending = kinds[c5_resume.INTERRUPTED]
        steps = c5_resume.hand_steps(pending, self.install, PACKAGE.image_name)
        text = "\n".join(steps)
        self.assertIn('T=".venv/bin/python3 -m esptool --chip esp32c5 -p PORT --no-stub"', text)
        self.assertIn("$T write-flash 0x20000 firmware/wardriving-solo/janos-1.7.0-solo-xiao-c5-app.bin", text)
        self.assertIn("$T write-flash 0x20000 firmware_cache/%s/b/original-application-sectors.bin"
                      % legacy_names.RECOVERY_DIR, text)
        self.assertIn("cd %s" % self.install.as_posix(), text)
        self.assertIn("Hold its BOOT button", text)
        self.assertIn("ls /dev/ttyACM*", text)
        self.assertIn("Never erase the whole flash", text)
        self.assertNotIn("erase-flash", text)
        for line in steps:
            self.assertLessEqual(len(line), 126, line)                        # one terminal line each
        nothing = c5_resume.hand_steps(kinds[c5_resume.BEFORE_WRITE], self.install)
        self.assertEqual(len(nothing), 1)
        self.assertIn("the board was not written", nothing[0])

    def test_the_steps_are_kept_next_to_the_backup(self):
        pending = self.every_kind()[c5_resume.INTERRUPTED]
        steps = c5_resume.hand_steps(pending, self.install, PACKAGE.image_name)
        path = c5_resume.write_steps_file(pending, steps)
        self.assertEqual(Path(path).name, c5_resume.STEPS_FILE)
        self.assertEqual(Path(path).read_text(encoding="utf-8").splitlines(), steps)
        gone = c5_resume.Pending(c5_resume.CUT_OFF, self.cache / "no-such-folder")
        self.assertEqual(c5_resume.write_steps_file(gone, steps), "")


NOTE_METHODS = {"_fwnote_journal", "_fwnote_scan", "_fwnote_steps"}


class Game(unittest.TestCase):
    """The game's own flash methods (watchdogs/app.py), with tests/test_flash_preflight.py's stand-ins."""

    def environment(self):
        with patch.object(harness, "METHODS", set(harness.METHODS) | NOTE_METHODS):
            env = harness.FlashEnvironment(aio=False, allow_upstream=False)
        self.addCleanup(env.close)
        builtins_ = env.namespace["__builtins__"]
        refuse = builtins_["__import__"]

        def importer(name, globals=None, locals=None, fromlist=(), level=0):
            if level == 1 and name == "" and "c5_resume" in (fromlist or ()):
                return SimpleNamespace(c5_resume=c5_resume)
            return refuse(name, globals, locals, fromlist, level)
        builtins_["__import__"] = importer
        env.px.KEY_F = 11
        env.cache = env.root / "firmware_cache"
        return env

    def run_threads(self, env, name):
        """The stand-in threads never run by themselves: run the ones with this target now."""
        ran = 0
        for thread in list(env.threads):
            if thread.target.__name__ == name and not getattr(thread, "ran", False):
                thread.ran = True
                thread.target(*thread.args, **thread.kwargs)
                ran += 1
        return ran

    def journal(self, env):
        return json.loads((env.cache / legacy_names.RECOVERY_DIR / c5_resume.INSTALL_FOLDER / c5_resume.JOURNAL)
                          .read_text(encoding="utf-8"))

    def test_a_good_install_is_noted_and_confirmed_by_the_boards_answer(self):
        env = self.environment()
        env.flash()
        self.assertEqual(len(env.flash_commands), 1)
        entry = self.journal(env)
        self.assertEqual((entry["state"], entry["target_package"]), ("write-complete", "1.7.0"))
        self.assertEqual(entry["usb_binding"]["serial_number"], env.c5_identity.serial_number)
        self.run_threads(env, "work")
        self.assertEqual(env.game._c5_pending.kind, c5_resume.UNCONFIRMED)    # written, not answered yet
        game = env.game
        game.loot = None
        game._solo = SimpleNamespace(confirmed_wifi=False, confirmed_ble=False, handle_line=lambda line: True)
        game._handle_serial_line("JanOS version: 1.7.0")
        self.run_threads(env, "work")
        self.assertEqual(self.journal(env)["state"], "confirmed")
        self.assertIsNone(game._c5_pending)

    def test_the_note_is_written_before_the_write_begins(self):
        env = self.environment()
        seen = {}
        env.during_flash = lambda game: seen.update(state=self.journal(env)["state"])
        env.flash()
        self.assertEqual(seen, {"state": "write-started"})

    def cut_off(self):
        """An install whose write was cut off, and the game started again with a silent board."""
        env = self.environment()
        env.flash_error = FlashError("Synthetic power cut during the write")
        env.flash()
        self.assertEqual(self.journal(env)["state"], "write-started")
        self.run_threads(env, "work")
        pending = env.game._c5_pending
        self.assertEqual(pending.kind, c5_resume.INTERRUPTED)
        env.flash_error = None
        env.flash_commands.clear()
        env.c5_calls.clear()
        env.output.clear()
        env.game._fw_version = ""                                             # the board no longer answers
        env.game._flash_fw_pending = False
        return env, pending

    def test_without_the_record_a_silent_board_cannot_be_written(self):
        env, _pending = self.cut_off()
        self.assertTrue(env.game._esp_stop_waiting)                           # the "stop" it was sent is unanswered
        env.flash()
        self.assertEqual(env.flash_commands, [])                              # refused: ESP32 operations look active
        env.game._esp_stop_waiting = False                                    # ... and with a clean slate:
        env.output.clear()
        env.flash()
        self.assertEqual(env.flash_commands, [])
        self.assertIn("A fresh reported JanOS version is required", "\n".join(env.output))

    def test_the_cut_off_write_is_finished_with_the_same_image_on_the_same_board(self):
        env, pending = self.cut_off()
        env.game._fw_resume = pending
        env.flash()
        self.assertEqual(len(env.flash_commands), 1)
        command = env.flash_commands[0][0]
        tail = command[command.index("write-flash") + 1:]
        self.assertEqual(tail[0], "0x20000")
        self.assertTrue(tail[1].endswith("janos-1.7.0-solo-xiao-c5-app.bin"))
        self.assertEqual(len(tail), 2)                                        # application only, nothing else
        for option in ("erase-flash", "--force", "--flash-size"):
            self.assertNotIn(option, command)
        self.assertTrue(any(call[0] == "preflight" for call in env.c5_calls))   # the read-only checks ran first
        entry = self.journal(env)
        self.assertEqual((entry["state"], entry["attempts"]), ("write-complete", 2))
        self.assertIsNone(env.game._fw_resume)                                # one confirmed finish per job
        self.assertEqual(env.game._fw_expected_version, "1.7.0")
        self.assertFalse(env.game._flash_running)
        self.assertFalse(env.game._flash_job_lock.locked())

    def test_another_board_is_never_written(self):
        env, pending = self.cut_off()
        env.c5_identity = c5_firmware.USBIdentity(0x303A, 0x1001, "ANOTHER-SYNTHETIC-DEVICE", "SYNTHETIC-USB-PORT")
        env.game._fw_resume = pending
        env.flash()
        self.assertEqual(env.flash_commands, [])
        self.assertFalse(any(call[0] == "preflight" for call in env.c5_calls))
        self.assertIn("not the board the interrupted write was for", "\n".join(env.output))
        self.assertEqual(self.journal(env)["state"], "write-started")         # still open for the right board
        self.assertIsNone(env.game._fw_resume)

    def test_f_shows_the_state_lists_the_steps_and_y_starts_the_finish(self):
        env, pending = self.cut_off()
        game = env.game
        game._flash_running = False
        game._flash_screen = True
        game._fw_package, game._fw_error, game._c5_esptool_error = env.c5_package, "", ""
        env.draw_calls.clear()
        game._draw_flash_screen()                                             # the main page says it in one line
        texts = [args[2] for name, args in env.draw_calls if name == "text"]
        self.assertIn(c5_resume.summary_line(pending), texts)
        self.assertTrue(any(text.startswith("F unfinished install") for text in texts))
        self.assertTrue(all(len(text) <= 82 for text in texts))
        env.buttons = {env.px.KEY_F}
        game._update_flash_screen()
        self.assertTrue(game._c5_pending_page)
        self.assertEqual(self.run_threads(env, "work"), 1)                    # the steps, written off the game thread
        said = "\n".join(env.output)
        self.assertIn("-m esptool --chip esp32c5 -p PORT --no-stub", said)
        self.assertIn("$T write-flash 0x20000 firmware/wardriving-solo/janos-1.7.0-solo-xiao-c5-app.bin", said)
        self.assertTrue((pending.directory / c5_resume.STEPS_FILE).is_file())
        env.draw_calls.clear()
        game._draw_flash_screen()
        texts = [args[2] for name, args in env.draw_calls if name == "text"]
        self.assertIn("A FIRMWARE WRITE DID NOT FINISH", texts[0])
        self.assertIn("will not start its firmware until the image is written again", " ".join(texts))
        self.assertIn("Y finish the write now   N / ESC back", texts)
        self.assertTrue(all(len(text) <= 80 for text in texts))
        env.buttons = {env.px.KEY_Y}
        game._update_flash_screen()
        self.assertFalse(game._c5_pending_page)
        self.assertIs(game._fw_resume, pending)
        jobs = [t for t in env.threads if t.target.__name__ == "_run_flash"]
        self.assertEqual(len(jobs), 1)
        jobs[0].target(*jobs[0].args)
        self.assertEqual(len(env.flash_commands), 1)
        self.assertEqual(self.journal(env)["state"], "write-complete")

    def test_n_or_escape_leaves_the_page_without_a_job(self):
        for key in ("KEY_N", "KEY_ESCAPE"):
            with self.subTest(key=key):
                env, _pending = self.cut_off()
                game = env.game
                game._flash_running, game._flash_screen, game._c5_pending_page = False, True, True
                env.buttons = {getattr(env.px, key)}
                game._update_flash_screen()
                self.assertFalse(game._c5_pending_page)
                self.assertTrue(game._flash_screen)                           # back on the Firmware screen, not closed
                self.assertIsNone(getattr(game, "_fw_resume", None))
                self.assertFalse(any(t.target.__name__ == "_run_flash" for t in env.threads))

    def test_y_needs_the_package_checked_first(self):
        env, _pending = self.cut_off()
        game = env.game
        game._flash_running, game._flash_screen, game._c5_pending_page = False, True, True
        game._fw_package, game._fw_error = None, ""
        env.buttons = {env.px.KEY_Y}
        game._update_flash_screen()
        self.assertIn("Press R", game._flash_status)
        self.assertFalse(any(t.target.__name__ == "_run_flash" for t in env.threads))

    def test_what_cannot_be_finished_offers_no_y(self):
        env = self.environment()
        game = env.game
        (env.cache / "flash-leftover").mkdir(parents=True)
        game._fwnote_scan()
        self.run_threads(env, "work")
        self.assertEqual(game._c5_pending.kind, c5_resume.CUT_OFF)
        game._flash_running, game._flash_screen, game._c5_pending_page = False, True, True
        game._fw_package, game._fw_error, game._c5_esptool_error = env.c5_package, "", ""
        env.draw_calls.clear()
        game._draw_flash_screen()
        texts = [args[2] for name, args in env.draw_calls if name == "text"]
        self.assertIn("N / ESC back", texts)
        env.buttons = {env.px.KEY_Y}
        game._update_flash_screen()
        self.assertFalse(any(t.target.__name__ == "_run_flash" for t in env.threads))

    def test_a_weak_sign_is_only_mentioned_while_the_board_is_silent(self):
        env = self.environment()
        game = env.game
        (env.cache / "flash-leftover").mkdir(parents=True)
        game._fwnote_scan()
        self.run_threads(env, "work")
        game._flash_running, game._flash_screen = False, True
        game._fw_package, game._fw_error, game._c5_esptool_error = env.c5_package, "", ""
        game._draw_flash_screen()                                             # the board answers (1.6.5): nothing said
        texts = [args[2] for name, args in env.draw_calls if name == "text"]
        self.assertNotIn(c5_resume.summary_line(game._c5_pending), texts)
        self.assertIn("P repair Python  R check  M migrate  ENTER install  ESC close", texts)
        game._fw_version = ""
        env.draw_calls.clear()
        game._draw_flash_screen()
        texts = [args[2] for name, args in env.draw_calls if name == "text"]
        self.assertIn(c5_resume.summary_line(game._c5_pending), texts)

    def test_the_edits_in_the_games_file_are_tagged(self):
        text = (ROOT / "watchdogs" / "app.py").read_text(encoding="utf-8")
        for needle in ('getattr(self, "_fw_resume", None) is not None:', '_journal("write-started", package, identity)',
                       'if getattr(self, "_c5_pending_page", False):', "def _fwnote_journal("):
            at = text.index(needle)
            self.assertIn("[bristle]", text[max(0, at - 600):at + 300], needle)


if __name__ == "__main__":
    unittest.main()
