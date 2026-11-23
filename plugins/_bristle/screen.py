"""[bristle] The Nodes screen (PLUGINS > Bristle: Nodes) and the small activity line on the map.

Laid out for 640x360 with the game's 5-pixel font: 126 characters across at most. Everything here
is plain text built from values the service already holds; nothing on this screen waits for a
serial port or a file.
"""
from __future__ import annotations

import time

from . import proto
from . import session as S

FONT_W = 5
MAX_CHARS = 126
C_BG, C_BAR, C_TITLE, C_TEXT, C_DIM = 0, 1, 3, 7, 13
C_OK, C_WARN, C_ERR, C_BORDER = 11, 10, 8, 5

HEADER = "%-3s %-12s %-4s %-9s %-30s %6s %5s %4s %4s %7s %5s %4s %6s" % (
    "#", "NAME", "LINK", "STATE", "CHANNELS / PIN", "BACKLG", "DROP", "FILL", "TEMP", "UPTIME", "RAM", "BLD", "SEEN")
LINKS = {proto.LINK_USB: "USB", proto.LINK_ESPNOW: "NOW", proto.LINK_BLE: "BLE"}
PINS = (None, "24", "5", "5lo", "5hi", "ble")
PIN_NAMES = {None: "auto", "24": "2.4 GHz", "5": "5 GHz", "5lo": "5 GHz low", "5hi": "5 GHz high", "ble": "Bluetooth"}
KEYS_1 = "[S] start/stop  [A] adopt  [I] identify  [P] pin band  [B] block/unblock  [X] not a node"
KEYS_2 = "[F] forget  [R] renumber  [U] update firmware  [K] owner key  [H] HUD  [UP/DOWN] select  [ESC] back"


def clip(text, limit=MAX_CHARS) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit - 2] + ".."


def duration(seconds) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 100:
        return "%ds" % seconds
    if seconds < 6000:
        return "%dm" % (seconds // 60)
    if seconds < 360000:
        return "%dh%02d" % (seconds // 3600, seconds % 3600 // 60)
    return "%dd" % (seconds // 86400)


def channels_text(plan, pin=None) -> str:
    """'1-11 36-64' for a plan, with the pin in front if there is one."""
    head = "pin %s: " % PIN_NAMES.get(pin, pin) if pin else ""
    if plan is None:
        return head + "-"
    if not plan.entries:
        return head + ("Bluetooth only" if plan.flags & proto.PLAN_FLAG_BLE else "nothing")
    region = proto.REGIONS[plan.region]
    order = list(region["ch24"]) + list(region["ch5"])
    have = sorted({ch for ch, _dwell in plan.entries}, key=order.index)
    runs, start, last = [], have[0], have[0]
    for ch in have[1:]:
        if order.index(ch) == order.index(last) + 1 and (ch > 14) == (start > 14):
            last = ch
            continue
        runs.append((start, last))
        start = last = ch
    runs.append((start, last))
    text = " ".join("%d" % a if a == b else "%d-%d" % (a, b) for a, b in runs)
    if plan.flags & proto.PLAN_FLAG_BLE:
        text += " +BLE"
    return head + text


def node_rows(service, now_mono=None, now_wall=None) -> list:
    """One dict per thing on the screen: adopted nodes first (by number), then what is waiting."""
    now_mono = time.monotonic() if now_mono is None else now_mono
    now_wall = time.time() if now_wall is None else now_wall
    fleet, registry = service.fleet, service.registry
    sessions = list(fleet.sessions)
    live = {s.hwid: s for s in fleet.authed()}
    rows = []
    for rec in sorted(list(registry.nodes.values()), key=lambda r: r.number):
        s = live.get(rec.hwid)
        row = dict(kind="node", hwid=rec.hwid, number=rec.number, session=s, pin=rec.pin,
                   name=rec.name or "%s-%d" % (proto.NAME_PREFIX, rec.number))
        if s is None:
            seen = duration(now_wall - rec.last_seen) + " ago" if rec.last_seen else "never"
            row.update(text=HEADER_FORMAT % (rec.number, clip(row["name"], 12), "-", "offline",
                                             clip(channels_text(None, rec.pin), 30), "-", "-", "-", "-", "-", "-",
                                             rec.build or "-", clip(seen, 6)), colour=C_DIM)
        else:
            h = s.health
            state = "scanning" if h.flags & proto.HB_RUNNING else "idle"
            if not h.flags & proto.HB_CLOCK:
                state = "no clock"
            age = now_mono - h.at if h.at else None
            row.update(text=HEADER_FORMAT % (
                rec.number, clip(row["name"], 12), LINKS.get(s.link_type, "?"), state,
                clip(channels_text(s.desired.plan, rec.pin), 30), h.backlog, h.dropped, "%d%%" % h.fill_pct,
                "-" if h.temp_c is None else "%dC" % h.temp_c, duration(h.uptime_s),
                "-" if h.ram_kb is None else "%dK" % h.ram_kb, h.build, "now" if age is not None and age < 5 else duration(age)),
                colour=C_ERR if h.dropped else C_WARN if (age is not None and age > 9) or state == "no clock" else C_OK)
        rows.append(row)
    for s in sessions:
        if s.state == S.NEW:
            wireless = s.link_type != proto.LINK_USB
            rows.append(dict(kind="new", hwid=s.hwid, session=s, colour=C_WARN, text=clip(
                "NEW  %s board, build %s, hw %s: %s" % (
                    s.board or "?", s.build, s.hwid, "plug into USB to adopt" if wireless else "[A] adopt   [B] block"))))
        elif s.state == S.FORGOTTEN:
            rows.append(dict(kind="new", hwid=s.hwid, session=s, colour=C_WARN, text=clip(
                "YOURS, NOT IN NODE MEMORY  hw %s (%s): [A] adopt it again   [B] block" % (s.hwid, s.board))))
        elif s.state == S.NOT_OURS:
            rows.append(dict(kind="other", hwid=s.hwid, session=s, colour=C_DIM, text=clip(
                "OTHER OWNER  hw %s (%s): ignored. Free it with its BOOT button (5 s) to adopt it.   [B] block" % (s.hwid, s.board))))
    for key, (port, reason) in list(service.new_ports.items()):
        rows.append(dict(kind="port", key=key, port=port, colour=C_WARN, text=clip(
            "NEW PORT  [A] say hello   [X] not a node   %s (%s, serial %s)" % (
                port.device, port.chip, port.serial_number or "-"))))
    for hwid, note in list(registry.blocked.items()):
        rows.append(dict(kind="blocked", hwid=hwid, colour=C_DIM,
                         text=clip("BLOCKED  hw %s %s   [B] unblock" % (hwid, note))))
    return rows


HEADER_FORMAT = "%-3s %-12s %-4s %-9s %-30s %6s %5s %4s %4s %7s %5s %4s %6s"


def totals_text(service, ingest) -> str:
    t = service.fleet.totals()
    c = ingest.counts
    return clip("Nodes %d/%d online   %s   %d finds/min   saved Wi-Fi %d  BT %d   no GPS %d   waiting %d   "
                "on nodes %d   dropped on nodes %d" % (
                    t["online"], t["known"], "SCANNING (session %d)" % t["session"] if t["running"] else "stopped",
                    ingest.per_minute(), c["written_wifi"], c["written_bt"], c["no_gps"] + c["not_saved"],
                    ingest.backlog(), t["backlog"], t["dropped"]))


def coverage(service) -> tuple:
    """(channels, {channel: [node numbers]}) for the coverage strip."""
    region = proto.REGIONS[service.fleet.region]
    channels = list(region["ch24"]) + list(region["ch5"])
    cover = {ch: [] for ch in channels}
    for s in service.fleet.authed():
        plan = s.desired.plan
        if plan is None or s.number is None:
            continue
        for ch, _dwell in plan.entries:
            if ch in cover:
                cover[ch].append(s.number)
    return channels, cover


def hud_lines(service, ingest) -> list:
    """What the activity line on the map says: [(text, colour)], at most two."""
    t = service.fleet.totals()
    if not t["known"] and not service.new_ports:
        return []
    main = "BRISTLE %d/%d  %s  %d/min  queue %d" % (
        t["online"], t["known"], "scan" if t["running"] else "idle", ingest.per_minute(), t["backlog"] + ingest.backlog())
    lines = [(main, C_OK if t["running"] and t["online"] else C_DIM)]
    warn = ""
    if t["dropped"]:
        warn = "%d find(s) dropped on a node" % t["dropped"]
    elif t["known"] and t["online"] < t["known"]:
        warn = "%d node(s) offline" % (t["known"] - t["online"])
    elif service.new_ports:
        warn = "new ESP32 port: PLUGINS > Bristle"
    elif t["running"] and ingest.counts["no_gps"] and time.time() - ingest.last_gps_wall > 10:
        warn = "no GPS fix: finds are not being placed"
    if warn:
        lines.append((warn, C_WARN))
    return [(clip(text, 56), colour) for text, colour in lines]


def draw_hud(pyxel, service, ingest, term_y) -> None:
    lines = hud_lines(service, ingest)
    if not lines:
        return
    width = max(len(text) for text, _colour in lines) * FONT_W + 6
    height = 9 * len(lines) + 2
    x, y = 3, term_y - height - 2
    pyxel.rect(x, y, width, height, C_BG)
    pyxel.rectb(x, y, width, height, C_BORDER)
    for index, (text, colour) in enumerate(lines):
        pyxel.text(x + 3, y + 2 + 9 * index, text, colour)


def draw(pyxel, w, h, service, ingest, rows, selected, status, kismet_active) -> None:
    pyxel.rect(0, 0, w, h, C_BG)
    pyxel.rect(0, 0, w, 12, C_BAR)
    pyxel.text(4, 3, "BRISTLE: NODES", C_TITLE)
    right = "owner key: set   Bluetooth adapter for Bristle: %s" % service.conf.get("ble_adapter", "hci0")
    pyxel.text(w - 4 - FONT_W * len(right), 3, right, C_DIM)
    pyxel.text(4, 16, totals_text(service, ingest), C_TEXT)
    pyxel.text(4, 28, HEADER, C_TITLE)
    pyxel.line(4, 37, w - 5, 37, C_BORDER)
    top, room = 40, 19
    first = max(0, min(selected - room + 1, len(rows) - room))
    if not rows:
        pyxel.text(4, top, "No nodes yet. Plug a flashed Bristle board into the powered hub: it shows up here.", C_DIM)
    for line, index in enumerate(range(first, min(len(rows), first + room))):
        row = rows[index]
        y = top + line * 9
        if index == selected:
            pyxel.rect(0, y - 1, w, 9, C_BAR)
        pyxel.text(4, y, clip(row["text"]), row["colour"])
    # channel coverage: which nodes cover each channel (and whether Kismet is hopping too)
    channels, cover = coverage(service)
    cy = 218
    pyxel.text(4, cy, "CHANNEL COVERAGE (node numbers under each channel)%s" % (
        "   Kismet: capturing, hopping its own channels" if kismet_active else "   Kismet: not capturing"), C_TITLE)
    cell = (w - 8) // max(1, len(channels))
    for index, ch in enumerate(channels):
        x = 4 + index * cell
        nodes = cover[ch]
        pyxel.text(x, cy + 11, str(ch), C_TEXT if nodes else C_DIM)
        label = "-" if not nodes else str(nodes[0]) if len(nodes) == 1 else "x%d" % len(nodes)
        pyxel.text(x, cy + 21, label[:3], C_OK if nodes else C_ERR if service.fleet.run_mask else C_DIM)
    if rows and 0 <= selected < len(rows) and rows[selected]["kind"] == "node":
        row = rows[selected]
        s = row["session"]
        plan = s.desired.plan if s is not None else None
        detail = "Node %d  hw %s  %s" % (row["number"], row["hwid"], channels_text(plan, row["pin"]))
        pyxel.text(4, cy + 36, clip(detail), C_DIM)
    for index, (text, kind) in enumerate(list(service.messages)[-6:]):
        colour = {"error": C_ERR, "warn": C_WARN, "ok": C_OK}.get(kind, C_DIM)
        pyxel.text(4, 266 + index * 9, clip(text), colour)
    if status:
        pyxel.text(4, h - 33, clip(status[0]), status[1])
    pyxel.text(4, h - 22, KEYS_1, C_DIM)
    pyxel.text(4, h - 12, KEYS_2, C_DIM)
