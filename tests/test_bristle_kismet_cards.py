# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""[bristle] WDG-Bristle 1.2: the Kismet channel split for any number of cards, and the per-card
channel lock (plugins/_kismet_extras.py, plugins/kismet_wardrive.py).

Pure logic, plus the picker driven through the measuring Canvas and a stand-in Kismet engine.
Nothing here starts Kismet or touches an adapter: what a real card and a real Kismet do with a lock
is a bench item (bristle/docs/BENCH_TESTS.md).
"""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from plugins import _kismet_extras as kx
from plugins._bristle import plan as node_plan
import plugins.kismet_wardrive as kismet

import test_v3_ng_plugins as ng

CH_24 = [str(n) for n in range(1, 14)]
CH_5LO = [str(n) for n in range(36, 65, 4)]
CH_5HI = [str(n) for n in range(100, 145, 4)] + [str(n) for n in range(149, 166, 4)]
CH_5 = CH_5LO + CH_5HI
CH_6 = ["%dW6e" % n for n in range(1, 94, 4)]
TRI, DUAL = CH_24 + CH_5 + CH_6, CH_24 + CH_5

IW_INFO = """
Wiphy phy1
\tBand 1:
\t\tFrequencies:
\t\t\t* 2412 MHz [1] (20.0 dBm)
\t\t\t* 2437 MHz [6] (20.0 dBm)
\t\t\t* 2462 MHz [11] (20.0 dBm)
\t\t\t* 2484 MHz [14] (disabled)
\tBand 2:
\t\tFrequencies:
\t\t\t* 5180 MHz [36] (20.0 dBm)
\t\t\t* 5260 MHz [52] (20.0 dBm) (no IR, radar detection)
\t\t\t* 5745 MHz [149] (20.0 dBm)
\t\t\t* 5885 MHz [177] (disabled)
\tBand 4:
\t\tFrequencies:
\t\t\t* 5955 MHz [1] (12.0 dBm) (no IR)
\t\t\t* 5975.0 MHz [5] (12.0 dBm) (no IR)
\tSupported interface modes:
\t\t * managed
\t\t * monitor
"""


def source(name, channels, running=True, kind="wifi", hop=None, **more):
    row = dict(uuid="uuid-" + name, name=name, interface=name, kind=kind, running=running, error=not running,
               reason="", channels=list(channels), hop_channels=list(channels if hop is None else hop), hop_rate=5.0)
    row.update(more)
    return row


def check_rules(case, sources, plan, locks=None):
    """The safety rules, for any plan: only channels the card offered, nothing that was hopped is
    dropped, no card is left with nothing."""
    locks = locks or {}
    hopping = [s for s in sources if s["kind"] == "wifi" and s["running"] and s["uuid"] not in locks]
    before = set()
    for s in hopping:
        before |= set(s["hop_channels"])
    before = {c for c in before if not any(kx.same_channel(c, lock) for lock in locks.values())}
    after = set()
    for s in hopping:
        mine = plan.get(s["uuid"], s["hop_channels"])
        case.assertTrue(mine, s["name"])                                  # never an empty list
        case.assertLessEqual(set(mine), set(s["hop_channels"]), s["name"])  # nothing it never offered
        case.assertEqual(len(mine), len(set(mine)), s["name"])             # nothing twice
        after |= set(mine)
    case.assertGreaterEqual(after, before)                                 # nothing dropped
    for uuid in locks:
        case.assertNotIn(uuid, plan)                                       # a locked card is not in the split


class Names(unittest.TestCase):
    def test_segments(self):
        cases = {"1": "2.4", "13HT40-": "2.4", "36": "5lo", "64": "5lo", "100": "5hi", "165": "5hi", "177": "5hi",
                 "37W6e": "6", "weird": None, "": None}
        for name, segment in cases.items():
            with self.subTest(name=name):
                self.assertEqual(kx.channel_segment(name), segment)

    def test_same_channel_and_the_name_to_send(self):
        self.assertTrue(kx.same_channel("6", "6HT40+"))
        self.assertFalse(kx.same_channel("1", "1W6e"))                      # 2.4 GHz 1 is not 6 GHz 1
        self.assertFalse(kx.same_channel("junk", "junk"))
        self.assertEqual(kx.lock_name("6", ["1", "6HT40+", "6", "11"]), "6")  # the plain name wins
        self.assertEqual(kx.lock_name("36", ["36VHT80", "40"]), "36VHT80")
        self.assertEqual(kx.lock_name("37w6e", ["37W6e"]), "37W6e")
        self.assertIsNone(kx.lock_name("149", CH_24))
        self.assertIsNone(kx.lock_name("", CH_24))

    def test_iw_output_becomes_kismet_channel_names(self):
        self.assertEqual(kx.parse_iw_channels(IW_INFO), ["1", "6", "11", "36", "52", "149", "1W6e", "5W6e"])
        for junk in ("", None, 7, "no frequencies here"):
            self.assertEqual(kx.parse_iw_channels(junk), [])

    def test_lock_choices(self):
        self.assertEqual(kx.lock_choices(["1", "6HT40+", "6", "36VHT80", "5W6e"]), ["1", "6", "36", "5W6e"])
        self.assertEqual(kx.lock_choices([]), kx.STANDARD_CHANNELS)           # a card whose list is not known
        self.assertIn("149", kx.STANDARD_CHANNELS)

    def test_describe(self):
        self.assertEqual(kx.describe_channels(CH_24), "2.4 GHz 1-13 (13 channels)")
        self.assertEqual(kx.describe_channels(CH_5LO + ["149"]), "5 GHz low 36-64 + 5 GHz high 149 (9 channels)")
        self.assertEqual(kx.describe_channels(["1"]), "2.4 GHz 1 (1 channel)")


class TwoCardsAreUnchanged(unittest.TestCase):
    def test_the_two_card_split_is_what_it_was(self):
        alfa, ac = source("alfa", TRI), source("ac1200", DUAL)
        plan, why = kx.plan_split([alfa, ac])
        self.assertEqual(plan, {ac["uuid"]: CH_24, alfa["uuid"]: CH_5 + CH_6})
        self.assertIn("(5/6 GHz)", why)
        self.assertEqual(kx.plan_split([ac, alfa])[0], plan)


class ManyCards(unittest.TestCase):
    def test_three_cards_2_4_then_5_low_then_5_high(self):
        cards = [source("a", DUAL), source("b", DUAL), source("c", DUAL)]
        plan, why = kx.plan_split(cards)
        self.assertEqual(plan, {"uuid-a": CH_24, "uuid-b": CH_5LO, "uuid-c": CH_5HI})
        self.assertIn("a: 2.4 GHz 1-13 (13 channels)", why)
        self.assertIn("b: 5 GHz low 36-64 (8 channels)", why)
        check_rules(self, cards, plan)

    def test_three_cards_keep_6_ghz_with_the_card_that_can_tune_it(self):
        cards = [source("alfa", TRI), source("b", DUAL), source("c", DUAL)]
        plan, _why = kx.plan_split(cards)
        check_rules(self, cards, plan)
        self.assertLessEqual(set(CH_6), set(plan["uuid-alfa"]))            # only it can tune 6 GHz
        # ... and it takes the shortest 5 GHz half next to it, not 2.4 GHz and not the long half
        self.assertEqual({kx.channel_segment(c) for c in plan["uuid-alfa"]}, {"5lo", "6"})
        self.assertEqual(plan["uuid-b"], CH_24)

    def test_four_cards_add_6_ghz_for_a_card_that_supports_it(self):
        cards = [source("b", DUAL), source("alfa", TRI), source("c", DUAL), source("d", DUAL)]
        plan, _why = kx.plan_split(cards)
        check_rules(self, cards, plan)
        self.assertEqual(plan["uuid-alfa"], CH_6)
        self.assertEqual(sorted(map(tuple, (plan["uuid-b"], plan["uuid-c"], plan["uuid-d"]))),
                         sorted(map(tuple, (CH_24, CH_5LO, CH_5HI))))

    def test_four_cards_without_6_ghz_the_fourth_shares_the_busiest_band(self):
        cards = [source(name, DUAL) for name in "abcd"]
        plan, _why = kx.plan_split(cards)
        check_rules(self, cards, plan)
        on_24 = [u for u, channels in plan.items() if {kx.channel_segment(c) for c in channels} == {"2.4"}]
        self.assertEqual(len(on_24), 2)                                    # 2.4 GHz is first in line
        self.assertEqual(sorted(plan[on_24[0]] + plan[on_24[1]], key=int), CH_24)
        self.assertFalse(set(plan[on_24[0]]) & set(plan[on_24[1]]))        # shared, not doubled

    def test_extra_cards_go_round_the_bands_one_each(self):
        cards = [source("alfa", TRI)] + [source("c%d" % i, DUAL) for i in range(6)]
        plan, _why = kx.plan_split(cards)
        check_rules(self, cards, plan)
        count = {}
        for channels in plan.values():
            for segment in {kx.channel_segment(c) for c in channels}:
                count[segment] = count.get(segment, 0) + 1
        self.assertEqual(count, {"2.4": 2, "5lo": 2, "5hi": 2, "6": 1})    # 7 cards: 4 bands, 3 extras

    def test_extra_cards_follow_the_counts_when_there_are_counts(self):
        cards = [source(name, DUAL) for name in "abcde"]
        plan, _why = kx.plan_split(cards, busy={"2.4": 10, "5lo": 400, "5hi": 5})
        check_rules(self, cards, plan)
        on_low = [u for u, channels in plan.items() if {kx.channel_segment(c) for c in channels} == {"5lo"}]
        self.assertEqual(len(on_low), 3)                                   # both extras went to the busy band

    def test_a_channel_only_one_card_can_tune_stays_with_it(self):
        cards = [source("a", CH_24 + ["14"]), source("b", DUAL + ["169"]), source("c", DUAL), source("d", CH_24)]
        plan, _why = kx.plan_split(cards)
        check_rules(self, cards, plan)
        self.assertIn("14", plan["uuid-a"])
        self.assertIn("169", plan["uuid-b"])

    def test_names_that_cannot_be_classified_are_kept(self):
        cards = [source("a", DUAL + ["weird"]), source("b", DUAL), source("c", DUAL + ["200"])]
        plan, _why = kx.plan_split(cards)
        check_rules(self, cards, plan)

    def test_single_band_cards_only(self):
        cards = [source(name, CH_24) for name in "abc"]
        plan, _why = kx.plan_split(cards)
        check_rules(self, cards, plan)
        self.assertEqual(sorted(sum(plan.values(), []), key=int), CH_24)   # 2.4 GHz shared three ways

    def test_the_rules_hold_for_every_mix_up_to_eight_cards(self):
        kinds = (CH_24, DUAL, TRI, CH_5, CH_24 + CH_5LO, CH_5HI + CH_6, CH_24 + ["1HT40+", "6HT40-"])
        for count in range(3, 9):
            for shift in range(len(kinds)):
                cards = [source("c%d" % i, kinds[(i * (shift + 1) + shift) % len(kinds)]) for i in range(count)]
                with self.subTest(count=count, shift=shift):
                    plan, why = kx.plan_split(cards)
                    self.assertTrue(plan, why)
                    check_rules(self, cards, plan)

    def test_unplugging_a_card_gives_its_channels_back(self):
        cards = [source("alfa", TRI), source("b", DUAL), source("c", DUAL), source("d", DUAL)]
        four, _why = kx.plan_split(cards)
        check_rules(self, cards, four)
        cards[2]["running"] = False                                        # c is unplugged
        three, _why = kx.plan_split(cards)
        check_rules(self, cards, three)
        self.assertNotIn("uuid-c", three)
        cards[1]["running"] = cards[3]["running"] = False                  # only the Alfa is left
        self.assertEqual(kx.plan_split(cards)[0], {})                      # back to all of its channels

    def test_a_card_whose_channels_are_not_known_stops_the_plan(self):
        plan, why = kx.plan_split([source("a", DUAL), source("b", []), source("c", DUAL)])
        self.assertEqual(plan, {})
        self.assertIn("not reported", why)


class Locks(unittest.TestCase):
    def test_a_locked_card_is_out_of_the_split_and_its_channel_out_of_the_lists(self):
        cards = [source("alfa", TRI), source("b", DUAL), source("c", DUAL), source("d", DUAL)]
        locks = {"uuid-d": "6"}
        plan, why = kx.plan_split(cards, locks)
        check_rules(self, cards, plan, locks)
        self.assertNotIn("6", sum(plan.values(), []))
        self.assertIn("d stays on channel 6", why)
        self.assertEqual({kx.channel_segment(c) for c in plan["uuid-alfa"]}, {"5lo", "6"})   # three cards now

    def test_wide_names_of_the_locked_channel_are_left_out_too(self):
        wide = CH_24 + ["6HT40+", "6HT40-"] + CH_5
        plan, _why = kx.plan_split([source("a", wide), source("b", wide), source("c", wide)], {"uuid-c": "6"})
        self.assertFalse([c for c in sum(plan.values(), []) if kx.same_channel(c, "6")])

    def test_one_hopping_card_next_to_a_locked_one_only_loses_that_channel(self):
        cards = [source("a", DUAL), source("b", DUAL)]
        plan, why = kx.plan_split(cards, {"uuid-b": "149"})
        self.assertEqual(plan, {"uuid-a": [c for c in DUAL if c != "149"]})
        self.assertIn("b stays on channel 149", why)

    def test_every_card_locked_means_nothing_to_plan(self):
        cards = [source("a", DUAL), source("b", DUAL)]
        self.assertEqual(kx.plan_split(cards, {"uuid-a": "1", "uuid-b": "36"})[0], {})

    def test_a_card_that_could_do_nothing_else_keeps_its_channel(self):
        plan, _why = kx.plan_split([source("a", ["6"]), source("b", DUAL), source("c", DUAL)], {"uuid-c": "6"})
        self.assertEqual(plan.get("uuid-a", ["6"]), ["6"])

    def test_locks_are_sorted_by_what_the_cards_can_do(self):
        cards = [source("a", DUAL), source("b", CH_24), source("c", [])]
        ok, refused, unknown = kx.resolve_locks(cards, {"uuid-a": "36", "uuid-b": "36", "uuid-c": "11", "gone": "1"})
        self.assertEqual((ok, refused, unknown), ({"uuid-a": "36"}, {"uuid-b": "36"}, {"uuid-c": "11"}))

    def test_the_picker_says_what_each_card_will_do(self):
        rows = dict(kx.picker_plan([("wlan1", "wlan1", TRI), ("wlan2", "wlan2", CH_24), ("wlan3", "wlan3", [])],
                                   {"wlan2": "36", "wlan3": "11"}))
        self.assertIn("cannot tune channel 36: it will hop instead", rows["wlan2"])
        self.assertIn("2.4 GHz", rows["wlan2"])                            # ... and it is planned as a hopping card
        self.assertEqual(rows["wlan3"], "locked on channel 11 (checked when Kismet starts)")
        self.assertIn("6 GHz", rows["wlan1"])
        off = dict(kx.picker_plan([("wlan1", "wlan1", TRI), ("wlan2", "wlan2", DUAL)], {"wlan2": "6"}, split_on=False))
        self.assertEqual(off, {"wlan1": "hops all its channels", "wlan2": "locked on channel 6"})
        waiting = dict(kx.picker_plan([("wlan1", "wlan1", TRI), ("wlan2", "wlan2", [])], {}))
        self.assertIn("worked out when Kismet starts", waiting["wlan2"])

    def test_kismet_says_whether_a_source_hops_and_where_it_is(self):
        rows = kx.parse_sources([
            {"kismet.datasource.uuid": "U1", "kismet.datasource.interface": "wlan1", "kismet.datasource.running": 1,
             "kismet.datasource.channels": ["1", "6"], "kismet.datasource.hopping": 0, "kismet.datasource.channel": "6"},
            {"kismet.datasource.uuid": "U2", "kismet.datasource.interface": "wlan2", "kismet.datasource.running": 1,
             "kismet.datasource.channels": ["1", "6"], "kismet.datasource.hopping": True},
            {"kismet.datasource.uuid": "U3", "kismet.datasource.interface": "wlan3",
             "kismet.datasource.channels": ["1"]}])
        self.assertEqual([(r["hopping"], r["channel"]) for r in rows], [(False, "6"), (True, ""), (None, "")])


class NodePlanner(unittest.TestCase):
    """The Bristle node planner treats a locked channel as covered by Kismet."""

    def test_a_locked_card_makes_its_channel_heavy_for_the_nodes(self):
        hopping = source("wlan1", DUAL)
        heavy, note = node_plan.kismet_heavy_channels([hopping, source("wlan2", DUAL, lock="6")])
        self.assertEqual(heavy, {6})
        self.assertIn("6", note)
        self.assertEqual(node_plan.kismet_heavy_channels([hopping, source("wlan2", DUAL, running=False, lock="6")])[0],
                         set())                                            # unplugged: not covered any more

    def test_the_nodes_put_the_locked_channel_last_with_the_shortest_dwell(self):
        from plugins._bristle import proto
        nodes = [node_plan.NodeCaps(number=1, cap_5g=True, cap_ble=True), node_plan.NodeCaps(number=2, cap_5g=True, cap_ble=True),
                 node_plan.NodeCaps(number=3, cap_5g=True, cap_ble=True)]
        plans = node_plan.split(nodes, "U", kismet_heavy={6})
        seen = False
        for plan in plans.values():
            for index, (channel, dwell) in enumerate(plan.entries):
                if channel == 6:
                    seen = True
                    self.assertEqual(dwell, proto.DWELL_MIN_MS)
                    self.assertEqual(index, len(plan.entries) - 1)
        self.assertTrue(seen)                                              # still visited: nothing is dropped


class FakeEngine:
    """What the plugin asks of the Kismet engine, recorded."""

    def __init__(self, refuse=()):
        self.hops, self.locks, self.refuse, self.locked = [], [], set(refuse), {}

    def set_hop_channels(self, uuid, channels, rate=5.0):
        self.hops.append((uuid, list(channels)))
        return True, None

    def set_channel(self, uuid, channel):
        if uuid in self.refuse:
            return False, "HTTP 500"
        self.locks.append((uuid, channel))
        return True, None


class Plugin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugin = kismet.KismetWardriveBridge()
        self.plugin.app = SimpleNamespace(_esc_consumed_frame=-1, _app_dir=self.tmp.name)
        self.plugin.term, self.plugin.msg = Mock(), Mock()
        self.plugin._wifi_all = [dict(id="wlan1", mac="02:00:00:00:00:01", guess="tri-band card", can_monitor=True, note=""),
                                 dict(id="wlan2", mac="02:00:00:00:00:02", guess="dual-band card", can_monitor=True, note=""),
                                 dict(id="wlan3", mac="02:00:00:00:00:03", guess="dual-band card", can_monitor=True, note="")]
        self.plugin._ble_all = []
        self.plugin._wifi_selected, self.plugin._ble_selected = {"wlan1", "wlan2", "wlan3"}, set()
        self.plugin._wifi_monitor = {"wlan1": True, "wlan2": True, "wlan3": True}
        self.plugin._wifi_channels = {"wlan1": TRI, "wlan2": DUAL, "wlan3": DUAL}
        self.plugin._menu_sel = 2
        self.plugin._overlay_active = True
        self.plugin._split_mode = "band"

    def press(self, canvas, *keys):
        canvas.pressed = set(keys)
        self.plugin._picker_on_update()
        canvas.pressed = set()

    def test_left_right_step_the_lock_and_l_clears_it(self):
        canvas = ng.Canvas()
        with ng.with_pyxel(canvas):
            self.press(canvas, "KEY_RIGHT")
            self.assertEqual(self.plugin._wifi_lock, {"wlan3": "1"})        # the card's first channel
            self.press(canvas, "KEY_LEFT")
            self.assertEqual(self.plugin._wifi_lock, {})                    # back to hopping
            self.press(canvas, "KEY_LEFT")
            self.assertEqual(self.plugin._wifi_lock, {"wlan3": "165"})      # ... and round to its last
            self.press(canvas, "KEY_L")
            self.assertEqual(self.plugin._wifi_lock, {})
            self.plugin._menu_sel = 3                                       # the START row: no card, nothing happens
            self.press(canvas, "KEY_RIGHT")
            self.assertEqual(self.plugin._wifi_lock, {})

    def test_the_picker_shows_the_split_per_card_and_the_lock(self):
        canvas = ng.Canvas()
        self.plugin._wifi_lock = {"wlan3": "6"}
        with ng.with_pyxel(canvas):
            self.plugin.draw(0, 0, ng.W, ng.H)
        self.assertEqual(canvas.overlaps(), [])
        said = canvas.texts
        self.assertIn("LOCKED: channel 6", said)
        self.assertIn("CHANNEL PLAN", said)
        self.assertTrue(any(t.startswith("wlan1: 5 GHz low") and "6 GHz" in t for t in said), said)
        self.assertTrue(any(t.startswith("wlan2: 2.4 GHz 1-5 7-13") for t in said), said)
        self.assertIn("wlan3: locked on channel 6", said)
        self.assertTrue(any(t.startswith("Channel split ON") for t in said))

    def test_with_the_split_off_only_the_lock_is_listed(self):
        canvas = ng.Canvas()
        self.plugin._wifi_lock = {"wlan3": "6"}
        self.plugin._split_mode = "off"
        with ng.with_pyxel(canvas):
            self.plugin.draw(0, 0, ng.W, ng.H)
        self.assertEqual(canvas.overlaps(), [])
        self.assertIn("wlan1: hops all its channels", canvas.texts)
        self.assertIn("wlan3: locked on channel 6", canvas.texts)

    def test_one_card_without_a_lock_shows_no_plan(self):
        canvas = ng.Canvas()
        self.plugin._wifi_selected = {"wlan1"}
        with ng.with_pyxel(canvas):
            self.plugin.draw(0, 0, ng.W, ng.H)
        self.assertNotIn("CHANNEL PLAN", canvas.texts)

    def test_a_full_screen_of_cards_still_fits(self):
        canvas = ng.Canvas()
        self.plugin._wifi_all = [dict(id="wlan%d" % i, mac="02:00:00:00:00:%02x" % i, guess="card", can_monitor=True,
                                      note="") for i in range(1, 9)]
        self.plugin._wifi_selected = {w["id"] for w in self.plugin._wifi_all}
        self.plugin._wifi_channels = {w["id"]: (TRI if w["id"] == "wlan1" else DUAL) for w in self.plugin._wifi_all}
        self.plugin._wifi_lock = {"wlan8": "149"}
        self.plugin._rescan_due = [9e18]
        with ng.with_pyxel(canvas):
            self.plugin.draw(0, 0, ng.W, ng.H)
        self.assertEqual(canvas.overlaps(), [])
        self.assertTrue(any(t.startswith("wlan8: locked on channel 149") for t in canvas.texts))

    def test_the_lock_is_saved_per_adapter_and_comes_back(self):
        self.plugin._wifi_lock = {"wlan2": "11"}
        self.plugin._save_adapter_prefs()
        saved = json.load(open(os.path.join(self.tmp.name, "kismet_adapters.json"), encoding="utf-8"))
        self.assertEqual(saved["wifi_locks"], {"02:00:00:00:00:02": "11"})
        self.assertEqual(len(saved["wifi_macs"]), 3)                        # what was there before is still there
        # the card comes back under another interface name: the lock follows its MAC
        fresh = kismet.KismetWardriveBridge()
        fresh.app, fresh.term, fresh.msg = self.plugin.app, Mock(), Mock()
        again = [dict(id="wlan7", mac="02:00:00:00:00:02", guess="dual-band card", can_monitor=True, note="")]
        with patch.object(kismet.hw, "detect_wifi", return_value=again), \
                patch.object(kismet.hw, "detect_ble", return_value=[]), \
                patch.object(kismet.hw, "phy_info", return_value=IW_INFO, create=True):
            fresh._rescan_adapters(initial=True)
            for _ in range(200):
                if fresh._wifi_channels.get("wlan7"):
                    break
                __import__("time").sleep(0.01)
        self.assertEqual(fresh._wifi_lock, {"wlan7": "11"})
        self.assertEqual(fresh._wifi_channels["wlan7"][:3], ["1", "6", "11"])  # read on the worker thread
        # a lock taken off is forgotten; a card that is not plugged in keeps the one saved for it
        fresh._wifi_selected, fresh._wifi_lock = {"wlan7"}, {}
        self.plugin._wifi_lock = {"wlan2": "11", "wlan3": "36"}
        self.plugin._save_adapter_prefs()
        fresh._save_adapter_prefs()
        saved = json.load(open(os.path.join(self.tmp.name, "kismet_adapters.json"), encoding="utf-8"))
        self.assertEqual(saved["wifi_locks"], {"02:00:00:00:00:03": "36"})

    def test_an_old_preference_file_without_locks_still_loads(self):
        with open(os.path.join(self.tmp.name, "kismet_adapters.json"), "w", encoding="utf-8") as fh:
            json.dump({"wifi_macs": ["02:00:00:00:00:01"], "ble_macs": []}, fh)
        self.assertEqual(self.plugin._load_adapter_prefs()["wifi_macs"], ["02:00:00:00:00:01"])
        self.plugin._wifi_lock = {}
        self.plugin._save_adapter_prefs()
        self.assertEqual(json.load(open(os.path.join(self.tmp.name, "kismet_adapters.json")))["wifi_locks"], {})

    def launch(self, phy_text):
        """Run the start worker with a stand-in engine and hardware; returns the engine's start call."""
        calls = []

        def start(wifi, ble, **kwargs):
            calls.append((list(wifi), kwargs))
            return True
        self.plugin.engine = SimpleNamespace(start=start, log_dir=self.tmp.name, running=False)
        found = {w["mac"]: w for w in self.plugin._wifi_all}
        with patch.object(kismet.hw, "resolve_wifi_by_mac", side_effect=lambda mac: found.get(mac)), \
                patch.object(kismet.hw, "kill_interferers"), \
                patch.object(kismet.hw, "set_monitor", return_value=(True, "ok")), \
                patch.object(kismet.hw, "phy_info", return_value=phy_text, create=True), \
                patch.object(kismet, "_gpsd_reachable", return_value=False), \
                patch.object(self.plugin, "_write_hardware_manifest"), \
                patch.object(self.plugin, "_ensure_gps"), \
                patch.object(kismet.threading, "Thread") as thread:
            self.plugin._launch_from_picker()
            target, args = thread.call_args.kwargs["target"], thread.call_args.kwargs["args"]
            target(*args)
        return calls[0]

    def test_the_lock_is_sent_with_the_launch(self):
        self.plugin._wifi_lock = {"wlan2": "6"}
        wifi, kwargs = self.launch(IW_INFO)
        self.assertEqual(wifi, ["wlan1", "wlan2", "wlan3"])
        self.assertEqual(kwargs.get("channel_locks"), {"wlan2": "6"})
        self.assertEqual(self.plugin._locks_wanted, {"wlan2": "6"})

    def test_no_lock_means_the_engine_is_called_exactly_as_before(self):
        _wifi, kwargs = self.launch(IW_INFO)
        self.assertEqual(kwargs, {"gps_override": False})

    def test_a_card_that_cannot_tune_the_channel_hops_and_says_so(self):
        self.plugin._wifi_lock = {"wlan2": "100"}                           # IW_INFO lists no channel 100
        _wifi, kwargs = self.launch(IW_INFO)
        self.assertNotIn("channel_locks", kwargs)
        said = " ".join(str(call.args[0]) for call in self.plugin.term.call_args_list)
        self.assertIn("wlan2 cannot tune channel 100", said)
        self.assertIn("hops like the others", said)

    def test_a_card_whose_channels_cannot_be_read_is_left_to_kismets_answer(self):
        self.plugin._wifi_lock = {"wlan2": "100"}
        _wifi, kwargs = self.launch("")                                     # iw said nothing
        self.assertEqual(kwargs.get("channel_locks"), {"wlan2": "100"})

    def test_the_kismet_command_line_carries_the_lock(self):
        engine = kismet.KismetBridgeEngine.__new__(kismet.KismetBridgeEngine)
        engine.log, engine.log_dir, engine.cfg = Mock(), self.tmp.name, {}
        started = {}

        class Stop(Exception):
            pass

        def popen(cmd, **_kwargs):
            started["cmd"] = cmd
            raise Stop()
        with patch.object(kismet.hw, "_run", return_value=(1, "", "")), patch.object(kismet.subprocess, "Popen", popen):
            with self.assertRaises(Stop):
                engine.start(["wlan1", "wlan2"], ["hci1"], channel_locks={"wlan2": "6"})
        engine._stdout_file.close()
        self.assertIn("wlan1:channel_hop=true,retry=true", started["cmd"])
        self.assertIn("wlan2:channel_hop=false,channel=6,retry=true", started["cmd"])
        self.assertIn("hci1:name=hci1_bt,retry=true", started["cmd"])

    def sources(self, **more):
        rows = [source("wlan1", TRI, hopping=True, channel="1"), source("wlan2", DUAL, hopping=True, channel="1"),
                source("wlan3", DUAL, hopping=True, channel="1")]
        for row in rows:
            row.update(more.get(row["name"], {}))
        return rows

    def test_a_lock_is_put_in_force_and_kept_out_of_the_split(self):
        self.plugin.engine = engine = FakeEngine()
        self.plugin._locks_wanted = {"wlan3": "6"}
        self.plugin._apply_split(self.sources())
        self.assertEqual(engine.locks, [("uuid-wlan3", "6")])
        self.assertEqual(engine.locked, {"uuid-wlan3": "6"})                # what the node planner reads
        sent = dict(engine.hops)
        self.assertEqual(set(sent), {"uuid-wlan1", "uuid-wlan2"})           # the locked card got no hop list
        self.assertNotIn("6", sent["uuid-wlan1"] + sent["uuid-wlan2"])
        self.assertIn("wlan3 stays on channel 6", self.plugin._split_report)

    def test_a_lock_the_launch_already_made_is_not_sent_again(self):
        self.plugin.engine = engine = FakeEngine()
        self.plugin._locks_wanted = {"wlan3": "6"}
        self.plugin._apply_split(self.sources(wlan3={"hopping": False, "channel": "6"}))
        self.assertEqual(engine.locks, [])
        self.assertEqual(engine.locked, {"uuid-wlan3": "6"})

    def test_a_card_kismet_says_cannot_tune_the_channel_hops_and_it_is_said_once(self):
        self.plugin.engine = engine = FakeEngine()
        self.plugin._locks_wanted = {"wlan2": "1W6e"}                       # a dual-band card: no 6 GHz
        self.plugin._apply_split(self.sources(wlan2={"hopping": False, "channel": "1"}))
        self.assertEqual(engine.locks, [])
        self.assertEqual(engine.locked, {})
        self.assertIn(("uuid-wlan2", DUAL), engine.hops)                    # first back to all of its channels
        events = [text for _level, text in self.plugin._kismet_events]
        self.assertEqual(len([t for t in events if "wlan2 cannot be locked to channel 1W6e" in t]), 1)
        # ... and then it takes part in the split like any hopping card
        self.assertEqual(len({uuid for uuid, _ in engine.hops}), 3)
        self.plugin._split_applied = None
        self.plugin._apply_split(self.sources())
        events = [text for _level, text in self.plugin._kismet_events]
        self.assertEqual(len([t for t in events if "cannot be locked" in t]), 1)   # not said again

    def test_kismet_refusing_the_lock_command_also_falls_back(self):
        self.plugin.engine = engine = FakeEngine(refuse={"uuid-wlan3"})
        self.plugin._locks_wanted = {"wlan3": "6"}
        self.plugin._apply_split(self.sources())
        self.assertEqual(engine.locked, {})
        self.assertTrue(any("wlan3 cannot be locked to channel 6 (HTTP 500)" in text
                            for _level, text in self.plugin._kismet_events))

    def test_the_lock_holds_with_the_split_switched_off(self):
        self.plugin.engine = engine = FakeEngine()
        self.plugin._split_mode = "off"
        self.plugin._locks_wanted = {"wlan3": "6"}
        self.plugin._apply_split(self.sources())
        self.assertEqual(engine.locks, [("uuid-wlan3", "6")])
        self.assertEqual(engine.hops, [])                                   # nobody's hopping is changed
        self.assertTrue(self.plugin._split_report.startswith("off (WDG_KISMET_SPLIT=off"))
        self.assertIn("wlan3 locked on channel 6", self.plugin._split_report)

    def test_unplugging_the_locked_card_gives_its_channel_back(self):
        self.plugin.engine = engine = FakeEngine()
        self.plugin._locks_wanted = {"wlan3": "6"}
        self.plugin._apply_split(self.sources())
        engine.hops.clear()
        rows = self.sources()
        rows[2]["running"] = False
        self.plugin._apply_split(rows)
        self.assertEqual(engine.locked, {})
        sent = dict(engine.hops)
        self.assertIn("6", sent["uuid-wlan1"] + sent["uuid-wlan2"])         # channel 6 is hopped again

    def test_three_cards_are_split_and_reported(self):
        self.plugin.engine = engine = FakeEngine()
        self.plugin._apply_split(self.sources())
        self.assertEqual(len(engine.hops), 3)
        self.assertTrue(self.plugin._split_report.startswith("band split: wlan1: 5 GHz low"))
        self.plugin._apply_split(self.sources())                            # same cards: nothing re-sent
        self.assertEqual(len(engine.hops), 3)

    def test_the_sources_kismet_reports_carry_the_lock_for_the_node_planner(self):
        engine = kismet.KismetBridgeEngine.__new__(kismet.KismetBridgeEngine)
        engine.locked = {"U1": "6"}
        payload = [{"kismet.datasource.uuid": "U1", "kismet.datasource.interface": "wlan1",
                    "kismet.datasource.running": 1, "kismet.datasource.channels": DUAL,
                    "kismet.datasource.hop_channels": DUAL},
                   {"kismet.datasource.uuid": "U2", "kismet.datasource.interface": "wlan2",
                    "kismet.datasource.running": 1, "kismet.datasource.channels": DUAL,
                    "kismet.datasource.hop_channels": DUAL}]
        engine._get = lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: payload)
        engine._auth = lambda: None
        rows, error = engine.datasources()
        self.assertIsNone(error)
        self.assertEqual([row.get("lock") for row in rows], ["6", None])
        self.assertEqual(node_plan.kismet_heavy_channels(rows)[0], {6})


if __name__ == "__main__":
    unittest.main()
