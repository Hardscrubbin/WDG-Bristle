"""One node link: handshake, owner checks, Node Memory/Block enforcement, keeping the node in step.

Transport-agnostic: give it a `send(line_text)` function and feed it received lines. USB, the ESP-NOW
bridge and BLE all drive the same NodeSession.

States:
  HELLO_WAIT  sent HI, waiting for HELLO
  NO_ANSWER   nothing answered HI after a few tries: not a Bristle node (or not running yet).
              Nothing more is sent. The link layer closes the port.
  NEW         unowned Bristle node; can be adopted (USB only). Never adopted automatically.
  NOT_OURS    owned by a different key (or failed proof). Ignored.
  FORGOTTEN   owned by OUR key but not in Node Memory. Offered for re-adoption; never automatic.
  BLOCKED     on the Block list. Ignored completely.
  AUTHED      ours, in Node Memory, handshake complete. Commands and finds flow.
  CLOSED      link gone.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from . import codec, crypto, proto
from .delivery import DeliveryTracker
from .plan import Plan
from .registry import Registry

HELLO_WAIT, NO_ANSWER, NEW, NOT_OURS, FORGOTTEN, BLOCKED, AUTHED, CLOSED = (
    "HELLO_WAIT", "NO_ANSWER", "NEW", "NOT_OURS", "FORGOTTEN", "BLOCKED", "AUTHED", "CLOSED")


@dataclass
class Find:
    """A new (not duplicate) find, ready for GPS placement and the loot writers."""
    hwid: str
    node: int
    kind: str                 # "AP" or "BT"
    seq: int
    t: float                  # unix seconds (with ms)
    session: int
    fields: dict[str, str]    # decoded line fields (b/c/r/k/e or m/r/at/nm/mf)


@dataclass
class Health:
    flags: int = 0
    plan_hash: str = ""
    run_mask: int = 0
    session: int = 0
    link: int = 0
    backlog: int = 0
    dropped: int = 0
    capacity: int = 0
    fill_pct: int = 0
    next_seq: int = 0
    oldest_seq: int = 0
    build: int = 0
    temp_c: int | None = None
    uptime_s: int | None = None
    ram_kb: int | None = None
    rssi: int | None = None
    at: float = 0.0


@dataclass
class Desired:
    """What the core wants this node doing. Filled in by the Fleet."""
    number: int | None = None
    plan: Plan | None = None
    run_mask: int = 0
    session: int = 0
    repeat_ap_ms: int = proto.REPEAT_AP_MS
    repeat_ble_ms: int = proto.REPEAT_BLE_MS


@dataclass
class NodeSession:
    send: Callable[[str], None]
    link_type: int
    registry: Registry
    delivery: DeliveryTracker
    owner_key: bytes
    on_find: Callable[[Find], None] = lambda f: None
    on_event: Callable[[str, "NodeSession", dict], None] = lambda kind, s, info: None
    clock: Callable[[], float] = time.time
    mono: Callable[[], float] = time.monotonic

    state: str = CLOSED
    hwid: str = ""
    board: str = ""
    build: int = 0
    caps: int = 0
    number: int | None = None
    health: Health = field(default_factory=Health)
    desired: Desired = field(default_factory=Desired)
    dropped_lines: int = 0
    usb_key: str = ""         # which USB device this is (set by the USB link; kept in Node Memory)
    _core_nonce: bytes = b""
    _hello_at: float = 0.0
    _hello_tries: int = 0
    _last_hb: float = 0.0
    _last_tx: float = 0.0
    _last_time_sent: float = 0.0
    _last_fix: dict[str, float] = field(default_factory=dict)

    # ------------------------------------------------------------------ outgoing
    def _cmd(self, msg_type: str, fields: dict | None = None) -> None:
        self._last_tx = self.mono()
        self.send(codec.build(msg_type, fields))

    def start(self) -> None:
        """Begin (or restart) the handshake."""
        self._hello_tries = 0
        self._say_hi()

    def _say_hi(self) -> None:
        self._core_nonce = crypto.new_nonce()
        self.state = HELLO_WAIT
        self._hello_at = self.mono()
        self._hello_tries += 1
        self._cmd("HI", {"v": proto.PROTOCOL_VERSION, "cn": self._core_nonce.hex()})

    def adopt(self, number: int | None = None) -> int:
        """Adopt this NEW (or FORGOTTEN) node. Only over USB. Returns the node number."""
        if self.link_type != proto.LINK_USB:
            raise PermissionError(proto.ERRORS["NOT_USB"])
        if self.state not in (NEW, FORGOTTEN):
            raise RuntimeError(f"can't adopt a node in state {self.state}")
        rec = self.registry.adopt(self.hwid, board=self.board, build=self.build,
                                  cap_5g=bool(self.caps & proto.HB_CAP_5G),
                                  cap_ble=bool(self.caps & proto.HB_CAP_BLE), number=number)
        if self.usb_key:
            self.registry.touch(self.hwid, usb_key=self.usb_key)
            self.registry.save()
        if self.state == NEW:
            self._cmd("ADOPT", {"k": self.owner_key.hex(), "n": rec.number})
        else:
            self.start()      # FORGOTTEN already holds our key: just redo the handshake
        return rec.number

    def block(self, note: str = "") -> None:
        if self.hwid:
            self.registry.block(self.hwid, note)
        self.state = BLOCKED
        self.on_event("blocked", self, {})

    # ------------------------------------------------------------------ incoming
    def feed_line(self, raw: bytes | str) -> None:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if text.startswith("#"):                      # the node's human console output
            self.on_event("console", self, {"text": text[1:].strip()})
            return
        try:
            msg = codec.parse(raw)
            codec.check_required(msg)
        except codec.LineError:
            self.dropped_lines += 1
            return
        self.handle(msg)

    def handle(self, msg: codec.Message) -> None:
        """Handle a parsed message (ESP-NOW payloads arrive here already authenticated by HMAC)."""
        if self.state in (BLOCKED, CLOSED):
            return
        t = msg.type
        if t == "HELLO":
            self._on_hello(msg)
        elif self.state in (NOT_OURS,):
            return
        elif t == "RSP":
            self._on_rsp(msg)
        elif t == "TREQ":
            self._send_time()
        elif self.state != AUTHED:
            return                                    # nothing else counts before the handshake
        elif t == "HB":
            self._on_hb(msg)
        elif t in ("AP", "BT"):
            self._on_find(msg)
        elif t == "OTAS":
            self.on_event("ota", self, dict(msg.fields))

    def _on_hello(self, msg: codec.Message) -> None:
        try:
            self.hwid = msg.get("hw", "").lower()
            hw = bytes.fromhex(self.hwid)
            if len(hw) != 6:
                raise ValueError
        except ValueError:
            self.dropped_lines += 1
            return
        self.board = msg.get("bd", "")
        self.build = msg.int("b", 0) or 0
        self.caps = msg.int("cap", 0) or 0
        if self.registry.is_blocked(self.hwid):
            self.state = BLOCKED
            self.on_event("blocked_seen", self, {})
            return
        np = msg.get("np")
        if not np:
            self.state = NEW
            ev = "new_node" if self.link_type == proto.LINK_USB else "new_node_wireless"
            self.on_event(ev, self, {"board": self.board, "build": self.build})
            return
        want = crypto.node_proof(self.owner_key, self._core_nonce, hw)
        if not crypto.proofs_equal(np, want):
            self.state = NOT_OURS
            self.on_event("not_ours", self, {"oid": msg.get("oid", "")})
            return
        rec = self.registry.known(self.hwid)
        if rec is None:
            self.state = FORGOTTEN
            self.on_event("forgotten", self, {})
            return
        try:
            nn = bytes.fromhex(msg.get("nn", ""))
        except ValueError:
            self.dropped_lines += 1
            return
        self._cmd("AUTH", {"cp": crypto.core_proof(self.owner_key, nn, hw)})
        self.state = AUTHED
        self.number = rec.number
        self._last_hb = self.mono()      # the silence timer starts now, not at the first heartbeat
        facts = dict(board=self.board, build=self.build, cap_5g=bool(self.caps & proto.HB_CAP_5G),
                     cap_ble=bool(self.caps & proto.HB_CAP_BLE))
        if self.usb_key and rec.usb_key != self.usb_key:
            facts["usb_key"] = self.usb_key
            self.registry.touch(self.hwid, **facts)
            self.registry.save()
        else:
            self.registry.touch(self.hwid, **facts)
        self._send_time()
        self.on_event("authed", self, {"number": rec.number})

    def _on_rsp(self, msg: codec.Message) -> None:
        c, ok = msg.get("c"), msg.get("ok") == "1"
        if c == "ADOPT" and ok:
            self.start()                              # node now holds the key: authenticate
        elif not ok and self.state == AUTHED and msg.get("e") == proto.ERRORS["NOT_AUTHED"]:
            # The node restarted (or timed the link out) and no longer knows us: shake hands again.
            self.on_event("reauth", self, {})
            self.start()
        self.on_event("rsp", self, {"c": c, "ok": ok, "e": msg.get("e")})

    def _on_hb(self, msg: codec.Message) -> None:
        h = self.health
        g = msg.int
        h.flags, h.plan_hash, h.run_mask = g("f", 0), msg.get("ph", ""), g("m", 0)
        h.session, h.link, h.backlog, h.dropped = g("s", 0), g("lk", 0), g("qb", 0), g("qd", 0)
        h.capacity, h.fill_pct, h.next_seq, h.oldest_seq = g("qc", 0), g("qf", 0), g("qn", 0), g("qo", 0)
        h.build, h.temp_c, h.uptime_s, h.ram_kb, h.rssi = g("b", 0), g("t"), g("up"), g("ram"), g("rs")
        h.at = self.mono()
        self._last_hb = h.at
        self.delivery.node(self.hwid).node_oldest(h.oldest_seq)
        self.registry.touch(self.hwid, build=h.build)
        self._reconcile()

    def _on_find(self, msg: codec.Message) -> None:
        seq = msg.int("q")
        if seq is None or not self.delivery.node(self.hwid).accept(seq):
            return
        t = float(msg.int("t", 0) or 0) + (msg.int("ms", 0) or 0) / 1000.0
        if t <= 0:
            return
        self.on_find(Find(self.hwid, self.number or 0, msg.type, seq, t, msg.int("s", 0) or 0,
                          dict(msg.fields)))

    # ------------------------------------------------------------------ keeping in step
    def _send_time(self) -> None:
        now = self.clock()
        self._last_time_sent = self.mono()
        self._cmd("TIME", {"u": int(now), "ms": int((now % 1) * 1000)})

    def _rate_ok(self, what: str, interval_s: float = 2.0) -> bool:
        now = self.mono()
        if now - self._last_fix.get(what, -1e9) < interval_s:
            return False
        self._last_fix[what] = now
        return True

    def _reconcile(self) -> None:
        """Send whatever command brings the node to the desired state (rate-limited per command)."""
        h, d = self.health, self.desired
        if not (h.flags & proto.HB_CLOCK) and self._rate_ok("time"):
            self._send_time()
        if d.number is not None and self.number != d.number and self._rate_ok("num"):
            self._cmd("NUM", {"n": d.number})
            self.number = d.number
        if d.plan is not None and h.plan_hash != d.plan.hash() and self._rate_ok("plan"):
            self._cmd("PLAN", d.plan.fields())
        if (h.run_mask != d.run_mask or (d.run_mask and h.session != d.session)) and self._rate_ok("run"):
            self._cmd("RUN", {"m": d.run_mask, "s": d.session})

    def sync(self) -> None:
        """Push the desired state to the node now instead of waiting for its next heartbeat."""
        if self.state == AUTHED:
            self._reconcile()

    def tick(self) -> None:
        """Call often (e.g. every 100 ms): sends acks, keeps the link alive, enforces timeouts."""
        now = self.mono()
        if self.state == HELLO_WAIT and now - self._hello_at > proto.USB_HELLO_WAIT_MS / 1000:
            if self._hello_tries < proto.USB_HELLO_TRIES:
                self._say_hi()
            else:
                # Not a Bristle node. Say nothing more to it.
                self.state = NO_ANSWER
                self.on_event("no_answer", self, {"tries": self._hello_tries})
            return
        if self.state != AUTHED:
            return
        ack = self.delivery.node(self.hwid).ack_due(proto.ACK_MIN_INTERVAL_MS / 1000, now)
        if ack is not None:
            self._cmd("ACK", {"q": ack})
        if now - self._last_hb > 3 * proto.HEARTBEAT_MS / 1000:
            # No heartbeat for three periods: the node restarted, or the cable is half out.
            self.on_event("silent", self, {"seconds": round(now - self._last_hb)})
            self.start()
            return
        if now - self._last_time_sent > proto.TIME_RESYNC_MS / 1000:
            self._send_time()            # node clocks drift: keep them close to ours
        if now - self._last_tx > proto.CORE_PING_MS / 1000:
            self._cmd("PING")            # a node that hears nothing for LINK_TIMEOUT_MS closes the link

    # ------------------------------------------------------------------ direct commands
    def identify(self, seconds: int = 10) -> None:
        self._cmd("IDENT", {"d": seconds})

    def forget_on_node(self) -> None:
        """Free the node (owner, number, plan, queue cleared on the node) and drop it from memory."""
        if self.state == AUTHED:
            self._cmd("FORGET")
        if self.hwid:
            self.registry.forget(self.hwid)
        self.state = NEW

    def close(self) -> None:
        self.state = CLOSED
