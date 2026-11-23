# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""[bristle] The one place Bristle reaches into the game.

Every use of the WDG app object by Bristle's logic goes through WdgHost: the GPS sample, the loot
writers, XP and the known-device sets, map markers, the terminal, the Kismet plugin, the
"wardriving" flag, the mute switch. Porting Bristle to another base (docs/PORTING_NG.md) means
writing this class again for that base; ingest.py, service.py, the nearby engine and the core
library do not change.

Runs on the game thread. Nothing here does serial, radio or file work of its own (the loot writers
it calls are the game's own, the same ones every other source uses).
"""
from __future__ import annotations

from pathlib import Path


class WdgHost:
    def __init__(self, app) -> None:
        self.app = app

    # ------------------------------------------------------------------ GPS
    def gps_sample(self):
        """The game's fresh, measured position: {"lat", "lon", "altitude", "hdop", "observed_at"
        (monotonic seconds)} or None."""
        gps = getattr(self.app, "gps", None)
        try:
            return gps.wardrive_sample() if gps is not None else None
        except Exception:
            return None

    # ------------------------------------------------------------------ loot
    def has_loot(self) -> bool:
        return getattr(self.app, "loot", None) is not None

    def session_dir(self) -> str:
        path = getattr(getattr(self.app, "loot", None), "session_path", None)
        return str(path) if path else ""

    def save_wifi(self, ssid, bssid, channel, auth, rssi, observed_at, position):
        """Through LootManager's own writer. Returns the Network object, or None if not saved."""
        from watchdogs.app_state import Network
        net = Network(ssid=ssid, bssid=bssid, channel=str(channel), auth=auth, rssi=str(rssi),
                      band="5" if channel > 14 else "2.4")
        if not self.app.loot.save_wardriving_network(net, observed_at=observed_at, position=position,
                                                     positioned_only=True, append_new=True):
            return None
        return net

    def save_bt(self, mac, rssi, name, observed_at, position) -> bool:
        return bool(self.app.loot.save_wardriving_bt(mac, rssi, name, observed_at=observed_at, position=position,
                                                     positioned_only=True, append_new=True))

    def save_bt_device(self, mac, rssi, name) -> None:
        self.app.loot.save_bt_device(mac, rssi, name, False, False, append_new=True)

    # ------------------------------------------------------------------ XP, lists, markers
    def credit(self, kind: str, key: str, first_xp: int):
        """XP for a find, as every other source gives it. True if the device is new to the game,
        False if it was known, None if the game has no XP hooks (then nothing is shown either)."""
        known = getattr(self.app, "_known_wifi" if kind == "wifi" else "_known_ble", None)
        gain = getattr(self.app, "gain_xp", None)
        if known is None or gain is None:
            return None
        if key in known:
            gain(1)
            return False
        known.add(key)
        gain(first_xp)
        return True

    def list_network(self, net) -> None:
        """Put a Wi-Fi network on the game's SCAN list (once)."""
        state = getattr(self.app, "state", None)
        if state is not None and all(getattr(n, "bssid", None) != net.bssid for n in state.networks):
            state.networks.append(net)

    def marker(self, kind: str, lat: float, lon: float, *args) -> None:
        """A marker on the map. kind: "wifi" (bssid, ssid, channel, rssi) or "bt" (mac, name, rssi)."""
        try:
            import pyxel
            from watchdogs import app as game
            cls_name, list_name = ("WifiNetwork", "wifi_networks") if kind == "wifi" else ("BleDevice", "ble_devices")
            sprite = getattr(game, cls_name)(lat, lon, *args)
            sprite.spawn_frame = pyxel.frame_count
            getattr(self.app, list_name).append(sprite)
        except Exception:
            pass                                    # the find is saved; only the marker is missing

    def message(self, text: str, colour: int = 7) -> None:
        msg = getattr(self.app, "msg", None)
        if callable(msg):
            msg(text, colour)

    def position(self):
        """Where the game thinks the player is: (lat, lon) or None."""
        lat, lon = getattr(self.app, "player_lat", None), getattr(self.app, "player_lon", None)
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon):
            return float(lat), float(lon)
        return None

    def muted(self) -> bool:
        return bool(getattr(self.app, "_muted", False))

    # ------------------------------------------------------------------ the game's own ESP32, Kismet
    def game_port(self):
        """The serial port WDG's own ESP32 link has open (Bristle must never open it too)."""
        return getattr(getattr(self.app, "serial", None), "device", None)

    def kismet(self, me=None):
        for plugin in getattr(self.app, "_plugins", ()) or ():
            if plugin is not me and callable(getattr(plugin, "kismet_active", None)):
                return plugin
        return None

    def kismet_active(self, me=None) -> bool:
        kismet = self.kismet(me)
        try:
            return bool(kismet is not None and kismet.kismet_active())
        except Exception:
            return False

    def queue_wigle(self, me=None):
        """The Kismet plugin's public "put this file in the WiGLE upload queue", or None."""
        queue = getattr(self.kismet(me), "queue_wigle_upload", None)
        return queue if callable(queue) else None

    def wardriving_csv(self) -> str:
        session = self.session_dir()
        return str(Path(session) / "wardriving.csv") if session else ""

    def set_wardriving(self, on: bool) -> None:
        """The game's own "a wardrive is on" flag (it keeps the game from flashing or re-using its
        ESP32 for something else mid-drive)."""
        state = getattr(self.app, "state", None)
        if state is not None and hasattr(state, "wardriving_running"):
            state.wardriving_running = bool(on)
