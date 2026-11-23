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

Only a find that has been written, or counted as dropped, is acknowledged to the node. Until then
the node keeps it in its flash queue.
"""
from __future__ import annotations

import collections
import time

from . import proto
from .gpsplace import Fix, GpsTrack

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


def decide(track: GpsTrack, find_time: float, now: float):
    """(PLACE, fix) | (HOLD, None) | (DROP, reason). ``now`` is this core's clock (unix seconds)."""
    fix = track.place(find_time)
    if fix is not None:
        return PLACE, fix
    latest = track.latest_time()
    if latest is not None and latest > find_time:
        return DROP, "no GPS position at that time"          # the track has moved past it
    if now - find_time <= WAIT_FOR_NEXT_FIX_S:
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
    """Owned by the plugin; every method runs on the game thread."""

    def __init__(self, app, delivery, say=lambda text: None) -> None:
        self.app = app
        self.delivery = delivery
        self.say = say
        self.track = GpsTrack()
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
        gps = getattr(self.app, "gps", None)
        sample = None
        try:
            sample = gps.wardrive_sample() if gps is not None else None
        except Exception:
            sample = None
        if not sample or sample.get("observed_at") == self._last_sample:
            return
        self._last_sample = sample.get("observed_at")
        mono_now = time.monotonic() if mono_now is None else mono_now
        wall_now = time.time() if wall_now is None else wall_now
        try:
            hdop = float(sample.get("hdop") or 0.0)
            fix = Fix(fix_time(sample, mono_now, wall_now), float(sample["lat"]), float(sample["lon"]),
                      float(sample.get("altitude") or 0.0),
                      round(hdop * 5.0, 1) if 0 < hdop < 99 else 0.0)   # the same metres LootManager derives
        except (KeyError, TypeError, ValueError):
            return
        if self.track.add(fix):
            self.last_gps_wall = wall_now

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
        deadline = time.perf_counter() + budget_s
        now = time.time() if now is None else now
        again = collections.deque()
        for queue in (self.held, self.incoming):
            while queue:
                if not final and time.perf_counter() >= deadline:
                    break
                find = queue.popleft()
                action, value = decide(self.track, find.t, now)
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
                self.delivery.node(find.hwid).processed(find.seq)
        self.held = again + self.held              # what was not reached keeps its place
        while len(self.held) > MAX_HELD:
            find = self.held.popleft()
            self.counts["no_gps"] += 1
            self.delivery.node(find.hwid).processed(find.seq)

    # ------------------------------------------------------------------ loot
    def _write(self, find, fix) -> None:
        loot = getattr(self.app, "loot", None)
        if loot is None:
            self.counts["not_saved"] += 1
            return
        position = {"lat": round(fix.lat, 7), "lon": round(fix.lon, 7),
                    "altitude": round(fix.alt or 0.0, 1), "accuracy": fix.acc or 0.0}
        fields = find.fields
        rssi = int(fields.get("r", "-100") or -100)
        if find.kind == "AP":
            self._write_wifi(loot, find, fix, position, fields, rssi)
        else:
            self._write_bt(loot, find, fix, position, fields, rssi)
        self._recent.append(time.monotonic())

    def _write_wifi(self, loot, find, fix, position, fields, rssi) -> None:
        from watchdogs.app_state import Network
        channel = int(fields.get("c", "0") or 0)
        bssid = colon_mac(fields["b"])
        net = Network(ssid=csv_safe(fields.get("e", "")), bssid=bssid, channel=str(channel),
                      auth=AUTH_NAMES.get(int(fields.get("k", "15") or 15), "UNKNOWN"), rssi=str(rssi),
                      band="5" if channel > 14 else "2.4")
        if not loot.save_wardriving_network(net, observed_at=find.t, position=position,
                                            positioned_only=True, append_new=True):
            self.counts["not_saved"] += 1
            return
        self.counts["written_wifi"] += 1
        app = self.app
        state = getattr(app, "state", None)
        if state is not None and bssid not in self._scan_bssids:
            self._scan_bssids.add(bssid)
            if all(getattr(n, "bssid", None) != bssid for n in state.networks):
                state.networks.append(net)
        known, gain = getattr(app, "_known_wifi", None), getattr(app, "gain_xp", None)
        if known is None or gain is None:
            return
        if bssid in known:
            gain(1)                                 # seen before: the same XP as every other source
            return
        known.add(bssid)
        gain(15)
        app.msg("[WiFi] %s Ch:%d (node %s)" % (net.ssid, channel, find.node))
        self._sprite("WifiNetwork", "wifi_networks", fix, bssid, net.ssid, channel, rssi)

    def _write_bt(self, loot, find, fix, position, fields, rssi) -> None:
        mac = colon_mac(fields["m"])
        name = csv_safe(fields.get("nm", ""))
        if not loot.save_wardriving_bt(mac, rssi, name, observed_at=find.t, position=position,
                                       positioned_only=True, append_new=True):
            self.counts["not_saved"] += 1
            return
        self.counts["written_bt"] += 1
        session = getattr(loot, "session_path", None)
        if session != self._bt_session:
            self._bt_session, self._bt_saved = session, set()
        if mac not in self._bt_saved:
            loot.save_bt_device(mac, rssi, name, False, False, append_new=True)
            self._bt_saved.add(mac)
        app = self.app
        known, gain = getattr(app, "_known_ble", None), getattr(app, "gain_xp", None)
        if known is None or gain is None:
            return
        if mac in known:
            gain(1)
            return
        known.add(mac)
        gain(10)
        app.msg("[BLE] %s %s %ddBm (node %s)" % (name, mac[-8:], rssi, find.node))
        self._sprite("BleDevice", "ble_devices", fix, mac, name, rssi)

    def _sprite(self, cls_name, list_name, fix, *args) -> None:
        """A marker on the map, at the place the find was made."""
        try:
            import pyxel
            from watchdogs import app as game
            sprite = getattr(game, cls_name)(fix.lat, fix.lon, *args)
            sprite.spawn_frame = pyxel.frame_count
            getattr(self.app, list_name).append(sprite)
        except Exception:
            pass                                    # the find is saved; only the marker is missing
