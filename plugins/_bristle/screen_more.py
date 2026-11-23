# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""[bristle] The other Bristle screens: alerts and their switches, skimmer suspects, hunt mode,
the alert popup on the map, and the small dialogs (flash a board, owner key, a line of text).

Laid out for 640x360 with the game's 5-pixel font, like the Nodes screen. The functions that build
text take plain values and are unit-tested; the draw functions only put that text on the screen.
Nothing here waits for a radio, a port or a file.
"""
from __future__ import annotations

import time

from . import alerts, flasher, skimmer
from .screen import C_BAR, C_BG, C_BORDER, C_DIM, C_ERR, C_OK, C_TEXT, C_TITLE, C_WARN, FONT_W, MAX_CHARS, clip, duration

ALERT_KEYS = "[UP/DOWN] select  [ENTER] switch this kind on/off  [O] sound on/off  [ESC] back"
SUSPECT_KEYS_1 = "[H] hunt  [M] mark on map  [R] reported  [X] false alarm (ignore it)  [N] notes  [P] place  [E] export report"
SUSPECT_KEYS_2 = "[U] undo a mark  [?] what this can and cannot find  [UP/DOWN] select  [ESC] back"
HUNT_KEYS = "[ESC] end the hunt        Listening only: no connecting, no pairing, nothing asked of the device."
SUSPECT_HEADER = "%-5s %-18s %-17s %-11s %-11s %5s %-26s %-9s %s" % (
    "SCORE", "NAME", "ADDRESS", "FIRST SEEN", "LAST SEEN", "SEEN", "ESTIMATED SPOT", "FROM ME", "STATUS")
SUSPECT_FORMAT = "%-5s %-18s %-17s %-11s %-11s %5s %-26s %-9s %s"
POPUP_SECONDS = 7.0


def distance_text(metres) -> str:
    if metres is None:
        return "-"
    return "%d m" % round(metres) if metres < 1000 else "%.1f km" % (metres / 1000.0)


def when_text(t: float, now: float) -> str:
    if not t:
        return "-"
    return duration(max(0, now - t)) + " ago"


# ------------------------------------------------------------------ alerts screen
def alert_switch_rows(center) -> list:
    """[(alert type, text, colour)], one per kind of alert."""
    rows = []
    for name, (label, _sound, repeat) in alerts.TYPES.items():
        on = center.is_on(name)
        rows.append((name, clip("[%s] %-46s same device again after %s" % (
            "ON " if on else "OFF", label, duration(repeat)), 100), C_OK if on else C_DIM))
    return rows


def alert_log_rows(center, now=None, limit=14) -> list:
    now = time.time() if now is None else now
    rows = []
    for alert in list(center.recent)[-limit:][::-1]:
        rows.append(clip("%-8s %-9s %s. %s" % (when_text(alert.t, now), alert.type, alert.title, alert.detail)))
    return rows


def draw_alerts(pyxel, w, h, service, selected) -> None:
    engine = service.nearby
    pyxel.rect(0, 0, w, h, C_BG)
    pyxel.rect(0, 0, w, 12, C_BAR)
    pyxel.text(4, 3, "BRISTLE: ALERTS", C_TITLE)
    counts = engine.center.counts
    right = "this run: %d shown, %d held back as repeats, %d switched off" % (
        counts["raised"], counts["held"], counts["off"])
    pyxel.text(w - 4 - FONT_W * len(right), 3, right, C_DIM)
    if engine.signature_error:
        pyxel.text(4, 16, clip("signatures.yaml was not loaded: " + engine.signature_error), C_ERR)
    else:
        pyxel.text(4, 16, clip("%d signatures loaded (plugins/_bristle/signatures.yaml, each with its source).  Sound: %s" % (
            len(engine.signatures), "on" if str(service.conf.get("alert_sound", "on")) == "on" else "off")), C_TEXT)
    for index, (_name, text, colour) in enumerate(alert_switch_rows(engine.center)):
        y = 30 + index * 10
        if index == selected:
            pyxel.rect(0, y - 1, w, 9, C_BAR)
        pyxel.text(4, y, text, colour)
    y = 30 + len(alerts.TYPES) * 10 + 8
    pyxel.text(4, y, "THIS RUN (newest first; every alert is also in this drive's bristle_alerts.csv)", C_TITLE)
    pyxel.line(4, y + 9, w - 5, y + 9, C_BORDER)
    rows = alert_log_rows(engine.center)
    if not rows:
        pyxel.text(4, y + 13, "No alerts yet.", C_DIM)
    for index, text in enumerate(rows):
        pyxel.text(4, y + 13 + index * 9, text, C_TEXT if index == 0 else C_DIM)
    pyxel.text(4, h - 22, clip("Following: %d places at least %d m apart within %d min. Thresholds are in bristle.conf." % (
        engine.follow.sightings, engine.follow.distance_m, engine.follow.window_s // 60)), C_DIM)
    pyxel.text(4, h - 12, ALERT_KEYS, C_DIM)


# ------------------------------------------------------------------ suspects screen
def suspect_rows(store, here=None, now=None) -> list:
    """One dict per suspect: text, colour, mac, and what the keys need."""
    now = time.time() if now is None else now
    out = []
    for row in store.rows(here):
        s, est = row["suspect"], row["estimate"]
        spot = "-" if est is None else "%.5f,%.5f +-%dm" % (est.lat, est.lon, round(est.radius_m))
        status = s.status or "new"
        colour = C_DIM if s.status == skimmer.STATUS_FALSE else C_OK if s.status == skimmer.STATUS_REPORTED \
            else C_ERR if s.score >= 60 else C_WARN
        out.append(dict(mac=s.mac, suspect=s, estimate=est, colour=colour, text=clip(SUSPECT_FORMAT % (
            s.score, clip(s.name or "(no name)", 18), skimmer.colon(s.mac), when_text(s.first, now),
            when_text(s.last, now), s.sightings, clip(spot, 26), distance_text(row["distance_m"]), status))))
    return out


def suspect_detail(row, cod_seen: bool) -> list:
    s = row["suspect"]
    kind = "Classic" if s.classic else "LE" if s.classic is False else "type not known"
    cod = skimmer.cod_text(s.cod)
    if s.cod is None and s.classic is not False and not cod_seen:
        cod = "not given (this Kismet does not report Class of Device)"
    lines = [clip("Why: %s" % ("; ".join(s.reasons) or "-")),
             clip("Bluetooth %s   Class of Device: %s   heard by: %s" % (kind, cod, ", ".join(s.heard_by) or "-"))]
    if s.best and s.best.get("lat") is not None:
        lines.append(clip("Strongest on foot: %d dBm at %.6f, %.6f" % (s.best["rssi"], s.best["lat"], s.best["lon"])))
    if s.notes:
        lines.append(clip("Notes: " + s.notes))
    return lines


def draw_suspects(pyxel, w, h, service, rows, selected, status, show_help) -> None:
    pyxel.rect(0, 0, w, h, C_BG)
    pyxel.rect(0, 0, w, 12, C_BAR)
    pyxel.text(4, 3, "POSSIBLE SKIMMERS (listening only)", C_TITLE)
    right = "kept in loot/skimmers   never uploaded"
    pyxel.text(w - 4 - FONT_W * len(right), 3, right, C_DIM)
    if show_help:
        pyxel.text(4, 20, "WHAT THIS CAN AND CANNOT FIND", C_TITLE)
        for index, line in enumerate(skimmer.LIMITS):
            pyxel.text(4, 34 + index * 11, line, C_TEXT)
        more = ("Sources: Bluetooth devices Kismet hears (tick hci1 or the UB500 in Kismet, never the adapter",
                "kept for Bristle) and the nodes' Bluetooth LE finds. Score: name 20, class Uncategorized 25,",
                "maker prefix 30-35, strong signal up to 10. Alerts need a high score AND a strong signal.",
                "Hunt mode never connects, pairs or probes. A Bluetooth Classic module only answers",
                "Kismet's ordinary discovery, so it updates slower than a Low Energy one.")
        for index, line in enumerate(more):
            pyxel.text(4, 100 + index * 11, line, C_DIM)
        pyxel.text(4, h - 12, "[?] back to the list   [ESC] close", C_DIM)
        return
    pyxel.text(4, 16, SUSPECT_HEADER, C_TITLE)
    pyxel.line(4, 25, w - 5, 25, C_BORDER)
    top, room = 28, 24
    first = max(0, min(selected - room + 1, len(rows) - room))
    if not rows:
        pyxel.text(4, top, "No suspects. A Bluetooth device that matches a skimmer signature is listed here when it is heard.", C_DIM)
        pyxel.text(4, top + 11, "This never means a pump is safe: press [?] for what is not detected.", C_DIM)
    for line, index in enumerate(range(first, min(len(rows), first + room))):
        y = top + line * 9
        if index == selected:
            pyxel.rect(0, y - 1, w, 9, C_BAR)
        pyxel.text(4, y, rows[index]["text"], rows[index]["colour"])
    if rows and 0 <= selected < len(rows):
        from . import kismet_tap
        for index, line in enumerate(suspect_detail(rows[selected], kismet_tap.state["cod_seen"])):
            pyxel.text(4, 250 + index * 10, line, C_DIM)
    if status:
        pyxel.text(4, h - 34, clip(status[0]), status[1])
    pyxel.text(4, h - 22, SUSPECT_KEYS_1, C_DIM)
    pyxel.text(4, h - 12, SUSPECT_KEYS_2, C_DIM)


# ------------------------------------------------------------------ hunt mode
def hunt_view(hunt, estimate, here, heading, mono=None) -> dict:
    """Everything the hunt screen shows, from plain values."""
    mono = time.monotonic() if mono is None else mono
    view = {"heard": hunt.heard(mono), "level": hunt.level, "bars": 0, "last": hunt.last_rssi,
            "readings": hunt.readings, "best": hunt.best, "guide": "", "distance": None}
    if view["heard"]:
        view["bars"] = skimmer.meter_bars(hunt.level)
    if estimate is not None and here is not None:
        away = skimmer.distance_m(here[0], here[1], estimate.lat, estimate.lon)
        view["distance"] = away
        bearing = skimmer.bearing_deg(here[0], here[1], estimate.lat, estimate.lon)
        if away <= max(8.0, estimate.radius_m):
            view["guide"] = "You are inside the estimated area (within %d m). Follow the signal." % round(max(8.0, estimate.radius_m))
        else:
            view["guide"] = "%s, %s  (bearing %d, %s)" % (distance_text(away), skimmer.turn(bearing, heading),
                                                       round(bearing), skimmer.compass(bearing))
    elif estimate is None:
        view["guide"] = "No estimated spot yet: it has no sighting with a GPS position."
    else:
        view["guide"] = "No GPS fix: cannot say which way. Follow the signal."
    return view


def draw_hunt(pyxel, w, h, service, suspect, view) -> None:
    pyxel.rect(0, 0, w, h, C_BG)
    pyxel.rect(0, 0, w, 12, C_BAR)
    pyxel.text(4, 3, "HUNT: POSSIBLE SKIMMER", C_TITLE)
    name = "%s  %s  score %d" % (suspect.name or "(no name)", skimmer.colon(suspect.mac), suspect.score)
    pyxel.text(w - 4 - FONT_W * len(name), 3, name, C_DIM)
    pyxel.text(4, 24, "WHERE", C_TITLE)
    pyxel.text(4, 36, clip(view["guide"]), C_TEXT)
    pyxel.text(4, 62, "SIGNAL", C_TITLE)
    x0, y0, bar_w, bars = 4, 74, 19, 30
    for index in range(bars):
        lit = view["heard"] and index < view["bars"]
        colour = (C_ERR if index >= 22 else C_WARN if index >= 12 else C_OK) if lit else C_BAR
        pyxel.rect(x0 + index * (bar_w + 2), y0, bar_w, 40, colour)
    if view["heard"]:
        pyxel.text(4, 120, "%d dBm now (smoothed %d dBm), %d readings. The beep gets faster as you get closer." % (
            view["last"], round(view["level"]), view["readings"]), C_TEXT)
    elif view["readings"]:
        pyxel.text(4, 120, "Not heard for a few seconds (last %d dBm, %d readings). Move back to where it was stronger." % (
            view["last"], view["readings"]), C_WARN)
    else:
        pyxel.text(4, 120, "Not heard yet. Go to the estimated spot; the meter starts when a radio hears it.", C_DIM)
    pyxel.text(4, 146, "STRONGEST SO FAR", C_TITLE)
    best = view["best"]
    if best is None:
        pyxel.text(4, 158, "-", C_DIM)
    elif best.get("lat") is None:
        pyxel.text(4, 158, "%d dBm (no GPS fix at that moment)" % best["rssi"], C_TEXT)
    else:
        pyxel.text(4, 158, "%d dBm at %.6f, %.6f  (saved with the suspect)" % (best["rssi"], best["lat"], best["lon"]), C_OK)
    pyxel.text(4, 184, "LISTENING WITH", C_TITLE)
    pyxel.text(4, 196, clip(service.hunt_note or "Kismet's readings"), C_TEXT)
    for index, line in enumerate(("A Low Energy device is read from every advertisement a radio hears.",
                                  "A Classic module (HC-05 type) only shows when Kismet's discovery finds it: slower.",
                                  "A weak or missing signal does not mean nothing is there. Never open a pump yourself.")):
        pyxel.text(4, 222 + index * 10, line, C_DIM)
    pyxel.text(4, h - 12, HUNT_KEYS, C_DIM)


# ------------------------------------------------------------------ popup, dialogs, text input
def draw_popup(pyxel, w, hud_top, alert) -> None:
    """An alert, top centre of the map, under the game's own top bar."""
    lines = [clip(alert.title, 70), clip(alert.detail, 70)] if alert.detail else [clip(alert.title, 70)]
    width = max(len(line) for line in lines) * FONT_W + 12
    height = 9 * len(lines) + 6
    x, y = (w - width) // 2, hud_top + 4
    pyxel.rect(x, y, width, height, C_BG)
    pyxel.rectb(x, y, width, height, C_ERR)
    for index, line in enumerate(lines):
        pyxel.text(x + 6, y + 4 + index * 9, line, C_ERR if index == 0 else C_TEXT)


def dialog_lines(dialog: dict) -> list:
    """[(text, colour)] for the box in the middle of the screen."""
    kind = dialog["type"]
    if kind == "flash":
        board = flasher.BOARDS[dialog["board"]]
        first = dialog["first"]
        return [("WRITE BRISTLE FIRMWARE TO %s" % dialog["device"], C_TITLE),
                ("Board:  < %s >   (%s)" % (board, flasher.BOARD_NAMES[board]), C_TEXT),
                ("ERASES THE WHOLE BOARD, then writes the full image." if first else
                 "Writes the app only: owner, number, settings and stored finds are kept.", C_WARN if first else C_OK),
                (dialog.get("note", ""), C_DIM),
                ("[LEFT/RIGHT] board   [ENTER] write it   [ESC] cancel", C_DIM)]
    if kind == "key":
        return [("OWNER KEY AND NODE MEMORY", C_TITLE),
                ("[E] export for a second core (a file locked with a passphrase)", C_TEXT),
                ("[M] import an export file placed in this install's folder", C_TEXT),
                ("The key itself is never shown. Only one core runs the fleet at a time.", C_DIM),
                ("[ESC] cancel", C_DIM)]
    shown = "*" * len(dialog["value"]) if dialog.get("mask") else dialog["value"]
    return [(dialog["title"], C_TITLE), (dialog.get("hint", ""), C_DIM),
            ("> " + shown[-100:] + "_", C_TEXT), ("[ENTER] ok   [ESC] cancel", C_DIM)]


def draw_dialog(pyxel, w, h, dialog) -> None:
    lines = [(clip(text, 112), colour) for text, colour in dialog_lines(dialog)]
    width = max(len(text) for text, _colour in lines) * FONT_W + 16
    height = 11 * len(lines) + 10
    x, y = (w - width) // 2, (h - height) // 2
    pyxel.rect(x, y, width, height, C_BG)
    pyxel.rectb(x, y, width, height, C_TITLE)
    for index, (text, colour) in enumerate(lines):
        pyxel.text(x + 8, y + 6 + index * 11, text, colour)


def typed(pyxel, value: str, limit: int = 120) -> str:
    """The text after this frame's key presses (printable characters and backspace)."""
    text = getattr(pyxel, "input_text", "") or ""
    for ch in text:
        if " " <= ch <= "~" and len(value) < limit:
            value += ch
    if pyxel.btnp(pyxel.KEY_BACKSPACE, 14, 2):
        value = value[:-1]
    return value


assert len(SUSPECT_KEYS_1) <= MAX_CHARS and len(SUSPECT_HEADER) <= MAX_CHARS
