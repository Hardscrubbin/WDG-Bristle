# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""Signed ESP-NOW frames: counters that survive restarts, packing, fragments, reassembly.

Reference implementation for the node firmware's Link_EspNow. Pure logic: it never touches a
radio or a file. Whoever owns an Endpoint hands it two things to keep: where the next block of
send counters starts, and the lowest counter it may still accept from the other side.

How a replay is kept out, also across restarts (spec, espnow_frame):
  * a sender takes its counters from reserved blocks and starts a fresh block when it starts, so it
    never signs two frames with one counter;
  * a receiver accepts only counters above the last one it took, and writes down the end of the
    block it is in, so after a restart it accepts nothing it could have seen before;
  * a frame that is too old is answered with a sync frame naming the lowest counter that will do.
    The sender jumps there. A sync frame can only move a counter up, so it needs no counter check.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable

from . import crypto, proto

BLOCK = proto.FRAME_COUNTER_BLOCK
MAX_PAYLOAD = proto.FRAME_MAX_BYTES - proto.FRAME_HEADER_BYTES - proto.FRAME_TAG_BYTES
COUNTER_MAX = 0xFFFFFFFF

OK, POLL, SYNCED, STALE, BAD, PARTIAL = "ok", "poll", "synced", "stale", "bad", "partial"


class CounterStore:
    """What has to outlive a restart, for every peer: {"tx": end of the reserved send block,
    "rx": lowest counter still acceptable}. ``on_change`` is called whenever a value moves; the
    owner saves ``data`` then (the core: a JSON file; the node keeps the same two numbers in NVS)."""

    def __init__(self, data: dict | None = None, on_change: Callable[[], None] = lambda: None) -> None:
        self.data: dict[str, dict[str, int]] = {}
        for peer, values in (data or {}).items():
            if isinstance(values, dict):
                self.data[str(peer)] = {"tx": int(values.get("tx", 0)), "rx": int(values.get("rx", 0))}
        self._on_change = on_change

    def _peer(self, peer: str) -> dict[str, int]:
        return self.data.setdefault(peer, {"tx": 0, "rx": 0})

    def reserve_tx(self, peer: str, at_least: int = 0) -> int:
        """Reserve a fresh block of send counters. Returns its first counter."""
        entry = self._peer(peer)
        start = max(entry["tx"], at_least)
        entry["tx"] = start + BLOCK
        self._on_change()
        return start

    def rx_floor(self, peer: str) -> int:
        return self._peer(peer)["rx"]

    def burn_rx(self, peer: str, floor: int) -> None:
        entry = self._peer(peer)
        if floor > entry["rx"]:
            entry["rx"] = floor
            self._on_change()

    # There is no "forget": counters only ever go up, also for a node that was freed and adopted
    # again. Whatever either end has lost (a wiped node, a rebuilt core) is put right by a sync frame.


class TxCounter:
    def __init__(self, reserve: Callable[[int], int]) -> None:
        self._reserve = reserve          # reserve(at_least) -> first counter of a fresh block
        self._next = 0
        self._end = 0                    # nothing reserved yet: the first frame reserves a block

    def next(self) -> int:
        if self._next >= self._end:
            self._next = self._reserve(self._next)
            self._end = self._next + BLOCK
        value = self._next
        if value > COUNTER_MAX:
            raise OverflowError("frame counter used up: free the node and adopt it again")
        self._next += 1
        return value

    def jump_to(self, floor: int) -> None:
        """The other side will take nothing below ``floor``."""
        if floor > self._next:
            self._next = self._reserve(floor)
            self._end = self._next + BLOCK


class RxGuard:
    def __init__(self, floor: int, burn: Callable[[int], None]) -> None:
        self.floor = floor               # lowest counter that is still acceptable
        self._burn = burn
        self._block = -1

    def accept(self, ctr: int) -> bool:
        if ctr < self.floor:
            return False
        block = ctr // BLOCK
        if block != self._block:
            self._burn((block + 1) * BLOCK)      # after a restart: nothing from this block again
            self._block = block
        self.floor = ctr + 1
        return True


def split_payloads(bodies: list[bytes]) -> list[tuple[int, bytes]]:
    """Line bodies -> [(frag byte, payload)]: whole lines, as many per frame as fit. A line longer
    than one frame travels alone, in pieces. Raises ValueError for a line no 15 pieces can hold."""
    out: list[tuple[int, bytes]] = []
    current = b""
    for body in bodies:
        if not body or b"\n" in body:
            raise ValueError("a line body is empty or holds a newline")
        if len(body) > MAX_PAYLOAD:
            if current:
                out.append((0x01, current))
                current = b""
            count = -(-len(body) // MAX_PAYLOAD)
            if count > proto.FRAME_MAX_FRAGMENTS:
                raise ValueError("line too long to fragment")
            for index in range(count):
                out.append(((index << 4) | count, body[index * MAX_PAYLOAD:(index + 1) * MAX_PAYLOAD]))
            continue
        joined = current + b"\n" + body if current else body
        if len(joined) > MAX_PAYLOAD:
            out.append((0x01, current))
            current = body
        else:
            current = joined
    if current:
        out.append((0x01, current))
    return out


class Reassembler:
    """Puts fragments back together. Pieces must arrive in order with consecutive counters."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []
        self._count = 0
        self._next_ctr = -1
        self.dropped = 0                 # lines lost to a missing or out-of-order piece

    def feed(self, ctr: int, frag: int, payload: bytes) -> list[bytes] | None:
        """Bodies that are complete now ([] is possible); None while a line is still arriving."""
        index, count = frag >> 4, frag & 0x0F
        if count <= 1:
            if self._parts:
                self.dropped += 1
                self._parts = []
            return [part for part in payload.split(b"\n") if part] if index == 0 and count == 1 else []
        if index == 0:
            if self._parts:
                self.dropped += 1
            self._parts, self._count, self._next_ctr = [payload], count, ctr + 1
            return None
        if not self._parts or count != self._count or index != len(self._parts) or ctr != self._next_ctr:
            self.dropped += 1
            self._parts = []
            return []
        self._parts.append(payload)
        self._next_ctr = ctr + 1
        if index < count - 1:
            return None
        body = b"".join(self._parts)
        self._parts = []
        return [body]


@dataclass
class Opened:
    status: str                          # OK | POLL | SYNCED | STALE | BAD | PARTIAL
    bodies: list[bytes] = field(default_factory=list)


class Endpoint:
    """One end of one core<->node pair: signs what goes out, checks what comes in."""

    def __init__(self, key: bytes, store: CounterStore, peer: str) -> None:
        self.key = key
        self.peer = peer
        self._store = store
        self.tx = TxCounter(lambda at_least: store.reserve_tx(peer, at_least))
        self.rx = RxGuard(store.rx_floor(peer), lambda floor: store.burn_rx(peer, floor))
        self.reassembler = Reassembler()
        self.bad = 0
        self.stale = 0

    # ------------------------------------------------------------------ out
    def seal(self, kind: int, payload: bytes = b"", frag: int = 0x01) -> bytes:
        return crypto.frame_build(self.key, kind, self.tx.next(), frag, payload)

    def seal_lines(self, bodies: list[bytes]) -> list[bytes]:
        return [self.seal(proto.FRAME_KIND_LINES, payload, frag) for frag, payload in split_payloads(bodies)]

    def poll_frame(self) -> bytes:
        return self.seal(proto.FRAME_KIND_POLL)

    def sync_frame(self) -> bytes:
        """Tells the other side the lowest counter this end will still take."""
        return self.seal(proto.FRAME_KIND_SYNC, struct.pack("<I", min(self.rx.floor, COUNTER_MAX)))

    # ------------------------------------------------------------------ in
    def open(self, data: bytes) -> Opened:
        try:
            frame = crypto.frame_parse(self.key, data)
        except crypto.FrameError:
            self.bad += 1
            return Opened(BAD)
        if frame.kind == proto.FRAME_KIND_SYNC:
            if len(frame.payload) != 4:
                self.bad += 1
                return Opened(BAD)
            self.tx.jump_to(struct.unpack("<I", frame.payload)[0])
            return Opened(SYNCED)
        if not self.rx.accept(frame.ctr):
            self.stale += 1
            return Opened(STALE)             # the caller answers with sync_frame(), not too often
        if frame.kind == proto.FRAME_KIND_POLL:
            return Opened(POLL)
        if frame.kind != proto.FRAME_KIND_LINES:
            self.bad += 1
            return Opened(BAD)
        bodies = self.reassembler.feed(frame.ctr, frame.frag, frame.payload)
        return Opened(PARTIAL) if bodies is None else Opened(OK, bodies)
