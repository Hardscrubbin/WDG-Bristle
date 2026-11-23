"""[merge-v3] WDGWars pair/status plugin (reply parsing, chip, key hygiene) and
the offline world basemap (a synthetic MBTiles file made here with sqlite3 +
Pillow, composed by the real code). Pairing and polling against a portal are
in tools/sim (test_v3_sim.py)."""
import io
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from watchdogs import plugin_loader
from watchdogs.tile_manager import PYXEL_PALETTE
from plugins._ng import basemap as ng_source
import plugins.ng_basemap as ng_basemap
import plugins.ng_wdgwars as ng_wdgwars

import test_v3_ng_plugins as ng

ROOT = Path(__file__).resolve().parents[1]
W, H, HUD_TOP, TERM_Y = 640, 360, 16, 234
MAP_H = TERM_Y - HUD_TOP


class SummaryTests(unittest.TestCase):
    def test_a_full_reply(self):
        state = {"ok": True, "me": {"username": "hard", "level": 12, "team": {"name": "Ghosts", "color": "#fff"}},
                 "uconsole_rank": {"position": 3, "of": 77},
                 "contracts": [{"title": "T%d" % n, "description": "D"} for n in range(9)],
                 "decay_nearby": [{"critical": 1}, {"vulnerable": True}, {}]}
        summary = ng_wdgwars.summarize(state)
        self.assertEqual((summary["username"], summary["level"], summary["team"]), ("hard", 12, "Ghosts"))
        self.assertEqual((summary["rank"], summary["rank_of"]), (3, 77))
        self.assertEqual((summary["tiles"], summary["critical"], summary["vulnerable"]), (3, 1, 1))
        self.assertEqual((len(summary["contracts"]), summary["contract_count"]), (ng_wdgwars.MAX_ROWS, 9))

    def test_hostile_and_partial_replies_never_raise(self):
        for state in (None, [], "text", {}, {"me": "x", "uconsole_rank": 5, "contracts": "none"},
                      {"me": {"username": "a\nb\x00ł" * 40, "level": "nine", "team": 7},
                       "uconsole_rank": {"position": True, "of": float("nan")},
                       "contracts": [None, 3, {"title": None}], "decay_nearby": [1, None, {"critical": "yes"}]}):
            summary = ng_wdgwars.summarize(state)
            self.assertLessEqual(len(summary["username"]), 24)
            self.assertTrue(all(32 <= ord(ch) < 127 for ch in summary["username"]))
            self.assertIn(summary["level"], (None,))
            self.assertIsNone(summary["rank"])
        self.assertIsNone(ng_wdgwars.summarize({"decay_nearby": None})["tiles"])     # null = not computed
        self.assertEqual(ng_wdgwars.summarize({"me": {"gang": "Old"}})["team"], "Old")

    def test_chip_text_and_staleness(self):
        plugin = ng_wdgwars.NgWdgWars()
        self.assertIsNone(plugin.chip_text())
        plugin._summary = ng_wdgwars.summarize({"me": {"team": {"name": "A" * 40}},
                                                 "uconsole_rank": {"position": 1234567}})
        plugin._summary_at = 1000.0
        text = plugin.chip_text(now=1010.0)
        self.assertTrue(text.startswith("WDGWars #1234567 AAAAAAAAAAAAAA"))
        self.assertLessEqual(len(text), 52)
        self.assertTrue(plugin.chip_text(now=1000.0 + ng_wdgwars.STATE_STALE + 1).endswith(" old"))
        canvas = ng.Canvas()
        plugin.draw_map_overlay(canvas, W, HUD_TOP, TERM_Y)               # fits left of the radar
        self.assertTrue(canvas.texts)

    def test_the_plugin_has_no_http_or_key_storage_of_its_own(self):
        source = (ROOT / "plugins" / "ng_wdgwars.py").read_text(encoding="utf-8")
        for forbidden in ("urlopen", "urllib", "import requests", "http.client", "import socket",
                          "write_text", " open(", "wdgwars.pl", "X-API-Key"):
            self.assertNotIn(forbidden, source)
        self.assertIn("Required Notice: Copyright (c) 2025-2026 LOCOSP", source)

    def test_no_request_without_a_configured_client(self):
        plugin = ng_wdgwars.NgWdgWars()
        portal = SimpleNamespace(fetch_game_state=Mock(), request_pair=Mock(), poll_pair=Mock(),
                                 adopt_api_key=Mock(), _api_key="", _api_url="https://x.invalid/api/upload/")
        plugin.app = SimpleNamespace(_plugins=[plugin, portal])
        plugin._next_state = 0.0
        plugin._tick_state(time.monotonic())
        portal.fetch_game_state.assert_not_called()
        self.assertIsNone(ng_wdgwars.NgWdgWars()._portal())               # no app at all: no client


def tile_png(colour):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (256, 256), colour).save(buffer, "PNG")
    return buffer.getvalue()


def make_mbtiles(path, maxzoom=2, colour_of=None):
    """A whole-world MBTiles: by default west half red-ish, east half green-ish
    (palette colours 8 and 11 undimmed), so a composed pixel says where it came from."""
    colour_of = colour_of or (lambda z, x, y: PYXEL_PALETTE[8] if x < (1 << z) / 2 else PYXEL_PALETTE[11])
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    db.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    db.executemany("INSERT INTO metadata VALUES (?, ?)", [("minzoom", "0"), ("maxzoom", str(maxzoom))])
    for z in range(maxzoom + 1):
        for x in range(1 << z):
            for y in range(1 << z):
                db.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)",
                           (z, x, (1 << z) - 1 - y, tile_png(colour_of(z, x, y))))
    db.commit()
    db.close()


class BasemapCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "maps").mkdir()
        self.path = self.root / "maps" / "basemap.mbtiles"
        dim = unittest.mock.patch.object(ng_basemap, "DIM", 1.0)
        dim.start()
        self.addCleanup(dim.stop)

    def composer(self, **kwargs):
        make_mbtiles(self.path, **kwargs)
        source = ng_source.Basemap(str(self.path))
        self.addCleanup(source.close)
        return ng_basemap.Composer(source, PYXEL_PALETTE)


class ComposeTests(BasemapCase):
    def test_zoom_follows_the_view_and_stops_when_the_data_runs_out(self):
        self.assertEqual(ng_basemap.pick_zoom(360.0, 640, 0, 6)[0], 1)        # WORLD
        self.assertEqual(ng_basemap.pick_zoom(45.0, 640, 0, 6)[0], 4)         # REGION
        zoom, upscale = ng_basemap.pick_zoom(5.0, 640, 0, 6)                  # CITY: zoom 6, blown up
        self.assertEqual(zoom, 6)
        self.assertLess(upscale, ng_basemap.MAX_UPSCALE)
        self.assertGreater(ng_basemap.pick_zoom(2.0, 640, 0, 6)[1], ng_basemap.MAX_UPSCALE)   # DISTRICT

    def test_world_view_puts_each_hemisphere_where_it_belongs(self):
        composer = self.composer()
        data = composer.compose(0.0, 0.0, 360.0, 360.0 * MAP_H / W, W, MAP_H)
        self.assertEqual(len(data), W * MAP_H)
        middle = (MAP_H // 2) * W
        self.assertEqual(data[middle + 10], 8)                                # far west: the west colour
        self.assertEqual(data[middle + W - 10], 11)                           # far east
        self.assertEqual(data[middle + W // 2 - 2], 8)
        self.assertEqual(data[middle + W // 2 + 2], 11)
        self.assertLessEqual(set(data), set(range(16)))                       # only pyxel colours

    def test_the_map_wraps_at_the_antimeridian(self):
        composer = self.composer()
        data = composer.compose(0.0, 180.0, 90.0, 90.0 * MAP_H / W, W, MAP_H)     # centred on 180 E
        middle = (MAP_H // 2) * W
        self.assertEqual(data[middle + 10], 11)                               # 135 E..180: east half
        self.assertEqual(data[middle + W - 10], 8)                            # 180..135 W: west half

    def test_mercator_rows_and_empty_poles(self):
        north = lambda z, x, y: PYXEL_PALETTE[12] if y < (1 << z) / 2 else PYXEL_PALETTE[9]
        composer = self.composer(colour_of=north)
        lat_span = 360.0 * MAP_H / W                                          # ~122 degrees tall
        data = composer.compose(40.0, 0.0, 360.0, lat_span, W, MAP_H)         # top edge at 101 N
        column = W // 2
        rows = [data[row * W + column] for row in range(MAP_H)]
        self.assertEqual(rows[0], 0)                                          # beyond 85 N: nothing
        equator_row = int((40.0 + lat_span / 2) / lat_span * MAP_H)
        self.assertEqual(rows[equator_row - 8], 12)                           # just north of the equator
        self.assertEqual(rows[equator_row + 8], 9)                            # just south of it
        self.assertEqual(rows[int((40.0 + lat_span / 2 - 60.0) / lat_span * MAP_H)], 12)   # 60 N

    def test_views_the_data_cannot_serve_return_nothing(self):
        composer = self.composer(maxzoom=1)
        self.assertIsNone(composer.compose(50.0, 19.0, 2.0, 2.0 * MAP_H / W, W, MAP_H))    # far too close
        empty = self.root / "empty.mbtiles"
        db = sqlite3.connect(empty)
        db.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        db.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
        db.execute("INSERT INTO tiles VALUES (0, 0, 0, ?)", (b"not a png",))
        db.commit()
        db.close()
        source = ng_source.Basemap(str(empty))
        self.addCleanup(source.close)
        self.assertIsNone(ng_basemap.Composer(source, PYXEL_PALETTE).compose(0, 0, 360.0, 122.0, W, MAP_H))

    def test_tile_cache_is_bounded(self):
        composer = self.composer(maxzoom=3)
        for x in range(8):
            for y in range(8):
                composer.tile(3, x, y)
        self.assertLessEqual(len(composer._tiles), ng_basemap.TILE_CACHE)


class FakeImage:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.data = bytearray(width * height)

    def data_ptr(self):
        return self.data


class FakePyxel:
    Image = FakeImage

    def __init__(self):
        self.blits = []

    def clip(self, *rect):
        pass

    def blt(self, x, y, image, u, v, w, h):
        self.blits.append((x, y, w, h, bytes(image.data[:8])))


class PluginTests(BasemapCase):
    def plugin(self, with_file=True):
        if with_file:
            make_mbtiles(self.path)
        plugin = ng_basemap.NgBasemap()
        self.said = []
        plugin.term = self.said.append
        plugin.on_load(SimpleNamespace(_app_dir=str(self.root)))
        self.addCleanup(plugin.on_unload)
        return plugin

    @staticmethod
    def proj(lat=0.0, lon=0.0, lon_span=360.0):
        return SimpleNamespace(center_lat=lat, center_lon=lon, lon_span=lon_span,
                               lat_span=lon_span * MAP_H / W)

    def draw_until_drawn(self, plugin, pyxel, proj, timeout=10.0):
        deadline = time.perf_counter() + timeout
        slowest = 0.0
        while time.perf_counter() < deadline:
            started = time.perf_counter()
            drawn = plugin.draw_basemap(pyxel, proj, W, H, HUD_TOP, TERM_Y)
            slowest = max(slowest, time.perf_counter() - started)
            if drawn:
                return slowest
            time.sleep(0.005)
        self.fail("the basemap was never drawn")

    def test_without_the_file_nothing_changes(self):
        plugin = self.plugin(with_file=False)
        self.assertFalse(plugin.available)
        self.assertFalse(plugin.draw_basemap(FakePyxel(), self.proj(), W, H, HUD_TOP, TERM_Y))
        self.assertIn("no offline world basemap file", self.said[0])
        self.assertEqual(plugin.menu_items(), [])

    def test_the_file_is_found_and_drawn_off_the_game_thread(self):
        plugin = self.plugin()
        self.assertTrue(plugin.available)
        self.assertIn("offline world basemap", self.said[0])
        pyxel = FakePyxel()
        self.assertFalse(plugin.draw_basemap(pyxel, self.proj(), W, H, HUD_TOP, TERM_Y))   # first frame: coastline
        slowest = self.draw_until_drawn(plugin, pyxel, self.proj())
        self.assertLess(slowest, 0.25)                    # the game thread only copies one image
        x, y, w, h, _first = pyxel.blits[-1]
        self.assertEqual((x, y, w, h), (0, HUD_TOP, W, MAP_H))
        self.assertEqual(plugin._image.data[(MAP_H // 2) * W + 10], 8)

    def test_a_small_pan_reuses_the_image_shifted_until_the_new_one_is_ready(self):
        plugin = self.plugin()
        pyxel = FakePyxel()
        self.draw_until_drawn(plugin, pyxel, self.proj())
        step = 360.0 / W                                  # one screen pixel
        moved = self.proj(lat=-3 * step, lon=5 * step)
        plugin._busy = True                               # as if the worker were still composing
        self.assertTrue(plugin.draw_basemap(pyxel, moved, W, H, HUD_TOP, TERM_Y))
        self.assertEqual(pyxel.blits[-1][:2], (-5, HUD_TOP - 3))
        plugin._busy = False
        other_zoom = self.proj(lon_span=180.0)
        self.assertFalse(plugin.draw_basemap(pyxel, other_zoom, W, H, HUD_TOP, TERM_Y))   # other zoom: not yet
        self.draw_until_drawn(plugin, pyxel, other_zoom)

    def test_too_close_views_fall_back_to_c5_and_are_not_recomposed_every_frame(self):
        plugin = self.plugin()
        pyxel = FakePyxel()
        close = self.proj(lat=50.0, lon=19.0, lon_span=0.5)
        deadline = time.perf_counter() + 5
        while time.perf_counter() < deadline and not plugin._empty:
            self.assertFalse(plugin.draw_basemap(pyxel, close, W, H, HUD_TOP, TERM_Y))
            time.sleep(0.005)
        self.assertTrue(plugin._empty)
        plugin._worker = Mock(side_effect=AssertionError("recomposed a view known to be empty"))
        for _ in range(5):
            self.assertFalse(plugin.draw_basemap(pyxel, close, W, H, HUD_TOP, TERM_Y))

    def test_a_broken_file_or_backend_disables_it_quietly(self):
        import gc
        self.addCleanup(gc.collect)       # WDGoNG's reader drops a failed connection unclosed (Windows locks it)
        self.path.write_bytes(b"this is not sqlite")
        plugin = ng_basemap.NgBasemap()
        plugin.term = Mock()
        plugin.on_load(SimpleNamespace(_app_dir=str(self.root)))
        self.assertFalse(plugin.draw_basemap(FakePyxel(), self.proj(), W, H, HUD_TOP, TERM_Y))
        del plugin
        gc.collect()
        self.path.unlink()
        good = self.plugin()
        broken = SimpleNamespace(Image=Mock(side_effect=RuntimeError("no image")), clip=Mock(), blt=Mock())
        deadline = time.perf_counter() + 5
        while time.perf_counter() < deadline and not good._failed:
            good.draw_basemap(broken, self.proj(), W, H, HUD_TOP, TERM_Y)
            time.sleep(0.005)
        self.assertTrue(good._failed)
        self.assertFalse(good.draw_basemap(broken, self.proj(), W, H, HUD_TOP, TERM_Y))

    def test_ng_source_is_unchanged_and_declared(self):
        import hashlib
        digest = hashlib.sha256((ROOT / "plugins" / "_ng" / "basemap.py").read_bytes()).hexdigest()
        self.assertEqual(digest[:16].upper(), "B4DEF0C6C6D3676F")
        notice = (ROOT / "plugins" / "_ng" / "NOTICE").read_text(encoding="utf-8")
        for needle in ("basemap.py", "B4DEF0C6C6D3676F", "plugins/ng_basemap.py", "plugins/ng_wdgwars.py"):
            self.assertIn(needle, notice)
        kinds = [type(plugin).__name__ for plugin in plugin_loader.discover_plugins()]
        self.assertEqual((kinds.count("NgBasemap"), kinds.count("NgWdgWars")), (1, 1))

    def test_app_asks_plugins_for_the_basemap_and_the_map_chip(self):
        source = (ROOT / "watchdogs" / "app.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('getattr(_p, "draw_basemap", None)'), 1)
        self.assertEqual(source.count('getattr(_p, "draw_map_overlay", None)'), 1)
        self.assertIn("if not tiles_drawn and not basemap_drawn:\n            self._draw_coastlines()", source)
        self.assertIn("self._draw_map_attribution(tiles_drawn)", source)      # the tile credit: tiles only


if __name__ == "__main__":
    unittest.main()
