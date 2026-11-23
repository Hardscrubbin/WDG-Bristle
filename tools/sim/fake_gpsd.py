"""[merge-v3] A scriptable stand-in for gpsd (JSON protocol on a local port).

Speaks enough of gpsd's protocol for the game's client: the VERSION banner,
?WATCH (JSON for every device, or raw NMEA for one device), DEVICES, DEVICE
(hot-plug), TPV, SKY and raw NMEA lines including antenna TXT sentences.
Nothing is sent on a timer: the scenario calls emit() for each receiver epoch,
so a run is deterministic.
"""
import collections
import json
import socket
import threading
import time

from tools.sim import net


def nmea(body: str) -> str:
    value = 0
    for char in body:
        value ^= ord(char)
    return "$%s*%02X" % (body, value)


def sats(used, seen=None, snr=30.0):
    """[(PRN, SNR, used)] with ``used`` satellites in use out of ``seen``."""
    seen = used if seen is None else max(seen, used)
    return [(n + 1, float(snr) if n < used else float(snr) - 12.0, n < used) for n in range(seen)]


class FakeGpsd:
    def __init__(self):
        self.devices = collections.OrderedDict()
        self.second = 0
        self.port = 0
        self._listener = None
        self._accept_thread = None
        self._serve_threads = []
        self._lock = threading.Lock()
        self._clients = []          # dict(sock, kind="new"|"json"|"raw", device=path|None)
        self._accepting = False
        self.connections = 0
        self.starts = 0

    # -- lifecycle -------------------------------------------------------
    def start(self):
        """Listen on the port (any free one the first time, THE SAME one after
        a stop: a restarted gpsd comes back on its port)."""
        if self._listener is not None:
            return self
        listener = net.listen(self.port)
        self.port = listener.getsockname()[1]
        self._listener = listener
        self._accepting = True
        self.starts += 1
        self._accept_thread = threading.Thread(target=self._accept_loop, args=(listener,), name="fake-gpsd",
                                               daemon=True)
        self._accept_thread.start()
        return self

    def stop(self):
        """gpsd dies: every client connection and the listening port go.
        Returns only when the port is free again (see tools/sim/net.py: on
        Linux a listening socket outlives close() for as long as the accept
        thread is still waiting on it)."""
        self._accepting = False
        with self._lock:
            clients, self._clients = self._clients, []
            serving, self._serve_threads = self._serve_threads, []
        for client in clients:
            net.close_socket(client["sock"])
        listener, self._listener = self._listener, None
        thread, self._accept_thread = self._accept_thread, None
        net.close_listener(listener, thread)
        for worker in serving:
            worker.join(1.0)

    def restart(self):
        self.stop()
        return self.start()

    _close = staticmethod(net.close_socket)

    def _accept_loop(self, listener):
        while self._accepting and self._listener is listener:
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            client = dict(sock=sock, kind="new", device=None)
            worker = threading.Thread(target=self._serve, args=(client, listener), name="fake-gpsd-client",
                                      daemon=True)
            with self._lock:
                if not self._accepting or self._listener is not listener:     # stopped meanwhile
                    net.close_socket(sock)
                    return
                self._clients.append(client)
                self._serve_threads = [t for t in self._serve_threads if t.is_alive()] + [worker]
            worker.start()

    def _serve(self, client, listener):
        sock = client["sock"]
        try:
            sock.sendall(b'{"class":"VERSION","release":"3.25","rev":"sim","proto_major":3,"proto_minor":15}\n')
            sock.settimeout(0.2)
            buffer = b""
            while self._accepting and self._listener is listener:   # this run of gpsd, not the next one
                try:
                    chunk = sock.recv(1024)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buffer += chunk
                while b";" in buffer:
                    command, buffer = buffer.split(b";", 1)
                    self._command(client, command.decode("ascii", errors="replace").strip())
        except OSError:
            pass
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
        self._close(sock)

    def _command(self, client, command):
        if not command.startswith("?WATCH"):
            return
        options = {}
        if "=" in command:
            try:
                options = json.loads(command.split("=", 1)[1])
            except ValueError:
                options = {}
        raw = bool(options.get("raw")) and not options.get("json", False)
        lines = [self._devices_report(), dict({"class": "WATCH", "enable": True}, **options)]
        self._send(client, "".join(json.dumps(line) + "\n" for line in lines))
        client["device"] = options.get("device")
        client["kind"] = "raw" if raw else "json"      # set last: wait_clients() keys on it

    def _devices_report(self):
        return {"class": "DEVICES", "devices": [
            {"class": "DEVICE", "path": path, "driver": state["driver"], "activated": "2026-11-21T12:00:00.000Z"}
            for path, state in self.devices.items() if state["plugged"]]}

    def _send(self, client, text) -> bool:
        try:
            client["sock"].sendall(text.encode("ascii", errors="replace"))
            return True
        except OSError:
            return False

    def clients(self, kind):
        with self._lock:
            return [client for client in self._clients if client["kind"] == kind]

    def wait_clients(self, json_clients=1, raw_clients=0, timeout=5.0) -> bool:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if len(self.clients("json")) >= json_clients and len(self.clients("raw")) >= raw_clients:
                return True
            time.sleep(0.01)
        return False

    # -- receivers -------------------------------------------------------
    def add(self, path, **state):
        base = dict(mode=3, lat=50.0, lon=19.0, sats=sats(9, 12), hdop=1.0, antenna=None,
                    antenna_style="ublox", silent=False, timeless=False, plugged=True,
                    driver="NMEA0183", last_time=None, drift=0.0)
        base.update(state)
        self.devices[path] = base
        return self

    def set(self, path, **state):
        self.devices[path].update(state)

    def _tpv(self, path, state, stamp):
        report = {"class": "TPV", "device": path, "mode": state["mode"]}
        if not state["timeless"]:
            report["time"] = stamp
        if state["mode"] >= 2:
            report.update(lat=state["lat"], lon=state["lon"], altMSL=210.0, speed=3.0)
        return report

    def _sky(self, path, state):
        return {"class": "SKY", "device": path, "hdop": state["hdop"],
                "satellites": [{"PRN": prn, "ss": snr, "used": used, "gnssid": 0}
                               for prn, snr, used in state["sats"]]}

    def _antenna_line(self, state):
        if state["antenna"] is None:
            return None
        if state["antenna_style"] == "ublox":
            return nmea("GPTXT,01,01,02,ANTSTATUS=%s" % state["antenna"])
        return nmea("GPTXT,01,01,01,ANTENNA %s" % state["antenna"])

    def emit(self, replay=False):
        """One receiver epoch for every plugged, non-silent receiver. With
        ``replay`` the previous epoch is sent again (same time, same place)."""
        if not replay:
            self.second += 1
        stamp = "2026-11-21T%02d:%02d:%02d.000Z" % (12 + self.second // 3600, self.second // 60 % 60,
                                                   self.second % 60)
        for path, state in self.devices.items():
            if state["silent"] or not state["plugged"]:
                continue
            if not replay:
                state["lat"] += state["drift"]
            text = "".join(json.dumps(line) + "\n" for line in (self._sky(path, state),
                                                                self._tpv(path, state, stamp)))
            for client in self.clients("json"):
                self._send(client, text)
            raw = nmea("GPGGA,120000.00,5000.0000,N,01900.0000,E,1,09,1.0,210.0,M,40.0,M,,") + "\r\n"
            antenna = self._antenna_line(state)
            if antenna:
                raw += antenna + "\r\n"
            for client in self.clients("raw"):
                if client["device"] == path:
                    self._send(client, raw)

    def send_json(self, *reports):
        text = "".join(json.dumps(report) + "\n" for report in reports)
        for client in self.clients("json"):
            self._send(client, text)

    def send_text(self, text):
        for client in self.clients("json"):
            self._send(client, text)

    def unplug(self, path):
        self.devices[path]["plugged"] = False
        self.send_json({"class": "DEVICE", "path": path})
        with self._lock:
            gone = [client for client in self._clients if client["kind"] == "raw" and client["device"] == path]
        for client in gone:
            self._close(client["sock"])

    def replug(self, path):
        self.devices[path]["plugged"] = True
        self.send_json({"class": "DEVICE", "path": path, "driver": self.devices[path]["driver"],
                        "activated": "2026-11-21T12:30:00.000Z"})
