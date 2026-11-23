"""[merge-v3] GPS comes up by itself.

* gpsd that is not running when the game starts (or when GPS is switched on
  in-game) is looked for every 5 s, exactly like after a drop, and GPS turns
  on when it answers -- no toggling.
* A Kismet wardrive that starts with GPS switched off switches it on
  (WDGoNG 0.10.0's ensure_gps).

"gpsd" is a listening socket on 127.0.0.1 inside the test. No real gpsd,
receiver, serial port, Kismet or pyxel window is used, and the serial-port
probing of GpsManager.setup() is replaced so no real port is ever opened.
"""

import logging
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import plugins.kismet_wardrive as kismet

import test_v3_background_io as bg

gps_module = bg.gps_module
FRAME_LIMIT = bg.FRAME_LIMIT


class LaunchCase(unittest.TestCase):
    """A rig whose only GPS is gpsd, and gpsd is not up yet."""

    def setUp(self):
        self.now = 2000.0
        closed = bg.FakeGpsd()
        closed.stop()                                   # a port nothing listens on
        for target, name, value in (
                (gps_module.time, "monotonic", Mock(side_effect=lambda: self.now)),
                (gps_module, "GPSD_HOST", "127.0.0.1"),
                (gps_module, "GPSD_PORT", closed.port),
                (gps_module, "GPSD_CONNECT_TIMEOUT", 0.3),
                (gps_module.GpsManager, "_user_configured", False),
                # [merge-v3] this rig's gpsd is not configured for any receiver,
                # whatever /etc/default/gpsd says on the machine running the test
                (gps_module, "gpsd_configured_devices", lambda path=None: []),
                # no real serial port may be probed or opened by these tests
                (gps_module.GpsManager, "_auto_detect", staticmethod(lambda exclude=None: None)),
                (gps_module.GpsManager, "_probe_nmea", staticmethod(lambda device, baud: False))):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.gps = gps_module.GpsManager(device="never-open-test-device")
        self.addCleanup(self.gps.close)

    def start_gpsd(self):
        gpsd = bg.FakeGpsd()
        self.addCleanup(gpsd.stop)
        patcher = patch.object(gps_module, "GPSD_PORT", gpsd.port)
        patcher.start()
        self.addCleanup(patcher.stop)
        return gpsd

    def tick(self):
        return self.gps.gpsd_reconnect_tick(self.now)

    def finish_attempt(self):
        thread = self.gps._gpsd_connect_thread
        self.assertIsNotNone(thread, "no attempt was started")
        thread.join(5.0)
        self.assertFalse(thread.is_alive())

    def read_until(self, predicate):
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline:
            self.gps.read_available()
            if predicate():
                return True
            time.sleep(0.005)
        return False


class GpsdNotUpAtLaunchTests(LaunchCase):
    def test_gpsd_that_starts_later_is_found_without_toggling(self):
        self.assertFalse(self.gps.setup())              # launch: nothing answers
        self.assertFalse(self.gps.available)
        self.assertTrue(self.gps.gpsd_retrying)
        self.assertEqual(self.tick(), "waiting")        # said exactly once
        self.assertEqual([self.tick() for _ in range(50)], [None] * 50)
        self.assertIsNone(self.gps._gpsd_connect_thread)    # not before 5 s

        self.now += 5.0                                 # first look: still down
        self.assertIsNone(self.tick())
        self.finish_attempt()
        self.assertIsNone(self.tick())

        gpsd = self.start_gpsd()                        # gpsd comes up
        self.now += 5.0
        self.assertIsNone(self.tick())
        self.finish_attempt()
        self.assertEqual(self.tick(), "found")          # said exactly once
        self.assertEqual([self.tick() for _ in range(50)], [None] * 50)
        self.assertTrue(self.gps.available)
        self.assertFalse(self.gps.gpsd_retrying)
        self.assertEqual(self.gps.device, "gpsd://127.0.0.1:%d" % gpsd.port)

        gpsd.accept()
        self.assertIn(b"?WATCH=", gpsd.watch_request())
        gpsd.send(bg.tpv(5))
        self.assertTrue(self.read_until(lambda: self.gps.wardrive_sample(self.now) is not None))

    def test_it_keeps_looking_every_five_seconds(self):
        self.gps.setup()
        self.assertEqual(self.tick(), "waiting")
        attempts = []
        real = gps_module.GpsManager._connect_gpsd
        with patch.object(gps_module.GpsManager, "_connect_gpsd",
                          staticmethod(lambda: attempts.append(self.now) or real())):
            for _ in range(240):                        # one minute, 4 ticks per second
                self.now += 0.25
                self.assertIsNone(self.tick())
                if self.gps._gpsd_connect_thread is not None:
                    self.gps._gpsd_connect_thread.join(5.0)
        self.assertEqual(len(attempts), 12)
        self.assertEqual({round(b - a, 2) for a, b in zip(attempts, attempts[1:])}, {5.0})

    def test_waiting_and_finding_are_each_logged_once(self):
        with self.assertLogs(gps_module.log, level="INFO") as logs:
            self.gps.setup()
            self.tick()
            for _ in range(3):
                self.now += 5.0
                self.tick()
                self.finish_attempt()
                self.tick()
            self.start_gpsd()
            self.now += 5.0
            self.tick()
            self.finish_attempt()
            self.assertEqual(self.tick(), "found")
        waiting = [r for r in logs.records if "checking for it every" in r.getMessage()]
        found = [r for r in logs.records if "gpsd connection found" in r.getMessage()]
        self.assertEqual((len(waiting), len(found)), (1, 1))
        self.assertEqual({r.levelno for r in waiting + found}, {logging.INFO})

    def test_gpsd_already_running_at_launch_connects_at_once_and_waits_for_nothing(self):
        self.start_gpsd()
        self.assertTrue(self.gps.setup())
        self.assertTrue(self.gps.available)
        self.assertFalse(self.gps.gpsd_retrying)
        self.assertIsNone(self.tick())

    def test_a_serial_gps_found_at_launch_means_no_gpsd_retry(self):
        self.gps._try_open = Mock(return_value=True)
        with patch.object(gps_module.GpsManager, "_auto_detect",
                          staticmethod(lambda exclude=None: "/dev/ttyUSB7")):
            self.assertTrue(self.gps.setup())
        self.assertFalse(self.gps.gpsd_retrying)
        self.assertIsNone(self.tick())

    def test_a_pinned_gps_device_is_never_replaced_by_gpsd(self):
        self.start_gpsd()                               # even with gpsd up
        with patch.object(gps_module.GpsManager, "_user_configured", True):
            self.assertFalse(self.gps.setup())          # the pinned device is absent
        self.assertFalse(self.gps.gpsd_retrying)
        for _ in range(10):
            self.now += 5.0
            self.assertIsNone(self.tick())
        self.assertIsNone(self.gps._gpsd_connect_thread)

    def test_turning_gps_off_ends_the_wait(self):
        self.gps.setup()
        self.gps.close()                                # SYSTEM > GPS off
        self.assertFalse(self.gps.gpsd_retrying)
        self.start_gpsd()
        for _ in range(10):
            self.now += 5.0
            self.assertIsNone(self.tick())
        self.assertFalse(self.gps.available)
        self.assertIsNone(self.gps._gpsd_connect_thread)

    def test_a_serial_gps_opened_meanwhile_ends_the_wait(self):
        self.gps.setup()
        self.assertEqual(self.tick(), "waiting")
        self.gps._conn = SimpleNamespace(close=Mock())  # upstream's UART retry opened the port
        self.start_gpsd()
        for _ in range(10):
            self.now += 5.0
            self.assertIsNone(self.tick())
        self.assertFalse(self.gps.gpsd_retrying)
        self.assertIsNone(self.gps._gpsd_sock)          # gpsd was not adopted next to it
        self.assertIsNone(self.gps._gpsd_connect_thread)

    def test_only_gpsd_is_retried_never_the_serial_port_probing(self):
        self.gps.setup()
        probe = Mock(side_effect=AssertionError("serial ports probed again"))
        with patch.object(gps_module.GpsManager, "_auto_detect", staticmethod(probe)), \
                patch.object(gps_module.GpsManager, "_probe_nmea", staticmethod(probe)):
            for _ in range(8):
                self.now += 5.0
                self.tick()
                if self.gps._gpsd_connect_thread is not None:
                    self.gps._gpsd_connect_thread.join(5.0)
        probe.assert_not_called()

    def test_a_link_found_this_way_is_then_covered_by_the_drop_reconnect(self):
        self.gps.setup()
        gpsd = self.start_gpsd()
        self.now += 5.0
        self.tick(), self.tick()
        self.finish_attempt()
        self.assertEqual(self.tick(), "found")
        gpsd.accept()
        gpsd.stop()                                     # ...and later gpsd restarts
        self.assertTrue(self.read_until(lambda: not self.gps.available))
        self.assertEqual(self.tick(), "lost")
        self.start_gpsd()
        self.now += 5.0
        self.tick()
        self.finish_attempt()
        self.assertEqual(self.tick(), "restored")


class LaunchInTheGameLoopTests(LaunchCase):
    """app.py: boot with no gpsd, then gpsd starts. GPS turns on by itself."""

    def game(self):
        game = bg.app_methods("_setup_gps_connection", "_toggle_gps", "_poll_gps",
                              time=gps_module.time)()
        game.gps, game._aio_available = self.gps, False
        game._solo_track = game._solo_recorder = None
        game._solo = SimpleNamespace(confirmed_wifi=False, confirmed_ble=False)
        game._plugins = []
        game._poll_radio_profiles, game._term_add, game.msg = Mock(), Mock(), Mock()
        game.player_lat, game.player_lon, game.gps_fix, game._manual_move = 0.0, 0.0, False, True
        game.gps_sats = game.gps_sats_vis = 0
        return game

    def boot(self, game):
        game._setup_gps_connection()                    # what _initialize does
        game._gps_enabled = game.gps.available          # ...on a rig without the AIO board

    def said(self, game):
        return [call.args[0] for call in game._term_add.call_args_list]

    def frames_until(self, game, predicate, seconds=6.0):
        """Run frames while game time advances; return the slowest frame."""
        slowest, steps = 0.0, int(seconds / 0.5)
        for _ in range(steps):
            self.now += 0.5
            slowest = max(slowest, bg.run_frames(game._poll_gps, frames=10))
            thread = self.gps._gpsd_connect_thread
            if thread is not None:
                thread.join(5.0)
            if predicate():
                break
        return slowest

    def test_boot_without_gpsd_then_gpsd_starts(self):
        game = self.game()
        self.boot(game)
        self.assertFalse(game._gps_enabled)
        game._poll_gps()
        self.assertEqual(self.said(game), ["[GPS] gpsd is not running; checking for it every 5 s"])
        slowest = self.frames_until(game, lambda: False, seconds=11.0)      # two failed looks
        self.assertLess(slowest, FRAME_LIMIT)
        self.assertEqual(len(self.said(game)), 1)                           # ...said nothing more

        gpsd = self.start_gpsd()
        slowest = self.frames_until(game, lambda: self.gps.available)
        self.assertLess(slowest, FRAME_LIMIT)
        game._poll_gps()
        self.assertTrue(game._gps_enabled)                                  # GPS is on now
        self.assertEqual(self.said(game)[1:], ["[GPS] gpsd found; GPS on, waiting for a fix"])
        self.assertIn("GPS ON", game.msg.call_args.args[0])
        self.assertFalse(game.gps_fix)                                      # found is not a fix

        gpsd.accept()
        gpsd.send(bg.tpv(9, lat=12.9))
        deadline = time.perf_counter() + 5.0
        while not game.gps_fix and time.perf_counter() < deadline:
            game._poll_gps()
            time.sleep(0.005)
        self.assertTrue(game.gps_fix)
        self.assertEqual(game.player_lat, 12.9)
        self.assertEqual(len(self.said(game)), 2)                           # once each

    def test_gps_on_pressed_while_gpsd_is_down_keeps_looking_too(self):
        game = self.game()
        game._gps_enabled = False
        game._toggle_gps()                              # SYSTEM > GPS on: nothing there yet
        self.assertFalse(game._gps_enabled)
        self.assertIn("No device found", game.msg.call_args.args[0])       # upstream's own message
        self.assertTrue(self.gps.gpsd_retrying)
        self.start_gpsd()
        self.frames_until(game, lambda: self.gps.available)
        game._poll_gps()
        self.assertTrue(game._gps_enabled)

    def test_gps_off_pressed_while_waiting_stops_it_for_good(self):
        game = self.game()
        self.boot(game)
        game._gps_enabled = True                        # e.g. an AIO rig that tracks the rail
        game._toggle_gps()                              # SYSTEM > GPS off
        self.assertFalse(self.gps.gpsd_retrying)
        self.start_gpsd()
        self.frames_until(game, lambda: self.gps.available, seconds=11.0)
        self.assertFalse(self.gps.available)
        self.assertFalse(game._gps_enabled)


class EnsureGpsTests(unittest.TestCase):
    """Kismet wardrive start switches GPS on (WDGoNG ensure_gps)."""

    def setUp(self):
        self.plugin = kismet.KismetWardriveBridge()
        self.gps = SimpleNamespace(available=False, gpsd_retrying=False)
        self.app = SimpleNamespace(gps=self.gps, _gps_enabled=False, gps_fix=False,
                                   _toggle_gps=Mock(side_effect=self.switch_on))
        self.plugin.app = self.app
        self.plugin.term, self.plugin.msg = Mock(), Mock()

    def switch_on(self):
        self.app._gps_enabled, self.gps.available = True, True

    def said(self):
        return " | ".join(call.args[0] for call in self.plugin.term.call_args_list)

    def test_gps_off_is_switched_on_through_the_games_own_toggle(self):
        self.assertTrue(self.plugin._ensure_gps())
        self.app._toggle_gps.assert_called_once_with()
        self.assertIn("Wardriving needs GPS -- switching it on", self.said())
        self.assertIn("searching for a fix", self.said())       # on, but no fix yet

    def test_gps_already_on_is_left_alone(self):
        self.switch_on()
        self.app.gps_fix = True
        self.assertTrue(self.plugin._ensure_gps())
        self.app._toggle_gps.assert_not_called()                 # a toggle would turn it OFF
        self.assertEqual(self.said(), "")

    def test_gps_on_but_not_answering_is_never_toggled_off(self):
        self.app._gps_enabled = True                             # AIO rail on, UART still silent
        self.assertFalse(self.plugin._ensure_gps())
        self.app._toggle_gps.assert_not_called()
        self.assertIn("rows are held until there is a fix", self.said())

    def test_while_the_game_is_looking_for_gpsd_nothing_is_toggled(self):
        self.gps.gpsd_retrying = True
        self.assertFalse(self.plugin._ensure_gps())
        self.app._toggle_gps.assert_not_called()                 # no blocking serial probe
        self.assertIn("still checking every 5 s", self.said())

    def test_gps_that_cannot_start_does_not_stop_the_wardrive(self):
        self.app._toggle_gps.side_effect = None                  # toggled, nothing came up
        self.assertFalse(self.plugin._ensure_gps())
        self.assertIn("rows are held until there is a fix", self.said())
        self.app._toggle_gps.side_effect = RuntimeError("GPIO")  # or the toggle itself fails
        self.assertFalse(self.plugin._ensure_gps())
        self.assertIn("GPS could not be switched on: GPIO", self.said())

    def test_hosts_without_gps_or_toggle_are_tolerated(self):
        self.plugin.app = SimpleNamespace()
        self.assertFalse(self.plugin._ensure_gps())
        self.plugin.app = SimpleNamespace(gps=self.gps, _gps_enabled=False)
        self.assertFalse(self.plugin._ensure_gps())

    def launch(self, **kwargs):
        self.plugin._wifi_all = [dict(id="wlan1", mac="02:00:00:00:00:01")]
        self.plugin._wifi_selected, self.plugin._wifi_monitor, self.plugin._ble_all = {"wlan1"}, {}, []
        self.plugin._lifecycle = "idle"
        with patch.object(kismet.threading, "Thread", Mock()) as thread:
            self.plugin._launch_from_picker(**kwargs)
        return thread

    def test_a_new_drive_ensures_gps_before_kismet_is_launched(self):
        order = []
        self.plugin._ensure_gps = Mock(side_effect=lambda: order.append("gps"))
        thread = self.launch()
        thread.side_effect = None
        self.plugin._ensure_gps.assert_called_once_with()
        thread.assert_called_once()                              # the start worker
        self.assertEqual(thread.call_args.kwargs["name"], "kismet-start")
        self.assertEqual(self.plugin._lifecycle, "starting")

    def test_a_rotation_relaunch_and_a_refused_start_do_not_touch_gps(self):
        self.plugin._ensure_gps = Mock()
        self.launch(rotation=True)                               # the drive carries on
        self.plugin._ensure_gps.assert_not_called()
        self.plugin._wifi_selected = set()
        self.plugin._lifecycle = "idle"
        with patch.object(kismet.threading, "Thread", Mock()):
            self.plugin._launch_from_picker()                    # no adapter selected
        self.plugin._ensure_gps.assert_not_called()


class EnsureGpsWithTheRealToggleTests(LaunchCase):
    """The same, through app.py's real GPS toggle and the real GpsManager."""

    def game(self):
        game = bg.app_methods("_setup_gps_connection", "_toggle_gps", time=gps_module.time)()
        game.gps, game._aio_available, game._gps_enabled = self.gps, False, False
        game.gps_fix, game.gps_sats, game.gps_sats_vis = False, 0, 0
        game.msg, game._term_add = Mock(), Mock()
        return game

    def plugin(self, game):
        plugin = kismet.KismetWardriveBridge()
        plugin.app, plugin.term, plugin.msg = game, Mock(), Mock()
        return plugin

    def test_wardrive_start_with_gps_off_and_gpsd_running_turns_gps_on(self):
        self.start_gpsd()
        game = self.game()
        self.assertTrue(self.plugin(game)._ensure_gps())
        self.assertTrue(game._gps_enabled)
        self.assertTrue(self.gps.available)
        self.assertEqual(game.msg.call_args.args[0], "[GPS] ON")

    def test_wardrive_start_with_gpsd_down_starts_the_background_wait(self):
        game = self.game()
        plugin = self.plugin(game)
        self.assertFalse(plugin._ensure_gps())                   # toggled: nothing answered
        self.assertTrue(self.gps.gpsd_retrying)
        self.assertFalse(plugin._ensure_gps())                   # a second start: no toggle
        self.assertEqual(game.msg.call_count, 1)                 # upstream's "No device found", once


if __name__ == "__main__":
    unittest.main()
