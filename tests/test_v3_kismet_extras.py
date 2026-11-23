"""[merge-v3] Kismet bridge helpers: channel bands, the two-adapter channel
split, datasource states and the picker's split line. Pure logic plus the
measuring Canvas; the end-to-end behaviour (crash, 401, unplug, GPS filter,
GPIO 23) is in tools/sim (test_v3_sim.py)."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from plugins import _kismet_extras as kx
import plugins.kismet_wardrive as kismet

import test_v3_ng_plugins as ng

CH_24 = [str(n) for n in range(1, 14)]
CH_5 = ["36", "40", "44", "48", "149", "153"]
CH_6 = ["1W6e", "5W6e", "37W6e"]


def source(name, channels, running=True, uuid=None, kind="wifi", hop=None):
    return dict(uuid=uuid or "uuid-" + name, name=name, interface=name, kind=kind, running=running,
                error=not running, reason="", channels=list(channels),
                hop_channels=list(channels if hop is None else hop), hop_rate=5.0)


class ChannelBandTests(unittest.TestCase):
    def test_kismet_channel_names(self):
        cases = {"1": "2.4", "14": "2.4", "6HT40+": "2.4", "36": "5", "36VHT80": "5", "165": "5",
                 "1W6e": "6", "233W6e": "6", "37w6e": "6", "0": None, "999": None, "": None,
                 "abc": None, None: None, 6: None}
        for name, band in cases.items():
            with self.subTest(name=name):
                self.assertEqual(kx.channel_band(name), band)


class SplitTests(unittest.TestCase):
    def test_tri_band_and_dual_band_adapter(self):
        alfa, ac = source("alfa", CH_24 + CH_5 + CH_6), source("ac1200", CH_24 + CH_5)
        plan, why = kx.plan_split([alfa, ac])
        self.assertEqual(plan[ac["uuid"]], CH_24)
        self.assertEqual(plan[alfa["uuid"]], CH_5 + CH_6)
        self.assertIn("alfa: 9 channels (5/6 GHz), ac1200: 13 channels (2.4 GHz)", why)
        self.assertEqual(kx.plan_split([ac, alfa])[0], plan)             # order does not matter

    def test_every_channel_is_still_hopped_by_someone(self):
        cases = ((CH_24 + CH_5 + CH_6, CH_24 + CH_5), (CH_24 + CH_5, CH_24 + CH_5),
                 (CH_24 + CH_5, CH_24), (CH_24 + CH_5 + ["weird"], CH_24 + CH_5 + ["200"]),
                 (CH_24 + ["36HT40+", "36VHT80"], CH_24 + ["1HT40+"]))
        for first, second in cases:
            with self.subTest(first=first, second=second):
                a, b = source("a", first), source("b", second)
                plan, _why = kx.plan_split([a, b])
                self.assertTrue(plan)
                self.assertEqual(set(plan[a["uuid"]]) | set(plan[b["uuid"]]), set(first) | set(second))
                self.assertLessEqual(set(plan[a["uuid"]]), set(first))    # nothing an adapter never offered
                self.assertLessEqual(set(plan[b["uuid"]]), set(second))
                self.assertTrue(plan[a["uuid"]] and plan[b["uuid"]])

    def test_the_hop_list_kismet_is_using_wins_over_the_full_list(self):
        a = source("a", CH_24 + CH_5 + CH_6, hop=["1", "6", "11", "36", "149"])
        b = source("b", CH_24 + CH_5, hop=["1", "6", "11", "36"])
        plan, _why = kx.plan_split([a, b])
        self.assertEqual(plan[a["uuid"]], ["36", "149"])
        self.assertEqual(plan[b["uuid"]], ["1", "6", "11"])

    def test_no_plan_when_a_split_makes_no_sense(self):
        for sources, fragment in (
                ([source("a", CH_24 + CH_5)], "one Wi-Fi adapter"),
                ([source("a", CH_24), source("b", CH_24, running=False)], "one Wi-Fi adapter"),
                # [bristle] 1.2: three adapters are split now (tests/test_bristle_kismet_cards.py);
                # until 1.1 this case read "3 Wi-Fi adapters capturing: left to Kismet's own hopping"
                ([source("a", CH_24), source("b", CH_24)], "do not allow"),
                ([source("a", CH_5), source("b", CH_5 + CH_6)], "do not allow"),
                ([source("a", []), source("b", CH_24)], "not reported"),
                ([source("a", CH_24 + CH_5), source("hci0", [], kind="bt")], "one Wi-Fi adapter")):
            with self.subTest(fragment=fragment):
                plan, why = kx.plan_split(sources)
                self.assertEqual(plan, {})
                self.assertIn(fragment, why)


class SourceTests(unittest.TestCase):
    def test_kismet_json_is_read_defensively(self):
        payload = [
            {"kismet.datasource.uuid": "U1", "kismet.datasource.name": "wlan1",
             "kismet.datasource.interface": "wlan1", "kismet.datasource.running": 1,
             "kismet.datasource.type_driver": {"kismet.datasource.driver.type": "linuxwifi"},
             "kismet.datasource.channels": ["1", "36", 7, None], "kismet.datasource.hop_rate": 4},
            {"kismet.datasource.uuid": "U2", "kismet.datasource.interface": "hci0",
             "kismet.datasource.running": False, "kismet.datasource.error": True,
             "kismet.datasource.error_reason": "x" * 500},
            {"kismet.datasource.channels": ["6"], "kismet.datasource.hop_rate": True},
            "junk", None, 5]
        rows = kx.parse_sources(payload)
        self.assertEqual([(r["name"], r["kind"], r["running"]) for r in rows],
                         [("wlan1", "wifi", True), ("hci0", "bt", False), ("", "wifi", False)])
        self.assertEqual(rows[0]["channels"], ["1", "36"])
        self.assertEqual((rows[0]["hop_rate"], rows[2]["hop_rate"]), (4.0, 5.0))
        self.assertEqual(len(rows[1]["reason"]), 120)
        for junk in (None, {}, "text", 7, [[]]):
            self.assertEqual(kx.parse_sources(junk), [])

    def test_changes_are_reported_once_per_transition(self):
        up, down = source("wlan2", CH_24), source("wlan2", CH_24, running=False)
        self.assertEqual(kx.source_changes({}, [up]), [])
        self.assertEqual(kx.source_changes({up["uuid"]: True}, [up]), [])
        lost = kx.source_changes({up["uuid"]: True}, [down])
        self.assertEqual(len(lost), 1)
        self.assertEqual(lost[0][0], "warn")
        self.assertIn("wlan2 stopped capturing", lost[0][1])
        self.assertEqual(kx.source_changes({up["uuid"]: False}, [down]), [])
        back = kx.source_changes({up["uuid"]: False}, [up])
        self.assertEqual(back, [("ok", "wlan2 is capturing again")])
        self.assertIn("is not capturing", kx.source_changes({}, [down])[0][1])


class PickerTests(unittest.TestCase):
    def plugin(self, selected):
        plugin = kismet.KismetWardriveBridge()
        plugin.app = SimpleNamespace(_esc_consumed_frame=-1)
        plugin.term, plugin.msg = Mock(), Mock()
        plugin._wifi_all = [dict(id="wlan1", mac="a", guess="Alfa AWUS036AXML (mt7921u)", can_monitor=True, note=""),
                            dict(id="wlan2", mac="b", guess="AC1200 (rtl8812au)", can_monitor=True,
                                 note="monitor driver not installed")]
        plugin._ble_all = [dict(id="hci1", mac="c", guess="UB500")]
        plugin._wifi_selected, plugin._ble_selected = set(selected), {"hci1"}
        plugin._wifi_monitor = {"wlan1": True, "wlan2": True}
        plugin._menu_sel = 0
        plugin._overlay_active = True
        return plugin

    def test_split_line_shows_only_with_two_wifi_adapters_and_fits(self):
        canvas = ng.Canvas()
        with ng.with_pyxel(canvas):
            both = self.plugin(["wlan1", "wlan2"])
            both.draw(0, 0, ng.W, ng.H)
            self.assertEqual(canvas.overlaps(), [])
            self.assertTrue(any(text.startswith("Channel split ON") for text in canvas.texts))
            canvas.pressed = {"KEY_S"}
            both._picker_on_update()
            canvas.pressed = set()
            canvas.new_frame()
            both._rescan_due = [9e18]
            both.draw(0, 0, ng.W, ng.H)
            self.assertEqual(canvas.overlaps(), [])
            self.assertTrue(any(text.startswith("Channel split OFF") for text in canvas.texts))
            self.assertTrue(any("Internal USB rail just switched on" in text for text in canvas.texts))
            self.assertEqual(both._split_mode, "off")
            canvas.new_frame()
            self.plugin(["wlan1"]).draw(0, 0, ng.W, ng.H)
            self.assertFalse(any(text.startswith("Channel split") for text in canvas.texts))

    def test_split_report_is_kept_for_the_hardware_screen(self):
        plugin = self.plugin(["wlan1", "wlan2"])
        calls = []
        plugin.engine = SimpleNamespace(set_hop_channels=lambda uuid, channels, rate: (calls.append(uuid), (True, None))[1])
        sources = [source("wlan1", CH_24 + CH_5 + CH_6), source("wlan2", CH_24 + CH_5)]
        plugin._apply_split(sources)
        self.assertEqual(len(calls), 2)
        self.assertTrue(plugin._split_report.startswith("band split: wlan1: 9 channels"))
        plugin._apply_split(sources)                                  # same adapters: nothing re-sent
        self.assertEqual(len(calls), 2)
        plugin.engine = SimpleNamespace(set_hop_channels=lambda *a: (False, "HTTP 403: read-only"))
        plugin._split_applied = None
        plugin._apply_split(sources)
        self.assertEqual(plugin._split_report, "not applied: HTTP 403: read-only")


if __name__ == "__main__":
    unittest.main()
