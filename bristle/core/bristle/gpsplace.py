"""Place finds by time using the core's GPS track.

Feed it fresh, measured fixes (WDG v3's wardrive_sample path). Each find carries its node timestamp;
`place(t)` returns the position at that time, interpolated between the two surrounding fixes when
they're close enough, else the nearest fix within the allowed age, else None (hold or drop the find,
following WDG's Kismet-plugin rules). Never returns 0,0 or an invalid coordinate.

Times. A node's clock is this core's clock, as it was when the core last set it. If the core's
clock is corrected mid-drive (GPS or NTP), finds stamped before the correction and fixes measured
after it would no longer line up. So the track is not kept on the wall clock: every fix is stored
on "track time", which runs with the monotonic clock and never jumps, and ClockMap remembers which
wall-clock offset was in force when. A find's time goes through ClockMap.to_track() first.
Track time starts out equal to the wall clock, so a track saved to disk still means something to
a later run of the game.
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


@dataclass
class Epoch:
    """A stretch of track time during which the wall clock read (track time + delta)."""
    start: float
    end: float
    delta: float


class ClockMap:
    """Turns wall-clock times (what a node stamps on a find) into track time.

    note(mono, wall) is called often with both clocks. While they move together nothing happens.
    When the wall clock jumps by more than ``jump_s`` a new epoch starts. to_track(t) looks for the
    epoch whose wall-clock span holds t; a node keeps the old clock for a moment after a jump (until
    the core's TIME reaches it), so an epoch stays valid for ``grace_s`` past its end. When two
    epochs both fit (the clock was set back), the later one wins.
    """

    def __init__(self, jump_s: float = 2.0, grace_s: float = 6.0, keep_s: float = 36 * 3600.0,
                 history: list | None = None) -> None:
        self.jump_s, self.grace_s, self.keep_s = jump_s, grace_s, keep_s
        self.epochs: list[Epoch] = []
        for item in history or []:
            try:
                self.epochs.append(Epoch(float(item[0]), float(item[1]), float(item[2])))
            except (TypeError, ValueError, IndexError):
                continue
        self._base: float | None = None      # track time = monotonic + base, for this run
        self.jumps = 0

    def track_time(self, mono: float) -> float:
        if self._base is None:
            raise RuntimeError("note() has not been called yet")
        return mono + self._base

    def note(self, mono: float, wall: float) -> bool:
        """True if the wall clock just jumped (the caller then sends every node the time again)."""
        if self._base is None:
            self._base = wall - mono         # this run's track time starts on the wall clock
            start = wall
            if self.epochs and self.epochs[-1].end > start:
                # the clock was set back while the game was off: keep this run's times above the
                # saved track's, so the two can never be mixed up
                self._base += self.epochs[-1].end - start + 1.0
                start = self.epochs[-1].end + 1.0
            self.epochs.append(Epoch(start, start, wall - start))
            return False
        now = mono + self._base
        current = self.epochs[-1]
        delta = wall - now
        current.end = now
        if abs(delta - current.delta) <= self.jump_s:
            return False
        self.epochs.append(Epoch(now, now, delta))
        self.jumps += 1
        cutoff = now - self.keep_s
        self.epochs = [e for e in self.epochs if e.end >= cutoff]
        return True

    def to_track(self, wall_time: float) -> float | None:
        for epoch in reversed(self.epochs):
            when = wall_time - epoch.delta
            if epoch.start - 1.0 <= when <= epoch.end + self.grace_s:
                return when
        return None

    def snapshot(self) -> list:
        return [[round(e.start, 3), round(e.end, 3), round(e.delta, 3)] for e in self.epochs]
