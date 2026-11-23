"""[merge-v3] gpsd -> GpsManager.wardrive_sample() -> trail / auto map / policy.

On the WDG-Bristle rig GPS only ever arrives through gpsd (JSON TPV/SKY
reports), never as NMEA. upstream 0.9.20/0.9.21's GPS trail, Auto Map + Turf and
wardrive GPS policy accept nothing but GpsManager.wardrive_sample(), which
stock upstream fills from the NMEA parsers only. These tests drive the real gpsd
reader (a real socket pair, fake monotonic time) and the real consumers.
No gpsd, receiver, pyxel or network is used.
"""

import ast
import importlib
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    with patch.dict(sys.modules, {"serial": SimpleNamespace()}):
        gps_module = importlib.import_module("watchdogs.gps_manager")
else:
    gps_module = importlib.import_module("watchdogs.gps_manager")

from watchdogs.wardrive_gps import WardriveGpsPolicy
from watchdogs.wardrive_track import WardriveTrack

SOURCE = Path(__file__).resolve().parents[1] / "watchdogs" / "app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
GAME = next(node for node in TREE.body
            if isinstance(node, ast.ClassDef) and node.name == "WatchDogsGame")


def app_methods(*names, **env):
    """Compile the real app.py methods into a bare class (upstream's test idiom)."""
    body = [node for node in GAME.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in body} == set(names), names
    module = ast.Module(body=[ast.ClassDef(name="Harness", bases=[], keywords=[],
                                           decorator_list=[], body=body)], type_ignores=[])
    namespace = dict(__name__="watchdogs.v3_method_test", __package__="watchdogs", **env)
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), "exec"), namespace)
    return namespace["Harness"]


def tpv(lat=12.5, lon=34.5, mode=3, stamp="2026-11-21T12:00:00.000Z", **extra):
    report = {"class": "TPV", "device": "/dev/ttyACM0", "mode": mode}
    if stamp is not None:
        report["time"] = stamp
    if lat is not None:
        report["lat"] = lat
    if lon is not None:
        report["lon"] = lon
    report.update(extra)
    return report


def sky(used=7, visible=11, hdop=0.9):
    return {"class": "SKY", "hdop": hdop,
            "satellites": [{"PRN": n, "used": n < used} for n in range(visible)]}


def setUpModule():
    # [merge-v3] Every manager in this file is handed its gpsd link by the
    # test itself. The machine's own gpsd must play no part: on the uConsole
    # (gpsd running, /etc/default/gpsd naming /dev/ttyAMA0) these tests would
    # otherwise open antenna-status connections to the real gpsd, and
    # GpsdBeforeAioUartTests would take the "gpsd owns the UART" path.
    import gps_env
    gps_env.no_gpsd_for_module()


class GpsdSampleTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.gps = gps_module.GpsManager(device="never-open-test-device")
        self.gps._available = True
        self.gps._using_gpsd = True
        clock = patch.object(gps_module.time, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def apply(self, *reports):
        for report in reports:
            self.gps._apply_gpsd_report(report)

    def sample(self):
        return self.gps.wardrive_sample(self.now)

    def test_tpv_fix_becomes_a_wardrive_sample_with_the_nmea_shape(self):
        self.apply(sky(), tpv(altMSL=42.5, speed=10.0))
        value = self.sample()
        # exactly the keys an NMEA GGA sample has (upstream test_wardrive_gps_sample)
        self.assertEqual(set(value), {"lat", "lon", "altitude", "quality", "sats",
                                     "hdop", "observed_at", "utc_seconds", "sentence"})
        self.assertEqual((value["lat"], value["lon"], value["altitude"]), (12.5, 34.5, 42.5))
        self.assertEqual((value["sats"], value["hdop"], value["quality"]), (7, 0.9, 1))
        self.assertEqual(value["observed_at"], 100.0)   # monotonic RECEIVE time
        self.assertEqual(value["utc_seconds"], 12 * 3600.0)
        self.assertEqual(value["sentence"], "TPV")
        self.assertTrue(self.gps.fix.valid)
        self.assertAlmostEqual(self.gps.fix.speed_knots, 19.438445)

    def test_two_d_fix_is_accepted_like_gga_quality_one(self):
        self.apply(tpv(mode=2))
        self.assertIsNotNone(self.sample())
        self.assertIsNone(self.gps.radio_fix(self.now))  # radio needs 3D + sats + hdop

    def test_sample_expires_after_two_seconds_without_a_new_report(self):
        self.apply(tpv())
        self.now += 1.99
        self.assertIsNotNone(self.sample())
        self.now += 0.02
        self.assertIsNone(self.sample())

    def test_polling_never_refreshes_the_sample(self):
        self.apply(tpv())
        for _ in range(5):
            self.now += 0.3
            self.sample()
        self.assertEqual(self.gps._wardrive_position["observed_at"], 100.0)

    def test_replayed_epoch_cannot_make_an_old_fix_fresh(self):
        self.apply(tpv())
        self.now += 1.5
        self.apply(tpv())          # same "time", same place: nothing new measured
        self.assertEqual(self.sample()["observed_at"], 100.0)
        self.now += 1.0            # 2.5 s after the only real measurement
        self.apply(tpv())
        self.assertIsNone(self.sample())

    def test_new_epoch_refreshes(self):
        self.apply(tpv())
        self.now += 1.0
        self.apply(tpv(lat=12.6, stamp="2026-11-21T12:00:01.000Z"))
        value = self.sample()
        self.assertEqual((value["lat"], value["observed_at"], value["utc_seconds"]),
                         (12.6, 101.0, 12 * 3600.0 + 1))

    def test_same_epoch_with_a_different_position_invalidates(self):
        self.apply(sky(), tpv())
        self.apply(tpv(lat=13.0))
        self.assertIsNone(self.sample())
        self.assertFalse(self.gps.fix.valid)
        self.assertIsNone(self.gps.radio_fix(self.now))

    def test_losing_the_fix_invalidates_at_once(self):
        self.apply(sky(), tpv())
        for mode in (1, 0):
            with self.subTest(mode=mode):
                self.apply(tpv(stamp="2026-11-21T12:00:0%d.000Z" % (5 + mode)))
                self.assertIsNotNone(self.sample())
                self.apply(tpv(mode=mode, lat=None, lon=None))
                self.assertIsNone(self.sample())
                self.assertFalse(self.gps.fix.valid)
                self.assertEqual(self.gps.fix.fix_quality, 0)
                self.assertIsNone(self.gps.radio_fix(self.now))

    def test_partial_tpv_without_position_neither_refreshes_nor_invalidates(self):
        self.apply(tpv())
        self.now += 0.5
        self.apply(tpv(lat=None, lon=None, stamp="2026-11-21T12:00:00.500Z"))
        self.assertEqual(self.sample()["observed_at"], 100.0)
        self.assertTrue(self.gps.fix.valid)

    def test_zero_zero_and_garbage_positions_are_never_samples(self):
        bad = [dict(lat=0.0, lon=0.0), dict(lat=91.0), dict(lon=181.0), dict(lat=float("nan")),
               dict(lat=float("inf")), dict(lat="12.5"), dict(lat=True)]
        for index, change in enumerate(bad):
            with self.subTest(change=change):
                self.apply(tpv(stamp="2026-11-21T12:01:%02d.000Z" % index))
                self.assertIsNotNone(self.sample())
                self.apply(tpv(stamp="2026-11-21T12:02:%02d.000Z" % index, **change))
                self.assertIsNone(self.sample())

    def test_fix_without_utc_time_places_the_player_but_is_not_a_sample(self):
        self.apply(tpv(stamp=None))
        self.assertTrue(self.gps.fix.valid)
        self.assertEqual(self.gps.fix.latitude, 12.5)
        self.assertIsNone(self.sample())

    def test_non_dict_and_unknown_reports_are_ignored(self):
        self.apply(tpv())
        self.apply([], "TPV", None, {"class": "VERSION"}, {"class": "DEVICES"})
        self.assertIsNotNone(self.sample())

    def test_sky_counts_with_and_without_the_satellite_list(self):
        self.apply(sky(used=5, visible=9, hdop=1.4))
        self.assertEqual((self.gps.fix.satellites, self.gps.fix.satellites_visible,
                          self.gps.fix.hdop), (5, 9, 1.4))
        self.apply({"class": "SKY", "nSat": 12, "uSat": 8, "hdop": 0.7})  # gpsd >= 3.22
        self.assertEqual((self.gps.fix.satellites, self.gps.fix.satellites_visible,
                          self.gps.fix.hdop), (8, 12, 0.7))
        self.apply({"class": "SKY", "hdop": "bad", "uSat": True})
        self.assertEqual((self.gps.fix.satellites, self.gps.fix.hdop), (8, 0.7))

    def test_radio_fix_keeps_its_stricter_gate(self):
        self.apply(sky(used=3), tpv())                      # too few satellites
        self.assertIsNone(self.gps.radio_fix(self.now))
        self.apply(sky(used=6, hdop=4.0), tpv(stamp="2026-11-21T12:00:01Z"))   # poor geometry
        self.assertIsNone(self.gps.radio_fix(self.now))
        self.apply(sky(used=6, hdop=1.0), tpv(stamp="2026-11-21T12:00:02Z"))
        fix = self.gps.radio_fix(self.now)
        self.assertEqual((fix["lat"], fix["lon"], fix["sats"]), (12.5, 34.5, 6))

    def test_close_forgets_the_gpsd_epoch_and_the_sample(self):
        self.apply(tpv())
        self.gps.close()
        self.assertIsNone(self.gps._gpsd_last_epoch)
        self.gps._available = True
        self.assertIsNone(self.sample())
        self.apply(tpv())          # same time after a reconnect is a new observation
        self.assertIsNotNone(self.sample())


class GpsdBeforeAioUartTests(unittest.TestCase):
    """upstream 0.9.20's AIO shortcut opens /dev/ttyAMA* directly; gpsd must still
    be asked first, or the game and gpsd read the same UART."""

    def game(self, gps, aio=True):
        game = app_methods("_setup_gps_connection")()
        game._aio_available, game.gps = aio, gps
        return game

    def manager(self):
        gps = gps_module.GpsManager(device="/dev/ttyAMA0")
        gps._try_open = Mock(return_value=True)
        gps.setup = Mock(side_effect=AssertionError("USB GPS probing must not run"))
        return gps

    def test_running_gpsd_is_used_instead_of_the_aio_uart(self):
        gps = self.manager()
        gps._try_open_gpsd = Mock(return_value=True)
        with patch.object(gps_module.GpsManager, "_user_configured", False):
            self.assertTrue(self.game(gps)._setup_gps_connection())
        gps._try_open_gpsd.assert_called_once_with()
        gps._try_open.assert_not_called()

    def test_without_gpsd_upstreams_uart_open_is_unchanged(self):
        gps = self.manager()
        gps._try_open_gpsd = Mock(return_value=False)
        with patch.object(gps_module.GpsManager, "_user_configured", False):
            self.assertTrue(self.game(gps)._setup_gps_connection())
        gps._try_open.assert_called_once_with("/dev/ttyAMA0")

    def test_a_pinned_gps_device_skips_gpsd(self):
        gps = self.manager()
        gps._try_open_gpsd = Mock(return_value=True)
        with patch.object(gps_module.GpsManager, "_user_configured", True):
            self.game(gps)._setup_gps_connection()
        gps._try_open_gpsd.assert_not_called()
        gps._try_open.assert_called_once_with("/dev/ttyAMA0")

    def test_gps_objects_without_the_hook_keep_upstreams_behaviour(self):
        gps = SimpleNamespace(device="/dev/ttyAMA0", _try_open=Mock(return_value=True))
        self.assertTrue(self.game(gps)._setup_gps_connection())
        gps._try_open.assert_called_once_with("/dev/ttyAMA0")

    def test_non_aio_rigs_still_go_through_setup_which_tries_gpsd_first(self):
        gps = gps_module.GpsManager(device="/dev/ttyAMA0")
        order = []
        gps._try_open_gpsd = lambda: order.append("gpsd") or True
        gps._probe_nmea = lambda *_a: order.append("serial") or False
        with patch.object(gps_module.GpsManager, "_user_configured", False):
            self.assertTrue(self.game(gps, aio=False)._setup_gps_connection())
        self.assertEqual(order, ["gpsd"])


class GpsdSocketPathTests(unittest.TestCase):
    """The same thing through the real non-blocking socket reader."""

    def setUp(self):
        self.now = 500.0
        clock = patch.object(gps_module.time, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.server, client = socket.socketpair()
        self.addCleanup(self.server.close)
        client.setblocking(False)
        self.gps = gps_module.GpsManager(device="never-open-test-device")
        self.gps._gpsd_sock, self.gps._using_gpsd, self.gps._available = client, True, True
        self.addCleanup(self.gps.close)

    def send(self, *reports, raw=b""):
        self.server.sendall(b"".join(json.dumps(r).encode() + b"\n" for r in reports) + raw)

    def test_read_available_returns_no_nmea_and_feeds_the_sample(self):
        self.send({"class": "VERSION", "release": "3.25"}, sky(), tpv())
        self.assertEqual(self.gps.read_available(), [])
        value = self.gps.wardrive_sample(self.now)
        self.assertEqual((value["lat"], value["lon"], value["sentence"]), (12.5, 34.5, "TPV"))

    def test_split_and_malformed_lines_do_not_lose_the_next_report(self):
        payload = json.dumps(tpv()).encode() + b"\n"
        self.server.sendall(b"{not json}\n" + payload[:20])
        self.gps.read_available()
        self.assertIsNone(self.gps.wardrive_sample(self.now))
        self.server.sendall(payload[20:])
        self.gps.read_available()
        self.assertIsNotNone(self.gps.wardrive_sample(self.now))

    def test_gpsd_closing_the_connection_disables_gps_and_the_sample(self):
        self.send(tpv())
        self.gps.read_available()
        self.server.close()
        self.gps.read_available()
        self.assertFalse(self.gps.available)
        self.assertIsNone(self.gps.wardrive_sample(self.now))


class GpsdConsumersTests(unittest.TestCase):
    """What upstream 0.9.20/0.9.21 does with the sample."""

    def setUp(self):
        self.now = 900.0
        clock = patch.object(gps_module.time, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.gps = gps_module.GpsManager(device="never-open-test-device")
        self.gps._available = self.gps._using_gpsd = True

    def fix(self, lat, second):
        self.gps._apply_gpsd_report(sky())
        self.gps._apply_gpsd_report(tpv(lat=lat, stamp="2026-11-21T12:00:%02d.000Z" % second))
        return self.gps.wardrive_sample(self.now)

    def test_wardrive_policy_accepts_the_gpsd_sample_as_first_fix(self):
        policy = WardriveGpsPolicy(clock=lambda: self.now)
        self.assertTrue(policy.feed_sample(self.fix(12.5, 0)))
        snapshot = policy.first_fix_snapshot
        self.assertEqual((snapshot["lat"], snapshot["lon"], snapshot["sats"]), (12.5, 34.5, 7))
        self.assertEqual(policy.position_for(self.now)["source"], "measured")

    def test_trail_draws_from_gpsd_samples(self):
        track = WardriveTrack()
        for second, lat in enumerate((12.5, 12.5001, 12.5002)):
            self.assertTrue(track.add_sample(self.fix(lat, second), now=self.now))
            self.now += 1.0
        self.assertEqual([point[0] for point in track.iter_points()], [12.5, 12.5001, 12.5002])
        self.assertEqual([point[3] for point in track.iter_points()], [True, False, False])

    def test_trail_still_rejects_what_is_not_a_measured_fix(self):
        track = WardriveTrack()
        sample = self.fix(12.5, 0)
        for change in (dict(sentence="GSV"), dict(sentence=None), dict(cached=True),
                       dict(backfill=True), dict(source="cache")):
            with self.subTest(change=change):
                self.assertFalse(track.add_sample(dict(sample, **change), now=self.now))
        self.assertEqual(track.point_count, 0)

    def test_auto_map_position_comes_from_the_gpsd_sample(self):
        Harness = app_methods("_auto_map_position")
        game = Harness()
        game.gps, game._gps_enabled = self.gps, True
        self.assertIsNone(game._auto_map_position())
        self.fix(12.5, 0)
        self.assertEqual(game._auto_map_position(), (12.5, 34.5))
        self.now += 2.5                       # stale: no new area request
        self.assertIsNone(game._auto_map_position())
        self.fix(12.7, 9)
        game._gps_enabled = False             # GPS switched off in-game
        self.assertIsNone(game._auto_map_position())

    def poll_game(self, *, kismet, solo=False):
        Harness = app_methods("_poll_gps", time=gps_module.time)
        game = Harness()
        game.gps = self.gps
        game.gps.read_available = lambda: []
        game._gps_enabled = True
        game._solo_track = WardriveTrack()
        game._solo_recorder = None
        game._solo = SimpleNamespace(confirmed_wifi=solo, confirmed_ble=False)
        game._plugins = [SimpleNamespace(),
                         SimpleNamespace(kismet_active=lambda: kismet)]
        game._poll_radio_profiles = Mock()
        game.player_lat, game.player_lon, game.gps_fix, game._manual_move = 0.0, 0.0, False, True
        return game

    def test_trail_records_while_kismet_wardrives_without_solo(self):
        game = self.poll_game(kismet=True)
        for second, lat in enumerate((12.5, 12.6)):
            self.fix(lat, second)
            game._poll_gps()
            self.now += 1.0
        self.assertEqual([point[0] for point in game._solo_track.iter_points()], [12.5, 12.6])
        # the player marker keeps upstream's legacy (non-SOLO) placement path
        self.assertTrue(game.gps_fix)
        self.assertEqual((game.player_lat, game.player_lon), (12.6, 34.5))

    def test_trail_stays_empty_when_nothing_is_wardriving(self):
        game = self.poll_game(kismet=False)
        self.fix(12.5, 0)
        game._poll_gps()
        self.assertEqual(game._solo_track.point_count, 0)
        self.assertTrue(game.gps_fix)

    def test_a_broken_kismet_plugin_cannot_break_the_gps_poll(self):
        game = self.poll_game(kismet=True)
        game._plugins = [SimpleNamespace(kismet_active=Mock(side_effect=RuntimeError("boom")))]
        self.fix(12.5, 0)
        game._poll_gps()
        self.assertEqual(game._solo_track.point_count, 0)
        self.assertTrue(game.gps_fix)


if __name__ == "__main__":
    unittest.main()
