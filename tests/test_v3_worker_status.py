"""[merge-v3] A worker that is faster than the screen that started it.

Four plugin screens start a worker thread and show "Reading..." / "Looking..."
/ "Asking..." / "Hunting..." while it works. That line used to be set AFTER
the worker was started. A worker that had already finished by then (nothing
to read, offline, rtl_power exiting at once) had its result overwritten, and
the screen kept saying "Reading the ADS-B log..." with nothing running. Found
when tests/test_v3_ng_plugins.py failed once in dozens of full runs.

Here the worker is the fastest possible one: it has finished by the time
Thread.start() returns. The status that is left must be the worker's result.
"""
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import plugins.ng_433_hunter as hunter
import plugins.ng_adsb_tracks as ng_adsb
import plugins.ng_loot_backup as ng_backup
import plugins.ng_wdgwars as ng_wdgwars

import test_v3_loot_backup as lb
import test_v3_ng_plugins as ng


class EagerThread:
    """threading.Thread whose target has run to its end when start() returns."""

    def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}
        self.name, self.daemon = name, daemon

    def start(self):
        self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        pass


def instant_workers(module):
    return patch.object(module.threading, "Thread", EagerThread)


class AdsbStatusTests(ng.AdsbPluginCase):
    def test_no_log_is_said_even_when_the_read_is_over_at_once(self):
        with instant_workers(ng_adsb):
            self.plugin.open_overlay()
        self.assertEqual(self.plugin._status, "No ADS-B log in this session yet (start SDR > ADS-B Radar).")
        self.assertFalse(self.plugin._busy)

    def test_the_read_result_is_not_covered_by_reading(self):
        self.receive(self.loot_manager, self.FLIGHTS)
        with instant_workers(ng_adsb):
            self.plugin.reload()
        self.assertEqual(self.plugin._status, "3 aircraft, 120 positions read.")

    def test_the_export_result_is_not_covered_by_writing(self):
        self.receive(self.loot_manager, self.FLIGHTS)
        with instant_workers(ng_adsb):
            self.plugin.export_kml()
        self.assertTrue(self.plugin._status.startswith("KML saved: loot/"), self.plugin._status)
        self.assertTrue((self.session / "adsb_tracks.kml").is_file())

    def test_while_a_worker_runs_the_working_line_is_shown_and_a_second_request_changes_nothing(self):
        release = ng.threading.Event()
        self.addCleanup(release.set)
        with patch.object(ng_adsb.adsb_tracks, "load", lambda *a, **k: release.wait(30) and None):
            self.receive(self.loot_manager, self.FLIGHTS)
            self.plugin.reload()
            self.assertEqual(self.plugin._status, "Reading the ADS-B log...")
            self.plugin.reload()                                  # refused: one read at a time
            self.assertEqual(self.plugin._status, "Reading the ADS-B log...")
            release.set()
            self.plugin._thread.join(20.0)


class BackupStatusTests(lb.ScreenCase):
    def test_the_scan_result_is_not_covered_by_looking(self):
        with instant_workers(ng_backup):
            self.plugin.open_overlay()
        self.assertTrue(self.plugin._status.startswith("Ready. 0 backup(s) in "), self.plugin._status)
        self.assertFalse(self.plugin._busy)


class PairingStatusTests(unittest.TestCase):
    def test_an_instant_no_from_the_portal_is_not_covered_by_asking(self):
        plugin = ng_wdgwars.NgWdgWars()
        portal = SimpleNamespace(fetch_game_state=Mock(return_value=None), request_pair=Mock(return_value=None),
                                 poll_pair=Mock(), adopt_api_key=Mock(), _api_key="",
                                 _api_url="https://x.invalid/api/upload/")
        plugin.app = SimpleNamespace(_plugins=[plugin, portal], msg=Mock(), _term_add=Mock())
        with instant_workers(ng_wdgwars):
            self.assertTrue(plugin.start_pairing())
        portal.request_pair.assert_called_once()
        self.assertEqual(plugin._status, "WDGWars gave no pairing code (offline, or pairing is not available).")
        self.assertIsNone(plugin._pair)
        self.assertFalse(plugin._busy)


class HunterStatusTests(unittest.TestCase):
    def test_rtl_power_that_exits_at_once_is_not_covered_by_hunting(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        process = SimpleNamespace(stdout=iter(()), terminate=Mock(), kill=Mock(), wait=Mock(return_value=1),
                                  poll=Mock(return_value=1))
        plugin = hunter.Ng433Hunter()
        plugin.app = SimpleNamespace(_app_dir=folder.name, loot=SimpleNamespace(active=True, session_dir=folder.name),
                                     _sdr=SimpleNamespace(running=False, mode=""), _aio_available=False,
                                     msg=Mock(), _term_add=Mock(), gps_fix=False)
        self.addCleanup(plugin.stop, True)
        with instant_workers(hunter), patch.object(hunter.shutil, "which", return_value="/usr/bin/rtl_power"), \
                patch.object(hunter.subprocess, "Popen", return_value=process):
            plugin.start()
        self.assertEqual(plugin._status,
                         "rtl_power ended by itself (SDR unplugged, off or busy). Press S to try again.")
        self.assertIsNone(plugin._proc)


if __name__ == "__main__":
    unittest.main()
