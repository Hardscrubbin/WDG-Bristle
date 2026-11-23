# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""[bristle] Nearby alerts and skimmer suspects: the part that runs on Bristle's worker thread.

Takes sightings from two places (the nodes' finds once the game has placed them, and the Bluetooth
devices and alerts Kismet reports) and runs them through the rules in nearby.py, skimmer.py and
alerts.py. It reads and writes files (the suspects list, the alert log), so none of it runs on
the game thread: the game only appends to ``incoming`` and reads plain values.

Listening only. Nothing here sends anything to any device it hears.
"""
from __future__ import annotations

import collections
import os
import time
from pathlib import Path

from . import alerts, kismet_tap, nearby, settings, skimmer
from .nearby import Observation

MAX_PER_PASS = 400
SKIMMER_DIR = "skimmers"                 # inside loot/: kept by the installer, part of every loot backup


def observation_from_find(find, fix) -> Observation | None:
    """A node's find -> a sighting. ``fix`` is where the game placed it (or None)."""
    f = find.fields
    try:
        rssi = int(f.get("r", ""))
    except ValueError:
        rssi = None
    lat, lon = (fix.lat, fix.lon) if fix is not None else (None, None)
    heard = "node %s" % find.node
    if find.kind == "AP":
        return Observation("wifi", nearby.norm_mac(f.get("b", "")), f.get("e", ""), rssi, find.t, lat, lon, heard)

    def integer(key):
        try:
            return int(f[key])
        except (KeyError, ValueError):
            return None
    uuids = []
    for part in f.get("sv", "").split("."):
        try:
            uuids.append(int(part, 16))
        except ValueError:
            continue
    return Observation("bt", nearby.norm_mac(f.get("m", "")), f.get("nm", ""), rssi, find.t, lat, lon, heard,
                       company_id=integer("mf"), mfg_type=integer("mt"), service_uuids=tuple(uuids),
                       classic=False, bristle_hwid=nearby.norm_mac(f.get("bh", "")),
                       bristle_owned=f.get("bo") == "1")


class NearbyEngine:
    def __init__(self, conf: dict, loot_base, registry, place, here, say, signatures_path=None) -> None:
        self.conf = conf
        self.registry = registry
        self._place = place                  # place(unix time) -> (lat, lon) or None
        self._here = here                    # here() -> (lat, lon) or None: where the uConsole is now
        self._say = say
        self.loot_base = Path(loot_base)
        self.incoming = collections.deque(maxlen=20000)      # Observations from the game thread
        self.hunt_readings = collections.deque(maxlen=500)   # (rssi, source) from scanner / node threads
        self.session_dir = ""                # set by the game thread: where this drive's files go
        self.signatures, self.signature_error = [], ""
        path = Path(signatures_path) if signatures_path else Path(__file__).with_name("signatures.yaml")
        try:
            self.signatures = nearby.load_signatures(path.read_text(encoding="utf-8"))
        except (OSError, nearby.SignatureError) as exc:
            self.signature_error = str(exc)
            say("[BRISTLE] signatures.yaml was not loaded (%s): nearby alerts are off until it is fixed" % exc, "error")
        self.center = alerts.AlertCenter(
            switches={name: settings.on(conf, "alert_" + name) for name in settings.ALERT_SWITCHES})
        self.follow = nearby.FollowDetector(
            distance_m=settings.number(conf, "follow_distance_m", 50, 20000),
            window_s=settings.number(conf, "follow_window_min", 5, 24 * 60) * 60,
            sightings=int(settings.number(conf, "follow_sightings", 2, 20)),
            min_span_s=settings.number(conf, "follow_min_minutes", 0, 600) * 60)
        self.suspects = skimmer.SuspectStore(self.loot_base / SKIMMER_DIR)
        self.ignore = settings.mac_list(conf, "ignore_macs")
        self.kismet_wanted = set(settings.name_list(conf, "kismet_alerts"))
        self.alert_score = int(settings.number(conf, "skimmer_alert_score", 1, 100))
        self.alert_rssi = int(settings.number(conf, "skimmer_alert_rssi", -100, -20))
        self.hunt: skimmer.Hunt | None = None
        self.counts = collections.Counter()
        self._kismet_at = {}                 # mac -> Kismet time last handled (it repeats devices)

    # ------------------------------------------------------------------ worker thread
    def process(self) -> None:
        for _ in range(MAX_PER_PASS):
            if not self.incoming:
                break
            self._observe(self.incoming.popleft())
        for _ in range(MAX_PER_PASS):
            if not kismet_tap.devices:
                break
            d = kismet_tap.devices.popleft()
            if self._kismet_at.get(d["mac"]) == d["t"]:
                continue
            self._kismet_at[d["mac"]] = d["t"]
            if len(self._kismet_at) > 20000:
                self._kismet_at.clear()
            position = self._place(d["t"]) or (None, None)
            self._observe(Observation("bt", d["mac"], d["name"], d["rssi"], d["t"], position[0], position[1],
                                      d["heard_by"], cod=d["cod"], classic=d["classic"]))
        while kismet_tap.alerts:
            self._kismet_alert(kismet_tap.alerts.popleft())
        hunt = self.hunt
        while self.hunt_readings:
            rssi, _source = self.hunt_readings.popleft()
            if hunt is not None:
                here = self._here() or (None, None)
                if hunt.reading(rssi, time.monotonic(), here[0], here[1], time.time()):
                    self.suspects.hunt_best(hunt.mac, hunt.best)

    def _mine(self, o: Observation) -> bool:
        if o.mac in self.ignore or o.mac in self.registry.nodes:
            return True
        if o.bristle_hwid and o.bristle_hwid in self.registry.nodes:
            return True
        name = o.name.upper()
        if name.startswith("BRISTLE-") and name[8:].isdigit():
            return self.registry.by_number(int(name[8:])) is not None     # heard by Kismet: one of my nodes
        return False

    def _observe(self, o: Observation) -> None:
        if len(o.mac) != 12:
            return
        self.counts["seen"] += 1
        hunt = self.hunt
        if hunt is not None and o.mac == hunt.mac and o.rssi is not None:
            self.hunt_readings.append((o.rssi, o.heard_by))
        if self._mine(o) or not self.signatures:
            return
        matches = nearby.match(self.signatures, o)
        where = ("%s %s" % (o.name, skimmer.colon(o.mac))).strip()
        signal = "" if o.rssi is None else ", %d dBm" % o.rssi
        raised = set()
        for sig in matches:
            if sig.type == "skimmer" or sig.type in raised:
                continue
            raised.add(sig.type)
            self._raise(alerts.Alert(sig.type, o.mac, "%s nearby" % sig.label,
                                     "%s%s, heard by %s" % (where, signal, o.heard_by or "?"),
                                     o.t, o.lat, o.lon, o.rssi, o.heard_by))
        followed = self.follow.observe(o)
        if followed is not None:
            self._raise(alerts.Alert("follow", o.mac, "Seen at %d places over %d min: %s" % (
                followed.places, round(followed.minutes), (followed.name or skimmer.colon(o.mac))[:24]),
                "%s, places up to %d m apart, heard by %s" % (where, round(followed.span_m), o.heard_by or "?"),
                o.t, o.lat, o.lon, o.rssi, o.heard_by))
        if o.kind == "bt" and not self.suspects.is_ignored(o.mac):
            points, reasons = skimmer.score(o, matches)
            if points >= skimmer.LOG_THRESHOLD:
                self.suspects.add(o, points, reasons)
                self.counts["suspect_sightings"] += 1
                if points >= self.alert_score and o.rssi is not None and o.rssi >= self.alert_rssi:
                    self._raise(alerts.Alert("skimmer", o.mac, "Possible skimmer close by (score %d)" % points,
                                             "%s%s: %s" % (where, signal, "; ".join(reasons)),
                                             o.t, o.lat, o.lon, o.rssi, o.heard_by))

    def _kismet_alert(self, item: dict) -> None:
        header = item.get("header", "")
        if header not in self.kismet_wanted:
            self.counts["kismet_off"] += 1
            return
        position = self._place(item.get("t", 0.0)) or self._here() or (None, None)
        self._raise(alerts.Alert("kismet", header, "Kismet: %s" % header, item.get("text", "")[:200],
                                 item.get("t", 0.0), position[0], position[1], None, "Kismet"))

    def _raise(self, alert: alerts.Alert) -> None:
        if self.center.raise_alert(alert):
            self._say("[ALERT] %s. %s" % (alert.title, alert.detail), "warn")

    def save(self) -> None:
        """Worker thread, every few seconds: the suspects list and this drive's alert log."""
        try:
            self.suspects.save()
        except OSError as exc:
            self._say("[BRISTLE] Could not save the skimmer suspects: %s" % exc, "error")
        if not self.center.to_log:
            return
        rows = []
        while self.center.to_log:
            rows.append(self.center.to_log.popleft().log_row())
        folder = Path(self.session_dir) if self.session_dir else self.loot_base / "bristle"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "bristle_alerts.csv"
            new = not path.exists()
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                if new:
                    handle.write(alerts.LOG_HEADER + "\n")
                handle.write("\n".join(rows) + "\n")
        except OSError as exc:
            self._say("[BRISTLE] Could not write the alert log: %s" % exc, "error")

    # ------------------------------------------------------------------ asked for by the screens
    def set_switch(self, name: str, on: bool) -> None:
        self.center.set_switch(name, on)

    def mark(self, mac: str, status: str, notes=None) -> None:
        self.suspects.mark(mac, status, notes)
        if status == skimmer.STATUS_FALSE:
            self.center.silence("skimmer", mac)

    def export_report(self, mac: str, place: str = "") -> str:
        """Writes the report next to the suspects list. Returns the file's path."""
        suspect = self.suspects.suspects[mac]
        folder = self.loot_base / SKIMMER_DIR / "reports"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ("possible-skimmer-%s-%s.txt" % (mac, time.strftime("%Y%m%d-%H%M%S", time.gmtime())))
        temporary = path.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(skimmer.report_text(suspect, place))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return str(path)
