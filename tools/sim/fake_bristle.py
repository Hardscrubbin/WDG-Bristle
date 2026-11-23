# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""[bristle] STAND-INS, NOT HARDWARE: a scripted Bristle node, a scripted bridge, a cable and the air.

Nothing in this file is a board, a radio or a USB port, and nothing that passes here says anything
about real hardware. It exists so the game's side of Bristle (plugins/_bristle: the service, the
fleet, the sessions, the ESP-NOW hub, the update sender) can be driven through whole conversations
on a PC, with faults put in on purpose.

ScriptedNode speaks the real protocol: the same lines, check sums, proofs and signed frames a node
sends. It was written from bristle/docs/PROTOCOL.md and from reading the node firmware
(Node.cpp, Owner.cpp, Queue.cpp, Clock.cpp, Link_EspNow.cpp, Bridge.cpp, Ota.cpp), not from the
core's code, so that a place where the two disagree shows up as a failed scenario. From the core
library it takes only what the spec's test vectors pin: building and reading a line (codec),
signing and opening a frame (crypto), and the protocol's numbers and names (proto, generated from
the spec). Everything else (what a node answers, when it believes a core, what it keeps across a
restart) is written out here.

It models firmware build 5. A board made with build=4 keeps the one thing build 5 fixed (see
STALE_MOVES_BRIDGE_UNTIL), so a scenario can show what a node that was not updated does.

What it does not model: radio timing, channel dwells, flash wear, RAM, the real size of anything,
Bluetooth's own transport (its lines travel over a plain stand-in connection). A "visit" to the
control channel is one function call. Time is a number the scenario moves. The author of the
stand-in is the author of the code it tests: a misreading of the firmware would be in both.
"""
import base64
import collections
import hashlib
import hmac
import os
import struct

from plugins._bristle import codec, crypto, proto

LINE_MAX = proto.LINE_MAX_BYTES
PAYLOAD_MAX = proto.FRAME_MAX_BYTES - proto.FRAME_HEADER_BYTES - proto.FRAME_TAG_BYTES
ESP_OUT_MAX = 3072                      # Link_EspNow.h: line bodies kept for the next visit
CURRENT_BUILD = 5                       # the firmware source this stand-in was written from
# Build 4 took the sender of an OLD (replayed) frame for the bridge; build 5 does not. A stand-in
# node made with build=4 still does, so a scenario can show what that costs a node not yet updated.
STALE_MOVES_BRIDGE_UNTIL = 4
OTA_STALL_MS = 90000                    # Ota.cpp
ERR = proto.ERRORS


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def plan_hash(flags: str, region: str, entries: list) -> str:
    """PROTOCOL.md, "Channel plans": FNV-1a 32 over f=<f>;r=<r>;c=<c>, the dwell on every entry."""
    text = "f=%s;r=%s;c=%s" % (flags, region, "/".join("%d.%d" % entry for entry in entries))
    value = 0x811C9DC5
    for byte in text.encode("ascii"):
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    return "%08x" % value


def parse_plan(fields: dict):
    """(flags, region, [(channel, dwell)]) or None for a plan a node refuses."""
    try:
        flags, region, default = int(fields["f"]), fields["r"], int(fields.get("d") or proto.DWELL_DEFAULT_MS)
        if region not in proto.REGIONS or not 0 < flags < 8:
            return None
        entries = []
        for item in filter(None, fields["c"].split("/")):
            channel, _, dwell = item.partition(".")
            entries.append((int(channel), int(dwell) if dwell else default))
        if len(entries) > proto.PLAN_MAX_ENTRIES:
            return None
        if any(not proto.DWELL_MIN_MS <= dwell <= proto.DWELL_MAX_MS for _channel, dwell in entries):
            return None
        return fields["f"], region, entries
    except (KeyError, ValueError):
        return None


def demo_image(build: int, size: int = 6000, good: bool = True) -> bytes:
    """A stand-in app image: the ESP image magic, the build it "is", filler. Not firmware."""
    head = (b"\xE9" if good else b"\x00") + b"BRISTLE-SIM-IMAGE build=%d;" % build
    body = hashlib.sha256(b"filler %d" % build).digest() * (size // 32 + 1)
    return (head + body)[:size]


def image_build(data: bytes):
    marker = b"build="
    at = data.find(marker)
    if at < 0:
        return None
    digits = data[at + len(marker):data.find(b";", at)]
    return int(digits) if digits.isdigit() else None


class SimTime:
    """The one clock of a scenario: seconds that only move when the scenario says so."""

    def __init__(self, start: float = 1000.0, unix: float = 1_795_400_000.0) -> None:
        self.now = float(start)
        self._unix = unix - start

    def monotonic(self) -> float:
        return self.now

    def wall(self) -> float:
        return self._unix + self.now


class Link:
    """Link.h: one connection to a core, with its own handshake state."""

    def __init__(self, kind: int, adoption: bool = False) -> None:
        self.type, self.allows_adoption = kind, adoption
        self.wire = None                    # USB and Bluetooth carry lines; ESP-NOW has none
        self.reader = codec.LineReader(proto.LINE_RELAY_MAX_BYTES)
        self.hello_seen = self.authed = False
        self.core_id = ""
        self.core_nonce = self.node_nonce = b""
        self.last_rx = self.last_hb = self.last_treq = 0
        self.rssi = None

    def drop(self) -> None:
        self.hello_seen = self.authed = False


class Flash:
    """What a board keeps through a restart (NVS and the queue's file system)."""

    def __init__(self, build: int) -> None:
        self.owned, self.key, self.number = False, b"", 0
        self.plan = None                    # (flags text, region, [(channel, dwell)])
        self.run_mask = self.session = 0
        self.repeat = (proto.REPEAT_AP_MS, proto.REPEAT_BLE_MS)
        self.role, self.channel = proto.ROLE_NODE, proto.ESPNOW_CONTROL_CHANNEL
        self.seq_end = proto.QUEUE_SEQ_BLOCK            # the first boot starts at 1000 (PROTOCOL.md's example)
        self.frame_tx_end = 0
        self.frame_rx_floor = 0
        self.records = []                   # the find queue: dicts, oldest first
        self.ack = 0                        # everything below this the core has
        self.dropped = 0
        self.boot = 0
        self.bases = {}                     # boot number -> clock offset, for finds stamped by uptime
        self.build, self.other_build, self.trial = build, None, False
        self.slot = None                    # bytes written to the spare app slot


class ScriptedNode:
    """A stand-in for one Bristle board. ``role`` 1 makes it the bridge. See the module text."""

    def __init__(self, time: SimTime, hwid: str, board: str = "xiao_c5", build: int = CURRENT_BUILD,
                 role: int = 0) -> None:
        self.time, self.hwid, self.board = time, hwid.lower(), board
        self.mac = bytes.fromhex(self.hwid)
        self.flash = Flash(build)
        self.flash.role = role
        self.air = None
        self.log = []                       # what a person would read on its console
        self.sent = collections.Counter()   # lines sent, by type
        self.heard = collections.Counter()  # lines taken from a core, by type
        self.heard_from_usb_since_plug = collections.Counter()
        self.refused = collections.Counter()    # what it answered "no" to, by error
        self.heard_on = collections.defaultdict(collections.Counter)    # link type -> lines taken, by type
        self.plans_taken = []               # every plan it accepted, in order
        self.selftest_ok = True             # a new image that fails this goes back (Ota.cpp)
        self.in_range = True
        self.watch = None
        self.boot_held = False
        self.clock_offset = 0
        self._clock_survives = False
        self._boot()

    # ------------------------------------------------------------------ start-up and restarts
    def _boot(self) -> None:
        f = self.flash
        f.boot += 1
        self.boot_at = self.time.now
        old = getattr(self, "usb", None)
        if getattr(self, "ble", None) is not None and self.ble.wire is not None:
            self.ble.wire.plugged = False   # a Bluetooth connection does not survive a restart
        self.usb = Link(proto.LINK_USB, adoption=True)
        self.esp = Link(proto.LINK_ESPNOW)
        self.ble = Link(proto.LINK_BLE)
        if old is not None:
            self.usb.wire = old.wire        # the cable stays in the socket through a restart
        self.links = [self.usb] if self.is_bridge else [self.usb, self.esp, self.ble]
        self.active = None
        self.next_seq = f.seq_end           # Queue.cpp begin(): a fresh block of numbers every start
        f.seq_end += proto.QUEUE_SEQ_BLOCK
        self.send_cursor = self.last_ack_seen = self.queue_ack()
        self.last_ack_move = 0
        self.clock_valid = getattr(self, "_clock_survives", False)      # Clock.cpp: kept through a reset
        self.clock_since_boot = False
        self._clock_survives = False
        self.rel_used = False
        self.restart_at = None
        self.bad_lines = 0
        self.last_beacon = -10 ** 9
        self.ident_until = 0
        # ESP-NOW link (Link_EspNow.cpp)
        self.esp_out, self.esp_out_bytes = [], 0
        self.esp_paused = self.esp_hurry = False
        self.tx_next = self.tx_end = 0
        self.rx_floor, self.burned = f.frame_rx_floor, -1
        self.have_bridge, self.bridge_mac = False, b""
        self.heard_ms = self.lost_since = self.last_visit = 0
        self.last_sync = -10 ** 9
        self.part_count = self.part_next = self.part_ctr = 0
        self.partial = b""
        self.inbox = collections.deque()    # frames that reached this board's radio
        self.frames_bad = self.frames_stale = 0
        self.visits = 0
        # the bridge's mailbox (Bridge.cpp)
        self.mail = []                      # (mac, frame, at ms)
        self.bridge_rx = self.bridge_tx = self.bridge_dropped = 0
        # updates (Ota.cpp)
        self.ota_state, self.ota_from = "idle", None
        self.ota_total = self.ota_got = self.ota_last_chunk = 0
        self.ota_sha = b""
        self.ota_data = bytearray()
        self.ota_restart_at = None
        self.ota_error = ""
        self.trial_selftest = None
        self.say("started build %d%s" % (f.build, " ON TRIAL" if f.trial else ""))

    def restart(self, power_cut: bool = False) -> None:
        """A reset (the clock survives, the queue is saved first) or a power cut (it does not, and
        finds not yet written to flash are gone)."""
        f = self.flash
        if f.trial:                         # a trial image that resets before it is confirmed: the
            self._roll_back("it reset before it was confirmed")     # bootloader starts the previous one
            return
        if power_cut:
            f.records = [r for r in f.records if r["saved"]]
        else:
            for record in f.records:
                record["saved"] = True
        self._clock_survives = self.clock_valid and not power_cut
        if self._clock_survives:
            self._unix_at_boot = self.unix_ms()
        self._boot()
        if self.clock_valid:
            self.clock_offset = self._unix_at_boot      # the system clock ran on through the reset

    def _roll_back(self, why: str) -> None:
        f = self.flash
        self.say("new firmware not confirmed (%s): going back to build %s" % (why, f.other_build))
        f.build, f.other_build, f.trial = f.other_build, f.build, False
        self._clock_survives = False
        self._boot()

    # ------------------------------------------------------------------ small things
    @property
    def is_bridge(self) -> bool:
        return self.flash.role == proto.ROLE_BRIDGE

    def now(self) -> int:
        """Uptime in milliseconds, like millis()."""
        return int(round((self.time.now - self.boot_at) * 1000))

    def unix_ms(self) -> int:
        return self.clock_offset + self.now()

    def say(self, text: str) -> None:
        self.log.append(text)

    def queue_ack(self) -> int:
        """Queue.cpp ackSeq(): the oldest find still held, or the next number when it holds none."""
        return self.flash.records[0]["seq"] if self.flash.records else max(self.flash.ack, getattr(self, "next_seq", 0))

    def backlog(self) -> int:
        return len(self.flash.records)

    # ------------------------------------------------------------------ sending
    def send(self, link: Link, msg_type: str, fields=None) -> bool:
        line = codec.build(msg_type, fields or {}, proto.LINE_RELAY_MAX_BYTES if msg_type == "ERX" else None)
        self.sent[msg_type] += 1
        if link is self.esp:
            return self._esp_send(codec.strip_crc(line))
        if link.wire is None:
            return False
        link.wire.from_node(line.encode("ascii"))
        return True

    @property
    def cable(self):
        return self.usb.wire

    def _esp_enabled(self) -> bool:
        return self.flash.owned and self.flash.channel != 0 and not self.esp_paused and not self.is_bridge

    def _esp_send(self, body: str) -> bool:
        if not self._esp_enabled() or self.esp_out_bytes + len(body) > ESP_OUT_MAX:
            return False
        if body.startswith("HB:"):          # only the newest heartbeat waits for the next visit
            for index, waiting in enumerate(self.esp_out):
                if waiting.startswith("HB:"):
                    self.esp_out_bytes -= len(waiting)
                    del self.esp_out[index]
                    break
        self.esp_out.append(body)
        self.esp_out_bytes += len(body)
        return True

    def rsp(self, link: Link, command: str, ok: bool, error=None) -> None:
        if not ok:
            self.refused[error] += 1
        self.send(link, "RSP", {"c": command, "ok": 1 if ok else 0, "e": error})

    def flags(self, link: Link) -> int:
        f = self.flash
        value = proto.HB_CAP_5G | proto.HB_CAP_BLE | proto.HB_QUEUE_FLASH | proto.HB_OTA_SLOTS
        if f.run_mask and f.plan and not self.is_bridge and self.ota_state == "idle":
            value |= proto.HB_RUNNING
        if f.owned:
            value |= proto.HB_OWNED
        if link.authed:
            value |= proto.HB_AUTHED
        if self.clock_since_boot:
            value |= proto.HB_CLOCK
        if self.ident_until and self.now() < self.ident_until:
            value |= proto.HB_IDENTIFYING
        if self.is_bridge:
            value |= proto.HB_BRIDGE
        if f.trial:
            value |= proto.HB_OTA_PENDING
        return value

    def heartbeat(self, link: Link) -> None:
        f = self.flash
        fields = {"n": f.number, "f": self.flags(link), "ph": plan_hash(*f.plan) if f.plan else "",
                  "m": f.run_mask, "s": f.session, "lk": link.type, "qb": self.backlog(), "qd": f.dropped,
                  "qc": 2944, "qf": min(100, self.backlog() // 300), "qn": self.next_seq, "qo": self.queue_ack(),
                  "b": f.build, "t": 41, "up": self.now() // 1000, "ram": 210}
        if link is self.esp and link.rssi is not None:
            fields["rs"] = link.rssi
        self.send(link, "HB", fields)
        link.last_hb = self.now()
        if self.is_bridge:
            self.send(link, "BST", {"rx": self.bridge_rx, "tx": self.bridge_tx, "tf": 0, "mb": len(self.mail),
                                    "md": self.bridge_dropped})

    def hello(self, link: Link) -> None:
        f = self.flash
        fields = {"v": proto.PROTOCOL_VERSION, "hw": self.hwid, "bd": self.board, "b": f.build,
                  "cap": self.flags(link), "ro": f.role, "nn": link.node_nonce.hex()}
        if f.owned:
            proof = hmac.new(f.key, proto.LABEL_NODE.encode() + link.core_nonce + self.mac, hashlib.sha256).digest()
            owner = hmac.new(f.key, proto.LABEL_OWNER_ID.encode(), hashlib.sha256).digest()
            fields.update(np=proof[:proto.AUTH_PROOF_BYTES].hex(), oid=owner[:proto.AUTH_OWNER_ID_BYTES].hex())
            if f.number:
                fields["n"] = f.number
        self.send(link, "HELLO", fields)

    def pick_active(self) -> None:
        """Node.cpp pickActive(): finds go out on the authenticated link with the lowest number."""
        best = None
        for link in self.links:
            if link.authed and (best is None or link.type < best.type):
                best = link
        if best is not self.active:
            self.active = best
            self.send_cursor = self.queue_ack()
            self.last_ack_move = self.now()
        self.esp_paused = self.usb.authed and not self.is_bridge

    def forget_everything(self, why: str) -> None:
        f = self.flash
        f.owned, f.key, f.number, f.plan = False, b"", 0, None
        f.run_mask = f.session = 0
        f.records, f.dropped, f.bases = [], 0, {}
        f.ack = self.next_seq
        for link in self.links:
            link.authed = False
        self.esp_out, self.esp_out_bytes = [], 0
        self.have_bridge, self.heard_ms = False, 0
        self.esp.drop()
        self.pick_active()
        self.say("owner forgotten (%s): any core can adopt this node over USB now" % why)

    # ------------------------------------------------------------------ what it hears (scripted)
    def hear(self, kind: str, **fields) -> bool:
        """The radio heard a network ("AP") or a Bluetooth device ("BT"). Queued only while it scans."""
        f = self.flash
        if self.is_bridge or not f.plan or not f.run_mask or self.ota_state != "idle":
            return False
        if kind == "AP" and not f.run_mask & proto.RUN_WIFI or kind == "BT" and not f.run_mask & proto.RUN_BLE:
            return False
        if self.clock_valid:
            stamp = dict(rel=False, sec=self.unix_ms() // 1000, ms=self.unix_ms() % 1000)
        else:
            stamp = dict(rel=True, sec=self.now() // 1000, ms=self.now() % 1000)
            self.rel_used = True
        if self.next_seq >= f.seq_end:
            f.seq_end += proto.QUEUE_SEQ_BLOCK
        f.records.append(dict(seq=self.next_seq, boot=f.boot, session=f.session, kind=kind, fields=dict(fields),
                              saved=False, **stamp))
        self.next_seq += 1
        return True

    def save_queue(self) -> None:
        for record in self.flash.records:
            record["saved"] = True

    def _resolve(self, record):
        """Clock.cpp resolve(): the find's time, or None if it was stamped in a boot that never got a clock."""
        if not record["rel"]:
            return record["sec"], record["ms"]
        up = record["sec"] * 1000 + record["ms"]
        if record["boot"] == self.flash.boot:
            if not self.clock_valid:
                return None
            total = self.clock_offset + up
        elif record["boot"] in self.flash.bases:
            total = self.flash.bases[record["boot"]] + up
        else:
            return None
        return total // 1000, total % 1000

    def _ack(self, seq: int) -> None:
        f = self.flash
        seq = min(seq, self.next_seq)
        if seq > f.ack:
            f.ack = seq
        self._advance()

    def _advance(self) -> None:
        """Queue.cpp advance(): the oldest find goes when the core has acked it, or when it was
        stamped by uptime in an earlier boot that never got a clock (it can never be timed, so it is
        never sent: dropped and counted). Stops at the oldest find that still has to be delivered."""
        f = self.flash
        while f.records:
            record = f.records[0]
            dead = record["rel"] and record["boot"] != f.boot and record["boot"] not in f.bases
            if record["seq"] >= f.ack and not dead:
                break
            f.records.pop(0)
            if record["seq"] >= f.ack and dead:
                f.dropped += 1

    def _stream_finds(self) -> None:
        now = self.now()
        if self.queue_ack() != self.last_ack_seen:
            self.last_ack_seen, self.last_ack_move = self.queue_ack(), now
        if self.active is None or not self.active.authed or not self.clock_valid or self.ota_state != "idle":
            return
        self.send_cursor = max(self.send_cursor, self.queue_ack())
        sent = 0
        for record in self.flash.records:
            if record["seq"] < self.send_cursor:
                continue
            if sent >= 8:
                break
            when = self._resolve(record)
            if when is not None:
                fields = {"q": record["seq"], "t": when[0], "ms": when[1], "s": record["session"]}
                fields.update(record["fields"])
                if not self.send(self.active, record["kind"], fields):
                    return                  # no room on the link: the same find next time
                sent += 1
            self.send_cursor = record["seq"] + 1
        if self.send_cursor >= self.next_seq and self.backlog() and now - self.last_ack_move > proto.RESEND_STALL_MS:
            self.send_cursor = self.queue_ack()
            self.last_ack_move = now

    # ------------------------------------------------------------------ the protocol (Node.cpp onMessage)
    def on_message(self, link: Link, msg) -> None:
        f, t, now = self.flash, msg.type, self.now()
        self.heard[t] += 1
        self.heard_on[link.type][t] += 1
        if link is self.usb:
            self.heard_from_usb_since_plug[t] += 1
        if t == "HI":
            core = msg.get("ci") or ""
            if not link.allows_adoption and core:
                for other in self.links:
                    fresh = other.authed and now - other.last_rx < proto.LINK_TIMEOUT_MS
                    if fresh and other.core_id and other.core_id != core:
                        self.rsp(link, "HI", False, ERR["BUSY"])
                        return
            try:
                nonce = bytes.fromhex(msg.get("cn") or "")
            except ValueError:
                nonce = b""
            if len(nonce) != proto.AUTH_NONCE_BYTES:
                self.rsp(link, "HI", False, ERR["BAD_FIELD"])
                return
            link.core_nonce, link.hello_seen, link.authed = nonce, True, False
            link.node_nonce = os.urandom(proto.AUTH_NONCE_BYTES)
            link.core_id = core
            self.pick_active()
            self.hello(link)
            return
        if t == "PING":
            self.rsp(link, "PING", True)
            return
        if t == "TIME":
            if f.owned and not link.authed:
                return                      # an owned node takes its clock from its owner only
            try:
                seconds = int(msg.get("u") or 0)
                millis = int(msg.get("ms") or 0) % 1000
            except ValueError:
                return
            if seconds < 1_600_000_000:
                return
            self.clock_offset = seconds * 1000 + millis - now
            self.clock_valid = self.clock_since_boot = True
            if self.rel_used:
                f.bases[f.boot] = self.clock_offset
            return
        if t == "IDENT":
            self.ident_until = now + max(0, int(msg.get("d") or 10)) * 1000
            self.rsp(link, "IDENT", True)
            return
        if t == "ADOPT":
            error = self._adopt(link, msg)
            if error is None:
                self.say("adopted as node %d" % f.number)
            self.rsp(link, "ADOPT", error is None, error)
            return
        if t == "AUTH":
            link.authed = False
            try:
                got = bytes.fromhex(msg.get("cp") or "")
            except ValueError:
                got = b""
            if f.owned and link.hello_seen and len(got) == proto.AUTH_PROOF_BYTES:
                want = hmac.new(f.key, proto.LABEL_CORE.encode() + link.node_nonce + self.mac, hashlib.sha256).digest()
                link.authed = hmac.compare_digest(got, want[:proto.AUTH_PROOF_BYTES])
            self.pick_active()
            if link.authed:
                self.heartbeat(link)
            elif f.owned:
                self.say("core proof failed: not my owner")
            return
        if not link.authed:
            self.rsp(link, t, False, ERR["NOT_AUTHED"])
            return
        if t == "ACK":
            self._ack(int(msg.get("q") or 0))
            return
        if t == "OTA":
            self._ota(link, msg)
            return
        if t == "ETX":
            if not self.is_bridge:
                self.rsp(link, "ETX", False, ERR["ROLE"])
                return
            try:
                to, frame = bytes.fromhex(msg.get("to") or ""), unb64(msg.get("d") or "")
            except ValueError:
                self.bad_lines += 1
                return
            if len(to) == 6 and self._frame_shape(frame):
                self._hold(to, frame)
            else:
                self.bad_lines += 1
            return
        if t == "NUM":
            number = int(msg.get("n") or 0)
            if not 0 < number <= 65535:
                self.rsp(link, "NUM", False, ERR["BAD_FIELD"])
                return
            f.number = number
            self.rsp(link, "NUM", True)
        elif t == "PLAN":
            plan = parse_plan(msg.fields)
            if plan is None:
                self.rsp(link, "PLAN", False, ERR["BAD_PLAN"])
                return
            f.plan = plan
            self.plans_taken.append(plan)
            self.rsp(link, "PLAN", True)
        elif t == "RUN":
            f.run_mask = int(msg.get("m") or 0) & (proto.RUN_WIFI | proto.RUN_BLE)
            f.session = int(msg.get("s") or f.session)
            if not f.run_mask:
                self.save_queue()
            self.rsp(link, "RUN", True)
        elif t == "REPEAT":
            f.repeat = (int(msg.get("ap") or 0), int(msg.get("ble") or 0))
            self.rsp(link, "REPEAT", True)
        elif t == "ESPN":
            try:
                channel = int(msg.get("ch"))
            except (TypeError, ValueError):
                channel = -1
            if not 0 <= channel <= 13:
                self.rsp(link, "ESPN", False, ERR["BAD_FIELD"])
                return
            if channel != f.channel:
                f.channel = channel
                self.have_bridge, self.heard_ms = False, 0
            self.rsp(link, "ESPN", True)
        elif t == "ROLE":
            role = int(msg.get("r") if msg.get("r") is not None else -1)
            if not link.allows_adoption:
                self.rsp(link, "ROLE", False, ERR["NOT_USB"])
                return
            if role not in (proto.ROLE_NODE, proto.ROLE_BRIDGE):
                self.rsp(link, "ROLE", False, ERR["BAD_FIELD"])
                return
            self.rsp(link, "ROLE", True)
            if role != f.role:
                f.role = role
                self.say("role is now %s: restarting" % ("bridge" if role else "node"))
                self.restart_at = now + 500
            return
        elif t == "WATCH":
            mac = msg.get("m") or ""
            if self.is_bridge or len(mac) != 12:
                self.rsp(link, "WATCH", False, ERR["ROLE"] if self.is_bridge else ERR["BAD_FIELD"])
                return
            self.watch = mac if int(msg.get("d") or 0) > 0 else None
            self.rsp(link, "WATCH", True)
        elif t == "FORGET":
            self.rsp(link, "FORGET", True)
            self.forget_everything("core asked")
            return
        else:
            self.rsp(link, t, False, ERR["UNKNOWN"])
            return
        self.heartbeat(link)

    def _adopt(self, link: Link, msg):
        f = self.flash
        if not link.allows_adoption:
            return ERR["NOT_USB"]
        if f.owned:
            return ERR["OWNED"]
        try:
            key, number = bytes.fromhex(msg.get("k") or ""), int(msg.get("n") or 0)
        except ValueError:
            return ERR["BAD_FIELD"]
        if len(key) != proto.AUTH_KEY_BYTES or not 0 < number <= 65535:
            return ERR["BAD_FIELD"]
        f.key, f.owned, f.number = key, True, number
        return None

    # ------------------------------------------------------------------ updates (Ota.cpp)
    def _otas(self, link: Link, error=None) -> None:
        fields = {"st": "error" if error else {"receiving": "receiving", "ready": "ready"}.get(self.ota_state, "idle")}
        if self.ota_state == "receiving" and not error:
            fields["o"] = self.ota_got
        if error:
            fields["e"] = error
        self.send(link, "OTAS", fields)

    def _ota_drop(self, why) -> None:
        self.ota_state, self.ota_from = "idle", None
        self.ota_data = bytearray()
        if why:
            self.ota_error = why
            self.say("update dropped (%s): still running build %d" % (why, self.flash.build))
        self.esp_hurry = False

    def _ota(self, link: Link, msg) -> None:
        op, now = msg.get("op"), self.now()
        if not op:
            self.rsp(link, "OTA", False, ERR["BAD_FIELD"])
        elif op == "status":
            self._otas(link)
        elif op == "abort":
            if self.ota_state != "idle" and self.ota_from is link:
                self._ota_drop("the core stopped it")
            self.rsp(link, "OTA", True)
            self._otas(link)
        elif op == "begin":
            if self.ota_state != "idle" and self.ota_from is not link:
                self.rsp(link, "OTA", False, ERR["BUSY"])
                return
            if self.ota_state != "idle":
                self._ota_drop(None)
            try:
                sha, size = bytes.fromhex(msg.get("sha") or ""), int(msg.get("size") or 0)
            except ValueError:
                sha, size = b"", 0
            if len(sha) != 32 or size <= 0:
                self.rsp(link, "OTA", False, ERR["BAD_FIELD"])
            elif size > proto.FLASH_APP_SLOT_BYTES:
                self.rsp(link, "OTA", False, ERR["OTA_SPACE"])
            else:
                self.ota_total, self.ota_got, self.ota_sha = size, 0, sha
                self.ota_data = bytearray()
                self.ota_state, self.ota_from, self.ota_last_chunk = "receiving", link, now
                self.ota_error = ""
                self.esp_hurry = link is self.esp
                self.say("update started: %d bytes" % size)
                self.rsp(link, "OTA", True)
                self._otas(link)
        elif self.ota_state == "idle" or self.ota_from is not link:
            self.rsp(link, "OTA", False, ERR["OTA_STATE"])
            self._otas(link)
        elif op == "chunk":
            try:
                offset, data = int(msg.get("o") if msg.get("o") is not None else -1), unb64(msg.get("d") or "")
            except ValueError:
                offset, data = -1, b""
            if (self.ota_state == "receiving" and offset == self.ota_got and data
                    and len(data) <= proto.OTA_CHUNK_BYTES + 8 and self.ota_got + len(data) <= self.ota_total):
                self.ota_data += data       # only the chunk it is waiting for is ever written
                self.ota_got += len(data)
                self.ota_last_chunk = now
            self._otas(link)                # every chunk is answered with the byte wanted next
        elif op == "verify":
            if self.ota_state != "receiving" or self.ota_got != self.ota_total:
                self.rsp(link, "OTA", False, ERR["OTA_STATE"])
            elif hashlib.sha256(bytes(self.ota_data)).digest() != self.ota_sha:
                self._ota_drop("the image does not match its SHA-256")
                self._otas(link, ERR["OTA_VERIFY"])
            else:
                self.ota_state = "ready"
                self._otas(link)
        elif op == "commit":
            if self.ota_state != "ready":
                self.rsp(link, "OTA", False, ERR["OTA_STATE"])
            elif not self.ota_data or self.ota_data[0] != 0xE9:     # esp_ota_end: not an app image for this chip
                self.ota_state, self.ota_from = "idle", None
                self.rsp(link, "OTA", False, ERR["OTA_VERIFY"])
                self._otas(link, ERR["OTA_VERIFY"])
            else:
                self.flash.slot = bytes(self.ota_data)
                self.rsp(link, "OTA", True)
                self.say("update complete: restarting into the new image (on trial until it meets its owner)")
                self.ota_restart_at = now + 1200
        else:
            self.rsp(link, "OTA", False, ERR["BAD_FIELD"])

    def _start_new_image(self) -> None:
        f = self.flash
        new = image_build(f.slot or b"")
        f.other_build, f.build, f.trial = f.build, new if new is not None else f.build, True
        f.slot = None
        self.save_queue()
        self._clock_survives = self.clock_valid
        if self._clock_survives:
            self._unix_at_boot = self.unix_ms()
        self._boot()
        if self.clock_valid:
            self.clock_offset = self._unix_at_boot

    # ------------------------------------------------------------------ USB
    def plug(self, wire, kind: int = proto.LINK_USB) -> None:
        link = self.usb if kind == proto.LINK_USB else self.ble
        link.wire = wire
        link.reader = codec.LineReader(proto.LINE_RELAY_MAX_BYTES)
        if link is self.usb:
            self.heard_from_usb_since_plug = collections.Counter()

    def unplug(self, kind: int = proto.LINK_USB) -> None:
        link = self.usb if kind == proto.LINK_USB else self.ble
        link.wire = None
        if link is self.ble:
            link.drop()                     # a Bluetooth connection that ends is known to have ended
            if self.ota_state != "idle" and self.ota_from is link and self.ota_restart_at is None:
                self._ota_drop("its link closed")
            self.pick_active()

    def advertising(self) -> bool:
        """Bluetooth: a node its core has on USB does not advertise (PROTOCOL.md); a bridge never does."""
        return not self.is_bridge and not self.usb.authed and self.ble.wire is None

    def wire_bytes(self, kind: int, data: bytes) -> None:
        """Bytes off the USB port (or lines written over Bluetooth). A line that does not check out
        is counted and dropped."""
        link = self.usb if kind == proto.LINK_USB else self.ble
        if link not in self.links:
            return
        for line in link.reader.feed(data):
            try:
                msg = codec.parse(line, proto.LINE_RELAY_MAX_BYTES)
            except codec.LineError:
                self.bad_lines += 1
                continue
            link.last_rx = self.now()
            self.on_message(link, msg)

    # ------------------------------------------------------------------ ESP-NOW (Link_EspNow.cpp)
    def _next_counter(self) -> int:
        if self.tx_next >= self.tx_end:
            f = self.flash
            self.tx_next = max(f.frame_tx_end, self.tx_next)        # Store: reserve a fresh block
            f.frame_tx_end = self.tx_next + proto.FRAME_COUNTER_BLOCK
            self.tx_end = self.tx_next + proto.FRAME_COUNTER_BLOCK
        self.tx_next += 1
        return self.tx_next - 1

    def _send_frame(self, kind: int, frag: int, payload: bytes, to=None) -> None:
        """``to``: one address for this frame only; None = the bridge, or everyone while it is not known."""
        frame = crypto.frame_build(self.flash.key, kind, self._next_counter(), frag, payload)
        if to is None and self.have_bridge:
            to = self.bridge_mac
        if self.air is not None:
            self.air.from_node(self, to, frame)

    def _flush(self, budget: int) -> int:
        sent = 0
        while self.esp_out and sent < budget:
            first = self.esp_out[0].encode("ascii")
            if len(first) > PAYLOAD_MAX:    # one long line: alone, in pieces with consecutive counters
                count = -(-len(first) // PAYLOAD_MAX)
                if count <= proto.FRAME_MAX_FRAGMENTS:
                    for index in range(count):
                        self._send_frame(proto.FRAME_KIND_LINES, (index << 4) | count,
                                         first[index * PAYLOAD_MAX:(index + 1) * PAYLOAD_MAX])
                        sent += 1
                self.esp_out_bytes -= len(self.esp_out.pop(0))
                continue
            payload, taken = b"", 0
            while taken < len(self.esp_out) and len(self.esp_out[taken]) <= PAYLOAD_MAX:
                body = self.esp_out[taken].encode("ascii")
                if len(payload) + len(body) + (1 if payload else 0) > PAYLOAD_MAX:
                    break
                payload = payload + b"\n" + body if payload else body
                taken += 1
            self._send_frame(proto.FRAME_KIND_LINES, 0x01, payload)
            sent += 1
            for _ in range(taken):
                self.esp_out_bytes -= len(self.esp_out.pop(0))
        return sent

    def _lines(self, payload: bytes) -> None:
        for body in filter(None, payload.split(b"\n")):
            try:
                msg = codec.parse_body(body.decode("ascii"))
            except (codec.LineError, UnicodeDecodeError):
                self.bad_lines += 1
                continue
            self.esp.last_rx = self.now()
            self.on_message(self.esp, msg)

    def _handle_frame(self, sender: bytes, data: bytes, rssi: int) -> None:
        now = self.now()
        try:
            frame = crypto.frame_parse(self.flash.key, data) if self.flash.owned else None
        except crypto.FrameError:
            frame = None
        if frame is None:
            self.frames_bad += 1            # not signed with my owner's key: not for me
            return
        if frame.kind == proto.FRAME_KIND_SYNC:
            if len(frame.payload) == 4:
                floor = struct.unpack("<I", frame.payload)[0]
                if floor > self.tx_next:
                    self.tx_next = max(self.flash.frame_tx_end, floor)
                    self.flash.frame_tx_end = self.tx_next + proto.FRAME_COUNTER_BLOCK
                    self.tx_end = self.tx_next + proto.FRAME_COUNTER_BLOCK
            return
        if frame.ctr < self.rx_floor:
            self.frames_stale += 1
            if now - self.last_sync >= 1000:
                self.last_sync = now
                if self.flash.build <= STALE_MOVES_BRIDGE_UNTIL:
                    self.bridge_mac, self.have_bridge = sender, True
                    self._send_frame(proto.FRAME_KIND_SYNC, 0x01, struct.pack("<I", self.rx_floor))
                else:                       # the answer goes to the sender; the bridge stays the bridge
                    self._send_frame(proto.FRAME_KIND_SYNC, 0x01, struct.pack("<I", self.rx_floor), to=sender)
            return
        block = frame.ctr // proto.FRAME_COUNTER_BLOCK
        if block != self.burned:
            self.flash.frame_rx_floor = max(self.flash.frame_rx_floor, (block + 1) * proto.FRAME_COUNTER_BLOCK)
            self.burned = block
        self.rx_floor = frame.ctr + 1
        self.bridge_mac, self.have_bridge = sender, True       # a frame from my owner: the bridge is there
        self.heard_ms, self.lost_since = now or 1, 0
        self.esp.rssi = rssi
        if frame.kind != proto.FRAME_KIND_LINES:
            return
        index, count = frame.frag >> 4, frame.frag & 0x0F
        if count <= 1:
            self.part_count = 0
            if index == 0 and count == 1:
                self._lines(frame.payload)
            return
        if index == 0:
            self.partial, self.part_count, self.part_next, self.part_ctr = b"", count, 0, frame.ctr
        if self.part_count != count or index != self.part_next or frame.ctr != self.part_ctr:
            self.part_count = 0             # a piece is missing or out of order: the line is dropped
            self.bad_lines += 1
            return
        self.partial += frame.payload
        self.part_next += 1
        self.part_ctr = frame.ctr + 1
        if self.part_next == self.part_count:
            self.part_count = 0
            self._lines(self.partial)

    def _bridge_known(self) -> bool:
        return bool(self.heard_ms) and self.now() - self.heard_ms < proto.ESPNOW_LOST_MS

    def visit(self) -> None:
        """One visit to the control channel: send what waits (or a poll), then take what comes back
        and answer it, as long as frames keep arriving."""
        self.last_visit = self.now()
        self.visits += 1
        if not self._bridge_known():
            self.have_bridge = False        # ask everyone on the channel
            self.lost_since = self.lost_since or self.now() or 1
        sent = self._flush(proto.ESPNOW_VISIT_FRAMES)
        if not sent:
            self._send_frame(proto.FRAME_KIND_POLL, 0x01, b"")
        for _round in range(8):
            if not self.inbox:
                break
            while self.inbox:
                sender, data, rssi = self.inbox.popleft()
                self._handle_frame(sender, data, rssi)
            if sent < proto.ESPNOW_VISIT_FRAMES:
                sent += self._flush(proto.ESPNOW_VISIT_FRAMES - sent)

    # ------------------------------------------------------------------ the bridge (Bridge.cpp)
    @staticmethod
    def _frame_shape(frame: bytes) -> bool:
        return (proto.FRAME_HEADER_BYTES + proto.FRAME_TAG_BYTES <= len(frame) <= proto.FRAME_MAX_BYTES
                and frame[:2] == proto.FRAME_MAGIC.encode() and frame[2] == proto.PROTOCOL_VERSION
                and len(frame) == proto.FRAME_HEADER_BYTES + frame[9] + proto.FRAME_TAG_BYTES)

    def _hold(self, mac: bytes, frame: bytes) -> None:
        waiting = [item for item in self.mail if item[0] == mac]
        if len(waiting) >= proto.ESPNOW_MAILBOX_FRAMES:
            self.mail.remove(waiting[0])    # that node is not checking in: its oldest mail goes
            self.bridge_dropped += 1
        self.mail.append((mac, frame, self.now()))

    def on_air(self, sender: bytes, frame: bytes, rssi: int) -> None:
        """The bridge's radio heard a frame. It checks the shape and nothing else."""
        if not self.is_bridge or not self._frame_shape(frame):
            return
        self.bridge_rx += 1
        for item in [item for item in self.mail if item[0] == sender]:     # first: the node is listening now
            self.mail.remove(item)
            if self.air is not None and self.air.from_bridge(self, sender, item[1]):
                self.bridge_tx += 1
        if self.usb.authed:
            self.send(self.usb, "ERX", {"fr": sender.hex(), "rs": rssi, "d": b64(frame)})

    # ------------------------------------------------------------------ the main loop (Node.cpp tick)
    def tick(self) -> None:
        now, f = self.now(), self.flash
        if self.restart_at is not None and now >= self.restart_at:
            self.restart()
            return
        if self.ota_restart_at is not None and now >= self.ota_restart_at:
            self._start_new_image()
            return
        self._advance()
        if self.is_bridge:
            fresh = [item for item in self.mail if now - item[2] <= proto.ESPNOW_MAILBOX_MS]
            self.bridge_dropped += len(self.mail) - len(fresh)      # mail nobody came for in 30 s
            self.mail = fresh
        elif self._esp_enabled() and self.in_range:
            every = 20 if self.esp_hurry else proto.ESPNOW_VISIT_INTERVAL_MS
            if self.lost_since and now - self.lost_since > 60000:
                every = proto.ESPNOW_SEARCH_MS
            if now - self.last_visit >= every:
                self.visit()
        self._stream_finds()
        owner_authed = False
        for link in self.links:
            owner_authed |= link.authed
            if not link.hello_seen:
                continue
            if now - link.last_rx > proto.LINK_TIMEOUT_MS:
                was = link.authed
                link.drop()
                if self.ota_state != "idle" and self.ota_from is link and self.ota_restart_at is None:
                    self._ota_drop("its link closed")
                self.pick_active()
                if was:
                    self.say("core went quiet: link closed, finds are being kept")
                continue
            if link.authed and now - link.last_hb >= proto.HEARTBEAT_MS:
                self.heartbeat(link)
            if not self.clock_since_boot and now - link.last_treq >= proto.TIME_REQUEST_MS:
                self.send(link, "TREQ")
                link.last_treq = now
        if self.ota_state != "idle" and self.ota_restart_at is None and now - self.ota_last_chunk > OTA_STALL_MS:
            self._ota_drop("nothing arrived for 90 s")
        if f.trial:
            if self.trial_selftest is None and now > 3000:
                self.trial_selftest = bool(self.selftest_ok)
            met = owner_authed if f.owned else now > 60000
            if self.trial_selftest and met:
                f.trial = False
                self.say("new firmware confirmed: build %d stays" % f.build)
            elif now > proto.OTA_CONFIRM_MS or self.trial_selftest is False:
                self._roll_back("it did not pass its self-test" if self.trial_selftest is False
                                else "its owner's core never authenticated")
                return
        if (self.cable is not None and not self.usb.hello_seen and now - self.last_beacon >= proto.BEACON_MS):
            self.last_beacon = now          # nobody has spoken to it on USB: it says what it is, unasked
            self.send(self.usb, "BCN", {"hw": self.hwid, "bd": self.board, "b": f.build,
                                        "o": 1 if f.owned else 0, "ro": f.role})
        if self.boot_held:
            self.boot_held = False
            self.forget_everything("BOOT held")


class Cable:
    """A USB cable between one core session and one scripted board: bytes both ways, in the order
    they were sent, a moment later. Faults: ``garble`` (how many of the next lines get a byte
    flipped, per direction) and ``chop`` (bytes are handed over in small pieces)."""

    def __init__(self, node: ScriptedNode, kind: int = proto.LINK_USB) -> None:
        self.node, self.kind = node, kind
        self.session = None
        self.to_node, self.to_core = collections.deque(), collections.deque()
        self.garble = {"to_node": 0, "to_core": 0}
        self.chop = 0
        self.hold_to_node = False           # the board is not reading its port yet: lines wait in it
        self.lose = {"to_node": ["", 0], "to_core": ["", 0]}    # [line start, how many more to lose]
        self.reader = codec.LineReader(proto.LINE_RELAY_MAX_BYTES)
        self.lines = {"to_node": 0, "to_core": 0}
        self.plugged = True
        node.plug(self, kind)

    def _fault(self, way: str, data: bytes):
        self.lines[way] += 1
        start, left = self.lose[way]
        if left > 0 and data.startswith(start.encode("ascii")):
            self.lose[way][1] = left - 1
            return None
        if self.garble[way] > 0:
            self.garble[way] -= 1
            middle = len(data) // 2
            data = data[:middle] + bytes([data[middle] ^ 0x01]) + data[middle + 1:]
        return data

    def from_core(self, line: str) -> None:
        data = self._fault("to_node", line.encode("ascii")) if self.plugged else None
        if data is not None:
            self.to_node.append(data)

    def from_node(self, data: bytes) -> None:
        data = self._fault("to_core", data) if self.plugged else None
        if data is not None:
            self.to_core.append(data)

    def _pieces(self, data: bytes):
        if not self.chop:
            yield data
            return
        for at in range(0, len(data), self.chop):
            yield data[at:at + self.chop]

    def pump(self) -> bool:
        """Hand over what was on the wire when this was called: a line takes a moment, so an answer
        to an answer waits for the next call. True if anything moved."""
        moved = False
        for _ in range(0 if self.hold_to_node else len(self.to_node)):
            if not self.plugged:
                break
            moved = True
            for piece in self._pieces(self.to_node.popleft()):
                self.node.wire_bytes(self.kind, piece)
        for _ in range(len(self.to_core)):
            if not self.plugged or self.session is None:
                break
            moved = True
            for piece in self._pieces(self.to_core.popleft()):
                for line in self.reader.feed(piece):
                    self.session.feed_line(line)
        return moved

    def pull(self) -> None:
        self.plugged = False
        self.to_node.clear()
        self.to_core.clear()
        if (self.node.usb if self.kind == proto.LINK_USB else self.node.ble).wire is self:
            self.node.unplug(self.kind)


class Air:
    """The control channel: who hears whom. A node's frame reaches every bridge in range (or only the
    one it is addressed to); what a bridge sends reaches that node's radio during the same visit.

    Faults: ``lose_to_bridge`` / ``lose_to_node`` (how many of the next frames vanish), ``tamper``
    (flip a bit in the next frames a bridge hears), and ``heard`` keeps every frame that was on the
    air so a scenario can play one back later, as an eavesdropper could."""

    def __init__(self) -> None:
        self.boards = []
        self.lose_to_bridge = self.lose_to_node = self.tamper = 0
        self.heard = []                     # (direction, node mac, frame)
        self.out_of_range = set()           # (bridge hwid, node hwid) pairs that cannot hear each other

    def add(self, board: ScriptedNode) -> None:
        board.air = self
        self.boards.append(board)

    @property
    def bridges(self) -> list:
        """The boards that are in the bridge role right now (a ROLE command changes that)."""
        return [board for board in self.boards if board.is_bridge]

    def _reach(self, bridge: ScriptedNode, node_mac: bytes) -> bool:
        return (bridge.hwid, node_mac.hex()) not in self.out_of_range

    def from_node(self, node: ScriptedNode, to, frame: bytes) -> None:
        self.heard.append(("up", node.mac, frame))
        if not node.in_range:
            return
        for bridge in self.bridges:
            if (to is not None and to != bridge.mac) or not self._reach(bridge, node.mac):
                continue
            if self.lose_to_bridge > 0:
                self.lose_to_bridge -= 1
                continue
            data = frame
            if self.tamper > 0:
                self.tamper -= 1
                data = frame[:12] + bytes([frame[12] ^ 0x40]) + frame[13:] if len(frame) > 20 else frame[:-1] + bytes([frame[-1] ^ 1])
            bridge.on_air(node.mac, data, -58)

    def from_bridge(self, bridge: ScriptedNode, to: bytes, frame: bytes) -> bool:
        self.heard.append(("down", to, frame))
        node = next((board for board in self.boards if board.mac == to and not board.is_bridge), None)
        if node is None or not node.in_range or not self._reach(bridge, to):
            return False
        if self.lose_to_node > 0:
            self.lose_to_node -= 1
            return True                     # it left the bridge; nobody caught it
        node.inbox.append((bridge.mac, frame, -61))
        return True

    def inject(self, sender: bytes, frame: bytes) -> None:
        """Somebody else transmits this frame, claiming to be ``sender`` (a replay, a forgery)."""
        for bridge in self.bridges:
            bridge.on_air(sender, frame, -70)

    def inject_to_node(self, node: ScriptedNode, frame: bytes, sender: bytes = b"\x02\xff\xff\xff\xff\xff") -> None:
        node.inbox.append((sender, frame, -70))


class Core:
    """The game's real Bristle service (plugins/_bristle/service.BristleService) in a scratch
    folder, without its threads: the scenario turns the crank. Its sessions, its ESP-NOW hub and
    its update sender read the scenario's clock instead of the machine's."""

    def __init__(self, ctx, time: SimTime, name: str = "core", prepare=None) -> None:
        from plugins._bristle import service as service_module
        ctx.patch(service_module, "bench_dirs", lambda: [])     # never the real user's bench key
        self.ctx, self.time, self.name = ctx, time, name
        self.install = os.path.join(ctx.tmp, name)
        os.makedirs(self.install, exist_ok=True)
        if prepare is not None:
            prepare(self.install)
        self.finds = []
        self.cables = {}                    # hwid -> (cable, stand-in link record)
        self.radios = {}                    # hwid -> (Bluetooth connection, its session)
        self._module = service_module
        self.boot()

    def boot(self) -> None:
        """Start (or start again, as after quitting the game): everything is read back from disk."""
        from types import SimpleNamespace
        self.service = self._module.BristleService(self.install, os.path.join(self.install, "loot"), self._on_find)
        fleet, time = self.service.fleet, self.time
        make = fleet.new_session

        def new_session(send, link_type):
            session = make(send, link_type)
            session.mono, session.clock = time.monotonic, time.wall
            return session
        fleet.new_session = new_session
        self.service.hub._new_session = new_session
        self.service.hub._mono = time.monotonic
        self._namespace = SimpleNamespace

    def _on_find(self, find) -> None:
        self.finds.append(find)
        # the game writes the find and then says so (ingest.py); here it is "written" at once
        self.service.fleet.delivery.node(find.hwid).processed(find.seq)

    # ------------------------------------------------------------------ USB
    def plug(self, node: ScriptedNode) -> Cable:
        cable = Cable(node)
        device = "/dev/sim-" + node.hwid[-4:]
        session = self.service.fleet.new_session(cable.from_core, proto.LINK_USB)
        session.usb_key = "sim:" + node.hwid
        cable.session = session
        port = self._namespace(key=session.usb_key, device=device, vid=0x303A, pid=0x1001, chip="stand-in",
                               serial_number=node.hwid)
        link = self._namespace(session=session, device=device, port=port, close=lambda: self.unplug(node))
        self.service.links[device] = link
        self.cables[node.hwid] = (cable, link)
        session.start()
        return cable

    def unplug(self, node: ScriptedNode) -> None:
        cable, link = self.cables.pop(node.hwid, (None, None))
        if cable is None:
            return
        cable.pull()
        self.service._closed(link)          # what the USB reader thread does when the port goes away

    def bluetooth(self, node: ScriptedNode):
        """Connect to that node over Bluetooth LE, as link_ble.py does for a node in Node Memory that
        advertises: after that the lines are the same as on USB. None if it is not advertising."""
        if not node.advertising() or self.service.registry.known(node.hwid) is None:
            return None
        wire = Cable(node, proto.LINK_BLE)
        session = self.service.fleet.new_session(wire.from_core, proto.LINK_BLE)
        session.hwid = node.hwid
        wire.session = session
        self.radios[node.hwid] = (wire, session)
        session.start()
        return session

    def bluetooth_off(self, node: ScriptedNode) -> None:
        wire, session = self.radios.pop(node.hwid, (None, None))
        if wire is not None:
            wire.pull()
            self.service.fleet.drop_session(session)

    def session(self, node: ScriptedNode):
        """This core's session with that board (USB first), whatever state it is in."""
        found = [s for s in self.service.fleet.sessions if s.hwid == node.hwid]
        found.sort(key=lambda s: s.link_type)
        return found[0] if found else None

    def tick(self) -> None:
        service = self.service
        service.fleet.tick()
        service.hub.tick()
        service._ota_tick()

    def said(self, text: str) -> bool:
        return any(text in message for message, _kind in self.service.messages)

    def messages(self) -> str:
        return " | ".join(message for message, _kind in self.service.messages)

    def release_folder(self, build: int, good: bool = True, size: int = 6000, board: str = "xiao_c5") -> bytes:
        """A stand-in firmware release in this core's scratch folder (the real flasher reads it)."""
        folder = os.path.join(self.install, "sim-firmware", "v1-build%d-sim" % build, board)
        os.makedirs(folder, exist_ok=True)
        data = demo_image(build, size, good=True)
        if not good:
            data = data[:40] + b"\xff" * 8 + data[48:]     # still starts like an image; see the scenario
        name = "bristle-%s-app.bin" % board
        with open(os.path.join(folder, name), "wb") as handle:
            handle.write(data)
        with open(os.path.join(os.path.dirname(folder), "SHA256SUMS"), "w", encoding="ascii", newline="\n") as handle:
            handle.write("%s  %s/%s\n" % (hashlib.sha256(data).hexdigest(), board, name))
        self.service.conf["firmware_dir"] = os.path.join(self.install, "sim-firmware")
        return data

    def start_update(self, node: ScriptedNode) -> None:
        self.service.start_ota(node.hwid)
        self.service.ota.sender._mono = self.time.monotonic
        self.service.ota.sender._heard()


class World:
    """Everything in one scenario, and the crank that moves it."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.time = SimTime()
        self.air = Air()
        self.cores, self.boards = [], []

    def core(self, name: str = "core", prepare=None) -> Core:
        core = Core(self.ctx, self.time, name, prepare)
        self.cores.append(core)
        return core

    def node(self, number: int, **kwargs) -> ScriptedNode:
        """A board with a made-up, locally administered address (02:...): never a real one's."""
        board = ScriptedNode(self.time, "02b1c5%06x" % number, **kwargs)
        self.boards.append(board)
        self.air.add(board)
        return board

    def settle(self, hops: int = 3) -> None:
        """A few hand-overs, not "until it is quiet": a cable has a speed, so a long exchange (an
        update, a backlog) takes simulated time and a scenario can cut it in the middle."""
        for _ in range(hops):
            moved = False
            for core in self.cores:
                for cable, _link in list(core.cables.values()) + list(core.radios.values()):
                    moved |= cable.pump()
            if not moved:
                return

    def run(self, seconds: float, step: float = 0.05, until=None) -> bool:
        """Let ``seconds`` pass. With ``until``: stop as soon as it is true, and say whether it was."""
        end = self.time.now + seconds
        while self.time.now < end:
            self.time.now = min(end, self.time.now + step)
            for board in self.boards:
                board.tick()
            self.settle()
            for core in self.cores:
                core.tick()
            self.settle()
            if until is not None and until():
                return True
        return until() if until is not None else True
