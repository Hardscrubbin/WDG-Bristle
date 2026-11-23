# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""[bristle] WDG-Bristle 1.2, "open a suspect on the map": the map is centred on the suspect's
estimated spot at a zoom that shows its confidence radius, and the radius is drawn.

The maths (plugins/_bristle/mapfocus.py) is tested as plain numbers. The game's own map projection
and the camera lines are taken from watchdogs/app.py as they are written there, and the plugin's
key and draw calls run against the measuring Canvas. No game window is opened; how it looks on the
uConsole is a bench item.
"""
import ast
import copy
import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugins import bristle_core  # noqa: E402
from plugins._bristle import host, mapfocus, screen, screen_more  # noqa: E402

import test_v3_ng_plugins as ng  # noqa: E402

SOURCE = ROOT / "watchdogs" / "app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
GAME = {}
for _node in TREE.body:                      # the game's constants, then its MapProjection class
    if isinstance(_node, ast.Assign):
        try:
            exec(compile(ast.Module(body=[_node], type_ignores=[]), str(SOURCE), "exec"), GAME)
        except Exception:
            pass
    if isinstance(_node, ast.ClassDef) and _node.name == "MapProjection":
        exec(compile(ast.Module(body=[_node], type_ignores=[]), str(SOURCE), "exec"), GAME)
SPANS = [level[0] for level in GAME["ZOOM_LEVELS"]]
W, MAP_H, HUD_TOP, TERM_Y = GAME["W"], GAME["MAP_H"], GAME["HUD_TOP"], GAME["TERM_Y"]
LAT, LON = 12.5, -45.25                      # open sea: a made-up spot


def fake_game_module():
    return SimpleNamespace(ZOOM_LEVELS=GAME["ZOOM_LEVELS"], W=W, MAP_H=MAP_H)


def make_app():
    proj = GAME["MapProjection"]()
    proj.zoom = 9
    return SimpleNamespace(proj=proj, player_lat=LAT + 0.2, player_lon=LON + 0.2, _esc_consumed_frame=-1,
                           ble_devices=[], wifi_networks=[])


class Maths(unittest.TestCase):
    def test_a_radius_is_wider_than_tall_away_from_the_equator(self):
        d_lat, d_lon = mapfocus.degrees(60.0, 111.32)
        self.assertAlmostEqual(d_lat, 0.001, places=6)
        self.assertAlmostEqual(d_lon, 0.002, places=5)                      # 1 / cos(60) = 2
        at_equator = mapfocus.degrees(0.0, 111.32)
        self.assertAlmostEqual(at_equator[0], at_equator[1], places=9)
        self.assertEqual(mapfocus.degrees(40.0, -5), (0.0, 0.0))
        self.assertLess(mapfocus.degrees(89.9, 100)[1], 0.05)                # never blows up near a pole

    def test_pixels_follow_the_maps_scale(self):
        across, up = mapfocus.radius_pixels(0.0, 111.32, 0.02, 640)          # 0.001 degrees on a 0.02 degree map
        self.assertAlmostEqual(across, 32.0, places=3)
        self.assertAlmostEqual(up, 32.0, places=3)

    def test_the_zoom_is_the_closest_one_that_shows_the_whole_radius(self):
        last = len(SPANS) - 1
        self.assertEqual(mapfocus.pick_zoom(SPANS, LAT, 30, W, MAP_H), last)      # a tight estimate: closest level
        self.assertEqual(mapfocus.pick_zoom(SPANS, LAT, 0, W, MAP_H), last)
        chosen = [mapfocus.pick_zoom(SPANS, LAT, radius, W, MAP_H) for radius in (30, 300, 1000, 5000, 50000, 5e6)]
        self.assertEqual(chosen, sorted(chosen, reverse=True))                # a wider radius never zooms in
        self.assertLess(chosen[3], chosen[0])
        for radius in (30, 300, 1000, 5000, 50000):
            index = mapfocus.pick_zoom(SPANS, LAT, radius, W, MAP_H)
            across, up = mapfocus.radius_pixels(LAT, radius, SPANS[index], W)
            with self.subTest(radius=radius):
                self.assertLessEqual(up, mapfocus.FILL * MAP_H)               # the whole circle is on the map
                self.assertLessEqual(across, mapfocus.FILL * W)
                if index < last:                                              # ... and one level closer it is not
                    closer = mapfocus.radius_pixels(LAT, radius, SPANS[index + 1], W)
                    self.assertTrue(closer[1] > mapfocus.FILL * MAP_H or closer[0] > mapfocus.FILL * W)
        self.assertEqual(mapfocus.pick_zoom(SPANS, LAT, 5e7, W, MAP_H), 0)    # too big for any level: the widest

    def test_the_outline_is_a_closed_ring_round_the_spot(self):
        proj = GAME["MapProjection"]()
        proj.zoom = len(SPANS) - 1
        proj.center_lat, proj.center_lon = LAT, LON
        points = mapfocus.outline(proj.geo_to_screen, LAT, LON, 100.0)
        self.assertEqual(len(points), mapfocus.STEPS + 1)
        self.assertEqual(points[0], points[-1])
        centre = proj.geo_to_screen(LAT, LON)
        across, up = mapfocus.radius_pixels(LAT, 100.0, SPANS[-1], W)
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        self.assertAlmostEqual((max(xs) - min(xs)) / 2, across, delta=1.5)
        self.assertAlmostEqual((max(ys) - min(ys)) / 2, up, delta=1.5)
        self.assertAlmostEqual((max(xs) + min(xs)) / 2, centre[0], delta=1.5)
        self.assertAlmostEqual((max(ys) + min(ys)) / 2, centre[1], delta=1.5)
        self.assertGreater(across, up)                                        # wider than tall at 12.5 degrees
        tiny = mapfocus.outline(proj.geo_to_screen, LAT, LON, 0.5)            # drawn big enough to see
        self.assertGreater(max(p[1] for p in tiny) - min(p[1] for p in tiny), 1)

    def test_only_the_parts_inside_the_map_area_are_drawn(self):
        ring = [(10, 10), (10, 50), (700, 50), (10, 300), (10, 10)]
        kept = mapfocus.segments(ring, 16, 234, 0, 640)
        self.assertEqual(kept, [])                                            # every piece touches the outside
        inside = mapfocus.segments([(10, 20), (30, 40), (30, 240), (50, 60)], 16, 234, 0, 640)
        self.assertEqual(inside, [(10, 20, 30, 40)])

    def test_the_label_always_fits_and_says_how_to_go_back(self):
        focus = mapfocus.Focus(LAT, LON, 41.6, "POSSIBLE SKIMMER " + "x" * 80, "02aabbccddee")
        text = mapfocus.label_text(focus)
        self.assertLessEqual(len(text), 62)
        self.assertIn("within about 42 m", text)
        self.assertIn("[ESC] back to you", text)
        self.assertTrue(mapfocus.label_text(mapfocus.Focus(LAT, LON, 10.0)).startswith("estimated spot"))


class HostAdapter(unittest.TestCase):
    def test_the_view_comes_from_the_games_own_projection(self):
        app = make_app()
        with patch.dict(sys.modules, {"watchdogs.app": fake_game_module()}):
            view = host.WdgHost(app).map_view()
        self.assertEqual(view["spans"], SPANS)
        self.assertEqual((view["zoom"], view["width"], view["map_h"]), (9, W, MAP_H))
        self.assertEqual(view["to_screen"](app.proj.center_lat, app.proj.center_lon), (W // 2, HUD_TOP + MAP_H // 2))
        self.assertIsNone(host.WdgHost(SimpleNamespace()).map_view())         # a base without a map: no crash

    def test_focus_holds_a_spot_and_release_puts_the_zoom_back(self):
        app = make_app()
        adapter = host.WdgHost(app)
        self.assertTrue(adapter.map_focus(LAT, LON, 13))
        self.assertEqual(app._map_focus, (LAT, LON))
        self.assertEqual(app.proj.zoom, 13)
        adapter.map_focus(LAT + 1, LON, 12)                                   # another suspect while one is open
        self.assertEqual(app._map_focus, (LAT + 1, LON))
        adapter.map_release()
        self.assertIsNone(app._map_focus)
        self.assertEqual(app.proj.zoom, 9)                                    # the zoom from before the first one
        adapter.map_release()                                                 # twice is harmless
        self.assertEqual((app.player_lat, app.player_lon), (LAT + 0.2, LON + 0.2))   # the player was never moved
        self.assertFalse(host.WdgHost(SimpleNamespace()).map_focus(LAT, LON, 3))

    def test_a_marker_is_also_filed_the_way_the_game_draws_markers(self):
        """The game draws a marker only for what it has on record as heard this session at a
        position. host.marker files it through the game's own method, with the position given."""
        filed = []
        app = make_app()
        app._record_scan_map_observation = lambda *args, **kwargs: filed.append((args, kwargs))
        fake = fake_game_module()
        fake.BleDevice = lambda lat, lon, mac, name, rssi: SimpleNamespace(lat=lat, lon=lon, mac=mac)
        fake.WifiNetwork = lambda lat, lon, bssid, ssid, channel, rssi: SimpleNamespace(lat=lat, lon=lon, bssid=bssid)
        with patch.dict(sys.modules, {"watchdogs.app": fake, "pyxel": SimpleNamespace(frame_count=7)}):
            adapter = host.WdgHost(app)
            adapter.marker("bt", LAT, LON, "02:DE:10:00:05:01", "POSSIBLE SKIMMER HC-05", -54)
            adapter.marker("wifi", LAT, LON, "02:DE:10:00:01:01", "DEMO-NET", 36, -60)
        self.assertEqual(len(app.ble_devices), 1)
        self.assertEqual(len(app.wifi_networks), 1)
        self.assertEqual(filed[0][0], ("bt", "02:DE:10:00:05:01", "POSSIBLE SKIMMER HC-05", -54, ""))
        self.assertEqual(filed[0][1]["position"], {"lat": LAT, "lon": LON, "source": "measured"})
        self.assertEqual(filed[1][0], ("wifi", "02:DE:10:00:01:01", "DEMO-NET", -60, "36"))
        # a base without that method: the marker is still added, nothing raises
        plain = make_app()
        with patch.dict(sys.modules, {"watchdogs.app": fake, "pyxel": SimpleNamespace(frame_count=7)}):
            host.WdgHost(plain).marker("bt", LAT, LON, "02:DE:10:00:05:02", "x", -70)
        self.assertEqual(len(plain.ble_devices), 1)


class Camera(unittest.TestCase):
    """The lines in app.py's update() that aim the camera, run exactly as they are written."""

    def camera_lines(self):
        game = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "WatchDogsGame")
        update = next(n for n in game.body if isinstance(n, ast.FunctionDef) and n.name == "update")
        for index, node in enumerate(update.body):
            if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "_map_focus"):
                return [copy.deepcopy(node), copy.deepcopy(update.body[index + 1])]
        self.fail("update() no longer reads _map_focus")

    def aim(self, app):
        module = ast.fix_missing_locations(ast.Module(body=self.camera_lines(), type_ignores=[]))
        exec(compile(module, str(SOURCE), "exec"), {"self": app})
        return app.proj._target_lat, app.proj._target_lon

    def test_the_camera_follows_the_player_until_a_spot_is_held(self):
        app = make_app()
        self.assertEqual(self.aim(app), (app.player_lat, app.player_lon))     # no attribute at all: as before
        app._map_focus = None
        self.assertEqual(self.aim(app), (app.player_lat, app.player_lon))
        app._map_focus = (LAT, LON)
        self.assertEqual(self.aim(app), (LAT, LON))
        for _ in range(200):                                                  # the view eases onto the spot
            self.aim(app)
            app.proj.update()
        self.assertAlmostEqual(app.proj.center_lat, LAT, places=4)
        self.assertAlmostEqual(app.proj.center_lon, LON, places=4)
        app._map_focus = None
        self.assertEqual(self.aim(app), (app.player_lat, app.player_lon))     # and goes back to the player

    def test_the_edit_is_tagged(self):
        text = SOURCE.read_text(encoding="utf-8")
        at = text.index('_map_focus = getattr(self, "_map_focus", None)')
        self.assertIn("[bristle]", text[at - 400:at])

    def player_lines(self):
        """The lines at the top of _draw_player that say where the player figure goes."""
        game = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "WatchDogsGame")
        method = next(n for n in game.body if isinstance(n, ast.FunctionDef) and n.name == "_draw_player")
        for index, node in enumerate(method.body):
            if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "_map_focus"):
                body = [copy.deepcopy(method.body[index - 1]), copy.deepcopy(node), copy.deepcopy(method.body[index + 1])]
                body.append(ast.Return(value=ast.Tuple(elts=[ast.Name(id="cx", ctx=ast.Load()),
                                                             ast.Name(id="cy", ctx=ast.Load())], ctx=ast.Load())))
                return ast.FunctionDef(name="where", args=ast.arguments(
                    posonlyargs=[], args=[ast.arg(arg="self")], kwonlyargs=[], kw_defaults=[], defaults=[]),
                    body=body, decorator_list=[], type_params=[])
        self.fail("_draw_player no longer reads _map_focus")

    def test_the_player_is_drawn_where_the_player_is_while_a_spot_is_held(self):
        env = dict(GAME)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[self.player_lines()], type_ignores=[])),
                     str(SOURCE), "exec"), env)
        where = env["where"]
        middle = (W // 2, HUD_TOP + MAP_H // 2)
        app = make_app()
        app.proj.center_lat, app.proj.center_lon = app.player_lat, app.player_lon
        self.assertEqual(where(app), middle)                                  # no spot held: the middle, as always
        app.proj.zoom = len(SPANS) - 1
        app._map_focus = (LAT, LON)
        app.proj.center_lat, app.proj.center_lon = LAT, LON                   # the view is on the suspect
        app.player_lat, app.player_lon = LAT + 0.002, LON - 0.004             # the player is a little way off
        x, y = where(app)
        self.assertEqual((x, y), app.proj.geo_to_screen(app.player_lat, app.player_lon))
        self.assertNotEqual((x, y), middle)
        self.assertLess(y, middle[1])                                         # north of the spot: above the middle
        self.assertLess(x, middle[0])                                         # west of it: left of the middle
        app.player_lat = LAT + 0.5                                            # far off the map: not drawn at all
        self.assertIsNone(where(app))


class Plugin(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.plugin = bristle_core.BristleNodes()
        self.plugin.app = self.app
        self.plugin.host = host.WdgHost(self.app)
        self.plugin.host.marker = Mock()
        self.plugin.msg = Mock()
        self.plugin.service = SimpleNamespace(conf={"hud": "off"})
        self.plugin._overlay_active = True
        self.plugin._view = bristle_core.SUSPECTS
        self.suspect = SimpleNamespace(name="HC-05", points=[(0, LAT, LON, -61)])
        self.est = SimpleNamespace(lat=LAT, lon=LON, radius_m=38.0)
        self.patch = patch.dict(sys.modules, {"watchdogs.app": fake_game_module()})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_m_centres_the_map_marks_the_spot_and_shows_the_map(self):
        canvas = ng.Canvas()
        self.plugin._open_on_map(canvas, "02aabbccddee", self.suspect, self.est)
        self.assertEqual(self.app._map_focus, (LAT, LON))
        self.assertEqual(self.app.proj.zoom, mapfocus.pick_zoom(SPANS, LAT, 38.0, W, MAP_H))
        self.assertFalse(self.plugin.overlay_active)                          # the suspects screen is closed
        self.assertEqual(self.app._esc_consumed_frame, canvas.frame_count)
        self.plugin.host.marker.assert_called_once()
        self.assertEqual(self.plugin.host.marker.call_args.args[:3], ("bt", LAT, LON))
        self.assertIn("within about 38 m", self.plugin.msg.call_args.args[0])
        self.assertEqual(self.plugin._focus.radius_m, 38.0)
        self.plugin._overlay_active = True
        self.plugin._open_on_map(canvas, "02aabbccddee", self.suspect, self.est)
        self.plugin.host.marker.assert_called_once()                          # opened again: still one marker

    def test_the_radius_and_its_label_are_drawn_on_the_map_only(self):
        canvas = ng.Canvas()
        self.plugin._open_on_map(canvas, "02aabbccddee", self.suspect, self.est)
        self.app.proj.center_lat, self.app.proj.center_lon = LAT, LON         # the view has arrived
        drawn = []
        canvas.line = lambda x1, y1, x2, y2, colour: drawn.append((x1, y1, x2, y2, colour))
        self.plugin.draw_map_overlay(canvas, W, HUD_TOP, TERM_Y)
        ring = [d for d in drawn if d[4] == screen.C_ERR]
        self.assertEqual(len(ring), mapfocus.STEPS)                           # the whole outline
        for x1, y1, x2, y2, _colour in drawn:
            self.assertTrue(HUD_TOP <= y1 <= TERM_Y and HUD_TOP <= y2 <= TERM_Y)
            self.assertTrue(0 <= x1 <= W and 0 <= x2 <= W)
        ys = [y for d in ring for y in (d[1], d[3])]
        across, up = mapfocus.radius_pixels(LAT, 38.0, SPANS[self.app.proj.zoom], W)
        self.assertAlmostEqual((max(ys) - min(ys)) / 2, up, delta=1.5)
        self.assertGreater(up, 5)                                             # big enough to see
        self.assertEqual(len([d for d in drawn if d[4] == screen.C_WARN]), 2)   # the cross on the spot
        label = [t for t in canvas.texts if "within about 38 m" in t]
        self.assertEqual(len(label), 1)
        x, y, width, _text = next(p for p in canvas.placed if p[3] == label[0])
        self.assertGreater(x, 3 + 56 * screen.FONT_W + 6)                     # clear of the activity line (bottom left)
        self.assertLessEqual(x + width, W)
        self.assertLess(y + 8, TERM_Y)

    def test_a_radius_that_runs_off_the_map_is_cut_not_drawn_over_the_bars(self):
        canvas = ng.Canvas()
        self.plugin._open_on_map(canvas, "02aabbccddee", self.suspect, self.est)
        self.app.proj.zoom = len(SPANS) - 1
        self.app.proj.center_lat, self.app.proj.center_lon = LAT + 0.004, LON     # the spot is near the bottom edge
        self.plugin._focus = mapfocus.Focus(LAT, LON, 250.0, "POSSIBLE SKIMMER", "02aabbccddee")
        self.plugin.draw_map_overlay(canvas, W, HUD_TOP, TERM_Y)                  # the Canvas refuses anything off screen
        self.assertLess(canvas.lines, mapfocus.STEPS + 2)

    def test_escape_or_an_arrow_key_lets_the_map_follow_you_again(self):
        for key in ("KEY_ESCAPE", "KEY_UP", "KEY_LEFT"):
            with self.subTest(key=key):
                canvas = ng.Canvas()
                self.app.proj.zoom = 9
                self.app._esc_consumed_frame = -1
                self.plugin._overlay_active = True
                self.plugin._open_on_map(canvas, "02aabbccddee", self.suspect, self.est)
                self.app._esc_consumed_frame = -1
                self.plugin._focus_keys(canvas)                               # no key: nothing changes
                self.assertIsNotNone(self.app._map_focus)
                canvas.pressed = {key}
                self.plugin._focus_keys(canvas)
                self.assertIsNone(self.app._map_focus)
                self.assertIsNone(self.plugin._focus)
                self.assertEqual(self.app.proj.zoom, 9)
                # ESC is swallowed so the game does not also act on it; an arrow still pans the map
                self.assertEqual(self.app._esc_consumed_frame, canvas.frame_count if key == "KEY_ESCAPE" else -1)

    def test_without_a_map_it_still_marks_the_spot_and_says_where(self):
        self.plugin.host = host.WdgHost(SimpleNamespace(_esc_consumed_frame=-1))
        self.plugin.host.marker = Mock()
        self.plugin._open_on_map(ng.Canvas(), "02aabbccddee", self.suspect, self.est)
        self.assertTrue(self.plugin.overlay_active)                           # the screen stays, with the old message
        self.assertIn("Marked on the map at", self.plugin._status[0])
        self.assertIsNone(self.plugin._focus)

    def test_the_key_line_names_it(self):
        self.assertIn("[M] open on map", screen_more.SUSPECT_KEYS_1)
        self.assertLessEqual(len(screen_more.SUSPECT_KEYS_1), screen.MAX_CHARS)


if __name__ == "__main__":
    unittest.main()
