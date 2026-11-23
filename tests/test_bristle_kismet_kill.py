# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""[bristle] WDG-Bristle 1.2.1: the K kill switch is marked in the game.

K already stopped Kismet from anywhere. What is new is that the game says so: a label on the map
while Kismet runs, one terminal line at the start of a drive, the PLUGINS menu label, and at stop
which Wi-Fi cards went back to managed mode and which were left as they were.

Stand-ins only: no Kismet is started and no adapter is touched. What a real card answers when it
is switched back is a bench item (bristle/docs/BENCH_TESTS.md).
"""
import re
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from plugins import _kismet_extras as kx
from plugins import _w5_hardware as w5
from plugins._bristle import screen_more
import plugins.kismet_wardrive as kismet

import test_v3_ng_plugins as ng

ROOT = Path(__file__).resolve().parents[1]
HUD_TOP, TERM_Y = 16, 234


def make_plugin():
    plugin = kismet.KismetWardriveBridge()
    plugin.app = SimpleNamespace(_esc_consumed_frame=-1, _app_dir=str(ROOT))
    plugin.term, plugin.msg = Mock(), Mock()
    return plugin


def said(plugin):
    return [call.args[0] for call in plugin.term.call_args_list]


class Words(unittest.TestCase):
    def test_the_wording_is_the_wording_asked_for(self):
        self.assertEqual(kismet.KILL_SWITCH_HUD, "K: stop Kismet")
        self.assertEqual(kismet.KILL_SWITCH_LINE,
                         "Kismet running. Press K anywhere to stop and turn monitor mode off")

    def test_the_plugins_menu_says_k_works_anywhere(self):
        items = {item.action: item for item in make_plugin().menu_items()}
        self.assertEqual(items["stop_wardrive"].label, "Kismet: Stop (or K anywhere)")
        self.assertEqual(items["stop_wardrive"].key, "j")              # the menu key did not move
        # the README's "TAB > PLUGINS > Kismet: Stop" still finds it
        self.assertTrue(items["stop_wardrive"].label.startswith("Kismet: Stop"))


class StartLine(unittest.TestCase):
    def started(self, segment):
        plugin = make_plugin()
        plugin._poll_result_lock = threading.Lock()
        plugin._lifecycle_result = ("start", True, ["wlan1"])
        plugin._segment_no = segment
        plugin._start_poll_thread, plugin._save_adapter_prefs = Mock(), Mock()
        plugin._apply_lifecycle_result()
        self.assertEqual(plugin._lifecycle, "running")
        return [line for line in said(plugin) if "Press K" in line]

    def test_a_drive_that_starts_says_once_how_to_stop_it(self):
        self.assertEqual(self.started(1), ["[KISMET] Kismet running. Press K anywhere to stop and turn monitor mode off"])

    def test_a_rotation_or_a_restart_after_a_crash_does_not_say_it_again(self):
        self.assertEqual(self.started(2), [])
        self.assertEqual(self.started(7), [])

    def test_a_start_that_failed_does_not_claim_kismet_is_running(self):
        plugin = make_plugin()
        plugin._poll_result_lock = threading.Lock()
        plugin._lifecycle_result = ("start", False, [])
        plugin._segment_no = 1
        plugin._restore_then_idle = Mock()
        plugin._apply_lifecycle_result()
        self.assertEqual([line for line in said(plugin) if "Press K" in line], [])


class MapLabel(unittest.TestCase):
    def draw(self, lifecycle="running", active=True):
        plugin = make_plugin()
        plugin._lifecycle = lifecycle
        plugin.kismet_active = lambda: active
        canvas = ng.Canvas()
        boxes = []
        canvas.rectb = lambda x, y, w, h, colour: boxes.append((x, y, w, h))
        plugin.draw_map_overlay(canvas, ng.W, HUD_TOP, TERM_Y)
        return canvas, boxes

    def test_it_is_on_the_map_while_kismet_captures(self):
        canvas, boxes = self.draw()
        self.assertEqual(canvas.texts, ["K: stop Kismet"])
        x, y, width, _text = canvas.placed[0]
        (bx, by, bw, bh), = boxes
        self.assertTrue(bx < x and x + width < bx + bw and by < y and y + ng.Canvas.FONT_H < by + bh)   # text inside its box
        self.assertTrue(HUD_TOP <= by and by + bh <= TERM_Y)                                              # box inside the map

    def test_it_is_gone_when_kismet_is_not_capturing(self):
        for lifecycle, active in (("idle", False), ("starting", False), ("stopping", True), ("running", False)):
            canvas, boxes = self.draw(lifecycle, active)
            self.assertEqual((canvas.texts, boxes), ([], []), (lifecycle, active))

    def test_it_keeps_clear_of_what_else_is_drawn_at_the_top_of_the_map(self):
        _canvas, boxes = self.draw()
        (x, y, w, h), = boxes
        # the emergency scan's lightbar and its status text: the map's top-left row (app.py
        # _draw_emergency_alert: lamps at y HUD_TOP+4, 16 px with the mount)
        self.assertGreaterEqual(y, HUD_TOP + 4 + 16)
        # the proximity icon, further down the left edge (app.py _draw_prox_icon: HUD_TOP+100)
        self.assertLess(y + h, HUD_TOP + 100 - 12)
        # the Bristle alert popup, top centre: even its widest form starts right of the label
        alert = SimpleNamespace(title="T" * 200, detail="D" * 200)
        popup = ng.Canvas()
        placed = []
        popup.rectb = lambda px, py, pw, ph, colour: placed.append((px, py, pw, ph))
        screen_more.draw_popup(popup, ng.W, HUD_TOP, alert)
        self.assertGreater(placed[0][0], x + w)
        # the radar and the WDGWars chip are on the right half
        self.assertLess(x + w, ng.W // 2)

    def test_the_game_asks_every_plugin_for_its_map_overlay(self):
        source = (ROOT / "watchdogs" / "app.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('getattr(_p, "draw_map_overlay", None)'), 1)


class StopReport(unittest.TestCase):
    def test_cards_the_game_switched_go_back_and_are_named(self):
        lines = kx.stop_report(["wlan1", "wlan2"], ["wlan1", "wlan2"], {"wlan1": (True, ""), "wlan2": (True, "")})
        self.assertEqual(lines, ["Back in managed mode and handed back to NetworkManager: wlan1, wlan2"])

    def test_a_card_without_monitor_ticked_is_named_as_left_alone(self):
        lines = kx.stop_report(["wlan1", "wlan2"], ["wlan1"], {"wlan1": (True, "")})
        self.assertEqual(lines, ["Back in managed mode and handed back to NetworkManager: wlan1",
                                 "Left as they were: wlan2 (monitor mode was not switched on for it in the picker)"])
        lines = kx.stop_report(["wlan1", "wlan2"], [], {})
        self.assertEqual(lines, ["Left as they were: wlan1, wlan2 (monitor mode was not switched on for them in the picker)"])

    def test_a_card_that_would_not_switch_back_is_said_loudly_with_the_command(self):
        lines = kx.stop_report(["wlan1", "wlan2"], ["wlan1", "wlan2"],
                               {"wlan1": (False, "Device or resource busy"), "wlan2": (True, "")})
        self.assertEqual(lines[0], "Back in managed mode and handed back to NetworkManager: wlan2")
        self.assertEqual(lines[1], "STILL IN MONITOR MODE: wlan1 (Device or resource busy). "
                                   "By hand: sudo iw dev wlan1 set type managed")
        self.assertEqual(kx.stop_report(["wlan1"], ["wlan1"], {"wlan1": (False, "")})[0],
                         "STILL IN MONITOR MODE: wlan1 (no reason given). By hand: sudo iw dev wlan1 set type managed")

    def test_a_card_that_was_in_monitor_mode_before_the_run_is_reported_truthfully(self):
        """The game switches every card with M ticked back to managed mode, also one that was in
        monitor mode before the run. The report says so instead of leaving it to be found out."""
        lines = kx.stop_report(["wlan1"], ["wlan1"], {"wlan1": (True, "")}, already={"wlan1"})
        self.assertEqual(lines, ["Back in managed mode and handed back to NetworkManager: wlan1",
                                 "wlan1 was already in monitor mode before this run; it is in managed mode now"])
        # ... and never for a card that did not go back
        lines = kx.stop_report(["wlan1"], ["wlan1"], {"wlan1": (False, "busy")}, already={"wlan1"})
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("STILL IN MONITOR MODE: wlan1"))

    def test_a_run_with_only_bluetooth_says_there_was_nothing_to_switch(self):
        self.assertEqual(kx.stop_report([], [], {}), ["No Wi-Fi card was in this run: nothing to switch back"])

    def test_every_line_fits_the_terminal(self):
        many = ["wlan%d" % index for index in range(1, 7)]
        for lines in (kx.stop_report(many, many, {iface: (True, "") for iface in many}),
                      kx.stop_report(many, [], {}),
                      kx.stop_report(many, many, {iface: (False, "Device or resource busy") for iface in many})):
            for line in lines:
                self.assertLessEqual(len("[KISMET] " + line), 126, line)       # 640 px / 5 px a character


class Restore(unittest.TestCase):
    def restore(self, answers, used, forced, before=()):
        plugin = make_plugin()
        plugin._wifi_used, plugin._monitor_before = list(used), set(before)
        released = []
        fake = SimpleNamespace(set_monitor=lambda iface, on, log=None: answers[iface],
                               release_interface_back=lambda iface, log=None: released.append(iface))
        with patch.object(kismet, "hw", fake):
            plugin._restore_adapters(list(forced))
        return plugin, released, said(plugin)

    def test_stop_names_the_cards_that_went_back_and_the_ones_left_alone(self):
        plugin, released, lines = self.restore({"wlan1": (True, "ok")}, ["wlan1", "wlan2"], ["wlan1"])
        self.assertEqual(released, ["wlan1"])                                  # wlan2 was never touched
        self.assertIn("[KISMET] Back in managed mode and handed back to NetworkManager: wlan1", lines)
        self.assertIn("[KISMET] Left as they were: wlan2 (monitor mode was not switched on for it in the picker)", lines)
        self.assertEqual((plugin._wifi_used, plugin._monitor_before), ([], set()))     # not said twice

    def test_a_card_that_refuses_is_reported_and_still_handed_back(self):
        _plugin, released, lines = self.restore({"wlan1": (False, "Device or resource busy")}, ["wlan1"], ["wlan1"])
        self.assertEqual(released, ["wlan1"])
        self.assertTrue(any(line.startswith("[KISMET] STILL IN MONITOR MODE: wlan1 (Device or resource busy)")
                            for line in lines), lines)
        self.assertFalse(any("Back in managed mode" in line for line in lines))

    def test_a_card_that_raises_is_reported_too(self):
        plugin = make_plugin()
        plugin._wifi_used = ["wlan1"]

        def broken(iface, on, log=None):
            raise OSError("no such device")
        fake = SimpleNamespace(set_monitor=broken, release_interface_back=lambda iface, log=None: None)
        with patch.object(kismet, "hw", fake):
            plugin._restore_adapters(["wlan1"])
        self.assertTrue(any("STILL IN MONITOR MODE: wlan1 (no such device)" in line for line in said(plugin)))

    def test_the_already_in_monitor_mode_card_gets_its_own_line(self):
        _plugin, _released, lines = self.restore({"wlan1": (True, "")}, ["wlan1"], ["wlan1"], before={"wlan1"})
        self.assertIn("[KISMET] wlan1 was already in monitor mode before this run; it is in managed mode now", lines)

    def test_a_rotation_says_nothing_and_remembers_what_the_cards_were_before_the_drive(self):
        plugin = make_plugin()
        plugin._wifi_used, plugin._monitor_before = ["wlan1", "wlan2"], {"wlan1"}
        plugin._relaunch_after_stop = True                                     # Kismet starts again at once
        switched = []
        fake = SimpleNamespace(set_monitor=lambda iface, on, log=None: switched.append(iface) or (True, ""),
                               release_interface_back=lambda iface, log=None: None)
        with patch.object(kismet, "hw", fake):
            plugin._restore_adapters(["wlan1"])
        self.assertEqual(switched, ["wlan1"])                                  # the cards are switched as before
        self.assertEqual(said(plugin), [])                                     # ... without a report in between
        self.assertEqual(plugin._monitor_before, {"wlan1"})
        plugin._relaunch_after_stop = False                                    # the drive ends
        with patch.object(kismet, "hw", fake):
            plugin._restore_adapters(["wlan1"])
        self.assertIn("[KISMET] wlan1 was already in monitor mode before this run; it is in managed mode now",
                      said(plugin))

    def test_only_the_first_segment_of_a_drive_records_what_the_cards_were(self):
        source = (ROOT / "plugins" / "kismet_wardrive.py").read_text(encoding="utf-8")
        at = source.index("self._wifi_used = list(wifi_ready)")
        block = source[at:at + 200]
        self.assertIn('if getattr(self, "_segment_no", 1) <= 1:', block)
        self.assertIn("self._monitor_before = set(monitor_before)", block)

    def test_a_stand_in_that_answers_nothing_is_taken_as_done_as_before(self):
        _plugin, _released, lines = self.restore({"wlan1": None}, ["wlan1"], ["wlan1"])
        self.assertIn("[KISMET] Back in managed mode and handed back to NetworkManager: wlan1", lines)


class IsMonitor(unittest.TestCase):
    def look(self, rc, text):
        calls = []

        def run(command, *args, **kwargs):
            calls.append(command)
            return rc, text, ""
        with patch.object(w5, "_run", run):
            answer = w5.is_monitor("wlan1")
        self.assertEqual(calls, [["iw", "dev", "wlan1", "info"]])              # one read-only command
        return answer

    def test_it_reads_the_type_and_changes_nothing(self):
        self.assertIs(self.look(0, "Interface wlan1\n\tifindex 4\n\ttype monitor\n\twiphy 1\n"), True)
        self.assertIs(self.look(0, "Interface wlan1\n\tifindex 4\n\ttype managed\n\twiphy 1\n"), False)
        self.assertIsNone(self.look(237, ""))                                  # no such device: unknown, not "no"

    def test_the_start_only_looks_and_a_failing_look_never_stops_the_run(self):
        source = (ROOT / "plugins" / "kismet_wardrive.py").read_text(encoding="utf-8")
        start = source.index("monitor_before = set()")
        block = source[start:source.index("hw.kill_interferers(cur", start)]
        self.assertIn('getattr(hw, "is_monitor", None)', block)
        self.assertIn("except Exception:", block)


class InstallScript(unittest.TestCase):
    """Item 3: install.sh carried a variable nothing read (shellcheck SC2034)."""

    def test_every_variable_install_sh_sets_is_read_somewhere(self):
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertNotIn("\r", text)                                           # a shell script: LF only
        self.assertNotIn("OLD_INSTALL_NAME", text)
        names = set(re.findall(r"^\s*([A-Z][A-Z0-9_]*)=", text, flags=re.M))
        names |= set(re.findall(r";\s*([A-Z][A-Z0-9_]*)=", text))              # RED=...; GREEN=...
        self.assertIn("OLD_DESKTOP_ID", names)
        unread = [name for name in sorted(names)
                  if not re.search(r"\$\{?%s\b" % name, text) and not re.search(r"\bexport\s+%s\b" % name, text)]
        self.assertEqual(unread, [])


if __name__ == "__main__":
    unittest.main()
