# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""Unit tests for the nearby alerts, the skimmer logic, the export file and the GPS clock map.

    python -I -m unittest discover -s tests -v      (run from the Bristle folder)
"""
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

from bristle import alerts, nearby, second_core, skimmer  # noqa: E402
from bristle.gpsplace import ClockMap, Fix, GpsTrack  # noqa: E402
from bristle.nearby import Observation  # noqa: E402
from bristle.plan import kismet_heavy_channels  # noqa: E402

SIGNATURES_TEXT = (ROOT / "core" / "bristle" / "signatures.yaml").read_text(encoding="utf-8")
SIGNATURES = nearby.load_signatures(SIGNATURES_TEXT)
KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")


class Ticker:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def ids(observation, types=None):
    return sorted(s.id for s in nearby.match(SIGNATURES, observation, types))


class SignatureFile(unittest.TestCase):
    def test_the_shipped_file_loads_and_every_entry_names_its_source(self):
        self.assertGreater(len(SIGNATURES), 15)
        for sig in SIGNATURES:
            self.assertGreater(len(sig.source), 30, sig.id)
            self.assertIn(sig.type, nearby.TYPES)
        self.assertEqual({s.type for s in SIGNATURES}, set(nearby.TYPES))

    def test_the_built_in_reader_gives_what_pyyaml_gives(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed here")
        simple = nearby.build_signatures(nearby.parse_simple_yaml(SIGNATURES_TEXT))
        full = nearby.build_signatures(yaml.safe_load(SIGNATURES_TEXT))
        self.assertEqual(simple, full)

    def test_an_entry_without_a_source_is_refused(self):
        text = 'signatures:\n  - id: x\n    type: axon\n    mac_prefix: ["00:25:DF"]\n'
        with self.assertRaises(nearby.SignatureError) as caught:
            nearby.build_signatures(nearby.parse_simple_yaml(text))
        self.assertIn("source", str(caught.exception))

    def test_mistakes_are_reported_not_skipped(self):
        for text in ('signatures:\n  - id: x\n    type: nonsense\n    source: "s"\n    name_exact: ["a"]\n',
                     'signatures:\n  - id: x\n    type: axon\n    source: "s"\n',
                     'signatures:\n  - id: x\n    type: axon\n    source: "s"\n    mac_prefix: ["00:25"]\n',
                     'signatures:\n  - id: x\n    type: axon\n    source: "s"\n    mac_prefix: ["00:25:DF"\n',
                     'signatures:\n  - id: x\n    type: axon\n    source: "s"\n    company_id: ["abc"]\n',
                     'signatures:\n  - id: x\n    type: axon\n    source: "s"\n    name_exact: ["a"]\n'
                     '  - id: x\n    type: axon\n    source: "s"\n    name_exact: ["b"]\n'):
            with self.assertRaises(nearby.SignatureError):
                nearby.build_signatures(nearby.parse_simple_yaml(text))

    def test_comments_quotes_and_hex_numbers(self):
        data = nearby.parse_simple_yaml(
            '# top\nversion: 1\nsignatures:\n  - id: "a#b"   # note\n    company_id: [0x004C, 76]\n'
            '    name_exact: ["HC-05", \'x, y\']\n    bristle: true\n')
        entry = data["signatures"][0]
        self.assertEqual(entry["id"], "a#b")
        self.assertEqual(entry["company_id"], [76, 76])
        self.assertEqual(entry["name_exact"], ["HC-05", "x, y"])
        self.assertIs(entry["bristle"], True)


class Matching(unittest.TestCase):
    def test_makers_by_mac_prefix(self):
        self.assertEqual(ids(Observation("bt", "0025df112233")), ["axon-oui"])
        self.assertEqual(ids(Observation("wifi", "0025df112233")), ["axon-oui"])
        self.assertEqual(ids(Observation("wifi", "b41e52aabbcc")), ["flock-oui"])
        self.assertEqual(ids(Observation("wifi", "b41e53aabbcc")), [])

    def test_an_apple_device_is_a_tracker_only_with_the_find_my_type(self):
        self.assertEqual(ids(Observation("bt", "c1" * 6, company_id=0x004C, mfg_type=0x12)), ["tracker-apple-findmy"])
        self.assertEqual(ids(Observation("bt", "c1" * 6, company_id=0x004C, mfg_type=0x10)), [])
        self.assertEqual(ids(Observation("bt", "c1" * 6, company_id=0x004C)), [])

    def test_trackers_by_service_uuid(self):
        self.assertEqual(ids(Observation("bt", "c2" * 6, service_uuids=(0x180F, 0xFEED))), ["tracker-tile-uuid"])
        self.assertEqual(ids(Observation("bt", "c2" * 6, service_uuids=(0xFD5A,))), ["tracker-samsung-smarttag"])
        self.assertEqual(ids(Observation("bt", "c2" * 6, service_uuids=(0xFC81,))), ["axon-service-uuid"])

    def test_kind_is_respected(self):
        self.assertEqual(ids(Observation("wifi", "11" * 6, name="piglet-wardrive")), ["pentest-piglet-setup"])
        self.assertEqual(ids(Observation("bt", "11" * 6, name="piglet-wardrive")), [])
        self.assertEqual(ids(Observation("wifi", "11" * 6, name="HC-05")), [])        # a skimmer name is a Bluetooth name

    def test_other_bristle_boards(self):
        self.assertEqual(ids(Observation("bt", "c3" * 6, bristle_hwid="a0b1c2d3e4f5")), ["rig-bristle-advert"])
        self.assertEqual(ids(Observation("bt", "c3" * 6, name="BRISTLE-7")), ["rig-bristle-name"])

    def test_skimmer_signatures(self):
        got = ids(Observation("bt", "000666aabbcc", name="RNBT-BBCC", cod=0x001F00), ("skimmer",))
        self.assertEqual(got, ["skimmer-class-uncategorized", "skimmer-mac-roving", "skimmer-name-rnbt"])
        self.assertEqual(ids(Observation("bt", "aa" * 6, name="hc-05"), ("skimmer",)), ["skimmer-name-hc"])
        self.assertEqual(ids(Observation("bt", "aa" * 6, name="HC-05 speaker"), ("skimmer",)), [])
        self.assertEqual(ids(Observation("bt", "aa" * 6, name="HC-08"), ("skimmer",)), ["skimmer-name-hc08"])
        self.assertEqual(ids(Observation("bt", "aa" * 6, cod=0x5A020C), ("skimmer",)), [])       # a phone

    def test_class_of_device_major(self):
        self.assertEqual(nearby.cod_major(0x001F00), 31)
        self.assertEqual(nearby.cod_major(0x5A020C), 2)
        self.assertIsNone(nearby.cod_major(None))


class Following(unittest.TestCase):
    def sight(self, detector, minute, lat, lon, mac="ab" * 6):
        return detector.observe(Observation("bt", mac, t=minute * 60.0, lat=lat, lon=lon, name="Tag"))

    def test_three_places_far_apart_over_time_is_reported(self):
        d = nearby.FollowDetector(distance_m=500, window_s=3600, sightings=3, min_span_s=600)
        self.assertIsNone(self.sight(d, 0, 47.6000, -122.3300))
        self.assertIsNone(self.sight(d, 6, 47.6100, -122.3300))       # 1.1 km on
        hit = self.sight(d, 14, 47.6200, -122.3300)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.places, 3)
        self.assertEqual(hit.name, "Tag")
        self.assertGreater(hit.span_m, 2000)
        self.assertAlmostEqual(hit.minutes, 14.0)

    def test_standing_still_or_one_short_stretch_never_counts(self):
        d = nearby.FollowDetector(distance_m=500, sightings=3, min_span_s=600)
        for minute in range(0, 40, 2):
            self.assertIsNone(self.sight(d, minute, 47.6000, -122.3300))
        fast = nearby.FollowDetector(distance_m=500, sightings=3, min_span_s=600)
        self.assertIsNone(self.sight(fast, 0, 47.60, -122.33))
        self.assertIsNone(self.sight(fast, 1, 47.61, -122.33))
        self.assertIsNone(self.sight(fast, 2, 47.62, -122.33))        # three places in two minutes: traffic

    def test_old_places_fall_out_of_the_window(self):
        d = nearby.FollowDetector(distance_m=500, window_s=1800, sightings=3, min_span_s=600)
        self.sight(d, 0, 47.60, -122.33)
        self.sight(d, 5, 47.61, -122.33)
        self.assertIsNone(self.sight(d, 50, 47.62, -122.33))

    def test_no_position_is_ignored_and_memory_is_bounded(self):
        d = nearby.FollowDetector(max_devices=50)
        self.assertIsNone(d.observe(Observation("bt", "ab" * 6, t=1.0)))
        for i in range(200):
            d.observe(Observation("bt", "%012x" % i, t=float(i), lat=47.6, lon=-122.3))
        self.assertLessEqual(len(d._seen), 50)


class AlertRules(unittest.TestCase):
    def test_switched_off_types_and_repeats_are_held(self):
        now = Ticker()
        center = alerts.AlertCenter(switches={"flock": False}, mono=now)
        self.assertFalse(center.raise_alert(alerts.Alert("flock", "b41e52000001", "Flock Safety device")))
        self.assertTrue(center.raise_alert(alerts.Alert("axon", "0025df000001", "Axon device")))
        self.assertFalse(center.raise_alert(alerts.Alert("axon", "0025df000001", "Axon device")))
        now.t += 601
        self.assertTrue(center.raise_alert(alerts.Alert("axon", "0025df000001", "Axon device")))
        self.assertEqual(center.counts["raised"], 2)
        self.assertEqual(len(center.to_log), 2)

    def test_a_burst_of_one_type_is_spaced_out(self):
        now = Ticker()
        center = alerts.AlertCenter(mono=now)
        self.assertTrue(center.raise_alert(alerts.Alert("tracker", "a", "Tile tracker")))
        self.assertFalse(center.raise_alert(alerts.Alert("tracker", "b", "Tile tracker")))
        now.t += 5
        self.assertTrue(center.raise_alert(alerts.Alert("tracker", "b", "Tile tracker")))

    def test_the_hud_line_goes_away_and_a_false_alarm_stays_silent(self):
        now = Ticker()
        center = alerts.AlertCenter(mono=now)
        center.raise_alert(alerts.Alert("skimmer", "aa", "Possible skimmer close by"))
        self.assertEqual(center.hud_line(), "Possible skimmer close by")
        now.t += 60
        self.assertEqual(center.hud_line(), "")
        center.silence("skimmer", "aa")
        now.t += 100000
        self.assertFalse(center.raise_alert(alerts.Alert("skimmer", "aa", "Possible skimmer close by")))

    def test_log_rows_are_safe_csv(self):
        row = alerts.Alert("pentest", "k", "A, B", "line\nbreak", t=1700000000.0, lat=47.5, lon=-122.25,
                           rssi=-60, heard_by="node 2").log_row()
        self.assertEqual(row.count(","), alerts.LOG_HEADER.count(","))
        self.assertTrue(row.startswith("2023-11-14T22:13:20Z,pentest,k,A; B,line break,47.500000,-122.250000,-60"))


class SkimmerScore(unittest.TestCase):
    def points(self, **kw):
        o = Observation("bt", kw.pop("mac", "aa" * 6), **kw)
        return skimmer.score(o, nearby.match(SIGNATURES, o, ("skimmer",)))

    def test_a_name_alone_is_weak(self):
        total, reasons = self.points(name="HC-05", rssi=-85)
        self.assertEqual(total, 20)
        self.assertEqual(len(reasons), 1)
        self.assertGreaterEqual(total, skimmer.LOG_THRESHOLD)

    def test_name_class_and_prefix_together_are_strong(self):
        total, reasons = self.points(mac="000666aabbcc", name="RNBT-BBCC", cod=0x001F00, rssi=-55)
        self.assertEqual(total, 20 + 25 + 35 + 10)
        self.assertEqual(len(reasons), 4)

    def test_nothing_that_matches_scores_nothing(self):
        self.assertEqual(self.points(name="JBL Flip", rssi=-40), (0, []))

    def test_signal_adds_a_little_and_the_score_never_passes_100(self):
        self.assertEqual(self.points(name="HC-06", rssi=-70)[0], 25)
        self.assertEqual(self.points(name="HC-06", rssi=-50)[0], 30)
        self.assertLessEqual(self.points(mac="98d331aabbcc", name="HC-05", cod=0x001F00, rssi=-30)[0], 100)


class Where(unittest.TestCase):
    def test_the_estimate_sits_near_the_loud_sightings(self):
        # a drive past a device at 47.60000,-122.33000: loud next to it, quiet at both ends
        pts = [(0, 47.6000, -122.3320, -92), (1, 47.6000, -122.3310, -84), (2, 47.6000, -122.3301, -61),
               (3, 47.6000, -122.3299, -60), (4, 47.6000, -122.3290, -85), (5, 47.6000, -122.3280, -93)]
        est = skimmer.estimate(pts)
        self.assertLess(nearby.distance_m(est.lat, est.lon, 47.6000, -122.3300), 15)
        self.assertEqual(est.points, 6)
        self.assertEqual(est.directions, 2)

    def test_more_directions_tighten_the_radius(self):
        line = [(0, 47.6000, -122.3303, -62), (1, 47.6000, -122.3297, -62)]
        around = line + [(2, 47.6002, -122.3300, -62), (3, 47.5998, -122.3300, -62)]
        self.assertLess(skimmer.estimate(around).radius_m, skimmer.estimate(line).radius_m)

    def test_one_sighting_gives_a_wide_radius_and_none_gives_nothing(self):
        est = skimmer.estimate([(0, 47.6, -122.33, -80)])
        self.assertGreater(est.radius_m, 5)
        self.assertEqual((est.lat, est.lon), (47.6, -122.33))
        self.assertIsNone(skimmer.estimate([]))
        self.assertIsNone(skimmer.estimate([(0, None, None, -60)]))

    def test_range_guess_grows_as_the_signal_falls(self):
        self.assertLess(skimmer.rough_range_m(-70), skimmer.rough_range_m(-80))
        self.assertLess(skimmer.rough_range_m(-80), skimmer.rough_range_m(-90))
        self.assertEqual(skimmer.rough_range_m(-20), 3.0)
        self.assertEqual(skimmer.rough_range_m(-120), 250.0)


class HuntMaths(unittest.TestCase):
    def test_bearings(self):
        self.assertAlmostEqual(skimmer.bearing_deg(47.60, -122.33, 47.61, -122.33), 0.0, places=3)
        self.assertAlmostEqual(skimmer.bearing_deg(47.60, -122.33, 47.60, -122.32), 90.0, delta=0.1)
        self.assertAlmostEqual(skimmer.bearing_deg(47.60, -122.33, 47.59, -122.33), 180.0, places=3)
        self.assertEqual([skimmer.compass(b) for b in (0, 44, 90, 200, 359)], ["N", "NE", "E", "S", "N"])

    def test_turns_are_said_against_the_way_you_move(self):
        self.assertEqual(skimmer.turn(90, None), "head E")
        self.assertEqual(skimmer.turn(90, 90), "ahead")
        self.assertEqual(skimmer.turn(180, 90), "right")
        self.assertEqual(skimmer.turn(0, 90), "left")
        self.assertEqual(skimmer.turn(270, 90), "behind")

    def test_beeps_speed_up_and_the_meter_fills_as_the_signal_grows(self):
        self.assertEqual(skimmer.beep_interval_s(-100), 1.5)
        self.assertEqual(skimmer.beep_interval_s(-30), 0.12)
        steps = [skimmer.beep_interval_s(r) for r in range(-95, -39, 5)]
        self.assertEqual(steps, sorted(steps, reverse=True))
        self.assertEqual(skimmer.meter_bars(-100), 0)
        self.assertEqual(skimmer.meter_bars(-35), 30)
        self.assertLess(skimmer.meter_bars(-80), skimmer.meter_bars(-60))

    def test_a_hunt_keeps_the_strongest_reading_and_goes_quiet_when_unheard(self):
        hunt = skimmer.Hunt("aa" * 6)
        self.assertFalse(hunt.heard(10.0))
        self.assertTrue(hunt.reading(-80, 10.0, 47.6, -122.33, wall=1.0))
        self.assertFalse(hunt.reading(-85, 11.0, 47.61, -122.33, wall=2.0))
        self.assertTrue(hunt.reading(-62, 12.0, 47.62, -122.33, wall=3.0))
        self.assertEqual(hunt.best, {"rssi": -62, "lat": 47.62, "lon": -122.33, "t": 3.0})
        self.assertLess(-80, hunt.level)                   # smoothed: it follows, it does not jump
        self.assertLess(hunt.level, -62)
        self.assertTrue(hunt.beep_due(13.0, 11.0))
        self.assertFalse(hunt.beep_due(13.0, 12.99))
        self.assertFalse(hunt.heard(30.0))
        self.assertFalse(hunt.beep_due(30.0, 0.0))


class Suspects(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.tmp.name) / "skimmers"
        self.store = skimmer.SuspectStore(self.folder)

    def tearDown(self):
        self.tmp.cleanup()

    def see(self, t=1700000000.0, rssi=-70, lat=47.6, lon=-122.33, name="HC-05", cod=0x001F00, by="Kismet hci1"):
        o = Observation("bt", "98d331aabbcc", name=name, rssi=rssi, t=t, lat=lat, lon=lon, heard_by=by, cod=cod,
                        classic=True)
        points, reasons = skimmer.score(o, nearby.match(SIGNATURES, o, ("skimmer",)))
        return self.store.add(o, points, reasons)

    def test_sightings_are_logged_and_the_list_survives_a_restart(self):
        self.see()
        self.see(t=1700000060.0, rssi=-58, by="node 2")
        self.store.save()
        rows = (self.folder / "sightings.csv").read_text().splitlines()
        self.assertEqual(rows[0], skimmer.SIGHTINGS_HEADER)
        self.assertEqual(rows[1], "2023-11-14T22:13:20Z,47.600000,-122.330000,-70,Kismet hci1,HC-05,"
                                  "98:D3:31:AA:BB:CC,0x001F00,85")
        self.assertEqual(len(rows), 3)
        again = skimmer.SuspectStore(self.folder)
        s = again.suspects["98d331aabbcc"]
        self.assertEqual((s.sightings, s.score, s.heard_by), (2, 90, ["Kismet hci1", "node 2"]))
        self.assertEqual(len(s.points), 2)
        self.see(t=1700000120.0)
        self.store.save()
        self.assertEqual(len((self.folder / "sightings.csv").read_text().splitlines()), 4)

    def test_a_false_alarm_goes_on_the_ignore_list_and_can_come_off(self):
        self.see()
        self.store.mark("98d331aabbcc", skimmer.STATUS_FALSE)
        self.assertTrue(self.store.is_ignored("98d331aabbcc"))
        self.store.save()
        self.assertTrue(skimmer.SuspectStore(self.folder).is_ignored("98d331aabbcc"))
        self.store.mark("98d331aabbcc", skimmer.STATUS_REPORTED, notes="told the manager")
        self.assertFalse(self.store.is_ignored("98d331aabbcc"))

    def test_rows_put_the_highest_score_first_and_false_alarms_last(self):
        self.see()
        weak = Observation("bt", "aa" * 6, name="HC-06", rssi=-90, t=1700000000.0, lat=47.61, lon=-122.33)
        self.store.add(weak, 20, ["default module name HC-05/HC-06"])
        rows = self.store.rows(here=(47.6, -122.33))
        self.assertEqual([r["suspect"].mac for r in rows], ["98d331aabbcc", "aa" * 6])
        self.assertLess(rows[0]["distance_m"], 5)
        self.assertGreater(rows[1]["distance_m"], 1000)
        self.store.mark("98d331aabbcc", skimmer.STATUS_FALSE)
        self.assertEqual(self.store.rows()[0]["suspect"].mac, "aa" * 6)

    def test_the_point_list_stays_bounded_and_keeps_the_loud_ones(self):
        self.see(rssi=-40, t=1.0)
        for i in range(skimmer.MAX_POINTS + 200):
            self.see(rssi=-90, t=10.0 + i)
        points = self.store.suspects["98d331aabbcc"].points
        self.assertLessEqual(len(points), skimmer.MAX_POINTS)
        self.assertIn(-40, [p[3] for p in points])

    def test_the_report_says_possible_and_never_safe(self):
        self.see()
        self.see(t=1700000060.0, rssi=-58, lat=47.6001)
        s = self.store.suspects["98d331aabbcc"]
        self.store.hunt_best(s.mac, {"rssi": -44, "lat": 47.60005, "lon": -122.33, "t": 1700000300.0})
        s.notes = "pump 4, nearest the road"
        text = skimmer.report_text(s, place="Example Fuel, 1 Main St", made_at=1700000400.0)
        for needed in ("POSSIBLE CARD SKIMMER", "98:D3:31:AA:BB:CC", "HC-05", "0x001F00 (Uncategorized)",
                       "Example Fuel, 1 Main St", "pump 4, nearest the road", "-44 dBm", "Estimated position: 47.6",
                       "never connected to, paired with", "not proof"):
            self.assertIn(needed, text)
        self.assertNotIn("is safe", text.lower())
        self.assertNotIn("confirmed skimmer", text.lower())


class Export(unittest.TestCase):
    MEMORY = {"version": 1, "nodes": [{"hwid": "a0b1c2d3e4f5", "number": 1, "board": "xiao_c5", "usb_key": "usb:x"},
                                      {"hwid": "a0b1c2d3e4f6", "number": 2, "board": "devkit_c5"}],
              "blocked": {"0102030405aa": "a Piglet"}, "not_nodes": {}}

    def bundle(self, passphrase="correct horse battery"):
        return second_core.export_bundle(KEY, self.MEMORY, passphrase, made_at=1700000000, n=2 ** 12)

    def test_round_trip(self):
        data = self.bundle()
        self.assertNotIn(KEY.hex().encode(), data)         # the key is never in the file in the clear
        self.assertNotIn(b"a0b1c2d3e4f5", data)
        key, memory, made = second_core.import_bundle(data, "correct horse battery")
        self.assertEqual((key, memory, made), (KEY, self.MEMORY, 1700000000))

    def test_two_exports_of_the_same_thing_differ(self):
        self.assertNotEqual(self.bundle(), self.bundle())

    def test_wrong_passphrase_and_any_change_to_the_file_fail(self):
        data = self.bundle()
        with self.assertRaises(second_core.ExportError) as caught:
            second_core.import_bundle(data, "correct horse batterx")
        self.assertIn("wrong passphrase", str(caught.exception))
        outer = json.loads(data)
        for field in ("data", "nonce", "salt"):
            changed = dict(outer)
            raw = bytearray(second_core.base64.b64decode(changed[field]))
            raw[3] ^= 1
            changed[field] = second_core.base64.b64encode(bytes(raw)).decode()
            with self.assertRaises(second_core.ExportError):
                second_core.import_bundle(json.dumps(changed).encode(), "correct horse battery")
        weaker = dict(outer, n=2 ** 10)                    # the header is bound into the seal
        with self.assertRaises(second_core.ExportError):
            second_core.import_bundle(json.dumps(weaker).encode(), "correct horse battery")

    def test_other_files_and_short_passphrases_are_refused(self):
        for junk in (b"", b"not json", b'{"format": "something else"}', b'{"format": "bristle-core-export"}'):
            with self.assertRaises(second_core.ExportError):
                second_core.import_bundle(junk, "correct horse battery")
        with self.assertRaises(second_core.ExportError):
            second_core.export_bundle(KEY, self.MEMORY, "short")
        with self.assertRaises(second_core.ExportError):
            second_core.export_bundle(b"", self.MEMORY, "correct horse battery")
        huge = json.loads(self.bundle())
        huge["n"] = 2 ** 24
        with self.assertRaises(second_core.ExportError):
            second_core.import_bundle(json.dumps(huge).encode(), "correct horse battery")

    def test_merging_keeps_what_is_here_and_moves_clashing_numbers(self):
        mine = {"version": 1, "nodes": [{"hwid": "0a0a0a0a0a0a", "number": 1, "usb_key": "usb:mine"},
                                        {"hwid": "a0b1c2d3e4f6", "number": 7, "name": "kept"}],
                "blocked": {}, "not_nodes": {"usb:janos": "JanOS board"}}
        merged, added, moved = second_core.merge_node_memory(mine, self.MEMORY)
        by_hw = {n["hwid"]: n for n in merged["nodes"]}
        self.assertEqual((added, moved), (1, 1))
        self.assertEqual(by_hw["0a0a0a0a0a0a"]["number"], 1)
        self.assertEqual(by_hw["a0b1c2d3e4f6"], {"hwid": "a0b1c2d3e4f6", "number": 7, "name": "kept"})
        self.assertEqual(by_hw["a0b1c2d3e4f5"]["number"], 2)        # 1 was taken here
        self.assertEqual(by_hw["a0b1c2d3e4f5"]["usb_key"], "")
        self.assertEqual(merged["blocked"], {"0102030405aa": "a Piglet"})
        self.assertEqual(merged["not_nodes"], {"usb:janos": "JanOS board"})
        self.assertEqual(len({n["number"] for n in merged["nodes"]}), 3)


class Clock(unittest.TestCase):
    def test_a_steady_clock_maps_straight_through(self):
        clock = ClockMap()
        clock.note(50.0, 1700000000.0)
        clock.note(60.0, 1700000010.0)
        self.assertAlmostEqual(clock.track_time(60.0), 1700000010.0)
        self.assertAlmostEqual(clock.to_track(1700000005.0), 1700000005.0)

    def test_finds_stamped_before_a_jump_still_meet_their_fixes(self):
        clock = ClockMap()
        track = GpsTrack()
        clock.note(100.0, 5000.0)                           # the uConsole's clock is an hour slow
        for i in range(10):
            clock.note(100.0 + i, 5000.0 + i)
            track.add(Fix(clock.track_time(100.0 + i), 47.6 + i * 0.0001, -122.33))
        self.assertTrue(clock.note(110.0, 8610.0))          # GPS sets it right: +3600 s
        for i in range(10, 20):
            clock.note(100.0 + i, 8600.0 + i)
            track.add(Fix(clock.track_time(100.0 + i), 47.6 + i * 0.0001, -122.33))
        self.assertEqual(clock.jumps, 1)
        before = track.place(clock.to_track(5004.5))        # stamped with the old clock
        after = track.place(clock.to_track(8615.5))         # stamped with the new clock
        self.assertAlmostEqual(before.lat, 47.60045, places=5)
        self.assertAlmostEqual(after.lat, 47.60155, places=5)
        # the node had not heard the new time yet: still the old clock, a moment after the jump
        late = track.place(clock.to_track(5011.5))
        self.assertAlmostEqual(late.lat, 47.60115, places=5)

    def test_a_clock_set_back_does_not_mix_the_two_stretches(self):
        clock = ClockMap()
        clock.note(100.0, 9000.0)
        clock.note(200.0, 9100.0)
        self.assertTrue(clock.note(201.0, 9001.0))          # set back 100 s
        clock.note(260.0, 9060.0)
        # 9050 was reached twice. The later stretch wins: that is what a node stamps now.
        self.assertAlmostEqual(clock.to_track(9050.0), clock.track_time(250.0))
        self.assertAlmostEqual(clock.to_track(9095.0), clock.track_time(195.0))

    def test_a_saved_track_still_places_finds_after_a_restart(self):
        first = ClockMap()
        first.note(100.0, 5000.0)
        first.note(400.0, 5300.0)
        saved = first.snapshot()
        later = ClockMap(history=saved)
        later.note(30.0, 6000.0)                            # a new run: the monotonic clock started over
        self.assertAlmostEqual(later.to_track(5150.0), 5150.0)
        self.assertAlmostEqual(later.to_track(6000.0), 6000.0)
        self.assertIsNone(later.to_track(5600.0))           # the game was off then: no track

    def test_a_restart_with_the_clock_behind_keeps_the_runs_apart(self):
        first = ClockMap()
        first.note(100.0, 5000.0)
        first.note(400.0, 5300.0)
        later = ClockMap(history=first.snapshot())
        later.note(10.0, 5200.0)                            # the clock lost time while the game was off
        now_track = later.track_time(10.0)
        self.assertGreater(now_track, 5300.0)
        self.assertAlmostEqual(later.to_track(5200.0), now_track)

    def test_times_nobody_knows_give_none(self):
        clock = ClockMap()
        clock.note(100.0, 5000.0)
        self.assertIsNone(clock.to_track(100.0))
        self.assertIsNone(clock.to_track(9000.0))


class KismetChannels(unittest.TestCase):
    def test_an_adapter_hopping_everything_makes_nothing_heavy(self):
        sources = [{"kind": "wifi", "running": True, "hop_channels": [str(c) for c in range(1, 12)] +
                    ["36", "40HT40-", "44", "48", "149", "153", "157", "161"]}]
        heavy, note = kismet_heavy_channels(sources)
        self.assertEqual(heavy, set())
        self.assertIn("evenly", note)

    def test_an_adapter_on_a_few_channels_makes_them_heavy(self):
        sources = [{"kind": "wifi", "running": True, "hop_channels": ["1", "6HT20", "11"]},
                   {"kind": "wifi", "running": True, "hop_channels": [str(c) for c in range(36, 166, 4)]},
                   {"kind": "bt", "running": True, "hop_channels": []},
                   {"kind": "wifi", "running": False, "hop_channels": ["3"]}]
        heavy, note = kismet_heavy_channels(sources)
        self.assertEqual(heavy, {1, 6, 11})
        self.assertIn("1, 6, 11", note)

    def test_a_card_locked_to_one_channel_makes_that_channel_heavy(self):
        everything = [str(c) for c in range(1, 14)] + [str(c) for c in range(36, 166, 4)]
        hopping = {"kind": "wifi", "running": True, "hop_channels": everything}
        # the core locked it: the lock wins over the hop list Kismet still reports for the card
        heavy, note = kismet_heavy_channels([hopping, {"kind": "wifi", "running": True, "lock": "6",
                                                       "hop_channels": everything}])
        self.assertEqual(heavy, {6})
        self.assertIn("6", note)
        # Kismet itself says the card is not hopping and where it is
        heavy, _note = kismet_heavy_channels([hopping, {"kind": "wifi", "running": True, "hopping": False,
                                                        "channel": "149", "hop_channels": everything}])
        self.assertEqual(heavy, {149})
        # a hopping card that reports the channel it is on at this moment is still a hopping card
        heavy, _note = kismet_heavy_channels([dict(hopping, hopping=True, channel="3")])
        self.assertEqual(heavy, set())
        # a locked card that is not capturing covers nothing
        self.assertEqual(kismet_heavy_channels([hopping, {"kind": "wifi", "running": False, "lock": "6"}])[0], set())

    def test_no_wifi_capture_is_said_plainly(self):
        self.assertEqual(kismet_heavy_channels([])[0], set())
        self.assertIn("not capturing", kismet_heavy_channels([{"kind": "bt", "running": True}])[1])


if __name__ == "__main__":
    unittest.main()
