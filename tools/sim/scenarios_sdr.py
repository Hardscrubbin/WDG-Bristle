"""[merge-v3] RTL-SDR scenarios: ADS-B (upstream's real SBS reader against a fake
dump1090 feed), 433 capture -> Flipper .sub, and the 433 Hunter."""
import os
import random
import socket
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.sim import flipper_sub, net
from tools.sim.core import check, check_equal, scenario

FRAME_LIMIT = 0.25


# ----------------------------------------------------------------------
# ADS-B
# ----------------------------------------------------------------------

class FakeDump1090:
    """A dump1090 SBS (BaseStation, port 30003) feed on a free local port."""

    def __init__(self):
        self.listener = net.listen(0, backlog=4)        # the port stays open for the whole scenario
        self.port = self.listener.getsockname()[1]
        self.clients = []
        self.open = True
        self.accepting = True
        self.thread = threading.Thread(target=self._accept, name="fake-dump1090", daemon=True)
        self.thread.start()

    def _accept(self):
        while self.open:
            try:
                sock, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self.accepting:
                self.clients.append(sock)
            else:
                sock.close()

    def send(self, lines):
        data = ("\r\n".join(lines) + "\r\n").encode("ascii", errors="replace")
        for sock in list(self.clients):
            try:
                sock.sendall(data)
            except OSError:
                self.clients.remove(sock)

    def send_raw(self, data):
        for sock in list(self.clients):
            try:
                sock.sendall(data)
            except OSError:
                self.clients.remove(sock)

    def drop_clients(self):
        clients, self.clients = self.clients, []
        for sock in clients:
            sock.close()

    def close(self):
        self.open = False
        self.drop_clients()
        net.close_listener(self.listener, self.thread)


def sbs(icao, lat, lon, alt=30000, callsign=""):
    lines = []
    if callsign:
        lines.append("MSG,1,1,1,%s,1,2026/11/21,12:00:00.000,2026/11/21,12:00:00.000,%s,,,,,,,,,,,0"
                     % (icao, callsign))
    lines.append("MSG,3,1,1,%s,1,2026/11/21,12:00:00.000,2026/11/21,12:00:00.000,,%d,,,%.5f,%.5f,,,,,,0"
                 % (icao, alt, lat, lon))
    return lines


class AdsbRig:
    def __init__(self, ctx):
        from watchdogs import sdr_manager
        from test_scan_session_map import loot_at
        self.feed = FakeDump1090()
        ctx.cleanup(self.feed.close)
        port = self.feed.port
        real_socket = socket.socket

        class Redirect(real_socket):
            def connect(self, address):
                return super().connect(("127.0.0.1", port) if address == ("127.0.0.1", 30003) else address)
        ctx.patch(socket, "socket", Redirect)
        position = SimpleNamespace(available=True, fix=SimpleNamespace(valid=True, latitude=50.06, longitude=19.94,
                                                                       altitude=210.0, hdop=1.0))
        manager = loot_at(ctx.tmp, "2026-11-21T12:00:00", position)
        self.loot = manager.__enter__()
        ctx.cleanup(manager.__exit__, None, None, None)
        self.session = Path(self.loot.session_dir)
        self.sdr = sdr_manager.SDRManager()
        self.sdr._adsb_log = open(self.session / "adsb_aircraft.csv", "a")
        self.sdr._adsb_log.write("timestamp,icao,callsign,lat,lon,alt_ft,speed_kt,heading,squawk\n")
        self.sdr.update_gps(50.06, 19.94)
        self.sdr.running, self.sdr.mode = True, "adsb"
        self.reader = threading.Thread(target=self.sdr._sbs_reader, name="sim-sbs", daemon=True)
        self.reader.start()
        ctx.cleanup(self._stop)

    def _stop(self):
        self.sdr._stop_event.set()
        self.reader.join(5)
        try:
            self.sdr._adsb_log.close()
        except Exception:
            pass

    def wait_connected(self, timeout=10.0):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self.feed.clients:
                return True
            time.sleep(0.02)
        return False

    def wait(self, predicate, timeout=10.0):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def rows(self):
        self.sdr._adsb_log.flush()
        lines = (self.session / "adsb_aircraft.csv").read_text(encoding="utf-8").splitlines()
        return [line.split(",") for line in lines[1:]]

    def export_kml(self, ctx):
        import plugins.ng_adsb_tracks as ng_adsb
        hud, term = [], []
        plugin = ng_adsb.NgAdsbTracks()
        plugin.on_load(SimpleNamespace(
            loot=self.loot, _sdr=self.sdr, gps_fix=True, gps=SimpleNamespace(wardrive_sample=lambda: dict(lat=50.06, lon=19.94)),
            msg=lambda text, colour=7: hud.append(text), _term_add=lambda text, raw=False: term.append(text),
            _esc_consumed_frame=-1))
        self.sdr._adsb_log.flush()
        plugin.export_kml()
        plugin._thread.join(30)
        check(not plugin._thread.is_alive(), "the KML export never finished")
        plugin._rx_next = float("inf")
        plugin.on_update()
        return self.session / "adsb_tracks.kml", hud, term


@scenario("adsb", "hundreds of aircraft; count = distinct ICAO; KML parses")
def adsb_many(ctx):
    rig = AdsbRig(ctx)
    check(rig.wait_connected(), "C5's SBS reader never connected to the feed")
    aircraft = 320
    for step in range(5):
        lines = []
        for n in range(aircraft):
            icao = "%06X" % (0x400000 + n * 257)
            lines += sbs(icao, 49.0 + (n % 40) * 0.05 + step * 0.01, 18.0 + (n // 40) * 0.2 + step * 0.01,
                         3000 + n * 100, "SIM%d" % n if n % 3 else "")
        rig.feed.send(lines)
    check(rig.wait(lambda: len(rig.rows()) >= aircraft * 5, timeout=30),       # 5 position messages each
          "the track log is short: %d rows for %d aircraft x 5 positions" % (len(rig.rows()), aircraft))
    rows = rig.rows()
    check_equal(len({row[1] for row in rows if len(row) > 1}), aircraft, "distinct aircraft in the log")
    check_equal(rig.loot._adsb_aircraft_count(rig.session / "adsb_aircraft.csv"), aircraft,
                "aircraft counted for the session (distinct ICAO, not rows)")
    check(all(len(row) == 9 for row in rows), "a row of the ADS-B log does not have 9 columns")
    kml, hud, term = rig.export_kml(ctx)
    check(kml.is_file(), "no KML written: %s" % (hud + term)[-3:])
    root = ET.parse(kml).getroot()                         # must be well-formed XML
    check(root.tag.endswith("kml"), "the export is not a KML document")
    namespace = {"k": root.tag[1:].split("}")[0]}
    placemarks = root.findall(".//k:Placemark", namespace)
    lines_found = root.findall(".//k:LineString/k:coordinates", namespace)
    check(len(lines_found) >= aircraft, "tracks in the KML: %d for %d aircraft" % (len(lines_found), aircraft))
    check(len(placemarks) >= aircraft, "placemarks in the KML: %d" % len(placemarks))
    for coordinates in lines_found[:50]:
        for triple in coordinates.text.split():
            lon, lat, _alt = (float(v) for v in triple.split(","))
            check(18 <= lon <= 20.5 and 48.9 <= lat <= 51.1, "a KML coordinate is off the track: %s" % triple)
    check(any("KML saved" in line for line in term + hud), "no message for the export")


@scenario("adsb", "garbage lines")
def adsb_garbage(ctx):
    rig = AdsbRig(ctx)
    check(rig.wait_connected(), "reader not connected")
    random.seed(7)
    garbage = ["", "MSG", "MSG,3", "MSG,3,1,1,,1,,,,,,,,,,,", "MSG,3,1,1,ZZZZZZ,1,a,b,c,d,,x,,,lat,lon,,,,,,0",
               "MSG,3,1,1,ABCDEF,1,,,,,,99999999999999999999,,,91.5,-999,,,,,,0", "SEL,,,,", "\x00\x01\x02",
               "MSG,3,1,1,ABC123,1,,,,,,nan,,,nan,inf,,,,,,0", "MSG," * 4000, "MSG,3,1,1,ABC,DEF,GHI"]
    for _ in range(3):
        rig.feed.send(garbage)
        rig.feed.send_raw(bytes(random.randrange(256) for _ in range(4000)) + b"\r\n")
        rig.feed.send_raw(b"MSG,3,1,1,4CA7B5,1,2026/11/21,12:00:00.000,2026/11/21,12:00:00.000,,36000,,,50.1")
        rig.feed.send_raw(b"0000,19.20000,,,,,,0\r\n")                      # a row split across packets
    rig.feed.send(sbs("48AE21", 50.4, 19.6, 9000, "LOT3924"))
    check(rig.wait(lambda: any(row[1] == "48AE21" for row in rig.rows() if len(row) > 1)),
          "a valid aircraft after the garbage was not logged")
    check(rig.reader.is_alive(), "garbage killed the SBS reader")
    good = [row for row in rig.rows() if len(row) == 9]
    check_equal(len(good), len(rig.rows()), "rows with 9 columns (garbage must not reach the log)")
    for row in good:
        lat, lon = float(row[3]), float(row[4])
        check(-90 <= lat <= 90 and -180 <= lon <= 180, "an impossible position was logged: %s" % row)
    count = rig.loot._adsb_aircraft_count(rig.session / "adsb_aircraft.csv")
    check(2 <= count <= 4, "aircraft counted after garbage: %d" % count)


@scenario("adsb", "feed stops and comes back", limit=90)
def adsb_feed_stops(ctx):
    rig = AdsbRig(ctx)
    check(rig.wait_connected(), "reader not connected")
    rig.feed.send(sbs("4CA7B5", 50.0, 19.0, 36000, "RYR81XX"))
    check(rig.wait(lambda: len(rig.rows()) >= 1), "first aircraft")
    rig.feed.accepting = False
    rig.feed.drop_clients()                                 # dump1090 dies
    time.sleep(1.0)
    check(rig.reader.is_alive(), "the reader gave up when the feed stopped")
    before = len(rig.rows())
    kml, _hud, _term = rig.export_kml(ctx)                  # exporting with a dead feed still works
    check(kml.is_file(), "no KML while the feed is down")
    ET.parse(kml)
    rig.feed.accepting = True
    check(rig.wait_connected(timeout=15), "the reader did not reconnect when the feed came back")
    rig.feed.send(sbs("48AE21", 50.4, 19.6, 9000, "LOT3924"))
    check(rig.wait(lambda: len(rig.rows()) > before), "nothing logged after the feed came back")
    check_equal(rig.loot._adsb_aircraft_count(rig.session / "adsb_aircraft.csv"), 2, "aircraft")
    header = (rig.session / "adsb_aircraft.csv").read_text(encoding="utf-8").count("timestamp,icao")
    check_equal(header, 1, "header lines in the log")


# ----------------------------------------------------------------------
# 433 capture -> Flipper .sub
# ----------------------------------------------------------------------

class CaptureRig:
    def __init__(self, ctx):
        import test_v3_ng_plugins as ng
        import plugins.ng_subghz_capture as ng_433
        from watchdogs import sdr_manager
        from test_scan_session_map import loot_at
        self.ng, self.ng_433 = ng, ng_433
        ng.FakeProcess.launched = []
        ctx.patch(sdr_manager.subprocess, "Popen", ng.FakeProcess)
        ctx.patch(sdr_manager.SDRManager, "has_rtl433", staticmethod(lambda: True))
        ctx.patch(ng_433, "WATCH_INTERVAL", 0.02)
        ctx.patch(ng_433, "QUIET_SECONDS", 0.06)        # the game's own value is 1 s; the rule is the same
        ctx.patch(ng_433, "SETTLE_INTERVAL", 0.02)
        position = SimpleNamespace(available=True, fix=SimpleNamespace(valid=True, latitude=50.0, longitude=19.0,
                                                                       altitude=210.0, hdop=1.0))
        manager = loot_at(ctx.tmp, "2026-11-21T12:00:00", position)
        self.loot = manager.__enter__()
        ctx.cleanup(manager.__exit__, None, None, None)
        self.sdr = sdr_manager.SDRManager()
        self.hud, self.term = [], []
        game = ng.app_methods("_sdr_433")()                 # upstream's real 433 menu action
        game._sdr, game.loot = self.sdr, self.loot
        game._sdr_enabled, game._aio_available = True, False
        game.player_lat, game.player_lon = 50.0, 19.0
        game.msg = lambda text, colour=7: self.hud.append(text)
        game._term_add = lambda text, raw=False: self.term.append(text)
        game._earn_badge = lambda *a, **k: None
        game._esc_consumed_frame = -1
        self.game = game
        self.plugin = ng_433.NgSubGhzCapture()
        self.plugin.on_load(game)
        ctx.cleanup(self._shutdown)

    def _shutdown(self):
        self.plugin.on_unload()
        thread = self.plugin._watch_thread
        if thread is not None:
            thread.join(10)
        self.sdr.stop()

    def wait(self, predicate, timeout=10.0):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            self.plugin.on_update_safe() if hasattr(self.plugin, "on_update_safe") else None
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def folder(self):
        return Path(self.plugin._capture_dir)


SIGNALS = {"g001_433.92M_250k.cu8": [500, -500, 1000, -500, 500, -1500, 500],
           "g002_433.92M_250k.cu8": [350, -1050] * 24 + [350, -10000, 350],
           "g003_433.92M_250k.cu8": [120, -240] * 700}                     # 1400 timings: several lines


@scenario("433", "captures become valid Flipper .sub files")
def capture_normal(ctx):
    flipper_sub.validate(flipper_sub.REFERENCE)                              # the checker accepts the reference
    reference_header, _ = flipper_sub.parse(flipper_sub.REFERENCE)
    rig = CaptureRig(ctx)
    check(rig.plugin.start_capture(), "capture did not start: %s" % rig.hud[-2:])
    check(any("Capture ON" in line for line in rig.hud + rig.term), "no 'capture on' message")
    for name, pulses in SIGNALS.items():
        rig.ng.write_cu8(rig.folder() / name, pulses)
    check(rig.wait(lambda: all((rig.folder() / name).with_suffix(".sub").is_file() for name in SIGNALS)),
          "not every recording was converted: %s" % sorted(p.name for p in rig.folder().iterdir()))
    for name, pulses in SIGNALS.items():
        text = (rig.folder() / name).with_suffix(".sub").read_text(encoding="utf-8")
        timings = flipper_sub.validate(text)                                # raises with the reason if not valid
        header, _ = flipper_sub.parse(text)
        check_equal(list(header), list(reference_header), "%s: header keys, in order" % name)
        for key in ("Filetype", "Version", "Protocol", "Preset", "Frequency"):
            check_equal(header[key], reference_header[key], "%s: %s" % (name, key))
        if pulses[-1] < 0:
            pulses = pulses[:-1]                 # a gap after the last pulse is silence, not a timing
        check_equal(len(timings), len(pulses), "%s: number of timings" % name)
        worst = max(abs(got - want) for got, want in zip(timings, pulses))
        check(worst <= 8, "%s: timings off by up to %d us (2 samples at 250 kHz is 8 us)" % (name, worst))
        check((rig.folder() / name).is_file(), "%s: the raw recording was not kept" % name)
    rig.plugin.stop_capture()
    check(any("Capture OFF" in line or "stopped" in line.lower() for line in rig.hud + rig.term),
          "no message when the capture stopped: %s" % (rig.hud + rig.term)[-3:])


@scenario("433", "no captures, corrupt and odd files")
def capture_corrupt(ctx):
    rig = CaptureRig(ctx)
    # Every capture of this scenario starts "in the same second". On the
    # uConsole that happened by chance about one run in four: the second
    # capture then took over the first one's folder and converted its
    # recordings again. Forced here, so the case is tested on every run.
    real_folder = rig.ng_433.new_capture_dir
    ctx.patch(rig.ng_433, "new_capture_dir", lambda parent, stamp=None: real_folder(parent, "2026-11-21_12-00-00"))
    check(rig.plugin.start_capture(), "capture did not start")
    time.sleep(0.3)
    check_equal([p.name for p in rig.folder().glob("*.sub")], [], ".sub files with nothing recorded")
    bad = {"g010_433.92M_250k.cu8": b"", "g011_433.92M_250k.cu8": b"\x7f",
           "g012_433.92M_250k.cu8": bytes((127, 128)) * 6000,                 # silence
           "g013_433.92M_250k.cu8": os.urandom(30001),                        # noise, odd length
           "notes.cu8": bytes((255, 128)) * 500}                              # no frequency in the name
    for name, data in bad.items():
        (rig.folder() / name).write_bytes(data)
    rig.ng.write_cu8(rig.folder() / "g014_433.92M_250k.cu8", SIGNALS["g001_433.92M_250k.cu8"])
    check(rig.wait(lambda: (rig.folder() / "g014_433.92M_250k.sub").is_file()),
          "a good recording after the bad ones was not converted (the watcher died?)")
    time.sleep(0.3)
    check(rig.plugin._watch_thread.is_alive(), "a corrupt file killed the capture watcher")
    for path in rig.folder().glob("*.sub"):
        flipper_sub.validate(path.read_text(encoding="utf-8"))               # whatever was written is valid
    for name in ("g010_433.92M_250k", "g011_433.92M_250k", "g012_433.92M_250k"):
        check(not (rig.folder() / (name + ".sub")).exists(), "%s: a .sub was written for an empty signal" % name)
    for name in bad:
        check((rig.folder() / name).is_file(), "%s: the raw file was removed" % name)
    # A recording that is still being written when the capture is stopped:
    # every time the watcher looks at it, more has arrived. (A writer thread
    # on a timer was used here first. It depended on which thread the system
    # ran when, and failed about once in 200 runs when the writer happened to
    # pause for longer than the quiet time. "Bigger at every look" says the
    # same thing and does not depend on timing.)
    growing = rig.folder() / "g015_433.92M_250k.cu8"
    rig.ng.write_cu8(growing, SIGNALS["g002_433.92M_250k.cu8"])
    real_state = rig.ng_433._file_state
    looks = []

    def still_growing(path):
        if os.path.basename(path) == growing.name:
            looks.append(1)
            with open(path, "ab") as fh:
                fh.write(bytes((127, 128)) * 64)
        return real_state(path)
    ctx.patch(rig.ng_433, "_file_state", still_growing)
    ctx.patch(rig.ng_433, "FINAL_WAIT", 0.6)
    time.sleep(0.15)                                   # several looks while the capture runs
    check(not growing.with_suffix(".sub").exists(), "a .sub was written while the recording was growing")
    first_folder, first_thread = rig.folder(), rig.plugin._watch_thread
    rig.plugin.stop_capture()
    first_thread.join(15)
    check(not first_thread.is_alive(), "the watcher did not finish after the capture was stopped")
    check(len(looks) >= 4, "the watcher hardly looked at the growing recording (%d looks)" % len(looks))
    check(not growing.with_suffix(".sub").exists(), "a .sub was written from a recording that was still growing")
    check(any(row[0] == growing.name and "still being written" in row[3] for row in rig.plugin._rows),
          "the unfinished recording is not explained in the list: %s" % rig.plugin._rows[-2:])
    check(any(row[0] == "g010_433.92M_250k.cu8" and "empty" in row[3] for row in rig.plugin._rows),
          "the empty recording is not explained in the list")
    check(not list(first_folder.glob("*.part")), "a half-written .sub was left behind")
    before = sorted(p.name for p in first_folder.iterdir())
    # start / stop with nothing at all, in the same session and the same second
    rig2 = CaptureRig(ctx)
    check(rig2.plugin.start_capture(), "second capture did not start")
    check(rig2.folder() != first_folder, "the second capture took over the first one's folder")
    second_thread = rig2.plugin._watch_thread
    rig2.plugin.stop_capture()
    second_thread.join(15)
    check(not second_thread.is_alive(), "the second capture's watcher did not finish")
    check_equal(sorted(p.name for p in rig2.folder().iterdir()), [], "files in the folder of an empty capture")
    check_equal((rig2.plugin._rows, rig2.plugin._converted, rig2.plugin._skipped), ([], 0, 0),
                "results of an empty capture")
    check_equal(sorted(p.name for p in first_folder.iterdir()), before,
                "the first capture's folder after the second capture")


# ----------------------------------------------------------------------
# 433 Hunter
# ----------------------------------------------------------------------

FAKE_RTL_POWER = r'''
import sys, time
mode = sys.argv[1]
bins = 150
low, step = 433400000.0, 8000.0
target = int((434415000 - low) / step)
row = 0
while True:
    values = [-40.0 + (i % 7) * 0.3 for i in range(bins)]
    values[64] = -12.0                                   # a constant carrier at ~433.916 MHz
    if mode == "burst" and row >= 6 and row % 2 == 0:
        values[target] = -40.0 + min(45.0, 20.0 + row)   # the remote, getting closer
    if mode == "learn" and row >= 6:
        values[90] = -5.0                                # a different remote at ~434.124 MHz
    sys.stdout.write("2026-11-21, 12:00:%02d, %d, %d, %.2f, 1, %s\n"
                     % (row % 60, low, low + bins * step, step, ", ".join("%.2f" % v for v in values)))
    sys.stdout.flush()
    row += 1
    if mode == "die" and row == 5:
        sys.exit(3)
    time.sleep(0.03)
'''


class HunterRig:
    def __init__(self, ctx, mode="burst", sdr_running=False):
        import plugins.ng_433_hunter as hunter
        import test_v3_ng_plugins as ng
        self.module, self.ng = hunter, ng
        script = Path(ctx.tmp) / "fake_rtl_power.py"
        script.write_text(FAKE_RTL_POWER, encoding="utf-8")
        self.canvas = ng.Canvas()
        old = sys.modules.get("pyxel")
        sys.modules["pyxel"] = self.canvas
        ctx.cleanup(self._restore, old)
        self.hud, self.term, self.beeps = [], [], []
        self.loot = SimpleNamespace(active=True, session_dir=ctx.tmp)
        self.app = SimpleNamespace(
            _app_dir=ctx.tmp, loot=self.loot, _sdr=SimpleNamespace(running=sdr_running, mode="adsb"),
            _aio_available=True, _sdr_enabled=True, gps_fix=True, player_lat=50.0, player_lon=19.0,
            msg=lambda text, colour=7: self.hud.append(text),
            _term_add=lambda text, raw=False: self.term.append(text),
            _play_subghz_beep=lambda: self.beeps.append(time.perf_counter()), _esc_consumed_frame=-1)
        self.plugin = hunter.Ng433Hunter()
        self.plugin.on_load(self.app)
        self.plugin.command = lambda: [sys.executable, "-I", str(script), mode]
        ctx.patch(hunter.shutil, "which", lambda name: name)
        ctx.cleanup(self.plugin.on_unload)
        self.frame_times = []
        self.plugin.open_overlay()

    @staticmethod
    def _restore(old):
        if old is None:
            sys.modules.pop("pyxel", None)
        else:
            sys.modules["pyxel"] = old

    def frame(self, *keys):
        self.canvas.pressed = set(keys)
        self.canvas.frame_count += 1
        started = time.perf_counter()
        self.plugin.on_update()
        self.canvas.new_frame()
        self.plugin.draw(0, 0, self.ng.W, self.ng.H)
        self.frame_times.append(time.perf_counter() - started)
        check_equal(self.canvas.overlaps(), [], "texts that run into each other")
        self.canvas.pressed = set()

    def until(self, predicate, timeout=15.0):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            self.frame()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def text(self):
        return "\n".join(self.canvas.texts)


@scenario("433", "Hunter: a remote getting closer")
def hunter_burst(ctx):
    rig = HunterRig(ctx)
    rig.frame("KEY_S")
    check(rig.plugin.hunting, "the hunt did not start: %s" % rig.plugin._status)
    check(rig.until(lambda: rig.plugin.engine.hits >= 3), "no bursts detected: %s" % vars(rig.plugin.engine))
    engine = rig.plugin.engine
    check(abs(engine.peak_mhz - 434.415) < 0.02, "the burst was found at %.3f MHz" % engine.peak_mhz)
    check(rig.until(lambda: rig.plugin.engine.hot >= 0.9), "the bar never filled as the remote came closer")
    check(len(rig.beeps) >= 3, "no beeps while a burst was present")
    check("HUNTING" in rig.text(), "the screen does not say it is hunting")
    log = (Path(ctx.tmp) / "key_hunt.csv").read_text(encoding="utf-8").splitlines()
    check(log[0].startswith("timestamp,peak_freq_mhz") and len(log) > 5, "key_hunt.csv was not written")
    check(all(len(line.split(",")) == 14 for line in log), "a key_hunt.csv row does not have 14 columns")
    rig.frame("KEY_S")
    check(not rig.plugin.hunting, "S did not stop the hunt")
    check(rig.plugin._thread is None or rig.until(lambda: not rig.plugin._thread.is_alive()),
          "the reader thread kept running after stop")
    check(max(rig.frame_times) < FRAME_LIMIT, "the game loop stalled for %.2f s" % max(rig.frame_times))


@scenario("433", "Hunter: learn another remote, SDR busy, rtl_power dies")
def hunter_faults(ctx):
    rig = HunterRig(ctx, mode="learn")
    rig.frame("KEY_S")
    rig.until(lambda: rig.plugin.engine.sweeps >= 4)
    rig.frame("KEY_K")
    check(rig.until(lambda: abs(rig.plugin.engine.target_hz - 434_124_000) < 8_000),
          "the new remote was not learned (target %.0f)" % rig.plugin.engine.target_hz)
    check(rig.until(lambda: any("Learned the remote at 434.12" in line for line in rig.hud)), "no 'learned' message")
    check(abs(rig.plugin.load_target() - 434_124_000) < 8_000, "the learned target was not saved")
    rig.frame("KEY_J")
    check_equal(rig.plugin.load_target(), 434_415_000.0, "target after J (back to the default)")
    rig.frame("KEY_ESCAPE")
    check(not rig.plugin.hunting and not rig.plugin.overlay_active, "ESC must close the screen and stop the hunt")
    # the game is using the SDR: refuse, say why, start nothing
    busy = HunterRig(ctx, sdr_running=True)
    busy.frame("KEY_S")
    check(not busy.plugin.hunting, "a hunt started while ADS-B was using the SDR")
    check("RTL-SDR is busy (adsb)" in busy.text(), "busy SDR was not explained: %s" % busy.plugin._status)
    # rtl_power exits by itself (dongle unplugged)
    dying = HunterRig(ctx, mode="die")
    dying.frame("KEY_S")
    check(dying.until(lambda: not dying.plugin.hunting, timeout=20), "a dead rtl_power was not noticed")
    check(dying.until(lambda: any("rtl_power ended" in line for line in dying.hud)), "no message for it")
    dying.frame("KEY_S")                                    # and it can be started again
    check(dying.plugin.hunting, "the hunt could not be restarted after rtl_power died")
    # rtl_power not installed
    missing = HunterRig(ctx)
    with mock.patch.object(missing.module.shutil, "which", return_value=None):
        missing.frame("KEY_S")
    check(not missing.plugin.hunting and "rtl_power is not installed" in missing.text(),
          "a missing rtl_power was not explained")
