"""[merge-v3] The smaller WDGoNG-derived extras: 433 Hunter detection maths,
the Flipper .sub repair, the session log browser and the rolling safety copy
of loot_db.json. Process-level behaviour (rtl_power, rtl_433 files) is in
tools/sim (test_v3_sim.py)."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from watchdogs import plugin_loader
from plugins import _loot_backup as engine
from plugins._ng import subghz_export
import plugins.ng_433_hunter as hunter
import plugins.ng_session_log as session_log
import plugins.ng_subghz_capture as capture
import plugins.ng_loot_backup as ng_backup

import test_v3_ng_plugins as ng

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    from tools.sim import flipper_sub  # noqa: E402
except ImportError:                    # [bristle] a build without tools/sim
    flipper_sub = None

BINS = 150
LOW, STEP = 433_400_000.0, 8_000.0
TARGET_BIN = int((hunter.HUNT_TARGET - LOW) / STEP)


def row(values):
    return "2026-11-21, 12:00:00, %d, %d, %.2f, 1, %s" % (LOW, LOW + BINS * STEP, STEP,
                                                         ", ".join("%.2f" % v for v in values))


def quiet(carrier=True):
    values = [-40.0] * BINS
    if carrier:
        values[64] = -10.0                     # a constant carrier: must become background
    return values


class HuntEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = hunter.HuntEngine()
        for _ in range(5):
            self.assertTrue(self.engine.feed(row(quiet())))

    def press(self, level, where=TARGET_BIN):
        values = quiet()
        values[where] = -40.0 + level
        self.engine.feed(row(values), now=100.0)

    def test_a_press_at_the_target_is_a_burst_and_closer_is_hotter(self):
        self.assertFalse(self.engine.burst)
        self.assertEqual(self.engine.hot, 0.0)
        self.press(15)                                       # under the 18 dB threshold
        self.assertFalse(self.engine.burst)
        self.press(25)
        self.assertTrue(self.engine.burst)
        self.assertEqual(self.engine.hits, 1)
        first = self.engine.hot
        self.press(40)
        self.assertGreater(self.engine.hot, first)
        self.assertEqual(self.engine.hits, 1)                # the same held press
        self.assertAlmostEqual(self.engine.peak_mhz, 434.412, places=2)
        self.engine.feed(row(quiet()))
        self.assertFalse(self.engine.burst)
        self.press(30)
        self.assertEqual(self.engine.hits, 2)
        self.assertAlmostEqual(self.engine.best, 40.0, delta=0.5)

    def test_constant_carriers_and_other_remotes_are_not_the_target(self):
        self.assertLess(abs(self.engine.rise[64]), 1.0)      # the carrier is background by now
        self.press(35, where=20)                             # a strong signal elsewhere in the band
        self.assertFalse(self.engine.burst)
        self.assertGreater(self.engine.other_db, 30)

    def test_the_whole_band_rising_means_right_here(self):
        self.engine.feed(row([value + 15 for value in quiet()]))
        self.assertTrue(self.engine.overload)
        self.assertEqual(self.engine.hot, 1.0)

    def test_a_held_remote_never_becomes_background(self):
        for _ in range(60):
            self.press(30)
        self.assertTrue(self.engine.burst)
        self.assertGreater(self.engine.jump, 25)

    def test_learning_takes_the_strongest_new_signal(self):
        self.engine.learning = True
        self.press(10, where=90)                             # too weak to learn
        self.assertTrue(self.engine.learning)
        self.press(30, where=90)
        self.assertFalse(self.engine.learning)
        self.assertAlmostEqual(self.engine.learned, LOW + STEP * 90.5)
        self.press(30, where=90)
        self.assertTrue(self.engine.burst)                   # the new target is hunted now

    def test_lines_that_are_not_rows_are_ignored(self):
        sweeps = self.engine.sweeps
        for line in ("", "garbage", "a,b,c,d,e,f,g,h", row([float("nan")] * BINS), "1,2,3,4,0,6,7,8",
                     "2026, 12:00, x, y, z, 1, 2, 3"):
            self.assertFalse(self.engine.feed(line))
        self.assertEqual(self.engine.sweeps, sweeps)
        self.assertTrue(self.engine.feed(row([-40.0] * 12)))  # another bin count: starts over, no crash

    def test_plugin_refuses_politely_and_never_transmits(self):
        plugin = hunter.Ng433Hunter()
        plugin.app = SimpleNamespace(_sdr=SimpleNamespace(running=True, mode="433"), msg=Mock(), _term_add=Mock())
        self.assertFalse(plugin.start())
        self.assertIn("busy (433)", plugin._status)
        plugin.app = SimpleNamespace(_sdr=SimpleNamespace(running=False), _aio_available=True, _sdr_enabled=False,
                                     msg=Mock(), _term_add=Mock())
        self.assertFalse(plugin.start())
        self.assertIn("SDR rail is off", plugin._status)
        self.assertEqual(plugin.command()[:2], ["rtl_power", "-f"])
        source = (ROOT / "plugins" / "ng_433_hunter.py").read_text(encoding="utf-8")
        for forbidden in ("rtl_tx", "hackrf", "sendraw", "tx_", "shell=True"):
            self.assertNotIn(forbidden, source)
        self.assertIn("Required Notice: Copyright (c) 2025-2026 LOCOSP", source)


@unittest.skipUnless(flipper_sub, "the .sub format checker (tools/sim) is not part of this build")
class SubRepairTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def sub(self, pulses, name="g001_433.92M_250k.sub"):
        path = self.root / name
        subghz_export.write_flipper_sub(pulses, 433_920_000, str(path))
        return path

    def test_same_sign_neighbours_are_joined(self):
        path = self.sub([500, 300, -400, -100, 700, -200, 600])
        with self.assertRaises(ValueError):
            flipper_sub.validate(path.read_text())            # what WDGoNG's converter can write
        out, count = capture.repair_raw_sub(str(path), 7)
        self.assertEqual((out, count), (str(path), 5))
        self.assertEqual(flipper_sub.validate(path.read_text()), [800, -500, 700, -200, 600])

    def test_valid_files_are_left_byte_for_byte(self):
        path = self.sub([500, -400, 700])
        before = path.read_bytes()
        self.assertEqual(capture.repair_raw_sub(str(path), 3), (str(path), 3))
        self.assertEqual(path.read_bytes(), before)

    def test_edges_and_long_files(self):
        path = self.sub([-50, -60, 500, -400, 700, -90])      # gaps at both ends
        capture.repair_raw_sub(str(path), 6)
        self.assertEqual(flipper_sub.validate(path.read_text()), [500, -400, 700])
        long = self.sub([120, -240] * 900 + [50, 60])
        capture.repair_raw_sub(str(long), 1802)
        timings = flipper_sub.validate(long.read_text())      # also checks 512 values per line
        self.assertEqual(len(timings), 1801)
        nothing = self.sub([300, 200])                        # one run only: no signal
        self.assertEqual(capture.repair_raw_sub(str(nothing), 2), (None, 0))
        self.assertFalse(nothing.exists())

    def test_ngs_converter_on_a_glitchy_recording_ends_up_valid(self):
        recording = self.root / "g009_433.92M_250k.cu8"
        off, on = bytes((127, 128)), bytes((255, 128))
        data = off * 400
        for _ in range(12):                                   # pulses with one-sample dropouts inside
            data += on * 60 + off * 2 + on * 60 + off * 150
        recording.write_bytes(data + off * 400)
        out, pulses = subghz_export.convert_cu8(str(recording))
        out, pulses = capture.repair_raw_sub(out, pulses)
        timings = flipper_sub.validate(Path(out).read_text())
        self.assertEqual(len(timings), pulses)
        self.assertEqual(len(timings), 23)                    # 12 pulses, 11 gaps

    def test_the_reference_sample_and_the_checker_agree(self):
        self.assertEqual(len(flipper_sub.validate(flipper_sub.REFERENCE)), 22)
        for broken, reason in (("Filetype: Flipper SubGhz Key File", "Filetype"), ("RAW_Data: 0 5", "zero"),
                               ("RAW_Data: -5 5", "positive"), ("RAW_Data: 5 5", "same sign"),
                               ("RAW_Data: " + " ".join(["5", "-5"] * 300), "512")):
            text = flipper_sub.REFERENCE
            if broken.startswith("Filetype"):
                text = text.replace("Filetype: Flipper SubGhz RAW File", broken)
            else:
                text = "\n".join(text.splitlines()[:5] + [broken]) + "\n"
            with self.assertRaises(ValueError, msg=reason):
                flipper_sub.validate(text)


class SessionLogTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        loot = self.root / "loot"
        for name, files in (("2026-11-20_10-00-00", {"wardriving.csv": "a,b\n" * 300, "captures_433/x/g1.sub": "RAW"}),
                            ("2026-11-21_09-30-00", {"adsb_aircraft.csv": "t,icao\n", "blob.bin": b"\x00\x01" * 50,
                                                     "long.log": "x" * 500 + "\n" + "tab\there\n"}),
                            ("kismet_logs", {"k.kismet": "K"}), (".piglet_collection", {"i.sqlite3": "S"}),
                            ("loot.pre-restore-2026-01-01_00-00-00", {"old": "o"})):
            for relative, content in files.items():
                path = loot / name / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content if isinstance(content, bytes) else content.encode())
        (loot / "loot_db.json").write_text(json.dumps({"version": 2, "sessions": {
            "2026-11-20_10-00-00": {"wifi": 300, "bt": 0, "hs": 2, "adsb": True}}}))
        self.loot = loot

    def test_scan_lists_real_sessions_newest_first_with_totals(self):
        sessions = session_log.scan(self.loot)
        self.assertEqual([s["name"] for s in sessions], ["2026-11-21_09-30-00", "2026-11-20_10-00-00"])
        self.assertEqual(sessions[1]["counts"], [("WiFi", 300), ("HS", 2)])
        self.assertEqual(sessions[0]["counts"], [])
        self.assertIn(("captures_433/x/g1.sub", 3), sessions[1]["files"])
        self.assertEqual(session_log.scan(self.root / "nowhere"), [])
        (self.loot / "loot_db.json").write_text("{broken")
        self.assertEqual(len(session_log.scan(self.loot)), 2)       # a damaged database: files only

    def test_preview_is_bounded_and_printable(self):
        lines = session_log.preview(self.loot / "2026-11-21_09-30-00" / "long.log")
        self.assertEqual(len(lines[0]), session_log.PREVIEW_COLS)
        self.assertEqual(lines[1], "tab here")
        self.assertIn("binary file", session_log.preview(self.loot / "2026-11-21_09-30-00" / "blob.bin")[0])
        big = self.loot / "2026-11-20_10-00-00" / "big.csv"
        big.write_bytes(b"row\n" * 100_000)
        lines = session_log.preview(big)
        self.assertLess(len(lines), 30_000)
        self.assertIn("first 96.0 KB shown", lines[-1])
        self.assertIn("cannot read", session_log.preview(self.loot / "missing")[0])

    def test_screen_navigates_previews_and_changes_nothing(self):
        before = {str(p): p.stat().st_mtime_ns for p in self.loot.rglob("*") if p.is_file()}
        plugin = session_log.NgSessionLog()
        plugin.on_load(SimpleNamespace(_app_dir=str(self.root), _esc_consumed_frame=-1))
        canvas = ng.Canvas()

        def frame(*keys):
            canvas.pressed = set(keys)
            plugin.on_update()
            deadline = time.perf_counter() + 5
            while plugin._busy and time.perf_counter() < deadline:
                time.sleep(0.005)
            canvas.new_frame()
            plugin.draw(0, 0, ng.W, ng.H)
            self.assertEqual(canvas.overlaps(), [])
            canvas.pressed = set()

        with ng.with_pyxel(canvas):
            plugin.open_overlay()
            frame()
            self.assertIn("SESSIONS (2, newest first)", canvas.texts)
            frame("KEY_DOWN")
            self.assertTrue(any(text.startswith("WiFi:300  HS:2") for text in canvas.texts))
            frame("KEY_RETURN")
            self.assertEqual(plugin._focus, "files")
            frame("KEY_RETURN")                                   # preview wardriving.csv (first file)
            self.assertEqual(plugin._focus, "preview")
            self.assertIn("2026-11-20_10-00-00/wardriving.csv", canvas.texts)
            frame("KEY_PAGEDOWN")
            self.assertGreater(plugin._scroll, 0)
            frame("KEY_ESCAPE")
            frame("KEY_ESCAPE")
            frame("KEY_ESCAPE")
            self.assertFalse(plugin.overlay_active)
        plugin._load_preview("2026-11-20_10-00-00", "../../loot_db.json")
        self.assertEqual(plugin._preview, ["not inside the session folder"])
        after = {str(p): p.stat().st_mtime_ns for p in self.loot.rglob("*") if p.is_file()}
        self.assertEqual(after, before)
        source = (ROOT / "plugins" / "ng_session_log.py").read_text(encoding="utf-8")
        for forbidden in ("unlink", "rmtree", "os.remove", "write_text", "write_bytes", '"w"', "'w'", "rename"):
            self.assertNotIn(forbidden, source)


class SafetyCopyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "loot").mkdir()
        self.db = self.root / "loot" / "loot_db.json"
        self.write(1)

    def write(self, xp):
        self.db.write_text(json.dumps({"version": 2, "sessions": {}, "xp": xp}))

    def test_copies_roll_and_identical_databases_are_not_copied_again(self):
        made = []
        for n in range(5):
            self.write(n)
            made.append(engine.auto_copy_db(str(self.root), now=1770000000 + n * 600))
            self.assertIsNone(engine.auto_copy_db(str(self.root), now=1770000100 + n * 600))   # unchanged
        self.assertTrue(all(made))
        copies = engine.auto_copies(str(self.root))
        self.assertEqual(copies, made[-engine.AUTO_KEEP:])
        self.assertEqual(json.loads(Path(copies[-1]).read_text())["xp"], 4)
        self.assertEqual([p.name for p in (self.root / "loot" / engine.AUTO_DIR).iterdir()
                          if p.name.endswith(".part")], [])

    def test_a_damaged_database_never_replaces_good_copies(self):
        good = engine.auto_copy_db(str(self.root), now=1770000000)
        for bad in ("{broken", "[]", '{"version": "2", "sessions": {}}', ""):
            self.db.write_text(bad)
            self.assertIsNone(engine.auto_copy_db(str(self.root), now=1770009999))
        self.assertEqual(engine.auto_copies(str(self.root)), [good])
        hint = engine.damaged_db_hint(str(self.root))
        self.assertIn("A good copy from " + engine.stamp(1770000000), hint)
        self.assertIn(os.path.basename(good), hint)
        self.assertIn(".damaged", hint)
        self.assertEqual(self.db.read_text(), "")                # nothing was changed by asking
        self.write(9)
        self.assertIsNone(engine.damaged_db_hint(str(self.root)))
        self.db.unlink()
        self.assertIsNone(engine.damaged_db_hint(str(self.root)))
        self.assertIsNone(engine.auto_copy_db(str(self.root)))

    def test_launcher_shows_the_hint_only_for_a_damaged_database(self):
        from watchdogs import __main__ as launcher
        engine.auto_copy_db(str(self.root), now=1770000000)
        self.db.write_text("{broken")
        LootDataError = type("LootDataError", (Exception,), {})
        with patch("builtins.print") as printed:
            self.assertIsNone(launcher._loot_recovery_hint(ValueError("x"), str(self.root)))
            printed.assert_not_called()
            hint = launcher._loot_recovery_hint(LootDataError("damaged"), str(self.root))
        self.assertIn("cp '", hint)
        self.assertTrue(printed.call_args.args[0].startswith("[DATA] loot_db.json cannot be read"))
        self.assertEqual(self.db.read_text(), "{broken")

    def test_the_backup_plugin_makes_the_copy_off_the_game_thread(self):
        plugin = ng_backup.NgLootBackup()
        plugin.on_load(SimpleNamespace(_app_dir=str(self.root), msg=Mock(), _term_add=Mock(), _plugins=[]))
        plugin._auto_next = 0.0
        started = time.perf_counter()
        plugin._auto_tick()
        self.assertLess(time.perf_counter() - started, 0.2)
        deadline = time.perf_counter() + 5
        while not plugin._auto_last and time.perf_counter() < deadline:
            time.sleep(0.01)
        self.assertTrue(plugin._auto_last.startswith("loot_db-"))
        self.assertGreater(plugin._auto_next, time.monotonic() + ng_backup.AUTO_INTERVAL - 5)
        self.assertEqual(len(engine.auto_copies(str(self.root))), 1)


class LoaderTests(unittest.TestCase):
    def test_every_new_plugin_loads_once_and_menu_keys_do_not_collide(self):
        plugins = plugin_loader.discover_plugins()
        kinds = [type(plugin).__name__ for plugin in plugins]
        for name in ("Ng433Hunter", "NgSessionLog", "NgWdgWars", "NgBasemap", "GpsHealth", "HardwareCheck"):
            self.assertEqual(kinds.count(name), 1, name)
        keys = {}
        for plugin in plugins:
            for item in plugin.menu_items():
                keys.setdefault(item.key, []).append(type(plugin).__name__)
        shared = {key: owners for key, owners in keys.items() if len(set(owners)) > 1}
        self.assertEqual(shared, {"j": ["JanosImport", "KismetWardriveBridge"]})   # as shipped by upstream + v2
        notice = (ROOT / "plugins" / "_ng" / "NOTICE").read_text(encoding="utf-8")
        for needle in ("plugins/ng_433_hunter.py", "plugins/ng_session_log.py", "auto_copy_db"):
            self.assertIn(needle, notice)


if __name__ == "__main__":
    unittest.main()
