"""[bristle] Bristle Nodes settings and the owner key.

Settings that are not secret live in ``bristle.conf`` in the install folder (KEY=VALUE, ``#``
comments; ``bristle.conf.example`` lists every key). The owner key is a secret: it lives in
``secrets.conf`` as ``BRISTLE_OWNER_KEY``, is created the first time Bristle starts, and is never
shown on screen or written to a log.
"""
from __future__ import annotations

import os
import secrets as _random
import tempfile
from pathlib import Path

KEY_NAME = "BRISTLE_OWNER_KEY"
KEY_BYTES = 16

DEFAULTS = {
    "region": "U",              # U (US channels) or E (EU channels)
    "ble_adapter": "hci0",      # the ONE Bluetooth adapter reserved for Bristle (Kismet must not use it)
    "ble_link": "on",           # link nodes over Bluetooth LE on that adapter (off: USB and the bridge only)
    "ble_max_links": "5",       # nodes linked over Bluetooth at once (an adapter carries about 7)
    "ble_scan": "active",       # active: nodes also ask each device for its scan response (more device
                                # names). passive: they only listen. Nothing ever connects or pairs
    "control_channel": "1",     # ESP-NOW control channel, 1-13 (nodes and the bridge are told)
    "repeat_ap_s": "300",       # a node reports the same access point again only after this long
    "repeat_ble_s": "30",       # ... and the same Bluetooth device
    "hud": "on",                # the small activity line on the map
    "hud_key": "F8",            # key that switches it on and off
    "tie_to_kismet": "on",      # start/stop the nodes together with a Kismet wardrive
    "auto_probe": "off",        # off: a new ESP32 port is listed and you decide. on: say hello by itself
    "exclude_ports": "",        # serial ports Bristle must never touch, comma separated
    "extra_usb_ids": "",        # more USB chips to treat as ESP32 boards: vid:pid,vid:pid (hex)
    "firmware_dir": "",         # where the node firmware releases are (empty: bristle/firmware/releases here)
    # ---- nearby alerts: on/off for each kind
    "alert_follow": "on",       # a device seen again and again at places far apart
    "alert_tracker": "on",      # AirTag / Tile / SmartTag type trackers
    "alert_axon": "on",
    "alert_flock": "on",
    "alert_pentest": "on",      # other wardriving and pentest devices
    "alert_rig": "on",          # Bristle boards that are not yours
    "alert_skimmer": "on",      # a possible skimmer, high score and close by
    "alert_kismet": "on",       # Kismet's own alerts (the list is kismet_alerts)
    "alert_sound": "on",
    # Kismet alerts to show. Take a name out of the list to switch that one off.
    "kismet_alerts": "DEAUTHFLOOD,BCASTDISCON,DISCONCODEINVALID,DEAUTHCODEINVALID,APSPOOF,BSSTIMESTAMP,"
                     "CHANCHANGE,ADVCRYPTCHANGE,DOT11KARMA,KARMAOUI,WPSBRUTE,NONCEDEGRADE,NONCEREUSE,AIRJACKSSID,CRYPTODROP",
    # ---- following: how far apart, within how long, how many places, over at least how long
    "follow_distance_m": "500",
    "follow_window_min": "60",
    "follow_sightings": "3",
    "follow_min_minutes": "10",
    "ignore_macs": "",          # your own devices (Piglets, Biscuits, phone): MACs, comma separated
    # ---- possible skimmers
    "skimmer_alert_score": "60",    # alert only at this score or more ...
    "skimmer_alert_rssi": "-70",    # ... and this signal or stronger (close by)
    "hunt_adapter": "",         # Bluetooth adapter hunt mode may listen with: empty = none (Kismet's and the
                                # nodes' readings only), or hci0 / hci1. An adapter Kismet uses is borrowed
                                # for the hunt and given back when it ends.
}

ALERT_SWITCHES = ("follow", "tracker", "axon", "flock", "pentest", "rig", "skimmer", "kismet")


def number(conf: dict, key: str, low: float, high: float) -> float:
    """A numeric setting, held inside sensible limits; the default if it is not a number."""
    try:
        value = float(str(conf.get(key, "")).strip())
    except ValueError:
        value = float(DEFAULTS[key])
    return max(low, min(high, value))


def name_list(conf: dict, key: str) -> list:
    return [item.strip().upper() for item in str(conf.get(key, "")).split(",") if item.strip()]


def mac_list(conf: dict, key: str) -> set:
    out = set()
    for item in str(conf.get(key, "")).split(","):
        mac = "".join(c for c in item.lower() if c in "0123456789abcdef")
        if len(mac) == 12:
            out.add(mac)
    return out


def _parse(text: str) -> dict:
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def load(path) -> dict:
    """Every setting, with the default for anything the file does not say."""
    conf = dict(DEFAULTS)
    try:
        found = _parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        found = {}
    for key, value in found.items():
        if key.lower() in conf:
            conf[key.lower()] = value
    conf["region"] = "E" if conf["region"].strip().upper().startswith("E") else "U"
    return conf


def on(conf: dict, key: str) -> bool:
    return str(conf.get(key, "")).strip().lower() in ("on", "1", "yes", "true")


def port_list(conf: dict) -> list:
    return [p.strip() for p in str(conf.get("exclude_ports", "")).split(",") if p.strip()]


def usb_ids(conf: dict) -> list:
    out = []
    for item in str(conf.get("extra_usb_ids", "")).split(","):
        vid, _, pid = item.strip().partition(":")
        try:
            out.append((int(vid, 16), int(pid, 16)))
        except ValueError:
            continue
    return out


def _replace_file(path: Path, text: str, mode=None) -> None:
    """Write next to the file and swap it in, so a crash or power cut leaves the old file whole.
    Keeps the file's owner and permissions (the game may be running under sudo)."""
    path = Path(path)
    previous = path.stat() if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        wanted = mode if mode is not None else (previous.st_mode & 0o777 if previous else 0o644)
        os.chmod(temporary, wanted)
        if previous is not None and hasattr(os, "chown"):
            try:
                os.chown(temporary, previous.st_uid, previous.st_gid)
            except OSError:
                pass
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def remember(path, key: str, value: str) -> bool:
    """Change one setting in bristle.conf and keep everything else in the file as it is."""
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        done = False
        for index, raw in enumerate(lines):
            stripped = raw.strip()
            if stripped and not stripped.startswith("#") and stripped.partition("=")[0].strip().lower() == key:
                lines[index] = "%s = %s" % (key, value)
                done = True
        if not done:
            lines.append("%s = %s" % (key, value))
        _replace_file(path, "\n".join(lines) + "\n")
        return True
    except OSError:
        return False


def core_id(folder) -> str:
    """This install's own id (8 hex), made once and kept with Node Memory. Nodes use it to tell
    one core from another that holds the same owner key (a second core)."""
    path = Path(folder) / "core_id"
    try:
        value = path.read_text(encoding="ascii").strip().lower()
        if len(value) == 8 and all(c in "0123456789abcdef" for c in value):
            return value
    except (OSError, UnicodeError):
        pass
    value = _random.token_hex(4)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _replace_file(path, value + "\n")
    except OSError:
        pass                                    # a new one every start is still better than none
    return value


def replace_owner_key(secrets_path, key: bytes) -> None:
    """Put an imported owner key into secrets.conf. The key that was there stays in the file as
    BRISTLE_OWNER_KEY_PREVIOUS (so the nodes it owns are never locked out for good), every other
    line is kept as it is, and the file stays mode 600."""
    if len(key) != KEY_BYTES:
        raise ValueError("not an owner key")
    secrets_path = Path(secrets_path)
    lines = secrets_path.read_text(encoding="utf-8").splitlines() if secrets_path.exists() else []
    kept, previous = [], None
    for raw in lines:
        name = raw.strip().partition("=")[0].strip()
        if not raw.strip().startswith("#") and name == KEY_NAME:
            previous = raw.strip().partition("=")[2].strip()
            continue
        if not raw.strip().startswith("#") and name == KEY_NAME + "_PREVIOUS":
            continue
        kept.append(raw)
    if previous and previous.lower() != key.hex():
        kept.append("# The owner key this install used before an import. Nodes it adopted still need it.")
        kept.append("%s_PREVIOUS=%s" % (KEY_NAME, previous))
    kept.append("%s=%s" % (KEY_NAME, key.hex()))
    _replace_file(secrets_path, "\n".join(kept) + "\n", mode=0o600)


def _key_from_text(text: str):
    value = _parse(text).get(KEY_NAME, "")
    try:
        key = bytes.fromhex(value)
    except ValueError:
        return None
    return key if len(key) == KEY_BYTES else None


def owner_key(secrets_path, bench_dirs=()):
    """(key, where it came from): "secrets", "bench" or "new".

    Order: the key already in secrets.conf; else the key the bench tool made (so nodes adopted on
    the bench keep working); else a new random key. A bench or new key is added to secrets.conf
    as one extra line: nothing else in the file is touched, and the file ends up mode 600.
    Raises OSError if secrets.conf cannot be written: Bristle then stays off instead of running
    with a key it would forget.
    """
    secrets_path = Path(secrets_path)
    text = ""
    if secrets_path.exists():
        text = secrets_path.read_text(encoding="utf-8")
        key = _key_from_text(text)
        if key is not None:
            return key, "secrets"
        if KEY_NAME in _parse(text):
            raise ValueError("%s in secrets.conf is not 32 hex digits. Fix or remove that line." % KEY_NAME)
    key, source = None, "new"
    for folder in bench_dirs:
        try:
            candidate = bytes.fromhex((Path(folder) / "owner.key").read_text().strip())
        except (OSError, ValueError):
            continue
        if len(candidate) == KEY_BYTES:
            key, source = candidate, "bench"
            break
    if key is None:
        key = _random.token_bytes(KEY_BYTES)
    addition = ("" if not text or text.endswith("\n") else "\n") + \
        "\n# Bristle Nodes owner key. Made by the game. Never share it; back it up with secrets.conf.\n" + \
        "%s=%s\n" % (KEY_NAME, key.hex())
    _replace_file(secrets_path, text + addition, mode=0o600)
    return key, source
