# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""[bristle] WDG-Bristle 1.2.1: unit tests for what the Bristle simulations found (tools/sim,
feature "bristle": a scripted node and bridge speaking the real protocol; stand-ins, not hardware).

Three faults on the game's side, each pinned here without the simulation harness:
  1. An ACK that never reached the node was never said again. A quiet node went on offering the
     same finds every 10 s; a node that restarted just before the ACK arrived was never
     acknowledged again at all, so its queue could only fill.
  2. A node's late answer to an earlier hello was taken for a board with another owner.
  3. A node that restarted made the terminal say "refused ACK: not authenticated" (and one such
     line for every other command on its way) instead of what had happened.

The same three are tested in the core library's own suite (tests/test_bristle_core.py is its
copy). The fourth finding is in the node firmware (build 5) and has no test a PC can run: see
bristle/docs/BACKTRACK.md.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugins._bristle import codec, crypto, proto, service  # noqa: E402
from plugins._bristle import session as S  # noqa: E402

HW = "02b1c5000001"                      # a made-up, locally administered address


class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


class Rig(unittest.TestCase):
    """The real service in a scratch folder, no threads, one USB session fed by hand."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        folder = Path(self._tmp.name)
        self.finds = []
        with patch.object(service, "bench_dirs", return_value=[]):
            self.service = service.BristleService(folder, folder / "loot", self.finds.append)
        self.key = self.service.fleet.owner_key
        self.service.registry.adopt(HW)
        self.now = Clock()
        self.sent = []
        self.session = self.service.fleet.new_session(self.sent.append, proto.LINK_USB)
        self.session.mono = self.now
        self.session.start()

    def messages(self):
        return [message for message, _kind in self.service.messages]

    def lines(self, kind):
        return [codec.parse(line) for line in self.sent if codec.parse(line).type == kind]

    def hello(self, nonce=None, nn="11" * 16):
        nonce = self.session._core_nonce if nonce is None else nonce
        self.session.feed_line(codec.build("HELLO", {
            "v": 1, "hw": HW, "bd": "xiao_c5", "b": 5, "cap": proto.HB_CAP_5G | proto.HB_CAP_BLE, "nn": nn,
            "np": crypto.node_proof(self.key, nonce, bytes.fromhex(HW))}))

    def heartbeat(self, oldest, next_seq, backlog):
        self.session.feed_line(codec.build("HB", {
            "n": 1, "f": proto.HB_OWNED | proto.HB_AUTHED | proto.HB_CLOCK, "ph": "", "m": 0, "s": 0, "lk": 1,
            "qb": backlog, "qd": 0, "qc": 2944, "qf": 0, "qn": next_seq, "qo": oldest, "b": 5}))

    def find(self, seq):
        self.session.feed_line(codec.build("AP", {"q": seq, "t": 1_795_400_000, "ms": 0, "s": 1,
                                                  "b": "02bb%08x" % seq, "c": 6, "r": -50, "k": 3, "e": "DEMO"}))

    def written(self):
        """The game writes a find and then says so; only then may the node forget it."""
        delivery = self.service.fleet.delivery.node(HW)
        for find in self.finds:
            delivery.processed(find.seq)

    def tick(self, seconds=1.0):
        self.now.t += seconds
        self.session.tick()

    def acks(self):
        return [int(message.get("q")) for message in self.lines("ACK")]


class LostAck(Rig):
    def test_an_ack_the_node_never_got_is_said_again(self):
        self.hello()
        self.heartbeat(oldest=1000, next_seq=1000, backlog=0)
        for seq in range(1000, 1005):
            self.find(seq)
        self.written()
        self.tick()
        self.assertEqual(self.acks(), [1005])                  # ... and that line is lost on the way
        for seq in range(1000, 1005):                          # after 10 s the node offers the same finds again
            self.find(seq)
        self.tick()
        self.assertEqual(len(self.finds), 5)                   # repeats are dropped, as before
        self.assertEqual(self.acks(), [1005])                  # nothing new, so nothing was said: the fault
        self.heartbeat(oldest=1000, next_seq=1005, backlog=5)  # its heartbeat: it still holds them
        self.tick()
        self.assertEqual(self.acks(), [1005, 1005])            # 1.2.1: told again
        for _ in range(5):                                     # now it has let them go
            self.heartbeat(oldest=1005, next_seq=1005, backlog=0)
            self.tick(2)
        self.assertEqual(self.acks(), [1005, 1005])            # and the core is quiet again
        self.assertEqual(self.session.state, S.AUTHED)

    def test_a_node_that_restarted_under_the_ack_is_acknowledged_again(self):
        self.hello()
        self.heartbeat(oldest=1000, next_seq=1000, backlog=0)
        for seq in range(1000, 1030):
            self.find(seq)
        self.written()
        self.tick()
        self.assertEqual(self.acks(), [1030])
        # the node had restarted: it answers "not authenticated" and the core shakes hands again
        self.session.feed_line(codec.build("RSP", {"c": "ACK", "ok": 0, "e": proto.ERRORS["NOT_AUTHED"]}))
        self.assertEqual(self.session.state, S.HELLO_WAIT)
        self.hello(nn="22" * 16)
        self.assertEqual(self.session.state, S.AUTHED)
        for seq in range(2000, 2010):                          # what it heard since: numbers from a fresh block
            self.find(seq)
        self.written()
        self.tick()
        self.assertEqual(self.acks(), [1030, 1030])            # 1.2.1: the ack point is said again
        self.heartbeat(oldest=2000, next_seq=2010, backlog=10)  # it dropped 1000..1029 and reports 2000
        self.tick()
        self.assertEqual(self.acks(), [1030, 1030, 2010])      # and what came after the restart is acknowledged
        self.assertEqual(len(self.finds), 40)

    def test_what_the_player_reads_when_a_node_restarts(self):
        self.hello()
        self.session.feed_line(codec.build("RSP", {"c": "ACK", "ok": 0, "e": proto.ERRORS["NOT_AUTHED"]}))
        self.session.feed_line(codec.build("RSP", {"c": "PING", "ok": 0, "e": proto.ERRORS["NOT_AUTHED"]}))
        said = self.messages()
        self.assertEqual([m for m in said if "restarted or dropped the link: connecting again" in m],
                         ["[BRISTLE] Node 1 restarted or dropped the link: connecting again"])
        self.assertFalse([m for m in said if "refused" in m], said)

    def test_a_refusal_for_any_other_reason_is_still_reported(self):
        self.hello()
        self.session.feed_line(codec.build("RSP", {"c": "PLAN", "ok": 0, "e": proto.ERRORS["BAD_PLAN"]}))
        self.assertIn("[BRISTLE] Node 1 refused PLAN: %s" % proto.ERRORS["BAD_PLAN"], self.messages())


class LateHello(Rig):
    def test_a_slow_node_is_not_announced_as_another_owners(self):
        first = self.session._core_nonce
        self.now.t += proto.USB_HELLO_WAIT_MS / 1000 + 0.1
        self.session.tick()                                    # no answer in time: a second hello
        self.assertEqual(len(self.lines("HI")), 2)
        self.hello(nonce=first)                                # the answer to the first one arrives now
        self.assertEqual(self.session.state, S.HELLO_WAIT)
        self.assertFalse([m for m in self.messages() if "another owner" in m])
        self.hello(nn="33" * 16)                               # the answer to the second
        self.assertEqual(self.session.state, S.AUTHED)
        self.assertEqual(len([m for m in self.messages() if "Node 1 online over USB" in m]), 1)
        self.assertEqual(len(self.lines("AUTH")), 1)
        self.assertEqual(self.lines("AUTH")[0].get("cp"), crypto.core_proof(self.key, bytes.fromhex("33" * 16),
                                                                            bytes.fromhex(HW)))

    def test_a_board_with_another_owner_is_still_left_alone(self):
        self.session.feed_line(codec.build("HELLO", {
            "v": 1, "hw": HW, "bd": "xiao_c5", "b": 5, "cap": 0, "nn": "44" * 16,
            "np": crypto.node_proof(bytes(range(16)), self.session._core_nonce, bytes.fromhex(HW))}))
        self.assertEqual(self.session.state, S.NOT_OURS)
        self.assertTrue([m for m in self.messages() if "has another owner: ignored" in m])
        self.assertEqual(len(self.lines("AUTH")), 0)


if __name__ == "__main__":
    unittest.main()
