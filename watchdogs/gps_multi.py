"""[merge-v3] Several GPS receivers behind one gpsd: health, source choice, alerts.

gpsd tags every TPV/SKY report with the device it came from. With two
receivers (the AIO GP-02 on a UART and a USB dongle) the game used to pour
both streams into one fix, so the position jumped between them. MultiGps keeps
one DeviceHealth per receiver and decides which single receiver the game
follows. GpsManager feeds it and lets only the chosen receiver's reports into
the existing fix / wardrive_sample / radio_fix paths.

Pure logic: no sockets, no threads, no pyxel. Every method takes ``now`` (a
monotonic time) so the simulations can run it at any speed.

Choice      best receiver = 3D fix, then more satellites used, then lower HDOP.
Hysteresis  the game leaves the receiver it follows only when
              * that receiver has no fix (or went silent) and another has one
                -- at once, or
              * another has been CLEARLY better for SWITCH_HOLD seconds without
                a break: a better fix class, or 2+ more satellites used, or
                (same satellites +-1 and HDOP lower by 0.5 or more).
Alerts      one event when a condition starts and one when it clears
            (re-armed): antenna SHORT, antenna OPEN, fix lost while another
            receiver keeps one, "weak antenna?", receiver stopped reporting.
Antenna     only what the receiver itself says in an NMEA TXT sentence
            ($GPTXT ... ANTSTATUS=OK / ANTENNA OPEN ...). Never guessed: a
            receiver that says nothing reads "not reported".
"""
import collections
import math
import re

STALE_AFTER = 5.0          # s without any report: the receiver's fix no longer counts
SILENT_AFTER = 10.0        # s without any report: "stopped reporting"
SWITCH_HOLD = 10.0         # s another receiver must stay clearly better
LOST_FIX_AFTER = 3.0       # s without a fix before "lost its fix" is raised
WEAK_AFTER = 60.0          # s the weak-antenna condition must hold
WEAK_SNR_DB = 10.0         # average used SNR this much below the other receiver
MIN_USED_MARGIN = 2        # "clearly better": this many more satellites used
MIN_HDOP_MARGIN = 0.5      # "clearly better": HDOP lower by this much
MAX_EVENTS = 64
MAX_SATS = 64

ANTENNA_OK, ANTENNA_OPEN, ANTENNA_SHORT = "OK", "OPEN", "SHORT"
NOT_REPORTED = "not reported"

_TXT_RE = re.compile(r"^\$(?:G[A-Z]|BD|QZ)TXT,")
_ANT_RE = re.compile(r"ANT(?:ENNA|STATUS)\s*[=: ]\s*(OK|OPEN|SHORT)\b", re.IGNORECASE)


def nmea_checksum_ok(line: str) -> bool:
    """True for ``$....*HH`` with a matching XOR checksum."""
    if not line.startswith("$") or "*" not in line:
        return False
    body, _, tail = line[1:].rpartition("*")
    if len(tail) < 2:
        return False
    value = 0
    for char in body:
        value ^= ord(char)
    try:
        return value == int(tail[:2], 16)
    except ValueError:
        return False


def antenna_from_nmea(line: str):
    """"OK" / "OPEN" / "SHORT" from a checksum-valid TXT sentence, else None.

    Understands the two forms in the field: u-blox ``$GPTXT,01,01,02,
    ANTSTATUS=OK*3B`` and AT6558-family (GP-02) ``$GPTXT,01,01,01,ANTENNA
    OPEN*25``. Anything else -- including ANTSTATUS=INIT / DONTKNOW -- is None:
    the status then stays whatever was last really reported."""
    if not isinstance(line, str):
        return None
    line = line.strip()
    if not _TXT_RE.match(line) or not nmea_checksum_ok(line):
        return None
    match = _ANT_RE.search(line)
    return match.group(1).upper() if match else None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def label_for(path: str) -> str:
    """A short name for a gpsd device path. Describes the kind of port only."""
    if not path:
        return "GPS"
    low = path.lower()
    if "/serial/by-id/" in low:
        name = path.rsplit("/", 1)[-1]
        if name.lower().startswith("usb-"):
            name = name[4:]
        name = re.sub(r"-if\d+(-port\d+)?$", "", name)
        return ("USB " + name.replace("_", " "))[:26]
    if re.search(r"/tty(acm|usb)\d+$", low):
        return "USB GPS"
    if re.search(r"/(ttyama|ttys|serial)\d+$", low):
        return "AIO GP-02 (UART)"
    return path.rsplit("/", 1)[-1][:26] or "GPS"


class DeviceHealth:
    """What one receiver last told gpsd."""

    def __init__(self, path: str, now: float):
        self.path = path
        self.label = label_for(path)
        self.driver = ""
        self.active = True             # False after gpsd reports it deactivated
        self.first_seen = now
        self.last_report = None        # monotonic time of the last TPV/SKY
        self.mode = 0                  # 0/1 none, 2 = 2D, 3 = 3D
        self.lat = self.lon = None
        self.fix_since = None          # start of the current unbroken fix
        self.no_fix_since = None       # start of the current stretch without one
        self.ever_fixed = False
        self.sats = []                 # [dict(prn, gnss, snr, used)], bounded
        self.seen = 0
        self.used = 0
        self.hdop = None
        self.antenna = None            # None = never reported by the receiver
        self.antenna_at = None

    # -- feeding ---------------------------------------------------------
    def note_tpv(self, obj: dict, now: float) -> None:
        self.last_report = now
        mode = obj.get("mode", 0)
        if isinstance(mode, bool) or not isinstance(mode, int):
            mode = 0
        lat, lon = _number(obj.get("lat")), _number(obj.get("lon"))
        if mode >= 2 and "lat" not in obj and "lon" not in obj:
            # a partial TPV (time/speed only) between full ones: changes nothing
            return
        usable = (mode >= 2 and lat is not None and lon is not None
                  and -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0.0 and lon == 0.0))
        self.mode = mode if usable else min(mode, 1)
        if usable:
            self.lat, self.lon = lat, lon
            self.ever_fixed = True
            self.no_fix_since = None
            if self.fix_since is None:
                self.fix_since = now
        else:
            self.fix_since = None
            if self.no_fix_since is None:
                self.no_fix_since = now

    def note_sky(self, obj: dict, now: float) -> None:
        self.last_report = now
        sats = obj.get("satellites")
        if isinstance(sats, list):
            rows = []
            for sat in sats[:MAX_SATS]:
                if not isinstance(sat, dict):
                    continue
                snr = _number(sat.get("ss"))
                rows.append(dict(prn=sat.get("PRN"), gnss=sat.get("gnssid"),
                                 snr=snr if snr is not None and snr >= 0 else None,
                                 used=sat.get("used") is True))
            self.sats = rows
            self.seen = len(rows)
            self.used = sum(1 for row in rows if row["used"])
        else:
            seen, used = obj.get("nSat"), obj.get("uSat")
            if isinstance(seen, int) and not isinstance(seen, bool) and seen >= 0:
                self.seen = seen
            if isinstance(used, int) and not isinstance(used, bool) and used >= 0:
                self.used = used
        hdop = _number(obj.get("hdop"))
        if hdop is not None and hdop >= 0:
            self.hdop = hdop

    def note_antenna(self, status: str, now: float) -> None:
        self.antenna, self.antenna_at = status, now

    # -- reading ---------------------------------------------------------
    def age(self, now: float):
        return None if self.last_report is None else max(0.0, now - self.last_report)

    def reporting(self, now: float) -> bool:
        age = self.age(now)
        return self.active and age is not None and age <= STALE_AFTER

    def silent(self, now: float) -> bool:
        """It did report once and has now said nothing for SILENT_AFTER s."""
        age = self.age(now)
        return age is not None and (age > SILENT_AFTER or not self.active)

    def has_fix(self, now: float) -> bool:
        return self.reporting(now) and self.mode >= 2

    def fix_class(self, now: float) -> int:
        if not self.has_fix(now):
            return 0
        return 2 if self.mode >= 3 else 1

    def used_snrs(self):
        return [row["snr"] for row in self.sats if row["used"] and row["snr"] is not None]

    def avg_snr(self):
        values = self.used_snrs()
        return sum(values) / len(values) if values else None

    def best_snr(self):
        values = self.used_snrs()
        return max(values) if values else None

    def antenna_text(self) -> str:
        return self.antenna or NOT_REPORTED

    def snapshot(self, now: float, chosen: bool) -> dict:
        return dict(path=self.path, label=self.label, driver=self.driver, in_use=chosen,
                    reporting=self.reporting(now), silent=self.silent(now), active=self.active,
                    mode=self.mode if self.reporting(now) else 0, has_fix=self.has_fix(now),
                    used=self.used, seen=self.seen, avg_snr=self.avg_snr(), best_snr=self.best_snr(),
                    hdop=self.hdop, age=self.age(now), antenna=self.antenna_text(),
                    antenna_reported=self.antenna is not None, lat=self.lat, lon=self.lon,
                    sats=[dict(row) for row in self.sats])


def _clearly_better(candidate: DeviceHealth, current: DeviceHealth, now: float):
    """Why ``candidate`` is clearly better than ``current``, or "" if it is not."""
    a, b = candidate.fix_class(now), current.fix_class(now)
    if a == 0:
        return ""
    if a != b:
        if a < b:
            return ""
        names = {0: "no fix", 1: "2D fix", 2: "3D fix"}
        return "%s vs %s" % (names[a], names[b])
    if candidate.used >= current.used + MIN_USED_MARGIN:
        return "%d satellites used vs %d" % (candidate.used, current.used)
    if (abs(candidate.used - current.used) < MIN_USED_MARGIN
            and candidate.hdop is not None and current.hdop is not None
            and candidate.hdop <= current.hdop - MIN_HDOP_MARGIN):
        return "HDOP %.1f vs %.1f" % (candidate.hdop, current.hdop)
    return ""


def _rank(device: DeviceHealth, now: float):
    hdop = device.hdop if device.hdop is not None else 99.0
    return (device.fix_class(now), device.used, -hdop)


class MultiGps:
    """Per-receiver health + which receiver the game follows + alerts."""

    def __init__(self):
        self.devices = collections.OrderedDict()   # path -> DeviceHealth
        self.chosen = None                         # path, or None before any report
        self.switches = 0
        self._candidate = None                     # (path, since, reason)
        self._events = collections.deque(maxlen=MAX_EVENTS)
        self._raised = {}                          # (kind, path) -> True while active
        self._weak_since = {}                      # path -> since
        self._changed = False                      # chosen changed since take_change()

    # -- feeding ---------------------------------------------------------
    def reset(self) -> None:
        """The gpsd link ended: forget every receiver (they report again on
        reconnect). Alerts already raised are forgotten too, without a
        "cleared" line -- the link going away is reported by GpsManager."""
        self.devices.clear()
        self.chosen = None
        self._candidate = None
        self._raised.clear()
        self._weak_since.clear()
        self._changed = True

    def _device(self, path: str, now: float) -> DeviceHealth:
        device = self.devices.get(path)
        if device is None:
            device = self.devices[path] = DeviceHealth(path, now)
        return device

    def note_devices(self, obj: dict, now: float) -> None:
        """A gpsd DEVICES list or a single DEVICE (activation / removal)."""
        rows = obj.get("devices") if obj.get("class") == "DEVICES" else [obj]
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                continue
            device = self._device(row["path"], now)
            if isinstance(row.get("driver"), str):
                device.driver = row["driver"][:24]
            if "activated" in row:
                activated = row.get("activated")
                device.active = bool(activated) and activated != 0
            elif obj.get("class") == "DEVICE":
                # gpsd announces a removed device as DEVICE without "activated"
                device.active = False
        self.tick(now)

    def feed(self, obj: dict, now: float) -> None:
        """One TPV or SKY report (anything else is ignored)."""
        cls = obj.get("class")
        if cls not in ("TPV", "SKY"):
            return
        path = obj.get("device") if isinstance(obj.get("device"), str) else ""
        if not path and self.chosen:
            path = self.chosen           # untagged report: the receiver in use
        elif path and "" in self.devices:
            # reports were untagged so far and now carry a name: same receiver
            known = self.devices.pop("")
            if path not in self.devices:
                known.path, known.label = path, label_for(path)
                self.devices[path] = known
            if self.chosen == "":
                self.chosen = path
        device = self._device(path, now)
        device.active = True
        if cls == "TPV":
            device.note_tpv(obj, now)
        else:
            device.note_sky(obj, now)
        self.tick(now)

    def feed_nmea(self, path: str, line: str, now: float) -> None:
        status = antenna_from_nmea(line)
        if status is not None:
            self._device(path, now).note_antenna(status, now)

    # -- choice ----------------------------------------------------------
    def is_chosen(self, path: str) -> bool:
        """Does a report from ``path`` belong to the receiver in use? An
        untagged report ("") always does."""
        return not path or self.chosen is None or self.chosen == path

    def take_change(self) -> bool:
        """True once after the followed receiver changed (or was forgotten)."""
        changed, self._changed = self._changed, False
        return changed

    def _say(self, level: str, text: str) -> None:
        self._events.append((level, text))

    def _switch(self, path: str, reason: str) -> None:
        previous = self.devices.get(self.chosen) if self.chosen is not None else None
        self.chosen = path
        self._candidate = None
        self._changed = True
        new = self.devices[path]
        if previous is None:
            if len(self.devices) > 1:
                self._say("info", "[GPS] using %s (%s)" % (new.label, reason))
            return
        self.switches += 1
        self._say("info", "[GPS] switched to %s: %s" % (new.label, reason))

    def _choose(self, now: float) -> None:
        if not self.devices:
            return
        current = self.devices.get(self.chosen) if self.chosen is not None else None
        others = [d for d in self.devices.values() if d is not current]
        if current is None:
            best = max(self.devices.values(), key=lambda d: _rank(d, now))
            self._switch(best.path, "has a fix" if best.has_fix(now) else "first receiver to report")
            return
        if not others:
            return
        best = max(others, key=lambda d: _rank(d, now))
        if not current.has_fix(now):
            if best.has_fix(now):
                why = ("%s stopped reporting" if not current.reporting(now)
                       else "%s lost its fix") % current.label
                self._switch(best.path, why)
            else:
                self._candidate = None
            return
        reason = _clearly_better(best, current, now)
        if not reason:
            self._candidate = None
            return
        if self._candidate is None or self._candidate[0] != best.path:
            self._candidate = (best.path, now, reason)
            return
        if now - self._candidate[1] >= SWITCH_HOLD:
            self._switch(best.path, "better for %d s (%s)" % (SWITCH_HOLD, reason))

    # -- alerts ----------------------------------------------------------
    def _alert(self, kind: str, path: str, active: bool, level: str, raised: str, cleared: str) -> None:
        key = (kind, path)
        if active and key not in self._raised:
            self._raised[key] = True
            self._say(level, raised)
        elif not active and key in self._raised:
            del self._raised[key]
            if cleared:
                self._say("ok", cleared)

    def _alerts(self, now: float) -> None:
        devices = list(self.devices.values())
        for device in devices:
            name = device.label
            self._alert("short", device.path, device.antenna == ANTENNA_SHORT, "alarm",
                        "[GPS] %s: ANTENNA SHORT -- check cable now" % name,
                        "[GPS] %s: antenna short cleared (now %s)" % (name, device.antenna_text()))
            self._alert("open", device.path, device.antenna == ANTENNA_OPEN, "warn",
                        "[GPS] %s: antenna OPEN -- not connected?" % name,
                        "[GPS] %s: antenna no longer open (now %s)" % (name, device.antenna_text()))
            self._alert("silent", device.path, device.silent(now), "warn",
                        "[GPS] %s stopped reporting -- unplugged or powered off?" % name,
                        "[GPS] %s is reporting again" % name)
            others_fixed = any(o.has_fix(now) for o in devices if o is not device)
            lost = (device.ever_fixed and device.reporting(now) and device.mode < 2
                    and device.no_fix_since is not None
                    and now - device.no_fix_since >= LOST_FIX_AFTER and others_fixed)
            key = ("lostfix", device.path)
            if lost:
                self._alert("lostfix", device.path, True, "warn",
                            "[GPS] %s lost its fix; another receiver still has one" % name, "")
            elif key in self._raised and (device.has_fix(now) or not device.reporting(now)
                                          or not others_fixed):
                regained = device.has_fix(now)
                self._alert("lostfix", device.path, False, "warn", "",
                            "[GPS] %s has a fix again" % name if regained else "")
            self._weak(device, devices, now)

    def _weak(self, device: DeviceHealth, devices, now: float) -> None:
        """"weak antenna?": for WEAK_AFTER s this receiver's average used SNR is
        WEAK_SNR_DB below another's, or it sees under half as many satellites."""
        text = ""
        if device.reporting(now):
            for other in devices:
                if other is device or not other.reporting(now):
                    continue
                mine, theirs = device.avg_snr(), other.avg_snr()
                if mine is not None and theirs is not None and mine <= theirs - WEAK_SNR_DB:
                    text = "average SNR %.0f dB vs %.0f dB on %s" % (mine, theirs, other.label)
                elif other.seen >= 4 and device.seen * 2 < other.seen:
                    text = "sees %d satellites vs %d on %s" % (device.seen, other.seen, other.label)
                if text:
                    break
        key = ("weak", device.path)
        if not text:
            self._weak_since.pop(device.path, None)
            self._alert("weak", device.path, False, "warn", "",
                        "[GPS] %s: signal back in line with the other receiver" % device.label)
            return
        since = self._weak_since.setdefault(device.path, now)
        if now - since >= WEAK_AFTER and key not in self._raised:
            self._alert("weak", device.path, True, "warn",
                        "[GPS] %s: weak antenna? %s" % (device.label, text), "")

    # -- driving ---------------------------------------------------------
    def tick(self, now: float) -> None:
        """Re-evaluate choice and alerts. Cheap; call on every report and frame."""
        self._choose(now)
        self._alerts(now)

    def drain_events(self) -> list:
        """[(level, text)] raised since the last call; level is one of
        "info", "ok", "warn", "alarm"."""
        events = list(self._events)
        self._events.clear()
        return events

    def chosen_device(self):
        return self.devices.get(self.chosen) if self.chosen is not None else None

    def snapshot(self, now: float) -> list:
        return [device.snapshot(now, device.path == self.chosen) for device in self.devices.values()]
