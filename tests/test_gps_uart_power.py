"""AIO UART startup/toggle must not probe the ESP32; fake serial/GPIO only.

[merge-v3] Two machines, one result. upstream wrote these tests for a board whose
GPS is read straight from /dev/ttyAMA0. This build asks gpsd first, so on a
machine that HAS a gpsd (the uConsole) five of them found the real gpsd and
failed, while they passed on the build PC. Each test now states the machine
it is about (tests/gps_env.py) instead of taking whatever it runs on:

* GpsUartPowerTests          no gpsd at all: upstream's tests, unchanged, on the
                             direct-UART path upstream wrote them for
* GpsUartPowerWithGpsdTests  the same boot / GPS ON / GPS OFF steps on a
                             machine with gpsd (running or not, configured for
                             the UART or not): the UART is never opened
"""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from watchdogs import gps_manager as module

import gps_env


SOURCE = Path(__file__).resolve().parents[1] / "watchdogs" / "app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
APP = next(node for node in TREE.body if isinstance(node, ast.ClassDef)
           and node.name == "WatchDogsGame")
INITIALIZE = next(node for node in APP.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_initialize")
OPEN = next(index for index, node in enumerate(INITIALIZE.body)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "_setup_gps_connection")
STATUS = next(node for node in INITIALIZE.body if isinstance(node, ast.If)
              and any(isinstance(child, ast.Assign) and any(
                  isinstance(target, ast.Attribute) and target.attr == "_gps_enabled"
                  for target in child.targets) for child in node.body))
BOOT = ast.FunctionDef(name="boot_gps", args=ast.arguments(posonlyargs=[],
    args=[ast.arg(arg="self")], kwonlyargs=[], kw_defaults=[], defaults=[]),
    decorator_list=[], body=[copy.deepcopy(INITIALIZE.body[OPEN]),
                            copy.deepcopy(INITIALIZE.body[OPEN + 1]), copy.deepcopy(STATUS)])
CLOCK = SimpleNamespace(now=100.0)
AIO = SimpleNamespace(get_status=Mock(return_value={"gps": True}))
ENV = dict(AioManager=AIO, time=SimpleNamespace(monotonic=lambda: CLOCK.now),
           C_ERROR=8, C_WARNING=9, C_SUCCESS=11)
HARNESS = ast.ClassDef(name="GpsGame", bases=[], keywords=[], decorator_list=[],
    body=[BOOT] + [copy.deepcopy(node) for node in APP.body
        if isinstance(node, ast.FunctionDef)
        and node.name in ("_setup_gps_connection", "_toggle_gps", "_poll_gps")])
exec(compile(ast.fix_missing_locations(ast.Module(body=[HARNESS], type_ignores=[])),
             str(SOURCE), "exec"), ENV)
GpsGame = ENV["GpsGame"]


class GpsUartCase(unittest.TestCase):
    def setUp(self):
        CLOCK.now = 100.0
        AIO.get_status.return_value = {"gps": True}
        AIO.get_status.reset_mock()
        self.g = GpsGame()
        self.g._aio_available = True
        self.g._aio_power = SimpleNamespace(toggle=Mock(return_value=True), claim=Mock())
        self.g.gps = module.GpsManager(device="/dev/ttyAMA0")
        self.addCleanup(self.g.gps.close)
        self.g._gps_enabled = False
        self.g.gps_fix = False
        self.g.msg, self.g._term_add = Mock(), Mock()
        self.g._poll_radio_profiles = Mock()
        self.g.player_lat, self.g.player_lon = 12.5, 34.5
        self.conn = SimpleNamespace(in_waiting=0, reset_input_buffer=Mock(), close=Mock())
        self.serial = Mock(return_value=self.conn)
        for name, value in (("exists", True), ("access", True)):
            patched = patch.object(module.os.path, name, return_value=value) if name == "exists" else \
                patch.object(module.os, name, return_value=value)
            patched.start()
            self.addCleanup(patched.stop)
        patched = patch.object(module.serial, "Serial", self.serial)
        patched.start()
        self.addCleanup(patched.stop)


class GpsUartPowerTests(GpsUartCase):
    """upstream's tests, on the machine upstream wrote them for: no gpsd."""

    def setUp(self):
        super().setUp()
        gps_env.no_gpsd(self)             # [merge-v3] not "whatever this machine has"

    def prohibit_setup_probe(self):
        self.g.gps.setup = Mock(side_effect=AssertionError("USB GPS probing must not run"))

    def test_boot_opens_only_configured_uart_even_before_nmea_arrives(self):
        self.prohibit_setup_probe()
        self.g.boot_gps()
        self.g.gps.setup.assert_not_called()
        self.serial.assert_called_once_with(port="/dev/ttyAMA0", baudrate=9600, timeout=0)
        self.assertTrue(self.g._gps_enabled)
        self.assertTrue(self.g.gps.available)
        self.assertFalse(self.g.gps.fix.valid)
        self.g._poll_gps()
        self.assertFalse(self.g.gps_fix)
        self.assertEqual(self.serial.call_count, 1)

    def test_toggle_on_keeps_silent_uart_open_and_does_not_claim_a_fix(self):
        self.prohibit_setup_probe()
        self.g._toggle_gps()
        self.g._aio_power.toggle.assert_called_once_with("gps", True)
        self.assertTrue(self.g._gps_enabled)
        self.assertTrue(self.g.gps.available)
        self.assertFalse(self.g.gps_fix)
        self.g.gps.setup.assert_not_called()
        self.serial.assert_called_once_with(port="/dev/ttyAMA0", baudrate=9600, timeout=0)

    def test_failed_open_keeps_power_enabled_and_existing_retry_recovers(self):
        self.prohibit_setup_probe()
        self.serial.side_effect = [OSError("synthetic UART not ready"), self.conn]
        self.g._toggle_gps()
        self.assertTrue(self.g._gps_enabled)
        self.assertFalse(self.g.gps.available)
        self.g._poll_gps()
        self.assertTrue(self.g._gps_enabled)
        self.assertTrue(self.g.gps.available)
        self.assertFalse(self.g.gps_fix)
        self.assertEqual(self.serial.call_count, 2)
        self.g.gps.setup.assert_not_called()
        self.assertIn("waiting for UART", self.g.msg.call_args_list[0].args[0])

    def test_failed_boot_open_remains_pending_and_uart_retries_are_throttled(self):
        self.prohibit_setup_probe()
        self.serial.side_effect = OSError("synthetic UART not ready")
        self.g.boot_gps()
        self.assertTrue(self.g._gps_enabled)
        self.g._poll_gps()
        self.assertEqual(self.serial.call_count, 2)
        CLOCK.now = 104.9
        self.g._poll_gps()
        self.assertEqual(self.serial.call_count, 2)
        CLOCK.now = 105.0
        self.g._poll_gps()
        self.assertEqual(self.serial.call_count, 3)
        self.assertTrue(self.g._gps_enabled)
        self.g.gps.setup.assert_not_called()

    def test_gpio_failure_does_not_open_uart_or_change_disabled_state(self):
        self.prohibit_setup_probe()
        self.g._aio_power.toggle.return_value = False
        self.g._toggle_gps()
        self.assertFalse(self.g._gps_enabled)
        self.serial.assert_not_called()
        self.g.gps.setup.assert_not_called()

    def test_toggle_off_closes_uart_clears_fix_and_disables_retry(self):
        self.prohibit_setup_probe()
        self.g._toggle_gps()
        self.g.gps_fix = True
        self.g.gps_sats, self.g.gps_sats_vis = 8, 12
        self.g._toggle_gps()
        self.conn.close.assert_called_once_with()
        self.assertFalse(self.g._gps_enabled)
        self.assertFalse(self.g.gps.available)
        self.assertFalse(self.g.gps_fix)
        self.assertEqual((self.g.gps_sats, self.g.gps_sats_vis), (0, 0))
        before = self.serial.call_count
        CLOCK.now += 10
        self.g._poll_gps()
        self.assertEqual(self.serial.call_count, before)

    def test_non_aio_and_usb_gps_keep_the_existing_setup_path(self):
        for aio, device in ((False, "/dev/ttyAMA0"), (True, "/dev/ttyUSB0"),
                            (True, "/dev/serial/by-id/synthetic-gps")):
            with self.subTest(aio=aio, device=device):
                self.g._aio_available = aio
                self.g.gps.device = device
                self.g.gps._available = False
                self.g._gps_enabled = False
                def setup():
                    self.g.gps._available = True
                    return True
                self.g.gps.setup = Mock(side_effect=setup)
                self.g._toggle_gps()
                self.g.gps.setup.assert_called_once_with()
                self.assertTrue(self.g._gps_enabled)
                self.serial.assert_not_called()

    def test_failed_non_aio_or_usb_setup_does_not_enable_uart_retry(self):
        for aio, device in ((False, "/dev/ttyAMA0"), (True, "/dev/ttyUSB0")):
            with self.subTest(aio=aio, device=device):
                self.g._aio_available = aio
                self.g.gps.device = device
                self.g.gps._available = False
                self.g._gps_enabled = False
                self.g.gps.setup = Mock(return_value=False)
                self.g._toggle_gps()
                self.assertFalse(self.g._gps_enabled)
                self.g._poll_gps()
                self.serial.assert_not_called()


class GpsUartPowerWithGpsdTests(GpsUartCase):
    """[merge-v3] The same steps on a machine that has gpsd. Whatever state
    gpsd is in, the game never opens the UART and never probes a serial port;
    only a pinned WDG_GPS_DEVICE (the player's own override) goes direct."""

    def setUp(self):
        super().setUp()
        self.probe = Mock(side_effect=AssertionError("a serial port was probed"))
        for name in ("_auto_detect", "_probe_nmea"):
            patched = patch.object(module.GpsManager, name, staticmethod(self.probe))
            patched.start()
            self.addCleanup(patched.stop)
        patched = patch.object(module, "GPSD_RECONNECT_INTERVAL", 0.0)     # retry at once, not after 5 s
        patched.start()
        self.addCleanup(patched.stop)

    def finish_attempt(self):
        thread = self.g.gps._gpsd_connect_thread
        self.assertIsNotNone(thread, "gpsd was not looked for")
        thread.join(5.0)
        self.assertFalse(thread.is_alive())

    def terminal(self):
        return [entry.args[0] for entry in self.g._term_add.call_args_list]

    def hud(self):
        return [entry.args[0] for entry in self.g.msg.call_args_list]

    # ---- gpsd running and configured for the UART: the uConsole ----------

    def test_boot_powers_the_gps_rail_then_uses_gpsd_and_never_opens_the_uart(self):
        gpsd = gps_env.Gpsd(self)
        order = []
        self.g._aio_power.toggle.side_effect = lambda *args: order.append((args, gpsd.connects)) or True
        self.g.boot_gps()
        self.assertEqual(order, [(("gps", True), 0)])            # GPIO 27 first, then gpsd is asked
        self.assertEqual(gpsd.connects, 1)
        self.serial.assert_not_called()
        self.probe.assert_not_called()
        self.assertTrue(self.g.gps.available)
        self.assertTrue(self.g.gps._using_gpsd)
        self.assertEqual(self.g.gps.device, "gpsd://127.0.0.1:%d" % module.GPSD_PORT)
        self.g._aio_power.claim.assert_called_once_with("gps")
        self.assertTrue(self.g._gps_enabled)
        self.assertFalse(self.g.gps.fix.valid)                   # connected is not a fix
        self.g._poll_gps()
        self.assertFalse(self.g.gps_fix)
        self.serial.assert_not_called()

    def test_a_fix_from_gpsd_is_the_games_fix(self):
        gpsd = gps_env.Gpsd(self)
        self.g.boot_gps()
        gpsd.send(gps_env.sky("/dev/ttyAMA0"), gps_env.tpv("/dev/ttyAMA0", lat=50.25, lon=19.5))
        self.g.gps.read_available()
        self.assertTrue(self.g.gps.fix.valid)
        self.assertEqual((self.g.gps.fix.latitude, self.g.gps.fix.longitude), (50.25, 19.5))
        self.assertEqual(self.g.gps.chosen_device, "/dev/ttyAMA0")
        self.serial.assert_not_called()

    def test_gps_off_closes_the_gpsd_link_and_gps_on_brings_it_back_without_the_uart(self):
        gpsd = gps_env.Gpsd(self)
        self.g.boot_gps()
        self.g._toggle_gps()                                      # SYSTEM > GPS: OFF
        self.assertEqual(self.g._aio_power.toggle.call_args, call("gps", False))
        self.assertFalse(self.g._gps_enabled)
        self.assertFalse(self.g.gps.available)
        self.assertFalse(self.g.gps.gpsd_retrying)
        asked = gpsd.connects
        for _ in range(3):                                        # off stays off: nothing is retried
            CLOCK.now += 10
            self.g._poll_gps()
        self.assertEqual(gpsd.connects, asked)
        self.assertIsNone(self.g.gps._gpsd_connect_thread)
        self.g._toggle_gps()                                      # ON again
        self.assertEqual(self.g._aio_power.toggle.call_args, call("gps", True))
        self.assertTrue(self.g._gps_enabled)
        self.assertTrue(self.g.gps.available)
        self.assertTrue(self.g.gps._using_gpsd)
        self.assertIn("[GPS] ON", self.hud())
        self.serial.assert_not_called()
        self.probe.assert_not_called()

    def test_gpsd_that_dies_is_waited_for_and_the_uart_stays_closed(self):
        gpsd = gps_env.Gpsd(self)
        self.g.boot_gps()
        gpsd.close()                                              # gpsd stops
        self.g._poll_gps()                                        # the read sees the closed link
        self.g._poll_gps()
        self.assertFalse(self.g.gps.available)
        self.assertIn("[GPS] gpsd connection lost; reconnecting every 5 s", self.terminal())
        for _ in range(3):                                        # upstream's 5 s UART retry has nothing to open
            CLOCK.now += 5.0
            self.g._poll_gps()
            self.finish_attempt()
        self.serial.assert_not_called()
        gpsd.running = True                                       # gpsd is back
        self.g._poll_gps()
        self.finish_attempt()
        self.g._poll_gps()
        self.assertTrue(self.g.gps.available)
        self.assertIn("[GPS] gpsd connection restored; waiting for a fix", self.terminal())
        self.serial.assert_not_called()
        self.probe.assert_not_called()

    # ---- gpsd configured for the UART but not answering (still starting) ----

    def test_gpsd_not_answering_yet_keeps_the_uart_closed_until_gpsd_is_up(self):
        gpsd = gps_env.Gpsd(self, running=False)
        self.g.boot_gps()
        self.g._aio_power.toggle.assert_called_once_with("gps", True)    # the rail is on for gpsd
        self.serial.assert_not_called()
        self.assertFalse(self.g.gps.available)
        self.assertTrue(self.g.gps.gpsd_retrying)
        self.assertTrue(self.g._gps_enabled)                      # the AIO rail state
        self.g._poll_gps()
        self.assertIn("[GPS] gpsd is not running; checking for it every 5 s", self.terminal())
        for _ in range(4):                                        # upstream's UART retry runs and is refused:
            CLOCK.now += 5.0                                      # the port belongs to gpsd
            self.g._poll_gps()
            self.finish_attempt()
        self.serial.assert_not_called()
        self.assertFalse(self.g.gps.available)
        gpsd.running = True                                       # gpsd comes up
        self.g._poll_gps()
        self.finish_attempt()
        self.g._poll_gps()
        self.assertTrue(self.g.gps.available)
        self.assertTrue(self.g.gps._using_gpsd)
        self.assertIn("[GPS] gpsd found; GPS on, waiting for a fix", self.terminal())
        self.serial.assert_not_called()
        self.probe.assert_not_called()

    def test_gps_on_with_gpsd_not_answering_does_not_fall_back_to_the_uart(self):
        gps_env.Gpsd(self, running=False)
        self.g._toggle_gps()
        self.assertIn(call("gps", True), self.g._aio_power.toggle.call_args_list)
        self.assertTrue(self.g._gps_enabled)                      # powered, waiting
        self.assertFalse(self.g.gps.available)
        self.assertTrue(self.g.gps.gpsd_retrying)
        self.serial.assert_not_called()
        self.probe.assert_not_called()

    # ---- gpsd running, but /etc/default/gpsd names no receiver -------------

    def test_gpsd_running_without_a_receiver_list_is_still_asked_before_the_uart(self):
        gpsd = gps_env.Gpsd(self, devices=())
        self.g.boot_gps()
        self.assertEqual(gpsd.connects, 1)
        self.assertTrue(self.g.gps._using_gpsd)
        self.assertTrue(self.g._gps_enabled)
        self.serial.assert_not_called()
        self.g._toggle_gps()                                      # OFF
        self.g._toggle_gps()                                      # ON
        self.assertTrue(self.g.gps._using_gpsd)
        self.serial.assert_not_called()
        self.probe.assert_not_called()

    def test_gpsd_configured_for_other_receivers_only_leaves_the_uart_to_the_game(self):
        # gpsd owns only the USB dongle and is not answering: the AIO UART is
        # not gpsd's, so upstream's direct open still applies to it
        gps_env.Gpsd(self, devices=("/dev/ttyACM0",), running=False)
        self.g.boot_gps()
        self.serial.assert_called_once_with(port="/dev/ttyAMA0", baudrate=9600, timeout=0)
        self.assertTrue(self.g.gps.available)
        self.assertFalse(self.g.gps._using_gpsd)

    # ---- the player's override ------------------------------------------------

    def test_a_pinned_gps_device_is_opened_directly_even_with_gpsd_running(self):
        gpsd = gps_env.Gpsd(self, pinned=True)                    # WDG_GPS_DEVICE=/dev/ttyAMA0
        self.g.boot_gps()
        self.serial.assert_called_once_with(port="/dev/ttyAMA0", baudrate=9600, timeout=0)
        self.assertEqual(gpsd.connects, 0)
        self.assertTrue(self.g.gps.available)
        self.assertFalse(self.g.gps._using_gpsd)
        self.probe.assert_not_called()


class SameResultOnEveryMachineTests(unittest.TestCase):
    """[merge-v3] The helpers above must really cut the test off from the
    machine: no connection to the real gpsd address, no read of the real
    /etc/default/gpsd, no dependence on WDG_GPS_DEVICE."""

    def test_no_gpsd_means_no_network_and_no_system_file(self):
        gps_env.no_gpsd(self)
        connect = Mock(side_effect=AssertionError("the real gpsd address was contacted"))
        opened = Mock(side_effect=AssertionError("the real /etc/default/gpsd was read"))
        with patch.object(module.socket, "create_connection", connect), \
                patch.object(module, "GPSD_DEFAULTS_FILE", "/etc/default/gpsd"), \
                patch("builtins.open", opened), \
                patch.object(module.GpsManager, "_auto_detect", staticmethod(lambda exclude=None: None)), \
                patch.object(module.os.path, "exists", return_value=False):
            gps = module.GpsManager(device="/dev/ttyAMA0")
            self.addCleanup(gps.close)
            self.assertFalse(gps.setup())
            self.assertEqual((gps.gpsd_owns_receivers(), gps.gpsd_claims("/dev/ttyAMA0"), gps.gpsd_owned_uart()),
                             (False, False, ""))
            self.assertFalse(gps.open_gpsd_if_running())
        connect.assert_not_called()
        opened.assert_not_called()

    def test_a_test_gpsd_is_a_socket_pair_not_the_real_port(self):
        gpsd = gps_env.Gpsd(self)
        connect = Mock(side_effect=AssertionError("the real gpsd address was contacted"))
        with patch.object(module.socket, "create_connection", connect):
            gps = module.GpsManager(device="/dev/ttyAMA0")
            self.addCleanup(gps.close)
            self.assertTrue(gps.setup())
            gpsd.send(gps_env.sky("/dev/ttyAMA0"), gps_env.tpv("/dev/ttyAMA0"),
                      gps_env.sky("/dev/ttyACM0"), gps_env.tpv("/dev/ttyACM0", lat=60.0))
            for _ in range(5):                                    # antenna watch included
                gps.read_available()
            self.assertTrue(gps.fix.valid)
            self.assertEqual((gps.gpsd_claims("/dev/ttyAMA0"), gps.gpsd_owned_uart()), (True, "/dev/ttyAMA0"))
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
