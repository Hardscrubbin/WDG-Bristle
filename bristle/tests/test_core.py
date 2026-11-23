"""Unit tests for the Bristle core library: test vectors and pure logic. No fakes, no simulations.

    python -I -m unittest discover -s tests -v      (run from the Bristle folder)
"""
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

from bristle import codec, crypto, proto  # noqa: E402
from bristle.delivery import NodeDelivery  # noqa: E402
from bristle.gpsplace import Fix, GpsTrack, valid  # noqa: E402
from bristle.plan import NodeCaps, Plan, split  # noqa: E402
from bristle.registry import Registry  # noqa: E402
from bristle import session as S  # noqa: E402
from bristle.delivery import DeliveryTracker  # noqa: E402

VEC = json.loads((ROOT / "tests" / "vectors" / "vectors.json").read_text())
KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")


class Vectors(unittest.TestCase):
    def test_crc(self):
        for v in VEC["crc16"]:
            self.assertEqual(f"{codec.crc16(v['input'].encode()):04X}", v["expect"])
        self.assertEqual(codec.crc16(b"123456789"), 0x29B1)   # standard CCITT-FALSE check value

    def test_encode(self):
        for v in VEC["encode"]:
            self.assertEqual(codec.pct_encode(v["input"]), v["expect"])
            self.assertEqual(codec.pct_decode(v["expect"]), v["input"])

    def test_plan_hash(self):
        for v in VEC["plan_hash"]:
            self.assertEqual(Plan.from_fields(str(v["f"]), v["r"], v["c"]).hash(), v["expect"])

    def test_owner_and_proofs(self):
        for v in VEC["owner_id"]:
            self.assertEqual(crypto.owner_id(bytes.fromhex(v["key"])), v["expect"])
        for v in VEC["node_proof"]:
            self.assertEqual(crypto.node_proof(bytes.fromhex(v["key"]), bytes.fromhex(v["cn"]),
                                               bytes.fromhex(v["hw"])), v["expect"])
        for v in VEC["core_proof"]:
            self.assertEqual(crypto.core_proof(bytes.fromhex(v["key"]), bytes.fromhex(v["nn"]),
                                               bytes.fromhex(v["hw"])), v["expect"])

    def test_frame(self):
        for v in VEC["frame"]:
            data = crypto.frame_build(bytes.fromhex(v["key"]), v["kind"], v["ctr"], v["frag"], v["payload"].encode())
            self.assertEqual(data.hex(), v["expect"])
            f = crypto.frame_parse(bytes.fromhex(v["key"]), data)
            self.assertEqual(f.payload, v["payload"].encode())


class Codec(unittest.TestCase):
    def test_round_trip_awkward_ssid(self):
        ssid = "Café ☕ ,=*:%\r\n end"
        line = codec.build("AP", {"q": 5, "t": 1, "s": 1, "b": "a42bb0123456", "c": 6, "r": -60, "k": 3, "e": ssid})
        m = codec.parse(line)
        codec.check_required(m)
        self.assertEqual(m.get("e"), ssid)
        self.assertNotIn("\n", line[:-1])

    def test_bad_crc_and_garbage_dropped(self):
        line = codec.build("PING")
        bad = line.replace("PING", "PONG")
        with self.assertRaises(codec.LineError):
            codec.parse(bad)
        for junk in ["", "PING", "PING:*12", "ping:*0000", "PING:a*ZZZZ", "\x00\xff"]:
            with self.assertRaises(codec.LineError):
                codec.parse(junk)

    def test_too_long_rejected(self):
        with self.assertRaises(codec.LineError):
            codec.build("AP", {"e": "x" * 300})

    def test_reader_splits_and_caps(self):
        r = codec.LineReader(max_bytes=16)
        self.assertEqual(r.feed(b"ab\r\ncd"), [b"ab"])
        self.assertEqual(r.feed(b"\n"), [b"cd"])
        r.feed(b"x" * 40)
        self.assertGreaterEqual(r.overflows, 1)

    def test_required_fields(self):
        with self.assertRaises(codec.LineError):
            codec.check_required(codec.parse(codec.build("HB", {"n": 1})))


class Frames(unittest.TestCase):
    def test_tamper_and_replay(self):
        data = bytearray(crypto.frame_build(KEY, 0, 1, 1, b"PING:"))
        data[12] ^= 1
        with self.assertRaises(crypto.FrameError):
            crypto.frame_parse(KEY, bytes(data))
        with self.assertRaises(crypto.FrameError):
            crypto.frame_parse(bytes(16), crypto.frame_build(KEY, 0, 1, 1, b"PING:"))
        g = crypto.ReplayGuard()
        self.assertTrue(g.accept(b"a", 5))
        self.assertFalse(g.accept(b"a", 5))
        self.assertFalse(g.accept(b"a", 4))
        self.assertTrue(g.accept(b"b", 1))


class Planner(unittest.TestCase):
    def covered(self, plans):
        return {ch for p in plans.values() for ch, _ in p.entries}

    def test_single_node_does_everything(self):
        p = split([NodeCaps(1, cap_5g=True)])
        self.assertTrue(p[1].flags & proto.PLAN_FLAG_WIFI and p[1].flags & proto.PLAN_FLAG_BLE)
        allc = set(proto.REGIONS["U"]["ch24"]) | set(proto.REGIONS["U"]["ch5"])
        self.assertEqual(self.covered(p), allc)

    def test_five_nodes_one_on_ble_full_coverage(self):
        nodes = [NodeCaps(i, cap_5g=True) for i in range(1, 6)]
        p = split(nodes)
        ble = [n for n, pl in p.items() if pl.flags == proto.PLAN_FLAG_BLE]
        self.assertEqual(len(ble), 1)
        allc = set(proto.REGIONS["U"]["ch24"]) | set(proto.REGIONS["U"]["ch5"])
        self.assertEqual(self.covered(p), allc)
        for pl in p.values():
            pl.validate()

    def test_5ghz_only_to_capable(self):
        p = split([NodeCaps(1, cap_5g=False), NodeCaps(2, cap_5g=True), NodeCaps(3, cap_5g=True)])
        five = set(proto.REGIONS["U"]["ch5"])
        self.assertFalse({c for c, _ in p[1].entries} & five)

    def test_pins(self):
        p = split([NodeCaps(1, True, pin="5hi"), NodeCaps(2, True), NodeCaps(3, True, pin="ble")])
        self.assertEqual(p[3].flags, proto.PLAN_FLAG_BLE)
        half = len(proto.REGIONS["U"]["ch5"]) // 2
        self.assertEqual([c for c, _ in p[1].entries], proto.REGIONS["U"]["ch5"][half:])

    def test_forty_nodes_everyone_gets_work(self):
        p = split([NodeCaps(i, cap_5g=True) for i in range(1, 41)])
        self.assertEqual(len(p), 40)
        for pl in p.values():
            self.assertTrue(pl.entries or pl.flags == proto.PLAN_FLAG_BLE)
            pl.validate()

    def test_kismet_heavy_goes_last_with_min_dwell(self):
        p = split([NodeCaps(1, True), NodeCaps(2, True)], kismet_heavy={1, 6, 11})
        wifi = [pl for pl in p.values() if pl.flags & proto.PLAN_FLAG_WIFI][0]
        tail = [e for e in wifi.entries if e[0] in {1, 6, 11}]
        self.assertTrue(all(dw == proto.DWELL_MIN_MS for _, dw in tail))
        idx = [i for i, e in enumerate(wifi.entries) if e[0] in {1, 6, 11}]
        self.assertEqual(idx, list(range(len(wifi.entries) - len(idx), len(wifi.entries))))

    def test_every_plan_fits_one_line_and_keeps_its_hash(self):
        """A lone node gets every channel of the region: that must still be one protocol line."""
        worst = 0
        for region in ("U", "E"):
            reg = proto.REGIONS[region]
            heavy_sets = [set(), {1, 6, 11}, set(reg["ch24"]), set(reg["ch5"]), set(reg["ch24"] + reg["ch5"][::2])]
            for heavy in heavy_sets:
                for dwell in (proto.DWELL_MIN_MS, 120, proto.DWELL_MAX_MS):
                    for count in (1, 2, 3, 5, 12, 40):
                        nodes = [NodeCaps(number=i + 1, cap_5g=(i % 4 != 3)) for i in range(count)]
                        for plan in split(nodes, region, dwell_ms=dwell, kismet_heavy=heavy).values():
                            line = codec.build("PLAN", plan.fields())
                            worst = max(worst, len(line))
                            self.assertLessEqual(len(line), proto.LINE_MAX_BYTES)
                            f = codec.parse(line)
                            back = Plan.from_fields(f.get("f"), f.get("r"), f.get("c"), f.get("d"))
                            self.assertEqual(back.entries, plan.entries)
                            self.assertEqual(back.hash(), plan.hash())
        self.assertGreater(worst, 100)             # the full-region plans really were in there

    def test_plan_without_a_default_dwell_needs_one_on_every_channel(self):
        self.assertEqual(Plan.from_fields("1", "U", "1/6.30/11", "120").entries, [(1, 120), (6, 30), (11, 120)])
        self.assertEqual(Plan.from_fields("1", "U", "1/6.30/11", "120").hash(),
                         Plan.from_fields("1", "U", "1.120/6.30/11.120").hash())
        with self.assertRaises(ValueError):
            Plan.from_fields("1", "U", "1/6.30/11")
        many = Plan(1, "U", [(ch, 100 + i) for i, ch in enumerate(proto.REGIONS["U"]["ch24"] + proto.REGIONS["U"]["ch5"])])
        with self.assertRaises(ValueError):       # 36 different dwell times can't fit one line
            many.validate()

    def test_region_e(self):
        p = split([NodeCaps(1, True)], region="E")
        self.assertIn(13, {c for c, _ in p[1].entries})


class RegistryTests(unittest.TestCase):
    def test_memory_block_and_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "nodes.json"
            r = Registry(path)
            a = r.adopt("A0:B1:C2:D3:E4:F5", board="xiao_c5", cap_5g=True)
            b = r.adopt("a0b1c2d3e4f6")
            self.assertEqual((a.number, b.number), (1, 2))
            self.assertIs(r.adopt("a0b1c2d3e4f5"), r.known("a0b1c2d3e4f5"))   # idempotent
            r.block("a0b1c2d3e4f6", "my biscuit")
            self.assertIsNone(r.known("a0b1c2d3e4f6"))
            with self.assertRaises(PermissionError):
                r.adopt("a0b1c2d3e4f6")
            r2 = Registry(path)
            self.assertEqual(r2.known("a0b1c2d3e4f5").number, 1)
            self.assertTrue(r2.is_blocked("A0-B1-C2-D3-E4-F6"))
            self.assertEqual(r2.next_number(), 2)

    def test_renumber_closes_gaps(self):
        with tempfile.TemporaryDirectory() as d:
            r = Registry(pathlib.Path(d) / "n.json")
            r.adopt("000000000001", number=3)
            r.adopt("000000000002", number=7)
            self.assertEqual(sorted(r.renumber().values()), [1, 2])


class DeliveryTests(unittest.TestCase):
    def test_exactly_once_and_ack_point(self):
        d = NodeDelivery()
        self.assertTrue(d.accept(0))
        self.assertTrue(d.accept(2))
        self.assertEqual(d.ack_point, 1)
        self.assertTrue(d.accept(1))
        self.assertEqual(d.ack_point, 3)
        self.assertFalse(d.accept(1))
        self.assertFalse(d.accept(2))
        self.assertEqual(d.duplicates, 2)

    def test_gap_skipped_when_node_reports_oldest(self):
        d = NodeDelivery()
        d.accept(0)
        d.accept(5)
        d.node_oldest(5)
        self.assertEqual(d.ack_point, 6)
        self.assertEqual(d.skipped, 4)

    def test_a_node_with_huge_sequence_numbers_costs_nothing(self):
        """After months of use a node's numbers are in the millions. Its first heartbeat must not
        be counted as a loss, and must not take time proportional to the number."""
        d = NodeDelivery()
        d.node_oldest(3_000_000_000)
        self.assertEqual((d.ack_point, d.skipped), (3_000_000_000, 0))
        self.assertTrue(d.accept(3_000_000_000))
        self.assertTrue(d.accept(3_000_000_002))
        d.node_oldest(3_000_001_000)                 # the node rebooted: a gap of unused numbers
        self.assertEqual(d.ack_point, 3_000_001_000)
        self.assertEqual(d.skipped, 998)             # 999 numbers passed, one of them already held

    def test_deferred_acks_wait_until_the_find_is_dealt_with(self):
        """WDG acks a find only after it is written (or counted as dropped), so a crash between
        receiving and writing can't lose it: the node still holds it."""
        tracker = DeliveryTracker(defer=True)
        d = tracker.node("a0b1c2d3e4f5")
        for seq in (0, 1, 2):
            self.assertTrue(d.accept(seq))
        self.assertEqual(d.ack_point, 3)                 # all received...
        self.assertEqual(d.ack_value(), 0)               # ...none written yet
        self.assertEqual(d.ack_due(0.0, now=1.0), 0)     # the node is told: keep everything
        d.processed(1)
        self.assertEqual(d.ack_value(), 0)               # 0 is still pending: nothing below it may go
        d.processed(0)
        self.assertEqual(d.ack_value(), 2)
        self.assertEqual(d.ack_due(0.0, now=2.0), 2)
        d.processed(2)
        self.assertEqual(d.ack_due(0.0, now=3.0), 3)
        self.assertEqual(tracker.snapshot(), {"a0b1c2d3e4f5": 3})
        again = DeliveryTracker(defer=True)              # after a core restart
        again.restore("a0b1c2d3e4f5", 3)
        self.assertFalse(again.node("a0b1c2d3e4f5").accept(2))     # offered again by the node: a repeat
        self.assertTrue(again.node("a0b1c2d3e4f5").accept(3))

    def test_ack_rate_limit(self):
        d = NodeDelivery()
        d.accept(0)
        self.assertEqual(d.ack_due(0.5, now=10.0), 1)
        d.accept(1)
        self.assertIsNone(d.ack_due(0.5, now=10.2))
        self.assertEqual(d.ack_due(0.5, now=10.6), 2)
        self.assertIsNone(d.ack_due(0.5, now=11.5))


class GpsPlacement(unittest.TestCase):
    def test_interpolate_nearest_and_reject(self):
        g = GpsTrack(max_gap_s=10, max_nearest_s=5)
        self.assertFalse(g.add(Fix(100, 0.0, 0.0)))
        self.assertTrue(g.add(Fix(100, 47.0, -117.0)))
        self.assertTrue(g.add(Fix(102, 47.002, -117.0)))
        self.assertFalse(g.add(Fix(102, 47.5, -117.0)))           # duplicate time
        mid = g.place(101)
        self.assertAlmostEqual(mid.lat, 47.001)
        self.assertEqual(g.place(105).t, 102)                    # nearest within 5 s
        self.assertIsNone(g.place(200))                          # too far from any fix
        self.assertFalse(valid(float("nan"), 1))


class SessionHandshake(unittest.TestCase):
    """Feeds hand-written HELLO lines into a session and checks its replies (no fake device)."""

    def make(self, link=proto.LINK_USB):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = Registry(pathlib.Path(self.tmp.name) / "n.json")
        self.sent, self.events, self.finds = [], [], []
        s = S.NodeSession(send=self.sent.append, link_type=link, registry=self.reg,
                          delivery=DeliveryTracker(), owner_key=KEY,
                          on_find=self.finds.append, on_event=lambda k, s, i: self.events.append(k))
        s.start()
        return s

    def tearDown(self):
        self.tmp.cleanup()

    def hello(self, s, hw="a0b1c2d3e4f5", owned=True, key=KEY):
        f = {"v": 1, "hw": hw, "bd": "xiao_c5", "b": 1, "cap": proto.HB_CAP_5G | proto.HB_CAP_BLE,
             "nn": "ffeeddccbbaa99887766554433221100"}
        if owned:
            f["np"] = crypto.node_proof(key, s._core_nonce, bytes.fromhex(hw))
        s.feed_line(codec.build("HELLO", f))

    def last(self, t):
        return [codec.parse(x) for x in self.sent if codec.parse(x).type == t]

    def test_unowned_usb_node_is_offered_not_adopted(self):
        s = self.make()
        self.hello(s, owned=False)
        self.assertEqual(s.state, S.NEW)
        self.assertIn("new_node", self.events)
        self.assertEqual(self.reg.nodes, {})
        self.assertFalse(self.last("ADOPT"))
        n = s.adopt()
        self.assertEqual(n, 1)
        self.assertEqual(self.last("ADOPT")[0].get("k"), KEY.hex())

    def test_wireless_adoption_refused(self):
        s = self.make(link=proto.LINK_ESPNOW)
        self.hello(s, owned=False)
        with self.assertRaises(PermissionError):
            s.adopt()

    def test_owned_known_node_authenticates(self):
        s = self.make()
        self.reg.adopt("a0b1c2d3e4f5")
        self.hello(s)
        self.assertEqual(s.state, S.AUTHED)
        cp = self.last("AUTH")[0].get("cp")
        self.assertEqual(cp, crypto.core_proof(KEY, bytes.fromhex("ffeeddccbbaa99887766554433221100"),
                                               bytes.fromhex("a0b1c2d3e4f5")))
        self.assertTrue(self.last("TIME"))

    def test_other_owner_ignored(self):
        s = self.make()
        self.reg.adopt("a0b1c2d3e4f5")
        self.hello(s, key=bytes(16))
        self.assertEqual(s.state, S.NOT_OURS)
        self.assertFalse(self.last("AUTH"))

    def test_ours_but_forgotten_needs_a_press(self):
        s = self.make()
        self.hello(s)
        self.assertEqual(s.state, S.FORGOTTEN)
        self.assertEqual(self.reg.nodes, {})

    def test_blocked_device_ignored(self):
        s = self.make()
        self.reg.block("a0b1c2d3e4f5", "biscuit")
        self.hello(s, owned=False)
        self.assertEqual(s.state, S.BLOCKED)
        s.feed_line(codec.build("AP", {"q": 0, "t": 1, "s": 1, "b": "a42bb0123456", "c": 6, "r": -50, "k": 3}))
        self.assertFalse(self.finds)

    def test_finds_only_after_auth_and_once(self):
        s = self.make()
        ap = codec.build("AP", {"q": 0, "t": 1700000000, "s": 1, "b": "a42bb0123456", "c": 6, "r": -50, "k": 3})
        s.feed_line(ap)
        self.assertFalse(self.finds)              # not authenticated yet
        self.reg.adopt("a0b1c2d3e4f5")
        self.hello(s)
        s.feed_line(ap)
        s.feed_line(ap)
        self.assertEqual(len(self.finds), 1)
        self.assertEqual(self.finds[0].fields["b"], "a42bb0123456")


class Ticker:
    """A clock the test moves by hand (the session takes its clock as a parameter)."""

    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t


class SessionKeepingInStep(unittest.TestCase):
    """Timeouts, keepalive and re-handshake, driven by a hand-moved clock and hand-written lines."""

    HW = "a0b1c2d3e4f5"

    def make(self, adopted=True):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "n.json"
        self.reg = Registry(self.path)
        if adopted:
            self.reg.adopt(self.HW)
        self.now = Ticker()
        self.sent, self.events = [], []
        s = S.NodeSession(send=self.sent.append, link_type=proto.LINK_USB, registry=self.reg,
                          delivery=DeliveryTracker(), owner_key=KEY, mono=self.now,
                          on_event=lambda k, s, i: self.events.append(k))
        s.start()
        return s

    def tearDown(self):
        if getattr(self, "tmp", None):
            self.tmp.cleanup()

    def count(self, t):
        return sum(1 for x in self.sent if codec.parse(x).type == t)

    def hello(self, s):
        s.feed_line(codec.build("HELLO", {
            "v": 1, "hw": self.HW, "bd": "xiao_c5", "b": 2, "cap": proto.HB_CAP_5G | proto.HB_CAP_BLE,
            "nn": "ffeeddccbbaa99887766554433221100",
            "np": crypto.node_proof(KEY, s._core_nonce, bytes.fromhex(self.HW))}))

    def heartbeat(self, s, flags=proto.HB_OWNED | proto.HB_AUTHED | proto.HB_CLOCK):
        s.feed_line(codec.build("HB", {"n": 1, "f": flags, "ph": "", "m": 0, "s": 0, "lk": 1, "qb": 0, "qd": 0,
                                       "qc": 2800, "qf": 1, "qn": 0, "qo": 0, "b": 2}))

    def test_three_hellos_then_silence_for_good(self):
        s = self.make()
        wait = proto.USB_HELLO_WAIT_MS / 1000 + 0.1
        for _ in range(proto.USB_HELLO_TRIES - 1):
            self.now.t += wait
            s.tick()
        self.assertEqual(self.count("HI"), proto.USB_HELLO_TRIES)
        self.assertEqual(s.state, S.HELLO_WAIT)
        self.now.t += wait
        s.tick()
        self.assertEqual(s.state, S.NO_ANSWER)
        self.assertIn("no_answer", self.events)
        before = len(self.sent)
        for _ in range(50):                    # a long time later: still nothing sent
            self.now.t += 60
            s.tick()
        self.assertEqual(len(self.sent), before)

    def test_a_late_hello_is_still_accepted(self):
        s = self.make()
        for _ in range(proto.USB_HELLO_TRIES):
            self.now.t += proto.USB_HELLO_WAIT_MS / 1000 + 0.1
            s.tick()
        self.assertEqual(s.state, S.NO_ANSWER)
        self.hello(s)
        self.assertEqual(s.state, S.AUTHED)

    def test_idle_link_is_pinged_and_clock_is_resent(self):
        s = self.make()
        self.hello(s)
        s.tick()                               # the first ack goes out
        self.assertEqual(self.count("TIME"), 1)
        self.assertEqual(self.count("PING"), 0)
        self.now.t += proto.CORE_PING_MS / 1000 + 0.1
        self.heartbeat(s)
        s.tick()
        self.assertEqual(self.count("PING"), 1)
        s.tick()                               # the ping itself counts as traffic: no second one yet
        self.assertEqual(self.count("PING"), 1)
        self.now.t += proto.TIME_RESYNC_MS / 1000
        self.heartbeat(s)
        s.tick()
        self.assertEqual(self.count("TIME"), 2)

    def test_node_without_clock_gets_the_time_again(self):
        s = self.make()
        self.hello(s)
        self.now.t += 3
        self.heartbeat(s, flags=proto.HB_OWNED | proto.HB_AUTHED)      # no CLOCK flag
        self.assertEqual(self.count("TIME"), 2)

    def test_silent_node_gets_a_new_handshake(self):
        s = self.make()
        self.hello(s)
        self.now.t += 3 * proto.HEARTBEAT_MS / 1000 + 0.1
        s.tick()
        self.assertIn("silent", self.events)
        self.assertEqual(s.state, S.HELLO_WAIT)
        self.assertEqual(self.count("HI"), 2)

    def test_node_that_forgot_us_gets_a_new_handshake(self):
        s = self.make()
        self.hello(s)
        s.feed_line(codec.build("RSP", {"c": "RUN", "ok": 0, "e": proto.ERRORS["NOT_AUTHED"]}))
        self.assertIn("reauth", self.events)
        self.assertEqual(s.state, S.HELLO_WAIT)
        self.assertEqual(self.count("HI"), 2)

    def test_usb_device_is_remembered_with_the_node(self):
        s = self.make()
        s.usb_key = "usb:1a86:55d3:5a47012345"
        self.hello(s)
        self.assertEqual(Registry(self.path).known(self.HW).usb_key, "usb:1a86:55d3:5a47012345")

    def test_start_reaches_the_node_at_once(self):
        from bristle.fleet import Fleet
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        reg = Registry(pathlib.Path(tmp.name) / "n.json")
        reg.adopt(self.HW, cap_5g=True)
        sent = []
        fleet = Fleet(reg, KEY)
        s = fleet.new_session(sent.append, proto.LINK_USB)
        s.start()
        self.hello(s)
        fleet.start()
        runs = [codec.parse(x) for x in sent if codec.parse(x).type == "RUN"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].get("m"), str(proto.RUN_WIFI | proto.RUN_BLE))
        self.assertTrue([x for x in sent if codec.parse(x).type == "PLAN"])


class UsbPortRules(unittest.TestCase):
    """Which serial ports may be spoken to. Rows of port facts in, a decision out: nothing is opened."""

    def setUp(self):
        from bristle import link_usb as U
        self.U = U
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "n.json"
        self.reg = Registry(self.path)
        self.new_xiao = U.PortInfo("/dev/ttyACM1", 0x303A, 0x1001, "A0:B1:C2:D3:E4:F5", "USB JTAG/serial")
        self.janos = U.PortInfo("/dev/ttyACM0", 0x303A, 0x1001, "AA:AA:AA:AA:AA:01", "USB JTAG/serial")
        self.gps = U.PortInfo("/dev/ttyACM2", 0x1546, 0x01A7, None, "u-blox 7")
        self.ch343 = U.PortInfo("/dev/ttyACM3", 0x1A86, 0x55D3, "5A47012345", "USB Single Serial")
        self.ch340 = U.PortInfo("/dev/ttyUSB0", 0x1A86, 0x7523, None, "USB Serial")

    def test_a_gps_dongle_is_never_a_candidate(self):
        for auto in (False, True):
            p = self.U.PortPolicy(self.reg, auto=auto)
            self.assertEqual(p.decide(self.gps)[0], self.U.SKIP)

    def test_a_new_esp32_port_is_listed_not_touched(self):
        p = self.U.PortPolicy(self.reg)
        for port in (self.new_xiao, self.janos, self.ch343, self.ch340):
            self.assertEqual(p.decide(port)[0], self.U.ASK, port.device)
        p.approve(self.new_xiao.key)
        self.assertEqual(p.decide(self.new_xiao)[0], self.U.PROBE)
        self.assertEqual(p.decide(self.janos)[0], self.U.ASK)       # same USB id, different device

    def test_not_a_node_beats_everything(self):
        self.reg.mark_not_node(self.janos.key, "JanOS")
        for auto in (False, True):
            p = self.U.PortPolicy(Registry(self.path), auto=auto)   # also: it survives a restart
            p.approve(self.janos.key)
            self.assertEqual(p.decide(self.janos), (self.U.SKIP, "marked not a node"))
        self.reg.unmark_not_node(self.janos.key)
        self.assertEqual(self.U.PortPolicy(Registry(self.path)).decide(self.janos)[0], self.U.ASK)

    def test_exclude_list_by_port_or_by_device(self):
        p = self.U.PortPolicy(self.reg, exclude=["/dev/ttyACM0", self.ch343.key], auto=True)
        self.assertEqual(p.decide(self.janos)[0], self.U.SKIP)
        self.assertEqual(p.decide(self.ch343)[0], self.U.SKIP)
        self.assertEqual(p.decide(self.new_xiao)[0], self.U.PROBE)

    def test_my_own_nodes_are_recognised_without_asking(self):
        self.reg.adopt("a0b1c2d3e4f5")                               # native USB: serial number is the hwid
        self.reg.adopt("0c0c0c0c0c0c")
        self.reg.touch("0c0c0c0c0c0c", usb_key=self.ch343.key)       # CH343: remembered at adoption
        p = self.U.PortPolicy(self.reg)
        self.assertEqual(p.decide(self.new_xiao), (self.U.PROBE, "one of your nodes"))
        self.assertEqual(p.decide(self.ch343), (self.U.PROBE, "one of your nodes"))
        self.assertEqual(p.decide(self.janos)[0], self.U.ASK)
        p.went_quiet(self.new_xiao.key)                              # mid-flash, say: keep trying, it's ours
        self.assertEqual(p.decide(self.new_xiao)[0], self.U.PROBE)

    def test_no_answer_means_left_alone_until_replugged(self):
        p = self.U.PortPolicy(self.reg, auto=True)
        self.assertEqual(p.decide(self.janos)[0], self.U.PROBE)
        p.went_quiet(self.janos.key)
        self.assertEqual(p.decide(self.janos)[0], self.U.SKIP)
        p.unplugged({self.janos.key})                                # still plugged in: still left alone
        self.assertEqual(p.decide(self.janos)[0], self.U.SKIP)
        p.unplugged(set())                                           # unplugged
        self.assertEqual(p.decide(self.janos)[0], self.U.PROBE)

    def test_a_node_cannot_be_marked_not_a_node(self):
        self.reg.adopt("0c0c0c0c0c0c")
        self.reg.touch("0c0c0c0c0c0c", usb_key=self.ch343.key)
        with self.assertRaises(PermissionError):
            self.reg.mark_not_node(self.ch343.key)

    def test_scan_opens_only_what_the_rules_allow(self):
        opened, shown = [], []
        self.reg.adopt("a0b1c2d3e4f5")
        self.reg.mark_not_node(self.janos.key)
        busy = {self.ch343.device}
        policy = self.U.PortPolicy(self.reg)
        policy.approve(self.ch343.key)
        sc = self.U.UsbScanner(opened.append, policy, on_port=lambda p, a, r: shown.append((p.device, a, r)),
                               ports=lambda: [self.new_xiao, self.janos, self.gps, self.ch343, self.ch340],
                               in_use=lambda d: d in busy)
        sc.scan_once()
        self.assertEqual([p.device for p in opened], ["/dev/ttyACM1"])
        self.assertIn(("/dev/ttyACM0", self.U.SKIP, "marked not a node"), shown)
        self.assertIn(("/dev/ttyACM3", self.U.SKIP, "in use by another program"), shown)
        self.assertIn(("/dev/ttyUSB0", self.U.ASK, "new port: not spoken to yet"), shown)
        self.assertFalse([x for x in shown if x[0] == "/dev/ttyACM2"])
        sc.scan_once()                                               # nothing changed: nothing reopened
        self.assertEqual(len(opened), 1)
        busy.clear()
        sc.scan_once()                                               # the other program let go of the CH343
        self.assertEqual([p.device for p in opened], ["/dev/ttyACM1", "/dev/ttyACM3"])

    def test_usb_serial_number_as_hardware_id(self):
        self.assertEqual(self.U.serial_as_hwid("A0:B1:C2:D3:E4:F5"), "a0b1c2d3e4f5")
        self.assertIsNone(self.U.serial_as_hwid("5A47012345"))
        self.assertIsNone(self.U.serial_as_hwid(None))
        self.assertEqual(self.ch340.key, "dev:/dev/ttyUSB0")


if __name__ == "__main__":
    unittest.main()
