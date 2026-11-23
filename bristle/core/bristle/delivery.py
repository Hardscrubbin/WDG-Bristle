"""Duplicate-free delivery: per-node sequence tracking and ack points.

The node sends finds with strictly increasing, never-reused sequence numbers. After a reconnect or a
stalled ack it resends from its oldest unacked find, so the core sees repeats. This tracker:
  - accepts each (node, seq) exactly once,
  - computes the ack point: the lowest seq the core still needs (everything below is held),
  - skips gaps the node can't fill (it reports its oldest queued seq `qo` in the heartbeat; anything
    below that is gone, e.g. dropped when flash filled, or skipped after a reboot),
  - says an ack again when the node shows it never got it: an ACK line can be lost like any other
    (a damaged line, a lost frame, a node that restarted just before it arrived), and a node that
    is never told keeps its finds and offers them again for ever.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class NodeDelivery:
    ack_point: int = 0                 # every seq < ack_point is held by the core
    above: set[int] = field(default_factory=set)   # held seqs >= ack_point (out-of-order arrivals)
    received: int = 0
    duplicates: int = 0
    skipped: int = 0                   # numbers the node moved past that we never got: unused numbers
                                       # (every node reboot leaves a gap) as well as finds it dropped.
                                       # Not a loss count: the node's own count (heartbeat qd) is.
    synced: bool = False               # we know where the node's queue starts: it told us, it sent a
                                       # find, or we restored our own ack point
    last_ack_sent: float = 0.0
    last_ack_value: int = -1
    defer: bool = False                # ack a find only after processed(seq): the core has written it
    unprocessed: set[int] = field(default_factory=set)

    def accept(self, seq: int) -> bool:
        """True if this seq is new (store the find), False if it's a repeat (drop it)."""
        if seq < self.ack_point or seq in self.above:
            self.duplicates += 1
            return False
        self.above.add(seq)
        self.received += 1
        self.synced = True
        if self.defer:
            self.unprocessed.add(seq)
        self._advance()
        return True

    def node_oldest(self, oldest: int) -> None:
        """Heartbeat `qo`: the node no longer holds anything below `oldest`."""
        if oldest < self.last_ack_value:
            self.say_again()              # it still holds finds we told it to forget: that ACK was lost
        if oldest > self.ack_point:
            if self.synced:               # the first report only tells us where to start
                self.skipped += (oldest - self.ack_point) - sum(1 for s in self.above if s < oldest)
            self.above = {s for s in self.above if s >= oldest}
            self.ack_point = oldest
            self._advance()
        self.synced = True

    def _advance(self) -> None:
        while self.ack_point in self.above:
            self.above.discard(self.ack_point)
            self.ack_point += 1

    def processed(self, seq: int) -> None:
        """The find with this seq has been dealt with for good (written, or counted as dropped)."""
        self.unprocessed.discard(seq)

    def say_again(self) -> None:
        """The next ack_due() sends the ack value even if it has not changed since the last one."""
        self.last_ack_value = -1

    def ack_value(self) -> int:
        """What the node may forget: everything below this is received AND dealt with."""
        return min(self.ack_point, min(self.unprocessed)) if self.unprocessed else self.ack_point

    def ack_due(self, min_interval_s: float, now: float | None = None) -> int | None:
        """Return the ack value to send now, or None (nothing new, or too soon)."""
        now = time.monotonic() if now is None else now
        value = self.ack_value()
        if value == self.last_ack_value:
            return None
        if now - self.last_ack_sent < min_interval_s:
            return None
        self.last_ack_sent = now
        self.last_ack_value = value
        return value


class DeliveryTracker:
    def __init__(self, defer: bool = False) -> None:
        self.nodes: dict[str, NodeDelivery] = {}
        self.defer = defer             # True: finds are acked only after processed() (WDG does this)

    def node(self, hwid: str) -> NodeDelivery:
        d = self.nodes.get(hwid)
        if d is None:
            d = self.nodes[hwid] = NodeDelivery(defer=self.defer)
        return d

    def snapshot(self) -> dict[str, int]:
        """hwid -> ack value, to keep across a core restart (see restore)."""
        return {hwid: d.ack_value() for hwid, d in self.nodes.items() if d.synced}

    def restore(self, hwid: str, ack_point: int) -> None:
        """After a core restart: resume from the last ack point we persisted for this node."""
        d = self.node(hwid)
        d.synced = True
        d.ack_point = max(d.ack_point, ack_point)
        d.above = {s for s in d.above if s >= d.ack_point}
