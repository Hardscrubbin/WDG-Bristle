"""[merge-v3] gpsd scenarios: the real GpsManager against tools/sim/fake_gpsd.py.

Two receivers as on the uConsole: the AIO GP-02 on a UART and a USB dongle.
"""
import os
import select
import time

from tools.sim.core import SimClock, check, check_equal, scenario
from tools.sim.fake_gpsd import FakeGpsd, sats

AIO = "/dev/ttyAMA0"
USB = "/dev/serial/by-id/usb-u-blox_AG_u-blox_7-if00"


class GpsRig:
    def __init__(self, ctx, devices, antenna_watch=False):
        from watchdogs import gps_manager as gm
        self.gm = gm
        self.clock = SimClock()
        self.clock.install(ctx, gm)
        self.gpsd = FakeGpsd()
        for path, state in devices:
            self.gpsd.add(path, **state)
        self.gpsd.start()
        ctx.cleanup(self.gpsd.stop)
        ctx.patch(gm, "GPSD_HOST", "127.0.0.1")
        ctx.patch(gm, "GPSD_PORT", self.gpsd.port)
        ctx.patch(gm, "GPSD_CONNECT_TIMEOUT", 0.5)
        ctx.patch(gm, "GPSD_DEFAULTS_FILE", os.path.join(ctx.tmp, "no-gpsd-defaults"))
        ctx.patch(gm, "GPS_ANTENNA_WATCH", antenna_watch)
        ctx.patch(gm.GpsManager, "_user_configured", False)
        self.gps = gm.GpsManager(device="never-open-sim-device")
        ctx.cleanup(self.gps.close)
        self.events = []          # (level, text) health events, in order
        self.link = []            # gpsd link events ("lost", "restored", ...)
        check(self.gps._try_open_gpsd(), "the game could not connect to the fake gpsd")
        check(self.gpsd.wait_clients(1), "gpsd never saw the game's WATCH")

    def pump(self, quiet=0.03):
        """Read until the sockets have been quiet for ``quiet`` seconds."""
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline:
            socks = [s for s in [self.gps._gpsd_sock] + [e[0] for e in self.gps._raw_socks.values()] if s]
            self.gps.read_available()
            event = self.gps.gpsd_reconnect_tick()
            if event:
                self.link.append(event)
            self.events.extend(self.gps.drain_health_events())
            if not socks:
                return
            try:
                ready, _, _ = select.select(socks, [], [], quiet)
            except (OSError, ValueError):
                ready = []
            if not ready:
                return

    def step(self, count=1, seconds=1.0, replay=False):
        for _ in range(count):
            self.clock.advance(seconds)
            self.gpsd.emit(replay=replay)
            self.pump()

    def idle(self, count=1, seconds=1.0):
        """Time passes and the game keeps polling, but gpsd sends nothing."""
        for _ in range(count):
            self.clock.advance(seconds)
            self.pump()

    def wait_raw(self, count):
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline:
            self.pump()
            if len(self.gps._raw_socks) >= count and len(self.gpsd.clients("raw")) >= count:
                return
            time.sleep(0.01)
        check(False, "the antenna watch never connected (%d of %d)" % (len(self.gps._raw_socks), count))

    def texts(self, level=None):
        return [text for lvl, text in self.events if level in (None, lvl)]

    def count(self, fragment):
        return sum(1 for text in self.texts() if fragment in text)

    def chosen(self):
        return self.gps.chosen_device

    def device(self, path):
        for row in self.gps.health()["devices"]:
            if row["path"] == path:
                return row
        check(False, "no health row for %s" % path)


def two(ctx, aio=None, usb=None, **kwargs):
    return GpsRig(ctx, [(AIO, dict(dict(lat=50.0, lon=19.0, sats=sats(9, 12)), **(aio or {}))),
                        (USB, dict(dict(lat=50.0001, lon=19.0001, sats=sats(8, 11)), **(usb or {})))],
                  **kwargs)


@scenario("gpsd", "two receivers, normal drive")
def two_receivers_normal(ctx):
    rig = two(ctx, aio=dict(drift=0.0001), usb=dict(drift=0.0001))
    last = None
    for _ in range(40):
        rig.step()
        sample = rig.gps.wardrive_sample()
        check(sample is not None, "no measured position while both receivers have a fix")
        if last is not None:
            jump = abs(sample["lat"] - last)
            check(jump < 0.00015, "the position jumped between the receivers (%.6f deg)" % jump)
        last = sample["lat"]
    health = rig.gps.health()
    check_equal(len(health["devices"]), 2, "receivers on the health screen")
    check_equal(health["switches"], 0, "source switches on a normal drive")
    check_equal(sum(1 for row in health["devices"] if row["in_use"]), 1, "receivers marked in use")
    check(rig.gps.fix.valid and rig.gps.fix.satellites == rig.device(rig.chosen())["used"],
          "the HUD satellite count is not the chosen receiver's")
    check_equal(rig.texts("warn") + rig.texts("alarm"), [], "alerts on a normal drive")
    check_equal(rig.count("[GPS] using "), 1, "the 'using <receiver>' line")


@scenario("gpsd", "one receiver drops its fix mid-drive")
def one_drops_mid_drive(ctx):
    rig = two(ctx)
    rig.step(5)
    first = rig.chosen()
    other = USB if first == AIO else AIO
    rig.gpsd.set(first, mode=1, sats=sats(0, 3))
    rig.step(1)
    check_equal(rig.chosen(), other, "receiver in use right after the first one lost its fix")
    check_equal(rig.count("switched to"), 1, "switch lines")
    check("lost its fix" in [t for t in rig.texts() if "switched to" in t][0], "the switch reason is missing")
    rig.step(1)
    check(rig.gps.wardrive_sample() is not None, "no position from the surviving receiver")
    rig.step(4)
    check_equal(rig.count("lost its fix; another receiver still has one"), 1, "'lost its fix' alerts")
    # the fault clears: the first receiver is back but only equal, so no flapping
    rig.gpsd.set(first, mode=3, sats=sats(8, 11))
    rig.step(30)
    check_equal(rig.chosen(), other, "receiver in use after the first one recovered (no flapping)")
    check_equal(rig.count("switched to"), 1, "switch lines after recovery")
    check_equal(rig.count("has a fix again"), 1, "'has a fix again' lines")
    check(rig.gps.fix.valid, "fix flag after recovery")


@scenario("gpsd", "a clearly better receiver wins only after 10 s")
def hysteresis(ctx):
    rig = two(ctx, aio=dict(sats=sats(5, 9), hdop=2.4), usb=dict(sats=sats(5, 9), hdop=2.4))
    rig.step(5)
    current = rig.chosen()
    other = USB if current == AIO else AIO
    rig.gpsd.set(other, sats=sats(11, 14), hdop=0.8)
    rig.step(9)
    check_equal(rig.chosen(), current, "receiver in use before the 10 s hold is over")
    rig.gpsd.set(other, sats=sats(5, 9), hdop=2.4)       # the advantage goes away: the hold restarts
    rig.step(3)
    rig.gpsd.set(other, sats=sats(11, 14), hdop=0.8)
    rig.step(9)
    check_equal(rig.chosen(), current, "receiver in use: an interrupted advantage must not add up")
    rig.step(3)
    check_equal(rig.chosen(), other, "receiver in use after 10 unbroken seconds clearly better")
    switches = [t for t in rig.texts() if "switched to" in t]
    check_equal(len(switches), 1, "switch lines")
    check("better for 10 s" in switches[0] and "satellites used" in switches[0], "reason: " + switches[0])


@scenario("gpsd", "gpsd restarts")
def gpsd_restarts(ctx):
    rig = two(ctx)
    rig.step(5)
    before = rig.gps.wardrive_sample()
    check(before is not None, "no position before the restart")
    rig.gpsd.stop()
    rig.idle(2)
    check_equal(rig.link, ["lost"], "link events after gpsd went away")
    check(not rig.gps.available and rig.gps.wardrive_sample() is None, "a position survived the drop")
    rig.gpsd.start()
    deadline = time.perf_counter() + 8.0
    while "restored" not in rig.link and time.perf_counter() < deadline:
        rig.clock.advance(1.0)
        rig.pump()
        time.sleep(0.05)
    check_equal(rig.link, ["lost", "restored"], "link events after gpsd came back")
    check(rig.gpsd.wait_clients(1), "the game did not WATCH the restarted gpsd")
    rig.step(1, replay=True)      # gpsd replays its last report to a new client
    check(rig.gps.wardrive_sample() is None, "a replayed report was accepted as a fresh position")
    rig.step(2)
    check(rig.gps.wardrive_sample() is not None and rig.gps.fix.valid, "no position after the reconnect")
    check_equal(rig.count("switched to"), 0, "switch lines across a restart")


@scenario("gpsd", "stale and replayed reports")
def stale_and_replayed(ctx):
    rig = two(ctx)
    rig.step(3)
    check(rig.gps.wardrive_sample() is not None, "no position")
    for _ in range(4):
        rig.step(1, replay=True)
    check(rig.gps.wardrive_sample() is None,
          "replayed reports kept a 4-second-old position looking fresh")
    rig.step(1)
    check(rig.gps.wardrive_sample() is not None, "a new epoch after replays was not accepted")
    # every receiver goes silent while gpsd keeps the socket open
    for path in (AIO, USB):
        rig.gpsd.set(path, silent=True)
    rig.idle(8)
    check(not rig.gps.fix.valid, "the fix flag stayed up 8 s after the receivers went silent")
    check(rig.gps.wardrive_sample() is None, "a stale position was served")
    for path in (AIO, USB):
        rig.gpsd.set(path, silent=False)
    rig.step(2)
    check(rig.gps.fix.valid and rig.gps.wardrive_sample() is not None, "no recovery after the silence")
    rig.gpsd.send_text('{"class":"TPV","device":"%s","mode":3,"lat":"x"}\nnot json at all\n{"class":"SKY"\n' % AIO)
    rig.step(2)
    check(rig.gps.available and rig.gps.wardrive_sample() is not None, "garbage from gpsd broke the reader")


@scenario("gpsd", "reports without a time field")
def no_time_field(ctx):
    rig = two(ctx, aio=dict(timeless=True), usb=dict(timeless=True))
    rig.step(5)
    check(rig.gps.fix.valid, "a fix without a time should still place the player")
    check(rig.gps.wardrive_sample() is None,
          "a fix with no time cannot be told from a replay and must not be a measured sample")
    for path in (AIO, USB):
        rig.gpsd.set(path, timeless=False)
    rig.step(2)
    check(rig.gps.wardrive_sample() is not None, "no sample once the time field is back")


@scenario("gpsd", "antenna SHORT and OPEN")
def antenna_short_open(ctx):
    rig = two(ctx, aio=dict(antenna="OK", antenna_style="at6558"), antenna_watch=True)
    rig.step(2)
    rig.wait_raw(2)
    rig.step(2)
    check_equal(rig.device(AIO)["antenna"], "OK", "GP-02 antenna status")
    check_equal(rig.device(USB)["antenna"], "not reported", "dongle antenna status (it sends none)")
    check(not rig.device(USB)["antenna_reported"], "a receiver that reports nothing must not read OK")
    rig.gpsd.set(AIO, antenna="SHORT")
    rig.step(5)
    shorts = [t for t in rig.texts("alarm") if "ANTENNA SHORT" in t]
    check_equal(len(shorts), 1, "SHORT alarms while the short lasts")
    check("check cable now" in shorts[0], "the SHORT alarm must say 'check cable now'")
    rig.gpsd.set(AIO, antenna="OK")
    rig.step(3)
    check_equal(rig.count("antenna short cleared"), 1, "'short cleared' lines")
    rig.gpsd.set(AIO, antenna="SHORT")
    rig.step(3)
    check_equal(len([t for t in rig.texts("alarm") if "ANTENNA SHORT" in t]), 2, "SHORT alarm re-armed")
    rig.gpsd.set(AIO, antenna="OPEN")
    rig.step(4)
    check_equal(rig.count("antenna OPEN"), 1, "OPEN alerts")
    check_equal(rig.device(AIO)["antenna"], "OPEN", "GP-02 antenna status")
    rig.gpsd.set(USB, antenna="OPEN", antenna_style="ublox")
    rig.step(3)
    check_equal(rig.device(USB)["antenna"], "OPEN", "dongle antenna status (u-blox form)")
    check_equal(rig.count("antenna OPEN"), 2, "OPEN alerts, one per receiver")


@scenario("gpsd", "one weak antenna")
def one_weak_antenna(ctx):
    rig = two(ctx, aio=dict(sats=sats(8, 11, snr=38.0)), usb=dict(sats=sats(8, 11, snr=24.0)))
    rig.step(55)
    check_equal(rig.count("weak antenna?"), 0, "weak-antenna alerts before 60 s")
    rig.step(10)
    weak = [t for t in rig.texts("warn") if "weak antenna?" in t]
    check_equal(len(weak), 1, "weak-antenna alerts after 60 s")
    check("USB" in weak[0] and "24 dB vs 38 dB" in weak[0], "wrong receiver or numbers: " + weak[0])
    rig.step(60)
    check_equal(rig.count("weak antenna?"), 1, "the alert must not repeat while it lasts")
    rig.gpsd.set(USB, sats=sats(8, 11, snr=37.0))
    rig.step(3)
    check_equal(rig.count("signal back in line"), 1, "cleared line")
    # half as many satellites is the other trigger
    rig.gpsd.set(USB, sats=sats(4, 5, snr=37.0))
    rig.step(65)
    check_equal(rig.count("weak antenna?"), 2, "weak-antenna alert on the satellite-count rule")
    check("sees 5 satellites vs 11" in rig.texts("warn")[-1], rig.texts("warn")[-1])


@scenario("gpsd", "both receivers lose their fix")
def both_lose_fix(ctx):
    rig = two(ctx)
    rig.step(5)
    for path in (AIO, USB):
        rig.gpsd.set(path, mode=1, sats=sats(0, 2))
    rig.step(1)          # they drop within the same second; one may be seen to drop first
    current = rig.chosen()
    rig.step(20)
    check(not rig.gps.fix.valid and rig.gps.wardrive_sample() is None, "a position without any fix")
    check_equal(rig.chosen(), current, "receiver in use while nobody has a fix (no flapping)")
    check(rig.count("switched to") <= 1, "more than one switch while both receivers were losing their fix")
    check_equal(rig.count("another receiver still has one"), 0, "'lost while the other keeps one' alerts")
    for path in (AIO, USB):
        rig.gpsd.set(path, mode=3, sats=sats(9, 12))
    rig.step(2)
    check(rig.gps.fix.valid and rig.gps.wardrive_sample() is not None, "no recovery")


@scenario("gpsd", "dongle unplugged and replugged")
def dongle_unplugged(ctx):
    rig = two(ctx, aio=dict(sats=sats(6, 9), hdop=2.0), usb=dict(sats=sats(11, 14), hdop=0.8),
              antenna_watch=True)
    rig.step(15)
    check_equal(rig.chosen(), USB, "the better receiver (dongle) should be in use")
    rig.wait_raw(2)
    rig.gpsd.unplug(USB)
    rig.pump()
    rig.step(2)
    check_equal(rig.chosen(), AIO, "receiver in use after the dongle was pulled")
    check(rig.gps.wardrive_sample() is not None, "no position from the GP-02 after the dongle went")
    rig.step(10)
    check_equal(rig.count("stopped reporting"), 2, "lines naming the unplugged dongle (switch + alert)")
    check_equal(len([t for t in rig.texts("warn") if "stopped reporting" in t]), 1, "'stopped reporting' alerts")
    rig.gpsd.replug(USB)
    rig.step(5)
    check_equal(rig.count("is reporting again"), 1, "'reporting again' lines")
    check_equal(rig.chosen(), AIO, "no switch back before the 10 s hold")
    rig.step(12)
    check_equal(rig.chosen(), USB, "back on the better receiver after the hold")
    check(rig.gps.fix.valid and rig.gps.wardrive_sample() is not None, "no position at the end")


@scenario("gpsd", "gpsd owns the ports: the game never opens them")
def never_opens_gpsd_ports(ctx):
    from unittest import mock
    from watchdogs import gps_manager as gm
    defaults = os.path.join(ctx.tmp, "gpsd-defaults")
    with open(defaults, "w", encoding="utf-8") as fh:
        fh.write('# test\nSTART_DAEMON="true"\nGPSD_OPTIONS="-n"\nDEVICES="/dev/ttyAMA0 /dev/ttyACM7"\nUSBAUTO="true"\n')
    ctx.patch(gm, "GPSD_DEFAULTS_FILE", defaults)
    ctx.patch(gm, "GPSD_HOST", "127.0.0.1")
    ctx.patch(gm, "GPSD_PORT", 1)                    # nothing listens: gpsd is "not up yet"
    ctx.patch(gm, "GPSD_CONNECT_TIMEOUT", 0.3)
    ctx.patch(gm.GpsManager, "_user_configured", False)
    opened = []
    def fake_serial(*args, **kwargs):
        opened.append(kwargs.get("port") or args)
        return mock.Mock()
    with mock.patch.object(gm.shutil, "which", return_value="/usr/sbin/gpsd"), \
            mock.patch.object(gm.serial, "Serial", fake_serial), \
            mock.patch.object(gm.os.path, "exists", return_value=True), \
            mock.patch.object(gm.os.path, "realpath", side_effect=lambda path: path), \
            mock.patch.object(gm.os, "access", return_value=True), \
            mock.patch.object(gm.glob, "glob", return_value=["/dev/ttyACM7", "/dev/ttyAMA0"]):
        gps = gm.GpsManager(device="/dev/ttyAMA0")
        ctx.cleanup(gps.close)
        check_equal(gps.gpsd_owned_uart(), "/dev/ttyAMA0", "UART gpsd owns")
        check(not gps.setup(), "setup() claimed a GPS although gpsd is not answering")
        check(gps.gpsd_retrying, "the game is not waiting for gpsd")
        check(not gps._try_open("/dev/ttyAMA0"), "the 5 s UART retry opened gpsd's port")
        check(not gps._try_open("/dev/ttyACM7"), "the game opened the dongle gpsd owns")
        check_equal(opened, [], "serial ports opened while gpsd owns the receivers")
        # a receiver gpsd does NOT own may still be opened directly
        check(gps._try_open("/dev/ttyUSB9"), "an unclaimed serial GPS could not be opened")
        check_equal(len(opened), 1, "direct opens of an unclaimed port")
