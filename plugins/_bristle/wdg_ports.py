"""[bristle] Which serial ports belong to Bristle boards, for WDG's own ESP32 code.

WDG picks "the ESP32" by USB chip, and a XIAO node has the same USB chip as the XIAO that runs the
game's own firmware. watchdogs/serial_manager.py asks here first and leaves Bristle boards alone:

  * a board that is in Node Memory is known by its USB identity (node_devices);
  * a Bristle board that is NOT adopted yet says so by itself: once a second it prints a beacon
    line while no core has spoken to it. is_bristle_board() opens the port, writes nothing, and
    listens for that line. So a new Bristle board is never taken as the game's ESP32, and nothing
    is ever sent to a port to find out what is on it.

The listening costs about 1.3 s the first time a port is looked at (once at startup when the
game's own ESP32 is plugged in), is done at most twice per plugged-in device, and is remembered
until the device is unplugged. Reads Node Memory straight from its file: this runs before any
plugin is loaded.
"""
from __future__ import annotations

import json
from pathlib import Path

from .link_usb import BeaconCache

BEACONS = BeaconCache()          # one for the whole game: Bristle's port scanner uses the same one


def node_devices(root=None) -> set:
    """Device paths of plugged-in boards that are in Node Memory. Empty when there are no nodes
    (and then the port list is not even read)."""
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent.parent
    try:
        data = json.loads((root / "loot" / "bristle" / "nodes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    nodes = data.get("nodes", []) if isinstance(data, dict) else []
    hwids = {str(n.get("hwid", "")) for n in nodes if isinstance(n, dict)}
    keys = {str(n.get("usb_key", "")) for n in nodes if isinstance(n, dict)} - {""}
    if not hwids:
        return set()
    try:
        from .link_usb import list_ports, serial_as_hwid
        return {p.device for p in list_ports() if p.key in keys or serial_as_hwid(p.serial_number) in hwids}
    except Exception:
        return set()


def is_bristle_board(device, ports=None, in_use=None, beacons=None) -> bool:
    """True if the board on this port announces itself as a Bristle board. Listens; never writes.
    False for anything it cannot tell (another program has the port, the port list cannot be
    read): then the game behaves as it always did."""
    try:
        from .link_usb import ESP32_USB_IDS, list_ports, port_in_use
        beacons = beacons if beacons is not None else BEACONS
        found = (ports or list_ports)()
        beacons.present({p.key for p in found})
        port = next((p for p in found if p.device == device), None)
        if port is None or (port.vid, port.pid) not in ESP32_USB_IDS:
            return False
        if beacons.known(port.key):
            return True
        if (in_use or port_in_use)(device):
            return False
        return bool(beacons.check(port.key, device))
    except Exception:
        return False
