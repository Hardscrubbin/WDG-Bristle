"""[merge-v3] Hardware Check scenarios: the real screen over scripted probes."""
import sys
import time
from types import SimpleNamespace

from tools.sim import fake_hw
from tools.sim.core import check, check_equal, scenario


class Screen:
    def __init__(self, ctx, probes=None, **game):
        import plugins.hardware_check as hardware_check
        import test_v3_ng_plugins as ng
        self.module, self.ng = hardware_check, ng
        self.canvas = ng.Canvas()
        old = sys.modules.get("pyxel")
        sys.modules["pyxel"] = self.canvas
        ctx.cleanup(self._restore, old)
        self.app = fake_hw.game(**game)
        self.plugin = hardware_check.HardwareCheck()
        self.plugin.probes = probes or fake_hw.FakeProbes()
        self.plugin.on_load(self.app)
        self.frame_times = []

    @staticmethod
    def _restore(old):
        if old is None:
            sys.modules.pop("pyxel", None)
        else:
            sys.modules["pyxel"] = old

    def frame(self, *keys):
        self.canvas.pressed = set(keys)
        started = time.perf_counter()
        self.plugin.on_update()
        self.canvas.new_frame()
        self.plugin.draw(0, 0, self.ng.W, self.ng.H)
        self.frame_times.append(time.perf_counter() - started)
        check_equal(self.canvas.overlaps(), [], "texts that run into each other")
        self.canvas.pressed = set()

    def settle(self, timeout=5.0):
        """Frames until a snapshot taken after this call is on screen."""
        self.plugin._next = 0.0
        before = self.plugin.probes.calls.get("model", 0)
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            self.frame()
            if self.plugin.probes.calls.get("model", 0) > before and not self.plugin._busy:
                self.frame()
                return
            time.sleep(0.005)
        check(False, "the hardware snapshot never finished")

    def text(self):
        return "\n".join(self.canvas.texts)

    def has(self, fragment):
        return fragment in self.text()


@scenario("hardware", "the full uConsole rig")
def full_rig(ctx):
    screen = Screen(ctx)
    screen.plugin.open_overlay()
    screen.settle()
    for expected in ("Board  CM5", "CPU temp 54.2 C", "no under-voltage or throttling since boot",
                     "gpsd sees the GP-02 (3D fix, 9/12 sats)", "SX1262 answers on SPI",
                     "tuner test OK (Rafael Micro R820T)", ">AIO GP-02 (UART)  3D  9/12  ant OK",
                     "ant not reported", "in use by: none", "connected on /dev/ttyACM0, firmware 1.7.0",
                     "monitor:yes Kismet:yes", "monitor:NO  Kismet:no", "[monitor driver installed]",
                     "hci1  TP-Link UB500 Adapter  Kismet:yes", "hci0  built-in  Kismet:no",
                     "state: capturing", "channels: band split"):
        check(screen.has(expected), "missing on the screen: %r" % expected)
    check_equal(screen.app.toggles, [], "rails switched without a key press")
    check_equal(screen.plugin.probes.calls.get("rtl_test"), 1, "tuner tests when the screen opened")
    screen.settle()
    check_equal(screen.plugin.probes.calls.get("rtl_test"), 1, "tuner tests on a plain refresh")
    screen.frame("KEY_T")
    screen.settle()
    check_equal(screen.plugin.probes.calls.get("rtl_test"), 2, "tuner tests after pressing T")


@scenario("hardware", "busy radios are not probed")
def busy_radios(ctx):
    probes = fake_hw.FakeProbes()
    screen = Screen(ctx, probes, _sdr=SimpleNamespace(running=True, mode="adsb+433"),
                    _mt=SimpleNamespace(running=True, ready=True))
    screen.plugin.open_overlay()
    screen.settle()
    check(screen.has("receiving (adsb+433)") and screen.has("in use by: ADS-B + 433 MHz"), "SDR owner not shown")
    check(screen.has("in use by Meshtastic"), "LoRa owner not shown")
    check_equal(probes.calls.get("rtl_test", 0), 0, "tuner tests while the game is using the SDR")
    check_equal(probes.calls.get("sx1262", 0), 0, "SPI probes while a mesh protocol owns the radio")
    screen.frame("KEY_T")
    screen.settle()
    check_equal(probes.calls.get("rtl_test", 0), 0, "T must not test a busy SDR")


@scenario("hardware", "under-voltage and throttling are loud")
def under_voltage(ctx):
    sysmon = SimpleNamespace(snapshot=dict(temp_c=83.0, cpu_pct=97, throttle_raw=0x50005, undervolt_now=True,
                                           undervolt_ever=True, throttled_now=True, throttled_ever=True))
    screen = Screen(ctx, _sysmon=sysmon)
    screen.plugin.open_overlay()
    screen.settle()
    check(screen.has("UNDER-VOLTAGE NOW -- the supply is sagging"), "no under-voltage banner")
    check(screen.has("Power  UNDER-VOLTAGE NOW"), "no under-voltage line")
    sysmon.snapshot = dict(sysmon.snapshot, undervolt_now=False, throttled_now=False)
    screen.settle()
    check(not screen.has("UNDER-VOLTAGE NOW --"), "the banner stayed after the voltage recovered")
    check(screen.has("ok now; under-voltage and throttling since boot"), "'since boot' history missing")


@scenario("hardware", "parts missing or switched off")
def parts_missing(ctx):
    probes = fake_hw.FakeProbes(
        rails=dict(gps=True, lora=True, sdr=True, usb=False), usb={"0bda:8812": "Realtek RTL8812AU"},
        wifi=[fake_hw.ONBOARD], ble=[], serial=[], rtl_test=("none", "no RTL-SDR found by rtl_test"),
        sx1262=("none", "no answer on SPI (status 0x00)"))
    screen = Screen(ctx, probes, _esp32=False, _plugins=[],
                    gps=SimpleNamespace(health=lambda: fake_hw.gps_health(source="waiting for gpsd")))
    screen.plugin.open_overlay()
    screen.settle()
    for expected in ("NO DATA from the GP-02 yet", "no answer on SPI (status 0x00)",
                     "no RTL-SDR found by rtl_test", "AC1200 on USB (0bda:8812) but NO INTERFACE",
                     "the monitor-mode driver is not installed", "no Bluetooth controller found",
                     "not connected to the game", "the Kismet plugin is not loaded",
                     "waiting for gpsd"):
        check(screen.has(expected), "missing on the screen: %r" % expected)
    probes.state.update(rails=dict(gps=False, lora=False, sdr=False, usb=False), wifi=[
        dict(fake_hw.AC1200, can_monitor=False, driver="rtl8xxxu")])
    screen.frame("KEY_T")
    screen.settle()
    check(screen.text().count("rail off: not tested") == 3, "rails that are off must not be tested")
    check(screen.has("[MONITOR DRIVER NOT INSTALLED]"), "an AC1200 on a non-monitor driver was not flagged")
    check_equal(probes.calls.get("sx1262"), 1, "SPI probes with the LoRa rail off")
    check_equal(probes.calls.get("rtl_test"), 1, "tuner tests with the SDR rail off")


@scenario("hardware", "not an AIO board, probes failing")
def bare_machine(ctx):
    probes = fake_hw.FakeProbes(model="", rails=None, usb={}, wifi=[], ble=[], serial=[],
                                raises=("wifi", "ble", "serial", "usb", "rtl_test"))
    screen = Screen(ctx, probes, _sysmon=None, _esp32=False, _plugins=[], gps=SimpleNamespace())
    screen.plugin.open_overlay()
    screen.settle()
    for expected in ("Board  unknown board", "not reported (no vcgencmd", "no AIO v2 board found",
                     "GPS manager has no health data", "no Wi-Fi adapter found"):
        check(screen.has(expected), "missing on the screen: %r" % expected)
    screen.frame("KEY_G")
    check_equal(screen.app.toggles, [], "a rail toggle on a machine with no rails")
    check(any("No AIO v2 rails" in line for line in screen.app.hud), "no message for the refused toggle")
    screen.plugin.collect = lambda: 1 / 0                 # a bug in a probe
    screen.settle_error = True
    screen.plugin._next = 0.0
    deadline = time.perf_counter() + 3
    while time.perf_counter() < deadline and "error" not in (screen.plugin._snap or {}):
        screen.frame()
    check(screen.has("Hardware check failed: ZeroDivisionError"), "a failing probe took the screen down")


@scenario("hardware", "slow probes never stall the game; keys switch rails only when pressed")
def slow_probes_and_keys(ctx):
    probes = fake_hw.FakeProbes(hang=0.25)                # every probe takes a quarter second
    screen = Screen(ctx, probes)
    screen.plugin.open_overlay()
    for _ in range(40):
        screen.frame()
        time.sleep(0.01)
    check(max(screen.frame_times) < 0.1, "a slow probe stalled the game loop for %.2f s" % max(screen.frame_times))
    check(screen.has("Looking at the hardware..."), "no 'looking' text while the first check runs")
    screen.settle(timeout=15)
    check(screen.has("Board  CM5"), "the snapshot never arrived")
    check_equal(screen.app.toggles, [], "rails switched while only watching")
    probes.state["hang"] = 0.0
    screen.frame("KEY_G")
    screen.frame("KEY_S")
    screen.frame("KEY_U")
    check_equal(screen.app.toggles, ["gps", "sdr", "usb"], "rail toggles on G, S, U")
    check_equal(len([line for line in screen.app.term if "rail toggled from Hardware Check" in line]), 3,
                "terminal lines for the toggles")
    screen.frame("KEY_ESCAPE")
    check(not screen.plugin.overlay_active, "ESC did not close the screen")
    calls = dict(probes.calls)
    for _ in range(20):
        screen.frame()
        time.sleep(0.01)
    check_equal(probes.calls, calls, "probes kept running with the screen closed")
