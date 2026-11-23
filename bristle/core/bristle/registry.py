"""Node Memory, the Block list and the "not a node" list.

Node Memory remembers every adopted node by its hardware id (6-byte base MAC, 12 hex) and keeps its
node number for good. The Block list holds hardware ids (or BLE MACs) the core must ignore. The
"not a node" list holds USB devices the core must never even say hello to (another ESP32 running
something else, for example). Saved as one JSON file, written atomically so a crash or power loss
can't corrupt it.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class NodeRecord:
    hwid: str                     # 12 lower-case hex
    number: int
    name: str = ""                # friendly label I can set
    board: str = ""               # last reported board
    build: int = 0                # last reported firmware build
    cap_5g: bool = False
    cap_ble: bool = True
    pin: str | None = None        # manual band pin: None, "24", "5", "5lo", "5hi", "ble"
    usb_key: str = ""             # the USB device it was last seen on (link_usb.usb_key)
    role: int = 0                 # 0 node, 1 ESP-NOW bridge (last reported)
    adopted_at: float = field(default_factory=time.time)
    last_seen: float = 0.0


def _norm(hwid: str) -> str:
    h = hwid.replace(":", "").replace("-", "").lower()
    if len(h) != 12 or any(c not in "0123456789abcdef" for c in h):
        raise ValueError(f"bad hardware id {hwid!r}")
    return h


class Registry:
    VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.nodes: dict[str, NodeRecord] = {}
        self.blocked: dict[str, str] = {}      # hwid/MAC -> note
        self.not_nodes: dict[str, str] = {}    # USB key -> note
        self.load()

    # ------------------------------------------------------------------ persistence
    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.nodes = {}
            for rec in data.get("nodes", []):
                r = NodeRecord(**{k: v for k, v in rec.items() if k in NodeRecord.__dataclass_fields__})
                self.nodes[r.hwid] = r
            self.blocked = dict(data.get("blocked", {}))
            self.not_nodes = dict(data.get("not_nodes", {}))

    def save(self) -> None:
        with self._lock:
            data = {"version": self.VERSION,
                    "nodes": [asdict(r) for r in sorted(self.nodes.values(), key=lambda r: r.number)],
                    "blocked": self.blocked,
                    "not_nodes": self.not_nodes}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".bristle-", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    # ------------------------------------------------------------------ queries
    def is_blocked(self, hwid_or_mac: str) -> bool:
        try:
            return _norm(hwid_or_mac) in self.blocked
        except ValueError:
            return False

    def known(self, hwid: str) -> NodeRecord | None:
        return self.nodes.get(_norm(hwid))

    def is_not_node(self, usb_key: str) -> bool:
        return usb_key in self.not_nodes

    def knows_usb(self, usb_key: str, hwid_guess: str | None = None) -> bool:
        """True if this USB device belongs to a node in Node Memory.

        hwid_guess: boards with native USB report their MAC as the USB serial number, which is the
        node's hardware id, so such a node is recognised even on a port it was never seen on.
        """
        if hwid_guess and hwid_guess in self.nodes:
            return True
        return bool(usb_key) and any(r.usb_key == usb_key for r in self.nodes.values())

    def by_number(self, number: int) -> NodeRecord | None:
        return next((r for r in self.nodes.values() if r.number == number), None)

    def next_number(self) -> int:
        used = {r.number for r in self.nodes.values()}
        n = 1
        while n in used:
            n += 1
        return n

    # ------------------------------------------------------------------ changes
    def adopt(self, hwid: str, board: str = "", build: int = 0, cap_5g: bool = False,
              cap_ble: bool = True, number: int | None = None) -> NodeRecord:
        h = _norm(hwid)
        with self._lock:
            if h in self.blocked:
                raise PermissionError("blocked device: unblock it first")
            if h in self.nodes:
                return self.nodes[h]
            n = number if number is not None else self.next_number()
            if self.by_number(n) is not None:
                raise ValueError(f"node number {n} is taken")
            rec = NodeRecord(hwid=h, number=n, board=board, build=build, cap_5g=cap_5g, cap_ble=cap_ble)
            self.nodes[h] = rec
            self.save()
            return rec

    def forget(self, hwid: str) -> None:
        with self._lock:
            self.nodes.pop(_norm(hwid), None)
            self.save()

    def block(self, hwid_or_mac: str, note: str = "") -> None:
        h = _norm(hwid_or_mac)
        with self._lock:
            self.nodes.pop(h, None)          # a blocked device can't stay in Node Memory
            self.blocked[h] = note
            self.save()

    def unblock(self, hwid_or_mac: str) -> None:
        with self._lock:
            self.blocked.pop(_norm(hwid_or_mac), None)
            self.save()

    def mark_not_node(self, usb_key: str, note: str = "") -> None:
        """Never say hello to this USB device again (until it is unmarked)."""
        if not usb_key:
            raise ValueError("no USB key")
        with self._lock:
            if any(r.usb_key == usb_key for r in self.nodes.values()):
                raise PermissionError("that device is one of your nodes: forget the node first")
            self.not_nodes[usb_key] = note
            self.save()

    def unmark_not_node(self, usb_key: str) -> None:
        with self._lock:
            self.not_nodes.pop(usb_key, None)
            self.save()

    def renumber(self) -> dict[str, int]:
        """Renumber 1..N in the order of current numbers (closes gaps). Returns hwid -> new number."""
        with self._lock:
            mapping = {}
            for i, r in enumerate(sorted(self.nodes.values(), key=lambda r: r.number), start=1):
                r.number = i
                mapping[r.hwid] = i
            self.save()
            return mapping

    def set_pin(self, hwid: str, pin: str | None) -> None:
        if pin not in (None, "24", "5", "5lo", "5hi", "ble"):
            raise ValueError(f"bad pin {pin}")
        with self._lock:
            self.nodes[_norm(hwid)].pin = pin
            self.save()

    def touch(self, hwid: str, **updates) -> None:
        """Update last_seen and any reported facts (board, build, caps). Saved lazily by the caller."""
        with self._lock:
            r = self.nodes.get(_norm(hwid))
            if r is None:
                return
            r.last_seen = time.time()
            for k, v in updates.items():
                if hasattr(r, k):
                    setattr(r, k, v)
