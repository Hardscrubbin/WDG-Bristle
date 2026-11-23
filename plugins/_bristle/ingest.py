"""[bristle] Finds from the nodes -> a position from the uConsole's GPS -> WDG's loot.

A node has no GPS. Every find carries the time the node saw it (the node's clock is set by this
core), and the game keeps a rolling track of fresh, measured GPS positions (the same
``wardrive_sample`` the rest of WDG uses for wardriving). A find is placed where the uConsole was
at the find's time.

Rules (decide() below is the whole of it):
  * a position at that time (between two fixes close together, or a fix within a few seconds):
    the find is written through LootManager's own writers, exactly like a Kismet or SOLO find;
  * the GPS track ends before the find's time: wait a few seconds for the next fix;
  * otherwise there is no position for it: it is counted as "no GPS" and not written.
Nothing is ever written at 0,0, and nothing is written at a position from a later time.

The track runs on "track time" (gpsplace.ClockMap): it follows the monotonic clock, so a
correction of the uConsole's clock mid-drive moves neither the fixes nor the finds stamped before
it. The track is also kept on disk by the service, so finds a node made before the game was
restarted are still placed.

Only a find that has been written, or counted as dropped, is acknowledged to the node. Until then
the node keeps it in its flash queue.

Everything that touches the game goes through host.WdgHost.
"""
from __future__ import annotations

import collections
import time

from . import proto
from .gpsplace import Fix, GpsTrack
from .host import WdgHost

PLACE, HOLD, DROP = "place", "hold", "drop"
WAIT_FOR_NEXT_FIX_S = 7.0        # how long past its own time a find waits for the GPS to catch up
MAX_HELD = 5000                  # finds waiting for a fix; beyond this the oldest are counted as no GPS
FRAME_BUDGET_S = 0.003           # loot writing per game frame

# proto security code -> the names LootManager's WiGLE mapping knows
AUTH_NAMES = {
    proto.SECURITY["OPEN"]: "OPEN", proto.SECURITY["WEP"]: "WEP", proto.SECURITY["WPA_PSK"]: "WPA",
    proto.SECURITY["WPA2_PSK"]: "WPA2", proto.SECURITY["WPA_WPA2_PSK"]: "WPA/WPA2",
    proto.SECURITY["WPA2_ENTERPRISE"]: "WPA2-EAP", proto.SECURITY["WPA3_PSK"]: "WPA3",
    proto.SECURITY["WPA2_WPA3_PSK"]: "WPA2/WPA3", proto.SECURITY["WAPI"]: "WAPI",
    proto.SECURITY["OWE"]: "OWE", proto.SECURITY["WPA3_ENTERPRISE"]: "WPA3-EAP",
}


def decide(track: GpsTrack, find_time: float, now: float, clock=None):
    """(PLACE, fix) | (HOLD, None) | (DROP, reason).

    Without ``clock``: find_time and now are both on the track's own time. With a ClockMap:
    find_time is the node's (wall-clock) time and is turned into track time first; ``now`` is
    track time."""
    when = find_time if clock is None else clock.to_track(find_time)
    if when is None:
        return DROP, "no GPS position at that time"          # the game was not running then
    fix = track.place(when)
    if fix is not None:
        return PLACE, fix
    latest = track.latest_time()
    if latest is not None and latest > when:
        return DROP, "no GPS position at that time"          # the track has moved past it
    if now - when <= WAIT_FOR_NEXT_FIX_S:
        return HOLD, None                                     # the next fix may still cover it
    return DROP, "no GPS fix"


def csv_safe(text: str) -> str:
    """Names go into LootManager's hand-built CSV rows: a comma would shift the columns."""
    if not text:
        return text
    return text.replace(",", ";").replace('"', "'").replace("\n", " ").replace("\r", " ")


def colon_mac(hex12: str) -> str:
    h = hex12.upper()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def fix_time(sample: dict, mono_now: float, wall_now: float) -> float:
    """The core-clock time a GPS sample was measured (its observed_at is on the monotonic clock)."""
    return wall_now - (mono_now - float(sample["observed_at"]))


class Ingest:
    """Owned by the plugin; every method runs on the game thread.

    ``service`` (the BristleService) brings the shared track, its clock map, the queue of fixes to
    save and the nearby engine. Without it (unit tests of the placing rules) the ingest keeps a
    track of its own on plain wall-clock time."""

    def __init__(self, app, delivery, say=lambda text: None, service=None) -> None:
        self.host = app if isinstance(app, WdgHost) else WdgHost(app)
        self.delivery = delivery
        self.say = say
        self.service = service
        self.track = service.track if service is not None else GpsTrack()
        self.clock = service.clock if service is not None else None
        self.incoming = collections.deque()        # appended by link threads (append is thread-safe)
        self.held = collections.deque()
        self.counts = collections.Counter()        # written_wifi, written_bt, no_gps, not_saved, ...
        self._last_sample = None
        self._bt_session = None
        self._bt_saved = set()
        self._scan_bssids = set()
        self._recent = collections.deque()         # times of written finds, for finds/min
        self.last_gps_wall = 0.0

    # ------------------------------------------------------------------ GPS
    def feed_gps(self, mono_now=None, wall_now=None) -> None:
        mono_now = time.monotonic() if mono_now is None else mono_now
        wall_now = time.time() if wall_now is None else wall_now
        if self.clock is not None and self.clock.note(mono_now, wall_now):
            self.service.clock_jumped = True       # the worker sends every node the time again
        sample = self.host.gps_sample()
        if not sample or sample.get("observed_at") == self._last_sample:
            return
        self._last_sample = sample.get("observed_at")
        try:
            hdop = float(sample.get("hdop") or 0.0)
            measured = (self.clock.track_time(float(sample["observed_at"])) if self.clock is not None
                        else fix_time(sample, mono_now, wall_now))
            fix = Fix(measured, float(sample["lat"]), float(sample["lon"]),
                      float(sample.get("altitude") or 0.0),
                      round(hdop * 5.0, 1) if 0 < hdop < 99 else 0.0)   # the same metres LootManager derives
        except (KeyError, TypeError, ValueError):
            return
        if self.track.add(fix):
            self.last_gps_wall = wall_now
            if self.service is not None:
                self.service.gps_out.append(fix)   # the worker appends it to the track on disk

    # ------------------------------------------------------------------ finds
    def add(self, find) -> None:
        self.incoming.append(find)                 # called from a link thread

    def backlog(self) -> int:
        return len(self.incoming) + len(self.held)

    def per_minute(self, now=None) -> int:
        now = time.monotonic() if now is None else now
        while self._recent and now - self._recent[0] > 60.0:
            self._recent.popleft()
        return len(self._recent)

    def drain(self, budget_s=FRAME_BUDGET_S, final=False, now=None) -> None:
        """Deal with waiting finds for at most budget_s. final=True: nothing may stay held."""
        if self.service is not None and not self.service.track_ready and not final:
            return                                 # the saved track is still being read back
        deadline = time.perf_counter() + budget_s
        if now is None:
            now = time.time()
            if self.clock is not None:
                try:
                    now = self.clock.track_time(time.monotonic())
                except RuntimeError:
                    return                         # no clock reading yet: next frame
        nearby = self.service.nearby if self.service is not None else None
        again = collections.deque()
        for queue in (self.held, self.incoming):
            while queue:
                if not final and time.perf_counter() >= deadline:
                    break
                find = queue.popleft()
                action, value = decide(self.track, find.t, now, self.clock)
                if action == HOLD and not final:
                    again.append(find)
                    continue
                if action == PLACE:
                    try:
                        self._write(find, value)
                    except Exception as exc:        # one bad find never stalls the queue
                        self.counts["not_saved"] += 1
                        self.say("[BRISTLE] Could not save a find from node %s: %s" % (find.node, exc))
                else:
                    self.counts["no_gps"] += 1
                if nearby is not None:              # alerts need no position; following does
                    from .nearby_engine import observation_from_find
                    observation = observation_from_find(find, value if action == PLACE else None)
                    if observation is not None:
                        nearby.incoming.append(observation)
                self.delivery.node(find.hwid).processed(find.seq)
        self.held = again + self.held              # what was not reached keeps its place
        while len(self.held) > MAX_HELD:
            find = self.held.popleft()
            self.counts["no_gps"] += 1
            self.delivery.node(find.hwid).processed(find.seq)

    # ------------------------------------------------------------------ loot
    def _write(self, find, fix) -> None:
        if not self.host.has_loot():
            self.counts["not_saved"] += 1
            return
        position = {"lat": round(fix.lat, 7), "lon": round(fix.lon, 7),
                    "altitude": round(fix.alt or 0.0, 1), "accuracy": fix.acc or 0.0}
        fields = find.fields
        rssi = int(fields.get("r", "-100") or -100)
        if find.kind == "AP":
            self._write_wifi(find, fix, position, fields, rssi)
        else:
            self._write_bt(find, fix, position, fields, rssi)
        self._recent.append(time.monotonic())

    def _write_wifi(self, find, fix, position, fields, rssi) -> None:
        channel = int(fields.get("c", "0") or 0)
        bssid = colon_mac(fields["b"])
        net = self.host.save_wifi(csv_safe(fields.get("e", "")), bssid, channel,
                                  AUTH_NAMES.get(int(fields.get("k", "15") or 15), "UNKNOWN"), rssi,
                                  find.t, position)
        if net is None:
            self.counts["not_saved"] += 1
            return
        self.counts["written_wifi"] += 1
        if bssid not in self._scan_bssids:
            self._scan_bssids.add(bssid)
            self.host.list_network(net)
        if self.host.credit("wifi", bssid, 15):
            self.host.message("[WiFi] %s Ch:%d (node %s)" % (net.ssid, channel, find.node))
            self.host.marker("wifi", fix.lat, fix.lon, bssid, net.ssid, channel, rssi)

    def _write_bt(self, find, fix, position, fields, rssi) -> None:
        mac = colon_mac(fields["m"])
        name = csv_safe(fields.get("nm", ""))
        if not self.host.save_bt(mac, rssi, name, find.t, position):
            self.counts["not_saved"] += 1
            return
        self.counts["written_bt"] += 1
        session = self.host.session_dir()
        if session != self._bt_session:
            self._bt_session, self._bt_saved = session, set()
        if mac not in self._bt_saved:
            self.host.save_bt_device(mac, rssi, name)
            self._bt_saved.add(mac)
        if self.host.credit("bt", mac, 10):
            self.host.message("[BLE] %s %s %ddBm (node %s)" % (name, mac[-8:], rssi, find.node))
            self.host.marker("bt", fix.lat, fix.lon, mac, name, rssi)
