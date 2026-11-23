"""[bristle] Which serial ports belong to Bristle nodes, for WDG's own ESP32 code.

WDG picks "the ESP32" by USB chip, and a XIAO node has the same USB chip as the XIAO that runs the
game's own firmware. watchdogs/serial_manager.py asks here first and leaves node ports alone.
Reads Node Memory straight from its file: this runs before any plugin is loaded.
"""
from __future__ import annotations

import json
from pathlib import Path


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
