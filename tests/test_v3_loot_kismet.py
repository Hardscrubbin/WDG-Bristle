"""[merge-v3] LootManager + Kismet bridge: override_fix, the single 0,0 rule,
the opt-in append path, the WiGLE export filter and the upload-log hold.

Real LootManager, real files in a temporary directory, the real Kismet plugin
class and upstream's real Wars Sync plugin. No Kismet, gpsd, network, pyxel or RF:
HTTP is a recorder, and no test talks to wigle.net or wdgwars.pl.
"""

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdogs.loot_manager import LootManager
from plugins.wardrive_upload import WardriveUpload
import plugins.kismet_wardrive as kismet

from test_scan_session_map import loot_at

W1, W2, W3 = "02:00:00:00:10:01", "02:00:00:00:10:02", "02:00:00:00:10:03"
B1, B2 = "02:00:00:00:20:01", "02:00:00:00:20:02"
T0 = 1_700_000_000  # fixed UTC observation time -> deterministic rows


def gps(valid=True, lat=12.5, lon=34.5):
    return SimpleNamespace(available=True, fix=SimpleNamespace(
        valid=valid, latitude=lat, longitude=lon, altitude=20.0, hdop=1.0))


def net(bssid, rssi=-60, ssid="Net"):
    return SimpleNamespace(bssid=bssid, ssid=ssid, auth="WPA2", channel="6", rssi=str(rssi))


def rows(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return [LootManager._wardrive_fields(line) for line in fh.read().splitlines()[2:]]


def zero_rows(text):
    """Data rows whose Lat,Lon columns are 0/0, parsed the way upstream parses."""
    found = []
    for line in text.splitlines()[2:]:
        fields = LootManager._wardrive_fields(line)
        if fields and LootManager._observation_position_missing(fields[6], fields[7]):
            found.append(fields[0])
    return found


class LootCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def loot(self, stamp="2026-01-01T12:00:00", position=None):
        position = gps() if position is None else position
        value = self.enterContext(loot_at(str(self.root), stamp, position))
        return value

    def csv(self, loot):
        return Path(loot.session_path) / "wardriving.csv"


class OverrideFixTests(LootCase):
    def test_override_fix_positions_a_held_record_when_the_live_fix_lapsed(self):
        loot = self.loot(position=gps(valid=False))
        self.assertTrue(loot.save_wardriving_network(net(W1), override_fix=(51.5, -0.12, 30.0, 4.5)))
        self.assertTrue(loot.save_wardriving_bt(B1, -70, "Tag", override_fix=(51.5, -0.12, 30.0, 4.5)))
        wifi, ble = rows(self.csv(loot))
        self.assertEqual(wifi[6:10], ["51.5", "-0.12", "30.0", "4.5"])
        self.assertEqual(ble[6:10], ["51.5", "-0.12", "30.0", "4.5"])
        self.assertEqual((wifi[10], ble[10]), ("WIFI", "BLE"))

    def test_live_fix_beats_override_fix(self):
        loot = self.loot(position=gps(lat=12.5, lon=34.5))
        loot.save_wardriving_network(net(W1), override_fix=(51.5, -0.12, 30.0, 4.5))
        self.assertEqual(rows(self.csv(loot))[0][6:8], ["12.5", "34.5"])

    def test_c5fw_explicit_position_beats_both(self):
        loot = self.loot(position=gps(lat=12.5, lon=34.5))
        loot.save_wardriving_network(net(W1), override_fix=(51.5, -0.12, 30.0, 4.5),
                                     position=dict(lat=-33.0, lon=151.0, altitude=5.0, accuracy=2.0))
        # upstream: an explicit None means "this observation has no position"; a
        # later fix (live or last-known-good) must not be attached to it.
        loot.save_wardriving_network(net(W2), override_fix=(51.5, -0.12, 30.0, 4.5), position=None)
        first, second = rows(self.csv(loot))
        self.assertEqual(first[6:8], ["-33.0", "151.0"])
        self.assertEqual(second[6:8], ["0.0", "0.0"])

    def test_unusable_override_fix_counts_as_no_position_and_does_not_raise(self):
        loot = self.loot(position=gps(valid=False))
        for bad in ((0.0, 0.0, 0.0, 0.0), (95.0, 0.0, 0.0, 0.0), ("x", 1, 2, 3),
                    (1.0, 2.0), None, (float("nan"), 1.0, 0.0, 0.0)):
            with self.subTest(bad=bad):
                self.assertTrue(loot.save_wardriving_network(
                    net(W1), override_fix=bad, positioned_only=True))
        self.assertFalse(self.csv(loot).exists())


class SingleZeroRuleTests(LootCase):
    def test_c5fw_default_still_writes_the_unpositioned_row_for_backfill(self):
        loot = self.loot(position=gps(valid=False))
        self.assertTrue(loot.save_wardriving_network(net(W1), observed_at=T0))
        self.assertEqual(rows(self.csv(loot))[0][6:8], ["0.0", "0.0"])

    def test_positioned_only_skips_the_write_and_still_reports_discovered(self):
        loot = self.loot(position=gps(valid=False))
        self.assertTrue(loot.save_wardriving_network(net(W1), positioned_only=True))
        self.assertTrue(loot.save_wardriving_bt(B1, -70, "Tag", positioned_only=True))
        self.assertFalse(self.csv(loot).exists())
        self.assertEqual(loot.wardrive_save_errors, 0)

    def test_positioned_only_writes_normally_when_there_is_a_position(self):
        loot = self.loot()
        loot.save_wardriving_network(net(W1), positioned_only=True)
        self.assertEqual(rows(self.csv(loot))[0][6:8], ["12.5", "34.5"])

    def test_a_live_fix_of_exactly_zero_zero_is_not_a_position(self):
        loot = self.loot(position=gps(lat=0.0, lon=0.0))
        self.assertTrue(loot.save_wardriving_network(net(W1), positioned_only=True))
        self.assertFalse(self.csv(loot).exists())

    def test_both_writers_share_one_position_decision(self):
        # the merge must not leave a second, separate 0,0 test in either writer
        source = Path(LootManager.__module__.replace(".", os.sep) + ".py")
        text = (Path(__file__).resolve().parents[1] / source).read_text(encoding="utf-8")
        for name in ("save_wardriving_network", "save_wardriving_bt"):
            body = text.split(f"    def {name}(", 1)[1].split("\n    def ", 1)[0]
            self.assertEqual(body.count("self._wardrive_coordinates(position, override_fix)"), 1, name)
            self.assertEqual(body.count("_observation_position_missing"), 1, name)
            self.assertNotIn("fix.valid", body, name)


class AppendNewTests(LootCase):
    def session(self, append):
        # a separate session folder per run; observed_at pins the row text
        loot = self.loot("2026-01-01T12:00:00" if append else "2025-01-01T12:00:00")
        kw = dict(append_new=append, observed_at=T0)
        results = [
            loot.save_wardriving_network(net(W1, -60), **kw),          # new
            loot.save_wardriving_bt(B1, -70, "Tag", **kw),             # new
            loot.save_wardriving_network(net(W1, -80), **kw),          # weaker: no-op
            loot.save_wardriving_network(net(W2, -55, 'A, "B"'), **kw),  # new, native comma/quote
            loot.save_wardriving_network(net(W1, -40), **kw),          # stronger: rewrite
            loot.save_wardriving_bt(B1, -75, "Tag", **kw),             # weaker: no-op
            loot.save_wardriving_bt(B2, -66, "Tag2", **kw),            # new
        ]
        return loot, results

    def test_append_path_produces_the_same_file_as_upstreams_rewrite_path(self):
        rewrite, r1 = self.session(False)
        expected = self.csv(rewrite).read_bytes()
        rewrite.close()
        appended, r2 = self.session(True)
        self.assertEqual(r1, r2)
        self.assertEqual(r1, [True, True, False, True, True, False, True])
        self.assertEqual(self.csv(appended).read_bytes(), expected)
        self.assertEqual([r[0] for r in rows(self.csv(appended))], [W1, B1, W2, B2])
        self.assertEqual(rows(self.csv(appended))[0][5], "-40")

    def test_new_device_is_appended_not_rewritten(self):
        loot = self.loot()
        loot.save_wardriving_network(net(W1), append_new=True)        # first row: atomic create
        with patch.object(loot, "_commit_wardrive_csv", wraps=loot._commit_wardrive_csv) as commit:
            self.assertTrue(loot.save_wardriving_network(net(W2), append_new=True))
            self.assertTrue(loot.save_wardriving_bt(B1, -70, "Tag", append_new=True))
            commit.assert_not_called()
            self.assertTrue(loot.save_wardriving_network(net(W2, -10), append_new=True))  # stronger
            commit.assert_called_once()
        self.assertEqual(len(rows(self.csv(loot))), 3)

    def test_index_stays_valid_without_rereading_the_file(self):
        loot = self.loot()
        for index in range(40):
            loot.save_wardriving_network(net("02:00:00:00:30:%02X" % index), append_new=True)
        with patch("builtins.open", side_effect=AssertionError("file was re-read")):
            self.assertFalse(loot.save_wardriving_network(net("02:00:00:00:30:05", -99), append_new=True))
        self.assertEqual(len(loot._wardrive_csv_cache[3]), 40)

    def test_failed_append_rolls_back_and_a_retry_recovers(self):
        loot = self.loot()
        loot.save_wardriving_network(net(W1), append_new=True)
        before = self.csv(loot).read_bytes()
        with patch("watchdogs.loot_manager.os.fsync", side_effect=OSError("disk full")):
            self.assertFalse(loot.save_wardriving_network(net(W2), append_new=True))
        self.assertEqual(self.csv(loot).read_bytes(), before)   # no torn/half row
        self.assertEqual(loot.wardrive_save_errors, 1)
        self.assertTrue(loot.save_wardriving_network(net(W2), append_new=True))
        self.assertEqual([r[0] for r in rows(self.csv(loot))], [W1, W2])

    def test_external_change_is_noticed_before_the_next_append(self):
        loot = self.loot()
        loot.save_wardriving_network(net(W1), append_new=True)
        with open(self.csv(loot), "a", encoding="utf-8", newline="") as fh:
            fh.write(f"{W2},Other,[WPA2_PSK][ESS],2026-01-01 00:00:00,6,-50,1.0,2.0,0.0,0.0,WIFI\n")
        self.assertFalse(loot.save_wardriving_network(net(W2, -70), append_new=True))  # known, weaker
        self.assertTrue(loot.save_wardriving_network(net(W3), append_new=True))
        self.assertEqual([r[0] for r in rows(self.csv(loot))], [W1, W2, W3])

    def test_torn_last_line_does_not_swallow_the_next_row(self):
        loot = self.loot()
        loot.save_wardriving_network(net(W1), append_new=True)
        with open(self.csv(loot), "a", encoding="utf-8", newline="") as fh:
            fh.write(f"{W2},Torn,[WPA2_PSK][ESS],2026-01-01 00:00:00,6,-50,1.0,2.0,0.0,0.0,WIFI")
        self.assertTrue(loot.save_wardriving_network(net(W3), append_new=True))
        self.assertEqual([r[0] for r in rows(self.csv(loot))], [W1, W2, W3])

    def test_c5fw_default_path_is_untouched_by_the_flag(self):
        loot = self.loot()
        with patch.object(loot, "_append_line_fsync") as append:
            loot.save_wardriving_network(net(W1))
            loot.save_wardriving_network(net(W2))
            loot.save_bt_device(B1, -70, "Tag", False, False)
            loot.save_bt_device(B2, -70, "Tag", False, False)
            append.assert_not_called()


class BtDeviceAppendTests(LootCase):
    def test_append_path_dedups_sanitises_and_batches_the_db_rescan(self):
        loot = self.loot()
        path = Path(loot.session_path) / "bt_devices.csv"
        with patch.object(loot, "update_session_loot", wraps=loot.update_session_loot) as rescan:
            self.assertTrue(loot.save_bt_device(B1, -70, 'Tile, "Tracker"', False, False, append_new=True))
            for index in range(30):
                self.assertTrue(loot.save_bt_device("02:00:00:00:40:%02X" % index, -60, "T",
                                                    False, False, append_new=True))
            self.assertFalse(loot.save_bt_device(B1, -50, "again", False, False, append_new=True))
            self.assertEqual(rescan.call_count, 1)          # upstream's path: one per device
            self.assertTrue(loot._loot_update_pending)
            loot.flush_pending_loot_update()
            self.assertEqual(rescan.call_count, 2)
            loot.flush_pending_loot_update()                 # nothing owed any more
            self.assertEqual(rescan.call_count, 2)
        table = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(table[0], ["timestamp", "mac", "rssi", "name", "airtag", "smarttag", "lat", "lon"])
        self.assertEqual(len(table), 32)
        self.assertTrue(all(len(row) == 8 for row in table))
        self.assertEqual(table[1][1:4], [B1, "-70", "Tile; 'Tracker'"])

    def test_both_paths_can_share_one_file(self):
        loot = self.loot()
        self.assertTrue(loot.save_bt_device(B1, -70, "A", False, False))                    # upstream
        self.assertTrue(loot.save_bt_device(B2, -70, "B", False, False, append_new=True))    # v2
        self.assertFalse(loot.save_bt_device(B2, -70, "B", False, False))                    # upstream sees it
        self.assertFalse(loot.save_bt_device(B1, -70, "A", False, False, append_new=True))   # v2 sees it
        text = (Path(loot.session_path) / "bt_devices.csv").read_text(encoding="utf-8")
        self.assertEqual(len(text.splitlines()), 3)


def make_plugin(app_dir, loot):
    """The real Kismet plugin, loaded the way app.py loads it, with a fake app."""
    plugin = kismet.KismetWardriveBridge()
    plugin.app = SimpleNamespace(loot=loot, _app_dir=str(app_dir), gps_fix=True,
                                 _term_add=lambda *_a, **_k: None, msg=lambda *_a, **_k: None)
    plugin.term = Mock()
    plugin.msg = Mock()
    plugin.engine = SimpleNamespace(log_dir=str(Path(app_dir) / "loot" / "kismet_logs"), running=False)
    os.makedirs(plugin.engine.log_dir, exist_ok=True)
    return plugin


class NoZeroZeroReachesAnUploadTests(LootCase):
    """A row that still has no position when the upload happens is either
    backfilled first or left out -- for WDGWars (upstream) and for WiGLE (Kismet)."""

    def mixed_session(self):
        loot = self.loot(position=gps(valid=False))
        loot.save_wardriving_network(net(W1, -60, "NoFixYet"), observed_at=T0)       # 0,0 (upstream default)
        loot.save_wardriving_network(net(W2, -61, "NeverFixed"), observed_at=T0 + 1)  # 0,0, stays 0,0
        loot.save_wardriving_bt(B1, -70, "NoFixBle", observed_at=T0 + 2)              # 0,0, stays 0,0
        loot.save_wardriving_network(net(W3, -62, 'Has, "Fix"'), observed_at=T0 + 3,
                                     position=dict(lat=12.5, lon=34.5, altitude=5.0, accuracy=3.0))
        return loot

    def wdgwars_rows(self, path):
        with patch.object(WardriveUpload, "_load_state"):
            plugin = WardriveUpload()
        return plugin._parse_csv(Path(path))

    def test_file_really_contains_unpositioned_rows(self):
        loot = self.mixed_session()
        self.assertEqual(zero_rows(self.csv(loot).read_text(encoding="utf-8")), [W1, W2, B1])

    def test_wdgwars_upload_payload_leaves_them_out(self):
        loot = self.mixed_session()
        payload = self.wdgwars_rows(self.csv(loot))
        self.assertEqual([item["bssid"] for item in payload], [W3])
        self.assertTrue(all((item["lat"], item["lon"]) != (0.0, 0.0) for item in payload))

    def test_backfilled_row_is_uploaded_with_its_real_position(self):
        loot = self.mixed_session()
        result = loot.fill_wardriving_position(
            "WIFI", W1, observed_at=T0, position=dict(lat=40.0, lon=-3.0, altitude=1.0, accuracy=2.0))
        self.assertEqual((result["status"], result["persisted"], result["changed"]),
                         ("updated", True, True))
        payload = {item["bssid"]: (item["lat"], item["lon"]) for item in self.wdgwars_rows(self.csv(loot))}
        self.assertEqual(payload, {W1: (40.0, -3.0), W3: (12.5, 34.5)})
        data, kept, dropped = kismet.positioned_wigle_export(self.csv(loot))
        self.assertEqual((kept, dropped), (2, 2))
        self.assertEqual(zero_rows(data.decode("utf-8")), [])

    def test_wigle_export_keeps_headers_and_only_positioned_clean_rows(self):
        loot = self.mixed_session()
        original = self.csv(loot).read_text(encoding="utf-8").splitlines()
        data, kept, dropped = kismet.positioned_wigle_export(self.csv(loot))
        lines = data.decode("utf-8").splitlines()
        self.assertEqual(lines[:2], original[:2])             # WiGLE pre-header + column header
        self.assertEqual((kept, dropped, len(lines)), (1, 3, 3))
        self.assertEqual(zero_rows(data.decode("utf-8")), [])
        fields = next(csv.reader([lines[2]]))
        self.assertEqual(len(fields), 11)                     # raw comma/quote in the SSID is gone
        self.assertEqual((fields[0], fields[1], fields[6], fields[7]),
                         (W3, "Has; 'Fix'", "12.5", "34.5"))
        self.assertTrue(data.endswith(b"\n"))

    def test_bytes_posted_to_wigle_never_contain_a_zero_zero_row(self):
        loot = self.mixed_session()
        plugin = make_plugin(self.root, loot)
        posted = []

        def post(url, **kwargs):
            posted.append(kwargs["files"]["file"][1])
            return SimpleNamespace(status_code=200, json=lambda: {"success": True}, text="")

        with patch.object(kismet, "requests", SimpleNamespace(post=post), create=True):
            ok, detail = plugin._do_wigle_upload(self.csv(loot))
        self.assertEqual((ok, detail), (True, None))
        self.assertEqual(len(posted), 1)
        self.assertIsInstance(posted[0], bytes)
        self.assertEqual(zero_rows(posted[0].decode("utf-8")), [])
        self.assertNotIn(b",0.0,0.0,", posted[0])
        self.assertIn(W3.encode(), posted[0])
        for mac in (W1, W2, B1):
            self.assertNotIn(mac.encode(), posted[0])

    def test_session_with_no_fix_at_all_posts_nothing(self):
        loot = self.loot(position=gps(valid=False))
        loot.save_wardriving_network(net(W1), observed_at=T0)
        loot.save_wardriving_bt(B1, -70, "Tag", observed_at=T0)
        plugin = make_plugin(self.root, loot)
        post = Mock()
        with patch.object(kismet, "requests", SimpleNamespace(post=post), create=True):
            ok, detail = plugin._do_wigle_upload(self.csv(loot))
        self.assertIsNone(ok)
        self.assertIn("no rows with a GPS position", detail)
        post.assert_not_called()
        self.assertEqual(self.wdgwars_rows(self.csv(loot)), [])

    def test_kismet_bridge_writes_can_never_add_a_zero_zero_row(self):
        # app.gps_fix says "fix" while LootManager's own fix has just lapsed
        loot = self.loot(position=gps(valid=False))
        plugin = make_plugin(self.root, loot)
        plugin._save_wifi_or_hold(net(W1), True)
        plugin._save_bt_or_hold(B1, -70, "Tag", True)
        self.assertFalse(self.csv(loot).exists())
        # held records flushed at stop with no last-known-good fix: left out
        plugin._held_networks, plugin._held_bt = {W2: net(W2)}, {B2: (-70, "Tag")}
        plugin._last_good_fix = None
        plugin._drain_backlog = Mock()
        plugin._write_session_summary = Mock()
        plugin._finish_stop()
        self.assertFalse(self.csv(loot).exists())
        # ...and with a last-known-good fix they are written under it
        plugin._held_networks, plugin._held_bt = {W2: net(W2)}, {B2: (-70, "Tag")}
        plugin._last_good_fix = (51.5, -0.12, 30.0, 4.5)
        plugin._finish_stop()
        written = rows(self.csv(loot))
        self.assertEqual([(r[0], r[6], r[7]) for r in written],
                         [(W2, "51.5", "-0.12"), (B2, "51.5", "-0.12")])
        self.assertEqual(zero_rows(self.csv(loot).read_text(encoding="utf-8")), [])

    def test_bt_devices_csv_is_not_an_upload_file(self):
        with patch.object(WardriveUpload, "_load_state"):
            plugin = WardriveUpload()
        self.assertNotIn("bt_devices.csv", plugin._upload_log_specs())
        self.assertIn("wardriving.csv", plugin._upload_log_specs())


class WigleQueueHoldTests(LootCase):
    """upstream's cleanup must not delete a wardriving.csv the WiGLE queue points at."""

    def setUp(self):
        super().setUp()
        with loot_at(str(self.root), "2020-01-01T12:00:00", gps()) as old:
            old.save_wardriving_network(net(W1), observed_at=T0)
            old.save_wardriving_bt(B1, -70, "Tag", observed_at=T0)
            self.old_session = Path(old.session_dir)
        self.old_csv = self.old_session / "wardriving.csv"
        self.loot = self.loot("2022-01-01T12:00:00")
        self.kismet = make_plugin(self.root, self.loot)
        with patch.object(WardriveUpload, "_load_state"):
            self.sync = WardriveUpload()
        self.sync.app = SimpleNamespace(loot=self.loot, _term_add=lambda *_a, **_k: None)
        self.sync._api_url = "https://example.invalid/api/upload/"
        self.sync._api_key = "synthetic-test-only"
        self.sync._save_state = Mock(return_value=True)

    def digest(self):
        return hashlib.sha256(self.old_csv.read_bytes()).hexdigest()

    def manual_delete(self):
        item = next(entry for entry in self.sync.list_pending_logs()
                    if entry["session"] == self.old_session.name and entry["filename"] == "wardriving.csv")
        return self.sync.delete_pending_log(item["session"], "wardriving.csv", item["token"])

    def test_plugin_registers_its_queue_with_the_loot_manager(self):
        self.kismet._register_upload_hold()
        self.kismet._register_upload_hold()                       # idempotent
        self.assertEqual(self.loot._upload_log_holds, [self.kismet._wigle_queue_hold])
        self.assertEqual(self.loot.upload_log_hold_reason(self.old_csv), "")

    def test_registration_follows_a_replaced_loot_manager(self):
        self.kismet._register_upload_hold()
        other = SimpleNamespace(add_upload_log_hold=Mock())
        self.kismet.app.loot = other
        self.kismet._register_upload_hold()
        other.add_upload_log_hold.assert_called_once_with(self.kismet._wigle_queue_hold)

    def test_queued_file_survives_every_discard_request(self):
        self.kismet._register_upload_hold()
        self.kismet._queue_wigle_upload(self.old_csv)
        self.assertIn("WiGLE", self.loot.upload_log_hold_reason(self.old_csv))
        # the one gate all four of upstream's deleters go through
        self.assertFalse(self.loot.prepare_upload_log_discard(self.old_session, "wardriving.csv", self.digest()))
        # manual D in Pending Upload Logs
        result = self.manual_delete()
        self.assertFalse(result["deleted"])
        self.assertIn("queued for WiGLE upload", result["reason"])
        # automatic no-GPS cleanup and the Auto-clean retention pass
        self.sync.cleanup_no_gps_logs()
        self.sync._retention_tick()
        self.assertTrue(self.old_csv.is_file())
        self.assertEqual([r[0] for r in rows(self.old_csv)], [W1, B1])

    def test_file_can_be_deleted_again_once_it_left_the_queue(self):
        self.kismet._register_upload_hold()
        self.kismet._queue_wigle_upload(self.old_csv)
        self.assertFalse(self.manual_delete()["deleted"])
        self.kismet._save_wigle_queue([])
        self.assertTrue(self.manual_delete()["deleted"])
        self.assertFalse(self.old_csv.exists())

    def test_hold_is_per_file_and_path_spelling_does_not_matter(self):
        self.kismet._register_upload_hold()
        spelled = str(self.old_session / "." / "wardriving.csv")
        self.kismet._save_wigle_queue([spelled])
        self.assertTrue(self.kismet._wigle_queue_hold(self.old_csv))
        self.assertFalse(self.kismet._wigle_queue_hold(self.old_session / "adsb_aircraft.csv"))

    def test_a_failing_hold_check_keeps_the_file(self):
        self.loot.add_upload_log_hold(Mock(side_effect=RuntimeError("boom")))
        self.assertFalse(self.loot.prepare_upload_log_discard(self.old_session, "wardriving.csv", self.digest()))
        self.assertTrue(self.old_csv.is_file())

    def test_without_a_hold_c5_deletes_exactly_as_before(self):
        self.assertTrue(self.manual_delete()["deleted"])

    def retry(self, queue, responses):
        self.kismet._save_wigle_queue([str(path) for path in queue])
        posted = []

        def post(url, **kwargs):
            posted.append(kwargs["files"]["file"])
            status = responses.pop(0)
            return SimpleNamespace(status_code=status, text="x",
                                   json=lambda: {"success": status == 200})

        with patch.object(kismet, "requests", SimpleNamespace(post=post), create=True), \
                patch.object(kismet, "HAVE_REQUESTS", True), \
                patch.object(kismet, "WIGLE_API_NAME", "name"), \
                patch.object(kismet, "WIGLE_API_TOKEN", "token"), \
                patch.object(kismet.hw, "internet_available", return_value=True):
            self.kismet._retry_wigle_queue()
            # The pass now runs on the WiGLE worker (tests/test_v3_background_io.py).
            worker = self.kismet._wigle_thread
            worker.join(30)
            self.assertFalse(worker.is_alive())
            self.kismet._drain_wigle_events()
        return posted, self.kismet._load_wigle_queue()

    def test_queue_tolerates_a_file_deleted_outside_the_game(self):
        gone = self.old_session / "deleted-by-hand.csv"
        posted, remaining = self.retry([gone, self.old_csv], [200])
        self.assertEqual(len(posted), 1)                       # the surviving file still went out
        self.assertEqual(posted[0][0], "wardriving.csv")
        self.assertEqual(remaining, [])
        said = " ".join(str(call.args[0]) for call in self.kismet.term.call_args_list)
        self.assertIn("is gone, dropped from the queue", said)

    def test_failed_retry_stays_queued_and_held(self):
        self.kismet._register_upload_hold()
        posted, remaining = self.retry([self.old_csv], [503])
        self.assertEqual(remaining, [str(self.old_csv)])
        self.assertFalse(self.manual_delete()["deleted"])
        self.assertTrue(self.old_csv.is_file())

    def test_wigle_and_wdgwars_are_different_services(self):
        # the "double post" question: the bridge only ever talks to wigle.net,
        # upstream's uploader never does.
        self.assertTrue(kismet.WIGLE_API_URL.startswith("https://api.wigle.net/"))
        root = Path(__file__).resolve().parents[1]
        self.assertNotIn("wigle.net", (root / "plugins" / "wardrive_upload.py").read_text(encoding="utf-8"))
        bridge = (root / "plugins" / "kismet_wardrive.py").read_text(encoding="utf-8")
        self.assertNotIn("WARDRIVE_API", bridge)       # never reads the WDGWars key/URL
        self.assertNotIn("urlopen", bridge)
        import re
        targets = re.findall(r"requests\.(?:post|put)\(\s*([A-Za-z_][\w.]*)", bridge)
        self.assertEqual(targets, ["WIGLE_API_URL"])    # its one and only upload call


if __name__ == "__main__":
    unittest.main()
