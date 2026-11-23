"""[merge-v3] Pure helpers for the Kismet bridge: datasource states and the
channel split between two Wi-Fi adapters. No I/O; plugins/kismet_wardrive.py
does the HTTP.

Channel split (two Wi-Fi adapters capturing at once, e.g. the Alfa
AWUS036AXML and an AC1200): by default Kismet makes every adapter hop over
every channel it supports, so both radios spend most of their time on the
same bands. "band" split gives the 2.4 GHz channels to one adapter and the 5
and 6 GHz channels to the other, so every band is watched all the time:

  * the adapter with the wider range (6 GHz, else more channels) takes 5/6 GHz,
    the other takes 2.4 GHz;
  * a channel only one adapter supports always stays with that adapter;
  * the plan is only made from channel names Kismet itself reported for each
    adapter, so nothing is asked of an adapter that it did not offer;
  * every channel that was being hopped before is still hopped by someone.

With one adapter, three or more, or identical single-band adapters, nothing
is changed (Kismet's own hopping stays as it is).
"""
import re

BAND_24, BAND_5, BAND_6 = "2.4", "5", "6"
_CHANNEL_RE = re.compile(r"^(\d+)")


def channel_band(name):
    """Band of a Kismet channel name: "1", "6HT40+", "36VHT80", "1W6e"... or
    None when it cannot be read."""
    if not isinstance(name, str):
        return None
    match = _CHANNEL_RE.match(name.strip())
    if not match:
        return None
    number = int(match.group(1))
    if "W6" in name.upper():
        return BAND_6
    if 1 <= number <= 14:
        return BAND_24
    if 32 <= number <= 177:
        return BAND_5
    return None


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def parse_sources(payload) -> list:
    """Kismet's /datasource/all_sources.json as plain dicts. Unknown shapes
    give an empty list, never an exception."""
    rows = []
    if not isinstance(payload, list):
        return rows
    for item in payload:
        if not isinstance(item, dict):
            continue
        driver = item.get("kismet.datasource.type_driver")
        if isinstance(driver, dict):
            driver = driver.get("kismet.datasource.driver.type")
        driver = _text(driver).lower()
        interface = _text(item.get("kismet.datasource.interface"))
        capture = _text(item.get("kismet.datasource.capture_interface"))
        channels = [c for c in (item.get("kismet.datasource.channels") or []) if isinstance(c, str)]
        hop = [c for c in (item.get("kismet.datasource.hop_channels") or []) if isinstance(c, str)]
        if "bluetooth" in driver or "btle" in driver or interface.startswith("hci"):
            kind = "bt"
        elif "wifi" in driver or any(channel_band(c) for c in channels + hop):
            kind = "wifi"
        else:
            kind = "other"
        rate = item.get("kismet.datasource.hop_rate")
        rows.append(dict(
            uuid=_text(item.get("kismet.datasource.uuid")),
            name=_text(item.get("kismet.datasource.name")) or interface or capture,
            interface=interface or capture, kind=kind,
            running=bool(item.get("kismet.datasource.running")),
            error=bool(item.get("kismet.datasource.error")),
            reason=_text(item.get("kismet.datasource.error_reason"))[:120],
            channels=channels, hop_channels=hop,
            hop_rate=float(rate) if isinstance(rate, (int, float)) and not isinstance(rate, bool)
            and rate > 0 else 5.0))
    return rows


def _bands(channels) -> dict:
    found = {}
    for channel in channels:
        band = channel_band(channel)
        if band:
            found.setdefault(band, []).append(channel)
    return found


def plan_split(sources) -> tuple:
    """({uuid: [channels]}, why) for the running Wi-Fi sources.

    An empty plan means "leave Kismet's hopping alone"; ``why`` says so in a
    form fit for the terminal."""
    wifi = [s for s in sources if s["kind"] == "wifi" and s["running"] and s["uuid"]]
    if len(wifi) < 2:
        return {}, "one Wi-Fi adapter capturing: no split needed"
    if len(wifi) > 2:
        return {}, "%d Wi-Fi adapters capturing: left to Kismet's own hopping" % len(wifi)
    lists = [list(s["hop_channels"] or s["channels"]) for s in wifi]
    if not lists[0] or not lists[1]:
        return {}, "Kismet has not reported the adapters' channels"
    bands = [_bands(channels) for channels in lists]

    def reach(index):
        return (BAND_6 in bands[index], len([b for b in (BAND_5, BAND_6) if b in bands[index]]),
                len(lists[index]))
    wide, narrow = (0, 1) if reach(0) >= reach(1) else (1, 0)
    if BAND_24 not in bands[narrow] or not (set(bands[wide]) - {BAND_24}):
        return {}, "the adapters' bands do not allow a 2.4 / 5-6 GHz split"
    plan = {wide: [c for c in lists[wide] if channel_band(c) != BAND_24],
            narrow: [c for c in lists[narrow] if channel_band(c) == BAND_24]}
    # A channel only one adapter can tune stays with it, and nothing that was
    # hopped before is dropped (names that could not be classified included).
    for index in (wide, narrow):
        other = narrow if index == wide else wide
        for channel in lists[index]:
            if channel not in plan[index] and channel not in plan[other]:
                plan[index].append(channel)
    if not plan[wide] or not plan[narrow]:
        return {}, "a split would leave one adapter with no channels"
    why = "%s: %d channels (5/6 GHz), %s: %d channels (2.4 GHz)" % (
        wifi[wide]["name"], len(plan[wide]), wifi[narrow]["name"], len(plan[narrow]))
    return {wifi[wide]["uuid"]: plan[wide], wifi[narrow]["uuid"]: plan[narrow]}, why


def source_changes(previous: dict, sources: list) -> list:
    """[(level, text)] for adapters that stopped or resumed capturing since
    ``previous`` ({uuid: running}). Call with {} the first time."""
    events = []
    for source in sources:
        uuid = source["uuid"] or source["name"]
        was = previous.get(uuid)
        if was is True and not source["running"]:
            detail = " (%s)" % source["reason"] if source["reason"] else ""
            events.append(("warn", "%s stopped capturing%s -- unplugged? Kismet keeps "
                                   "retrying it; the other adapters carry on" % (source["name"], detail)))
        elif was is False and source["running"]:
            events.append(("ok", "%s is capturing again" % source["name"]))
        elif was is None and not source["running"]:
            detail = " (%s)" % source["reason"] if source["reason"] else ""
            events.append(("warn", "%s is not capturing%s" % (source["name"], detail)))
    return events
