# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""[bristle] "Open a suspect on the map": which zoom shows its confidence radius, and the radius as
points on the screen.

Plain numbers in, plain numbers out: no drawing library and no game object here. The host adapter
(host.py) says how the game's map turns a position into a screen point; the screen file
(screen_more.py) draws the points this module hands back.

The game's map is a plain latitude/longitude grid: one degree of longitude is as wide on the screen
as one degree of latitude is tall. A circle on the ground is therefore wider than tall on the map
(by 1 / cos(latitude)), and that is how the radius is drawn.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

METRES_PER_DEGREE = 111320.0          # one degree of latitude
FILL = 0.40                           # the radius may take this part of the map's height / width
MIN_RADIUS_M = 5.0                    # a tighter estimate is still drawn this big, so it can be seen
STEPS = 36                            # points on the outline


@dataclass(frozen=True)
class Focus:
    """A spot the map is held on, with the radius to draw round it."""
    lat: float
    lon: float
    radius_m: float
    label: str = ""
    key: str = ""                     # what it belongs to (a suspect's address)


def degrees(lat: float, radius_m: float) -> tuple:
    """(degrees of latitude, degrees of longitude) that ``radius_m`` spans at this latitude."""
    radius = max(float(radius_m), 0.0)
    shrink = max(math.cos(math.radians(max(-85.0, min(85.0, float(lat))))), 0.05)
    return radius / METRES_PER_DEGREE, radius / (METRES_PER_DEGREE * shrink)


def radius_pixels(lat: float, radius_m: float, lon_span: float, width_px: int) -> tuple:
    """(across, up) in pixels on a map that shows ``lon_span`` degrees across ``width_px``."""
    d_lat, d_lon = degrees(lat, radius_m)
    scale = width_px / float(lon_span)
    return d_lon * scale, d_lat * scale


def pick_zoom(lon_spans, lat: float, radius_m: float, width_px: int, map_h_px: int) -> int:
    """The index of the closest zoom level at which the whole radius still fits on the map with
    room round it. ``lon_spans``: degrees shown across the screen at each level, widest first.
    A radius too big for every level gets the widest one."""
    radius = max(float(radius_m), MIN_RADIUS_M)
    best = 0
    for index, span in enumerate(lon_spans):
        across, up = radius_pixels(lat, radius, span, width_px)
        if across <= FILL * width_px and up <= FILL * map_h_px:
            best = index
    return best


def outline(to_screen, lat: float, lon: float, radius_m: float, steps: int = STEPS) -> list:
    """The radius as a closed list of screen points [(x, y), ...]; the last point is the first.
    ``to_screen(lat, lon)`` is the map's own conversion."""
    d_lat, d_lon = degrees(lat, max(float(radius_m), MIN_RADIUS_M))
    points = []
    for step in range(steps + 1):
        angle = 2.0 * math.pi * (step % steps) / steps
        x, y = to_screen(lat + d_lat * math.sin(angle), lon + d_lon * math.cos(angle))
        points.append((int(x), int(y)))
    return points


def segments(points, top: int, bottom: int, left: int = 0, right: int = 640) -> list:
    """[(x1, y1, x2, y2)] for the parts of an outline that lie inside the map area, so the radius
    is never drawn over the bars above and below the map."""
    out = []
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if all(left <= x <= right for x in (x1, x2)) and all(top <= y <= bottom for y in (y1, y2)):
            out.append((x1, y1, x2, y2))
    return out


def label_text(focus: Focus, limit: int = 62) -> str:
    """The line shown with the radius."""
    name = (focus.label or "estimated spot").strip()
    tail = ": within about %d m  [ESC] back to you" % round(max(focus.radius_m, 0.0))
    room = max(8, limit - len(tail))
    if len(name) > room:
        name = name[:room - 2] + ".."
    return name + tail
