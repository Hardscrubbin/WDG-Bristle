"""USB serial link: find Bristle nodes on USB ports and run a NodeSession over each.

Discovery means writing a `HI` line to a port and waiting for `HELLO`. That must never reach a device
that isn't a node: the GPS dongle gpsd is reading, another ESP32 running other firmware, a radio.
So nothing is written to a port unless PortPolicy.decide() says "probe", and it only says that when
  - the port's USB chip is a known ESP32 interface (native USB, CH343/CH340, CP210x, or one you added),
  - it is not on the exclude list and not marked "not a node",
  - it has not already failed to answer since it was plugged in, and
  - it is a node from Node Memory, or you asked for it (approve / auto mode).
A new port that passes the first three is only reported ("ask"): you decide.
Before a probe the scanner also checks that no other program has the port open.

A Bristle board that no core has spoken to says so by itself: once a second it prints a BCN line.
listen_for_beacon() opens a port, writes nothing, and waits for that line. So a new board is told
apart from any other ESP32 without a byte being sent to either, and a port that beacons is said
hello to without asking (it is a Bristle board; adopting it is still your decision).

For the record: an ESP32 running JanOS (ESP-IDF console) answers an unknown line with
"Unrecognized command" and nothing else, so one stray `HI` does it no harm. The rules above are
there so it never gets one.

Needs pyserial (imported lazily, so the rest of the library works without it).
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from . import codec, proto

# Known ESP32 USB interfaces. Add more in settings rather than editing this list.
ESP32_USB_IDS: dict[tuple[int, int], str] = {
    (0x303A, 0x1001): "Espressif USB-JTAG/serial (native USB, e.g. XIAO ESP32-C5)",
    (0x1A86, 0x55D3): "WCH CH343 (e.g. Waveshare ESP32-C5 kit UART port)",
    (0x1A86, 0x7523): "WCH CH340",
    (0x10C4, 0xEA60): "Silicon Labs CP210x",
}

PROBE, ASK, SKIP = "probe", "ask", "skip"


def usb_key(vid: int | None, pid: int | None, serial_number: str | None, device: str) -> str:
    """A name for the USB device itself, the same whichever port it is plugged into.

    Chips with a serial number get "usb:<vid>:<pid>:<serial>". Chips without one (CH340) can only be
    told apart by where they are plugged in: "dev:<device path>".
    """
    if vid is not None and pid is not None and serial_number:
        return f"usb:{vid:04x}:{pid:04x}:{serial_number.strip().lower()}"
    return f"dev:{device}"


def serial_as_hwid(serial_number: str | None) -> str | None:
    """Espressif native USB reports the chip's MAC as its serial number: that is the node's hwid."""
    h = re.sub(r"[^0-9a-fA-F]", "", serial_number or "").lower()
    return h if len(h) == 12 else None


@dataclass(frozen=True)
class PortInfo:
    device: str
    vid: int | None
    pid: int | None
    serial_number: str | None
    description: str = ""

    @property
    def key(self) -> str:
        return usb_key(self.vid, self.pid, self.serial_number, self.device)

    @property
    def chip(self) -> str:
        if self.vid is None or self.pid is None:
            return "no USB id"
        return f"{self.vid:04x}:{self.pid:04x}"


def list_ports() -> list[PortInfo]:
    """Every serial port on the machine. Reads the port list only; opens nothing."""
    from serial.tools import list_ports as lp   # lazy import
    return [PortInfo(p.device, p.vid, p.pid, p.serial_number, p.description or "") for p in lp.comports()]


class PortPolicy:
    """Decides what may be done with a serial port. Pure: it reads nothing but what it is given."""

    def __init__(self, registry, exclude: Iterable[str] = (), auto: bool = False,
                 extra_ids: Iterable[tuple[int, int]] = ()) -> None:
        self.registry = registry
        self.exclude = {e.strip() for e in exclude if e and e.strip()}   # device paths or USB keys
        self.auto = auto                 # probe new ports without asking (never excluded/marked ones)
        self.ids = set(ESP32_USB_IDS) | set(extra_ids)
        self.approved: set[str] = set()  # USB keys you said to probe
        self.quiet: set[str] = set()     # USB keys that did not answer since they were plugged in
        self._lock = threading.Lock()

    def decide(self, port: PortInfo) -> tuple[str, str]:
        """(PROBE | ASK | SKIP, the reason in plain words)."""
        key = port.key
        if port.vid is None or (port.vid, port.pid) not in self.ids:
            return SKIP, "not an ESP32 USB interface"
        if port.device in self.exclude or key in self.exclude:
            return SKIP, "on the exclude list"
        if self.registry.is_not_node(key):
            return SKIP, "marked not a node"
        known = self.registry.knows_usb(key, serial_as_hwid(port.serial_number))
        with self._lock:
            if key in self.quiet and not known:
                return SKIP, "did not answer (unplug it and plug it in again to retry)"
            if known:
                return PROBE, "one of your nodes"
            if key in self.approved:
                return PROBE, "you asked"
        if self.auto:
            return PROBE, "new port (auto mode)"
        return ASK, "new port: not spoken to yet"

    def approve(self, key: str) -> None:
        with self._lock:
            self.approved.add(key)
            self.quiet.discard(key)

    def went_quiet(self, key: str) -> None:
        with self._lock:
            self.quiet.add(key)
            self.approved.discard(key)

    def unplugged(self, present: set[str]) -> None:
        """Forget "did not answer" and "you asked" for devices that are no longer plugged in."""
        with self._lock:
            self.quiet &= present
            self.approved &= present


def port_in_use(device: str) -> bool:
    """True if another process has this device open (Linux /proc check; False elsewhere)."""
    target = os.path.realpath(device)
    proc = "/proc"
    if not os.path.isdir(proc):
        return False
    me = str(os.getpid())
    for pid in os.listdir(proc):
        if not pid.isdigit() or pid == me:
            continue
        fd_dir = os.path.join(proc, pid, "fd")
        try:
            for fd in os.listdir(fd_dir):
                try:
                    if os.path.realpath(os.path.join(fd_dir, fd)) == target:
                        return True
                except OSError:
                    continue
        except OSError:
            continue
    return False


BEACON_LISTEN_S = 1.3 * proto.BEACON_MS / 1000            # long enough to catch one beacon for sure


def listen_for_beacon(device: str, seconds: float = BEACON_LISTEN_S, serial_factory=None,
                      mono: Callable[[], float] = time.monotonic) -> dict | None:
    """Open the port, WRITE NOTHING, and wait for a Bristle beacon. Returns the beacon's fields
    (hw, bd, b, o, ro) or None. DTR and RTS are held low before the port opens, as for a link, so
    opening it cannot reset a board."""
    try:
        if serial_factory is None:
            import serial   # lazy import
            ser = serial.Serial()
            ser.port = device
            ser.baudrate = proto.USB_BAUD
            ser.timeout = 0.1
            ser.dtr = False
            ser.rts = False
            ser.open()
        else:
            ser = serial_factory(device)
    except Exception:
        return None
    reader = codec.LineReader(proto.LINE_RELAY_MAX_BYTES)
    deadline = mono() + seconds
    try:
        while mono() < deadline:
            try:
                data = ser.read(256)
            except Exception:
                return None
            for line in reader.feed(data or b""):
                try:
                    msg = codec.parse(line)
                    codec.check_required(msg)
                except codec.LineError:
                    continue
                if msg.type == "BCN":
                    return dict(msg.fields)
        return None
    finally:
        try:
            ser.close()
        except Exception:
            pass


class BeaconCache:
    """Remembers which plugged-in USB devices beaconed, so a port is listened to once, not at every
    look. "Yes" holds until the device is unplugged. After a "no" the device is listened to once
    more, ``retry_s`` later (a board that was still starting gets a second chance); a second "no"
    holds until it is unplugged. A device seen for the first time less than ``settle_s`` ago is
    given until then before it is listened to."""

    def __init__(self, listen: Callable[[str], dict | None] = listen_for_beacon, retry_s: float = 20.0,
                 settle_s: float = 2.5, mono: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._listen, self.retry_s, self.settle_s = listen, retry_s, settle_s
        self._mono, self._sleep = mono, sleep
        self._first_seen: dict[str, float] = {}
        self._yes: dict[str, dict] = {}
        self._no: dict[str, tuple] = {}          # key -> (times it stayed silent, when)
        self._lock = threading.Lock()
        self._primed = False

    def present(self, keys: set) -> None:
        """Tell the cache what is plugged in now (what is gone is forgotten)."""
        with self._lock:
            now = self._mono()
            for key in keys:
                # everything that is there at the first look counts as settled
                self._first_seen.setdefault(key, now if self._primed else now - self.settle_s)
            self._primed = True
            for store in (self._first_seen, self._yes, self._no):
                for key in [k for k in store if k not in keys]:
                    del store[key]

    def known(self, key: str) -> dict | None:
        return self._yes.get(key)

    def check(self, key: str, device: str) -> dict | None:
        """The beacon of this device, listening for it if that has not been done lately."""
        with self._lock:                      # one listener at a time: two readers would split the data
            if key in self._yes:
                return self._yes[key]
            now = self._mono()
            silent, when = self._no.get(key, (0, -1e9))
            if silent >= 2 or now - when < self.retry_s:
                return None
            wait = self._first_seen.get(key, now - self.settle_s) + self.settle_s - now
            if wait > 0:
                self._sleep(wait)
            beacon = self._listen(device)
            if beacon:
                self._yes[key] = beacon
            else:
                self._no[key] = (silent + 1, self._mono())
            return beacon


class UsbLink:
    """One open serial port running one NodeSession. Reader thread; thread-safe send."""

    def __init__(self, port: PortInfo, make_session: Callable[[Callable[[str], None]], "object"],
                 on_closed: Callable[["UsbLink"], None] = lambda l: None) -> None:
        self.port = port
        self.device = port.device
        self._make_session = make_session
        self._on_closed = on_closed
        self._ser = None
        self._tx_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.session = None
        self.reader = codec.LineReader(proto.LINE_RELAY_MAX_BYTES)   # a bridge's ERX lines are long

    def send(self, line: str) -> None:
        ser = self._ser
        if ser is None:
            return
        data = line.encode("ascii")
        with self._tx_lock:
            try:
                ser.write(data)
            except Exception:
                self._stop.set()

    def open(self) -> None:
        import serial   # lazy import
        # Set DTR/RTS low BEFORE the port opens, so opening it can't reset the board (CH343 boards
        # wire these lines to EN/BOOT).
        ser = serial.Serial()
        ser.port = self.device
        ser.baudrate = proto.USB_BAUD
        ser.timeout = 0.1
        ser.write_timeout = 1.0
        ser.dtr = False
        ser.rts = False
        ser.open()
        self._ser = ser
        self.session = self._make_session(self.send)
        self.session.usb_key = self.port.key
        self._thread = threading.Thread(target=self._run, name=f"bristle-usb-{self.device}", daemon=True)
        self._thread.start()
        self.session.start()

    def _run(self) -> None:
        ser = self._ser
        try:
            while not self._stop.is_set():
                try:
                    data = ser.read(512)
                except Exception:
                    break
                if data:
                    for line in self.reader.feed(data):
                        self.session.feed_line(line)
        finally:
            self.close()

    def close(self) -> None:
        self._stop.set()
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
            self._on_closed(self)


class UsbScanner:
    """Looks at the port list every few seconds and opens a UsbLink where the policy says "probe".

    on_port(port, action, reason) is called when a port shows up or its answer changes, so the
    screen (or the bench tool) can show "new port" and "skipped because ..." lines.
    """

    def __init__(self, open_link: Callable[[PortInfo], None], policy: PortPolicy, interval_s: float = 3.0,
                 on_port: Callable[[PortInfo, str, str], None] = lambda p, a, r: None,
                 ports: Callable[[], list[PortInfo]] = list_ports,
                 in_use: Callable[[str], bool] = port_in_use, beacons: "BeaconCache | None" = None) -> None:
        self._open_link = open_link
        self.policy = policy
        self.interval = interval_s
        self._on_port = on_port
        self._ports = ports
        self._in_use = in_use
        self.beacons = beacons                                  # None: never listen, always ask
        self.active: set[str] = set()                           # device paths with an open link
        self.seen: dict[str, tuple[PortInfo, str, str]] = {}    # USB key -> last decision
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="bristle-usb-scan", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def rescan(self) -> None:
        self._wake.set()

    def released(self, device: str) -> None:
        self.active.discard(device)

    def scan_once(self) -> None:
        ports = self._ports()
        present = {p.key for p in ports}
        self.policy.unplugged(present)
        if self.beacons is not None:
            self.beacons.present(present)
        for key in [k for k in self.seen if k not in present]:
            del self.seen[key]
        for p in ports:
            if p.device in self.active:
                continue
            action, reason = self.policy.decide(p)
            if action == ASK and self.beacons is not None and not self._in_use(p.device):
                # Nothing is written: the port is only listened to. A Bristle board announces itself.
                if self.beacons.check(p.key, p.device):
                    self.policy.approve(p.key)
                    action, reason = PROBE, "it says it is a Bristle board"
            if action == PROBE and self._in_use(p.device):
                action, reason = SKIP, "in use by another program"
            if action == SKIP and reason.startswith("not an ESP32"):
                continue                                    # not worth a line on the screen
            if self.seen.get(p.key, (None, None, None))[1:] != (action, reason):
                self.seen[p.key] = (p, action, reason)
                self._on_port(p, action, reason)
            if action == PROBE:
                self.active.add(p.device)
                try:
                    self._open_link(p)
                except Exception:
                    self.active.discard(p.device)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:
                pass
            self._wake.wait(self.interval)
            self._wake.clear()
