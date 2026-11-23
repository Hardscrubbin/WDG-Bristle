# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""Unit tests for the wireless links' logic: signed frames and their counters, fragments, the
bridge hub, the Bluetooth link's choice of nodes, the USB beacon, firmware updates.

    python -I -m unittest discover -s tests -v      (copied here by Bristle's tools/sync_wdg.py: edit it there)
"""
import hashlib
import pathlib
import struct
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugins._bristle import codec, crypto, espnow, link_ble, link_usb, ota, proto  # noqa: E402
from plugins._bristle import session as S  # noqa: E402
from plugins._bristle.delivery import DeliveryTracker  # noqa: E402
from plugins._bristle.fleet import Fleet  # noqa: E402
from plugins._bristle.link_espnow import EspNowHub  # noqa: E402
from plugins._bristle.registry import Registry  # noqa: E402

KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
OTHER = bytes.fromhex("ff0102030405060708090a0b0c0d0e0f")
HW = "a0b1c2d3e4f5"


class Ticker:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def pair(core_store=None, node_store=None):
    """The two ends of one link, each with its own permanent storage."""
    core_store = core_store if core_store is not None else espnow.CounterStore()
    node_store = node_store if node_store is not None else espnow.CounterStore()
    return espnow.Endpoint(KEY, core_store, HW), espnow.Endpoint(KEY, node_store, "core"), core_store, node_store


class Counters(unittest.TestCase):
    def test_a_frame_is_taken_once(self):
        core, node, _, _ = pair()
        frame = core.seal_lines([b"PING:"])[0]
        self.assertEqual(node.open(frame).bodies, [b"PING:"])
        self.assertEqual(node.open(frame).status, espnow.STALE)

    def test_counters_only_go_up_and_start_a_fresh_block_after_a_restart(self):
        store = espnow.CounterStore()
        first = espnow.Endpoint(KEY, store, HW)
        used = [crypto.frame_parse(KEY, first.seal(proto.FRAME_KIND_POLL)).ctr for _ in range(3)]
        self.assertEqual(used, [0, 1, 2])
        again = espnow.Endpoint(KEY, espnow.CounterStore(store.data), HW)      # restart: storage only
        self.assertEqual(crypto.frame_parse(KEY, again.seal(proto.FRAME_KIND_POLL)).ctr, proto.FRAME_COUNTER_BLOCK)

    def test_a_block_that_runs_out_reserves_the_next(self):
        store = espnow.CounterStore()
        end = espnow.Endpoint(KEY, store, HW)
        counters = [end.tx.next() for _ in range(proto.FRAME_COUNTER_BLOCK + 2)]
        self.assertEqual(counters, list(range(proto.FRAME_COUNTER_BLOCK + 2)))
        self.assertEqual(store.data[HW]["tx"], 2 * proto.FRAME_COUNTER_BLOCK)

    def test_a_restarted_receiver_refuses_what_it_could_have_seen(self):
        core, node, core_store, node_store = pair()
        old = [core.seal_lines([b"PING:"])[0] for _ in range(5)]
        for frame in old[:3]:
            self.assertEqual(node.open(frame).status, espnow.OK)
        node2 = espnow.Endpoint(KEY, espnow.CounterStore(node_store.data), "core")     # the node restarts
        for frame in old:                                    # replayed, and the two it never saw
            self.assertEqual(node2.open(frame).status, espnow.STALE)

    def test_sync_gets_a_live_sender_going_again(self):
        core, node, _, node_store = pair()
        self.assertEqual(node.open(core.seal_lines([b"PING:"])[0]).status, espnow.OK)
        node2 = espnow.Endpoint(KEY, espnow.CounterStore(node_store.data), "core")
        self.assertEqual(node2.open(core.seal_lines([b"PING:"])[0]).status, espnow.STALE)
        self.assertEqual(core.open(node2.sync_frame()).status, espnow.SYNCED)
        self.assertEqual(node2.open(core.seal_lines([b"RUN:m=1,s=2"])[0]).bodies, [b"RUN:m=1,s=2"])

    def test_both_ends_restarted_still_meet(self):
        core, node, core_store, node_store = pair()
        for _ in range(3):
            node.open(core.seal_lines([b"PING:"])[0])
            core.open(node.seal_lines([b"RSP:c=PING,ok=1"])[0])
        core2 = espnow.Endpoint(KEY, espnow.CounterStore(core_store.data), HW)
        node2 = espnow.Endpoint(KEY, espnow.CounterStore(node_store.data), "core")
        # fresh blocks on both sides are already above what the other end burned
        self.assertEqual(node2.open(core2.seal_lines([b"PING:"])[0]).status, espnow.OK)
        self.assertEqual(core2.open(node2.seal_lines([b"RSP:c=PING,ok=1"])[0]).status, espnow.OK)

    def test_a_replayed_sync_does_nothing(self):
        core, node, _, _ = pair()
        sync = node.sync_frame()
        core.open(sync)
        before = core.tx.next()
        core.open(sync)
        self.assertEqual(core.tx.next(), before + 1)

    def test_wrong_key_and_damage_are_refused(self):
        core, node, _, _ = pair()
        frame = bytearray(core.seal_lines([b"PING:"])[0])
        stranger = espnow.Endpoint(OTHER, espnow.CounterStore(), HW)
        self.assertEqual(node.open(stranger.seal_lines([b"PING:"])[0]).status, espnow.BAD)
        frame[12] ^= 1
        self.assertEqual(node.open(bytes(frame)).status, espnow.BAD)
        self.assertEqual(node.open(b"BR").status, espnow.BAD)

    def test_a_sync_must_carry_four_bytes(self):
        core, node, _, _ = pair()
        self.assertEqual(core.open(node.seal(proto.FRAME_KIND_SYNC, b"\x01")).status, espnow.BAD)

    def test_the_spec_vectors_hold_for_poll_and_fragment_headers(self):
        import json
        vectors = json.loads((ROOT / "tests" / "fixtures" / "bristle_vectors.json").read_text())["frame"]
        self.assertGreaterEqual(len(vectors), 3)
        for v in vectors:
            built = crypto.frame_build(bytes.fromhex(v["key"]), v["kind"], v["ctr"], v["frag"], v["payload"].encode())
            self.assertEqual(built.hex(), v["expect"])


class Fragments(unittest.TestCase):
    def test_lines_are_packed_whole(self):
        bodies = [("AP:q=%d,t=1700000000,s=1,b=aabbccddeeff,c=6,r=-60,k=3,e=Net%d" % (i, i)).encode() for i in range(12)]
        payloads = espnow.split_payloads(bodies)
        self.assertGreater(len(payloads), 1)
        self.assertLess(len(payloads), len(bodies))
        for frag, payload in payloads:
            self.assertEqual(frag, 0x01)
            self.assertLessEqual(len(payload), espnow.MAX_PAYLOAD)
        self.assertEqual(b"\n".join(p for _f, p in payloads).split(b"\n"), bodies)

    def test_a_long_line_is_cut_and_put_back(self):
        core, node, _, _ = pair()
        long_line = b"ETX:to=aabbccddeeff,d=" + b"A" * 300
        frames = core.seal_lines([b"PING:", long_line, b"ACK:q=5"])
        kinds = [crypto.frame_parse(KEY, f).frag for f in frames]
        self.assertEqual(kinds, [0x01, 0x02, 0x12, 0x01])
        got = []
        for frame in frames:
            opened = node.open(frame)
            got += opened.bodies
        self.assertEqual(got, [b"PING:", long_line, b"ACK:q=5"])

    def test_a_missing_piece_drops_that_line_only(self):
        core, node, _, _ = pair()
        frames = core.seal_lines([b"X" * 500, b"ACK:q=5"])
        self.assertEqual(node.open(frames[0]).status, espnow.PARTIAL)
        # frames[1] is lost
        self.assertEqual(node.open(frames[2]).bodies, [])
        self.assertEqual(node.reassembler.dropped, 1)
        self.assertEqual(node.open(frames[3]).bodies, [b"ACK:q=5"])

    def test_too_long_and_bad_bodies_are_refused(self):
        with self.assertRaises(ValueError):
            espnow.split_payloads([b"X" * (espnow.MAX_PAYLOAD * proto.FRAME_MAX_FRAGMENTS + 1)])
        with self.assertRaises(ValueError):
            espnow.split_payloads([b"A\nB"])

    def test_every_line_the_protocol_allows_fits(self):
        longest = b"X" * (proto.LINE_RELAY_MAX_BYTES - 6)
        self.assertLessEqual(len(espnow.split_payloads([longest])), 2)


class Hub(unittest.TestCase):
    """The bridge hub against the reference Endpoint: frames in as ERX fields, ETX lines out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = Registry(pathlib.Path(self.tmp.name) / "n.json")
        self.reg.adopt(HW)
        self.now = Ticker()
        self.events = []
        self.fleet = Fleet(self.reg, KEY, on_event=lambda k, s, i: self.events.append(k), core_id="c0ffee01")
        self.to_bridge = []
        self.bridge = self.fleet.new_session(self.to_bridge.append, proto.LINK_USB)
        self.bridge.state, self.bridge.role = S.AUTHED, proto.ROLE_BRIDGE
        self.core_store = espnow.CounterStore()
        self.hub = EspNowHub(self.fleet.new_session, self.fleet.drop_session, KEY, self.core_store,
                             is_blocked=self.reg.is_blocked, mono=self.now)
        self.hub.attach(self.bridge)
        self.node = espnow.Endpoint(KEY, espnow.CounterStore(), "core")

    def tearDown(self):
        self.tmp.cleanup()

    def erx(self, frame, mac=HW):
        self.hub.on_erx(self.bridge, {"fr": mac, "rs": "-61", "d": S.b64url(frame)})

    def delivered(self):
        """What the bridge was handed for the node, opened as the node would."""
        bodies = []
        for line in self.to_bridge:
            msg = codec.parse(line, proto.LINE_RELAY_MAX_BYTES)
            if msg.type == "ETX":
                self.assertEqual(msg.get("to"), HW)
                bodies += self.node.open(S.b64url_decode(msg.get("d"))).bodies
        self.to_bridge.clear()
        return [codec.parse_body(b.decode()) for b in bodies]

    def test_a_poll_from_our_node_gets_a_hello_and_the_node_comes_online(self):
        self.erx(self.node.poll_frame())
        hi = self.delivered()
        self.assertEqual([m.type for m in hi], ["HI"])
        self.assertEqual(hi[0].get("ci"), "c0ffee01")
        hello = codec.body("HELLO", {"v": 1, "hw": HW, "bd": "xiao_c5", "b": 4, "cap": 48,
                                      "nn": "ffeeddccbbaa99887766554433221100",
                                      "np": crypto.node_proof(KEY, bytes.fromhex(hi[0].get("cn")), bytes.fromhex(HW))})
        self.erx(self.node.seal_lines([hello.encode()])[0])
        answer = [m.type for m in self.delivered()]
        self.assertEqual(answer[:2], ["AUTH", "TIME"])       # then the plan and the settings follow
        self.assertIn("authed", self.events)
        session = self.fleet.session_for(HW)
        self.assertEqual(session.link_type, proto.LINK_ESPNOW)
        self.assertEqual(session.health.rssi, -61)

    def test_frames_signed_with_another_key_never_make_a_session(self):
        stranger = espnow.Endpoint(OTHER, espnow.CounterStore(), "core")
        self.erx(stranger.poll_frame(), mac="0102030405aa")
        self.assertEqual(self.hub.stats["strangers"], 1)
        self.assertEqual(self.hub.peers, {})
        self.assertEqual(self.to_bridge, [])

    def test_a_blocked_device_is_ignored(self):
        self.reg.block("0102030405bb")
        self.erx(self.node.poll_frame(), mac="0102030405bb")
        self.assertEqual(self.hub.peers, {})

    def test_a_replayed_frame_is_answered_with_one_sync(self):
        frame = self.node.poll_frame()
        self.erx(frame)
        self.to_bridge.clear()
        self.erx(frame)
        self.erx(frame)                                    # at once: no second sync yet
        etx = [codec.parse(l, proto.LINE_RELAY_MAX_BYTES) for l in self.to_bridge]
        self.assertEqual(len(etx), 1)
        self.assertEqual(self.node.open(S.b64url_decode(etx[0].get("d"))).status, espnow.SYNCED)
        self.assertEqual(self.hub.stats["stale"], 2)

    def test_only_the_newest_ack_and_ping_wait_for_the_next_visit(self):
        self.erx(self.node.poll_frame())
        self.delivered()
        peer = self.hub.peers[HW]
        peer.in_mailbox = proto.ESPNOW_MAILBOX_FRAMES      # the mailbox is full: everything waits
        for q in (5, 9, 12):
            self.hub._queue(peer, codec.build("ACK", {"q": q}))
            self.hub._queue(peer, codec.build("PING"))
        self.assertEqual(list(peer.out), ["ACK:q=12", "PING:"])
        self.hub.flush()
        self.assertEqual(self.to_bridge, [])
        self.erx(self.node.poll_frame())                   # the node checks in: the mailbox is free again
        self.assertEqual([m.type for m in self.delivered()], ["ACK", "PING"])

    def test_a_node_that_stays_away_goes_offline_and_a_lost_bridge_takes_all_with_it(self):
        self.erx(self.node.poll_frame())
        self.assertEqual(len(self.hub.peers), 1)
        self.now.t += 3 * proto.LINK_TIMEOUT_MS / 1000 + 1
        self.hub.tick()
        self.assertEqual(self.hub.peers, {})
        self.erx(self.node.poll_frame())
        self.hub.detach(self.bridge)
        self.assertEqual(self.hub.peers, {})
        self.assertEqual([s for s in self.fleet.sessions if s.link_type == proto.LINK_ESPNOW], [])


class Busy(unittest.TestCase):
    def make(self, link=proto.LINK_ESPNOW):
        self.tmp = tempfile.TemporaryDirectory()
        reg = Registry(pathlib.Path(self.tmp.name) / "n.json")
        reg.adopt(HW)
        self.now = Ticker()
        self.sent, self.events = [], []
        s = S.NodeSession(send=self.sent.append, link_type=link, registry=reg, delivery=DeliveryTracker(),
                          owner_key=KEY, mono=self.now, on_event=lambda k, s, i: self.events.append(k),
                          core_id="c0ffee02")
        s.start()
        return s

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_busy_node_is_left_alone_and_asked_again_later(self):
        s = self.make()
        s.feed_line(codec.build("RSP", {"c": "HI", "ok": 0, "e": proto.ERRORS["BUSY"]}))
        self.assertEqual(s.state, S.BUSY)
        self.assertEqual(self.events.count("busy"), 1)
        sent = len(self.sent)
        for _ in range(20):
            self.now.t += 1
            s.tick()
        self.assertEqual(len(self.sent), sent)             # nothing at all while it is busy
        self.now.t += proto.BUSY_RETRY_MS / 1000
        s.tick()
        self.assertEqual(codec.parse(self.sent[-1]).type, "HI")
        s.feed_line(codec.build("RSP", {"c": "HI", "ok": 0, "e": proto.ERRORS["BUSY"]}))
        self.assertEqual(self.events.count("busy"), 1)     # said once, not every half minute

    def test_wireless_links_wait_longer_than_usb(self):
        usb, now = self.make(proto.LINK_USB), None
        self.tmp.cleanup()
        air = self.make(proto.LINK_ESPNOW)
        self.assertGreater(air.hello_wait_s, usb.hello_wait_s)
        self.assertGreater(air.silence_s, usb.silence_s)
        self.assertGreaterEqual(air.ack_interval_s, proto.ESPNOW_VISIT_INTERVAL_MS / 1000)


class Bridges(unittest.TestCase):
    def test_a_bridge_gets_no_plan_and_is_not_counted_as_a_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = Registry(pathlib.Path(tmp) / "n.json")
            reg.adopt(HW)
            reg.adopt("0102030405cc")
            fleet = Fleet(reg, KEY)
            sent = {HW: [], "0102030405cc": []}
            for hw, role in ((HW, proto.ROLE_BRIDGE), ("0102030405cc", proto.ROLE_NODE)):
                s = fleet.new_session(sent[hw].append, proto.LINK_USB)
                s.start()
                s.feed_line(codec.build("HELLO", {
                    "v": 1, "hw": hw, "bd": "xiao_c5", "b": 4, "cap": 48, "ro": role,
                    "nn": "ffeeddccbbaa99887766554433221100",
                    "np": crypto.node_proof(KEY, s._core_nonce, bytes.fromhex(hw))}))
            fleet.set_control_channel(6)
            fleet.start()
            self.assertEqual([s.hwid for s in fleet.bridges()], [HW])
            self.assertEqual([s.hwid for s in fleet.authed()], ["0102030405cc"])
            self.assertEqual(fleet.totals()["known"], 1)
            bridge = fleet.bridges()[0]
            bridge.feed_line(codec.build("HB", {"n": 1, "f": proto.HB_BRIDGE | proto.HB_AUTHED | proto.HB_CLOCK,
                                                 "ph": "", "m": 0, "s": 0, "lk": 1, "qb": 0, "qd": 0, "qc": 0,
                                                 "qf": 0, "qn": 0, "qo": 0, "b": 4}))
            types = [codec.parse(l).type for l in sent[HW]]
            self.assertIn("ESPN", types)
            self.assertNotIn("PLAN", types)
            self.assertNotIn("RUN", types)
            self.assertNotIn("ACK", types)


class Bluetooth(unittest.TestCase):
    def advert(self, hwid, owned=True, rssi=-60, seen=100.0):
        return link_ble.Advert(hwid, owned, rssi, seen)

    def test_service_data(self):
        self.assertEqual(link_ble.parse_service_data(bytes.fromhex(HW) + b"\x01"), (HW, True))
        self.assertEqual(link_ble.parse_service_data(bytes.fromhex(HW) + b"\x00"), (HW, False))
        self.assertIsNone(link_ble.parse_service_data(b"\x01\x02"))
        self.assertIsNone(link_ble.parse_service_data(None))

    def test_only_nodes_in_node_memory_are_linked_strongest_first_up_to_the_limit(self):
        adverts = {h: self.advert(h, rssi=r) for h, r in (("aa" * 6, -80), ("bb" * 6, -50), ("cc" * 6, -65),
                                                          ("dd" * 6, -40))}
        adverts["ee" * 6] = self.advert("ee" * 6, owned=False)
        known = {"aa" * 6, "bb" * 6, "cc" * 6}
        connect, unowned, strangers, waiting = link_ble.choose(adverts, known, set(), set(), set(), {}, 105.0, 2)
        self.assertEqual(connect, ["bb" * 6, "cc" * 6])
        self.assertEqual(unowned, ["ee" * 6])
        self.assertEqual(strangers, ["dd" * 6])            # someone else's rig: never linked
        self.assertEqual(waiting, 1)

    def test_usb_wins_and_blocked_stale_and_just_failed_are_skipped(self):
        adverts = {h: self.advert(h) for h in ("aa" * 6, "bb" * 6, "cc" * 6, "dd" * 6)}
        adverts["dd" * 6] = self.advert("dd" * 6, seen=10.0)
        known = set(adverts)
        connect, _, _, waiting = link_ble.choose(adverts, known, {"cc" * 6}, set(), {"aa" * 6}, {"bb" * 6: 100.0},
                                                 105.0, 5)
        self.assertEqual(connect, [])
        self.assertEqual(waiting, 0)

    def test_links_that_are_up_count_against_the_limit(self):
        adverts = {h: self.advert(h) for h in ("aa" * 6, "bb" * 6)}
        connect, _, _, waiting = link_ble.choose(adverts, set(adverts), set(), {"11" * 6, "22" * 6}, set(), {},
                                                 105.0, 2)
        self.assertEqual((connect, waiting), ([], 2))


class CannedBytes:
    """What a serial port hands back when it is only read: the bytes given, then nothing."""

    def __init__(self, data):
        self.data = data
        self.written = []
        self.closed = False

    def read(self, n):
        chunk, self.data = self.data[:n], self.data[n:]
        return chunk

    def write(self, data):
        self.written.append(data)

    def close(self):
        self.closed = True


class Beacon(unittest.TestCase):
    def listen(self, data):
        port = CannedBytes(data)
        clock = Ticker()

        def mono():
            clock.t += 0.05
            return clock.t
        result = link_usb.listen_for_beacon("/dev/ttyACM9", 1.0, serial_factory=lambda device: port, mono=mono)
        self.assertEqual(port.written, [])                 # listening never writes
        self.assertTrue(port.closed)
        return result

    def test_a_bristle_board_is_recognised_by_its_beacon(self):
        line = codec.build("BCN", {"hw": HW, "bd": "xiao_c5", "b": 4, "o": 0})
        beacon = self.listen(b"# Bristle node build 4\r\n" + line.encode())
        self.assertEqual(beacon["hw"], HW)

    def test_other_firmware_and_noise_are_not(self):
        self.assertIsNone(self.listen(b"esp32c5> Unrecognized command\r\nI (123) wifi: scan done\r\n"))
        self.assertIsNone(self.listen(b""))
        self.assertIsNone(self.listen(b"BCN:hw=" + HW.encode() + b",bd=x,b=4*0000\n"))     # bad CRC

    def test_the_cache_listens_once_and_gives_a_slow_starter_a_second_chance(self):
        calls = []
        answers = [None, {"hw": HW}]
        clock = Ticker()
        slept = []
        cache = link_usb.BeaconCache(listen=lambda device: (calls.append(device), answers.pop(0))[1],
                                     retry_s=20.0, settle_s=2.5, mono=clock,
                                     sleep=lambda s: (slept.append(s), setattr(clock, "t", clock.t + s)))
        cache.present({"usb:a"})
        self.assertIsNone(cache.check("usb:a", "/dev/ttyACM0"))
        self.assertIsNone(cache.check("usb:a", "/dev/ttyACM0"))        # no second listen yet
        self.assertEqual(len(calls), 1)
        clock.t += 21
        self.assertEqual(cache.check("usb:a", "/dev/ttyACM0"), {"hw": HW})
        self.assertEqual(cache.check("usb:a", "/dev/ttyACM0"), {"hw": HW})
        self.assertEqual(len(calls), 2)
        answers.extend([None, None, {"hw": HW}])
        cache.present({"usb:a", "usb:c"})
        clock.t += 5
        for _ in range(4):                                 # silent twice: not listened to again
            self.assertIsNone(cache.check("usb:c", "/dev/ttyACM2"))
            clock.t += 30
        self.assertEqual(len(calls), 4)
        answers.clear()
        slept.clear()
        self.assertEqual(slept, [])                        # it was there at the first look: no settling
        cache.present({"usb:a", "usb:b", "usb:c"})         # a board plugged in just now
        answers.append(None)
        cache.check("usb:b", "/dev/ttyACM1")
        self.assertEqual(len(slept), 1)                    # ... is given time to start before a "no"
        cache.present(set())                               # unplugged: forgotten
        self.assertIsNone(cache.known("usb:a"))

    def test_the_scanner_says_hello_to_a_port_that_beacons_and_only_lists_one_that_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = Registry(pathlib.Path(tmp) / "n.json")
            policy = link_usb.PortPolicy(reg)
            opened, shown = [], []
            ports = [link_usb.PortInfo("/dev/ttyACM0", 0x303A, 0x1001, "A0:B1:C2:D3:E4:F5"),
                     link_usb.PortInfo("/dev/ttyACM1", 0x303A, 0x1001, "11:22:33:44:55:66")]
            cache = link_usb.BeaconCache(listen=lambda device: {"hw": HW} if device.endswith("0") else None)
            scanner = link_usb.UsbScanner(opened.append, policy, ports=lambda: ports, in_use=lambda d: False,
                                          on_port=lambda p, a, r: shown.append((p.device, a)), beacons=cache)
            scanner.scan_once()
            self.assertEqual([p.device for p in opened], ["/dev/ttyACM0"])
            self.assertIn(("/dev/ttyACM1", link_usb.ASK), shown)


def image(size=5000):
    return bytes([ota.ESP_IMAGE_MAGIC]) + bytes((i * 7) % 251 for i in range(size - 1))


class Ota(unittest.TestCase):
    def make(self, size=5000, **kw):
        self.sent = []
        self.now = Ticker()
        self.img = image(size)
        return ota.OtaSender(self.img, self.sent.append, mono=self.now, **kw)

    def node(self, sender, lose=()):
        """Feeds the sender the answers the protocol defines: the next chunk in order is taken,
        anything else is answered with the offset that is wanted."""
        got = bytearray()
        handled = 0
        while handled < len(self.sent):
            cmd = self.sent[handled]
            handled += 1
            if handled in lose:
                continue
            if cmd["op"] == "begin":
                got.clear()
                sender.on_status({"st": "receiving", "o": "0"})
            elif cmd["op"] == "chunk":
                if cmd["o"] == len(got):
                    import base64
                    got += base64.urlsafe_b64decode(cmd["d"] + "=" * (-len(cmd["d"]) % 4))
                sender.on_status({"st": "receiving", "o": str(len(got))})
            elif cmd["op"] == "verify":
                ok = hashlib.sha256(got).hexdigest() == sender.sha and len(got) == len(self.img)
                sender.on_status({"st": "ready"} if ok else {"st": "error", "e": proto.ERRORS["OTA_VERIFY"]})
            elif cmd["op"] == "commit":
                sender.on_rsp(True)
        return bytes(got)

    def test_an_image_arrives_whole_and_in_order(self):
        s = self.make()
        s.start()
        self.assertEqual(self.sent[0], {"op": "begin", "size": 5000, "sha": hashlib.sha256(self.img).hexdigest()})
        self.assertEqual(self.node(s), self.img)
        self.assertEqual(s.state, ota.DONE)
        self.assertEqual(s.percent, 100)
        self.assertEqual([c["op"] for c in self.sent[-2:]], ["verify", "commit"])
        self.assertEqual(s.resends, 0)

    def test_never_more_than_a_window_ahead_of_the_node(self):
        s = self.make(window=4)
        s.start()
        s.on_status({"st": "receiving", "o": "0"})
        chunks = [c for c in self.sent if c["op"] == "chunk"]
        self.assertEqual([c["o"] for c in chunks], [0, 144, 288, 432])

    def test_every_chunk_line_fits_every_link(self):
        s = self.make(size=2000)
        s.start()
        self.node(s)
        for cmd in self.sent:
            line = codec.build("OTA", dict(cmd, o=proto.FLASH_APP_SLOT_BYTES) if cmd["op"] == "chunk" else cmd)
            self.assertLessEqual(len(line), proto.LINE_MAX_BYTES)
            self.assertLessEqual(len(codec.strip_crc(line)), espnow.MAX_PAYLOAD)

    def test_lost_chunks_are_sent_again_from_where_the_node_is(self):
        s = self.make()
        s.start()
        got = self.node(s, lose={3, 9, 10})
        while s.active:                                     # the node went quiet: time passes
            self.now.t += 2.1
            s.tick()
            got = self.node_resume(s, got)
        self.assertEqual(s.state, ota.DONE)
        self.assertEqual(got, self.img)
        self.assertGreater(s.resends, 0)

    def node_resume(self, sender, got):
        import base64
        got = bytearray(got)
        while self.sent:
            cmd = self.sent.pop(0)
            if cmd["op"] == "chunk":
                if cmd["o"] == len(got):
                    got += base64.urlsafe_b64decode(cmd["d"] + "=" * (-len(cmd["d"]) % 4))
                sender.on_status({"st": "receiving", "o": str(len(got))})
            elif cmd["op"] == "verify":
                sender.on_status({"st": "ready"} if bytes(got) == self.img else {"st": "error", "e": "bad"})
            elif cmd["op"] == "commit":
                sender.on_rsp(True)
        return bytes(got)

    def test_a_failed_check_on_the_node_ends_the_update(self):
        s = self.make()
        s.start()
        s.on_status({"st": "receiving", "o": "0"})
        s.on_status({"st": "error", "e": proto.ERRORS["OTA_VERIFY"]})
        self.assertEqual(s.state, ota.FAILED)
        self.assertEqual(s.error, proto.ERRORS["OTA_VERIFY"])

    def test_a_link_cut_before_commit_leaves_the_old_firmware(self):
        s = self.make()
        s.start()
        s.on_status({"st": "receiving", "o": "0"})
        s.link_lost()
        self.assertEqual(s.state, ota.FAILED)
        self.assertIn("old firmware", s.error)
        self.assertNotIn("commit", [c["op"] for c in self.sent])

    def test_a_link_cut_by_the_restart_after_commit_counts_as_done(self):
        s = self.make(size=1024)
        s.start()
        s.on_status({"st": "receiving", "o": "0"})
        s.on_status({"st": "receiving", "o": "1024"})
        s.on_status({"st": "ready"})
        self.assertEqual(self.sent[-1], {"op": "commit"})
        s.link_lost()
        self.assertEqual(s.state, ota.DONE)

    def test_a_node_that_never_answers_gets_an_abort(self):
        s = self.make(max_stalls=3)
        s.start()
        for _ in range(10):
            self.now.t += 2.1
            s.tick()
        self.assertEqual(s.state, ota.FAILED)
        self.assertEqual(self.sent[-1], {"op": "abort"})

    def test_a_node_that_restarted_mid_update_is_noticed(self):
        s = self.make()
        s.start()
        s.on_status({"st": "receiving", "o": "0"})
        s.on_status({"st": "idle"})
        self.assertEqual(s.state, ota.FAILED)

    def test_only_an_app_image_that_fits_is_taken(self):
        with self.assertRaises(ota.OtaError):
            ota.check_image(b"\x00" * 4096)
        with self.assertRaises(ota.OtaError):
            ota.check_image(b"\xe9" + b"\x00" * 10)
        with self.assertRaises(ota.OtaError):
            ota.check_image(b"\xe9" + b"\x00" * proto.FLASH_APP_SLOT_BYTES)
        ota.check_image(image())


class Relay(unittest.TestCase):
    def test_relay_lines_may_be_longer_than_any_other_line(self):
        frame = crypto.frame_build(KEY, 0, 1, 1, b"X" * espnow.MAX_PAYLOAD)
        self.assertEqual(len(frame), proto.FRAME_MAX_BYTES)
        line = codec.build("ERX", {"fr": HW, "rs": -100, "d": S.b64url(frame)}, proto.LINE_RELAY_MAX_BYTES)
        self.assertLessEqual(len(line), proto.LINE_RELAY_MAX_BYTES)
        with self.assertRaises(codec.LineError):
            codec.parse(line)
        self.assertEqual(S.b64url_decode(codec.parse(line, proto.LINE_RELAY_MAX_BYTES).get("d")), frame)
        self.assertEqual(struct.unpack_from("<I", frame, 4)[0], 1)


if __name__ == "__main__":
    unittest.main()
