"""[merge-v3] A tiny gpsd-protocol filter so Kismet reads ONE GPS receiver.

With two receivers behind gpsd, a plain gpsd client gets both streams mixed,
and Kismet's gpsd client does not pick a device: its .kismet log would be
geotagged with a position that jumps between the two. This proxy connects to
the real gpsd, and re-serves on a local port only the TPV / SKY reports of
the receiver the game is following (GpsManager.chosen_device), so Kismet's
own geotags match the game's.

It is only used when gpsd reports two or more receivers. With one receiver
Kismet is pointed straight at gpsd, exactly as v2 did. WDG_KISMET_GPS_PROXY=0
in secrets.conf turns the proxy off even with two receivers.

Standard library only. Three kinds of daemon thread: one reads gpsd
(reconnecting every 2 s), one accepts clients, one per client answers ?WATCH.
A client that stops reading is dropped; nothing here can block the game.
"""
import json
import socket
import threading
import time

VERSION_LINE = (b'{"class":"VERSION","release":"3.25","rev":"wdg-filter",'
                b'"proto_major":3,"proto_minor":15}\n')
UPSTREAM_RETRY = 2.0
CLIENT_SEND_TIMEOUT = 1.0
MAX_CLIENTS = 4
MAX_LINE = 65536


class GpsdDeviceProxy:
    def __init__(self, chosen, upstream=("127.0.0.1", 2947), port=0, log=None):
        self._chosen = chosen                  # callable -> device path or None
        self._upstream = upstream
        self._want_port = port
        self._log = log or (lambda _text: None)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._clients = []                     # sockets that sent ?WATCH
        self._listener = None
        self._threads = []
        self.port = 0
        self.forwarded = 0                     # reports passed on (for tests / diagnostics)
        self.dropped = 0                       # reports from the other receiver(s)
        self.upstream_connected = False
        self._last_device = None

    @property
    def running(self) -> bool:
        return self._listener is not None and not self._stop.is_set()

    def start(self) -> int:
        """Listen on 127.0.0.1 (the wanted port, or any free one if that is
        taken) and start the threads. Returns the port."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", self._want_port))
        except OSError:
            listener.bind(("127.0.0.1", 0))
        listener.listen(MAX_CLIENTS)
        listener.settimeout(0.5)
        self._listener = listener
        self.port = listener.getsockname()[1]
        for target, name in ((self._accept_loop, "wdg-gpsproxy-accept"),
                             (self._upstream_loop, "wdg-gpsproxy-gpsd")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self.port

    def stop(self) -> None:
        """Never waits (it runs on the game thread). shutdown() before close():
        on Linux close() alone leaves the port LISTENING until the accept
        thread's wait times out (up to 0.5 s), and a capture restarted in that
        window would find the port busy. shutdown() ends that wait at once.
        If the port is busy all the same, start() takes any free port and the
        bridge hands Kismet that one."""
        self._stop.set()
        with self._lock:
            clients, self._clients = self._clients, []
        for sock in clients + ([self._listener] if self._listener is not None else []):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass                              # a listener on Windows, or already gone
            try:
                sock.close()
            except OSError:
                pass
        self._listener = None

    # -- clients ---------------------------------------------------------
    def _accept_loop(self) -> None:
        listener = self._listener
        while not self._stop.is_set():
            try:
                sock, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                crowded = len(self._clients) >= MAX_CLIENTS
            if crowded:
                sock.close()
                continue
            threading.Thread(target=self._serve, args=(sock,), name="wdg-gpsproxy-client",
                             daemon=True).start()

    def _devices_line(self) -> bytes:
        device = self._last_device or self._chosen() or ""
        rows = [{"class": "DEVICE", "path": device, "driver": "wdg-filter"}] if device else []
        return (json.dumps({"class": "DEVICES", "devices": rows}) + "\n").encode()

    def _serve(self, sock) -> None:
        watching = False
        try:
            sock.settimeout(CLIENT_SEND_TIMEOUT)
            sock.sendall(VERSION_LINE)
            buffer = b""
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(1024)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buffer = (buffer + chunk)[-4096:]
                while b";" in buffer:
                    command, buffer = buffer.split(b";", 1)
                    command = command.strip()
                    if command.startswith(b"?WATCH"):
                        sock.sendall(self._devices_line()
                                     + b'{"class":"WATCH","enable":true,"json":true}\n')
                        if not watching:
                            watching = True
                            with self._lock:
                                self._clients.append(sock)
                    elif command.startswith(b"?DEVICES"):
                        sock.sendall(self._devices_line())
                    elif command.startswith(b"?VERSION"):
                        sock.sendall(VERSION_LINE)
        except OSError:
            pass
        with self._lock:
            if sock in self._clients:
                self._clients.remove(sock)
        try:
            sock.close()
        except OSError:
            pass

    def _broadcast(self, line: bytes) -> None:
        with self._lock:
            clients = list(self._clients)
        for sock in clients:
            try:
                sock.sendall(line)
            except OSError:                       # gone, or not reading: drop it
                with self._lock:
                    if sock in self._clients:
                        self._clients.remove(sock)
                try:
                    sock.close()
                except OSError:
                    pass

    # -- gpsd ------------------------------------------------------------
    def _upstream_loop(self) -> None:
        while not self._stop.is_set():
            sock = None
            try:
                sock = socket.create_connection(self._upstream, timeout=2.0)
                sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
                sock.settimeout(1.0)
                self.upstream_connected = True
                buffer = b""
                while not self._stop.is_set():
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        self._handle(line.strip())
                    if len(buffer) > MAX_LINE:
                        buffer = b""
            except OSError:
                pass
            self.upstream_connected = False
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            self._stop.wait(UPSTREAM_RETRY)

    def _handle(self, line: bytes) -> None:
        if not line or (b'"TPV"' not in line and b'"SKY"' not in line):
            return
        try:
            report = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            return
        if not isinstance(report, dict) or report.get("class") not in ("TPV", "SKY"):
            return
        device = report.get("device") if isinstance(report.get("device"), str) else ""
        try:
            chosen = self._chosen()
        except Exception:
            chosen = None
        if chosen and device and device != chosen:
            self.dropped += 1
            return
        self._last_device = device or self._last_device
        self.forwarded += 1
        self._broadcast(line + b"\n")


def wait_until(predicate, timeout=5.0, step=0.01) -> bool:
    """Small helper for tests and simulations."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())
