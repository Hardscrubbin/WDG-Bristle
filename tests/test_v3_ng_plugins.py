"""[merge-v3] Phase 2, batch 1: WDGoNG 0.10.0 features as upstream plugins.

a) NG bug fixes: the ADS-B session count is ported (upstream had the bug); the
   WDGWars "not yet" replies are shown to be handled by upstream already.
b) ADS-B track log + receiver log + KML export (plugins/ng_adsb_tracks.py).
c) 433 MHz capture -> Flipper RAW .sub (plugins/ng_subghz_capture.py).

upstream's real SDRManager, LootManager, plugin loader and app.py methods are used;
dump1090 / rtl_433 are never started (the process launcher is replaced), no
radio, GPS, network or pyxel window is involved. pyxel is a recording canvas
that fails the test when anything is drawn outside 640x360 or 16 colours.
"""

import ast
import hashlib
import io
import json
import math
import os
import sys
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from watchdogs import plugin_loader, sdr_manager
from watchdogs.loot_manager import LootManager
from plugins.plugin_base import PluginBase
from plugins.wardrive_upload import WardriveUpload
from plugins._ng import adsb_tracks, subghz_export
import plugins.ng_adsb_tracks as ng_adsb
import plugins.ng_subghz_capture as ng_433

import test_v3_loot_kismet as lk
from test_scan_session_map import loot_at

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "watchdogs" / "app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
GAME = next(node for node in TREE.body
            if isinstance(node, ast.ClassDef) and node.name == "WatchDogsGame")
FRAME_LIMIT = 0.5     # a blocked loop here would sit for many seconds
W, H = 640, 360


def app_methods(*names, **env):
    body = [node for node in GAME.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in body} == set(names), names
    module = ast.Module(body=[ast.ClassDef(name="Harness", bases=[], keywords=[],
                                           decorator_list=[], body=body)], type_ignores=[])
    namespace = dict(__name__="watchdogs.v3_method_test", __package__="watchdogs",
                     C_WARNING=10, C_SUCCESS=11, C_ERROR=8, C_TEXT=7, **env)
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), "exec"), namespace)
    return namespace["Harness"]


class Canvas:
    """pyxel stand-in: records draw calls and enforces the screen contract
    (640x360, colours 0..15). Text is measured with the font the game really
    uses: app.py replaces pyxel.text with Spleen 5x8, one px wider and two
    taller than pyxel's built-in 4x6. Lines may leave the screen only while a
    clip rectangle is set, as pyxel then cuts them."""
    frame_count = 7
    FONT_W, FONT_H = 5, 8

    def __init__(self):
        self.pressed = set()
        self.texts, self.lines, self.clip_rect = [], 0, None
        self.placed = []

    def new_frame(self):
        self.texts, self.placed, self.lines = [], [], 0

    def __getattr__(self, name):
        if name.startswith("KEY_"):
            return name
        raise AttributeError(name)

    def btnp(self, key, *_repeat):
        return key in self.pressed

    @staticmethod
    def _colour(colour):
        assert isinstance(colour, int) and 0 <= colour <= 15, colour

    @staticmethod
    def _inside(x, y, w=1, h=1):
        assert x >= 0 and y >= 0 and x + w <= W and y + h <= H, (x, y, w, h)

    def rect(self, x, y, w, h, colour):
        self._colour(colour)
        self._inside(x, y, w, h)

    rectb = rect

    def text(self, x, y, text, colour):
        self._colour(colour)
        self._inside(x, y, self.FONT_W * len(text), self.FONT_H)
        self.texts.append(text)
        self.placed.append((x, y, self.FONT_W * len(text), text))

    def overlaps(self):
        """Pairs of texts drawn on the same line that run into each other."""
        found = []
        for i, (x1, y1, w1, t1) in enumerate(self.placed):
            for x2, y2, w2, t2 in self.placed[i + 1:]:
                if y1 == y2 and t1.strip() and t2.strip() and x1 < x2 + w2 and x2 < x1 + w1:
                    found.append((t1, t2))
        return found

    def line(self, x1, y1, x2, y2, colour):
        self._colour(colour)
        self.lines += 1
        if self.clip_rect is None:
            self._inside(x1, y1)
            self._inside(x2, y2)

    def pset(self, x, y, colour):
        self._colour(colour)

    def clip(self, *rect):
        if rect:
            self._inside(*rect)
        self.clip_rect = rect or None


def with_pyxel(canvas):
    return patch.dict(sys.modules, {"pyxel": canvas})


def sbs(icao, lat, lon, alt=30000, callsign=""):
    """dump1090 SBS lines: an identification message and a position message."""
    out = []
    if callsign:
        out.append(f"MSG,1,1,1,{icao},1,2026/11/21,12:00:00.000,2026/11/21,12:00:00.000,{callsign},,,,,,,,,,,0")
    out.append(f"MSG,3,1,1,{icao},1,2026/11/21,12:00:00.000,2026/11/21,12:00:00.000,,{alt},,,{lat},{lon},,,,,,0")
    return out


def fake_app(loot, **extra):
    return SimpleNamespace(loot=loot, msg=Mock(), _term_add=Mock(), _esc_consumed_frame=-1, **extra)


# ---------------------------------------------------------------------------
# plugins/_ng: NG sources, notice, discovery
# ---------------------------------------------------------------------------

class NgSourceTests(unittest.TestCase):
    NG = ROOT / "plugins" / "_ng"

    def test_ng_files_are_byte_for_byte_the_wdgong_0_10_0_sources(self):
        expected = {
            "adsb_tracks.py": "72a497587444217d89b2bf3024294ec830cc5c6e1f7f3a05628fc453cbeb918f",
            "subghz_export.py": "cfc0a6526acb3d8404bab03839e4b9a9799d55e42183d80274c8f9aa1e4e0aa5",
            "LICENSE-WDGoNG.txt": "072c5c8d537a1e497de00bc17349bc32efb6bfeffeccee3b3437b423809ed955",
        }
        for name, digest in expected.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((self.NG / name).read_bytes()).hexdigest(), digest)

    def test_notice_says_what_the_licence_requires(self):
        notice = (self.NG / "NOTICE").read_text(encoding="utf-8")
        for needed in ("LOCOSP", "WDGoNG", "0.10.0", "PolyForm Noncommercial License 1.0.0",
                       "Required Notice: Copyright (c) 2025-2026 LOCOSP",
                       "personal, noncommercial use", "adsb_tracks.py", "subghz_export.py",
                       "https://polyformproject.org/licenses/noncommercial/1.0.0"):
            self.assertIn(needed, notice)
        licence = (self.NG / "LICENSE-WDGoNG.txt").read_text(encoding="utf-8")
        self.assertIn("Required Notice: Copyright (c) 2025-2026 LOCOSP", licence)
        self.assertIn("# PolyForm Noncommercial License 1.0.0", licence)
        for plugin in ("ng_adsb_tracks.py", "ng_subghz_capture.py"):
            text = (ROOT / "plugins" / plugin).read_text(encoding="utf-8")
            self.assertIn("Required Notice: Copyright (c) 2025-2026 LOCOSP", text)
            self.assertIn("PolyForm Noncommercial License 1.0.0", text)

    def test_nothing_from_the_do_not_port_list_is_in_the_tree(self):
        names = {path.name for path in ROOT.rglob("*.py")}
        for banned in ("host_wifi.py", "cluster_brain.py", "cluster_enow.py", "cluster_link.py",
                       "radio_arbiter.py", "radio_detect.py", "wdgo_extras.py", "wdgo_extras_hub.py",
                       "wdgo_extras_sandbox.py"):
            self.assertNotIn(banned, names)

    def test_loader_finds_both_plugins_and_never_treats_ng_sources_as_plugins(self):
        with patch.object(plugin_loader.log, "warning") as warned:
            loaded = plugin_loader.discover_plugins()
        warned.assert_not_called()
        kinds = {type(plugin).__name__ for plugin in loaded}
        self.assertLessEqual({"NgAdsbTracks", "NgSubGhzCapture", "KismetWardriveBridge",
                              "WardriveUpload", "JanosImport"}, kinds)
        self.assertEqual(len(loaded), len(kinds))               # each exactly once
        for plugin in loaded:
            self.assertIsInstance(plugin, PluginBase)
            self.assertFalse(type(plugin).__module__.startswith("plugins._"))

    def test_each_plugin_can_be_switched_off_with_disabled_plugins(self):
        with patch("watchdogs.config.DISABLED_PLUGINS", ["ng_adsb_tracks", "ng_subghz_capture"]):
            kinds = {type(plugin).__name__ for plugin in plugin_loader.discover_plugins()}
        self.assertFalse({"NgAdsbTracks", "NgSubGhzCapture"} & kinds)
        self.assertIn("KismetWardriveBridge", kinds)

    def test_features_are_plugins_not_app_edits(self):
        app = SOURCE.read_text(encoding="utf-8")
        for name in ("ng_adsb_tracks", "ng_subghz_capture", "adsb_tracks", "subghz_export",
                     "adsb_receiver", "captures_433"):
            self.assertNotIn(name, app)
        manager = (ROOT / "watchdogs" / "sdr_manager.py").read_text(encoding="utf-8")
        self.assertNotIn('"-S"', manager)                        # upstream's launcher is untouched

    def test_menu_entries_are_unique_within_the_plugins_menu(self):
        items = ng_adsb.NgAdsbTracks().menu_items() + ng_433.NgSubGhzCapture().menu_items()
        self.assertEqual([item.action for item in items],
                         ["open_overlay", "export_kml", "toggle_capture", "open_overlay"])
        keys = [item.key for item in items]
        self.assertEqual(len(set(keys)), len(keys))
        taken = {"k", "j", "u", "t", "w", "l", "b"}              # Kismet, JanOS, Wars Sync, backup
        self.assertFalse(taken & set(keys))
        for plugin in (ng_adsb.NgAdsbTracks, ng_433.NgSubGhzCapture):
            self.assertIs(plugin.BACKGROUND_TICK, True)


# ---------------------------------------------------------------------------
# a) NG bug fixes
# ---------------------------------------------------------------------------

class AdsbLog(lk.LootCase):
    """A session whose ADS-B log is written by upstream's real SBS parser."""

    def receive(self, loot, flights, updates=40):
        manager = sdr_manager.SDRManager()
        path = Path(loot.session_dir) / "adsb_aircraft.csv"
        manager._adsb_log = open(path, "a")
        self.addCleanup(lambda: manager._adsb_log and manager._adsb_log.close())
        if manager._adsb_log.tell() == 0:                        # upstream's own header (start_adsb)
            manager._adsb_log.write("timestamp,icao,callsign,lat,lon,alt_ft,speed_kt,heading,squawk\n")
        for step in range(updates):
            for icao, (lat, lon, alt, callsign) in flights.items():
                for line in sbs(icao, lat + step * 0.01, lon + step * 0.02, alt, callsign):
                    manager._parse_sbs_line(line)
        manager._adsb_log.flush()
        return path, manager

    FLIGHTS = {"4CA7B5": (50.0, 19.0, 36000, "RYR81XX"),
               "48AE21": (50.4, 19.6, 9000, "LOT3924"),
               "3C6444": (49.8, 18.7, 24000, "")}


class AdsbCountTests(AdsbLog):
    def test_upstreams_receive_path_writes_a_track_log_not_one_row_per_aircraft(self):
        loot = self.loot()
        path, manager = self.receive(loot, self.FLIGHTS)
        rows = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(manager.total_aircraft_seen, 3)
        self.assertGreater(len(rows) - 1, 100)                   # what stock upstream counted

    def test_session_count_is_distinct_aircraft(self):
        loot = self.loot()
        path, _ = self.receive(loot, self.FLIGHTS)
        self.assertEqual(loot._scan_session_dir(Path(loot.session_dir))["adsb"], 3)
        loot.update_session_loot()
        self.assertEqual(loot.loot_totals["adsb"], 3)

    def test_count_ignores_case_headers_and_blank_or_broken_lines(self):
        loot = self.loot()
        path = Path(loot.session_dir) / "adsb_aircraft.csv"
        path.write_text(
            "timestamp,icao,callsign,lat,lon,alt_ft,speed_kt,heading\n"        # LootManager's header
            "1,4ca7b5,A,50,19,1,1,1\n"
            "2,4CA7B5,A,50.1,19,1,1,1\n"
            "\n"
            "garbage-without-commas\n"
            "3,,missing-icao,50,19,1,1,1\n"
            "timestamp,icao,callsign,lat,lon,alt_ft,speed_kt,heading,squawk\n"  # SDRManager's header
            "4, 48ae21 ,B,50,19,1,1,1,7000\n", encoding="utf-8")
        self.assertEqual(loot._adsb_aircraft_count(path), 2)
        path.write_text("timestamp,icao,callsign,lat,lon,alt_ft,speed_kt,heading,squawk\n", encoding="utf-8")
        self.assertEqual(loot._adsb_aircraft_count(path), 0)

    def test_count_is_cached_until_the_log_changes(self):
        loot = self.loot()
        path, manager = self.receive(loot, self.FLIGHTS)
        self.assertEqual(loot._adsb_aircraft_count(path), 3)
        with patch("builtins.open", side_effect=AssertionError("the log was read again")):
            self.assertEqual(loot._adsb_aircraft_count(path), 3)
        for line in sbs("AAAAAA", 51.0, 20.0):
            manager._parse_sbs_line(line)
        manager._adsb_log.flush()
        self.assertEqual(loot._adsb_aircraft_count(path), 4)

    def old_database(self):
        """A loot_db.json written before the fix: ADS-B stored as log rows."""
        with loot_at(str(self.root), "2020-01-01T12:00:00", lk.gps()) as old:
            path, _ = self.receive(old, self.FLIGHTS)
            rows = len(path.read_text(encoding="utf-8").splitlines()) - 1
            old.save_xp(4321)
            name = Path(old.session_dir).name
        db_path = self.root / "loot" / "loot_db.json"
        db = json.loads(db_path.read_text(encoding="utf-8"))
        db.pop("adsb_unique", None)
        db["sessions"][name]["adsb"] = rows
        db["sessions"]["2019-05-05_10-00-00"] = {"adsb": 77, "wardriving": 5}   # log long gone
        db["totals"]["adsb"] = rows + 77
        db_path.write_text(json.dumps(db), encoding="utf-8")
        return name, rows, db_path

    def test_existing_database_is_converted_once_and_only_the_adsb_field(self):
        name, rows, db_path = self.old_database()
        before = json.loads(db_path.read_text(encoding="utf-8"))
        loot = self.loot("2022-01-01T12:00:00")
        self.assertEqual(loot._db["sessions"][name]["adsb"], 3)                  # was `rows`
        self.assertEqual(loot._db["sessions"]["2019-05-05_10-00-00"],
                         {"adsb": 77, "wardriving": 5})                          # no log: kept
        self.assertIs(loot._db["adsb_unique"], True)
        self.assertEqual(loot.load_xp(), 4321)
        for key, value in before["sessions"][name].items():
            if key != "adsb":
                self.assertEqual(loot._db["sessions"][name][key], value, key)
        loot.update_session_loot()
        self.assertEqual(loot.loot_totals["adsb"], 80)
        saved = json.loads(db_path.read_text(encoding="utf-8"))
        self.assertIs(saved["adsb_unique"], True)
        self.assertEqual(saved["sessions"][name]["adsb"], 3)
        self.assertGreater(rows, 100)

    def test_converted_database_is_not_rescanned_on_the_next_start(self):
        name, _, _ = self.old_database()
        with loot_at(str(self.root), "2022-01-01T12:00:00", lk.gps()) as first:
            first.update_session_loot()
        with patch.object(LootManager, "_adsb_aircraft_count",
                          side_effect=AssertionError("old logs were read again")):
            again = self.loot("2023-01-01T12:00:00")
        self.assertEqual(again._db["sessions"][name]["adsb"], 3)

    def test_a_fresh_database_is_marked_from_the_start(self):
        loot = self.loot()
        self.assertIs(loot._db["adsb_unique"], True)

    def test_recount_problems_never_stop_the_database_loading(self):
        name, rows, _ = self.old_database()
        with patch.object(LootManager, "_adsb_aircraft_count", side_effect=OSError("unreadable")):
            loot = self.loot("2022-01-01T12:00:00")
        self.assertEqual(loot._db["sessions"][name]["adsb"], rows)               # left as it was
        self.assertEqual(loot.load_xp(), 4321)
        self.assertNotIn("adsb_unique", loot._db)                                # so it is retried
        with loot_at(str(self.root), "2023-01-01T12:00:00", lk.gps()) as later:
            self.assertEqual(later._db["sessions"][name]["adsb"], 3)

    def test_a_database_without_adsb_data_is_loaded_exactly_as_it_is(self):
        # upstream's own guarantee (test_loot_legacy_migration): loading never alters a save
        with loot_at(str(self.root), "2020-01-01T12:00:00", lk.gps()) as old:
            old.save_wardriving_network(lk.net(lk.W1), observed_at=lk.T0)
            old.save_xp(777)
        db_path = self.root / "loot" / "loot_db.json"
        db = json.loads(db_path.read_text(encoding="utf-8"))
        db.pop("adsb_unique", None)
        db_path.write_text(json.dumps(db), encoding="utf-8")
        original = db_path.read_bytes()
        probe = LootManager.__new__(LootManager)
        probe._base, probe._db_path = self.root / "loot", db_path
        loaded = probe._load_or_build_db()
        self.assertEqual(loaded, db)                                             # no marker added
        self.assertEqual(db_path.read_bytes(), original)                         # nothing written


class UpstreamAlreadyHandlesNotYetRepliesTests(unittest.TestCase):
    """NG fix "WDGWars upload: empty / HTML / login-redirect replies mean
    'not yet'". upstream 0.9.21.3's uploader needs no port: such a reply is never a
    receipt, so the batch stays pending and is offered again on the next sync.
    This pins that behaviour; plugins/wardrive_upload.py is not changed by it."""

    REPLIES = {
        "empty": b"",
        "whitespace": b"  \n",
        "html error page": b"<!DOCTYPE html><html><body>502 Bad Gateway</body></html>",
        "login redirect page": b"<html><head><title>Sign in</title></head><form action='/login'></form></html>",
        "json but not an object": b"[]",
        "queued job, no receipt": b'{"ok": true, "job_id": 17}',
    }

    def setUp(self):
        with patch.object(WardriveUpload, "_load_state"):
            self.plugin = WardriveUpload()
        self.plugin._api_url = "https://portal.example.invalid/api/upload/"
        self.plugin._api_key = "a" * 64
        self.plugin.app = SimpleNamespace(xp=1)
        self.said = []
        self.plugin._sync_log = lambda text, colour=13: self.said.append(text)
        self.records = {"networks": [{"ssid": "synthetic"}], "aircraft": [], "meshcore_nodes": []}

    def post(self, body):
        with patch.object(self.plugin, "_wait_upload_slot", return_value=True), \
                patch("plugins.wardrive_upload._open", return_value=io.BytesIO(body)) as opened:
            result = self.plugin._post_upload_batch(
                self.records, (self.plugin._api_url, self.plugin._api_key), lambda: True)
        self.assertEqual(opened.call_count, 1)                   # no hammering either
        return result

    def test_non_json_replies_leave_the_batch_pending_without_raising(self):
        for name in ("empty", "whitespace", "html error page", "login redirect page",
                     "json but not an object"):
            with self.subTest(reply=name):
                self.said.clear()
                self.assertEqual(self.post(self.REPLIES[name]), (None, None))
                self.assertEqual(self.said, ["  Invalid server response; left pending"])

    def test_a_reply_without_a_receipt_is_never_counted_as_uploaded(self):
        import plugins.wardrive_upload as upload
        result, code = self.post(self.REPLIES["queued job, no receipt"])
        self.assertIsNone(code)
        confirmed, _info, imported = upload._upload_receipt(result, self.records)
        self.assertEqual((confirmed, imported), (False, 0))

    def test_upstreams_uploader_is_the_stock_file_plus_the_one_phase_1_line(self):
        text = (ROOT / "plugins" / "wardrive_upload.py").read_text(encoding="utf-8")
        self.assertEqual(text.count("[merge-v3]"), 1)


# ---------------------------------------------------------------------------
# b) ADS-B tracks + KML
# ---------------------------------------------------------------------------

def km(lat1, lon1, lat2, lon2):
    return adsb_tracks.km_between(lat1, lon1, lat2, lon2)


class AdsbPluginCase(AdsbLog):
    RECEIVER = (50.06, 19.94)

    def setUp(self):
        super().setUp()
        self.loot_manager = self.loot()
        self.session = Path(self.loot_manager.session_dir)
        self.sample = dict(lat=self.RECEIVER[0], lon=self.RECEIVER[1])
        self.sdr = SimpleNamespace(running=True, mode="adsb")
        self.plugin = ng_adsb.NgAdsbTracks()
        self.plugin.on_load(fake_app(
            self.loot_manager, _sdr=self.sdr, gps_fix=True,
            gps=SimpleNamespace(wardrive_sample=lambda: self.sample)))

    def hud(self):
        return [call.args[0] for call in self.plugin.app.msg.call_args_list]

    def terminal(self):
        return [call.args[0] for call in self.plugin.app._term_add.call_args_list]

    def finish(self):
        self.plugin._thread.join(20.0)
        self.assertFalse(self.plugin._thread.is_alive())
        self.plugin._rx_next = float("inf")                      # no receiver row from this tick
        self.plugin.on_update()


class AdsbReceiverLogTests(AdsbPluginCase):
    def rows(self):
        path = self.session / "adsb_receiver.csv"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def test_receiver_position_is_logged_every_30_s_while_the_radar_runs(self):
        self.assertTrue(self.plugin._log_receiver(1000.0))
        self.assertFalse(self.plugin._log_receiver(1010.0))
        self.assertFalse(self.plugin._log_receiver(1029.9))
        self.sample = dict(lat=50.07, lon=19.95)
        self.assertTrue(self.plugin._log_receiver(1030.0))
        self.assertEqual(self.rows(), ["timestamp,lat,lon", "1000,50.060000,19.940000",
                                       "1030,50.070000,19.950000"])
        self.assertEqual(adsb_tracks._read_receiver(str(self.session)),
                         [(1000, 50.06, 19.94), (1030, 50.07, 19.95)])

    def test_nothing_is_logged_without_the_radar_a_fresh_fix_or_a_session(self):
        self.sdr.mode = "433"
        self.assertFalse(self.plugin._log_receiver(1000.0))
        self.sdr.mode, self.sdr.running = "adsb", False
        self.assertFalse(self.plugin._log_receiver(1100.0))
        self.sdr.running = True
        self.sample = None                                        # fix older than 2 s / no fix
        self.assertFalse(self.plugin._log_receiver(1200.0))
        self.sample = dict(lat=1.0, lon=2.0)
        self.plugin.app.loot = None
        self.assertFalse(self.plugin._log_receiver(1300.0))
        self.assertEqual(self.rows(), [])

    def test_it_is_ticked_with_the_screen_closed(self):
        tick = app_methods("_tick_background_plugins")()
        tick._plugins, tick._term_add = [self.plugin], Mock()
        with with_pyxel(Canvas()):
            tick._tick_background_plugins()
        self.assertEqual(len(self.rows()), 2)                     # header + this frame's position
        tick._term_add.assert_not_called()

    def test_adsb_plus_433_mode_counts_as_radar_running(self):
        self.sdr.mode = "adsb+433"
        self.assertTrue(self.plugin._log_receiver(1000.0))

    def test_unwritable_log_is_reported_once_and_never_raises(self):
        with patch("builtins.open", side_effect=PermissionError("read-only loot")):
            for stamp in (1000.0, 1030.0, 1060.0):
                self.assertFalse(self.plugin._log_receiver(stamp))
        self.assertEqual(len(self.terminal()), 1)


class AdsbKmlExportTests(AdsbPluginCase):
    def test_export_writes_the_sessions_tracks_as_kml_into_the_session_folder(self):
        self.receive(self.loot_manager, self.FLIGHTS)
        self.plugin._log_receiver(time.time())
        self.plugin.export_kml()                                  # the PLUGINS menu entry
        self.finish()
        kml = self.session / "adsb_tracks.kml"
        self.assertTrue(kml.is_file())
        self.assertEqual([p.name for p in self.session.glob("*.tmp")], [])
        root = ET.fromstring(kml.read_text(encoding="utf-8"))
        ns = {"k": "http://www.opengis.net/kml/2.2"}
        names = [node.text for node in root.findall(".//k:Placemark/k:name", ns)]
        self.assertEqual(names, ["3C6444", "LOT3924", "RYR81XX", "Receiver",
                                 "Coverage (max range per 10 degrees)"])
        lines = root.findall(".//k:Placemark/k:LineString/k:coordinates", ns)
        first = lines[0].text.split()                             # 3C6444: lon,lat,metres
        self.assertEqual(len(first), 40)
        lon, lat, metres = map(float, first[0].split(","))
        self.assertEqual((round(lat, 2), round(lon, 2), round(metres)), (49.8, 18.7, round(24000 * 0.3048)))
        receiver = root.find(".//k:Placemark[k:name='Receiver']/k:Point/k:coordinates", ns).text
        self.assertEqual(receiver, "19.940000,50.060000,0")
        self.assertIn("GPS during the session", kml.read_text(encoding="utf-8"))
        self.assertIn("[ADS-B] KML saved: %s/adsb_tracks.kml" % self.session.name, self.hud())
        self.assertTrue(any("3 aircraft" in line for line in self.terminal()))

    def test_range_and_coverage_are_measured_from_the_logged_receiver(self):
        self.receive(self.loot_manager, self.FLIGHTS)
        self.plugin._log_receiver(time.time())
        self.plugin.reload()
        self.finish()
        data = self.plugin._data
        self.assertEqual((data["n_aircraft"], data["rx_source"]), (3, "gps-log"))
        self.assertEqual(data["n_points"], 120)
        farthest = max(km(*self.RECEIVER, lat + step * 0.01, lon + step * 0.02)
                       for lat, lon, _alt, _cs in self.FLIGHTS.values() for step in range(40))
        self.assertAlmostEqual(data["max_km"], farthest, places=2)
        self.assertGreater(data["max_km"], 80.0)
        self.assertEqual(sum(1 for d in data["coverage"] if d > 0) > 0, True)

    def test_repeated_position_messages_do_not_become_track_points(self):
        # upstream logs a row for every message of a positioned aircraft
        _, manager = self.receive(self.loot_manager, {"4CA7B5": (50.0, 19.0, 36000, "RYR81XX")}, updates=1)
        for _ in range(50):
            manager._parse_sbs_line(sbs("4CA7B5", 50.0, 19.0, 36000, "RYR81XX")[0])   # ident only
        manager._adsb_log.flush()
        self.plugin.reload()
        self.finish()
        self.assertEqual((self.plugin._data["n_aircraft"], self.plugin._data["n_points"]), (1, 1))

    def test_without_a_log_nothing_is_written_and_the_player_is_told(self):
        self.plugin.export_kml()
        self.finish()
        self.assertFalse((self.session / "adsb_tracks.kml").exists())
        self.assertIn("[ADS-B] No ADS-B log in this session yet", self.hud())

    def test_without_a_session_nothing_starts(self):
        self.plugin.app.loot = None
        self.plugin.export_kml()
        self.assertIsNone(self.plugin._thread)
        self.assertEqual(self.hud(), ["[ADS-B] No loot session is open"])

    def test_reading_a_big_log_never_blocks_the_game_loop(self):
        self.receive(self.loot_manager, self.FLIGHTS)
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        real = adsb_tracks.load

        def slow_load(*args, **kwargs):
            entered.set()
            release.wait(60.0)
            return real(*args, **kwargs)

        with patch.object(adsb_tracks, "load", slow_load), with_pyxel(Canvas()):
            started = time.perf_counter()
            self.plugin.export_kml()
            self.assertLess(time.perf_counter() - started, FRAME_LIMIT)
            self.assertTrue(entered.wait(10.0))
            slowest = 0.0
            for _ in range(200):
                began = time.perf_counter()
                self.plugin.on_update()
                slowest = max(slowest, time.perf_counter() - began)
            self.assertLess(slowest, FRAME_LIMIT)
            self.assertTrue(self.plugin._thread.is_alive() and self.plugin._thread.daemon)
            self.plugin.export_kml()                              # second request: refused
            self.assertIn("Still reading", self.hud()[-1])
            release.set()
            self.finish()
        self.assertTrue((self.session / "adsb_tracks.kml").is_file())

    def test_a_failing_export_is_reported_and_frees_the_worker(self):
        self.receive(self.loot_manager, self.FLIGHTS)
        with patch.object(adsb_tracks, "write_kml", side_effect=OSError("disk full")):
            self.plugin.export_kml()
            self.finish()
        self.assertTrue(any("disk full" in line for line in self.terminal()))
        self.assertFalse(self.plugin._busy)
        self.plugin.export_kml()
        self.finish()
        self.assertTrue((self.session / "adsb_tracks.kml").is_file())


class AdsbScreenTests(AdsbPluginCase):
    def test_build_view_fits_the_map_panel_and_keeps_track_ends(self):
        self.receive(self.loot_manager, self.FLIGHTS)
        self.plugin._log_receiver(time.time())
        data = adsb_tracks.load(str(self.session))
        view = ng_adsb.build_view(data)
        px, py, pw, ph = ng_adsb.PLOT
        points = [xy for _colour, line in view["lines"] for xy in line] + view["ring"] + [view["receiver"]]
        for x, y in points:
            self.assertTrue(px <= x <= px + pw and py <= y <= py + ph, (x, y))
        self.assertEqual([colour for colour, _ in view["lines"]], [11, 9, 12])   # by ICAO: 24k, 9k, 36k ft
        self.assertEqual([row[0] for row in view["rows"]], ["3C6444", "LOT3924", "RYR81XX"])
        self.assertEqual(view["rows"][0][2:5], (40, 24000, 24000))
        self.assertEqual(len(view["ring"]), adsb_tracks.SECTORS + 1)

    def test_long_sessions_are_thinned_for_drawing(self):
        pts = [(i, 50.0 + i * 1e-4, 19.0 + i * 1e-4, 30000, 400, 90) for i in range(50000)]
        data = dict(tracks={"AAAAAA": dict(callsign="", pts=pts)}, receiver=None, coverage=[0.0] * 36,
                    max_km=0.0, bounds=(50.0, 19.0, 55.0, 24.0), rx_source="")
        view = ng_adsb.build_view(data, max_points=1000)
        line = view["lines"][0][1]
        self.assertLessEqual(len(line), 1002)
        self.assertGreater(len(line), 100)
        self.assertEqual(view["receiver"], None)

    def test_altitude_scale(self):
        self.assertEqual([ng_adsb.altitude_colour(a) for a in (0, 9999, 10000, 19999, 29999, 39999, 40000)],
                         [9, 9, 10, 10, 11, 12, 14])

    def test_screen_draws_inside_640x360_with_16_colours(self):
        self.receive(self.loot_manager, self.FLIGHTS)
        self.plugin._log_receiver(time.time())
        canvas = Canvas()
        with with_pyxel(canvas):
            self.plugin.draw(0, 0, W, H)                          # closed: draws nothing
            self.assertEqual(canvas.texts, [])
            self.plugin.open_overlay()                            # reads in the background
            self.plugin.draw(0, 0, W, H)                          # "Reading..." state
            self.finish()
            canvas.new_frame()
            self.plugin.draw(0, 0, W, H)
        self.assertEqual(canvas.overlaps(), [])                   # no text runs into another
        said = " | ".join(canvas.texts)
        self.assertIn("ADS-B TRACKS - " + self.session.name, said)
        self.assertIn("RADAR ON", said)
        self.assertIn("3 aircraft, 120 positions read.", said)
        self.assertIn("[R] Reload  [E] Export KML  [UP/DOWN] Scroll  [ESC] Back", said)
        self.assertTrue(any(text.startswith("RYR81XX   4CA7B5") for text in canvas.texts))
        self.assertGreater(canvas.lines, 100)
        self.assertIsNone(canvas.clip_rect)                       # clip released again

    def test_empty_session_screen_and_a_very_long_list_still_fit(self):
        canvas = Canvas()
        with with_pyxel(canvas):
            self.plugin.open_overlay()
            self.finish()
            self.plugin.draw(0, 0, W, H)
        self.assertTrue(any("No ADS-B log in this session yet" in text for text in canvas.texts))
        flights = {"%06X" % (0x400000 + n): (50.0 + n * 0.01, 19.0, 1000 * n, "") for n in range(60)}
        self.receive(self.loot_manager, flights, updates=2)
        canvas = Canvas()
        with with_pyxel(canvas):
            self.plugin.reload()
            self.finish()
            canvas.pressed = {"KEY_DOWN"}
            for _ in range(100):
                self.plugin.on_update()
            self.plugin.draw(0, 0, W, H)
        self.assertEqual(canvas.overlaps(), [])
        self.assertEqual(self.plugin._scroll, 60 - ng_adsb.LIST_ROWS)
        self.assertTrue(any(text.startswith("37-60 of 60") for text in canvas.texts))

    def test_keys(self):
        canvas = Canvas()
        self.plugin._rx_next = float("inf")
        with with_pyxel(canvas):
            self.plugin._overlay_active = True
            with patch.object(self.plugin, "reload") as reload, \
                    patch.object(self.plugin, "export_kml") as export:
                canvas.pressed = {"KEY_R"}
                self.plugin.on_update()
                canvas.pressed = {"KEY_E"}
                self.plugin.on_update()
                reload.assert_called_once_with()
                export.assert_called_once_with()
            canvas.pressed = {"KEY_ESCAPE"}
            self.plugin.on_update()
        self.assertFalse(self.plugin.overlay_active)
        self.assertEqual(self.plugin.app._esc_consumed_frame, Canvas.frame_count)


# ---------------------------------------------------------------------------
# c) 433 MHz capture -> Flipper RAW .sub
# ---------------------------------------------------------------------------

def write_cu8(path, pulses_us, sample_rate=250_000, lead=400):
    """A synthetic OOK recording: + = carrier on, - = gap (microseconds)."""
    off, on = bytes((127, 128)), bytes((255, 128))
    data = bytearray(off * lead)
    for value in pulses_us:
        samples = int(round(abs(value) * sample_rate / 1_000_000))
        data += (on if value > 0 else off) * samples
    data += off * lead
    Path(path).write_bytes(bytes(data))


SIGNAL = [500, -500, 1000, -500, 500, -1500, 500]


class FakeStream:
    def readline(self):
        return b""

    def close(self):
        pass


class FakeProcess:
    """An rtl_433 that has already exited: nothing is ever signalled or killed."""
    launched = []

    def __init__(self, command, **kwargs):
        self.command, self.kwargs, self.pid = list(command), kwargs, 0
        self.stdout = FakeStream() if kwargs.get("stdout") is not None else None
        self.stderr = None
        FakeProcess.launched.append(self)

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


class NgSubGhzExportTests(lk.LootCase):
    def test_rtl_433_recording_becomes_a_flipper_raw_sub(self):
        recording = self.root / "g001_433.92M_250k.cu8"
        write_cu8(recording, SIGNAL)
        out, pulses = subghz_export.convert_cu8(str(recording))
        self.assertEqual((Path(out).name, pulses), ("g001_433.92M_250k.sub", len(SIGNAL)))
        lines = Path(out).read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[:5], ["Filetype: Flipper SubGhz RAW File", "Version: 1",
                                     "Frequency: 433920000", "Preset: FuriHalSubGhzPresetOok650Async",
                                     "Protocol: RAW"])
        self.assertTrue(lines[5].startswith("RAW_Data: "))
        values = [int(v) for v in lines[5].split(": ", 1)[1].split()]
        self.assertEqual([v > 0 for v in values], [v > 0 for v in SIGNAL])
        for got, want in zip(values, SIGNAL):
            self.assertLessEqual(abs(got - want), 8)             # two samples at 250 kHz

    def test_frequency_and_rate_come_from_the_rtl_433_file_name(self):
        self.assertEqual(subghz_export.parse_rtl433_filename("g017_868.35M_1000k.cu8"),
                         (868_350_000, 1_000_000))
        self.assertEqual(subghz_export.parse_rtl433_filename("notes.txt"), (None, None))

    def test_silence_produces_no_sub(self):
        recording = self.root / "g002_433.92M_250k.cu8"
        recording.write_bytes(bytes((127, 128)) * 5000)
        self.assertEqual(subghz_export.convert_cu8(str(recording)), (None, 0))
        self.assertFalse(recording.with_suffix(".sub").exists())


class CaptureCase(lk.LootCase):
    def setUp(self):
        super().setUp()
        FakeProcess.launched = []
        self.loot_manager = self.loot()
        self.session = Path(self.loot_manager.session_dir)
        self.sdr = sdr_manager.SDRManager()                      # upstream's real manager
        for target, name, value in (
                (sdr_manager.subprocess, "Popen", FakeProcess),
                (sdr_manager.SDRManager, "has_rtl433", staticmethod(lambda: True)),
                (ng_433, "WATCH_INTERVAL", 0.02),
                # [merge-v3] no waiting in these tests: "unchanged on two looks"
                # (the quiet time has its own tests below)
                (ng_433, "QUIET_SECONDS", 0.0), (ng_433, "SETTLE_INTERVAL", 0.02)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        game = app_methods("_sdr_433")()                         # upstream's real menu action
        game._sdr, game.loot = self.sdr, self.loot_manager
        game._sdr_enabled, game._aio_available = True, False
        game.player_lat, game.player_lon = 50.0, 19.0
        game.msg, game._term_add, game._earn_badge = Mock(), Mock(), Mock()
        game._esc_consumed_frame = -1
        self.game = game
        self.plugin = ng_433.NgSubGhzCapture()
        self.plugin.on_load(game)
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.plugin.on_unload()
        thread = self.plugin._watch_thread
        if thread is not None:
            thread.join(10.0)
        self.sdr.stop()

    def hud(self):
        return [call.args[0] for call in self.game.msg.call_args_list]

    def terminal(self):
        return [call.args[0] for call in self.game._term_add.call_args_list]

    def rtl433(self):
        return [p for p in FakeProcess.launched if p.command[0] == "rtl_433"]

    def wait_for(self, predicate, what):
        deadline = time.perf_counter() + 10.0
        while time.perf_counter() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("timed out waiting for " + what)

    def record(self, name, pulses=SIGNAL):
        path = Path(self.plugin._capture_dir) / name
        write_cu8(path, pulses)
        return path


class CaptureHookTests(CaptureCase):
    def test_capture_runs_upstreams_own_scanner_with_raw_recording_into_the_session(self):
        self.assertTrue(self.plugin.start_capture())
        process, = self.rtl433()
        self.assertEqual(process.command, sdr_manager.RTL433_CMD + ["-S", "all"])
        directory = Path(process.kwargs["cwd"])
        self.assertEqual(directory.parent, self.session / "captures_433")
        self.assertTrue(directory.is_dir())
        self.assertIs(self.sdr._rtl433_proc, process)            # upstream still owns the process
        self.assertTrue(self.sdr.running)
        self.assertEqual(self.sdr.mode, "433")
        self.assertTrue((self.session / "sdr_sensors.csv").is_file())   # upstream's decoding unchanged
        self.assertIn("[433] Scanner active", self.hud())        # upstream's own message
        self.assertTrue(any(line.startswith("[433] Capture ON -> loot/%s/captures_433/" % self.session.name)
                            for line in self.hud()))
        self.game._earn_badge.assert_called_once_with("iot_hunter")

    def test_each_finished_recording_gets_a_sub_next_to_it(self):
        self.plugin.start_capture()
        first = self.record("g001_433.92M_250k.cu8")
        self.wait_for(lambda: first.with_suffix(".sub").is_file(), "the first .sub")
        second = self.record("g002_433.92M_250k.cu8", [300, -300] * 20 + [300])
        silent = Path(self.plugin._capture_dir) / "g003_433.92M_250k.cu8"
        silent.write_bytes(bytes((127, 128)) * 4000)
        self.wait_for(lambda: len(self.plugin._rows) == 3, "all three recordings")
        self.assertTrue(first.is_file() and second.is_file() and silent.is_file())   # raw I/Q is kept
        self.assertEqual(self.plugin._rows, [
            ("g001_433.92M_250k.cu8", "g001_433.92M_250k.sub", len(SIGNAL), ""),
            ("g002_433.92M_250k.cu8", "g002_433.92M_250k.sub", 41, ""),
            ("g003_433.92M_250k.cu8", "", 0, "no usable signal (raw kept)")])
        self.assertEqual((self.plugin._converted, self.plugin._skipped), (2, 1))
        self.assertIn("Frequency: 433920000", second.with_suffix(".sub").read_text(encoding="utf-8"))
        with with_pyxel(Canvas()):
            self.plugin.on_update()
        self.assertIn("[433] captured g001_433.92M_250k.sub (7 pulses)", self.terminal())

    def test_a_recording_still_being_written_is_left_alone_until_it_is_complete(self):
        self.plugin.start_capture()
        self.plugin._watch_stop.set()                            # drive the sweeps by hand
        self.plugin._watch_thread.join(10.0)
        directory, sizes, done = self.plugin._capture_dir, {}, set()
        growing = Path(directory) / "g009_433.92M_250k.cu8"
        growing.write_bytes(bytes((127, 128)) * 100)
        self.plugin._sweep(directory, sizes, done, False)        # first sight
        write_cu8(growing, SIGNAL)                               # rtl_433 wrote more
        self.plugin._sweep(directory, sizes, done, False)        # size changed: wait
        self.assertEqual((done, growing.with_suffix(".sub").exists()), (set(), False))
        self.plugin._sweep(directory, sizes, done, False)        # stable: converted
        self.assertEqual(done, {growing.name})
        self.assertTrue(growing.with_suffix(".sub").is_file())

    def test_stop_stops_upstreams_scanner_and_converts_what_was_left(self):
        self.plugin.start_capture()
        with patch.object(ng_433, "WATCH_INTERVAL", 30.0):       # watcher asleep until stop
            time.sleep(0.1)
            late = self.record("g004_433.92M_250k.cu8")
            self.plugin.toggle_capture()                         # the menu entry again
            self.plugin._watch_thread.join(10.0)
        self.assertFalse(self.plugin.capture_active())
        self.assertFalse(self.sdr.running)                       # upstream's own stop ran
        self.assertEqual(self.sdr.mode, "")
        self.assertIn("[433] Stopped", self.hud())
        self.assertTrue(late.with_suffix(".sub").is_file())
        with with_pyxel(Canvas()):
            self.plugin.on_update()
        self.assertTrue(any(line.startswith("[433] Capture finished: 1 signal(s), 1 .sub written")
                            for line in self.terminal()))

    # ---- [merge-v3] a .sub only from a complete, non-empty recording, and
    # ---- never from another capture's folder (found on the uConsole) -------

    def names(self, directory=None):
        return sorted(p.name for p in Path(directory or self.plugin._capture_dir).iterdir())

    def test_a_capture_folder_is_never_adopted(self):
        parent = self.session / "captures_433"
        first = Path(ng_433.new_capture_dir(str(parent), "2026-11-21_12-00-00"))
        (first / "g001_433.92M_250k.cu8").write_bytes(b"\x80" * 64)
        second = Path(ng_433.new_capture_dir(str(parent), "2026-11-21_12-00-00"))
        third = Path(ng_433.new_capture_dir(str(parent), "2026-11-21_12-00-00"))
        self.assertEqual([p.name for p in (first, second, third)],
                         ["2026-11-21_12-00-00", "2026-11-21_12-00-00_2", "2026-11-21_12-00-00_3"])
        self.assertEqual((self.names(second), self.names(third)), ([], []))

    def test_two_captures_in_the_same_second_do_not_share_recordings(self):
        real = ng_433.new_capture_dir
        with patch.object(ng_433, "new_capture_dir", lambda parent, stamp=None: real(parent, "2026-11-21_12-00-00")):
            self.plugin.start_capture()
            first = Path(self.plugin._capture_dir)
            recording = self.record("g001_433.92M_250k.cu8")
            self.wait_for(lambda: recording.with_suffix(".sub").is_file(), "the first capture's .sub")
            self.plugin.stop_capture()
            self.plugin.start_capture()                          # same second, nothing recorded
            second = Path(self.plugin._capture_dir)
            self.plugin.stop_capture()
            self.plugin._watch_thread.join(10.0)
        self.assertNotEqual(first, second)
        self.assertEqual(self.names(second), [])                 # no .cu8, no .sub: it was empty
        self.assertEqual((self.plugin._rows, self.plugin._converted, self.plugin._skipped), ([], 0, 0))
        self.assertEqual(self.names(first), ["g001_433.92M_250k.cu8", "g001_433.92M_250k.sub"])
        with with_pyxel(Canvas()):
            self.plugin.on_update()
        self.assertTrue(any(line.startswith("[433] Capture finished: 0 signal(s), 0 .sub written")
                            for line in self.terminal()))

    def test_an_empty_recording_never_becomes_a_sub(self):
        real, handed_over = subghz_export.convert_cu8, []

        def watched(path, *args, **kwargs):
            handed_over.append(os.path.basename(path))
            return real(path, *args, **kwargs)

        with patch.object(subghz_export, "convert_cu8", watched):
            self.plugin.start_capture()
            empty = Path(self.plugin._capture_dir) / "g001_433.92M_250k.cu8"
            empty.write_bytes(b"")
            good = self.record("g002_433.92M_250k.cu8")
            self.wait_for(lambda: good.with_suffix(".sub").is_file(), "the good recording")
            time.sleep(0.15)                                     # several more looks at the empty one
            self.assertEqual([row[0] for row in self.plugin._rows], [good.name])   # still waiting for data
            self.plugin.stop_capture()
            self.plugin._watch_thread.join(10.0)
        self.assertEqual(handed_over, [good.name])               # the converter never saw the empty file
        self.assertEqual(self.plugin._rows[-1], (empty.name, "", 0, "empty recording (raw kept)"))
        self.assertEqual((self.plugin._converted, self.plugin._skipped), (1, 1))
        self.assertEqual(self.names(), [empty.name, good.name, good.with_suffix(".sub").name])

    def test_a_recording_that_changes_while_it_is_read_is_not_finished(self):
        self.plugin.start_capture()
        self.plugin._watch_stop.set()                            # drive the looks by hand
        self.plugin._watch_thread.join(10.0)
        directory, sizes, done = self.plugin._capture_dir, {}, set()
        recording = self.record("g001_433.92M_250k.cu8")
        real, reads = subghz_export.convert_cu8, []

        def writer_still_busy(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if not reads:                                        # rtl_433 wrote more meanwhile
                with open(path, "ab") as fh:
                    fh.write(bytes((127, 128)) * 500)
            reads.append(path)
            return result

        with patch.object(subghz_export, "convert_cu8", writer_still_busy):
            self.assertEqual(self.plugin._sweep(directory, sizes, done, False), [recording.name])   # first sight
            self.assertEqual(self.plugin._sweep(directory, sizes, done, False), [recording.name])   # read, changed
            self.assertEqual((done, self.names()), (set(), [recording.name]))                      # nothing left behind
            self.assertEqual(self.plugin._sweep(directory, sizes, done, False), [recording.name])   # seen anew
            self.assertEqual(self.plugin._sweep(directory, sizes, done, False), [])                 # complete
        self.assertEqual(done, {recording.name})
        self.assertEqual(self.names(), [recording.name, recording.with_suffix(".sub").name])
        self.assertEqual(self.plugin._rows, [(recording.name, "g001_433.92M_250k.sub", len(SIGNAL), "")])

    def test_stop_gives_no_sub_to_a_recording_that_is_still_being_written(self):
        real_state, ticks = ng_433._file_state, iter(range(1, 1_000_000))

        def growing(path):                                       # g009 is different on every look
            state = real_state(path)
            return (state[0] + next(ticks), state[1]) if state and "g009" in path else state

        with patch.object(ng_433, "FINAL_WAIT", 0.4), patch.object(ng_433, "SETTLE_INTERVAL", 0.02), \
                patch.object(ng_433, "_file_state", growing):
            self.plugin.start_capture()
            unfinished = self.record("g009_433.92M_250k.cu8")
            complete = self.record("g010_433.92M_250k.cu8")
            began = time.perf_counter()
            self.plugin.stop_capture()
            self.assertLess(time.perf_counter() - began, FRAME_LIMIT)    # Stop itself does not wait
            self.plugin._watch_thread.join(10.0)
        self.assertFalse(self.plugin._watch_thread.is_alive())
        self.assertEqual(self.names(), [unfinished.name, complete.name, complete.with_suffix(".sub").name])
        self.assertIn((unfinished.name, "", 0, "still being written at stop: no .sub (raw kept)"),
                      self.plugin._rows)
        self.assertEqual((self.plugin._converted, self.plugin._skipped), (1, 1))

    def by_hand(self):
        """Stop the watcher thread and return what a test needs to look itself."""
        self.plugin.start_capture()
        self.plugin._watch_stop.set()
        self.plugin._watch_thread.join(10.0)
        return self.plugin._capture_dir, {}, set()

    def test_a_recording_must_have_been_unchanged_for_the_quiet_time(self):
        directory, sizes, done = self.by_hand()
        recording = self.record("g001_433.92M_250k.cu8")
        clock = [1000.0]
        with patch.object(ng_433, "QUIET_SECONDS", 1.0), \
                patch.object(ng_433.time, "monotonic", lambda: clock[0]):
            look = lambda: self.plugin._sweep(directory, sizes, done, False)
            self.assertEqual(look(), [recording.name])           # first sight
            clock[0] += 0.5
            self.assertEqual(look(), [recording.name])           # unchanged, but only for 0.5 s
            clock[0] += 0.4
            self.assertEqual(look(), [recording.name])           # 0.9 s
            with open(recording, "ab") as fh:                    # ...and then more arrives
                fh.write(bytes((127, 128)) * 200)
            clock[0] += 0.2
            self.assertEqual(look(), [recording.name])           # changed: the quiet time starts again
            clock[0] += 0.9
            self.assertEqual(look(), [recording.name])
            self.assertEqual((done, self.names()), (set(), [recording.name]))
            clock[0] += 0.1
            self.assertEqual(look(), [])                         # quiet for a full second: converted
        self.assertEqual(self.names(), [recording.name, recording.with_suffix(".sub").name])

    def test_a_recording_rtl_433_still_has_open_is_not_finished_however_quiet(self):
        directory, sizes, done = self.by_hand()
        recording = self.record("g001_433.92M_250k.cu8")
        self.plugin._capture.proc = SimpleNamespace(pid=4321, poll=lambda: None)
        answers = [True, True, False]
        asked = []

        def still_open(pid, path):
            asked.append((pid, os.path.basename(path)))
            return answers.pop(0)

        with patch.object(ng_433, "_open_in_process", still_open):
            look = lambda: self.plugin._sweep(directory, sizes, done, False)
            self.assertEqual(look(), [recording.name])           # first sight (not asked yet)
            self.assertEqual(look(), [recording.name])           # quiet, but open in rtl_433
            self.assertEqual(look(), [recording.name])           # still open
            self.assertEqual(self.names(), [recording.name])
            self.assertEqual(look(), [])                         # closed: converted
        self.assertEqual(asked, [(4321, recording.name)] * 3)
        self.assertTrue(recording.with_suffix(".sub").is_file())

    def test_open_files_of_a_process_are_read_from_proc_where_there_is_one(self):
        for pid in (None, 0, -1, "12", 2 ** 22 + 12345):          # nothing to ask: "cannot tell"
            self.assertIsNone(ng_433._open_in_process(pid, "/tmp/x.cu8"))
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("no /proc here (the uConsole has one)")
        path = self.root / "open.cu8"
        with open(path, "wb") as handle:
            handle.write(b"\x80" * 8)
            handle.flush()
            self.assertIs(ng_433._open_in_process(os.getpid(), str(path)), True)
        self.assertIs(ng_433._open_in_process(os.getpid(), str(path)), False)

    def test_stop_waits_for_rtl_433_to_exit_before_the_last_look(self):
        self.plugin.start_capture()
        process, = self.rtl433()
        polls = []
        process.poll = lambda: polls.append(1) or (None if len(polls) < 4 else 0)
        self.plugin._capture.proc = process
        with patch.object(ng_433, "WATCH_INTERVAL", 30.0):       # watcher asleep until stop
            time.sleep(0.1)
            late = self.record("g004_433.92M_250k.cu8")
            self.plugin._watch_stop.set()
            self.plugin._watch_thread.join(10.0)
        self.assertGreaterEqual(len(polls), 4)                   # it waited for the exit
        self.assertTrue(late.with_suffix(".sub").is_file())

    def test_a_sub_has_its_name_only_when_it_is_complete(self):
        real = subghz_export.convert_cu8
        written, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def paused(path, *args, **kwargs):
            result = real(path, *args, **kwargs)                 # the Flipper file is on disk...
            written.set()
            release.wait(60.0)                                   # ...but the check is still to come
            return result

        with patch.object(subghz_export, "convert_cu8", paused):
            self.plugin.start_capture()
            recording = self.record("g001_433.92M_250k.cu8")
            self.assertTrue(written.wait(10.0))
            self.assertEqual(self.names(), [recording.name, "g001_433.92M_250k.sub.part"])
            release.set()
            self.wait_for(lambda: recording.with_suffix(".sub").is_file(), "the .sub")
        self.assertEqual(self.names(), [recording.name, "g001_433.92M_250k.sub"])

    def test_a_capture_still_converting_cannot_write_into_the_next_one(self):
        real = subghz_export.convert_cu8
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def slow(path, *args, **kwargs):
            entered.set()
            release.wait(60.0)
            return real(path, *args, **kwargs)

        with patch.object(subghz_export, "convert_cu8", slow):
            self.plugin.start_capture()
            first_dir = self.plugin._capture_dir
            recording = self.record("g001_433.92M_250k.cu8")
            self.assertTrue(entered.wait(10.0))
            self.plugin.stop_capture()
            first_thread = self.plugin._watch_thread
            self.assertTrue(self.plugin.start_capture())         # while the first is still converting
            second_dir = self.plugin._capture_dir
            release.set()
            first_thread.join(10.0)
        self.assertFalse(first_thread.is_alive())
        self.assertNotEqual(first_dir, second_dir)
        self.assertTrue(recording.with_suffix(".sub").is_file())         # the first capture's, in its folder
        self.assertEqual(self.names(second_dir), [])
        self.assertEqual((self.plugin._rows, self.plugin._converted, self.plugin._skipped, self.plugin._limit),
                         ([], 0, 0, ""))
        self.assertTrue(self.plugin.capture_active())

    def test_without_a_capture_upstreams_launches_are_exactly_stock(self):
        self.plugin.start_capture()
        self.plugin.stop_capture()                               # hook stays installed
        self.game._sdr_433()                                     # plain scanner from upstream's menu
        plain = self.rtl433()[-1]
        self.assertEqual(plain.command, sdr_manager.RTL433_CMD)
        self.assertNotIn("cwd", plain.kwargs)
        self.assertEqual(sdr_manager.RTL433_CMD[-2:], ["-M", "level"])   # constants never edited
        self.assertFalse(self.plugin.capture_active())
        with with_pyxel(Canvas()):
            self.plugin.on_update()
        self.assertFalse(self.plugin.capture_active())           # a plain scan is not a capture

    def test_only_the_rtl_433_process_is_changed_never_dump1090(self):
        self.plugin.start_capture()
        self.sdr._spawn_owned("_dump1090_proc", ["dump1090", "--net"])
        other = FakeProcess.launched[-1]
        self.assertEqual(other.command, ["dump1090", "--net"])
        self.assertNotIn("cwd", other.kwargs)

    def test_a_running_plain_scanner_is_restarted_with_recording(self):
        self.game._sdr_433()
        self.assertEqual(self.rtl433()[-1].command, sdr_manager.RTL433_CMD)
        self.assertTrue(self.plugin.start_capture())
        self.assertEqual(len(self.rtl433()), 2)
        self.assertEqual(self.rtl433()[-1].command[-2:], ["-S", "all"])
        self.assertEqual(self.hud().count("[433] Stopped"), 1)

    def test_scanner_stopped_from_upstreams_menu_ends_the_capture(self):
        self.plugin.start_capture()
        self.game._sdr_433()                                     # SDR > 433 MHz Scanner: stop
        with with_pyxel(Canvas()):
            self.plugin.on_update()
        self.assertFalse(self.plugin.capture_active())
        self.assertIn("[433] Capture ended (the 433 scanner stopped)", self.hud())
        self.game._sdr_433()                                     # ...and a later plain start
        self.assertEqual(self.rtl433()[-1].command, sdr_manager.RTL433_CMD)

    def test_sdr_switched_off_means_no_capture_and_no_leftover_folder(self):
        self.game._sdr_enabled = False
        self.assertFalse(self.plugin.start_capture())
        self.assertEqual(self.rtl433(), [])
        self.assertFalse(self.plugin.capture_active())
        self.assertIn("[SDR] Enable SDR first (SYSTEM > SDR)", self.hud())   # upstream said why
        captures = self.session / "captures_433"
        self.assertEqual(list(captures.iterdir()) if captures.exists() else [], [])
        self.game._sdr_enabled = True
        self.game._sdr_433()                                     # the scanner still works as stock
        self.assertEqual(self.rtl433()[-1].command, sdr_manager.RTL433_CMD)

    def test_missing_rtl_433_or_session_is_explained(self):
        with patch.object(sdr_manager.SDRManager, "has_rtl433", staticmethod(lambda: False)):
            self.assertFalse(self.plugin.start_capture())
        self.assertIn("rtl_433 not installed", self.hud()[-1])
        self.game.loot = None
        self.assertFalse(self.plugin.start_capture())
        self.assertIn("No loot session is open", self.hud()[-1])
        self.assertEqual(self.rtl433(), [])

    def test_a_future_c5_without_the_launcher_disables_capture_only(self):
        self.game._sdr = SimpleNamespace(running=False, mode="", has_rtl433=lambda: True)
        self.assertFalse(self.plugin.start_capture())
        self.assertIn("capture is unavailable in this build", self.hud()[-1])
        self.assertFalse(self.plugin.capture_active())

    def test_capture_stops_itself_at_the_file_limit(self):
        with patch.object(ng_433, "MAX_FILES", 2):
            self.plugin.start_capture()
            for index in (1, 2):
                self.record("g%03d_433.92M_250k.cu8" % index)
            self.wait_for(lambda: bool(self.plugin._limit), "the limit")
            with with_pyxel(Canvas()):
                self.plugin.on_update()
        self.assertFalse(self.plugin.capture_active())
        self.assertFalse(self.sdr.running)
        self.assertTrue(any("Capture OFF (2 signals recorded)" in line for line in self.hud()))

    def test_capture_stops_itself_when_storage_runs_low(self):
        usage = SimpleNamespace(free=ng_433.MIN_FREE_BYTES - 1)
        with patch.object(ng_433.shutil, "disk_usage", return_value=usage):
            self.plugin.start_capture()
            self.wait_for(lambda: bool(self.plugin._limit), "the storage guard")
            with with_pyxel(Canvas()):
                self.plugin.on_update()
        self.assertTrue(any("loot storage is nearly full" in line for line in self.hud()))
        self.assertFalse(self.sdr.running)

    def test_conversion_never_runs_on_the_game_thread(self):
        release, entered, threads = threading.Event(), threading.Event(), []
        self.addCleanup(release.set)

        def slow_convert(path, *args, **kwargs):
            threads.append(threading.current_thread())
            entered.set()
            release.wait(60.0)
            return None, 0

        with patch.object(subghz_export, "convert_cu8", slow_convert), with_pyxel(Canvas()):
            self.plugin.start_capture()
            self.record("g001_433.92M_250k.cu8")
            self.assertTrue(entered.wait(10.0))
            slowest = 0.0
            for _ in range(200):
                began = time.perf_counter()
                self.plugin.on_update()
                slowest = max(slowest, time.perf_counter() - began)
            release.set()
        self.assertLess(slowest, FRAME_LIMIT)
        self.assertEqual([t.name for t in threads], ["ng-433-capture"])
        self.assertTrue(threads[0].daemon)

    def test_a_failing_conversion_is_reported_and_the_watcher_carries_on(self):
        real = subghz_export.convert_cu8

        def flaky(path, *args, **kwargs):
            if "g001" in path:
                raise ValueError("bad recording")
            return real(path, *args, **kwargs)

        with patch.object(subghz_export, "convert_cu8", flaky):
            self.plugin.start_capture()
            self.record("g001_433.92M_250k.cu8")
            good = self.record("g002_433.92M_250k.cu8")
            self.wait_for(lambda: len(self.plugin._rows) == 2, "both recordings")
        self.assertEqual(self.plugin._rows[0][3], "failed: bad recording")
        self.assertTrue(good.with_suffix(".sub").is_file())

    def test_nothing_in_the_plugin_can_transmit(self):
        text = (ROOT / "plugins" / "ng_subghz_capture.py").read_text(encoding="utf-8")
        for word in ("cmd_tx", "cmd_replay", "subghz_tx", "serial", "rtl_sdr", "hackrf"):
            self.assertNotIn(word, text)


class CaptureScreenTests(CaptureCase):
    def test_screens_draw_inside_640x360_with_16_colours(self):
        canvas = Canvas()
        with with_pyxel(canvas):
            self.plugin.draw(0, 0, W, H)
            self.assertEqual(canvas.texts, [])                   # closed: nothing drawn
            self.plugin.open_overlay()
            self.plugin.draw(0, 0, W, H)                         # idle
            self.plugin.start_capture()
            for index in range(1, 41):                           # longer than the list
                self.record("g%03d_433.92M_250k.cu8" % index, [400, -400, 400])
            self.wait_for(lambda: len(self.plugin._rows) == 40, "40 recordings")
            self.plugin._rows.append(("x" * 80 + ".cu8", "", 0, "failed: " + "y" * 300))
            canvas.pressed = {"KEY_DOWN"}
            for _ in range(60):
                self.plugin.on_update()
            canvas.new_frame()
            self.plugin.draw(0, 0, W, H)
        self.assertEqual(canvas.overlaps(), [])                  # no text runs into another
        said = " | ".join(canvas.texts)
        self.assertIn("433 MHz CAPTURE - raw .cu8 + Flipper RAW .sub", said)
        self.assertIn("CAPTURING", said)
        self.assertIn("running (rtl_433)", said)
        self.assertIn("loot/%s/captures_433/" % self.session.name, said)
        self.assertIn("[C] Start/Stop capture  [UP/DOWN] Scroll  [ESC] Back", said)
        self.assertIn("nothing is transmitted from here", said)
        self.assertEqual(self.plugin._scroll, 41 - ng_433.LIST_ROWS)
        self.assertTrue(any(text.startswith("16-41 of 41") for text in canvas.texts))

    def test_keys(self):
        canvas = Canvas()
        with with_pyxel(canvas):
            self.plugin.open_overlay()
            canvas.pressed = {"KEY_C"}
            self.plugin.on_update()
            self.assertTrue(self.plugin.capture_active())
            self.plugin.on_update()                              # C again
            self.assertFalse(self.plugin.capture_active())
            canvas.pressed = {"KEY_ESCAPE"}
            self.plugin.on_update()
        self.assertFalse(self.plugin.overlay_active)
        self.assertEqual(self.game._esc_consumed_frame, Canvas.frame_count)


if __name__ == "__main__":
    unittest.main()
