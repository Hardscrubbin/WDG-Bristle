# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""Nearby alerts: the signatures file, matching, and "is something following me".

Pure logic. An Observation is one sighting of one device, from a node or from Kismet. Nothing here
talks to a device; it only looks at what the radios already heard.

Signatures file (signatures.yaml, next to this module; you may edit it). One entry per signature:

    - id: axon-oui                 a name for the entry, unique
      type: axon                   which alert it belongs to: axon | flock | tracker | pentest | rig | skimmer
      label: Axon device           what the alert says
      kind: any                    bt | wifi | any
      mac_prefix: ["00:25:DF"]     every test that is present must match; inside a list, any one value
      source: "IEEE OUI registry: ..."   where each value comes from. Required. No source, no entry.

Tests: mac_prefix (MAC or BSSID starts with), name_exact, name_prefix (Bluetooth name or Wi-Fi
SSID; upper/lower case is ignored), company_id (Bluetooth SIG company id in the manufacturer
data), mfg_type (first byte of the manufacturer data after the company id), service_uuid16
(a 16-bit service UUID in the advertisement), cod_major (Bluetooth Classic major device class),
bristle (true: an advertisement of a Bristle board). ``weight`` is used by the skimmer score only.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass, field

TYPES = ("axon", "flock", "tracker", "pentest", "rig", "skimmer")
LIST_TESTS = ("mac_prefix", "name_exact", "name_prefix", "company_id", "mfg_type", "service_uuid16", "cod_major")


@dataclass
class Observation:
    kind: str                         # "bt" or "wifi"
    mac: str                          # 12 hex, lower case
    name: str = ""                    # Bluetooth name or SSID
    rssi: int | None = None
    t: float = 0.0                    # unix seconds
    lat: float | None = None
    lon: float | None = None
    heard_by: str = ""                # "node 3", "Kismet hci1", ...
    company_id: int | None = None
    mfg_type: int | None = None
    service_uuids: tuple = ()         # 16-bit service UUIDs as ints
    cod: int | None = None            # 24-bit Class of Device (Bluetooth Classic), if the source gives it
    classic: bool | None = None       # True: Bluetooth Classic, False: LE, None: not known
    bristle_hwid: str = ""            # an advertisement of a Bristle board: its hwid
    bristle_owned: bool = False


def norm_mac(text: str) -> str:
    return "".join(c for c in str(text).lower() if c in "0123456789abcdef")


def cod_major(cod: int | None) -> int | None:
    """Major device class: bits 12..8 of the 24-bit Class of Device."""
    return None if cod is None else (int(cod) >> 8) & 0x1F


@dataclass
class Signature:
    id: str
    type: str
    label: str
    source: str
    kind: str = "any"
    weight: int = 0
    tests: dict = field(default_factory=dict)       # test name -> tuple of values
    bristle: bool = False

    def matches(self, o: Observation) -> bool:
        if self.kind != "any" and self.kind != o.kind:
            return False
        t = self.tests
        if self.bristle and not o.bristle_hwid:
            return False
        if "mac_prefix" in t and not any(o.mac.startswith(p) for p in t["mac_prefix"]):
            return False
        name = o.name.lower()
        if "name_exact" in t and name not in t["name_exact"]:
            return False
        if "name_prefix" in t and not any(name.startswith(p) for p in t["name_prefix"]):
            return False
        if "company_id" in t and o.company_id not in t["company_id"]:
            return False
        if "mfg_type" in t and o.mfg_type not in t["mfg_type"]:
            return False
        if "service_uuid16" in t and not any(u in t["service_uuid16"] for u in o.service_uuids):
            return False
        if "cod_major" in t and cod_major(o.cod) not in t["cod_major"]:
            return False
        return bool(t) or self.bristle


class SignatureError(ValueError):
    pass


# ------------------------------------------------------------------ reading the file
def _scalar(text: str):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(text, 16) if low.startswith("0x") else int(text, 10)
    except ValueError:
        return text


def _split_list(inner: str) -> list:
    items, current, quote = [], "", ""
    for ch in inner:
        if quote:
            current += ch
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            current += ch
        elif ch == ",":
            items.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        items.append(current)
    return [_scalar(item) for item in items]


def _strip_comment(line: str) -> str:
    quote = ""
    for index, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def parse_simple_yaml(text: str) -> dict:
    """The small part of YAML the signatures file uses: top-level ``key: value`` lines and
    ``key:`` followed by a list of ``- key: value`` mappings; values are quoted or bare strings,
    numbers (decimal or 0x hex), true/false, or ``[a, b]`` lists on one line. Anything else is an
    error with its line number, so a typo cannot quietly switch a signature off."""
    result: dict = {}
    current_list = None
    current_item = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        if indent == 0:
            key, sep, value = body.partition(":")
            if not sep:
                raise SignatureError("line %d: expected 'key: value'" % number)
            if value.strip():
                result[key.strip()] = _scalar(value)
                current_list = current_item = None
            else:
                current_list = result[key.strip()] = []
                current_item = None
            continue
        if current_list is None:
            raise SignatureError("line %d: indented text outside a list" % number)
        if body.startswith("- "):
            current_item = {}
            current_list.append(current_item)
            body = body[2:].strip()
        elif current_item is None:
            raise SignatureError("line %d: expected '- key: value'" % number)
        key, sep, value = body.partition(":")
        if not sep or not key.strip():
            raise SignatureError("line %d: expected 'key: value'" % number)
        value = value.strip()
        if value.startswith("["):
            if not value.endswith("]"):
                raise SignatureError("line %d: a list must close on the same line" % number)
            current_item[key.strip()] = _split_list(value[1:-1])
        elif not value:
            raise SignatureError("line %d: '%s' has no value" % (number, key.strip()))
        else:
            current_item[key.strip()] = _scalar(value)
    return result


def _as_list(value) -> list:
    return list(value) if isinstance(value, (list, tuple)) else [value]


def build_signatures(data: dict) -> list[Signature]:
    out: list[Signature] = []
    seen = set()
    for index, entry in enumerate(data.get("signatures") or [], start=1):
        if not isinstance(entry, dict):
            raise SignatureError("entry %d is not a mapping" % index)
        sid = str(entry.get("id") or "")
        where = "entry %d (%s)" % (index, sid or "no id")
        if not sid or sid in seen:
            raise SignatureError("%s: every entry needs its own id" % where)
        seen.add(sid)
        if entry.get("type") not in TYPES:
            raise SignatureError("%s: type must be one of %s" % (where, ", ".join(TYPES)))
        if not str(entry.get("source") or "").strip():
            raise SignatureError("%s: no source. Every value needs a published source." % where)
        kind = str(entry.get("kind") or "any")
        if kind not in ("bt", "wifi", "any"):
            raise SignatureError("%s: kind must be bt, wifi or any" % where)
        tests: dict = {}
        for name in LIST_TESTS:
            if name not in entry:
                continue
            values = _as_list(entry[name])
            if not values:
                raise SignatureError("%s: %s is empty" % (where, name))
            if name == "mac_prefix":
                cleaned = tuple(norm_mac(v) for v in values)
                if any(len(p) < 6 for p in cleaned):
                    raise SignatureError("%s: a mac_prefix needs at least 3 bytes" % where)
                tests[name] = cleaned
            elif name in ("name_exact", "name_prefix"):
                tests[name] = tuple(str(v).lower() for v in values)
            else:
                try:
                    tests[name] = tuple(int(v) for v in values)
                except (TypeError, ValueError):
                    raise SignatureError("%s: %s must be numbers" % (where, name)) from None
        bristle = bool(entry.get("bristle", False))
        if not tests and not bristle:
            raise SignatureError("%s: no test" % where)
        out.append(Signature(sid, entry["type"], str(entry.get("label") or sid), str(entry["source"]),
                             kind, int(entry.get("weight", 0) or 0), tests, bristle))
    return out


def load_signatures(text: str) -> list[Signature]:
    """Uses PyYAML when it is installed and the built-in reader when it is not; both give the same
    result for a file that keeps to the form described at the top of this module."""
    try:
        import yaml
    except ImportError:
        return build_signatures(parse_simple_yaml(text))
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SignatureError(str(exc)) from exc
    if not isinstance(data, dict):
        raise SignatureError("the file must start with 'signatures:'")
    return build_signatures(data)


def match(signatures: list[Signature], o: Observation, types: tuple | None = None) -> list[Signature]:
    return [s for s in signatures if (types is None or s.type in types) and s.matches(o)]


# ------------------------------------------------------------------ following
def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371000.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


@dataclass
class Followed:
    mac: str
    places: int
    minutes: float
    span_m: float
    name: str = ""


class FollowDetector:
    """Flags a device met again and again at places far apart.

    A sighting counts as a new place when it is at least ``distance_m`` from every place this
    device was already seen at. With ``sightings`` places inside ``window_s``, the first and last of
    them at least ``min_span_s`` apart, the device is reported (once; the caller rate-limits
    repeats). Standing still never counts: the places must be apart.

    Limits worth knowing: many phones and trackers change their Bluetooth address every few
    minutes, and then each address is a new device here. A tracker away from its owner keeps one
    address for much longer, which is the case this is for.
    """

    def __init__(self, distance_m: float = 500.0, window_s: float = 3600.0, sightings: int = 3,
                 min_span_s: float = 600.0, max_devices: int = 20000) -> None:
        self.distance_m, self.window_s = float(distance_m), float(window_s)
        self.sightings, self.min_span_s = max(2, int(sightings)), float(min_span_s)
        self.max_devices = max_devices
        self._seen: OrderedDict[str, list] = OrderedDict()      # mac -> [(t, lat, lon)]
        self._names: dict[str, str] = {}

    def observe(self, o: Observation) -> Followed | None:
        if o.lat is None or o.lon is None or not o.mac:
            return None
        places = self._seen.get(o.mac)
        if places is None:
            places = self._seen[o.mac] = []
            while len(self._seen) > self.max_devices:
                old, _ = self._seen.popitem(last=False)
                self._names.pop(old, None)
        else:
            self._seen.move_to_end(o.mac)
        if o.name:
            self._names[o.mac] = o.name
        cutoff = o.t - self.window_s
        places[:] = [p for p in places if p[0] >= cutoff]
        if any(distance_m(o.lat, o.lon, p[1], p[2]) < self.distance_m for p in places):
            return None
        places.append((o.t, o.lat, o.lon))
        if len(places) < self.sightings or places[-1][0] - places[0][0] < self.min_span_s:
            return None
        span = max(distance_m(a[1], a[2], b[1], b[2]) for a in places for b in places)
        return Followed(o.mac, len(places), (places[-1][0] - places[0][0]) / 60.0, span,
                        self._names.get(o.mac, ""))

    def forget(self, mac: str) -> None:
        self._seen.pop(mac, None)
        self._names.pop(mac, None)
