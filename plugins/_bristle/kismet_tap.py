# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""[bristle] What Bristle takes from Kismet, without changing what the Kismet plugin does.

The Kismet plugin calls two functions here, both on its own polling thread:
  * bt_device(d, mac, name, rssi)  for every Bluetooth device in a poll (d is Kismet's own record);
  * poll(engine, base_url)         after each poll: Kismet's alerts, and now and then its sources.
Everything lands in plain queues that Bristle's worker thread empties. If Bristle is not running
nothing reads them and they stay small (bounded). Nothing here talks to a device: it only reads
what Kismet already has.

Class of Device: current Kismet reports it for Bluetooth Classic devices as
``bluetooth.device.class`` (it takes it from the inquiry result). An older Kismet does not; then
``cod`` stays None, the skimmer score works without that part, and ``cod_seen`` stays False so the
suspects screen can say so.
"""
from __future__ import annotations

import collections
import threading
import time

devices = collections.deque(maxlen=4000)     # dicts: mac, name, rssi, t, classic, cod, heard_by
alerts = collections.deque(maxlen=200)       # dicts: header, text, t, mac, channel
sources = {"t": 0.0, "list": [], "error": ""}
state = {"cod_seen": False, "alerts_error": "", "alerts_since": 0.0, "bt_seen": 0}
enabled = threading.Event()                  # set by Bristle's service when it wants the feed
SOURCES_EVERY_S = 20.0
ALERTS_EVERY_S = 4.0
_last_alert_poll = 0.0
_source_names: dict = {}
_kismet = {"engine": None, "url": ""}        # kept from the last poll, for borrow()


def _norm(mac) -> str:
    return "".join(c for c in str(mac).lower() if c in "0123456789abcdef")


def bt_device(d: dict, mac, name, rssi) -> None:
    """One Bluetooth device from a Kismet poll. Never raises."""
    if not enabled.is_set():
        return
    try:
        bt = d.get("bluetooth.device")
        cod = None
        if isinstance(bt, dict):
            value = bt.get("bluetooth.device.class")
            if isinstance(value, (int, float)) and value >= 0:
                cod = int(value) & 0xFFFFFF
                state["cod_seen"] = True
        kind = str(d.get("kismet.device.base.type") or "")
        classic = True if "BR/EDR" in kind.upper() else False if "BTLE" in kind.upper() else None
        heard_by = "Kismet"
        seen = d.get("kismet.device.base.seenby")
        if isinstance(seen, list) and seen and isinstance(seen[0], dict):
            uuid = seen[0].get("kismet.common.seenby.uuid")
            if uuid in _source_names:
                heard_by = "Kismet " + _source_names[uuid]
        when = d.get("kismet.device.base.last_time")
        clean = _norm(mac)
        if len(clean) != 12:
            return
        devices.append({"mac": clean, "name": "" if name == mac else str(name or ""),
                        "rssi": int(rssi) if isinstance(rssi, (int, float)) and rssi > -100 else None,
                        "t": float(when) if isinstance(when, (int, float)) and when > 0 else time.time(),
                        "classic": classic, "cod": cod, "heard_by": heard_by})
        state["bt_seen"] += 1
    except Exception:
        pass


def poll(engine, base_url: str) -> None:
    """After a device poll, on the Kismet plugin's polling thread. Never raises."""
    global _last_alert_poll
    if not enabled.is_set():
        return
    now = time.monotonic()
    _kismet.update(engine=engine, url=base_url)
    try:
        if now - sources["t"] >= SOURCES_EVERY_S:
            found, error = engine.datasources()
            sources.update(t=now, list=found if not error else [], error=error or "")
            for source in found or []:
                if source.get("uuid"):
                    _source_names[source["uuid"]] = str(source.get("name") or source.get("interface") or "")
        if now - _last_alert_poll < ALERTS_EVERY_S:
            return
        _last_alert_poll = now
        since = int(state["alerts_since"]) or int(time.time()) - 5
        response = engine._get("%s/alerts/last-time/%d/alerts.json" % (base_url, since), auth=engine._auth(), timeout=5)
        if response.status_code != 200:
            state["alerts_error"] = "HTTP %d" % response.status_code
            return
        state["alerts_error"] = ""
        newest = state["alerts_since"]
        for item in response.json() or []:
            if not isinstance(item, dict):
                continue
            stamp = item.get("kismet.alert.timestamp")
            stamp = float(stamp) if isinstance(stamp, (int, float)) else time.time()
            if stamp <= state["alerts_since"]:
                continue
            newest = max(newest, stamp)
            alerts.append({"header": str(item.get("kismet.alert.header") or "").upper(),
                           "text": str(item.get("kismet.alert.text") or ""), "t": stamp,
                           "mac": _norm(item.get("kismet.alert.transmitter_mac") or item.get("kismet.alert.source_mac") or ""),
                           "channel": str(item.get("kismet.alert.channel") or "")})
        state["alerts_since"] = max(newest, float(since))
    except Exception as exc:
        state["alerts_error"] = str(exc)[:120] or type(exc).__name__


def bluetooth_sources() -> list:
    """Kismet's Bluetooth datasources that are running: [{"uuid", "name", "interface"}]."""
    return [dict(s) for s in sources["list"] if s.get("kind") == "bt" and s.get("running", True)]


def borrow(uuid: str, back: bool) -> tuple:
    """Hunt mode: close one of Kismet's Bluetooth sources (back=False) or open it again (back=True).
    (ok, reason). Runs on Bristle's worker thread; needs a Kismet login that may change sources."""
    command = "open_source" if back else "close_source"
    engine, base_url = _kismet["engine"], _kismet["url"]
    if engine is None or not getattr(engine, "running", False):
        return False, "Kismet is not running"
    try:
        response = engine._post("%s/datasource/by-uuid/%s/%s.cmd" % (base_url, uuid, command),
                                auth=engine._auth(), timeout=8)
    except Exception as exc:
        return False, str(exc)[:120] or type(exc).__name__
    if response.status_code == 200:
        return True, ""
    if response.status_code in (401, 403):
        return False, "this Kismet login may not change datasources"
    return False, "HTTP %d" % response.status_code
