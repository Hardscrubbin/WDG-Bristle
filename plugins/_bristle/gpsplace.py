"""Place finds by time using the core's GPS track.

Feed it fresh, measured fixes (WDG v3's wardrive_sample path). Each find carries its node timestamp;
`place(t)` returns the position at that time, interpolated between the two surrounding fixes when
they're close enough, else the nearest fix within the allowed age, else None (hold or drop the find,
following WDG's Kismet-plugin rules). Never returns 0,0 or an invalid coordinate.
"""
from __future__ import annotations

import bisect
import math
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class Fix:
    t: float          # unix seconds (UTC)
    lat: float
    lon: float
    alt: float | None = None
    acc: float | None = None     # horizontal accuracy, metres


def valid(lat: float, lon: float) -> bool:
    return (math.isfinite(lat) and math.isfinite(lon) and -90.0 <= lat <= 90.0
            and -180.0 <= lon <= 180.0 and not (lat == 0.0 and lon == 0.0))


class GpsTrack:
    def __init__(self, keep_seconds: float = 3 * 3600, max_gap_s: float = 10.0,
                 max_nearest_s: float = 5.0) -> None:
        self.keep = keep_seconds        # how much history to keep (long enough for node backlogs)
        self.max_gap = max_gap_s        # interpolate only between fixes this close together
        self.max_nearest = max_nearest_s  # else use the nearest fix only if this close in time
        self._t: list[float] = []
        self._f: list[Fix] = []
        self._lock = threading.Lock()

    def add(self, fix: Fix) -> bool:
        if not valid(fix.lat, fix.lon) or not math.isfinite(fix.t):
            return False
        with self._lock:
            i = bisect.bisect_right(self._t, fix.t)
            if i > 0 and self._t[i - 1] == fix.t:
                return False                       # replayed/duplicate report
            self._t.insert(i, fix.t)
            self._f.insert(i, fix)
            cutoff = self._t[-1] - self.keep
            j = bisect.bisect_left(self._t, cutoff)
            if j:
                del self._t[:j]
                del self._f[:j]
        return True

    def latest_time(self) -> float | None:
        with self._lock:
            return self._t[-1] if self._t else None

    def place(self, t: float) -> Fix | None:
        with self._lock:
            if not self._t:
                return None
            i = bisect.bisect_left(self._t, t)
            before = self._f[i - 1] if i > 0 else None
            after = self._f[i] if i < len(self._f) else None
        if after is not None and after.t == t:
            return after
        if before is not None and after is not None and after.t - before.t <= self.max_gap:
            w = (t - before.t) / (after.t - before.t)
            lat = before.lat + (after.lat - before.lat) * w
            lon = before.lon + (after.lon - before.lon) * w
            return Fix(t, lat, lon, None, max(before.acc or 0, after.acc or 0) or None)
        best = min((f for f in (before, after) if f is not None), key=lambda f: abs(f.t - t))
        return best if abs(best.t - t) <= self.max_nearest else None
