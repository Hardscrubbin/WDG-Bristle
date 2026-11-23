"""The fleet: every node session, run state and channel planning in one place.

WDG's plugin owns one Fleet. Transports (USB, ESP-NOW bridge, BLE) create NodeSessions through
`fleet.new_session(...)`. The Fleet decides each node's desired state; sessions push it to the nodes.
"""
from __future__ import annotations

import threading
from typing import Callable

from . import plan as planmod
from . import proto
from .delivery import DeliveryTracker
from .registry import Registry
from .session import AUTHED, BUSY, Find, NodeSession


class Fleet:
    def __init__(self, registry: Registry, owner_key: bytes, region: str = "U",
                 on_find: Callable[[Find], None] = lambda f: None,
                 on_event: Callable[[str, NodeSession, dict], None] = lambda k, s, i: None,
                 defer_acks: bool = False, core_id: str = "") -> None:
        if len(owner_key) != proto.AUTH_KEY_BYTES:
            raise ValueError("owner key must be 16 bytes")
        self.registry = registry
        self.owner_key = owner_key
        self.region = region
        self.delivery = DeliveryTracker(defer=defer_acks)
        self.sessions: list[NodeSession] = []
        self.run_mask = 0
        self.session_id = 0
        self.kismet_heavy: set[int] = set()
        self.core_id = core_id
        self.repeat_ms = (proto.REPEAT_AP_MS, proto.REPEAT_BLE_MS)
        self.control_channel: int | None = None
        self.ble_active = False              # True: nodes also ask for scan responses (the game's default)
        self._on_find = on_find
        self._on_event = on_event
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ sessions
    def new_session(self, send: Callable[[str], None], link_type: int) -> NodeSession:
        s = NodeSession(send=send, link_type=link_type, registry=self.registry, delivery=self.delivery,
                        owner_key=self.owner_key, on_find=self._on_find, on_event=self._event,
                        core_id=self.core_id)
        s.desired.repeat_ap_ms, s.desired.repeat_ble_ms = self.repeat_ms
        s.desired.control_channel = self.control_channel
        with self._lock:
            self.sessions.append(s)
        return s

    def drop_session(self, s: NodeSession) -> None:
        s.close()
        with self._lock:
            if s in self.sessions:
                self.sessions.remove(s)
        self.replan()

    def _event(self, kind: str, s: NodeSession, info: dict) -> None:
        if kind == "authed":
            self.replan()
        self._on_event(kind, s, info)

    def bridges(self) -> list[NodeSession]:
        """Boards in the bridge role that are online (they relay; they are not scanning nodes)."""
        return [s for s in list(self.sessions) if s.state == AUTHED and s.is_bridge]

    def busy(self) -> list[NodeSession]:
        """Nodes of ours that another core is running right now."""
        return [s for s in list(self.sessions) if s.state == BUSY]

    def authed(self) -> list[NodeSession]:
        """One session per scanning node (USB wins over wireless when a node has both)."""
        best: dict[str, NodeSession] = {}
        for s in list(self.sessions):
            if s.state != AUTHED or s.is_bridge:
                continue
            cur = best.get(s.hwid)
            if cur is None or (s.link_type == proto.LINK_USB and cur.link_type != proto.LINK_USB):
                best[s.hwid] = s
        return list(best.values())

    # ------------------------------------------------------------------ run control
    def start(self, mask: int = proto.RUN_WIFI | proto.RUN_BLE) -> int:
        with self._lock:
            self.session_id += 1
            self.run_mask = mask
            self._push()
            return self.session_id

    def stop(self) -> None:
        with self._lock:
            self.run_mask = 0
            self._push()

    def set_kismet_channels(self, heavy: set[int]) -> None:
        if set(heavy) != self.kismet_heavy:
            self.kismet_heavy = set(heavy)
            self.replan()

    def set_repeat(self, ap_ms: int, ble_ms: int) -> None:
        """How long a node waits before it reports the same device again (sent to every node)."""
        with self._lock:
            self.repeat_ms = (int(ap_ms), int(ble_ms))
            for s in self.sessions:
                s.desired.repeat_ap_ms, s.desired.repeat_ble_ms = self.repeat_ms
            self._push()

    def set_control_channel(self, channel: int | None) -> None:
        with self._lock:
            self.control_channel = channel
            for s in self.sessions:
                s.desired.control_channel = channel
            for s in self.bridges():
                s.sync()
            self._push()

    def watch(self, mac: str, seconds: int) -> int:
        """Hunt mode: every online node listens for this MAC. Returns how many were told."""
        nodes = self.authed()
        for s in nodes:
            s.watch(mac, seconds)
        return len(nodes)

    def replan(self) -> None:
        with self._lock:
            nodes = []
            for s in self.authed():
                rec = self.registry.known(s.hwid)
                if rec is None:
                    continue
                nodes.append(planmod.NodeCaps(number=rec.number, cap_5g=rec.cap_5g,
                                              cap_ble=rec.cap_ble, pin=rec.pin))
            plans = planmod.split(nodes, self.region, kismet_heavy=self.kismet_heavy)
            if self.ble_active:
                for plan in plans.values():
                    if plan.flags & proto.PLAN_FLAG_BLE:
                        plan.flags |= proto.PLAN_FLAG_BLE_ACTIVE
            for s in self.authed():
                rec = self.registry.known(s.hwid)
                if rec is not None:
                    s.desired.number = rec.number
                    s.desired.plan = plans.get(rec.number)
            self._push()

    def _push(self) -> None:
        for s in self.authed():
            s.desired.run_mask = self.run_mask
            s.desired.session = self.session_id
            s.sync()          # start/stop reaches the node now, not at its next heartbeat

    def tick(self) -> None:
        with self._lock:
            sessions = list(self.sessions)
        for s in sessions:
            s.tick()

    def resync_time(self) -> None:
        """Send the clock to every node now (the core's own clock was just corrected)."""
        for s in self.authed():
            s._send_time()

    def session_for(self, hwid: str) -> NodeSession | None:
        return next((s for s in self.authed() if s.hwid == hwid), None)

    # ------------------------------------------------------------------ totals for the screen/HUD
    def totals(self) -> dict:
        authed = self.authed()
        return {
            "known": sum(1 for r in list(self.registry.nodes.values()) if not r.role),
            "bridges": len(self.bridges()),
            "busy": len(self.busy()),
            "online": len(authed),
            "backlog": sum(s.health.backlog for s in authed),
            "dropped": sum(s.health.dropped for s in authed),
            "received": sum(d.received for d in self.delivery.nodes.values()),
            "duplicates": sum(d.duplicates for d in self.delivery.nodes.values()),
            "running": bool(self.run_mask),
            "session": self.session_id,
        }
