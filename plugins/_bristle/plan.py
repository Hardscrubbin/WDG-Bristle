"""Channel plans: canonical text, hash, and splitting channels across nodes."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from . import codec, proto


def fnv1a32(data: bytes) -> int:
    h = 0x811C9DC5
    for b in data:
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


@dataclass
class Plan:
    flags: int                       # proto.PLAN_FLAG_WIFI | proto.PLAN_FLAG_BLE
    region: str                      # "U" or "E"
    entries: list[tuple[int, int]] = field(default_factory=list)   # (channel, dwell_ms)

    def channels_text(self) -> str:
        return "/".join(f"{ch}.{dw}" for ch, dw in self.entries)

    def canonical(self) -> str:
        return f"f={self.flags};r={self.region};c={self.channels_text()}"

    def hash(self) -> str:
        return f"{fnv1a32(self.canonical().encode('ascii')):08x}"

    def fields(self) -> dict[str, object]:
        """Fields for a PLAN message.

        The most common dwell goes out once, as d=; only entries with another dwell carry their own.
        (With a dwell on every entry, a plan with every channel of a region would not fit one line.)
        The hash is unaffected: it is always taken over canonical(), which spells every dwell out.
        """
        if not self.entries:
            return {"f": self.flags, "r": self.region, "c": ""}
        counts = Counter(dw for _, dw in self.entries)
        common = max(counts, key=lambda dw: (counts[dw], -dw))
        c = "/".join(str(ch) if dw == common else f"{ch}.{dw}" for ch, dw in self.entries)
        return {"f": self.flags, "r": self.region, "d": common, "c": c}

    @classmethod
    def from_fields(cls, f: str, r: str, c: str, d: str | None = None) -> "Plan":
        entries: list[tuple[int, int]] = []
        if c:
            for item in c.split("/"):
                ch_s, dot, dw_s = item.partition(".")
                if not dot and d in (None, ""):
                    raise ValueError(f"channel {ch_s} has no dwell and the plan has no default (d=)")
                entries.append((int(ch_s), int(dw_s) if dot else int(d)))
        p = cls(int(f), r, entries)
        p.validate()
        return p

    def validate(self) -> None:
        if self.region not in proto.REGIONS:
            raise ValueError(f"bad region {self.region}")
        if len(self.entries) > proto.PLAN_MAX_ENTRIES:
            raise ValueError("too many plan entries")
        allowed = set(proto.REGIONS[self.region]["ch24"]) | set(proto.REGIONS[self.region]["ch5"])
        for ch, dw in self.entries:
            if ch not in allowed:
                raise ValueError(f"channel {ch} not allowed in region {self.region}")
            if not proto.DWELL_MIN_MS <= dw <= proto.DWELL_MAX_MS:
                raise ValueError(f"dwell {dw} out of range")
        size = len(codec.body("PLAN", self.fields())) + 6        # "*CRC\n" and a little slack
        if size > proto.LINE_MAX_BYTES:
            raise ValueError(f"plan needs {size} bytes on the wire, a line holds {proto.LINE_MAX_BYTES}: "
                             "use fewer different dwell times")


@dataclass
class NodeCaps:
    """What the planner needs to know about one node."""
    number: int
    cap_5g: bool
    cap_ble: bool = True
    pin: str | None = None           # None (auto), "24", "5", "5lo", "5hi", "ble"


def _chunks(items: list[int], n: int) -> list[list[int]]:
    """Split items into n contiguous, near-equal chunks (n >= 1).

    With more nodes than channels (a 40-node fleet), every node still gets one channel: channels
    are dealt round-robin, so busy channels end up watched by more than one node.
    """
    if n > len(items) and items:
        return [[items[i % len(items)]] for i in range(n)]
    out: list[list[int]] = []
    base, extra = divmod(len(items), n)
    i = 0
    for k in range(n):
        size = base + (1 if k < extra else 0)
        out.append(items[i:i + size])
        i += size
    return out


def split(nodes: list[NodeCaps], region: str = "U", dwell_ms: int | None = None,
          kismet_heavy: set[int] | None = None) -> dict[int, Plan]:
    """Assign every node a plan.

    Rules:
      - With 2+ nodes, one BLE-capable node runs BLE only (a node pinned to "ble" wins; otherwise
        the highest-numbered BLE-capable node, so low numbers keep Wi-Fi).
      - Pinned nodes get their pinned band. The rest share whatever isn't fully pinned.
      - 5 GHz goes only to 5 GHz-capable nodes. 2.4 GHz is shared by all Wi-Fi nodes.
      - Channels Kismet already covers heavily (kismet_heavy) go last in each node's list and get the
        minimum dwell, so nodes spend their time where Kismet is thin.
      - Every allowed channel ends up in at least one plan when there is a capable node for it.
    """
    if region not in proto.REGIONS:
        raise ValueError(f"bad region {region}")
    dwell = dwell_ms or proto.DWELL_DEFAULT_MS
    heavy = kismet_heavy or set()
    reg = proto.REGIONS[region]
    ch24 = list(reg["ch24"])
    ch5 = list(reg["ch5"])
    half = len(ch5) // 2
    bands = {"24": ch24, "5": ch5, "5lo": ch5[:half], "5hi": ch5[half:]}

    result: dict[int, Plan] = {}
    if not nodes:
        return result
    ordered = sorted(nodes, key=lambda n: n.number)

    # 1. BLE duty
    ble_node: NodeCaps | None = next((n for n in ordered if n.pin == "ble"), None)
    if ble_node is None and len(ordered) >= 2:
        candidates = [n for n in ordered if n.cap_ble and n.pin is None]
        if candidates:
            ble_node = candidates[-1]
    if ble_node is not None:
        result[ble_node.number] = Plan(proto.PLAN_FLAG_BLE, region, [])
    wifi_nodes = [n for n in ordered if n is not ble_node]

    def entries(chs: list[int]) -> list[tuple[int, int]]:
        light = [c for c in chs if c not in heavy]
        hv = [c for c in chs if c in heavy]
        return [(c, dwell) for c in light] + [(c, proto.DWELL_MIN_MS) for c in hv]

    # 2. Pinned Wi-Fi nodes
    covered: set[int] = set()
    auto: list[NodeCaps] = []
    for n in wifi_nodes:
        if n.pin in bands:
            chs = bands[n.pin]
            if n.pin != "24" and not n.cap_5g:
                chs = ch24   # can't honour a 5 GHz pin: fall back to 2.4
            result[n.number] = Plan(proto.PLAN_FLAG_WIFI, region, entries(chs))
            covered |= set(chs)
        else:
            auto.append(n)

    # 3. Auto nodes share what's left (if everything is pinned-covered, they share it all again)
    rest24 = [c for c in ch24 if c not in covered] or ch24
    rest5 = [c for c in ch5 if c not in covered] or ch5
    auto5 = [n for n in auto if n.cap_5g]
    auto24 = [n for n in auto if not n.cap_5g]

    if auto:
        if auto5:
            # 2.4-only nodes take 2.4 GHz; 5 GHz nodes split 5 GHz, and also 2.4 if nobody else has it.
            per5 = _chunks(rest5, len(auto5))
            if auto24:
                per24 = _chunks(rest24, len(auto24))
                for n, chs in zip(auto24, per24):
                    result[n.number] = Plan(proto.PLAN_FLAG_WIFI, region, entries(chs))
                for n, chs in zip(auto5, per5):
                    result[n.number] = Plan(proto.PLAN_FLAG_WIFI, region, entries(chs))
            else:
                per24 = _chunks(rest24, len(auto5))
                for n, c24, c5 in zip(auto5, per24, per5):
                    result[n.number] = Plan(proto.PLAN_FLAG_WIFI, region, entries(c24 + c5))
        else:
            per24 = _chunks(rest24, len(auto24))
            for n, chs in zip(auto24, per24):
                result[n.number] = Plan(proto.PLAN_FLAG_WIFI, region, entries(chs))

    # A lone node does Wi-Fi + BLE
    if len(ordered) == 1 and ordered[0].cap_ble and ordered[0].number in result:
        p = result[ordered[0].number]
        p.flags |= proto.PLAN_FLAG_BLE

    # Empty Wi-Fi plans (more nodes than channels): give them the whole 2.4 band
    for n in wifi_nodes:
        p = result.get(n.number)
        if p is not None and p.flags & proto.PLAN_FLAG_WIFI and not p.entries:
            p.entries = entries(ch24)
    return result
