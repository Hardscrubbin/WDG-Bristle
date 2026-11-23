"""[merge-v3] A scriptable stand-in for Kismet's REST API plus the pieces of
the machine the Kismet bridge touches (the kismet process, the adapter layer).

FakeKismet    HTTP server with the endpoints plugins/kismet_wardrive.py uses:
              /system/status.json, /datasource/all_sources.json,
              /devices/views/all/devices.json,
              /devices/last-time/<ts>/devices.json and
              /datasource/by-uuid/<uuid>/set_channel.cmd.
FakeProc      what subprocess.Popen(["kismet", ...]) returns here: starting it
              brings the server up, crash()/terminate() take it down.
KismetRig     the real plugin + the real LootManager + a stand-in game.
"""
import collections
import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from tools.sim import net

CH_24 =[str(n) for n in range(1, 14)]
CH_5 = [str(n) for n in (36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 149, 153, 157, 161, 165)]
CH_6 = ["%dW6e" % n for n in (1, 5, 9, 13, 37, 41, 45, 49, 85, 89, 93)]


class FakeKismet:
    def __init__(self):
        self.devices = collections.OrderedDict()      # mac -> device object
        self.sources = []
        self.mode = "ok"            # "ok" | "401" | "500" | "garbage"
        self.delay = 0.0            # seconds every request takes
        self.delta = True           # False: no last-time endpoint (404)
        self.readonly = False       # True: set_channel.cmd answers 403
        self.requests = collections.Counter()
        self.set_channel_calls = []
        self.port = 0
        self._server = None
        self._lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------
    def up(self):
        if self._server is not None:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                owner._handle(self, None)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                owner._handle(self, self.rfile.read(length) if length else b"")

        # Kismet comes back on ITS port after a crash or a restart. The port
        # is free once down() has returned (it waits for the serving thread);
        # a bind that still finds it busy is retried, see tools/sim/net.py.
        ThreadingHTTPServer.allow_reuse_address = True
        self._server = net.retry_busy(lambda: ThreadingHTTPServer(("127.0.0.1", self.port), Handler), self.port)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs=dict(poll_interval=0.05),
                                        name="fake-kismet", daemon=True)
        self._thread.start()

    def down(self):
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()             # returns when serve_forever() has left select()
            server.server_close()         # only now is the listening socket closed
            thread = getattr(self, "_thread", None)
            if thread is not None:
                thread.join(3.0)

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    # -- content ---------------------------------------------------------
    def add_ap(self, mac, ssid="Net", channel="6", signal=-60, crypt="WPA2-PSK", handshake=False,
               last_time=None):
        with self._lock:
            self.devices[mac] = {
                "kismet.device.base.phyname": "IEEE802.11", "kismet.device.base.macaddr": mac,
                "kismet.device.base.commonname": ssid, "kismet.device.base.type": "Wi-Fi AP",
                "kismet.device.base.channel": channel, "kismet.device.base.crypt": crypt,
                "kismet.device.base.signal": {"kismet.common.signal.last_signal": signal},
                "kismet.device.base.last_time": int(time.time() if last_time is None else last_time),
                "dot11.device": {"dot11.device.wpa_handshake_list": [1] if handshake else []}}

    def add_bt(self, mac, name="Tag", signal=-70, last_time=None):
        with self._lock:
            self.devices[mac] = {
                "kismet.device.base.phyname": "BTLE", "kismet.device.base.macaddr": mac,
                "kismet.device.base.commonname": name, "kismet.device.base.type": "BTLE",
                "kismet.device.base.signal": {"kismet.common.signal.last_signal": signal},
                "kismet.device.base.last_time": int(time.time() if last_time is None else last_time)}

    def add_client(self, mac):
        with self._lock:
            self.devices[mac] = {
                "kismet.device.base.phyname": "IEEE802.11", "kismet.device.base.macaddr": mac,
                "kismet.device.base.commonname": mac, "kismet.device.base.type": "Wi-Fi Client",
                "kismet.device.base.last_time": int(time.time())}

    def touch(self, mac, signal=None):
        with self._lock:
            device = self.devices[mac]
            device["kismet.device.base.last_time"] = int(time.time()) + 1
            if signal is not None:
                device["kismet.device.base.signal"] = {"kismet.common.signal.last_signal": signal}

    def add_source(self, name, kind="wifi", channels=None, running=True):
        uuid = "5FE308BD-0000-0000-0000-%012X" % (len(self.sources) + 1)
        self.sources.append({
            "kismet.datasource.uuid": uuid, "kismet.datasource.name": name,
            "kismet.datasource.interface": name,
            "kismet.datasource.type_driver": {
                "kismet.datasource.driver.type": "linuxwifi" if kind == "wifi" else "linuxbluetooth"},
            "kismet.datasource.running": running, "kismet.datasource.error": not running,
            "kismet.datasource.error_reason": "" if running else "interface not found",
            "kismet.datasource.channels": list(channels or []),
            "kismet.datasource.hop_channels": list(channels or []),
            "kismet.datasource.hop_rate": 5.0})
        return uuid

    def source(self, name):
        return next(s for s in self.sources if s["kismet.datasource.name"] == name)

    def set_source(self, name, running, reason=""):
        source = self.source(name)
        source["kismet.datasource.running"] = running
        source["kismet.datasource.error"] = not running
        source["kismet.datasource.error_reason"] = reason

    # -- requests --------------------------------------------------------
    def _reply(self, handler, status, body=b"{}", content="application/json"):
        handler.send_response(status)
        handler.send_header("Content-Type", content)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _handle(self, handler, body):
        path = urllib.parse.urlsplit(handler.path).path
        self.requests[path.split("/")[1] if "/" in path[1:] else path] += 1
        if self.delay:
            time.sleep(self.delay)
        try:
            if self.mode == "401":
                return self._reply(handler, 401, b'{"error":"login required"}')
            if self.mode == "500" and path != "/system/status.json":
                return self._reply(handler, 500, b"oops", "text/plain")
            if path == "/system/status.json":
                return self._reply(handler, 200, b'{"kismet.system.version":"sim"}')
            if path == "/datasource/all_sources.json":
                return self._reply(handler, 200, json.dumps(self.sources).encode())
            if path.startswith("/datasource/by-uuid/") and path.endswith("/set_channel.cmd"):
                if self.readonly:
                    return self._reply(handler, 403, b'{"error":"permission denied"}')
                uuid = path.split("/")[3]
                form = urllib.parse.parse_qs((body or b"").decode("utf-8", errors="replace"))
                command = json.loads(form.get("json", ["{}"])[0])
                self.set_channel_calls.append((uuid, command))
                for source in self.sources:
                    if source["kismet.datasource.uuid"] == uuid and "channels" in command:
                        source["kismet.datasource.hop_channels"] = list(command["channels"])
                return self._reply(handler, 200, b'{}')
            if path == "/devices/views/all/devices.json" or path.startswith("/devices/last-time/"):
                since = 0
                if path.startswith("/devices/last-time/"):
                    if not self.delta:
                        return self._reply(handler, 404, b"not found", "text/plain")
                    since = int(path.split("/")[3])
                if self.mode == "garbage":
                    return self._reply(handler, 200, b'[{"kismet.device.base.macaddr": 7}, 3, null, {"x"')
                with self._lock:
                    rows = [d for d in self.devices.values()
                            if d.get("kismet.device.base.last_time", 0) > since]
                    payload = json.dumps(rows).encode()
                return self._reply(handler, 200, payload)
            return self._reply(handler, 404, b"not found", "text/plain")
        except (BrokenPipeError, ConnectionError, OSError):
            pass


class FakeProc:
    """subprocess.Popen(["kismet", ...]) for the rig."""

    def __init__(self, server, command):
        self.server, self.command, self.code = server, list(command), None
        server.up()

    def poll(self):
        return self.code

    def crash(self, code=-11):
        self.code = code
        self.server.down()

    def terminate(self):
        if self.code is None:
            self.code = 0
        self.server.down()

    kill = terminate

    def wait(self, timeout=None):
        return self.code


class _Forward:
    """Attribute proxy: named overrides, everything else from the real module."""

    def __init__(self, real, **overrides):
        self.__dict__["_real"] = real
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FakePyxel:
    """Just enough pyxel for the bridge: key constants and btnp()."""
    frame_count = 0

    def __init__(self):
        self.pressed = set()

    def __getattr__(self, name):
        if name.startswith("KEY_"):
            return name
        raise AttributeError(name)

    def btnp(self, key, *_repeat):
        return key in self.pressed


class KismetRig:
    """The real Kismet bridge plugin in a simulated machine."""

    def __init__(self, ctx, wifi=None, ble=None, monitor_fails=(), aio=False):
        import plugins.kismet_wardrive as kismet
        from test_scan_session_map import loot_at
        self.ctx, self.kismet = ctx, kismet
        self.server = FakeKismet()
        self.procs = []
        self.launches = []                      # every kismet command line
        self.monitor_calls = []                 # (iface, enable)
        self.monitor_fails = set(monitor_fails)
        self.wifi = list(wifi if wifi is not None else [self.adapter("wlan1", "00:c0:ca:00:00:01", "Alfa AXML")])
        self.ble = list(ble if ble is not None else [dict(id="hci1", mac="00:1a:7d:00:00:01", guess="UB500")])
        self.frame_times = []
        self.hud, self.term = [], []
        self.xp = 0
        self.handshakes = 0

        rig = self

        def popen(command, **_kwargs):
            rig.launches.append(list(command))
            proc = FakeProc(rig.server, command)
            rig.procs.append(proc)
            return proc

        def set_monitor(iface, enable, log=print):
            rig.monitor_calls.append((iface, enable))
            if enable and iface in rig.monitor_fails:
                return False, "%s would not switch to monitor mode" % iface
            return True, "ok"

        fake_hw = _Forward(
            kismet.hw, _run=lambda *a, **k: (1, "", ""), have=lambda tool: tool == "kismet",
            detect_wifi=lambda: [dict(w) for w in rig.wifi], detect_ble=lambda: [dict(b) for b in rig.ble],
            resolve_wifi_by_mac=lambda mac: next((dict(w) for w in rig.wifi if w["mac"] == mac), None),
            resolve_ble_by_mac=lambda mac: next((dict(b) for b in rig.ble if b["mac"] == mac), None),
            kill_interferers=lambda iface, log=print: None, set_monitor=set_monitor,
            release_interface_back=lambda iface, log=print: None,
            bring_up_ble=lambda hci, log=print: (True, "%s up" % hci),
            read_system_diagnostics=lambda: {})
        ctx.patch(kismet, "hw", fake_hw)
        ctx.patch(kismet, "subprocess", _Forward(kismet.subprocess, Popen=popen))
        ctx.patch(kismet, "time", _Forward(time, sleep=lambda seconds: time.sleep(min(seconds, 0.02))))
        ctx.patch(kismet, "DATASOURCE_CHECK_INTERVAL", 0.1)
        ctx.patch(kismet, "POLL_ERROR_REPEAT", 0.6)
        ctx.patch(kismet, "KISMET_GPSD_ENABLED", False)
        ctx.patch(kismet, "KISMET_SESSION_ROTATE_MINUTES", 0)
        ctx.patch(kismet, "KISMET_ROTATE_RSS_MB", 0)
        self.server.up()                        # pick a port, then wait for "kismet" to start
        self.server.down()
        ctx.patch(kismet, "KISMET_URL", self.server.url)
        ctx.cleanup(self.server.down)
        # the bridge draws map sprites through pyxel and watchdogs.app: stand-ins
        fake_pyxel = _FakePyxel()
        self.pyxel = fake_pyxel
        fake_app_module = SimpleNamespace(WifiNetwork=lambda *a: SimpleNamespace(),
                                          BleDevice=lambda *a: SimpleNamespace())
        for name, module in (("pyxel", fake_pyxel), ("watchdogs.app", fake_app_module)):
            old = sys.modules.get(name)
            sys.modules[name] = module
            ctx.cleanup(self._restore_module, name, old)

        self.position = SimpleNamespace(available=True, device="gpsd://sim", gpsd_retrying=False,
                                        fix=SimpleNamespace(valid=True, latitude=50.0, longitude=19.0,
                                                            altitude=210.0, hdop=1.0))
        manager = loot_at(ctx.tmp, "2026-11-21T12:00:00", self.position)
        self.loot = manager.__enter__()
        ctx.cleanup(manager.__exit__, None, None, None)
        self.app = SimpleNamespace(
            loot=self.loot, _app_dir=ctx.tmp, gps_fix=True, player_lat=50.0, player_lon=19.0,
            gps=self.position, _gps_enabled=True, _whitelist=None, _known_wifi=set(), _known_ble=set(),
            gain_xp=self._gain_xp, _trigger_hs_event=self._handshake,
            msg=lambda text, colour=7: self.hud.append(text),
            _term_add=lambda text, raw=False: self.term.append(text),
            _esc_consumed_frame=-1, _plugins=[], wifi_networks=[], ble_devices=[],
            state=SimpleNamespace(networks=[], wardriving_running=False, wardriving_networks=0,
                                  scan_done=False),
            _aio_available=aio, _usb_enabled=not aio, _sysmon=None, _toggle_gps=lambda: None)
        self.plugin = kismet.KismetWardriveBridge()
        self.plugin.POLL_INTERVAL = 0.05
        self.plugin.DIAG_INTERVAL = 3600.0
        self.plugin.WIGLE_RETRY_INTERVAL = 1e9
        self.plugin.on_load(self.app)
        self.app._plugins.append(self.plugin)
        ctx.cleanup(self._shutdown)

    @staticmethod
    def adapter(name, mac, guess, can_monitor=True):
        return dict(id=name, mac=mac, guess=guess, can_monitor=can_monitor, note="")

    @staticmethod
    def _restore_module(name, old):
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old

    def _gain_xp(self, amount):
        self.xp += amount

    def _handshake(self):
        self.handshakes += 1

    def _shutdown(self):
        try:
            self.plugin._stop_poll_thread()
            self.plugin._stop_gps_proxy()
            if self.plugin.engine:
                self.plugin.engine.running = False
        except Exception:
            pass

    # -- driving ---------------------------------------------------------
    def frame(self):
        started = time.perf_counter()
        self.plugin.on_update()
        self.frame_times.append(time.perf_counter() - started)

    def frames(self, seconds):
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            self.frame()
            time.sleep(0.005)

    def until(self, predicate, timeout=15.0):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            self.frame()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def start(self):
        self.plugin.start_wardrive()
        if not self.plugin.overlay_active:
            return False
        self.plugin._launch_from_picker()
        return self.until(lambda: self.plugin._lifecycle in ("running", "idle"))

    def stop(self):
        self.plugin.stop_wardrive()
        return self.until(lambda: self.plugin._lifecycle == "idle")

    def said(self, fragment):
        return [line for line in self.term if fragment in line]

    def csv_rows(self):
        from watchdogs.loot_manager import LootManager
        import os
        path = os.path.join(self.loot.session_path, "wardriving.csv")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8", newline="") as fh:
            lines = fh.read().splitlines()[2:]
        return [LootManager._wardrive_fields(line) for line in lines]

    def max_frame(self):
        return max(self.frame_times) if self.frame_times else 0.0
