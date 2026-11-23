# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""ESP-NOW link: nodes reached through the bridge board.

The bridge is a Bristle board in the bridge role, on USB. It relays: an ESP-NOW frame it hears
comes up as an ERX line, and a frame the core wants delivered goes down as an ETX line. The bridge
keeps that frame in a mailbox and sends it the moment the node next checks in (a scanning node is
on the control channel only for a few milliseconds every visit). The bridge decides nothing.

Everything is checked here, on the core: the frame's tag (owner key), its counter (no replays,
also across restarts: espnow.py), then the lines inside go to the node's NodeSession like lines
from any other link. A frame with a wrong tag is not ours and is only counted.
"""
from __future__ import annotations

import collections
import threading
import time
from typing import Callable

from . import codec, crypto, espnow, proto
from .session import AUTHED, CLOSED, NO_ANSWER, NodeSession, b64url_decode

# Lines that replace an older one of the same kind still waiting for the node's next visit.
LATEST_WINS = ("ACK:", "TIME:", "PING:", "PLAN:", "RUN:", "REPEAT:", "IDENT:", "HI:")
SYNC_EVERY_S = 1.0
GONE_AFTER_S = 3 * proto.LINK_TIMEOUT_MS / 1000


class _Peer:
    def __init__(self, mac: str, endpoint: espnow.Endpoint) -> None:
        self.mac = mac
        self.endpoint = endpoint
        self.session: NodeSession | None = None
        self.out: collections.deque = collections.deque()       # line bodies waiting to be framed
        self.in_mailbox = 0              # frames handed to the bridge since the node last checked in
        self.last_heard = 0.0
        self.last_sync = -1e9
        self.rssi: int | None = None
        self.overflow = 0                # lines dropped because the node stayed away too long


class EspNowHub:
    MAX_WAITING = 64                     # line bodies kept for a node that is not checking in

    def __init__(self, new_session: Callable[[Callable[[str], None], int], NodeSession],
                 drop_session: Callable[[NodeSession], None], owner_key: bytes, counters: espnow.CounterStore,
                 is_blocked: Callable[[str], bool] = lambda mac: False,
                 mono: Callable[[], float] = time.monotonic) -> None:
        self._new_session, self._drop_session = new_session, drop_session
        self.key = owner_key
        self.counters = counters
        self._is_blocked = is_blocked
        self._mono = mono
        self.bridge: NodeSession | None = None
        self.peers: dict[str, _Peer] = {}
        self.stats = collections.Counter()   # frames, strangers, stale, bad, relayed, bridge counters
        self.bridge_stats: dict = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ the bridge
    def attach(self, bridge: NodeSession) -> None:
        with self._lock:
            self.bridge = bridge

    def detach(self, bridge: NodeSession) -> None:
        with self._lock:
            if self.bridge is not bridge:
                return
            self.bridge = None
            for peer in list(self.peers.values()):
                self._close(peer)
            self.peers.clear()

    def _close(self, peer: _Peer) -> None:
        if peer.session is not None:
            self._drop_session(peer.session)
            peer.session = None

    # ------------------------------------------------------------------ in
    def on_erx(self, bridge: NodeSession, fields: dict) -> None:
        """An ERX line from the bridge (called on the bridge's reader thread)."""
        mac = str(fields.get("fr", "")).lower()
        try:
            data = b64url_decode(fields.get("d", ""))
            bytes.fromhex(mac)
        except (ValueError, TypeError):
            self.stats["bad"] += 1
            return
        if len(mac) != 12 or self._is_blocked(mac):
            self.stats["blocked"] += 1
            return
        with self._lock:
            if bridge is not self.bridge:
                return
            self.stats["frames"] += 1
            peer = self.peers.get(mac)
            if peer is None:
                try:
                    crypto.frame_parse(self.key, data)       # is it signed with our key at all?
                except crypto.FrameError:
                    self.stats["strangers"] += 1
                    return
                peer = self.peers[mac] = _Peer(mac, espnow.Endpoint(self.key, self.counters, mac))
            opened = peer.endpoint.open(data)
            now = self._mono()
            if opened.status == espnow.BAD:
                self.stats["bad"] += 1
                return
            if opened.status == espnow.STALE:
                self.stats["stale"] += 1
                if now - peer.last_sync >= SYNC_EVERY_S:
                    peer.last_sync = now
                    self._relay(peer, peer.endpoint.sync_frame())
                return
            peer.last_heard = now
            peer.in_mailbox = 0                               # it just checked in: the mailbox emptied
            try:
                peer.rssi = int(fields["rs"])
            except (KeyError, ValueError):
                pass
            if peer.session is None or peer.session.state in (CLOSED, NO_ANSWER):
                if peer.session is not None:
                    self._drop_session(peer.session)
                peer.session = self._new_session(lambda line, p=peer: self._queue(p, line), proto.LINK_ESPNOW)
                peer.session.hwid = mac
                peer.session.start()
            session = peer.session
            if peer.rssi is not None:
                session.health.rssi = peer.rssi
        for body in opened.bodies:                            # outside the lock: handlers may send
            try:
                message = codec.parse_body(body.decode("ascii"))
                codec.check_required(message)
            except (codec.LineError, UnicodeDecodeError):
                session.dropped_lines += 1
                continue
            session.handle(message)
        self.flush(mac)

    def on_bst(self, fields: dict) -> None:
        self.bridge_stats = dict(fields)

    # ------------------------------------------------------------------ out
    def _queue(self, peer: _Peer, line: str) -> None:
        body = codec.strip_crc(line)
        with self._lock:
            for prefix in LATEST_WINS:
                if body.startswith(prefix):
                    for index, waiting in enumerate(peer.out):
                        if waiting.startswith(prefix):
                            del peer.out[index]
                            break
                    break
            peer.out.append(body)
            while len(peer.out) > self.MAX_WAITING:
                peer.out.popleft()
                peer.overflow += 1

    def _relay(self, peer: _Peer, frame: bytes) -> None:
        bridge = self.bridge
        if bridge is not None and bridge.state == AUTHED:
            bridge.relay(peer.mac, frame)
            peer.in_mailbox += 1
            self.stats["relayed"] += 1

    def flush(self, mac: str | None = None) -> None:
        """Frame what is waiting and hand it to the bridge, as far as the mailbox has room."""
        with self._lock:
            peers = [self.peers[mac]] if mac in self.peers else list(self.peers.values()) if mac is None else []
            for peer in peers:
                room = proto.ESPNOW_MAILBOX_FRAMES - peer.in_mailbox
                while peer.out and room > 0:
                    batch = []
                    size = 0
                    while peer.out and size + len(peer.out[0]) + 1 <= espnow.MAX_PAYLOAD:
                        size += len(peer.out[0]) + 1
                        batch.append(peer.out.popleft().encode("ascii"))
                    if not batch:                             # one long line: it goes alone, in pieces
                        batch.append(peer.out.popleft().encode("ascii"))
                    try:
                        frames = peer.endpoint.seal_lines(batch)
                    except ValueError:
                        peer.overflow += 1
                        continue
                    for frame in frames:
                        self._relay(peer, frame)
                    room -= len(frames)

    def tick(self) -> None:
        """Service thread, a few times a second."""
        now = self._mono()
        with self._lock:
            bridge = self.bridge
            if bridge is None or bridge.state != AUTHED:
                return
            for mac, peer in list(self.peers.items()):
                if now - peer.last_heard > GONE_AFTER_S:
                    self._close(peer)
                    del self.peers[mac]
        self.flush()

    def rows(self) -> list[dict]:
        with self._lock:
            return [{"mac": p.mac, "rssi": p.rssi, "waiting": len(p.out), "age": self._mono() - p.last_heard,
                     "overflow": p.overflow} for p in self.peers.values()]
