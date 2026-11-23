"""[merge-v3] Slow or failing I/O must never stop the game loop.

* gpsd auto-reconnect: a gpsd that restarts or drops the socket is retried
  every 5 s and GPS resumes without the in-game toggle; the drop and the
  reconnect are reported once each.
* WiGLE uploads (manual and queued retries) run on a worker thread; the game
  loop keeps ticking while an upload hangs.
* The notice-only update check fails quietly offline, uses a short timeout and
  runs in the background.

"gpsd" here is a listening TCP socket on 127.0.0.1 inside the test, WiGLE is a
function that blocks until the test releases it, and GitHub is a stub. Nothing
leaves this machine; no real gpsd, Kismet, pyxel window or network is used.
"""

import ast
import http.client
import importlib
import json
import logging
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    import serial  # noqa: F401
except ModuleNotFoundError:
    with patch.dict(sys.modules, {"serial": SimpleNamespace()}):
        gps_module = importlib.import_module("watchdogs.gps_manager")
else:
    gps_module = importlib.import_module("watchdogs.gps_manager")

from watchdogs import game_updates, merge_build
import plugins.kismet_wardrive as kismet

import test_v3_loot_kismet as lk

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "watchdogs" / "app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
GAME = next(node for node in TREE.body
            if isinstance(node, ast.ClassDef) and node.name == "WatchDogsGame")

# One frame of the 30 FPS loop is 33 ms. A blocked loop in these tests would
# sit for seconds (the hung upload / connect is only released by the test), so
# a generous bound still separates "ticking" from "frozen" on a slow PC.
FRAME_LIMIT = 0.5
FRAMES = 300


def app_methods(*names, **env):
    """Compile the real app.py methods into a bare class (upstream's test idiom)."""
    body = [node for node in GAME.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in body} == set(names), names
    module = ast.Module(body=[ast.ClassDef(name="Harness", bases=[], keywords=[],
                                           decorator_list=[], body=body)], type_ignores=[])
    namespace = dict(__name__="watchdogs.v3_method_test", __package__="watchdogs",
                     C_WARNING=10, C_SUCCESS=11, C_ERROR=8, C_TEXT=7, **env)
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), "exec"), namespace)
    return namespace["Harness"]


def run_frames(tick, frames=FRAMES):
    """Call ``tick`` like the game loop does; return the slowest frame (s)."""
    slowest = 0.0
    for _ in range(frames):
        started = time.perf_counter()
        tick()
        slowest = max(slowest, time.perf_counter() - started)
    return slowest


def tpv(second, lat=12.5, lon=34.5):
    return {"class": "TPV", "mode": 3, "lat": lat, "lon": lon,
            "time": "2026-11-21T12:00:%02d.000Z" % second}


class FakeGpsd:
    """A listening socket standing in for gpsd on 127.0.0.1."""

    def __init__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.listener.settimeout(5.0)
        self.port = self.listener.getsockname()[1]
        self.client = None

    def accept(self):
        self.client, _ = self.listener.accept()
        self.client.settimeout(5.0)
        return self.client

    def watch_request(self):
        return self.client.recv(200)

    def send(self, *reports):
        self.client.sendall(b"".join(json.dumps(r).encode() + b"\n" for r in reports))

    def stop(self):
        """gpsd stops: the client connection and the listening port both go."""
        for sock in (self.client, self.listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self.client = None


class GpsdCase(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        clock = patch.object(gps_module.time, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        # Windows takes ~2 s to refuse a loopback connection to a closed port;
        # the attempts against a stopped "gpsd" here need not wait that long.
        quick = patch.object(gps_module, "GPSD_CONNECT_TIMEOUT", 0.3)
        quick.start()
        self.addCleanup(quick.stop)
        self.gps = gps_module.GpsManager(device="never-open-test-device")
        self.addCleanup(self.gps.close)
        self.gpsd = self.serve()
        self.assertTrue(self.gps._try_open_gpsd())
        self.gpsd.accept()

    def serve(self):
        """Start a (new) gpsd and point the manager's config at it."""
        gpsd = FakeGpsd()
        self.addCleanup(gpsd.stop)
        for name, value in (("GPSD_HOST", "127.0.0.1"), ("GPSD_PORT", gpsd.port)):
            patcher = patch.object(gps_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return gpsd

    def read_until(self, predicate):
        """Poll the non-blocking reader until loopback delivery has happened."""
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline:
            self.gps.read_available()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def drop(self):
        self.gpsd.stop()
        self.assertTrue(self.read_until(lambda: not self.gps.available), "drop was not noticed")

    def finish_attempt(self):
        thread = self.gps._gpsd_connect_thread
        self.assertIsNotNone(thread, "no reconnect attempt was started")
        thread.join(5.0)
        self.assertFalse(thread.is_alive())

    def tick(self):
        return self.gps.gpsd_reconnect_tick(self.now)


class GpsdReconnectTests(GpsdCase):
    def test_fix_then_drop_then_automatic_reconnect_and_fix_again(self):
        self.assertIn(b"?WATCH=", self.gpsd.watch_request())
        self.gpsd.send(tpv(0))
        self.assertTrue(self.read_until(lambda: self.gps.wardrive_sample(self.now) is not None))

        self.drop()                                    # gpsd is stopped
        self.assertIsNone(self.gps.wardrive_sample(self.now))
        self.assertEqual(self.tick(), "lost")          # reported exactly once
        self.assertEqual([self.tick() for _ in range(50)], [None] * 50)
        self.assertIsNone(self.gps._gpsd_connect_thread)   # not before 5 s

        self.now += 5.0                                # first attempt: still down
        self.assertIsNone(self.tick())
        self.finish_attempt()
        self.assertIsNone(self.tick())
        self.assertFalse(self.gps.available)

        self.gpsd = self.serve()                       # gpsd is started again
        before = self.gps._gpsd_connect_thread
        self.now += 4.5
        self.assertIsNone(self.tick())                 # not yet 5 s since that attempt
        self.assertIs(self.gps._gpsd_connect_thread, before)
        self.now += 0.5
        self.assertIsNone(self.tick())                 # attempt starts
        self.assertIsNot(self.gps._gpsd_connect_thread, before)
        self.finish_attempt()
        self.assertEqual(self.tick(), "restored")      # reported exactly once
        self.assertEqual([self.tick() for _ in range(50)], [None] * 50)
        self.assertTrue(self.gps.available)
        self.assertEqual(self.gps.device, "gpsd://127.0.0.1:%d" % self.gpsd.port)

        self.gpsd.accept()                             # the new link is a real watch
        self.assertIn(b"?WATCH=", self.gpsd.watch_request())
        self.gpsd.send(tpv(30, lat=12.6))
        self.assertTrue(self.read_until(lambda: self.gps.wardrive_sample(self.now) is not None))
        self.assertEqual(self.gps.wardrive_sample(self.now)["lat"], 12.6)

    def test_retries_every_five_seconds_for_as_long_as_gpsd_is_down(self):
        self.drop()
        self.assertEqual(self.tick(), "lost")
        attempts = []
        real = gps_module.GpsManager._connect_gpsd
        with patch.object(gps_module.GpsManager, "_connect_gpsd",
                          staticmethod(lambda: attempts.append(self.now) or real())):
            for _ in range(240):                       # one minute at 4 ticks per second
                self.now += 0.25
                self.assertIsNone(self.tick())
                thread = self.gps._gpsd_connect_thread
                if thread is not None:
                    thread.join(5.0)
        self.assertEqual(len(attempts), 12)
        self.assertEqual({round(b - a, 2) for a, b in zip(attempts, attempts[1:])}, {5.0})

    def test_drop_and_reconnect_are_each_logged_once(self):
        self.gpsd.send(tpv(0))
        self.read_until(lambda: self.gps.wardrive_sample(self.now) is not None)
        with self.assertLogs(gps_module.log, level="INFO") as logs:
            self.drop()
            self.tick()
            for _ in range(3):                         # three failed attempts: silent
                self.now += 5.0
                self.tick()
                self.finish_attempt()
                self.tick()
            self.gpsd = self.serve()
            self.now += 5.0
            self.tick()
            self.finish_attempt()
            self.assertEqual(self.tick(), "restored")
        lost = [r for r in logs.records if "reconnecting every" in r.getMessage()]
        restored = [r for r in logs.records if "restored" in r.getMessage()]
        self.assertEqual((len(lost), len(restored), len(logs.records)), (1, 1, 2))
        self.assertEqual((lost[0].levelno, restored[0].levelno), (logging.WARNING, logging.INFO))

    def test_replayed_report_after_a_reconnect_is_not_a_new_measurement(self):
        self.gpsd.send(tpv(0))
        self.assertTrue(self.read_until(lambda: self.gps.wardrive_sample(self.now) is not None))
        self.drop()
        self.gpsd = self.serve()
        self.now += 5.0
        self.tick(), self.tick()
        self.finish_attempt()
        self.assertEqual(self.tick(), "restored")
        self.gpsd.accept()
        self.gpsd.send(tpv(0))                         # gpsd replays its last TPV
        self.gpsd.send({"class": "SKY", "nSat": 9, "uSat": 7, "hdop": 1.0})
        self.assertTrue(self.read_until(lambda: self.gps.fix.satellites == 7))
        self.assertIsNone(self.gps.wardrive_sample(self.now))
        self.gpsd.send(tpv(31))                        # a new epoch is
        self.assertTrue(self.read_until(lambda: self.gps.wardrive_sample(self.now) is not None))

    def test_a_slow_connect_never_blocks_the_loop_and_is_not_stacked(self):
        self.drop()
        self.assertEqual(self.tick(), "lost")
        release, started = threading.Event(), []

        def slow_connect():
            started.append(1)
            release.wait(30.0)
            return None

        self.addCleanup(release.set)
        with patch.object(gps_module.GpsManager, "_connect_gpsd", staticmethod(slow_connect)):
            def frame():
                self.now += 1.0                        # 5 minutes of game time
                self.assertIsNone(self.tick())
            slowest = run_frames(frame)
            self.assertLess(slowest, FRAME_LIMIT)
            self.assertEqual(len(started), 1)          # one attempt in flight, not 60
            self.assertTrue(self.gps._gpsd_connect_thread.is_alive())
            self.assertTrue(self.gps._gpsd_connect_thread.daemon)
            release.set()
            self.finish_attempt()

    def test_turning_gps_off_stops_the_reconnect_and_discards_a_late_socket(self):
        self.drop()
        self.assertEqual(self.tick(), "lost")
        self.gpsd = self.serve()
        release = threading.Event()
        real = gps_module.GpsManager._connect_gpsd
        made = []

        def held_connect():
            release.wait(30.0)
            made.append(real())
            return made[-1]

        self.addCleanup(release.set)
        with patch.object(gps_module.GpsManager, "_connect_gpsd", staticmethod(held_connect)):
            self.now += 5.0
            self.tick()                                # attempt in flight
            self.gps.close()                           # GPS OFF in-game
            release.set()
            self.finish_attempt()
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].fileno(), -1)         # the late socket was closed
        self.assertIsNone(self.gps._gpsd_connect_result)
        for _ in range(20):
            self.now += 5.0
            self.assertIsNone(self.tick())
        self.assertFalse(self.gps.available)
        self.assertFalse(self.gps._gpsd_lost)

    def test_read_error_is_a_drop_too(self):
        broken = Mock()
        broken.recv.side_effect = ConnectionResetError("reset by peer")
        self.gps._gpsd_sock.close()
        self.gps._gpsd_sock = broken
        self.gps.read_available()
        self.assertFalse(self.gps.available)
        self.assertEqual(self.tick(), "lost")

    def test_nothing_is_retried_unless_a_working_link_dropped(self):
        never = gps_module.GpsManager(device="never-open-test-device")     # gpsd never seen
        serial_gps = gps_module.GpsManager(device="/dev/ttyAMA0")
        serial_gps._available = True                                        # NMEA receiver
        with patch.object(gps_module.GpsManager, "_connect_gpsd",
                          staticmethod(Mock(side_effect=AssertionError("connect attempted")))):
            for manager in (never, serial_gps):
                for _ in range(20):
                    self.now += 5.0
                    self.assertIsNone(manager.gpsd_reconnect_tick(self.now))
                self.assertIsNone(manager._gpsd_connect_thread)


class GpsdReconnectInTheGameLoopTests(GpsdCase):
    """app.py's _poll_gps() drives the reconnect; the GPS toggle is never used."""

    def game(self):
        game = app_methods("_poll_gps", time=gps_module.time)()
        game.gps, game._gps_enabled = self.gps, True
        game._solo_track = game._solo_recorder = None
        game._solo = SimpleNamespace(confirmed_wifi=False, confirmed_ble=False)
        game._plugins = []
        game._poll_radio_profiles, game._term_add, game.msg = Mock(), Mock(), Mock()
        game._toggle_gps = Mock(side_effect=AssertionError("GPS was toggled"))
        game.player_lat, game.player_lon, game.gps_fix, game._manual_move = 0.0, 0.0, False, True
        return game

    def said(self, game):
        return [call.args[0] for call in game._term_add.call_args_list]

    def poll_until(self, game, predicate):
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline:
            game._poll_gps()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def test_gps_resumes_without_toggling_and_each_event_is_shown_once(self):
        game = self.game()
        self.gpsd.send(tpv(0))
        self.assertTrue(self.poll_until(game, lambda: game.gps_fix))
        self.assertEqual((game.player_lat, game.player_lon), (12.5, 34.5))

        self.gpsd.stop()                                           # gpsd restarts...
        self.assertTrue(self.poll_until(game, lambda: len(self.said(game)) == 1))
        self.assertEqual(self.said(game), ["[GPS] gpsd connection lost; reconnecting every 5 s"])
        self.assertFalse(game.gps_fix)                             # no stale "fix" for Kismet
        self.assertIn("gpsd lost", game.msg.call_args.args[0])

        self.gpsd = self.serve()                                   # ...and is back
        slowest = 0.0
        for _ in range(12):                                        # 6 s of frames
            self.now += 0.5
            slowest = max(slowest, run_frames(game._poll_gps, frames=10))
            thread = self.gps._gpsd_connect_thread
            if thread is not None:
                thread.join(5.0)
        self.assertLess(slowest, FRAME_LIMIT)
        self.assertEqual(self.said(game), ["[GPS] gpsd connection lost; reconnecting every 5 s",
                                           "[GPS] gpsd connection restored; waiting for a fix"])
        self.assertTrue(self.gps.available)
        self.assertFalse(game.gps_fix)                             # restored is not a fix yet

        self.gpsd.accept()
        self.gpsd.send(tpv(40, lat=12.7))
        self.assertTrue(self.poll_until(game, lambda: game.gps_fix))
        self.assertEqual(game.player_lat, 12.7)
        game._toggle_gps.assert_not_called()
        self.assertEqual(len(self.said(game)), 2)                  # still once each

    def test_no_reconnect_after_gps_is_switched_off_in_game(self):
        game = self.game()
        self.gpsd.stop()
        self.assertTrue(self.poll_until(game, lambda: len(self.said(game)) == 1))   # "lost"
        self.assertTrue(self.gps.gpsd_retrying)
        # SYSTEM > GPS, the real toggle: closes the GPS manager, which ends the retry
        toggle = app_methods("_toggle_gps", "_poll_gps", time=gps_module.time)()
        toggle.__dict__.update({name: value for name, value in game.__dict__.items()
                                if name != "_toggle_gps"})
        toggle._aio_available, toggle.gps_sats, toggle.gps_sats_vis = False, 0, 0
        toggle._toggle_gps()
        self.assertFalse(toggle._gps_enabled)
        self.assertFalse(self.gps.gpsd_retrying)
        for _ in range(20):
            self.now += 5.0
            toggle._poll_gps()
        self.assertIsNone(self.gps._gpsd_connect_thread)
        self.assertEqual(len(self.said(game)), 2)                 # "lost", then "[SYS] GPS disabled"

    def test_gps_objects_without_the_hook_poll_exactly_as_before(self):
        game = self.game()
        game.gps = SimpleNamespace(available=False, device="x")    # upstream's test stand-in
        game._poll_gps()
        game._poll_radio_profiles.assert_called_once_with()
        self.assertEqual(self.said(game), [])


# ---------------------------------------------------------------------------
# WiGLE uploads on a worker thread
# ---------------------------------------------------------------------------

class HungWigle:
    """requests.post stand-in: blocks until released, like a stalled upload."""

    def __init__(self, status=200, error=None):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.status, self.error = status, error

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        self.entered.set()
        self.release.wait(60.0)
        if self.error is not None:
            raise self.error
        status = self.status
        return SimpleNamespace(status_code=status, text="x", json=lambda: {"success": status == 200})


class WigleWorkerTests(lk.LootCase):
    def setUp(self):
        super().setUp()
        self.loot_manager = self.loot()
        self.loot_manager.save_wardriving_network(
            lk.net(lk.W1), observed_at=lk.T0,
            position=dict(lat=12.5, lon=34.5, altitude=5.0, accuracy=3.0))
        self.csv_path = self.csv(self.loot_manager)
        self.plugin = lk.make_plugin(self.root, self.loot_manager)
        self.plugin._last_wigle_retry = time.time()        # no retry pass unless a test asks
        self.wigle = HungWigle()
        self.addCleanup(self.wigle.release.set)
        for target, name, value in (
                (kismet, "requests", SimpleNamespace(post=self.wigle.post)),
                (kismet, "HAVE_REQUESTS", True),
                (kismet, "WIGLE_API_NAME", "name"),
                (kismet, "WIGLE_API_TOKEN", "token")):
            patcher = patch.object(target, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.internet = patch.object(kismet.hw, "internet_available", return_value=True)
        self.internet.start()
        self.addCleanup(self.internet.stop)

    def hud(self):
        return [call.args[0] for call in self.plugin.msg.call_args_list]

    def terminal(self):
        return [call.args[0] for call in self.plugin.term.call_args_list]

    def finish(self):
        self.wigle.release.set()
        thread = self.plugin._wigle_thread
        thread.join(10.0)
        self.assertFalse(thread.is_alive())
        self.plugin.on_update()                            # the frame that shows the result

    def start_manual(self):
        started = time.perf_counter()
        self.plugin.upload_wigle()
        self.assertLess(time.perf_counter() - started, FRAME_LIMIT)   # the menu action returns
        self.assertTrue(self.wigle.entered.wait(10.0), "the upload never started")

    def test_game_loop_keeps_ticking_while_a_manual_upload_hangs(self):
        self.start_manual()
        self.assertEqual(self.hud(), ["Uploading to WiGLE.net in the background..."])
        frames = []
        slowest = run_frames(lambda: (self.plugin.on_update(), frames.append(1)))
        self.assertEqual(len(frames), FRAMES)              # every frame ran...
        self.assertLess(slowest, FRAME_LIMIT)              # ...and none waited for WiGLE
        self.assertTrue(self.plugin._wigle_thread.is_alive())   # which is still hung
        self.assertTrue(self.plugin._wigle_busy)
        self.assertEqual(len(self.wigle.calls), 1)
        url, kwargs = self.wigle.calls[0]
        self.assertEqual(url, kismet.WIGLE_API_URL)
        self.assertEqual(kwargs["timeout"], 120)           # the 120 s timeout, on the worker
        self.assertEqual(self.plugin._wigle_thread.name, "kismet-wigle")
        self.assertTrue(self.plugin._wigle_thread.daemon)
        self.assertIsNot(self.plugin._wigle_thread, threading.current_thread())
        self.assertNotIn("Uploaded to WiGLE.net", self.hud())    # no result claimed early
        self.finish()
        self.assertEqual(self.hud()[-1], "Uploaded to WiGLE.net")
        self.assertIn("[KISMET] WiGLE upload OK", self.terminal())
        self.assertFalse(self.plugin._wigle_busy)

    def test_game_loop_keeps_ticking_while_a_queued_retry_hangs(self):
        self.plugin._queue_wigle_upload(self.csv_path)
        self.plugin._last_wigle_retry = 0.0                # the once-a-minute retry is due
        started = time.perf_counter()
        self.plugin.on_update()                            # this frame starts the pass
        self.assertLess(time.perf_counter() - started, FRAME_LIMIT)
        self.assertTrue(self.wigle.entered.wait(10.0))

        def frame():
            self.plugin._last_wigle_retry = 0.0            # "due" again on every frame
            self.plugin.on_update()

        self.assertLess(run_frames(frame), FRAME_LIMIT)
        self.assertEqual(len(self.wigle.calls), 1)         # never a second upload in parallel
        self.assertEqual(self.wigle.calls[0][1]["timeout"], 120)
        self.assertEqual(self.plugin._load_wigle_queue(), [str(self.csv_path)])   # still queued
        self.finish()
        self.assertEqual(self.plugin._load_wigle_queue(), [])
        self.assertIn("WiGLE: queued upload sent", self.hud()[-1])
        self.assertTrue(any("Queued WiGLE upload succeeded" in line for line in self.terminal()))

    def test_the_connectivity_ping_is_off_the_game_thread_too(self):
        self.internet.stop()
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def slow_ping():
            entered.set()
            release.wait(60.0)
            return False

        with patch.object(kismet.hw, "internet_available", slow_ping):
            self.plugin._queue_wigle_upload(self.csv_path)
            self.plugin._last_wigle_retry = 0.0
            self.assertLess(run_frames(self.plugin.on_update), FRAME_LIMIT)
            self.assertTrue(entered.wait(10.0))
            release.set()
            self.plugin._wigle_thread.join(10.0)
        self.internet.start()
        self.assertEqual(self.wigle.calls, [])             # offline: nothing was posted
        self.assertEqual(self.plugin._load_wigle_queue(), [str(self.csv_path)])

    def test_second_upload_request_while_one_runs_is_refused_not_queued_behind_it(self):
        self.start_manual()
        self.plugin.upload_wigle()
        self.assertIn("already running", self.hud()[-1])
        self.finish()
        self.assertEqual(len(self.wigle.calls), 1)

    def test_timed_out_manual_upload_is_queued_and_reported(self):
        self.wigle.error = TimeoutError("read timed out (120 s)")
        self.start_manual()
        self.assertLess(run_frames(self.plugin.on_update, frames=50), FRAME_LIMIT)
        self.assertEqual(self.plugin._load_wigle_queue(), [])        # not before it failed
        self.finish()
        self.assertEqual(self.hud()[-1], "WiGLE upload failed -- queued, will retry automatically")
        self.assertTrue(any("read timed out" in line for line in self.terminal()))
        self.assertEqual(self.plugin._load_wigle_queue(), [str(self.csv_path)])

    def test_failed_retry_stays_queued_and_is_not_a_hud_message_every_minute(self):
        self.wigle.status = 503
        self.plugin._queue_wigle_upload(self.csv_path)
        self.plugin._last_wigle_retry = 0.0
        self.plugin.on_update()
        self.assertTrue(self.wigle.entered.wait(10.0))
        self.finish()
        self.assertEqual(self.plugin._load_wigle_queue(), [str(self.csv_path)])
        self.assertEqual(self.hud(), [])
        self.assertTrue(any("still failing" in line for line in self.terminal()))

    def test_file_queued_while_a_retry_pass_runs_is_not_lost(self):
        other = self.csv_path.with_name("other-session.csv")
        other.write_bytes(self.csv_path.read_bytes())
        self.plugin._queue_wigle_upload(self.csv_path)
        self.plugin._last_wigle_retry = 0.0
        self.plugin.on_update()
        self.assertTrue(self.wigle.entered.wait(10.0))     # first file is uploading
        self.plugin._queue_wigle_upload(other)             # game thread queues another
        held = self.plugin._wigle_queue_hold(other)        # upstream's delete hold sees it at once
        self.assertIn("WiGLE", held)
        self.finish()                                      # the first file goes out
        self.assertEqual(len(self.wigle.calls), 1)
        self.assertEqual(self.plugin._load_wigle_queue(), [str(other)])   # the new one waits

    def test_manual_success_removes_an_older_queued_copy(self):
        self.plugin._queue_wigle_upload(str(self.csv_path.parent / "." / self.csv_path.name))
        self.start_manual()
        self.finish()
        self.assertEqual(self.plugin._load_wigle_queue(), [])   # no second post a minute later
        self.plugin._last_wigle_retry = 0.0
        self.plugin.on_update()
        self.assertEqual(len(self.wigle.calls), 1)

    def test_quitting_during_a_manual_upload_queues_the_file(self):
        self.start_manual()
        self.plugin._kismet_proc_alive = Mock(return_value=False)
        self.plugin.on_unload()
        self.assertEqual(self.plugin._load_wigle_queue(), [str(self.csv_path)])
        self.finish()                                      # (it did go out: dequeued again)
        self.assertEqual(self.plugin._load_wigle_queue(), [])

    def test_a_crashing_job_reports_and_frees_the_worker(self):
        self.assertTrue(self.plugin._start_wigle_job(Mock(side_effect=RuntimeError("boom"))))
        self.plugin._wigle_thread.join(10.0)
        self.plugin.on_update()
        self.assertFalse(self.plugin._wigle_busy)
        self.assertTrue(any("boom" in line for line in self.terminal()))
        self.assertTrue(self.plugin._start_wigle_job(Mock()))
        self.plugin._wigle_thread.join(10.0)

    def test_long_manual_upload_says_it_is_still_running(self):
        self.start_manual()
        self.plugin.on_update()
        self.assertEqual(len(self.hud()), 1)               # not on every frame
        self.plugin._wigle_noted -= 31.0
        self.plugin._wigle_started -= 31.0
        self.plugin.on_update()
        self.plugin.on_update()
        self.assertEqual(len(self.hud()), 2)
        self.assertRegex(self.hud()[-1], r"WiGLE upload still running \(3\d s\)")
        self.finish()

    def test_queue_file_is_replaced_atomically_and_garbage_reads_as_empty(self):
        self.plugin._save_wigle_queue(["a", "b"])
        queue_file = self.plugin._queue_file()
        self.assertEqual(json.loads(queue_file.read_text()), ["a", "b"])
        self.assertEqual([p.name for p in queue_file.parent.iterdir()], [queue_file.name])
        queue_file.write_text('{"not": "a list"}')
        self.assertEqual(self.plugin._load_wigle_queue(), [])
        queue_file.write_text("[trunc")
        self.assertEqual(self.plugin._load_wigle_queue(), [])

    def test_no_blocking_upload_call_is_left_on_the_game_thread(self):
        # Static guard: the only callers of the blocking pieces are the two
        # worker bodies, and on_update()/upload_wigle() only start jobs.
        text = (ROOT / "plugins" / "kismet_wardrive.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        bridge = next(node for node in tree.body
                      if isinstance(node, ast.ClassDef) and node.name == "KismetWardriveBridge")
        callers = {"_do_wigle_upload": set(), "internet_available": set()}
        for method in bridge.body:
            if not isinstance(method, ast.FunctionDef):
                continue
            for node in ast.walk(method):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr in callers:
                    callers[node.func.attr].add(method.name)
        self.assertEqual(callers["_do_wigle_upload"], {"_manual_wigle_worker", "_retry_wigle_worker"})
        self.assertEqual(callers["internet_available"], {"_retry_wigle_worker"})


# ---------------------------------------------------------------------------
# Notice-only update check: quiet offline, short timeout, background
# ---------------------------------------------------------------------------

RELEASE = dict(schema=2, repository=game_updates.REPOSITORY, repository_id=game_updates.REPOSITORY_ID,
               channel=game_updates.CHANNEL, release_id="v9.9.9", version="9.9.9",
               source_commit="a" * 40, title="RELEASE 9.9.9", notes="1. SOMETHING\nNotes.")
METADATA = dict(id=game_updates.REPOSITORY_ID, full_name=game_updates.REPOSITORY,
                owner=dict(id=game_updates.OWNER_ID, login=game_updates.OWNER))
METADATA_URL = f"https://api.github.com/repositories/{game_updates.REPOSITORY_ID}"
FEED_URL = (f"https://raw.githubusercontent.com/{game_updates.REPOSITORY}/"
            f"{game_updates.BRANCH}/{game_updates.FEED_PATH}")
OFFLINE = [
    urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution")),
    urllib.error.URLError(TimeoutError("timed out")),
    TimeoutError("The read operation timed out"),
    ConnectionRefusedError(111, "Connection refused"),
    OSError(101, "Network is unreachable"),
    ssl.SSLError("certificate verify failed (captive portal)"),
    http.client.IncompleteRead(b"half"),
    http.client.RemoteDisconnected("Remote end closed connection without response"),
]


class NoticeCheckTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.git = Mock(side_effect=AssertionError("git must not run in the merged build"))
        # No injected metadata: this is the object app.py really builds.
        self.updates = game_updates.GameUpdates(root=ROOT, home=self.home, git=self.git)
        slow = patch.object(game_updates.GameUpdates, "_github_metadata",
                            side_effect=AssertionError("C5's 12 s + gh fallback was used"))
        slow.start()
        self.addCleanup(slow.stop)
        gh = patch.object(game_updates.subprocess, "run",
                          side_effect=AssertionError("a subprocess was started"))
        gh.start()
        self.addCleanup(gh.stop)

    def network(self, metadata=METADATA, feed=RELEASE):
        requested = []

        def fetch(url, limit, accept="application/json"):
            requested.append((url, limit, accept))
            value = metadata if url == METADATA_URL else feed
            if isinstance(value, BaseException):
                raise value
            return value if isinstance(value, str) else json.dumps(value)

        return fetch, requested

    def test_online_check_uses_two_short_requests_and_no_git_or_gh(self):
        fetch, requested = self.network()
        result = merge_build.notice_only_check(self.updates, fetch=fetch)
        self.assertEqual((result.status, result.release.version), ("available", "9.9.9"))
        self.assertEqual(requested, [
            (METADATA_URL, game_updates.MAX_FEED_BYTES, "application/vnd.github+json"),
            (FEED_URL, game_updates.MAX_FEED_BYTES, "application/json")])
        self.assertTrue(self.updates.source_info()["verified"])
        self.assertIsNone(self.updates._metadata)          # shared object not re-pointed
        self.git.assert_not_called()

    def test_every_kind_of_offline_is_a_quiet_error_result(self):
        for where in ("metadata", "feed"):
            for failure in OFFLINE:
                with self.subTest(where=where, failure=repr(failure)[:50]):
                    fetch, requested = self.network(**{where: failure})
                    with self.assertNoLogs(level="INFO"):          # nothing above debug
                        result = merge_build.notice_only_check(self.updates, fetch=fetch)
                    self.assertEqual((result.status, result.release), ("error", None))
                    self.assertEqual(result.installed_version, "0.9.21.3")
                    self.assertEqual(result.message,
                                     "Cannot check for updates. Check network and GitHub access.")
                    self.assertEqual(len(requested), 1 if where == "metadata" else 2)
                    if where == "metadata":
                        self.assertFalse(self.updates.source_info()["verified"])
        self.assertFalse((self.home / ".watchdogs_game_updates.json").exists())

    def test_captive_portal_html_is_an_error_not_a_crash_or_an_update(self):
        html = "<html><body>Sign in to Wi-Fi</body></html>"
        for where in ("metadata", "feed"):
            with self.subTest(where=where):
                fetch, _ = self.network(**{where: html})
                result = merge_build.notice_only_check(self.updates, fetch=fetch)
                self.assertEqual((result.status, result.release), ("error", None))

    def test_wrong_repository_identity_is_still_refused(self):
        fetch, requested = self.network(metadata=dict(METADATA, id=1))
        result = merge_build.notice_only_check(self.updates, fetch=fetch)
        self.assertEqual(result.status, "error")
        self.assertEqual(len(requested), 1)                # the feed is never read
        self.assertFalse(self.updates.source_info()["verified"])

    def test_the_real_check_never_opens_a_connection(self):
        # [bristle] What app.py calls: no reader is handed in, so nothing may touch the network.
        def refuse(*_args, **_kwargs):
            raise AssertionError("the update check tried to use the network")

        with patch.object(merge_build.urllib.request, "urlopen", refuse), \
                patch.object(socket, "create_connection", refuse):
            result = merge_build.notice_only_check(self.updates)
        self.assertEqual((result.status, result.release), ("unpublished", None))
        self.assertEqual(result.installed_version, "0.9.21.3")
        self.assertEqual(result.message, merge_build.NOTICE_NONE)
        self.git.assert_not_called()

    def test_there_is_no_network_reader_left_in_the_module(self):
        source = Path(merge_build.__file__).read_text(encoding="utf-8")
        self.assertFalse(hasattr(merge_build, "_fetch_feed"))
        self.assertNotIn("urlopen(", source)
        self.assertNotIn("github.com/", source.replace("api.github.com/repositories/", ""))

    # ---- the app: background, and nothing on screen when it fails ----------

    def game(self):
        Harness = app_methods("_request_game_update_check", "_check_game_update",
                              "_poll_game_update_events", threading=threading,
                              logging=logging, pyxel=SimpleNamespace(frame_count=1))
        game = Harness()
        game._game_updates = self.updates
        game._game_update_events = Queue()
        game._game_update_checking = game._gameupd_running = False
        game._game_update_status, game._game_update_message = "idle", ""
        game._game_update_notice_queued = None
        game._game_update_available = False
        game._term_add, game.msg = Mock(), Mock()
        return game

    def test_startup_is_not_delayed_by_a_check_that_hangs(self):
        game = self.game()
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        offline = game_updates.UpdateResult(
            "error", "0.9.21.3", message="Cannot check for updates. Check network and GitHub access.")

        def hung_check(_updates):
            entered.set()
            release.wait(60.0)
            return offline

        workers = []
        real_thread = threading.Thread

        def tracked(*args, **kwargs):
            workers.append(real_thread(*args, **kwargs))
            return workers[-1]

        with patch.object(merge_build, "notice_only_check", hung_check), \
                patch.object(threading, "Thread", tracked):
            started = time.perf_counter()
            game._request_game_update_check()              # what update() does on frame 1
            self.assertLess(time.perf_counter() - started, FRAME_LIMIT)
            self.assertTrue(entered.wait(10.0))
            self.assertEqual(len(workers), 1)
            self.assertTrue(workers[0].daemon)             # cannot hold up quitting either
            self.assertLess(run_frames(game._poll_game_update_events), FRAME_LIMIT)
            self.assertEqual(game._game_update_status, "checking")
            game._request_game_update_check()              # no second check stacked on it
            self.assertEqual(len(workers), 1)
            release.set()
            workers[0].join(10.0)
        game._poll_game_update_events()
        self.assertEqual(game._game_update_status, "error")
        self.assertFalse(game._game_update_checking)
        self.assertIsNone(game._game_update_notice_queued)   # no popup
        self.assertFalse(game._game_update_available)
        game._term_add.assert_not_called()                   # nothing in the terminal
        game.msg.assert_not_called()                         # nothing on the HUD

    def test_the_real_worker_reports_managed_manually_and_is_quiet(self):
        game = self.game()
        with self.assertNoLogs(level="INFO"):
            game._check_game_update()
        game._poll_game_update_events()
        self.assertEqual(game._game_update_status, "unpublished")
        self.assertEqual(game._game_update_message, merge_build.NOTICE_NONE)
        self.assertIsNone(game._game_update_notice_queued)   # no popup
        game.msg.assert_not_called()

    def test_update_starts_the_check_once_through_the_background_request(self):
        update = next(node for node in GAME.body
                      if isinstance(node, ast.FunctionDef) and node.name == "update")
        text = ast.get_source_segment(SOURCE.read_text(encoding="utf-8"), update)
        self.assertEqual(text.count("self._request_game_update_check()"), 1)
        self.assertNotIn("notice_only_check", text)          # never called on the game thread
        self.assertNotIn("_check_game_update()", text)


if __name__ == "__main__":
    unittest.main()
