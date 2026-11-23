# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""One place every nearby alert goes through: switches, rate limits, and what the screen, the HUD
line, the sound and the session log each get. Pure logic; the callers do the drawing and writing.
"""
from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass

# type -> (what it is called on the switches screen, sound name, default seconds before the same
# device may alert again)
TYPES = {
    "follow":  ("Following: seen again at places far apart", "follow", 900),
    "tracker": ("Trackers (AirTag, Tile, SmartTag type)", "notice", 900),
    "axon":    ("Axon devices", "notice", 600),
    "flock":   ("Flock Safety cameras", "notice", 600),
    "pentest": ("Wardriving and pentest devices", "notice", 600),
    "rig":     ("Other Bristle rigs", "notice", 600),
    "skimmer": ("Possible skimmer close by", "skimmer", 300),
    "kismet":  ("Kismet's own alerts", "warning", 120),
}
LOG_HEADER = "time_utc,type,key,title,detail,lat,lon,rssi,heard_by"


@dataclass
class Alert:
    type: str
    key: str                  # what the rate limit is per: a MAC, an alert name
    title: str                # short: the popup and the HUD line
    detail: str = ""
    t: float = 0.0            # unix seconds
    lat: float | None = None
    lon: float | None = None
    rssi: int | None = None
    heard_by: str = ""

    def log_row(self) -> str:
        def cell(value) -> str:
            return "" if value is None else str(value).replace(",", ";").replace("\n", " ").replace("\r", " ")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.t))
        return ",".join(cell(v) for v in (stamp, self.type, self.key, self.title, self.detail,
                                          None if self.lat is None else "%.6f" % self.lat,
                                          None if self.lon is None else "%.6f" % self.lon,
                                          self.rssi, self.heard_by))


class AlertCenter:
    def __init__(self, switches: dict | None = None, repeat_s: dict | None = None, type_gap_s: float = 4.0,
                 hud_seconds: float = 45.0, mono=time.monotonic) -> None:
        self.switches = {name: True for name in TYPES}
        self.switches.update(switches or {})
        self.repeat_s = {name: spec[2] for name, spec in TYPES.items()}
        self.repeat_s.update(repeat_s or {})
        self.type_gap_s, self.hud_seconds = type_gap_s, hud_seconds
        self._mono = mono
        self._last_key: dict[tuple, float] = {}
        self._last_type: dict[str, float] = {}
        self._lock = threading.Lock()
        self.popups = collections.deque(maxlen=20)      # for the game thread: popup + sound
        self.to_log = collections.deque(maxlen=2000)    # for the worker thread: the session log
        self.recent = collections.deque(maxlen=60)      # for the alerts screen
        self.latest: tuple[Alert, float] | None = None
        self.counts = collections.Counter()             # raised, off, held (rate limit)

    def is_on(self, alert_type: str) -> bool:
        return bool(self.switches.get(alert_type, True))

    def set_switch(self, alert_type: str, on: bool) -> None:
        self.switches[alert_type] = bool(on)

    def raise_alert(self, alert: Alert) -> bool:
        """False if this alert type is off or the same thing alerted a moment ago."""
        if not self.is_on(alert.type):
            self.counts["off"] += 1
            return False
        now = self._mono()
        with self._lock:
            key = (alert.type, alert.key)
            last = self._last_key.get(key)
            if last is not None and now - last < self.repeat_s.get(alert.type, 300):
                self.counts["held"] += 1
                return False
            last_type = self._last_type.get(alert.type)
            if last_type is not None and now - last_type < self.type_gap_s:
                self.counts["held"] += 1
                return False
            self._last_key[key] = self._last_type[alert.type] = now
            if len(self._last_key) > 5000:
                for old in sorted(self._last_key, key=self._last_key.get)[:1000]:
                    del self._last_key[old]
            if not alert.t:
                alert.t = time.time()
            self.popups.append(alert)
            self.to_log.append(alert)
            self.recent.append(alert)
            self.latest = (alert, now)
            self.counts["raised"] += 1
        return True

    def hud_line(self) -> str:
        """The newest alert, for the map line; empty once it is old."""
        latest = self.latest
        if latest is None or self._mono() - latest[1] > self.hud_seconds:
            return ""
        return latest[0].title

    def silence(self, alert_type: str, key: str) -> None:
        """Never alert for this key again in this run (the device was marked a false alarm)."""
        with self._lock:
            self._last_key[(alert_type, key)] = float("inf")
