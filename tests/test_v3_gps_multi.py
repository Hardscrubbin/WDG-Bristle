"""[merge-v3] Two GPS receivers behind one gpsd: choice, hysteresis, alerts,
antenna status, the "never open gpsd's ports" rule, the AIO GPS rail at
launch and the GPS Health screen.

No gpsd, serial port, GPIO or pyxel: reports are fed straight into the real
MultiGps / GpsManager, pinctrl is a recorder and pyxel is the measuring Canvas
from test_v3_ng_plugins. The socket-level behaviour (two WATCH connections,
restart, unplug) is covered by tools/sim (test_v3_sim.py).
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from watchdogs import gps_manager as gps_module
from watchdogs import gps_multi, plugin_loader
import plugins.gps_health as gps_health

import test_v3_ng_plugins as ng

AIO, USB = "/dev/ttyAMA0", "/dev/ttyACM0"
W, H = ng.W, ng.H


def nmea(body):
    value = 0
    for char in body:
        value ^= ord(char)
    return "$%s*%02X" % (body, value)


def tpv(device, second, mode=3, lat=50.0, lon=19.0, **extra):
    report = {"class": "TPV", "device": device, "mode": mode,
              "time": "2026-11-21T12:%02d:%02d.000Z" % (second // 60, second % 60)}
    if mode >= 2:
        report.update(lat=lat, lon=lon)
    report.update(extra)
    return report


def sky(device, used=8, seen=11, hdop=1.0, snr=30.0):
    return {"class": "SKY", "device": device, "hdop": hdop,
            "satellites": [{"PRN": n + 1, "ss": snr if n < used else snr - 10, "used": n < used}
                           for n in range(seen)]}


class AntennaParsingTests(unittest.TestCase):
    def test_both_field_formats_are_understood(self):
        self.assertEqual(gps_multi.antenna_from_nmea(nmea("GPTXT,01,01,02,ANTSTATUS=OK")), "OK")
        self.assertEqual(gps_multi.antenna_from_nmea(nmea("GNTXT,01,01,02,ANTSTATUS=SHORT")), "SHORT")
        self.assertEqual(gps_multi.antenna_from_nmea(nmea("GPTXT,01,01,01,ANTENNA OPEN")), "OPEN")
        self.assertEqual(gps_multi.antenna_from_nmea(nmea("GPTXT,01,01,01,ANTENNA OK") + "\r\n"), "OK")

    def test_nothing_is_guessed(self):
        for line in (nmea("GPTXT,01,01,02,ANTSTATUS=INIT"), nmea("GPTXT,01,01,02,ANTSTATUS=DONTKNOW"),
                     nmea("GPTXT,01,01,02,u-blox ag - www.u-blox.com"), nmea("GPGGA,ANTENNA OPEN"),
                     "$GPTXT,01,01,01,ANTENNA OPEN*00", "$GPTXT,01,01,01,ANTENNA SHORT", "", None, 7):
            with self.subTest(line=line):
                self.assertIsNone(gps_multi.antenna_from_nmea(line))

    def test_a_silent_receiver_reads_not_reported_never_ok(self):
        device = gps_multi.DeviceHealth(USB, 0.0)
        self.assertEqual(device.antenna_text(), "not reported")
        self.assertFalse(device.snapshot(0.0, True)["antenna_reported"])

    def test_labels_describe_the_port(self):
        self.assertEqual(gps_multi.label_for("/dev/ttyAMA0"), "AIO GP-02 (UART)")
        self.assertEqual(gps_multi.label_for("/dev/ttyACM1"), "USB GPS")
        self.assertTrue(gps_multi.label_for("/dev/serial/by-id/usb-u-blox_AG_u-blox_7-if00")
                        .startswith("USB u-blox AG"))
        self.assertEqual(gps_multi.label_for(""), "GPS")


class Feeder:
    def __init__(self):
        self.multi = gps_multi.MultiGps()
        self.now = 100.0
        self.second = 0
        self.state = {AIO: dict(mode=3, used=8, seen=11, hdop=1.0, snr=30.0),
                      USB: dict(mode=3, used=8, seen=11, hdop=1.0, snr=30.0)}
        self.events = []

    def step(self, count=1, only=None):
        for _ in range(count):
            self.now += 1.0
            self.second += 1
            for path, state in self.state.items():
                if only is not None and path not in only:
                    continue
                self.multi.feed(sky(path, state["used"], state["seen"], state["hdop"], state["snr"]), self.now)
                self.multi.feed(tpv(path, self.second, mode=state["mode"]), self.now)
            self.multi.tick(self.now)
            self.events.extend(self.multi.drain_events())

    def texts(self, fragment):
        return [text for _level, text in self.events if fragment in text]


class ChoiceTests(unittest.TestCase):
    def setUp(self):
        self.f = Feeder()

    def test_best_receiver_is_3d_then_satellites_then_hdop(self):
        cases = ((dict(mode=2), dict(mode=3), USB),
                 (dict(used=9), dict(used=5), AIO),
                 (dict(hdop=2.5), dict(hdop=0.9), USB))
        for aio, usb, expected in cases:
            with self.subTest(aio=aio, usb=usb):
                f = Feeder()
                f.state[AIO].update(aio)
                f.state[USB].update(usb)
                f.step(1)                       # the first report picks whoever spoke first
                f.step(12)                      # ...and 10 s clearly better settles it
                self.assertEqual(f.multi.chosen, expected)

    def test_small_differences_never_switch(self):
        self.f.step(3)
        chosen = self.f.multi.chosen
        other = USB if chosen == AIO else AIO
        self.f.state[other].update(used=9, hdop=0.8)       # one satellite, 0.2 HDOP: not "clearly"
        self.f.step(120)
        self.assertEqual(self.f.multi.chosen, chosen)
        self.assertEqual(self.f.multi.switches, 0)

    def test_losing_the_fix_switches_at_once_with_the_reason(self):
        self.f.step(3)
        chosen = self.f.multi.chosen
        self.f.state[chosen]["mode"] = 1
        self.f.step(1)
        self.assertNotEqual(self.f.multi.chosen, chosen)
        switch = self.f.texts("switched to")
        self.assertEqual(len(switch), 1)
        self.assertIn("lost its fix", switch[0])

    def test_take_change_reports_a_switch_once(self):
        self.f.step(2)
        self.assertTrue(self.f.multi.take_change())
        self.assertFalse(self.f.multi.take_change())
        self.f.state[self.f.multi.chosen]["mode"] = 0
        self.f.step(1)
        self.assertTrue(self.f.multi.take_change())

    def test_one_receiver_alone_is_simply_used_and_says_nothing(self):
        del self.f.state[USB]
        self.f.step(30)
        self.assertEqual(self.f.multi.chosen, AIO)
        self.assertEqual(self.f.events, [])

    def test_events_and_satellite_lists_are_bounded(self):
        many = {"class": "SKY", "device": AIO,
                "satellites": [{"PRN": n, "ss": 20, "used": True} for n in range(500)]}
        self.f.multi.feed(many, 1.0)
        self.assertEqual(len(self.f.multi.devices[AIO].sats), gps_multi.MAX_SATS)
        for n in range(500):
            self.f.multi._say("info", "event %d" % n)
        self.assertEqual(len(self.f.multi.drain_events()), gps_multi.MAX_EVENTS)

    def test_hostile_reports_do_not_raise(self):
        for report in ({"class": "SKY", "device": AIO, "satellites": [None, 3, {"PRN": "x", "ss": "y"}]},
                       {"class": "TPV", "device": AIO, "mode": "3", "lat": [], "lon": {}},
                       {"class": "TPV", "device": 7, "mode": True},
                       {"class": "DEVICES", "devices": "none"}, {"class": "DEVICE"},
                       {"class": "SKY", "device": AIO, "nSat": -4, "uSat": "many", "hdop": float("nan")}):
            self.f.multi.feed(report, 5.0)
            self.f.multi.note_devices(report, 5.0)
        self.f.multi.tick(6.0)


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.f = Feeder()
        self.f.step(3)

    def test_short_is_an_alarm_once_and_rearms(self):
        for expected in (1, 2):
            self.f.multi.feed_nmea(AIO, nmea("GPTXT,01,01,01,ANTENNA SHORT"), self.f.now)
            self.f.step(5)
            alarms = [text for level, text in self.f.events if level == "alarm"]
            self.assertEqual(len(alarms), expected)
            self.assertIn("ANTENNA SHORT -- check cable now", alarms[-1])
            self.f.multi.feed_nmea(AIO, nmea("GPTXT,01,01,01,ANTENNA OK"), self.f.now)
            self.f.step(2)
        self.assertEqual(len(self.f.texts("antenna short cleared")), 2)

    def test_lost_fix_alert_only_while_the_other_keeps_one(self):
        self.f.state[AIO]["mode"] = 1
        self.f.step(2)
        self.assertEqual(self.f.texts("another receiver still has one"), [])     # under 3 s
        self.f.step(3)
        self.assertEqual(len(self.f.texts("another receiver still has one")), 1)
        self.f.step(30)
        self.assertEqual(len(self.f.texts("another receiver still has one")), 1)
        self.f.state[AIO]["mode"] = 3
        self.f.step(1)
        self.assertEqual(len(self.f.texts("has a fix again")), 1)

    def test_weak_antenna_needs_sixty_seconds_of_ten_db(self):
        self.f.state[USB]["snr"] = 19.0                                       # 11 dB below
        self.f.step(59)
        self.assertEqual(self.f.texts("weak antenna?"), [])
        self.f.step(3)
        self.assertEqual(len(self.f.texts("weak antenna?")), 1)
        self.f.state[USB]["snr"] = 21.0                                       # 9 dB: not weak
        self.f.step(2)
        self.assertEqual(len(self.f.texts("back in line")), 1)
        self.f.step(120)
        self.assertEqual(len(self.f.texts("weak antenna?")), 1)

    def test_a_receiver_that_stops_reporting_is_named_once(self):
        self.f.step(12, only=[AIO])
        self.assertEqual(len([t for level, t in self.f.events
                              if level == "warn" and "stopped reporting" in t]), 1)
        self.f.step(1)
        self.assertEqual(len(self.f.texts("is reporting again")), 1)

    def test_removed_device_message_counts_as_stopped(self):
        self.f.multi.note_devices({"class": "DEVICE", "path": USB}, self.f.now)
        self.f.events.extend(self.f.multi.drain_events())
        self.assertEqual(len(self.f.texts("stopped reporting")), 1)
        self.assertFalse(self.f.multi.devices[USB].has_fix(self.f.now))


class ManagerCase(unittest.TestCase):
    def setUp(self):
        self.now = 500.0
        clock = patch.object(gps_module.time, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        watch = patch.object(gps_module, "GPS_ANTENNA_WATCH", False)
        watch.start()
        self.addCleanup(watch.stop)
        self.gps = gps_module.GpsManager(device="never-open-test-device")
        self.gps._using_gpsd = self.gps._available = True
        self.second = 0

    def epoch(self, aio=None, usb=None):
        self.now += 1.0
        self.second += 1
        for path, state in ((AIO, aio), (USB, usb)):
            if state is None:
                continue
            state = dict(dict(mode=3, used=8, seen=11, hdop=1.0, lat=50.0, lon=19.0), **state)
            self.gps._apply_gpsd_report(sky(path, state["used"], state["seen"], state["hdop"]))
            self.gps._apply_gpsd_report(tpv(path, self.second, state["mode"], state["lat"], state["lon"]))
        self.gps._health_tick()


class ManagerRoutingTests(ManagerCase):
    def test_only_the_followed_receiver_feeds_the_fix(self):
        for _ in range(5):
            self.epoch(aio=dict(lat=50.0, used=9), usb=dict(lat=60.0, used=4, seen=6))
        self.assertEqual(self.gps.chosen_device, AIO)
        self.assertEqual((self.gps.fix.latitude, self.gps.fix.satellites, self.gps.fix.satellites_visible),
                         (50.0, 9, 11))
        self.assertEqual(self.gps.wardrive_sample()["lat"], 50.0)

    def test_a_switch_drops_the_old_position_and_takes_the_new_receivers_numbers(self):
        for _ in range(3):
            self.epoch(aio=dict(lat=50.0, used=9), usb=dict(lat=60.0, used=6, seen=8, hdop=1.7))
        self.now += 1.0
        self.second += 1
        self.gps._apply_gpsd_report(tpv(AIO, self.second, mode=1))          # the GP-02 loses its fix
        self.assertEqual(self.gps.chosen_device, USB)
        self.assertIsNone(self.gps.wardrive_sample())                       # nothing from the old receiver
        self.assertFalse(self.gps.fix.valid)
        self.assertEqual((self.gps.fix.satellites, self.gps.fix.hdop), (6, 1.7))
        self.gps._apply_gpsd_report(tpv(USB, self.second, lat=60.0))
        self.assertEqual(self.gps.wardrive_sample()["lat"], 60.0)
        events = self.gps.drain_health_events()
        self.assertEqual(len([text for _level, text in events if "switched to" in text]), 1)
        self.assertEqual(self.gps.drain_health_events(), [])

    def test_untagged_reports_still_work_as_one_receiver(self):
        report = tpv(AIO, 1)
        del report["device"]
        untagged_sky = sky(AIO)
        del untagged_sky["device"]
        self.gps._apply_gpsd_report(untagged_sky)
        self.gps._apply_gpsd_report(report)
        self.assertTrue(self.gps.fix.valid)
        self.assertEqual(self.gps.fix.satellites, 8)
        self.assertEqual(len(self.gps.health()["devices"]), 1)

    def test_a_silent_followed_receiver_clears_the_fix(self):
        self.epoch(aio={})
        self.assertTrue(self.gps.fix.valid)
        self.now += gps_multi.STALE_AFTER + 1
        self.gps._health_tick()
        self.assertFalse(self.gps.fix.valid)
        self.assertIsNone(self.gps.wardrive_sample())

    def test_closing_forgets_the_receivers_but_keeps_events_bounded(self):
        self.epoch(aio={}, usb={})
        self.gps.close()
        self.assertEqual(self.gps.health()["devices"], [])
        self.assertIsNone(self.gps.chosen_device)
        for n in range(500):
            self.gps._multi._say("info", "x%d" % n)
            self.gps._after_multi()
        self.assertLessEqual(len(self.gps._health_events), gps_module.GPS_HEALTH_EVENTS_MAX)

    def test_health_reports_how_gps_is_read(self):
        self.assertEqual(self.gps.health()["source"], "gpsd")
        self.gps.close()
        self.assertEqual(self.gps.health()["source"], "off")
        self.gps._wait_for_gpsd()
        self.assertEqual(self.gps.health()["source"], "waiting for gpsd")


class GpsdOwnsThePortsTests(unittest.TestCase):
    def defaults(self, text):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = os.path.join(folder.name, "gpsd")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_devices_line_is_parsed_like_the_shell_would(self):
        cases = (('DEVICES="/dev/ttyAMA0 /dev/ttyACM0"\n', ["/dev/ttyAMA0", "/dev/ttyACM0"]),
                 ("DEVICES='/dev/ttyS0'  # the UART\n", ["/dev/ttyS0"]),
                 ('#DEVICES="/dev/ttyAMA0"\nDEVICES=""\n', []),
                 ('DEVICES=/dev/serial/by-id/usb-x-if00\nGPSD_OPTIONS="-n"\n', ["/dev/serial/by-id/usb-x-if00"]),
                 ('START_DAEMON="true"\n', []),
                 ('DEVICES="relative junk"\n', []))
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(gps_module.gpsd_configured_devices(self.defaults(text)), expected)
        self.assertEqual(gps_module.gpsd_configured_devices(os.path.join(os.sep, "no", "such", "file")), [])

    def manager(self, text, **patches):
        values = dict(GPSD_DEFAULTS_FILE=self.defaults(text), GPSD_HOST="127.0.0.1")
        values.update(patches)
        for name, value in values.items():
            patcher = patch.object(gps_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, name, value in ((gps_module.shutil, "which", Mock(return_value="/usr/sbin/gpsd")),
                                    (gps_module.os.path, "realpath", lambda path: path),
                                    (gps_module.GpsManager, "_user_configured", False)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        gps = gps_module.GpsManager(device="/dev/ttyAMA0")
        self.addCleanup(gps.close)
        return gps

    def test_claimed_ports_are_never_opened(self):
        gps = self.manager('DEVICES="/dev/ttyAMA0 /dev/ttyACM0"\n')
        with patch.object(gps_module.serial, "Serial") as serial_open:
            self.assertTrue(gps.gpsd_claims("/dev/ttyAMA0"))
            self.assertFalse(gps._try_open("/dev/ttyAMA0"))
            self.assertFalse(gps._try_open("/dev/ttyACM0"))
            serial_open.assert_not_called()
        self.assertTrue(gps.gpsd_retrying)                      # it looks for gpsd instead
        self.assertEqual(gps.gpsd_owned_uart(), "/dev/ttyAMA0")
        self.assertEqual(gps.gpsd_reconnect_tick(), "waiting")
        self.assertFalse(gps._try_open("/dev/ttyAMA0"))         # the 5 s UART retry, again
        self.assertIsNone(gps.gpsd_reconnect_tick(now=0.0))     # ...does not announce "waiting" twice

    def test_the_rule_stands_down_when_it_should(self):
        text = 'DEVICES="/dev/ttyAMA0"\n'
        self.assertFalse(self.manager(text, GPSD_HOST="10.0.0.9").gpsd_claims("/dev/ttyAMA0"))
        self.assertFalse(self.manager("").gpsd_claims("/dev/ttyAMA0"))
        gps = self.manager(text)
        self.assertFalse(gps.gpsd_claims("/dev/ttyUSB3"))
        self.assertFalse(gps.gpsd_claims("never-open-test-device"))
        with patch.object(gps_module.shutil, "which", return_value=None):
            self.assertFalse(gps.gpsd_claims("/dev/ttyAMA0"))   # gpsd is not installed
        with patch.object(gps_module.GpsManager, "_user_configured", True):
            self.assertFalse(gps.gpsd_claims("/dev/ttyAMA0"))   # WDG_GPS_DEVICE pinned: user's call
        self.assertEqual(self.manager('DEVICES="/dev/ttyACM0"\n').gpsd_owned_uart(), "")

    def test_setup_waits_for_gpsd_instead_of_probing_serial_ports(self):
        gps = self.manager('DEVICES="/dev/ttyAMA0"\n', GPSD_PORT=1, GPSD_CONNECT_TIMEOUT=0.2)
        with patch.object(gps_module.serial, "Serial") as serial_open, \
                patch.object(gps_module.glob, "glob", return_value=["/dev/ttyACM0", "/dev/ttyUSB0"]):
            self.assertFalse(gps.setup())
            serial_open.assert_not_called()
        self.assertTrue(gps.gpsd_retrying)


class RailAtLaunchTests(unittest.TestCase):
    def harness(self, owned_uart, aio=True, toggle_ok=True):
        Harness = ng.app_methods("_setup_gps_connection")
        game = Harness()
        game._aio_available = aio
        game._aio_power = SimpleNamespace(toggle=Mock(return_value=toggle_ok))
        game._term_add = Mock()
        game.gps = SimpleNamespace(device="/dev/ttyAMA0", gpsd_owned_uart=Mock(return_value=owned_uart),
                                   setup=Mock(return_value=True), _try_open=Mock(return_value=True),
                                   open_gpsd_if_running=Mock(return_value=False))
        return game

    def test_rail_is_powered_and_the_uart_is_left_to_gpsd(self):
        game = self.harness("/dev/ttyAMA0")
        self.assertTrue(game._setup_gps_connection())
        game._aio_power.toggle.assert_called_once_with("gps", True)
        game.gps.setup.assert_called_once_with()
        game.gps._try_open.assert_not_called()

    def test_a_failed_rail_write_is_said_and_gpsd_is_still_used(self):
        game = self.harness("/dev/ttyAMA0", toggle_ok=False)
        game._setup_gps_connection()
        self.assertIn("GPIO 27", game._term_add.call_args.args[0])
        game.gps.setup.assert_called_once_with()

    def test_without_gpsd_ownership_c5_behaviour_is_unchanged(self):
        game = self.harness("")
        self.assertTrue(game._setup_gps_connection())
        game._aio_power.toggle.assert_not_called()
        game.gps._try_open.assert_called_once_with("/dev/ttyAMA0")
        game = self.harness("/dev/ttyAMA0", aio=False)
        game._setup_gps_connection()
        game._aio_power.toggle.assert_not_called()

    def test_stand_in_gps_objects_without_the_new_method_still_work(self):
        game = self.harness("")
        game.gps = SimpleNamespace(device="/dev/ttyAMA0", _try_open=Mock(return_value=True))
        self.assertTrue(game._setup_gps_connection())


class ScreenTests(ManagerCase):
    def setUp(self):
        super().setUp()
        self.app = SimpleNamespace(gps=self.gps, msg=Mock(), _term_add=Mock(), _esc_consumed_frame=-1,
                                   _play_svc_sound=Mock())
        self.plugin = gps_health.GpsHealth()
        self.plugin.on_load(self.app)

    def frame(self, canvas):
        canvas.new_frame()
        self.plugin._health_at = 0.0
        self.plugin.draw(0, 0, W, H)
        self.assertEqual(canvas.overlaps(), [])
        return canvas.texts

    def hud(self):
        return [call.args[0] for call in self.app.msg.call_args_list]

    def terminal(self):
        return [call.args[0] for call in self.app._term_add.call_args_list]

    def test_two_receivers_side_by_side(self):
        long_path = "/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00"
        for _ in range(3):
            self.now += 1.0
            self.second += 1
            for path, used, seen in ((AIO, 9, 12), (long_path, 14, 31)):
                self.gps._apply_gpsd_report(sky(path, used, seen, 0.9, 41.0))
                self.gps._apply_gpsd_report(tpv(path, self.second))
        self.gps._multi.feed_nmea(AIO, nmea("GPTXT,01,01,01,ANTENNA OK"), self.now)
        canvas = ng.Canvas()
        with ng.with_pyxel(canvas):
            self.plugin.open_overlay()
            texts = self.frame(canvas)
        self.assertEqual(texts.count(">>>"), 1)                       # exactly one receiver in use
        self.assertIn("AIO GP-02 (UART)", texts)
        self.assertIn("9/12", texts)
        self.assertIn("14/31", texts)
        self.assertIn("41 dB", texts)
        self.assertIn("OK", texts)
        self.assertIn("not reported", texts)                          # the dongle said nothing
        self.assertTrue(any(text.startswith("In use: AIO GP-02") for text in texts))
        self.assertIn(long_path[:120], texts)

    def test_every_state_draws_inside_the_screen(self):
        canvas = ng.Canvas()
        with ng.with_pyxel(canvas):
            self.plugin.open_overlay()
            self.assertTrue(any("no receiver has reported yet" in t for t in self.frame(canvas)))
            self.epoch(aio=dict(mode=1, used=0, seen=0), usb=dict(mode=2, used=3, seen=64))
            self.now += 30
            self.frame(canvas)                                        # both silent by now
            self.assertIn("SILENT", canvas.texts)
            for n in range(5):                                        # four receivers: only three rows
                self.gps._apply_gpsd_report(tpv("/dev/ttyUSB%d" % n, n + 50))
            self.frame(canvas)
            self.gps.close()
            self.assertTrue(any("GPS is off" in t for t in self.frame(canvas)))
            self.gps._wait_for_gpsd()
            self.assertTrue(any("checks for it every 5 s" in t for t in self.frame(canvas)))
            self.app.gps = SimpleNamespace()                          # a GPS object with no health()
            self.assertTrue(any("no health data" in t for t in self.frame(canvas)))

    def test_alerts_reach_hud_and_terminal_once_with_the_screen_closed(self):
        self.epoch(aio={}, usb={})
        self.gps._multi.feed_nmea(AIO, nmea("GPTXT,01,01,01,ANTENNA SHORT"), self.now)
        self.epoch(aio={}, usb={})
        canvas = ng.Canvas()
        with ng.with_pyxel(canvas):
            for _ in range(5):
                self.plugin.on_update()                               # screen is closed
        alarms = [text for text in self.hud() if "ANTENNA SHORT -- check cable now" in text]
        self.assertEqual(len(alarms), 1)
        self.assertEqual(self.app.msg.call_args_list[-1].args[1], gps_health.C_ERR)
        self.assertEqual(len([t for t in self.terminal() if "ANTENNA SHORT -- check cable now" in t]), 1)
        self.assertTrue(any(t.startswith("[GPS] !!! ") for t in self.terminal()))
        self.app._play_svc_sound.assert_called_once()
        with ng.with_pyxel(canvas):
            self.plugin.open_overlay()
            self.assertTrue(any("ANTENNA SHORT" in text for text in self.frame(canvas)))
            self.assertIn("SHORT", canvas.texts)

    def test_escape_closes_and_the_screen_is_read_only(self):
        canvas = ng.Canvas()
        with ng.with_pyxel(canvas):
            self.plugin.open_overlay()
            canvas.pressed = {"KEY_ESCAPE"}
            self.plugin.on_update()
        self.assertFalse(self.plugin.overlay_active)
        self.assertEqual(self.app._esc_consumed_frame, canvas.frame_count)
        source = open(gps_health.__file__, encoding="utf-8").read()
        for forbidden in ("subprocess", "toggle(", "pinctrl", "systemctl", "socket"):
            self.assertNotIn(forbidden, source)

    def test_loader_finds_the_plugin(self):
        found = [type(plugin).__name__ for plugin in plugin_loader.discover_plugins()]
        self.assertEqual(found.count("GpsHealth"), 1)
        self.assertEqual([item.key for item in gps_health.GpsHealth().menu_items()], ["g"])


if __name__ == "__main__":
    unittest.main()
