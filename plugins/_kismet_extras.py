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

With one adapter, or identical single-band adapters, nothing is changed
(Kismet's own hopping stays as it is).

[bristle] WDG-Bristle 1.2 adds the split for three and more cards and the
per-card channel lock; they are described where they start, further down.
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
        # [bristle] whether the source is hopping, and the channel it is on (a locked card)
        hopping = item.get("kismet.datasource.hopping")
        rows.append(dict(
            hopping=bool(hopping) if isinstance(hopping, (bool, int)) else None,
            channel=_text(item.get("kismet.datasource.channel")),
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


def plan_split(sources, locks=None, busy=None) -> tuple:
    """({uuid: [channels]}, why) for the running Wi-Fi sources.

    An empty plan means "leave Kismet's hopping alone"; ``why`` says so in a
    form fit for the terminal.

    [bristle] 1.2: any number of cards (see plan_cards below), and ``locks``
    ({uuid: channel}) for cards that stay on one channel: they take no part in
    the split and their channel is left out of what the others hop."""
    result = plan_cards(sources, locks, busy)
    return result["plan"], result["why"]


def _plan_two(wifi, lists) -> tuple:
    """The two-card split, exactly as before 1.2: ({index: [channels]}, why)."""
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
    return {wide: plan[wide], narrow: plan[narrow]}, why


# ---------------------------------------------------------------------------
# [bristle] WDG-Bristle 1.2: a split for any number of cards, and channel locks.
#
#   3 cards   2.4 GHz / 5 GHz low (36-64) / 5 GHz high (100 and up)
#   4 cards   the same, and 6 GHz for a card that can tune it
#   more      each extra card joins the busiest band it can tune and shares that
#             band's channels with the card already there
#
# "Busiest" is ``busy`` ({segment: networks seen there}) when the caller has
# counts; without counts the order is 2.4 GHz, 5 GHz low, 5 GHz high, 6 GHz, one
# extra card each, then round again.
#
# The rules of the two-card split hold for every count: a plan is made only from
# channel names Kismet (or the card) reported, a channel only one card can tune
# stays with that card, every channel hopped before is still hopped by a card,
# and no card is left with nothing to do. A card locked to one channel is not
# part of the split; its channel is watched all the time already, so the other
# cards leave it out. When that card is unplugged the lock goes with it and the
# next plan gives the channel back to the others.
# ---------------------------------------------------------------------------
SEG_24, SEG_5LO, SEG_5HI, SEG_6 = "2.4", "5lo", "5hi", "6"
SEGMENTS = (SEG_24, SEG_5LO, SEG_5HI, SEG_6)
SEGMENT_NAMES = {SEG_24: "2.4 GHz", SEG_5LO: "5 GHz low", SEG_5HI: "5 GHz high", SEG_6: "6 GHz"}
_SEGMENT_NEED = {SEG_24: 1, SEG_5LO: 2, SEG_5HI: 2, SEG_6: 3}     # how wide a card the segment asks for
_DEFAULT_BUSY = {SEG_24: 1.0, SEG_5LO: 0.99, SEG_5HI: 0.98, SEG_6: 0.97}
_MATCH_LIMIT = 7                                                  # cards tried for a band of their own
STANDARD_CHANNELS = ([str(n) for n in range(1, 15)] + [str(n) for n in range(36, 65, 4)]
                     + [str(n) for n in range(100, 145, 4)] + [str(n) for n in range(149, 166, 4)])
_IW_CHANNEL_RE = re.compile(r"\*\s+(\d{4,5})(?:\.\d+)?\s+MHz\s+\[(\d+)\]([^\n]*)")


def channel_number(name):
    """The channel number at the start of a Kismet channel name, or None."""
    match = _CHANNEL_RE.match(name.strip()) if isinstance(name, str) else None
    return int(match.group(1)) if match else None


def channel_segment(name):
    """"2.4", "5lo" (below channel 100), "5hi" or "6"; None when the name cannot be read."""
    band = channel_band(name)
    if band == BAND_5:
        return SEG_5LO if channel_number(name) < 100 else SEG_5HI
    return {BAND_24: SEG_24, BAND_6: SEG_6}.get(band)


def same_channel(first, second) -> bool:
    """True when two names are the same channel of the same band ("6" and "6HT40+")."""
    band = channel_band(first)
    return band is not None and band == channel_band(second) and channel_number(first) == channel_number(second)


def lock_name(wanted, channels):
    """The name in ``channels`` to send for a lock on ``wanted``, or None when the card does not
    list that channel. The plain name wins over a wide one ("6" before "6HT40+")."""
    wanted = str(wanted or "").strip()
    found = [c for c in channels if isinstance(c, str) and c.strip().lower() == wanted.lower()]
    if not found:
        found = sorted((c for c in channels if same_channel(c, wanted)), key=len)
    return found[0] if found else None


def parse_iw_channels(text) -> list:
    """Channel names a card can tune, from ``iw phy <phy> info`` (or ``... channels``), as Kismet
    names them: "1", "36", "5W6e". Channels the card marks disabled are left out."""
    names = []
    for frequency, number, rest in _IW_CHANNEL_RE.findall(text if isinstance(text, str) else ""):
        if "disabled" in rest:
            continue
        mhz = int(frequency)
        if 2400 <= mhz <= 2500:
            name = number
        elif 5150 <= mhz <= 5925:
            name = number
        elif 5945 <= mhz <= 7125:
            name = number + "W6e"
        else:
            continue
        if name not in names:
            names.append(name)
    return names


def lock_choices(channels) -> list:
    """What LEFT/RIGHT steps through in the picker for one card: its own plain channels when they
    are known, the usual 2.4 and 5 GHz channels when they are not."""
    plain = []
    for name in channels or ():
        number, band = channel_number(name), channel_band(name)
        if band is None:
            continue
        simple = "%dW6e" % number if band == BAND_6 else str(number)
        if simple not in plain:
            plain.append(simple)
    return plain or list(STANDARD_CHANNELS)


def _runs(numbers, step=1) -> str:
    """[1, 2, 3, 6] -> "1-3 6". With step 4 (5 and 6 GHz), channels four apart are neighbours."""
    numbers = sorted(set(numbers))
    if not numbers:
        return ""
    runs, start, last = [], numbers[0], numbers[0]
    for number in numbers[1:]:
        if number - last in (1, step):
            last = number
            continue
        runs.append((start, last))
        start = last = number
    runs.append((start, last))
    return " ".join(str(a) if a == b else "%d-%d" % (a, b) for a, b in runs)


def describe_channels(channels) -> str:
    """"2.4 GHz 1-13 + 5 GHz low 36-64 (21 channels)" for one card's list."""
    by_segment = {}
    for name in channels:
        by_segment.setdefault(channel_segment(name), []).append(name)
    parts = []
    for segment in SEGMENTS:
        if segment in by_segment:
            parts.append("%s %s" % (SEGMENT_NAMES[segment], _runs(
                (channel_number(n) for n in by_segment[segment]), 1 if segment == SEG_24 else 4)))
    if None in by_segment:
        parts.append("%d other" % len(by_segment[None]))
    count = len(channels)
    return "%s (%d channel%s)" % (" + ".join(parts) or "nothing", count, "" if count == 1 else "s")


def _weights(busy) -> dict:
    counts = {s: float(busy.get(s) or 0) for s in SEGMENTS} if isinstance(busy, dict) else {}
    if not any(value > 0 for value in counts.values()):
        return dict(_DEFAULT_BUSY)
    return {s: counts[s] + _DEFAULT_BUSY[s] * 1e-6 for s in SEGMENTS}


def _plan_many(lists, busy) -> dict:
    """{index: [channels]} for three or more hopping cards."""
    from itertools import combinations, permutations
    count = len(lists)
    can = [{channel_segment(c) for c in channels} - {None} for channels in lists]
    reach = [len(_bands(channels)) for channels in lists]
    wanted = [SEG_24, SEG_5LO, SEG_5HI] + ([SEG_6] if count >= 4 else [])
    wanted = [s for s in wanted if any(s in segments for segments in can)]
    pool = list(range(min(count, _MATCH_LIMIT)))
    on = {s: [] for s in SEGMENTS}                 # segment -> the cards working it
    size_of = [{s: sum(1 for c in channels if channel_segment(c) == s) for s in SEGMENTS} for channels in lists]
    present = [s for s in SEGMENTS if any(s in segments for segments in can)]

    def longest(cards, chosen):
        """About how long the longest hop list gets: a band without a card of its own is carried
        by the cards that can tune it, on top of their own band."""
        total = {card: size_of[card][segment] for card, segment in zip(cards, chosen)}
        for segment in present:
            if segment not in chosen:
                able = [card for card in cards if segment in can[card]]
                for card in able:
                    total[card] += max(size_of[c][segment] for c in able) / len(able)
        return max(total.values())

    for size in range(min(len(pool), len(wanted)), 0, -1):
        best = None
        for chosen in combinations(wanted, size):
            for cards in permutations(pool, size):
                if all(chosen[i] in can[cards[i]] for i in range(size)):
                    # least over-qualified cards first, then the shortest longest list, then order
                    waste = sum(reach[cards[i]] - _SEGMENT_NEED[chosen[i]] for i in range(size))
                    score = (waste, longest(cards, chosen), cards)
                    if best is None or score < best[0]:
                        best = (score, cards, chosen)
            if best is not None:
                break                               # the first set of bands that can be staffed, in order
        if best is not None:
            for card, segment in zip(best[1], best[2]):
                on[segment].append(card)
            break
    weights = _weights(busy)
    placed = {card for cards in on.values() for card in cards}
    for card in range(count):                       # extra cards: the busiest band each can tune
        if card in placed or not can[card]:
            continue
        def load(segment):
            return (float("inf") if not on[segment] else weights[segment] / len(on[segment]), -SEGMENTS.index(segment))
        on[max(can[card], key=load)].append(card)
    plan = {card: [] for card in range(count)}
    taken = set()
    for segment in SEGMENTS:
        cards = on[segment]
        names = []
        for card in cards:
            names += [c for c in lists[card] if channel_segment(c) == segment and c not in names]
        share = {card: 0 for card in cards}
        for name in names:
            able = [card for card in cards if name in lists[card]]
            card = min(able, key=lambda c: (share[c], cards.index(c)))
            plan[card].append(name)
            share[card] += 1
            taken.add(name)
    # A channel nobody was given yet (a band without a card of its own, a name that cannot be
    # classified, a channel only one card lists) goes to a card that can tune it: the one with
    # the shortest list so far. Nothing that was hopped before is dropped.
    for card in range(count):
        for name in lists[card]:
            if name in taken:
                continue
            able = [other for other in range(count) if name in lists[other]]
            plan[min(able, key=lambda c: (len(plan[c]), c))].append(name)
            taken.add(name)
    for card in range(count):                       # never leave a card with nothing to do
        if not plan[card]:
            plan[card] = list(lists[card])
    return plan


def resolve_locks(sources, wanted) -> tuple:
    """Sort the locks asked for ({uuid: channel}) by what the cards can do:
    (ok {uuid: the card's own name for that channel}, refused {uuid: channel}, unknown {uuid: channel}).
    ``unknown`` is a card whose channel list is not known yet."""
    ok, refused, unknown = {}, {}, {}
    by_uuid = {s["uuid"]: s for s in sources if s.get("uuid")}
    for uuid, channel in (wanted or {}).items():
        source = by_uuid.get(uuid)
        if source is None or not channel:
            continue
        channels = list(source.get("channels") or []) or list(source.get("hop_channels") or [])
        if not channels:
            unknown[uuid] = str(channel)
            continue
        name = lock_name(channel, channels)
        if name is None:
            refused[uuid] = str(channel)
        else:
            ok[uuid] = name
    return ok, refused, unknown


def plan_cards(sources, locks=None, busy=None) -> dict:
    """The whole picture for the running Wi-Fi sources:

        plan    {uuid: [channels]} to send to Kismet ({} = leave its hopping alone)
        why     one sentence for the terminal
        cards   [{"uuid", "name", "text"}] one line per card, for the picker

    ``locks`` is {uuid: channel} for cards that are locked to one channel."""
    locks = {uuid: str(channel) for uuid, channel in (locks or {}).items() if channel}
    wifi = [s for s in sources if s["kind"] == "wifi" and s["running"] and s["uuid"]]
    locked = [s for s in wifi if s["uuid"] in locks]
    hopping = [s for s in wifi if s["uuid"] not in locks]
    cards = {s["uuid"]: "locked on channel %s" % locks[s["uuid"]] for s in locked}
    lock_note = "; ".join("%s stays on channel %s" % (s["name"], locks[s["uuid"]]) for s in locked)

    def done(plan, why):
        if lock_note:
            why = "%s; %s" % (why, lock_note)
        rows = []
        for source in wifi:
            text = cards.get(source["uuid"])
            if text is None:
                mine = plan.get(source["uuid"])
                text = describe_channels(mine) if mine else "hops all its channels"
            rows.append({"uuid": source["uuid"], "name": source["name"], "text": text})
        return {"plan": plan, "why": why, "cards": rows}

    originals = [list(s["hop_channels"] or s["channels"]) for s in hopping]
    lists = []
    for channels in originals:
        kept = [c for c in channels if not any(same_channel(c, lock) for lock in locks.values())]
        lists.append(kept or list(channels))        # a card that can do nothing else keeps its list
    if len(hopping) < 2:
        if hopping and lists[0] and lists[0] != originals[0]:
            return done({hopping[0]["uuid"]: lists[0]},
                        "%s: %d channels (the locked channel left out)" % (hopping[0]["name"], len(lists[0])))
        return done({}, "one Wi-Fi adapter capturing: no split needed" if len(wifi) < 2
                    else "one Wi-Fi adapter hopping: no split needed")
    if any(not channels for channels in lists):
        return done({}, "Kismet has not reported the adapters' channels")
    if len(hopping) == 2:
        by_index, why = _plan_two(hopping, lists)
        if not by_index:
            trimmed = {hopping[i]["uuid"]: lists[i] for i in (0, 1) if lists[i] != originals[i]}
            return done(trimmed, why)
        return done({hopping[i]["uuid"]: channels for i, channels in by_index.items()}, why)
    by_index = _plan_many(lists, busy)
    why = ", ".join("%s: %s" % (hopping[i]["name"], describe_channels(by_index[i])) for i in range(len(hopping)))
    return done({hopping[i]["uuid"]: by_index[i] for i in range(len(hopping))}, why)


def stop_report(used, forced, results, already=()) -> list:
    """[bristle] 1.2.1. What to say when a Kismet run ends: which Wi-Fi cards went back to managed
    mode and which were left as they were. Plain lines for the terminal.

    ``used``: every Wi-Fi interface the run captured with. ``forced``: the ones the game put into
    monitor mode itself. ``results``: {interface: (back in managed mode?, reason if not)}.
    ``already``: forced ones that were in monitor mode before the run touched them."""
    lines = []
    back = [iface for iface in forced if results.get(iface, (True, ""))[0]]
    stuck = [iface for iface in forced if not results.get(iface, (True, ""))[0]]
    left = [iface for iface in used if iface not in forced]
    if back:
        lines.append("Back in managed mode and handed back to NetworkManager: %s" % ", ".join(back))
    for iface in back:
        if iface in already:
            lines.append("%s was already in monitor mode before this run; it is in managed mode now" % iface)
    for iface in stuck:
        reason = results[iface][1] or "no reason given"
        lines.append("STILL IN MONITOR MODE: %s (%s). By hand: sudo iw dev %s set type managed" % (iface, reason, iface))
    if left:
        lines.append("Left as they were: %s (monitor mode was not switched on for %s in the picker)" % (
            ", ".join(left), "it" if len(left) == 1 else "them"))
    if not lines and not used:
        lines.append("No Wi-Fi card was in this run: nothing to switch back")
    return lines


def picker_sources(cards) -> list:
    """Sources in the form plan_cards takes, from what the picker knows before Kismet runs:
    ``cards`` is [(id, name, channels)], channels as parse_iw_channels gives them."""
    return [dict(uuid=card_id, name=name, interface=card_id, kind="wifi", running=True, error=False, reason="",
                 channels=list(channels or []), hop_channels=list(channels or []), hop_rate=5.0)
            for card_id, name, channels in cards]


def picker_plan(cards, wanted_locks, split_on=True, busy=None) -> list:
    """[(id, text)] for the picker: what each selected Wi-Fi card will do once Kismet runs.
    A lock the card cannot tune is said here, and that card is planned as a hopping one."""
    sources = picker_sources(cards)
    ok, refused, unknown = resolve_locks(sources, wanted_locks)
    locks = dict(ok)
    locks.update(unknown)
    if not split_on:
        rows = []
        for source in sources:
            uuid = source["uuid"]
            rows.append({"uuid": uuid, "text": "locked on channel %s" % locks[uuid] if uuid in locks
                         else "hops all its channels"})
    elif any(not s["channels"] for s in sources if s["uuid"] not in locks) and \
            sum(1 for s in sources if s["uuid"] not in locks) >= 2:
        rows = [{"uuid": s["uuid"], "text": "locked on channel %s" % locks[s["uuid"]] if s["uuid"] in locks
                 else "its share is worked out when Kismet starts (channels not read yet)"} for s in sources]
    else:
        rows = plan_cards(sources, locks, busy)["cards"]
    out = []
    for row in rows:
        text = row["text"]
        if row["uuid"] in refused:
            text = "cannot tune channel %s: it will hop instead. %s" % (refused[row["uuid"]], text)
        elif row["uuid"] in unknown:
            text += " (checked when Kismet starts)"
        out.append((row["uuid"], text))
    return out


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
