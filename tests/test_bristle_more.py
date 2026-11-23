# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""Unit tests for the WDG side of Bristle 1.1: the taps, the hunt listener's packet reading,
firmware images and flash commands, the nearby engine, the GPS track on disk, the export file,
the host adapter and the screens' text. Logic only: no radio, no board, no game window.
"""
import hashlib
import re
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugins._bristle import (alerts, codec, flasher, host, hunt_scan, ingest, kismet_tap, link_usb, nearby_engine,  # noqa: E402
                              proto, screen, screen_more, second_core, service, settings, skimmer, wdg_ports)
from plugins._bristle import session as S  # noqa: E402
from plugins._bristle.gpsplace import Fix  # noqa: E402
from plugins._bristle.nearby import Observation  # noqa: E402

HW = "a0b1c2d3e4f5"
T0 = 1_795_300_000.0


class Temp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        for queue in (kismet_tap.devices, kismet_tap.alerts):
            queue.clear()

    def make_service(self, name="a"):
        folder = self.root / name
        folder.mkdir(exist_ok=True)
        with patch.object(service, "bench_dirs", return_value=[]):
            return service.BristleService(folder, folder / "loot", lambda find: None)


class NewSettings(Temp):
    def test_numbers_lists_and_macs(self):
        conf = dict(settings.DEFAULTS, follow_distance_m="abc", ble_max_links="99", ignore_macs="AA:BB:CC:DD:EE:01, junk,"
                    "0011223344ff", kismet_alerts="deauthflood, ,APSPOOF")
        self.assertEqual(settings.number(conf, "follow_distance_m", 50, 20000), 500.0)
        self.assertEqual(settings.number(conf, "ble_max_links", 1, 7), 7.0)
        self.assertEqual(settings.mac_list(conf, "ignore_macs"), {"aabbccddee01", "0011223344ff"})
        self.assertEqual(settings.name_list(conf, "kismet_alerts"), ["DEAUTHFLOOD", "APSPOOF"])
        for name in settings.ALERT_SWITCHES:
            self.assertIn("alert_" + name, settings.DEFAULTS)
        self.assertEqual(set(settings.ALERT_SWITCHES), set(alerts.TYPES))

    def test_the_core_id_is_made_once(self):
        first = settings.core_id(self.root / "d")
        self.assertRegex(first, "^[0-9a-f]{8}$")
        self.assertEqual(settings.core_id(self.root / "d"), first)

    def test_an_imported_key_replaces_the_old_one_and_keeps_it_and_everything_else(self):
        path = self.root / "secrets.conf"
        path.write_text("WIGLE_API_NAME=abc\n# note\nBRISTLE_OWNER_KEY=%s\nWDG_KEY=xyz\n" % ("11" * 16), encoding="utf-8")
        settings.replace_owner_key(path, bytes.fromhex("22" * 16))
        text = path.read_text(encoding="utf-8")
        self.assertIn("WIGLE_API_NAME=abc\n# note\n", text)
        self.assertIn("WDG_KEY=xyz\n", text)
        self.assertIn("BRISTLE_OWNER_KEY_PREVIOUS=%s\n" % ("11" * 16), text)
        self.assertTrue(text.endswith("BRISTLE_OWNER_KEY=%s\n" % ("22" * 16)))
        self.assertEqual(settings.owner_key(path)[0], bytes.fromhex("22" * 16))
        settings.replace_owner_key(path, bytes.fromhex("33" * 16))
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("BRISTLE_OWNER_KEY_PREVIOUS="), 1)
        self.assertIn("PREVIOUS=%s" % ("22" * 16), text)
        with self.assertRaises(ValueError):
            settings.replace_owner_key(path, b"short")


class KismetTap(Temp):
    def setUp(self):
        super().setUp()
        kismet_tap.enabled.set()
        self.addCleanup(kismet_tap.enabled.clear)
        kismet_tap.state.update(cod_seen=False, alerts_since=0.0)

    def test_a_classic_device_with_its_class(self):
        record = {"kismet.device.base.type": "BR/EDR", "kismet.device.base.last_time": 1795300010,
                  "bluetooth.device": {"bluetooth.device.class": 0x001F00}}
        kismet_tap.bt_device(record, "98:D3:31:AA:BB:CC", "HC-05", -66)
        got = kismet_tap.devices.popleft()
        self.assertEqual(got, {"mac": "98d331aabbcc", "name": "HC-05", "rssi": -66, "t": 1795300010.0,
                               "classic": True, "cod": 0x001F00, "heard_by": "Kismet"})
        self.assertTrue(kismet_tap.state["cod_seen"])

    def test_an_older_kismet_without_the_class_and_a_device_without_a_name(self):
        kismet_tap.bt_device({"kismet.device.base.type": "BTLE"}, "AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:FF", -100)
        got = kismet_tap.devices.popleft()
        self.assertEqual((got["name"], got["rssi"], got["classic"], got["cod"]), ("", None, False, None))
        self.assertFalse(kismet_tap.state["cod_seen"])
        kismet_tap.bt_device("not a record", "x", "y", -50)            # never raises, never queues junk
        self.assertEqual(len(kismet_tap.devices), 0)

    def test_nothing_is_kept_while_bristle_is_off(self):
        kismet_tap.enabled.clear()
        kismet_tap.bt_device({}, "AA:BB:CC:DD:EE:FF", "x", -50)
        self.assertEqual(len(kismet_tap.devices), 0)

    def test_alerts_are_read_once_each(self):
        calls = []
        payload = [{"kismet.alert.header": "DEAUTHFLOOD", "kismet.alert.text": "flood", "kismet.alert.timestamp": 100.5,
                    "kismet.alert.transmitter_mac": "AA:BB:CC:00:00:01", "kismet.alert.channel": "6"}]

        class Engine:
            running = True

            def datasources(self):
                return [{"uuid": "u1", "name": "hci1", "interface": "hci1", "kind": "bt", "running": True}], None

            def _auth(self):
                return None

            def _get(self, url, **kwargs):
                calls.append(url)
                return SimpleNamespace(status_code=200, json=lambda: payload)

        engine = Engine()
        with patch.object(kismet_tap, "_last_alert_poll", -1e9), patch.dict(kismet_tap.sources, {"t": -1e9}):
            kismet_tap.poll(engine, "http://localhost:2501")
            self.assertEqual(len(kismet_tap.alerts), 1)
            self.assertEqual(kismet_tap.alerts[0]["header"], "DEAUTHFLOOD")
            self.assertEqual(kismet_tap.alerts[0]["mac"], "aabbcc000001")
            self.assertEqual([s["name"] for s in kismet_tap.bluetooth_sources()], ["hci1"])
            with patch.object(kismet_tap, "_last_alert_poll", -1e9):
                kismet_tap.poll(engine, "http://localhost:2501")
            self.assertEqual(len(kismet_tap.alerts), 1)            # the same alert is not taken twice
        self.assertTrue(all("/alerts/last-time/" in url for url in calls))


class HuntPackets(unittest.TestCase):
    MAC = "c0ffee123456"

    def legacy(self, mac, rssi, data=b"\x02\x01\x06"):
        report = bytes([0x03, 0x00]) + bytes(reversed(bytes.fromhex(mac))) + bytes([len(data)]) + data + struct.pack("b", rssi)
        body = bytes([hunt_scan.SUB_ADV_REPORT, 1]) + report
        return bytes([hunt_scan.HCI_EVENT, hunt_scan.EVT_LE_META, len(body)]) + body

    def extended(self, mac, rssi, data=b"\x02\x01\x06"):
        report = (struct.pack("<HB", 0x0010, 0x01) + bytes(reversed(bytes.fromhex(mac))) + bytes([1, 0, 0xFF, 127])
                  + struct.pack("b", rssi) + struct.pack("<H", 0) + bytes([0]) + bytes(6) + bytes([len(data)]) + data)
        body = bytes([hunt_scan.SUB_EXT_ADV_REPORT, 1]) + report
        return bytes([hunt_scan.HCI_EVENT, hunt_scan.EVT_LE_META, len(body)]) + body

    def test_both_report_formats_give_the_address_in_printed_order_and_the_signal(self):
        self.assertEqual(hunt_scan.parse_le_reports(self.legacy(self.MAC, -71)), [(self.MAC, -71)])
        self.assertEqual(hunt_scan.parse_le_reports(self.extended(self.MAC, -48)), [(self.MAC, -48)])

    def test_no_reading_and_damaged_packets(self):
        self.assertEqual(hunt_scan.parse_le_reports(self.legacy(self.MAC, 127)), [])
        self.assertEqual(hunt_scan.parse_le_reports(self.legacy(self.MAC, -70)[:9]), [])
        self.assertEqual(hunt_scan.parse_le_reports(b""), [])
        self.assertEqual(hunt_scan.parse_le_reports(bytes([0x04, 0x0E, 4, 1, 0x0C, 0x20, 0])), [])

    def test_the_scan_is_passive_and_duplicates_are_not_filtered(self):
        for extended in (False, True):
            params = hunt_scan.scan_params_command(extended)
            self.assertEqual(params[0], hunt_scan.HCI_COMMAND)
            self.assertEqual(params[3], len(params) - 4)
            enable = hunt_scan.scan_enable_command(extended, True)
            self.assertEqual(enable[4:6], bytes([1, 0]))           # on, and every advertisement reported
        self.assertEqual(hunt_scan.scan_params_command(False)[4], hunt_scan.SCAN_PASSIVE)
        self.assertEqual(hunt_scan.scan_params_command(True)[7], hunt_scan.SCAN_PASSIVE)
        self.assertEqual(struct.unpack_from("<H", hunt_scan.scan_params_command(False), 1)[0], 0x200B)
        self.assertEqual(struct.unpack_from("<H", hunt_scan.scan_enable_command(True, False), 1)[0], 0x2042)

    def test_command_answers(self):
        done = bytes([0x04, 0x0E, 4, 1, 0x0C, 0x20, 0x00])
        refused = bytes([0x04, 0x0F, 4, 0x0C, 1, 0x0C, 0x20])
        self.assertEqual(hunt_scan.command_result(done, 0x200C), 0)
        self.assertEqual(hunt_scan.command_result(refused, 0x200C), 0x0C)
        self.assertIsNone(hunt_scan.command_result(done, 0x200B))
        self.assertEqual(hunt_scan.adapter_index("hci1"), 1)
        with self.assertRaises(ValueError):
            hunt_scan.adapter_index("wlan0")


class Firmware(Temp):
    def release(self, name="v1-build4-dev", damage=False):
        folder = self.root / "bristle" / "firmware" / "releases" / name
        lines = []
        for board in flasher.BOARDS:
            (folder / board).mkdir(parents=True, exist_ok=True)
            for kind in ("FULL", "app"):
                data = bytes([0xE9]) + (board + kind + name).encode() * 200
                path = folder / board / ("bristle-%s-%s.bin" % (board, kind))
                path.write_bytes(data)
                lines.append("%s  %s/%s" % (hashlib.sha256(data).hexdigest(), board, path.name))
        (folder / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="ascii")
        if damage:
            (folder / "xiao_c5" / "bristle-xiao_c5-app.bin").write_bytes(b"\xe9changed")
        return folder

    def test_the_newest_release_is_used_and_an_image_must_match_its_hash(self):
        self.release("v1-build3-dev")
        newest = self.release("v1-build10")
        (self.root / "bristle" / "firmware" / "releases" / "notes").mkdir()
        found = flasher.releases(self.root)
        self.assertEqual([p.name for p in found], ["v1-build10", "v1-build3-dev"])
        self.assertEqual(flasher.build_of("v1-build10"), 10)
        self.assertEqual(flasher.image(newest, "devkit_c5_uart", "FULL").name, "bristle-devkit_c5_uart-FULL.bin")
        self.assertEqual(flasher.releases(self.root / "nowhere"), [])

    def test_a_changed_or_unlisted_image_is_never_used(self):
        folder = self.release(damage=True)
        with self.assertRaises(flasher.FirmwareError) as caught:
            flasher.image(folder, "xiao_c5", "app")
        self.assertIn("SHA-256", str(caught.exception))
        (folder / "SHA256SUMS").write_text("", encoding="ascii")
        with self.assertRaises(flasher.FirmwareError):
            flasher.image(folder, "devkit_c5", "app")
        with self.assertRaises(flasher.FirmwareError):
            flasher.image(folder, "esp8266", "app")

    def test_first_flash_erases_everything_and_a_later_one_keeps_settings(self):
        first = flasher.flash_commands("py", "/dev/ttyACM1", "FULL.bin", True)
        self.assertEqual([c[-1] if len(c) == 10 else c[9:] for c in first], ["erase-flash", ["write-flash", "0x0", "FULL.bin"]])
        later = flasher.flash_commands("py", "/dev/ttyACM1", "app.bin", False)
        self.assertEqual(later[0][9:], ["erase-region", "0xe000", "0x2000"])      # so the app just written boots
        self.assertEqual(later[1][9:], ["write-flash", "0x10000", "app.bin"])
        for command in first + later:
            self.assertEqual(command[:7], ["py", "-m", "esptool", "--chip", "esp32c5", "--port", "/dev/ttyACM1"])
            self.assertNotIn("erase-flash", command[8:] if command in later else [])

    def test_a_flash_job_runs_its_steps_in_order_and_reports_a_failure(self):
        ran = []
        done = []
        job = flasher.FlashJob("/dev/ttyACM1", self.root / "x.bin", True, done=done.append,
                               run=lambda command, on_line: (ran.append(command[9]), on_line("Hash of data verified.")))
        job.start()
        job.thread.join(5)
        self.assertEqual((ran, job.state, job.line), (["erase-flash", "write-flash"], "done", "Hash of data verified."))
        self.assertEqual(done, [job])

        def fail(command, on_line):
            raise flasher.FirmwareError("could not open port")
        bad = flasher.FlashJob("/dev/ttyACM1", self.root / "x.bin", False, run=fail)
        bad.start()
        bad.thread.join(5)
        self.assertEqual((bad.state, bad.error), ("failed", "could not open port"))

    def test_board_guess_and_names(self):
        self.assertEqual(flasher.guess_board(0x303A, 0x1001), "xiao_c5")
        self.assertEqual(flasher.guess_board(0x1A86, 0x55D3), "devkit_c5_uart")
        self.assertEqual(set(flasher.BOARD_NAMES), set(flasher.BOARDS))


def node_find(kind, seq, t, **fields):
    return S.Find(HW, 3, kind, seq, t, 7, {k: str(v) for k, v in fields.items()})


class Engine(Temp):
    def engine(self, **conf):
        said = []
        registry = SimpleNamespace(nodes={"0a0a0a0a0a0a": object()}, by_number=lambda n: object() if n == 4 else None)
        made = nearby_engine.NearbyEngine(dict(settings.DEFAULTS, **conf), self.root / "loot", registry,
                                          place=lambda t: (47.6, -122.33), here=lambda: (47.6, -122.33),
                                          say=lambda text, kind="info": said.append(text))
        return made, said

    def test_a_find_becomes_a_sighting_with_everything_the_node_reported(self):
        o = nearby_engine.observation_from_find(
            node_find("BT", 1, T0, m="c0ffee123456", r=-61, at=1, nm="Tag", mf=76, mt=18, sv="fd5a.feed", bh=HW, bo=1),
            Fix(T0, 47.5, -122.2))
        self.assertEqual((o.kind, o.mac, o.name, o.rssi, o.lat, o.heard_by), ("bt", "c0ffee123456", "Tag", -61, 47.5, "node 3"))
        self.assertEqual((o.company_id, o.mfg_type, o.service_uuids, o.bristle_hwid, o.bristle_owned),
                         (76, 18, (0xFD5A, 0xFEED), HW, True))
        ap = nearby_engine.observation_from_find(node_find("AP", 2, T0, b="b41e52000001", c=6, r=-70, k=3, e="Cam"), None)
        self.assertEqual((ap.kind, ap.mac, ap.name, ap.lat), ("wifi", "b41e52000001", "Cam", None))

    def test_signature_alerts_are_raised_logged_and_switchable(self):
        engine, said = self.engine(alert_flock="off")
        engine.session_dir = str(self.root / "session")
        engine.incoming.append(Observation("wifi", "b41e52000001", "", -70, T0, 47.6, -122.33, "node 1"))
        engine.incoming.append(Observation("bt", "0025df000001", "Body 3", -60, T0, 47.6, -122.33, "node 2"))
        engine.process()
        self.assertEqual([a.type for a in engine.center.recent], ["axon"])
        self.assertEqual(engine.center.counts["off"], 1)
        self.assertTrue(any(text.startswith("[ALERT] Axon device") for text in said))
        engine.save()
        rows = (self.root / "session" / "bristle_alerts.csv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(rows[0], alerts.LOG_HEADER)
        self.assertIn(",axon,0025df000001,", rows[1])

    def test_my_own_devices_never_alert(self):
        engine, _said = self.engine(ignore_macs="0025DF000009")
        for o in (Observation("bt", "0025df000009", "Body", -50, T0),
                  Observation("bt", "11" * 6, "BRISTLE-4", -50, T0),                      # my node 4, heard by Kismet
                  Observation("bt", "22" * 6, "", -50, T0, bristle_hwid="0a0a0a0a0a0a"),  # my node, heard by a node
                  Observation("wifi", "0a0a0a0a0a0a", "x", -50, T0)):
            engine.incoming.append(o)
        engine.process()
        self.assertEqual(len(engine.center.recent), 0)
        engine.incoming.append(Observation("bt", "33" * 6, "BRISTLE-9", -50, T0))      # not one of mine
        engine.incoming.append(Observation("bt", "44" * 6, "", -50, T0, bristle_hwid="0b0b0b0b0b0b", bristle_owned=True))
        engine.center.type_gap_s = 0
        engine.process()
        self.assertEqual([a.type for a in engine.center.recent], ["rig", "rig"])

    def test_a_skimmer_like_device_from_kismet_is_logged_and_alerts_only_when_strong(self):
        engine, _said = self.engine()
        kismet_tap.devices.append({"mac": "98d331aabbcc", "name": "HC-05", "rssi": -88, "t": T0, "classic": True,
                                   "cod": 0x001F00, "heard_by": "Kismet hci1"})
        engine.process()
        suspect = engine.suspects.suspects["98d331aabbcc"]
        self.assertEqual((suspect.score, suspect.sightings, suspect.heard_by), (80, 1, ["Kismet hci1"]))
        self.assertEqual(suspect.points[0][1:3], [47.6, -122.33])       # placed with the uConsole's GPS
        self.assertEqual(len(engine.center.recent), 0)                  # far away: listed, no alert
        kismet_tap.devices.append({"mac": "98d331aabbcc", "name": "HC-05", "rssi": -58, "t": T0 + 30, "classic": True,
                                   "cod": 0x001F00, "heard_by": "Kismet hci1"})
        engine.process()
        self.assertEqual([a.type for a in engine.center.recent], ["skimmer"])
        self.assertIn("Possible skimmer", engine.center.recent[0].title)
        engine.save()
        self.assertTrue((self.root / "loot" / "skimmers" / "suspects.json").exists())
        self.assertEqual(len((self.root / "loot" / "skimmers" / "sightings.csv").read_text().splitlines()), 3)

    def test_a_name_alone_is_listed_but_never_alerts_and_a_false_alarm_is_dropped_for_good(self):
        engine, _said = self.engine()
        engine.incoming.append(Observation("bt", "aa" * 6, "HC-08", -40, T0, 47.6, -122.33, "node 2", classic=False))
        engine.process()
        self.assertEqual(engine.suspects.suspects["aa" * 6].score, 30)
        self.assertEqual(len(engine.center.recent), 0)
        engine.mark("aa" * 6, skimmer.STATUS_FALSE)
        engine.incoming.append(Observation("bt", "aa" * 6, "HC-08", -40, T0 + 60, 47.6, -122.33, "node 2"))
        engine.process()
        self.assertEqual(engine.suspects.suspects["aa" * 6].sightings, 1)

    def test_kismet_alerts_follow_their_own_list(self):
        engine, _said = self.engine(kismet_alerts="DEAUTHFLOOD")
        kismet_tap.alerts.append({"header": "DEAUTHFLOOD", "text": "Deauth flood on Net", "t": T0, "mac": "", "channel": "6"})
        kismet_tap.alerts.append({"header": "PROBECHAN", "text": "other", "t": T0, "mac": "", "channel": "6"})
        engine.process()
        self.assertEqual([(a.type, a.key) for a in engine.center.recent], [("kismet", "DEAUTHFLOOD")])
        self.assertEqual(engine.counts["kismet_off"], 1)

    def test_hunt_readings_move_the_strongest_marker_and_a_report_is_written(self):
        engine, _said = self.engine()
        engine.incoming.append(Observation("bt", "98d331aabbcc", "HC-05", -70, T0, 47.6, -122.33, "node 2", cod=0x001F00))
        engine.process()
        engine.hunt = skimmer.Hunt("98d331aabbcc")
        engine.hunt_readings.append((-66, "hci0"))
        engine.incoming.append(Observation("bt", "98d331aabbcc", "HC-05", -52, T0 + 5, 47.6, -122.33, "node 2"))
        engine.process()
        engine.process()
        self.assertEqual(engine.hunt.readings, 2)
        self.assertEqual(engine.suspects.suspects["98d331aabbcc"].best["rssi"], -52)
        path = engine.export_report("98d331aabbcc", "Example Fuel")
        text = Path(path).read_text(encoding="utf-8")
        self.assertIn("POSSIBLE CARD SKIMMER", text)
        self.assertIn("Example Fuel", text)
        self.assertEqual(Path(path).parent, self.root / "loot" / "skimmers" / "reports")

    def test_a_broken_signatures_file_switches_alerts_off_and_says_so(self):
        bad = self.root / "sig.yaml"
        bad.write_text("signatures:\n  - id: x\n    type: axon\n    mac_prefix: [\"00:25:DF\"]\n", encoding="utf-8")
        said = []
        engine = nearby_engine.NearbyEngine(dict(settings.DEFAULTS), self.root / "loot",
                                            SimpleNamespace(nodes={}, by_number=lambda n: None),
                                            place=lambda t: None, here=lambda: None,
                                            say=lambda text, kind="info": said.append(text), signatures_path=bad)
        self.assertIn("source", engine.signature_error)
        self.assertTrue(any("signatures.yaml was not loaded" in text for text in said))
        engine.incoming.append(Observation("bt", "0025df000001", "x", -50, T0))
        engine.process()
        self.assertEqual(len(engine.center.recent), 0)


class TrackOnDisk(Temp):
    def test_fixes_survive_a_restart_and_place_the_backlog(self):
        first = self.make_service()
        first._load_track()
        mono = time.monotonic()
        wall = time.time()
        first.clock.note(mono - 100, wall - 100)
        for index in range(60):
            first.clock.note(mono - 100 + index, wall - 100 + index)
            fix = Fix(first.clock.track_time(mono - 100 + index), 47.6 + index * 0.0001, -122.33, 20.0, 4.0)
            first.track.add(fix)
            first.gps_out.append(fix)
        first._save_track()
        self.assertEqual(len((first.dir / service.TRACK_FILE).read_text().splitlines()), 60)
        again = self.make_service()
        self.assertFalse(again.track_ready)
        again._load_track()
        self.assertTrue(again.track_ready)
        again.clock.note(5.0, wall + 5)                        # a new run of the game
        where = again.place(wall - 100 + 30.5)                 # a find a node made before the restart
        self.assertAlmostEqual(where[0], 47.60305, places=5)
        self.assertIsNone(again.place(wall - 5000))

    def test_fixes_older_than_a_day_are_dropped_from_the_file(self):
        svc = self.make_service()
        old, new = time.time() - 2 * 86400, time.time() - 60
        (svc.dir / service.TRACK_FILE).write_text("%.3f,47.6,-122.33,0.0,5.0\nnot,a,row\n%.3f,47.7,-122.33,0.0,5.0\n"
                                                  % (old, new), encoding="ascii")
        svc._load_track()
        self.assertEqual(len((svc.dir / service.TRACK_FILE).read_text().splitlines()), 1)
        self.assertEqual(svc.track.latest_time(), round(new, 3))

    def test_a_clock_correction_mid_drive_loses_no_find(self):
        svc = self.make_service()
        svc._load_track()
        ing = ingest.Ingest(SimpleNamespace(gps=None), svc.fleet.delivery, service=svc)
        samples = iter(range(10_000))

        def sample():
            index = next(samples)
            return {"lat": 47.6 + index * 0.0001, "lon": -122.33, "altitude": 10.0, "hdop": 1.0,
                    "observed_at": 1000.0 + index}
        ing.host.gps_sample = sample
        for index in range(10):
            ing.feed_gps(mono_now=1000.0 + index, wall_now=5000.0 + index)
        self.assertFalse(svc.clock_jumped)
        for index in range(10, 20):                            # GPS corrects the clock by an hour
            ing.feed_gps(mono_now=1000.0 + index, wall_now=8600.0 + index)
        self.assertTrue(svc.clock_jumped)
        early, late, lagging = 5004.5, 8615.5, 5011.5          # old clock, new clock, old clock just after
        got = [ingest.decide(svc.track, t, svc.clock.track_time(1019.0), svc.clock) for t in (early, late, lagging)]
        self.assertEqual([action for action, _fix in got], [ingest.PLACE] * 3)
        self.assertAlmostEqual(got[0][1].lat, 47.60045, places=5)
        self.assertAlmostEqual(got[1][1].lat, 47.60155, places=5)
        self.assertAlmostEqual(got[2][1].lat, 47.60115, places=5)
        self.assertEqual(len(svc.gps_out), 20)


class SecondCore(Temp):
    def test_export_on_one_install_and_import_on_another(self):
        one, two = self.make_service("one"), self.make_service("two")
        one.registry.adopt(HW, board="xiao_c5", number=1)
        one.registry.adopt("a0b1c2d3e4f6", board="devkit_c5", number=2)
        two.registry.adopt("0c0c0c0c0c0c", number=1)
        one.export_core("correct horse battery")
        exported = one.export_path()
        self.assertTrue(exported.exists())
        self.assertNotIn(one.fleet.owner_key.hex().encode(), exported.read_bytes())
        with self.assertRaises(RuntimeError):
            two.import_core("correct horse battery")           # the file is not in that install yet
        two.export_path().write_bytes(exported.read_bytes())
        with self.assertRaises(second_core.ExportError):
            two.import_core("wrong passphrase!")
        self.assertEqual(settings.owner_key(two.install_dir / "secrets.conf")[0], two.fleet.owner_key)   # untouched
        two.import_core("correct horse battery")
        self.assertEqual({r.hwid: r.number for r in two.registry.nodes.values()},
                         {"0c0c0c0c0c0c": 1, HW: 2, "a0b1c2d3e4f6": 3})
        secrets = (two.install_dir / "secrets.conf").read_text(encoding="utf-8")
        self.assertIn("BRISTLE_OWNER_KEY=%s" % one.fleet.owner_key.hex(), secrets)
        self.assertIn("BRISTLE_OWNER_KEY_PREVIOUS=%s" % two.fleet.owner_key.hex(), secrets)
        self.assertTrue(any("RESTART THE GAME" in text for text, _kind in two.messages))
        if os.name == "posix":
            self.assertEqual(exported.stat().st_mode & 0o777, 0o600)


class Ports(Temp):
    def ports(self):
        return [link_usb.PortInfo("/dev/ttyACM0", 0x303A, 0x1001, "AA:AA:AA:AA:AA:01"),      # the game's own XIAO
                link_usb.PortInfo("/dev/ttyACM1", 0x303A, 0x1001, "A0:B1:C2:D3:E4:F5"),      # a Bristle board, not adopted
                link_usb.PortInfo("/dev/ttyUSB0", 0x0403, 0x6001, "FT1")]                    # a GPS dongle

    def test_a_board_that_beacons_is_a_bristle_board_and_nothing_else_is(self):
        listened = []
        cache = link_usb.BeaconCache(listen=lambda device: (listened.append(device),
                                                            {"hw": HW} if device == "/dev/ttyACM1" else None)[1])
        check = lambda device: wdg_ports.is_bristle_board(device, ports=self.ports, in_use=lambda d: False, beacons=cache)
        self.assertTrue(check("/dev/ttyACM1"))
        self.assertFalse(check("/dev/ttyACM0"))
        self.assertFalse(check("/dev/ttyUSB0"))                # not an ESP32 chip: never even listened to
        self.assertFalse(check("/dev/ttyACM7"))                # not there
        self.assertEqual(sorted(listened), ["/dev/ttyACM0", "/dev/ttyACM1"])
        self.assertTrue(check("/dev/ttyACM1"))                 # remembered: not listened to again
        self.assertEqual(len(listened), 2)

    def test_a_port_another_program_holds_is_left_alone(self):
        cache = link_usb.BeaconCache(listen=lambda device: self.fail("listened to a port in use"))
        self.assertFalse(wdg_ports.is_bristle_board("/dev/ttyACM1", ports=self.ports, in_use=lambda d: True, beacons=cache))

    def test_the_games_esp32_search_never_takes_an_unadopted_bristle_board(self):
        from watchdogs import serial_manager
        listed = [("/dev/ttyACM1", "new Bristle board", True), ("/dev/ttyACM0", "the game's XIAO", True)]
        with patch.object(serial_manager, "list_usb_serial_devices", return_value=listed), \
                patch.object(wdg_ports, "node_devices", return_value=set()), \
                patch.object(wdg_ports, "is_bristle_board", side_effect=lambda device: device == "/dev/ttyACM1"):
            self.assertEqual(serial_manager.detect_esp32_port(), "/dev/ttyACM0")
        with patch.object(serial_manager, "list_usb_serial_devices", return_value=listed[:1]), \
                patch.object(wdg_ports, "node_devices", return_value=set()), \
                patch.object(wdg_ports, "is_bristle_board", return_value=True):
            self.assertIsNone(serial_manager.detect_esp32_port())   # only the new board is plugged in


class HostAdapter(unittest.TestCase):
    def test_the_wardriving_flag_and_the_public_wigle_queue(self):
        queued = []
        kismet = SimpleNamespace(kismet_active=lambda: True, queue_wigle_upload=queued.append)
        app = SimpleNamespace(state=SimpleNamespace(wardriving_running=False), _plugins=[kismet], _muted=True,
                              loot=SimpleNamespace(session_path="/loot/s1"), serial=SimpleNamespace(device="/dev/ttyACM0"))
        h = host.WdgHost(app)
        h.set_wardriving(True)
        self.assertTrue(app.state.wardriving_running)
        self.assertTrue(h.kismet_active())
        h.queue_wigle()("x.csv")
        self.assertEqual(queued, ["x.csv"])
        self.assertTrue(h.muted())
        self.assertEqual(h.game_port(), "/dev/ttyACM0")
        self.assertEqual(Path(h.wardriving_csv()), Path("/loot/s1") / "wardriving.csv")
        self.assertIsNone(host.WdgHost(SimpleNamespace()).queue_wigle())
        self.assertIsNone(host.WdgHost(SimpleNamespace()).gps_sample())

    def test_the_plugin_marks_the_game_as_wardriving_only_while_nodes_scan(self):
        import plugins.bristle_core as core
        plugin = core.BristleNodes()
        state = SimpleNamespace(wardriving_running=False)
        plugin.host = host.WdgHost(SimpleNamespace(state=state))
        plugin.service = SimpleNamespace(fleet=SimpleNamespace(run_mask=3))
        plugin._mark_wardriving()
        self.assertTrue(state.wardriving_running)
        plugin.service.fleet.run_mask = 0
        plugin._kismet_was = True                              # Kismet still capturing: its flag stays
        plugin._mark_wardriving()
        self.assertTrue(state.wardriving_running)
        plugin.service.fleet.run_mask = 3
        plugin._mark_wardriving()
        plugin.service.fleet.run_mask, plugin._kismet_was = 0, False
        plugin._mark_wardriving()
        self.assertFalse(state.wardriving_running)
        plugin.service = None

    def test_the_kismet_plugin_offers_a_public_queue_function_and_bristle_uses_only_that(self):
        kismet = (ROOT / "plugins" / "kismet_wardrive.py").read_text(encoding="utf-8")
        self.assertIn("def queue_wigle_upload(self, csv_path):", kismet)
        for name in ("bristle_core.py", "_bristle/host.py", "_bristle/service.py"):
            self.assertNotIn("_queue_wigle_upload", (ROOT / "plugins" / name).read_text(encoding="utf-8"), name)

    def test_logic_modules_never_import_the_drawing_library_or_the_game(self):
        thin = {"screen.py", "screen_more.py", "host.py", "wdg_ports.py"}
        for path in sorted((ROOT / "plugins" / "_bristle").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if path.name not in ("screen.py", "screen_more.py", "host.py"):
                self.assertNotIn("import pyxel", text, path.name)
                self.assertNotIn("pyxel.", text, path.name)
            if path.name not in thin and path.name != "flasher.py":
                self.assertNotIn("from watchdogs", text, path.name)
                self.assertIsNone(re.search(r"self\.app\b", text), path.name)


class Screens(Temp):
    def store(self):
        store = skimmer.SuspectStore(self.root / "skimmers")
        o = Observation("bt", "98d331aabbcc", "HC-05 with a very long name indeed", -58, T0, 47.6, -122.33,
                        "Kismet hci1", cod=0x001F00, classic=True)
        store.add(o, 90, ["default module name HC-05/HC-06", "device class Uncategorized",
                          "MAC prefix of Shenzhen Bolutek modules", "strong signal (-58 dBm)"])
        store.add(Observation("bt", "aa" * 6, "", -90, T0, None, None, "node 12"), 20, ["x"])
        return store

    def test_suspect_rows_and_details_fit_the_screen(self):
        store = self.store()
        rows = screen_more.suspect_rows(store, here=(47.61, -122.33), now=T0 + 5000)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertLessEqual(len(row["text"]), screen.MAX_CHARS)
            for line in screen_more.suspect_detail(row, cod_seen=False):
                self.assertLessEqual(len(line), screen.MAX_CHARS)
        self.assertIn("1.1 km", rows[0]["text"])
        self.assertIn("this Kismet does not report Class of Device", "\n".join(screen_more.suspect_detail(rows[1], False)))
        for line in (screen_more.SUSPECT_KEYS_1, screen_more.SUSPECT_KEYS_2, screen_more.HUNT_KEYS,
                     screen_more.ALERT_KEYS, screen_more.SUSPECT_HEADER, screen.KEYS_1, screen.KEYS_2) + skimmer.LIMITS:
            self.assertLessEqual(len(line), screen.MAX_CHARS, line)

    def test_alert_rows_fit_and_show_their_switch(self):
        center = alerts.AlertCenter(switches={"flock": False})
        center.raise_alert(alerts.Alert("axon", "k", "Axon device (body camera, Taser, dock) nearby",
                                        "Body 3 00:25:DF:00:00:01, -60 dBm, heard by node 2 " * 3, t=T0))
        rows = screen_more.alert_switch_rows(center)
        self.assertEqual(len(rows), len(alerts.TYPES))
        self.assertTrue(dict((name, text) for name, text, _c in rows)["flock"].startswith("[OFF]"))
        for _name, text, _colour in rows:
            self.assertLessEqual(len(text), screen.MAX_CHARS)
        for text in screen_more.alert_log_rows(center, now=T0 + 90):
            self.assertLessEqual(len(text), screen.MAX_CHARS)

    def test_the_hunt_view_says_where_and_how_strong(self):
        hunt = skimmer.Hunt("98d331aabbcc")
        estimate = skimmer.estimate([(0, 47.6000, -122.3300, -60)])
        far = screen_more.hunt_view(hunt, estimate, (47.6100, -122.3300), None, mono=10.0)
        self.assertIn("1.1 km, head S", far["guide"])
        self.assertFalse(far["heard"])
        hunt.reading(-50, 10.0, 47.6, -122.33, wall=T0)
        near = screen_more.hunt_view(hunt, estimate, (47.60001, -122.33), 90.0, mono=10.5)
        self.assertTrue(near["heard"])
        self.assertGreater(near["bars"], 15)
        self.assertIn("inside the estimated area", near["guide"])
        self.assertIn("No GPS fix", screen_more.hunt_view(hunt, estimate, None, None, mono=10.5)["guide"])
        self.assertIn("No estimated spot", screen_more.hunt_view(hunt, None, (47.6, -122.33), None, mono=10.5)["guide"])
        moving = screen_more.hunt_view(hunt, estimate, (47.6100, -122.3300), 180.0, mono=10.5)
        self.assertIn("ahead", moving["guide"])
        for view in (far, near, moving):
            self.assertLessEqual(len(view["guide"]), screen.MAX_CHARS)

    def test_dialogs_fit_and_a_passphrase_is_never_shown(self):
        flash = {"type": "flash", "key": "k", "device": "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5A47012345-if00",
                 "first": True, "board": 2, "note": "Image: v1-build4-dev"}
        secret = {"type": "text", "title": "EXPORT: a passphrase", "hint": "", "value": "correct horse", "mask": True}
        for dialog in (flash, dict(flash, first=False), {"type": "key"}, secret):
            for text, _colour in screen_more.dialog_lines(dialog):
                self.assertLessEqual(len(text), 112, text)
        self.assertIn("ERASES THE WHOLE BOARD", screen_more.dialog_lines(flash)[2][0])
        self.assertIn("are kept", screen_more.dialog_lines(dict(flash, first=False))[2][0])
        shown = "\n".join(text for text, _colour in screen_more.dialog_lines(secret))
        self.assertNotIn("correct horse", shown)
        self.assertIn("*" * 13, shown)

    def test_the_title_bar_names_the_links_and_the_limit(self):
        svc = SimpleNamespace(ble=SimpleNamespace(adapter="hci0", limit=5, status={"linked": 5, "waiting": 2, "error": ""}),
                              hub=SimpleNamespace(bridge=object(), peers={"a": 1, "b": 2}))
        text = screen.links_text(svc)
        self.assertIn("BLE hci0: 5/5 linked, LIMIT: 2 waiting", text)
        self.assertIn("bridge: 2 node(s)", text)
        off = SimpleNamespace(ble=None, ble_note="off (ble_link = off)", hub=SimpleNamespace(bridge=None, peers={}))
        self.assertIn("BLE link: off", screen.links_text(off))
        self.assertIn("bridge: none", screen.links_text(off))
        broken = SimpleNamespace(ble=SimpleNamespace(adapter="hci0", limit=5, status={"error": "the Python package 'bleak' is not installed"}))
        self.assertLessEqual(len(screen.links_text(broken)), 86)

    def test_hud_shows_the_latest_alert_for_a_while(self):
        registry = SimpleNamespace(nodes={HW: SimpleNamespace(role=0)})
        fleet = SimpleNamespace(totals=lambda: {"known": 1, "online": 1, "running": True, "backlog": 0, "dropped": 0})
        center = alerts.AlertCenter()
        center.raise_alert(alerts.Alert("skimmer", "k", "Possible skimmer close by (score 90)"))
        svc = SimpleNamespace(fleet=fleet, registry=registry, new_ports={}, nearby=SimpleNamespace(center=center))
        ing = ingest.Ingest(SimpleNamespace(), None)
        lines = screen.hud_lines(svc, ing)
        self.assertEqual(lines[-1][0], "! Possible skimmer close by (score 90)")
        for text, _colour in lines:
            self.assertLessEqual(len(text) * screen.FONT_W + 6, 300)


if __name__ == "__main__":
    unittest.main()
