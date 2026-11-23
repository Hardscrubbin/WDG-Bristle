"""GPS receiver — NMEA parser for UART/USB GPS modules, with an optional
gpsd client preferred when gpsd is running (common on Kali/DragonOS rigs).
[merge] gpsd support ported from Hardscrubbin's gps_manager; upstream's NMEA/radio_fix
logic is kept intact."""

import glob
import json
import logging
import math
import os
import re
import shutil
import socket
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

import serial

from .config import GPS_DEVICE, GPS_BAUD_RATE, GPSD_HOST, GPSD_PORT
from . import gps_multi  # [merge-v3] several receivers behind one gpsd

log = logging.getLogger(__name__)

# AUTO radio selection needs fresh, measured positions, independent of the
# player's map position and the older display-oriented ``fix.valid`` flag.
RADIO_FIX_MAX_AGE = 10.0
RADIO_FIX_RMC_MAX_AGE = 2.0
RADIO_FIX_RMC_TIME_TOLERANCE = 2.0
RADIO_FIX_RMC_DISTANCE_METRES = 100.0
WARDRIVE_SAMPLE_MAX_AGE = 2.0
# [merge-v3] gpsd auto-reconnect: seconds between attempts after a drop, and
# how long one attempt may take (it runs on its own thread, never the game's).
GPSD_RECONNECT_INTERVAL = 5.0
GPSD_CONNECT_TIMEOUT = 1.5
# [merge-v3] Where the system gpsd is told which receivers it owns. The game
# never opens a serial port listed there (see GpsManager.gpsd_claims).
# WDG_GPSD_DEFAULTS points this elsewhere (the self-test uses it).
GPSD_DEFAULTS_FILE = os.environ.get("WDG_GPSD_DEFAULTS", "/etc/default/gpsd")
# [merge-v3] Antenna status comes from the receivers' own NMEA TXT sentences,
# read through one extra gpsd connection per receiver. WDG_GPS_ANTENNA_WATCH=0
# turns that off (antenna status then reads "not reported").
GPS_ANTENNA_WATCH = os.environ.get("WDG_GPS_ANTENNA_WATCH", "1").strip() != "0"
GPS_RAW_RETRY_INTERVAL = 30.0
GPS_HEALTH_EVENTS_MAX = 64


def gpsd_configured_devices(path=None) -> list:
    """[merge-v3] The device paths in DEVICES="..." of /etc/default/gpsd.
    [] when the file is missing, unreadable or names none."""
    try:
        with open(path or GPSD_DEFAULTS_FILE, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(65536)
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        match = re.match(r"\s*DEVICES\s*=\s*(.*?)\s*(?:#.*)?$", line)
        if match:
            value = match.group(1).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            found = [item for item in value.split() if item.startswith("/")]
    return found


@dataclass
class GpsFix:
    """Snapshot of current GPS state."""
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    speed_knots: float = 0.0
    satellites: int = 0
    satellites_visible: int = 0
    fix_quality: int = 0       # 0=no fix, 1=GPS, 2=DGPS
    hdop: float = 99.9
    timestamp: str = ""        # UTC time from NMEA (hhmmss.ss)
    valid: bool = False


class _LineBuffer:
    """Accumulate raw bytes and yield complete NMEA sentences."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, raw: bytes) -> List[str]:
        self._buf += raw
        lines: List[str] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            decoded = line.decode("ascii", errors="replace").strip()
            if decoded.startswith("$"):
                lines.append(decoded)
        # Prevent unbounded growth if no newlines arrive
        if len(self._buf) > 1024:
            self._buf = self._buf[-512:]
        return lines


class GpsManager:
    """Manage a UART GPS receiver via pyserial + urwid watch_file."""

    # True when user explicitly set the GPS device via env var
    _user_configured = bool(
        os.environ.get("WDG_GPS_DEVICE")
        or os.environ.get("JANOS_GPS_DEVICE")
    )

    def __init__(self, device: str = GPS_DEVICE,
                 baud: int = GPS_BAUD_RATE) -> None:
        self.device = device
        self._baud = baud
        self._conn: Optional[serial.Serial] = None
        self._buf = _LineBuffer()
        self.fix = GpsFix()
        self._available = False
        self._gsv_visible: dict = {}  # constellation prefix → satellite count
        self._radio_position = None
        self._recent_rmc = None
        self._last_gga = None
        # [merge-v3] conflict: both sides appended state here. Kept upstream's
        # wardrive sample slot and v2's gpsd client state.
        self._wardrive_position = None
        # [merge] gpsd client state. When a local gpsd is reachable it owns the
        # real GPS device, so we read its JSON instead of opening a serial port.
        self._gpsd_sock: Optional[socket.socket] = None
        self._using_gpsd = False
        self._gpsd_buf = b""
        # [merge-v3] (time, lat, lon) of the last TPV accepted as a measurement.
        self._gpsd_last_epoch = None
        # [merge-v3] gpsd auto-reconnect state, see gpsd_reconnect_tick().
        self._gpsd_lost = False           # retrying: a link dropped, or gpsd was
        self._gpsd_retry_kind = ""        # not up at launch ("lost" / "launch")
        self._gpsd_events = []            # "lost"/"restored"/"waiting"/"found"
        self._gpsd_retry_at = 0.0         # monotonic time of the next attempt
        self._gpsd_connect_thread = None
        self._gpsd_connect_result = None  # socket handed over by that thread
        self._gpsd_connect_gen = 0        # bumped by close(): retires a late attempt
        self._gpsd_connect_lock = threading.Lock()
        # [merge-v3] several receivers behind one gpsd: per-receiver health and
        # the one receiver whose reports feed self.fix / wardrive_sample().
        self._multi = gps_multi.MultiGps()
        self._followed = None             # path the fix paths last followed
        self._health_events = []          # (level, text), drained by the UI
        # [merge-v3] one raw-NMEA gpsd connection per receiver (antenna TXT)
        self._raw_socks = {}              # path -> [socket, buffer]
        self._raw_pending = set()         # paths with a connect in flight
        self._raw_ready = []              # (generation, path, socket) from workers
        self._raw_retry_at = {}           # path -> monotonic time of next attempt
        self._raw_gen = 0

    @property
    def available(self) -> bool:
        return self._available

    def radio_fix(self, now=None) -> Optional[dict]:
        """Return a fresh credible GGA position, or None, without refreshing it.

        Complete checksum-valid GGA is sufficient; RMC is optional. When a
        fresh RMC is present it must agree in UTC time and position. GSV, UI
        polling and RMC alone cannot create/refresh this radio-selection fix.
        ``observed_at`` is the monotonic receive time, never NMEA/wall time.
        """
        now = time.monotonic() if now is None else now
        position = self._radio_position
        if not self.available or position is None or not math.isfinite(now):
            return None
        age = now - position["observed_at"]
        if not 0 <= age <= RADIO_FIX_MAX_AGE:
            return None
        return {key: position[key] for key in
                ("lat", "lon", "observed_at", "quality", "sats", "hdop")}

    def wardrive_sample(self, now=None) -> Optional[dict]:
        """Return an unfiltered measured GGA/RMC location younger than 2 s.

        [merge-v3] Under gpsd the same slot is fed by TPV reports
        (``sentence == "TPV"``); see ``_apply_gpsd_report``.

        This is independent of RF AUTO's stricter GGA quality policy and the
        player's visual jitter filter. It provides no cached position or
        startup backfill. ``observed_at`` is the monotonic receive time of the
        accepted position sentence; polling cannot refresh it. As in Piglet,
        an RMC position carries the latest available GGA altitude/quality
        metrics, which may not belong to the same sentence.
        """
        now = time.monotonic() if now is None else now
        position = self._wardrive_position
        if not self.available or position is None or not math.isfinite(now):
            return None
        if not 0 <= now - position["observed_at"] < WARDRIVE_SAMPLE_MAX_AGE:
            return None
        return dict(position)

    def _record_wardrive_sample(self, lat, lon, utc_seconds, observed_at, sentence):
        self._wardrive_position = dict(
            lat=lat, lon=lon, altitude=self.fix.altitude,
            quality=self.fix.fix_quality, sats=self.fix.satellites,
            hdop=self.fix.hdop, observed_at=observed_at,
            utc_seconds=utc_seconds, sentence=sentence)

    def _invalidate_position(self):
        self.fix.valid = False
        self._radio_position = None
        self._wardrive_position = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def setup(self) -> bool:
        """Try to open GPS serial port. Returns True on success.
        If user set WDG_GPS_DEVICE (or legacy JANOS_GPS_DEVICE), use that directly.
        Otherwise probe default path for NMEA, then auto-detect USB GPS.
        Never raises — GPS is optional."""
        device = self.device

        if self._user_configured:
            # User explicitly set env var — trust it, skip gpsd (deliberate override)
            if self._try_open(device):
                return True
            log.info("GPS device %s (from env) not available — GPS disabled",
                     device)
            return False

        # [merge] Prefer gpsd when it's running. On Kali/DragonOS wardriving
        # builds gpsd auto-starts and holds the real GPS exclusively, so this
        # is the only path that never has to open the serial port itself.
        if self._try_open_gpsd():
            return True

        # [merge-v3] gpsd is installed and configured to own the receivers but
        # is not answering yet. Probing serial ports now would open a port
        # gpsd is about to read (two readers = broken NMEA on both). Wait for
        # gpsd instead; never touch the ports.
        if self.gpsd_owns_receivers():
            log.info("gpsd is configured to own the GPS receivers; not opening any serial port")
            self._wait_for_gpsd()
            return False

        # No env var / no gpsd — probe default path, then auto-detect
        if os.path.exists(device) and self._probe_nmea(device, self._baud):
            if self._try_open(device):
                return True

        log.info("GPS not on %s, scanning for USB GPS...", device)
        detected = self._auto_detect(exclude={device})
        if detected:
            self.device = detected
            if self._try_open(detected):
                return True
        log.info("No GPS found — GPS disabled")
        # [merge-v3] No gpsd and no serial receiver. On a gpsd rig that just
        # means gpsd is not up yet (game started first, gpsd still starting,
        # service stopped): keep looking for it instead of staying off until
        # GPS is toggled by hand. See gpsd_reconnect_tick().
        self._wait_for_gpsd()
        return False

    # ------------------------------------------------------------------
    # [merge] gpsd client (ported from Hardscrubbin's gps_manager)
    # ------------------------------------------------------------------

    def open_gpsd_if_running(self) -> bool:
        """[merge-v3] Use gpsd when one is listening, exactly as setup() does,
        for callers that open a known UART without calling setup() (app.py's
        AIO shortcut). False = no gpsd, or the user pinned a device with
        WDG_GPS_DEVICE (a deliberate override, same rule as setup())."""
        if self._user_configured:
            return False
        return self._try_open_gpsd()

    # ------------------------------------------------------------------
    # [merge-v3] ports that belong to gpsd
    # ------------------------------------------------------------------

    def _gpsd_devices(self) -> list:
        """Receivers the local gpsd is configured to own, or [] when the user
        pinned WDG_GPS_DEVICE (deliberate override), gpsd is on another host,
        or gpsd is not installed."""
        if self._user_configured or GPSD_HOST not in ("127.0.0.1", "localhost", "::1"):
            return []
        devices = gpsd_configured_devices()
        if not devices or shutil.which("gpsd") is None:
            return []
        return devices

    def gpsd_owns_receivers(self) -> bool:
        return bool(self._gpsd_devices())

    def gpsd_claims(self, device) -> bool:
        """True when gpsd is configured to own ``device``: the game must not
        open it, whether or not gpsd is answering at this moment."""
        if not device or not isinstance(device, str) or not device.startswith("/"):
            return False
        try:
            real = os.path.realpath(device)
            return any(os.path.realpath(item) == real for item in self._gpsd_devices())
        except OSError:
            return False

    def gpsd_owned_uart(self) -> str:
        """The on-board UART receiver gpsd owns (the AIO GP-02), or "". The
        game powers its rail but leaves the port to gpsd."""
        for item in self._gpsd_devices():
            try:
                real = os.path.realpath(item)
            except OSError:
                continue
            if re.fullmatch(r"/dev/(ttyAMA|ttyS|serial)\d+", real):
                return item
        return ""

    def _try_open_gpsd(self) -> bool:
        """Connect to gpsd's local JSON service instead of opening a serial
        port. Preferred because gpsd already owns the real device, so there's
        no chance of colliding with it or of grabbing another peripheral's port."""
        sock = self._connect_gpsd()
        if sock is None:
            return False
        self._adopt_gpsd(sock)
        log.info("GPS via gpsd at %s:%d", GPSD_HOST, GPSD_PORT)
        return True

    @staticmethod
    def _connect_gpsd() -> Optional[socket.socket]:
        """[merge-v3] One blocking connect + WATCH handshake (v2's code, split
        out so the reconnect thread can run it). A non-blocking socket, or
        None. Touches no GpsManager state."""
        try:
            sock = socket.create_connection((GPSD_HOST, GPSD_PORT), timeout=GPSD_CONNECT_TIMEOUT)
        except OSError as exc:
            log.debug("gpsd not reachable at %s:%d: %s", GPSD_HOST, GPSD_PORT, exc)
            return None
        try:
            sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
            sock.setblocking(False)
        except OSError as exc:
            log.debug("gpsd handshake failed: %s", exc)
            try:
                sock.close()
            except OSError:
                pass
            return None
        return sock

    def _adopt_gpsd(self, sock) -> None:
        self._cancel_gpsd_retry()  # connected, however it happened
        self._gpsd_sock = sock
        self._gpsd_buf = b""
        self._using_gpsd = True
        self._available = True
        self.device = f"gpsd://{GPSD_HOST}:{GPSD_PORT}"

    # ------------------------------------------------------------------
    # [merge-v3] gpsd auto-reconnect
    # ------------------------------------------------------------------

    @property
    def gpsd_retrying(self) -> bool:
        """True while gpsd is being looked for every GPSD_RECONNECT_INTERVAL s."""
        return self._gpsd_lost

    def _cancel_gpsd_retry(self) -> None:
        """Stop looking for gpsd: forget the drop / the wait, retire an attempt
        still in flight and discard a socket it already delivered."""
        self._gpsd_lost = False
        self._gpsd_retry_kind = ""
        self._gpsd_events = []
        with self._gpsd_connect_lock:
            self._gpsd_connect_gen += 1
            pending, self._gpsd_connect_result = self._gpsd_connect_result, None
        if pending is not None:
            try:
                pending.close()
            except OSError:
                pass

    def _wait_for_gpsd(self) -> None:
        """setup() found neither gpsd nor a serial receiver: retry gpsd every
        GPSD_RECONNECT_INTERVAL seconds, exactly like after a drop, until it
        answers, a serial GPS is opened instead, or GPS is closed (in-game
        GPS OFF). Only gpsd is retried -- never the serial port probing, which
        blocks and can grab another peripheral's port."""
        self._gpsd_lost = True
        self._gpsd_retry_kind = "launch"
        self._gpsd_retry_at = time.monotonic() + GPSD_RECONNECT_INTERVAL
        self._gpsd_events = ["waiting"]
        log.info("gpsd is not running at %s:%d; checking for it every %.0f s",
                 GPSD_HOST, GPSD_PORT, GPSD_RECONNECT_INTERVAL)

    def _gpsd_dropped(self, reason) -> None:
        """A gpsd link that was working ended by itself (gpsd restarted,
        crashed or closed the socket). v2 and phase 1 called close() here and
        GPS then stayed off until it was toggled in-game. Still close -- no
        position survives the drop -- but remember that it was a drop so
        gpsd_reconnect_tick() brings the link back.

        The last accepted TPV epoch is kept across the drop: gpsd replays its
        most recent TPV to a client that connects, and that replay is not a
        new measurement (same rule as _apply_gpsd_report). An explicit
        close() -- GPS switched off in-game -- still forgets it."""
        epoch = self._gpsd_last_epoch
        self.close()
        self._gpsd_last_epoch = epoch
        self._gpsd_lost = True
        self._gpsd_retry_kind = "lost"
        self._gpsd_retry_at = time.monotonic() + GPSD_RECONNECT_INTERVAL
        self._gpsd_events.append("lost")
        log.warning("gpsd %s; GPS is off, reconnecting every %.0f s",
                    reason, GPSD_RECONNECT_INTERVAL)

    def gpsd_reconnect_tick(self, now=None) -> Optional[str]:
        """Call once per game frame. Never blocks.

        Returns one event per state change, otherwise None, so the caller can
        log each exactly once:
          "lost"      a working link dropped         -> "restored" when back
          "waiting"   gpsd was not up when setup()   -> "found" when it answers
                      looked for a GPS (launch, or GPS ON in-game)
        While retrying, a connect attempt is started every
        GPSD_RECONNECT_INTERVAL seconds on a short-lived daemon thread (the
        connect itself can take up to GPSD_CONNECT_TIMEOUT when WDG_GPSD_HOST
        is not this machine) and its socket is adopted here, on the caller's
        thread. Failed attempts are silent (debug log only). Nothing is
        retried for a serial GPS, a pinned WDG_GPS_DEVICE, or after an
        explicit close() (GPS OFF in-game)."""
        if self._gpsd_events:
            return self._gpsd_events.pop(0)
        if not self._gpsd_lost:
            return None
        if self._conn is not None:        # a serial GPS was opened meanwhile
            self._cancel_gpsd_retry()
            return None
        with self._gpsd_connect_lock:
            sock, self._gpsd_connect_result = self._gpsd_connect_result, None
        if sock is not None:
            event = "restored" if self._gpsd_retry_kind == "lost" else "found"
            self._adopt_gpsd(sock)
            log.info("gpsd connection %s at %s:%d", event, GPSD_HOST, GPSD_PORT)
            return event
        thread = self._gpsd_connect_thread
        if thread is not None and thread.is_alive():
            return None
        now = time.monotonic() if now is None else now
        if now < self._gpsd_retry_at:
            return None
        self._gpsd_retry_at = now + GPSD_RECONNECT_INTERVAL
        thread = threading.Thread(target=self._gpsd_connect_worker,
                                  args=(self._gpsd_connect_gen,),
                                  name="wdg-gpsd-reconnect", daemon=True)
        self._gpsd_connect_thread = thread
        try:
            thread.start()
        except RuntimeError as exc:  # interpreter shutting down / no threads left
            log.debug("gpsd reconnect thread not started: %s", exc)
        return None

    def _gpsd_connect_worker(self, generation) -> None:
        sock = self._connect_gpsd()
        if sock is None:
            return
        with self._gpsd_connect_lock:
            if (generation == self._gpsd_connect_gen and self._gpsd_lost
                    and self._gpsd_connect_result is None):
                self._gpsd_connect_result = sock
                return
        try:  # GPS was switched off (or reopened) while this attempt ran
            sock.close()
        except OSError:
            pass

    def _read_gpsd(self) -> None:
        """Drain whatever gpsd has sent (non-blocking) and apply it."""
        if not self._gpsd_sock:
            return
        try:
            while True:
                chunk = self._gpsd_sock.recv(4096)
                if not chunk:
                    self._gpsd_dropped("closed the connection")  # [merge-v3]
                    return
                self._gpsd_buf += chunk
        except BlockingIOError:
            pass
        except OSError as exc:
            self._gpsd_dropped(f"read error ({exc})")  # [merge-v3]
            return
        while b"\n" in self._gpsd_buf:
            line, self._gpsd_buf = self._gpsd_buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            try:
                self._apply_gpsd_report(obj)
            except Exception as exc:
                log.debug("gpsd report parse error: %s — %s", obj, exc)
        # Prevent unbounded growth if gpsd ever sends a line with no \n
        if len(self._gpsd_buf) > 65536:
            self._gpsd_buf = self._gpsd_buf[-4096:]
        self._health_tick()  # [merge-v3]

    # ------------------------------------------------------------------
    # [merge-v3] several receivers: health, source choice, antenna status
    # ------------------------------------------------------------------

    def _after_multi(self) -> None:
        """Log what MultiGps just decided and, when the followed receiver
        changed to a different one, drop the position measured by the old."""
        for level, text in self._multi.drain_events():
            (log.warning if level in ("warn", "alarm") else log.info)("%s", text)
            self._health_events.append((level, text))
        if len(self._health_events) > GPS_HEALTH_EVENTS_MAX:
            del self._health_events[:-GPS_HEALTH_EVENTS_MAX]
        if not self._multi.take_change():
            return
        chosen = self._multi.chosen
        if chosen is None or chosen == self._followed:
            return   # forgotten on a dropped link, or the same receiver again
        switched = self._followed is not None
        self._followed = chosen
        if switched:
            # another receiver: its epochs and position are unrelated to the
            # old one's. Nothing measured by the old receiver is re-served.
            self._gpsd_last_epoch = None
            self._invalidate_position()
        health = self._multi.chosen_device()
        if health is not None and health.last_report is not None:
            self.fix.satellites = health.used
            self.fix.satellites_visible = health.seen
            if health.hdop is not None:
                self.fix.hdop = health.hdop

    def _health_tick(self) -> None:
        """Once per poll while on gpsd: time-based checks (a receiver that
        went silent, the 10 s switch hold, alerts) and the antenna watches."""
        now = time.monotonic()
        self._multi.tick(now)
        self._after_multi()
        health = self._multi.chosen_device()
        if (health is not None and health.last_report is not None
                and not health.reporting(now) and self.fix.valid):
            # the receiver the game follows went silent: its last position is
            # not a fix any more (gpsd keeps the socket open, so nothing else
            # would ever clear it)
            self._invalidate_position()
        if GPS_ANTENNA_WATCH and self._using_gpsd:
            self._service_raw_watches(now)

    def drain_health_events(self) -> list:
        """[(level, text)] since the last call: source switches and alerts.
        level: "info", "ok", "warn" or "alarm". Each was logged already."""
        events, self._health_events = self._health_events, []
        return events

    def health(self, now=None) -> dict:
        """A snapshot for the GPS Health and Hardware Check screens."""
        now = time.monotonic() if now is None else now
        return dict(source=("gpsd" if self._using_gpsd else "serial" if self._conn else
                            "waiting for gpsd" if self._gpsd_lost else "off"),
                    endpoint=self.device, chosen=self._multi.chosen,
                    switches=self._multi.switches,
                    devices=self._multi.snapshot(now) if self._using_gpsd else [],
                    configured=self._gpsd_devices())

    @property
    def chosen_device(self):
        """gpsd path of the receiver the game follows, or None."""
        return self._multi.chosen if self._using_gpsd else None

    def _service_raw_watches(self, now) -> None:
        with self._gpsd_connect_lock:
            ready, self._raw_ready = self._raw_ready, []
        for generation, path, sock in ready:
            self._raw_pending.discard(path)
            if generation != self._raw_gen or path in self._raw_socks:
                try:
                    sock.close()
                except OSError:
                    pass
                continue
            self._raw_socks[path] = [sock, b""]
        for path in list(self._multi.devices):
            if (not path or path in self._raw_socks or path in self._raw_pending
                    or now < self._raw_retry_at.get(path, 0.0)):
                continue
            self._raw_retry_at[path] = now + GPS_RAW_RETRY_INTERVAL
            self._raw_pending.add(path)
            thread = threading.Thread(target=self._raw_watch_worker, args=(self._raw_gen, path),
                                      name="wdg-gpsd-nmea", daemon=True)
            try:
                thread.start()
            except RuntimeError:
                self._raw_pending.discard(path)
        for path, entry in list(self._raw_socks.items()):
            sock = entry[0]
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        raise OSError("closed")
                    entry[1] += chunk
            except BlockingIOError:
                pass
            except OSError:
                try:
                    sock.close()
                except OSError:
                    pass
                del self._raw_socks[path]
                continue
            while b"\n" in entry[1]:
                line, entry[1] = entry[1].split(b"\n", 1)
                if b"TXT" in line[:8]:
                    self._multi.feed_nmea(path, line.decode("ascii", errors="replace").strip(), now)
            if len(entry[1]) > 8192:
                entry[1] = entry[1][-512:]

    def _raw_watch_worker(self, generation, path) -> None:
        sock = None
        try:
            sock = socket.create_connection((GPSD_HOST, GPSD_PORT), timeout=GPSD_CONNECT_TIMEOUT)
            request = json.dumps({"enable": True, "json": False, "raw": 1, "device": path})
            sock.sendall(("?WATCH=%s;\n" % request).encode("ascii", errors="replace"))
            sock.setblocking(False)
        except OSError as exc:
            log.debug("gpsd raw watch for %s failed: %s", path, exc)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            sock = None
        with self._gpsd_connect_lock:
            if sock is not None:
                self._raw_ready.append((generation, path, sock))
        if sock is None:
            self._raw_pending.discard(path)

    def _close_raw_watches(self) -> None:
        self._raw_gen += 1
        with self._gpsd_connect_lock:
            ready, self._raw_ready = self._raw_ready, []
        for sock in [entry[0] for entry in self._raw_socks.values()] + [item[2] for item in ready]:
            try:
                sock.close()
            except OSError:
                pass
        self._raw_socks = {}
        self._raw_pending = set()
        self._raw_retry_at = {}

    @staticmethod
    def _gpsd_number(value):
        """A finite JSON number, or None (bool/str/NaN/inf are not numbers)."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        return value if math.isfinite(value) else None

    @staticmethod
    def _gpsd_utc_seconds(stamp):
        """Seconds of the UTC day from gpsd's ISO-8601 TPV ``time``, or None."""
        if not isinstance(stamp, str):
            return None
        match = re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)(?:Z|[+-]00:?00)?", stamp)
        if not match:
            return None
        hour, minute, second = int(match[1]), int(match[2]), float(match[3])
        if hour > 23 or minute > 59 or not 0 <= second < 61:
            return None
        return hour * 3600 + minute * 60 + second

    def _apply_gpsd_report(self, obj: dict) -> None:
        """Translate a gpsd TPV/SKY JSON report into self.fix and into the same
        measured-position paths the NMEA parsers feed.

        [merge-v3] v2 only filled ``self.fix`` (+ radio_fix). upstream 0.9.20/0.9.21
        added ``wardrive_sample()``, the only position the GPS trail, Auto Map
        + Turf and the wardrive GPS policy accept, and nothing but the NMEA
        parsers recorded it -- so on a gpsd-only rig those features never got
        a position. A TPV with a 2D/3D fix now records a wardrive sample too:

        * ``observed_at`` is the monotonic time this report was RECEIVED;
        * a TPV whose ``time`` equals the previous accepted TPV's is the same
          receiver epoch replayed (gpsd emits several TPVs per cycle on some
          receivers, and replays its last one to a new client) and may not
          make that position look fresh again, mirroring upstream's repeated-epoch
          rule for GGA; a different position under the same ``time`` is
          contradictory and invalidates, as it does for GGA;
        * no fix (mode < 2), a lat/lon that is present but not a usable
          number, or 0,0 invalidates instead of leaving an older sample to be
          re-served; a TPV that simply carries no lat/lon is ignored.

        radio_fix() keeps v2's stricter gate (3D, HDOP<=3, >=4 sats)."""
        if not isinstance(obj, dict):
            return
        cls = obj.get("class")
        # [merge-v3] gpsd tags each report with its receiver. Every report
        # updates that receiver's health; only the receiver the game follows
        # goes on into the fix below (see gps_multi.py for the choice).
        if cls in ("DEVICES", "DEVICE"):
            self._multi.note_devices(obj, time.monotonic())
            self._after_multi()
            return
        if cls in ("TPV", "SKY"):
            self._multi.feed(obj, time.monotonic())
            self._after_multi()
            source = obj.get("device") if isinstance(obj.get("device"), str) else ""
            if not self._multi.is_chosen(source):
                return
        if cls == "TPV":
            mode = obj.get("mode", 0)  # 0/1=no fix, 2=2D, 3=3D
            if isinstance(mode, bool) or not isinstance(mode, int):
                mode = 0
            self.fix.fix_quality = 1 if mode >= 2 else 0
            t = obj.get("time", "")
            if isinstance(t, str) and "T" in t:
                self.fix.timestamp = t.split("T", 1)[1].rstrip("Z").replace(":", "")[:9]
            if mode < 2:
                self._gpsd_last_epoch = None
                self._invalidate_position()
                return
            if "lat" not in obj and "lon" not in obj:
                # gpsd sends partial TPVs (time/speed only) between full ones.
                # They carry no position: neither refresh nor invalidate; the
                # last sample simply ages out after WARDRIVE_SAMPLE_MAX_AGE.
                return
            lat, lon = self._gpsd_number(obj.get("lat")), self._gpsd_number(obj.get("lon"))
            if (lat is None or lon is None  # present but not a finite number
                    or not -90 <= lat <= 90 or not -180 <= lon <= 180
                    or (lat == 0.0 and lon == 0.0)):
                self._gpsd_last_epoch = None
                self._invalidate_position()
                return
            altitude = self._gpsd_number(obj.get("altMSL"))
            if altitude is None:
                altitude = self._gpsd_number(obj.get("alt"))
            speed = self._gpsd_number(obj.get("speed"))
            utc_seconds = self._gpsd_utc_seconds(t)
            previous = self._gpsd_last_epoch
            if utc_seconds is not None and previous is not None and previous[0] == t:
                if previous[1] != lat or previous[2] != lon:
                    self._gpsd_last_epoch = None
                    self._invalidate_position()
                return  # same epoch, same place: nothing new was measured
            self.fix.latitude, self.fix.longitude = lat, lon
            if altitude is not None:
                self.fix.altitude = altitude
            if speed is not None and speed >= 0:
                self.fix.speed_knots = speed * 1.9438445  # m/s -> knots
            self.fix.valid = True
            now = time.monotonic()
            if utc_seconds is None:
                # A fix with no usable UTC time cannot be told apart from a
                # replay, so it may place the player (self.fix above, as v2
                # did) but is not accepted as a fresh measured sample.
                self._gpsd_last_epoch = None
                self._wardrive_position = None
                self._radio_position = None
                return
            self._gpsd_last_epoch = (t, lat, lon)
            self._record_wardrive_sample(lat, lon, utc_seconds, now, "TPV")
            sats, hdop = self.fix.satellites, self.fix.hdop
            if mode >= 3 and sats >= 4 and math.isfinite(hdop) and hdop <= 3:
                self._radio_position = dict(
                    lat=lat, lon=lon, observed_at=now,
                    quality=self.fix.fix_quality or 1,
                    sats=sats, hdop=hdop, utc_seconds=utc_seconds)
            else:
                self._radio_position = None
        elif cls == "SKY":
            sats = obj.get("satellites")
            if isinstance(sats, list):
                self.fix.satellites_visible = len(sats)
                self.fix.satellites = sum(
                    1 for s in sats if isinstance(s, dict) and s.get("used"))
            else:
                # [merge-v3] gpsd >= 3.22 also sends SKY reports without the
                # per-satellite list, carrying only the nSat/uSat counts.
                visible, used = obj.get("nSat"), obj.get("uSat")
                if isinstance(visible, int) and not isinstance(visible, bool) and visible >= 0:
                    self.fix.satellites_visible = visible
                if isinstance(used, int) and not isinstance(used, bool) and used >= 0:
                    self.fix.satellites = used
            hdop = self._gpsd_number(obj.get("hdop"))
            if hdop is not None and hdop >= 0:
                self.fix.hdop = hdop

    @staticmethod
    def _probe_nmea(device: str, baud: int) -> bool:
        """Quick check if a serial port outputs NMEA sentences."""
        try:
            conn = serial.Serial(port=device, baudrate=baud, timeout=2)
            data = conn.read(512)
            conn.close()
            return b"$GP" in data or b"$GN" in data
        except Exception:
            return False

    def _try_open(self, device: str) -> bool:
        """Try to open a serial port as GPS. Returns True on success."""
        if self.gpsd_claims(device):
            # [merge-v3] this port is gpsd's. Every direct open goes through
            # here (launch, GPS ON, the 5 s UART retry), so this is the one
            # place that keeps the game off it; look for gpsd instead.
            log.debug("not opening %s: gpsd is configured to own it", device)
            if not self._using_gpsd and not self._gpsd_lost:
                self._wait_for_gpsd()
            return False
        if not os.path.exists(device):
            return False
        if not os.access(device, os.R_OK):
            log.warning("No read access to %s", device)
            return False
        try:
            self._conn = serial.Serial(
                port=device,
                baudrate=self._baud,
                timeout=0,
            )
            self._conn.reset_input_buffer()
            self._invalidate_position()
            self._recent_rmc = None
            self._last_gga = None
            self._available = True
            log.info("GPS opened: %s @ %d baud", device, self._baud)
            return True
        except Exception as exc:
            log.debug("GPS open failed on %s: %s", device, exc)
            return False

    @staticmethod
    def _auto_detect(exclude: Optional[set] = None) -> Optional[str]:
        """Scan common serial paths for a GPS device (NMEA probe)."""
        skip = exclude or set()
        candidates = sorted(
            glob.glob("/dev/ttyUSB*")
            + glob.glob("/dev/ttyACM*")
            + glob.glob("/dev/ttyAMA*")
        )
        for path in candidates:
            if path in skip:
                continue
            if not os.access(path, os.R_OK):
                continue
            try:
                conn = serial.Serial(port=path, baudrate=9600, timeout=2)
                data = conn.read(512)
                conn.close()
                if b"$GP" in data or b"$GN" in data:
                    log.info("GPS auto-detected on %s", path)
                    return path
                log.debug("GPS probe %s — no NMEA data", path)
            except Exception:
                continue
        return None

    def close(self) -> None:
        if self._gpsd_sock:  # [merge]
            try:
                self._gpsd_sock.close()
            except OSError:
                pass
            self._gpsd_sock = None
        self._using_gpsd = False
        self._gpsd_last_epoch = None  # [merge-v3]
        self._close_raw_watches()     # [merge-v3]
        self._multi.reset()           # [merge-v3] receivers report again on reconnect
        self._multi.take_change()
        # [merge-v3] An explicit close ends any reconnect or wait for gpsd.
        self._cancel_gpsd_retry()
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        self._available = False
        self._invalidate_position()
        self._recent_rmc = None
        self._last_gga = None

    @property
    def fd(self) -> int:
        """File descriptor for urwid watch_file."""
        if self._gpsd_sock is not None:  # [merge]
            return self._gpsd_sock.fileno()
        if self._conn is None:
            raise RuntimeError("GPS port not open")
        return self._conn.fileno()

    # ------------------------------------------------------------------
    # Reading & parsing
    # ------------------------------------------------------------------

    def read_available(self) -> List[str]:
        """Non-blocking read — return complete NMEA sentences. In gpsd mode
        gpsd sends JSON (not NMEA), so we parse it here and return []."""
        if self._using_gpsd:  # [merge]
            self._read_gpsd()
            return []
        if not self._conn:
            return []
        try:
            waiting = self._conn.in_waiting
            if waiting <= 0:
                return []
            raw = self._conn.read(waiting)
            return self._buf.feed(raw)
        except Exception as exc:
            log.debug("GPS read error: %s", exc)
            self.close()
            return []

    def process_sentences(self, sentences: List[str]) -> None:
        """Parse NMEA sentences and update self.fix."""
        for s in sentences:
            try:
                self._parse(s)
            except Exception as exc:
                log.debug("GPS parse error: %s — %s", s.strip(), exc)

    def _parse(self, sentence: str) -> None:
        sentence = sentence.strip()
        body, marker, checksum = sentence.partition("*")
        kind = body.split(",", 1)[0]
        is_position = kind in ("$GPGGA", "$GNGGA", "$GPRMC", "$GNRMC")
        checksum_ok = False
        if marker and re.fullmatch(r"[0-9a-fA-F]{2}", checksum):
            expected = 0
            for character in body[1:]:
                expected ^= ord(character)
            checksum_ok = expected == int(checksum, 16)
        if not checksum_ok:
            if is_position:
                self._invalidate_position()
            return
        parts = body.split(",")
        if len(parts) < 3:
            if is_position:
                self._invalidate_position()
            return
        try:
            if kind in ("$GPGGA", "$GNGGA"):
                self._parse_gga(parts)
            elif kind in ("$GPRMC", "$GNRMC"):
                self._parse_rmc(parts)
            elif kind in ("$GPGSV", "$GLGSV", "$GNGSV", "$GBGSV", "$GAGSV"):
                self._parse_gsv(parts)
        except (ValueError, IndexError, OverflowError):
            if is_position:
                self._invalidate_position()
            raise

    @staticmethod
    def _utc_seconds(value: str) -> float:
        if not re.fullmatch(r"\d{6}(?:\.\d+)?", value):
            raise ValueError("invalid NMEA UTC time")
        hour, minute, second = int(value[:2]), int(value[2:4]), float(value[4:])
        if hour > 23 or minute > 59 or not 0 <= second < 60:
            raise ValueError("NMEA UTC time out of bounds")
        return hour * 3600 + minute * 60 + second

    @staticmethod
    def _position(parts, lat_index, lon_index):
        lat = GpsManager._coordinate(parts[lat_index], parts[lat_index + 1], True)
        lon = GpsManager._coordinate(parts[lon_index], parts[lon_index + 1], False)
        return lat, lon

    @staticmethod
    def _coordinate(value: str, direction: str, latitude: bool) -> float:
        width = 4 if latitude else 5
        if not re.fullmatch(r"\d{" + str(width) + r"}(?:\.\d+)?", value):
            raise ValueError("incomplete NMEA coordinate")
        if direction not in (("N", "S") if latitude else ("E", "W")):
            raise ValueError("invalid NMEA direction")
        degrees, minutes = int(value[:width - 2]), float(value[width - 2:])
        limit = 90 if latitude else 180
        if minutes >= 60 or degrees > limit or (degrees == limit and minutes != 0):
            raise ValueError("NMEA coordinate out of bounds")
        coordinate = degrees + minutes / 60
        return -coordinate if direction in ("S", "W") else coordinate

    @staticmethod
    def _consistent_positions(first, second):
        utc_delta = abs(first["utc_seconds"] - second["utc_seconds"])
        utc_delta = min(utc_delta, 86400 - utc_delta)  # midnight rollover
        if utc_delta > RADIO_FIX_RMC_TIME_TOLERANCE:
            return False
        lat1, lat2 = math.radians(first["lat"]), math.radians(second["lat"])
        dlat = lat2 - lat1
        dlon = math.radians(second["lon"] - first["lon"])
        hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        distance = 6371000 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, hav))))
        return distance <= RADIO_FIX_RMC_DISTANCE_METRES

    def _parse_gga(self, p: List[str]) -> None:
        """$GPGGA: time, lat, N/S, lon, E/W, quality, sats, hdop, alt, ..."""
        if len(p) < 10:
            raise ValueError("incomplete GGA")
        quality = int(p[6]) if p[6] else 0
        sats = int(p[7]) if p[7] else 0
        hdop = float(p[8]) if p[8] else 99.9
        utc_seconds = self._utc_seconds(p[1])
        if quality not in range(9) or not 0 <= sats <= 99 or not math.isfinite(hdop) or hdop < 0:
            raise ValueError("invalid GGA quality metrics")
        self.fix.fix_quality, self.fix.satellites, self.fix.hdop = quality, sats, hdop
        self.fix.timestamp = p[1]
        if quality == 0:
            self._invalidate_position()
            return
        lat, lon = self._position(p, 2, 4)
        altitude = float(p[9]) if p[9] else self.fix.altitude
        if not math.isfinite(altitude):
            raise ValueError("invalid GGA altitude")
        now = time.monotonic()
        position = dict(lat=lat, lon=lon, observed_at=now, quality=quality,
                        sats=sats, hdop=hdop, utc_seconds=utc_seconds)
        credible = quality in (1, 2, 4, 5) and sats >= 4 and hdop <= 3
        rmc = self._recent_rmc
        if (rmc is not None and 0 <= now - rmc["observed_at"] <= RADIO_FIX_RMC_MAX_AGE
                and not self._consistent_positions(position, rmc)):
            self._invalidate_position()
            return
        previous = self._last_gga
        self._last_gga = position
        repeated_epoch = previous is not None and previous["utc_seconds"] == utc_seconds
        if repeated_epoch and (previous["lat"] != lat or previous["lon"] != lon):
            self._invalidate_position()
            return
        self.fix.latitude, self.fix.longitude, self.fix.altitude = lat, lon, altitude
        self.fix.valid = True
        self._record_wardrive_sample(lat, lon, utc_seconds, now, "GGA")
        if repeated_epoch:
            # Repeated receiver output from the same epoch cannot make an old
            # observation fresh or count as several stable AUTO samples.
            if not credible:
                self._radio_position = None
            return
        self._radio_position = position if credible else None

    def _parse_rmc(self, p: List[str]) -> None:
        """$GPRMC: time, status, lat, N/S, lon, E/W, speed, ..."""
        if len(p) < 8:
            raise ValueError("incomplete RMC")
        utc_seconds = self._utc_seconds(p[1])
        if p[2] == "V":
            self.fix.timestamp = p[1]
            self._recent_rmc = None
            self._invalidate_position()
            return
        if p[2] != "A":
            raise ValueError("invalid RMC status")
        lat, lon = self._position(p, 3, 5)
        speed = float(p[7]) if p[7] else 0.0
        if not math.isfinite(speed) or speed < 0:
            raise ValueError("invalid RMC speed")
        now = time.monotonic()
        rmc = dict(lat=lat, lon=lon, utc_seconds=utc_seconds, observed_at=now)
        self._recent_rmc = rmc
        position = self._radio_position
        if (position is not None
                and 0 <= now - position["observed_at"] <= RADIO_FIX_MAX_AGE
                and not self._consistent_positions(position, rmc)):
            self._invalidate_position()
            return
        self.fix.valid = True
        self.fix.timestamp = p[1]
        self.fix.latitude, self.fix.longitude, self.fix.speed_knots = lat, lon, speed
        self._record_wardrive_sample(lat, lon, utc_seconds, now, "RMC")

    def _parse_gsv(self, p: List[str]) -> None:
        """$xxGSV: total_msgs, msg_num, sats_in_view, ..."""
        if len(p) < 4:
            return
        prefix = p[0][:3]  # $GP, $GL, $GN, $GB, $GA
        total_visible = int(p[3]) if p[3] else 0
        self._gsv_visible[prefix] = total_visible
        self.fix.satellites_visible = sum(self._gsv_visible.values())

    @staticmethod
    def _to_decimal(value: str, direction: str) -> float:
        """Convert NMEA ddmm.mmmm to decimal degrees."""
        return GpsManager._coordinate(value, direction, direction in ("N", "S"))
