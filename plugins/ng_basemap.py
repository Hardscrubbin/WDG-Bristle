"""Offline whole-world basemap under upstream's map markers.

After WDGoNG 0.10.0's "Offline whole-world ctOS basemap" (watchdogs/basemap.py,
copied unchanged to plugins/_ng/basemap.py, and the tile drawing in
wdgong_ui/main.py).
Required Notice: Copyright (c) 2025-2026 LOCOSP
PolyForm Noncommercial License 1.0.0 (see plugins/_ng/NOTICE). This build is
for personal, noncommercial use.

What it does. upstream draws downloaded street tiles (Stadia / MapTiler) from the
DISTRICT zoom inwards and only a coastline outline further out. WDGoNG ships a
whole-world map as one MBTiles file (zoom 0-6). When that file is present this
plugin draws it wherever upstream has no downloaded tiles for the view -- WORLD down
to CITY zoom -- under the grid, turf, markers and the player. Downloaded or
cached online tiles always win: app.py only asks for the basemap when its own
tile renderer drew nothing.

THE DATA FILE IS NOT IN THIS BUILD. WDGoNG's basemap.mbtiles (about 29 MB) is
not part of the WDGoNG 0.10.0 source archive this build was made from (it is
git-ignored there). Copy it from a WDGoNG install,
    wdgong_ui/assets/map/basemap.mbtiles
to one of
    <game folder>/maps/basemap.mbtiles          (kept across re-installs)
    <game folder>/assets/map/basemap.mbtiles
    ~/.watchdogs/basemap.mbtiles
and restart. Without the file nothing changes: upstream's coastline is drawn as
before.

How. WDGoNG uploads each tile to the GPU (raylib). pyxel has 16 colours and no
GPU tiles, so here a worker thread composes the visible map area once per
view: it reads the needed tiles (sqlite3, read-only), reduces them to upstream's
16-colour palette (Pillow), re-projects them from Web Mercator to upstream's
equirectangular map row by row, and hands the game thread one finished
640-wide image. The game thread only copies that image; it never decodes a
tile. While a new view is being composed the previous one is shown shifted to
where it belongs, so panning never blanks the map.

No menu entry: it is automatic. The terminal says at launch which file is used,
or that there is none.
"""
import collections
import io
import math
import os
import threading
from pathlib import Path

from plugins.plugin_base import PluginBase
from plugins._ng import basemap as ng_basemap

FILE_NAME = "basemap.mbtiles"
TILE = 256
MAX_UPSCALE = 4.0           # past this the zoom-6 data is only blocks: leave it to the coastline
TILE_CACHE = 48             # reduced tiles kept (64 KB each)
try:
    DIM = max(0.2, min(1.0, float(os.environ.get("WDG_BASEMAP_DIM", "0.6"))))
except ValueError:
    DIM = 0.6               # the map is a backdrop: darken it so markers stay readable


def candidate_paths(app_dir) -> list:
    root = Path(__file__).resolve().parent.parent
    home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    sudo_user = os.environ.get("SUDO_USER")
    paths = [Path(app_dir) / "maps" / FILE_NAME, root / "maps" / FILE_NAME,
             root / "assets" / "map" / FILE_NAME, home / ".watchdogs" / FILE_NAME]
    if sudo_user:
        paths.append(Path("/home") / sudo_user / ".watchdogs" / FILE_NAME)
    seen, ordered = set(), []
    for path in paths:
        if str(path) not in seen:
            seen.add(str(path))
            ordered.append(path)
    return ordered


def mercator_y(lat: float, zoom: int) -> float:
    """World pixel row of ``lat`` at ``zoom`` (Web Mercator, 256 px tiles)."""
    lat = max(-ng_basemap.WEB_MERC_LAT, min(ng_basemap.WEB_MERC_LAT, lat))
    return (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * TILE * (1 << zoom)


def pick_zoom(lon_span: float, width: int, minzoom: int, maxzoom: int):
    """(tile zoom, upscale factor) for a view ``lon_span`` degrees wide."""
    degrees_per_pixel = lon_span / width
    exact = math.log2(360.0 / (TILE * degrees_per_pixel))
    zoom = max(minzoom, min(maxzoom, int(round(exact))))
    return zoom, (360.0 / (TILE * (1 << zoom))) / degrees_per_pixel


class Composer:
    """Tiles -> one palette-index image for a view. Worker thread only."""

    def __init__(self, source, palette):
        from PIL import Image
        self._image = Image
        self.source = source
        flat = []
        for red, green, blue in palette[:16]:
            flat += [red, green, blue]
        flat += flat[:3] * (256 - 16)                 # pad; indices >= 16 are folded back below
        self._palette = Image.new("P", (1, 1))
        self._palette.putpalette(flat)
        self._fold = [index if index < 16 else 0 for index in range(256)]
        self._dim = [int(value * DIM) for value in range(256)] * 3
        self._tiles = collections.OrderedDict()

    def tile(self, zoom, x, y):
        """The tile as a 256x256 "P" image of palette indices, or None."""
        key = (zoom, x, y)
        if key in self._tiles:
            self._tiles.move_to_end(key)
            return self._tiles[key]
        data = self.source.tile(zoom, x, y)
        image = None
        if data:
            try:
                rgb = self._image.open(io.BytesIO(data)).convert("RGB")
                if rgb.size != (TILE, TILE):
                    rgb = rgb.resize((TILE, TILE), self._image.NEAREST)
                if DIM < 1.0:
                    rgb = rgb.point(self._dim)
                image = rgb.quantize(palette=self._palette, dither=self._image.Dither.NONE).point(self._fold)
            except Exception:
                image = None
        self._tiles[key] = image
        if len(self._tiles) > TILE_CACHE:
            self._tiles.popitem(last=False)
        return image

    def compose(self, center_lat, center_lon, lon_span, lat_span, width, height):
        """bytes(width * height) of palette indices for the view, or None when
        the view is too close for the data or no tile covers it."""
        zoom, upscale = pick_zoom(lon_span, width, self.source.minzoom, self.source.maxzoom)
        if upscale > MAX_UPSCALE:
            return None
        world = TILE * (1 << zoom)
        count = 1 << zoom
        left = (center_lon - lon_span / 2.0 + 180.0) / 360.0 * world
        right = (center_lon + lon_span / 2.0 + 180.0) / 360.0 * world
        top_lat = center_lat + lat_span / 2.0
        lat_step = lat_span / height
        top = mercator_y(min(top_lat, ng_basemap.WEB_MERC_LAT), zoom)
        bottom = mercator_y(max(top_lat - lat_span, -ng_basemap.WEB_MERC_LAT), zoom)
        tx0, tx1 = math.floor(left / TILE), math.floor((right - 1e-9) / TILE)
        ty0 = max(0, min(count - 1, math.floor(top / TILE)))
        ty1 = max(0, min(count - 1, math.floor((bottom - 1e-9) / TILE)))
        if tx1 - tx0 > 12 or ty1 - ty0 > 12:
            return None
        mosaic = self._image.new("P", ((tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE), 0)
        found = False
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                tile = self.tile(zoom, tx % count, ty)     # the world repeats east-west
                if tile is not None:
                    mosaic.paste(tile, ((tx - tx0) * TILE, (ty - ty0) * TILE))
                    found = True
        if not found:
            return None
        out = self._image.new("P", (width, height), 0)
        x0, x1 = left - tx0 * TILE, right - tx0 * TILE
        limit = mosaic.size[1] - 1
        for row in range(height):
            lat = top_lat - (row + 0.5) * lat_step
            if abs(lat) > ng_basemap.WEB_MERC_LAT:
                continue                                   # beyond the map's poles: stays empty
            y = min(limit, max(0, int(mercator_y(lat, zoom)) - ty0 * TILE))
            strip = mosaic.resize((width, 1), self._image.NEAREST, box=(x0, y, x1, y + 1))
            out.paste(strip, (0, row))
        return out.tobytes()


class NgBasemap(PluginBase):
    NAME = "Offline World Basemap (WDGoNG)"
    VERSION = "1.0"
    AUTHOR = "LOCOSP (WDGoNG 0.10.0 feature); plugin for WDG-Bristle"

    def __init__(self):
        super().__init__()
        self.path = None
        self._source = None
        self._composer = None
        self._lock = threading.Lock()
        self._busy = False
        self._result = None           # (key, bytes or None) from the worker
        self._shown_key = None        # key of the image now in self._image
        self._image = None            # pyxel.Image (width x map height)
        self._empty = set()           # views known to have no basemap coverage
        self._failed = False

    def on_load(self, app) -> None:
        self.app = app
        app_dir = getattr(app, "_app_dir", "") or str(Path(__file__).resolve().parent.parent)
        for path in candidate_paths(app_dir):
            source = ng_basemap.Basemap(str(path)) if path.is_file() else None
            if source is not None and source.ok:
                self.path, self._source = path, source
                break
            if source is not None:
                source.close()
        try:
            if self._source is not None:
                self.term("[MAP] offline world basemap: %s (zoom %d-%d)"
                          % (self.path, self._source.minzoom, self._source.maxzoom))
            else:
                self.term("[MAP] no offline world basemap file (maps/%s) -- coastline only; "
                          "see plugins/ng_basemap.py" % FILE_NAME)
        except Exception:
            pass

    def on_unload(self) -> None:
        source, self._source = self._source, None
        if source is not None and not self._busy:
            source.close()

    @property
    def available(self) -> bool:
        return self._source is not None and not self._failed

    # ------------------------------------------------------------------
    @staticmethod
    def view_key(proj, width, map_h):
        """The view in whole screen pixels: the image only changes when this does."""
        lon_span, lat_span = proj.lon_span, proj.lat_span
        return (lon_span, int(round(proj.center_lon / (lon_span / width))),
                int(round(proj.center_lat / (lat_span / map_h))))

    def _worker(self, key, view) -> None:
        data = None
        try:
            if self._composer is None:
                from watchdogs.tile_manager import PYXEL_PALETTE
                self._composer = Composer(self._source, PYXEL_PALETTE)
            data = self._composer.compose(*view)
        except Exception:
            self._failed = True               # Pillow missing, a broken file...: coastline from now on
        finally:
            self._result = (key, data)
            self._busy = False

    def _upload(self, pyxel, width, map_h, data) -> bool:
        """Copy palette-index bytes into the cached pyxel image (game thread)."""
        try:
            if self._image is None:
                self._image = pyxel.Image(width, map_h)
            target = self._image.data_ptr()
            try:
                import ctypes
                ctypes.memmove(target, data, len(data))
            except Exception:
                target[0:len(data)] = data
            return True
        except Exception:
            self._failed = True
            self._image = None
            return False

    def draw_basemap(self, pyxel, proj, width, height, hud_top, term_y) -> bool:
        """Called by app.py when its tile renderer drew nothing. True when the
        basemap was drawn (app.py then skips the coastline)."""
        if not self.available:
            return False
        map_h = term_y - hud_top
        key = self.view_key(proj, width, map_h)
        result = self._result
        if result is not None:
            self._result = None
            done_key, data = result
            if data is None:
                self._empty.add(done_key)
                if len(self._empty) > 256:
                    self._empty.clear()
            elif self._upload(pyxel, width, map_h, data):
                self._shown_key = done_key
        if key != self._shown_key and key not in self._empty and not self._busy:
            self._busy = True
            lon_span, lat_span = proj.lon_span, proj.lat_span
            # the centre the key stands for, so image and key agree to the pixel
            view = (key[2] * (lat_span / map_h), key[1] * (lon_span / width), lon_span, lat_span,
                    width, map_h)
            try:
                threading.Thread(target=self._worker, args=(key, view), name="ng-basemap",
                                 daemon=True).start()
            except RuntimeError:
                self._busy = False
        if self._image is None or self._shown_key is None or self._shown_key[0] != key[0]:
            return False              # nothing for this zoom yet: coastline this frame
        if key in self._empty:
            return False
        # the composed view, moved to where it belongs in the current view
        dx = self._shown_key[1] - key[1]
        dy = key[2] - self._shown_key[2]
        if abs(dx) >= width or abs(dy) >= map_h:
            return False
        pyxel.clip(0, hud_top, width, map_h)
        pyxel.blt(dx, hud_top + dy, self._image, 0, 0, width, map_h)
        pyxel.clip()
        return True
