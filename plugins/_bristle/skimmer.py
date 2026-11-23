# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""Possible card skimmers: scoring, the suspects list, where a suspect probably is, and the maths
for walking up to it. Listening only: nothing in here, or anywhere in Bristle, connects to,
pairs with or requests anything from a suspect device. What is known about it comes from the
ordinary scanning every Bluetooth scanner does (Kismet's discovery, the nodes' scan).

What this can and cannot do (also shown on the suspects screen):
  * It finds Bluetooth modules that still look like they came out of the bag: a default serial
    module name, the "Uncategorized" device class, a maker prefix seen on recovered skimmers.
    That is what Bhaskar et al. found on skimmers pulled out of gas pumps (USENIX Security 2019).
  * A skimmer whose installer renamed the module, one that phones home over the cellular network,
    and one with no radio at all are NOT found.
  * Ordinary products use the same modules. A hit is a "possible skimmer", never proof, and no
    hit never means a pump is safe.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .nearby import Observation, Signature, cod_major, distance_m

LIMITS = (
    "Finds Bluetooth serial modules that look factory-fresh: default name, device class",
    "'Uncategorized', a maker prefix seen on recovered skimmers. Listening only.",
    "NOT found: a renamed module, a skimmer that reports over the cellular network (GSM),",
    "a skimmer with no radio. Ordinary products use the same modules.",
    "A hit is a POSSIBLE skimmer. No hit never means a pump is safe.",
)
LOG_THRESHOLD = 20           # a sighting scoring this much is logged
STRONG_RSSI, NEAR_RSSI = -60, -75
SIGHTINGS_HEADER = "time_utc,lat,lon,rssi,heard_by,name,mac,class_of_device,score"
MAX_POINTS = 400             # sightings kept per suspect for the position estimate
STATUS_NEW, STATUS_REPORTED, STATUS_FALSE = "", "reported", "false alarm"


def colon(mac: str) -> str:
    mac = mac.upper()
    return ":".join(mac[i:i + 2] for i in range(0, len(mac), 2))


def cod_text(cod: int | None) -> str:
    if cod is None:
        return "not given"
    return "0x%06X%s" % (cod, " (Uncategorized)" if cod_major(cod) == 31 else "")


def score(o: Observation, matches: list[Signature]) -> tuple[int, list[str]]:
    """(score 0..100, the reasons). Each matching skimmer signature adds its weight once per kind
    of test (name, device class, MAC prefix); a strong signal adds a little. A name on its own
    stays a weak score."""
    best: dict[str, tuple[int, str]] = {}
    for sig in matches:
        if sig.type != "skimmer":
            continue
        group = ("mac" if "mac_prefix" in sig.tests else "class" if "cod_major" in sig.tests else "name")
        if sig.weight > best.get(group, (0, ""))[0]:
            best[group] = (sig.weight, sig.label)
    if not best:
        return 0, []
    total = sum(weight for weight, _label in best.values())
    reasons = [label for _weight, label in best.values()]
    if o.rssi is not None:
        if o.rssi >= STRONG_RSSI:
            total += 10
            reasons.append("strong signal (%d dBm)" % o.rssi)
        elif o.rssi >= NEAR_RSSI:
            total += 5
            reasons.append("fair signal (%d dBm)" % o.rssi)
    return min(100, total), reasons


# ------------------------------------------------------------------ where is it
def rssi_weight(rssi: int) -> float:
    """A sighting's say in the position estimate: 6 dB stronger counts twice as much (signal
    amplitude; a loud sighting was made close to the device)."""
    return 10.0 ** ((max(-110, min(-20, rssi)) + 110) / 20.0)


def rough_range_m(rssi: int) -> float:
    """How far away a device heard at this strength may be. Only good for "about": a module
    inside a metal pump cabinet is much weaker than one in free air."""
    return max(3.0, min(250.0, 10.0 ** ((-55 - rssi) / 22.0)))


@dataclass
class Estimate:
    lat: float
    lon: float
    radius_m: float
    points: int
    directions: int           # how many of the eight compass directions the sightings came from


def estimate(points: list) -> Estimate | None:
    """points: [(t, lat, lon, rssi), ...]. The signal-weighted centre of the sightings, with a
    radius that says how sure it is: the weighted spread of the sightings, never less than the
    range the best sighting allows on its own, and wider while everything was heard from one side."""
    usable = [(p[1], p[2], p[3]) for p in points if p[1] is not None and p[2] is not None and p[3] is not None]
    if not usable:
        return None
    weights = [rssi_weight(r) for _lat, _lon, r in usable]
    total = sum(weights)
    lat = sum(w * p[0] for w, p in zip(weights, usable)) / total
    lon = sum(w * p[1] for w, p in zip(weights, usable)) / total
    spread = math.sqrt(sum(w * distance_m(lat, lon, p[0], p[1]) ** 2 for w, p in zip(weights, usable)) / total)
    best = max(r for _lat, _lon, r in usable)
    sectors = set()
    for p in usable:
        if distance_m(lat, lon, p[0], p[1]) > 3.0:
            sectors.add(int(((bearing_deg(lat, lon, p[0], p[1]) + 22.5) % 360) // 45))
    radius = max(spread, rough_range_m(best) * (0.5 if len(sectors) >= 3 else 1.0))
    if len(usable) == 1:
        radius = rough_range_m(best)
    return Estimate(lat, lon, round(radius, 1), len(usable), len(sectors))


# ------------------------------------------------------------------ hunt maths
def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compass bearing from point 1 to point 2, 0 = north, 90 = east."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def compass(bearing: float) -> str:
    return COMPASS[int(((bearing + 22.5) % 360) // 45)]


def turn(bearing: float, heading: float | None) -> str:
    """Which way to go, said against the way you are moving (or the compass, standing still)."""
    if heading is None:
        return "head " + compass(bearing)
    relative = (bearing - heading + 360.0) % 360.0
    return ("ahead", "ahead right", "right", "behind right", "behind", "behind left", "left",
            "ahead left")[int(((relative + 22.5) % 360) // 45)]


def beep_interval_s(rssi: float) -> float:
    """Seconds between beeps: 1.5 s at -95 dBm and weaker, 0.12 s at -40 dBm and stronger."""
    part = (max(-95.0, min(-40.0, rssi)) + 95.0) / 55.0
    return round(1.5 - part * (1.5 - 0.12), 3)


def meter_bars(rssi: float, bars: int = 30) -> int:
    return int(round((max(-100.0, min(-35.0, rssi)) + 100.0) / 65.0 * bars))


class Hunt:
    """The state of one hunt: a smoothed signal, the strongest reading so far and where it was."""

    def __init__(self, mac: str, smoothing: float = 0.35, stale_s: float = 8.0) -> None:
        self.mac = mac
        self.smoothing, self.stale_s = smoothing, stale_s
        self.level: float | None = None
        self.last_rssi: int | None = None
        self.last_heard = 0.0             # monotonic seconds
        self.readings = 0
        self.best: dict | None = None     # {"rssi", "lat", "lon", "t"}

    def reading(self, rssi: int, mono: float, lat: float | None = None, lon: float | None = None,
                wall: float | None = None) -> bool:
        """True if this is the strongest reading so far (the marker moved)."""
        self.readings += 1
        self.last_rssi, self.last_heard = rssi, mono
        self.level = rssi if self.level is None else self.level + self.smoothing * (rssi - self.level)
        if self.best is None or rssi > self.best["rssi"]:
            self.best = {"rssi": rssi, "lat": lat, "lon": lon, "t": wall if wall is not None else time.time()}
            return True
        return False

    def heard(self, mono: float) -> bool:
        return self.level is not None and mono - self.last_heard <= self.stale_s

    def beep_due(self, mono: float, last_beep: float) -> bool:
        return self.heard(mono) and mono - last_beep >= beep_interval_s(self.level)


# ------------------------------------------------------------------ the suspects list
@dataclass
class Suspect:
    mac: str
    name: str = ""
    cod: int | None = None
    classic: bool | None = None
    first: float = 0.0
    last: float = 0.0
    sightings: int = 0
    score: int = 0                         # the highest score any sighting reached
    reasons: list = field(default_factory=list)
    status: str = STATUS_NEW
    notes: str = ""
    heard_by: list = field(default_factory=list)
    points: list = field(default_factory=list)     # [[t, lat, lon, rssi], ...] newest last
    best: dict | None = None                       # strongest reading in a hunt, with its position

    def estimate(self) -> Estimate | None:
        return estimate(self.points)


class SuspectStore:
    """loot/skimmers/: suspects.json (the list) and sightings.csv (every sighting, appended).

    add() only changes memory and queues the CSV row; save() does the file work and is called from
    a worker thread. Nothing here is ever uploaded anywhere.
    """

    def __init__(self, folder) -> None:
        self.folder = Path(folder)
        self.suspects: dict[str, Suspect] = {}
        self.ignored: set[str] = set()
        self._rows: list[str] = []
        self.dirty = False
        self.load()

    def load(self) -> None:
        try:
            data = json.loads((self.folder / "suspects.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in data.get("suspects", []):
            try:
                known = {k: v for k, v in item.items() if k in Suspect.__dataclass_fields__}
                s = Suspect(**known)
                self.suspects[s.mac] = s
            except TypeError:
                continue
        self.ignored = {str(m) for m in data.get("ignored", [])}

    def is_ignored(self, mac: str) -> bool:
        return mac in self.ignored

    def add(self, o: Observation, points: int, reasons: list[str]) -> Suspect:
        """Record one scored sighting. The caller has checked the ignore list."""
        s = self.suspects.get(o.mac)
        if s is None:
            s = self.suspects[o.mac] = Suspect(mac=o.mac, first=o.t)
        s.last = o.t
        s.sightings += 1
        if o.name:
            s.name = o.name
        if o.cod is not None:
            s.cod = o.cod
        if o.classic is not None:
            s.classic = o.classic
        if points > s.score:
            s.score, s.reasons = points, list(reasons)
        if o.heard_by and o.heard_by not in s.heard_by:
            s.heard_by.append(o.heard_by)
        if o.lat is not None and o.lon is not None and o.rssi is not None:
            s.points.append([round(o.t, 1), round(o.lat, 6), round(o.lon, 6), int(o.rssi)])
            if len(s.points) > MAX_POINTS:
                # keep the strongest half and the newest: old weak sightings add the least
                keep = sorted(s.points[:-50], key=lambda p: p[3], reverse=True)[:MAX_POINTS - 100]
                s.points = sorted(keep, key=lambda p: p[0]) + s.points[-50:]
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(o.t))
        name = o.name.replace(",", ";").replace("\n", " ").replace("\r", " ")
        self._rows.append(",".join(str(v) for v in (
            stamp, "" if o.lat is None else "%.6f" % o.lat, "" if o.lon is None else "%.6f" % o.lon,
            "" if o.rssi is None else o.rssi, o.heard_by.replace(",", ";"), name, colon(o.mac),
            "" if o.cod is None else "0x%06X" % o.cod, points)))
        self.dirty = True
        return s

    def mark(self, mac: str, status: str, notes: str | None = None) -> None:
        s = self.suspects.get(mac)
        if s is None:
            return
        s.status = status
        if notes is not None:
            s.notes = notes
        if status == STATUS_FALSE:
            self.ignored.add(mac)
        else:
            self.ignored.discard(mac)
        self.dirty = True

    def hunt_best(self, mac: str, best: dict | None) -> None:
        s = self.suspects.get(mac)
        if s is not None and best and (s.best is None or best["rssi"] > s.best.get("rssi", -999)):
            s.best = dict(best)
            self.dirty = True

    def rows(self, here: tuple | None = None) -> list[dict]:
        """For the screen: highest score first, false alarms last."""
        out = []
        for s in self.suspects.values():
            est = s.estimate()
            away = distance_m(here[0], here[1], est.lat, est.lon) if here and est else None
            out.append({"suspect": s, "estimate": est, "distance_m": away})
        out.sort(key=lambda r: (r["suspect"].status == STATUS_FALSE, -r["suspect"].score, -r["suspect"].last))
        return out

    def save(self) -> None:
        """Worker thread. Appends the queued sightings and rewrites the list (through a temporary
        file, so a power cut leaves the old list whole)."""
        if not self.dirty and not self._rows:
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        rows, self._rows = self._rows, []
        if rows:
            path = self.folder / "sightings.csv"
            new = not path.exists()
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                if new:
                    handle.write(SIGHTINGS_HEADER + "\n")
                handle.write("\n".join(rows) + "\n")
        self.dirty = False
        data = {"version": 1, "ignored": sorted(self.ignored),
                "suspects": [vars(s) for s in sorted(self.suspects.values(), key=lambda s: s.first)]}
        temporary = self.folder / ".suspects.json.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.folder / "suspects.json")


# ------------------------------------------------------------------ the report
def _when(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(t)) if t else "-"


def report_text(s: Suspect, place: str = "", made_at: float | None = None) -> str:
    """A plain summary to hand to a station manager, the police or the fuel-pump inspectors."""
    est = s.estimate()
    lines = [
        "POSSIBLE CARD SKIMMER: BLUETOOTH SIGHTING REPORT",
        "",
        "This report lists radio observations only. It was made by scanning for Bluetooth devices",
        "while passing by, the way any phone does. The device was never connected to, paired with",
        "or asked for data, and no pump or terminal was opened or touched.",
        "It is a reason to inspect, not proof of a skimmer. Ordinary products use the same",
        "Bluetooth modules. A device that is not listed here may still be a skimmer.",
        "",
        "Report made:       " + _when(made_at if made_at is not None else time.time()),
        "Place:             " + (place or "(fill in: station name and address)"),
        "First seen:        " + _when(s.first),
        "Last seen:         " + _when(s.last),
        "Times seen:        %d" % s.sightings,
        "",
        "Device name:       " + (s.name or "(none received)"),
        "Bluetooth address: " + colon(s.mac),
        "Bluetooth type:    " + ("Classic (BR/EDR)" if s.classic else "Low Energy" if s.classic is False else "not known"),
        "Class of Device:   " + cod_text(s.cod),
        "Score:             %d of 100" % s.score,
        "Why it was listed: " + ("; ".join(s.reasons) if s.reasons else "-"),
        "Heard by:          " + (", ".join(s.heard_by) if s.heard_by else "-"),
        "",
    ]
    if est is not None:
        lines += ["Estimated position: %.6f, %.6f (within about %d m; from %d sightings, %d direction%s)" % (
            est.lat, est.lon, round(est.radius_m), est.points, est.directions, "" if est.directions == 1 else "s"),
            "Map:                https://www.openstreetmap.org/?mlat=%.6f&mlon=%.6f#map=19/%.6f/%.6f" % (
                est.lat, est.lon, est.lat, est.lon)]
    else:
        lines.append("Estimated position: none (no sighting had a GPS position)")
    if s.best and s.best.get("lat") is not None:
        lines.append("Strongest reading on foot: %d dBm at %.6f, %.6f (%s)" % (
            s.best["rssi"], s.best["lat"], s.best["lon"], _when(s.best.get("t", 0))))
    lines += ["", "Signal readings (strongest first; a stronger signal was nearer the device):"]
    readings = sorted(s.points, key=lambda p: p[3], reverse=True)[:25]
    if not readings:
        lines.append("  (none with a position)")
    for t, lat, lon, rssi in readings:
        lines.append("  %4d dBm   %.6f, %.6f   %s" % (rssi, lat, lon, _when(t)))
    lines += ["", "Notes:", "  " + (s.notes or "(none)"), "",
              "Not detected by this method: a renamed module, a skimmer that reports over the",
              "cellular network, a skimmer with no radio.", ""]
    return "\n".join(lines)
