"""
oui_lookup.py — Revision 2.5 — offline MAC-address vendor identification
and best-effort device-type categorization.

Fully offline: the first 3 bytes of a MAC address (the OUI) map to the
manufacturer who registered that block with the IEEE. This ships a
curated subset of a few hundred common consumer/networking/IoT vendors
— not the full IEEE registry (that's ~50,000 entries and would bloat
this project for marginal benefit on a hobbyist wardrive). Unknown
prefixes just show as "Unknown vendor" rather than guessing.

If you want fuller coverage later: download the official registry CSV
from https://standards-oui.ieee.org/oui/oui.csv and this module's
load_extra_csv() will merge it in — the built-in dict below is meant
to be "good enough out of the box," not the final word.

DEVICE CATEGORY is a second, even softer guess on top of the vendor
name: this is pattern-matching on vendor names commonly associated
with a product category (e.g. "Ring", "Nest", "Arlo" -> camera/IoT
brands), not a real device fingerprint. Treat it as a fun hint for
the dashboard/report, not a security-grade identification — plenty of
vendors make more than one kind of product, and this will occasionally
be wrong or just say "Unknown".
"""

# A deliberately curated, not-exhaustive OUI prefix table. Format:
# "AA:BB:CC" (uppercase, colon-separated, first 3 octets) -> vendor name.
OUI_TABLE = {
    "00:1A:11": "Google", "F4:F5:D8": "Google", "3C:5A:B4": "Google",
    "DA:A1:19": "Google", "94:EB:2C": "Google", "18:B4:30": "Nest Labs",
    "64:16:66": "Nest Labs", "D8:EB:97": "Nest Labs",
    "B0:7F:B9": "Ubiquiti Networks", "24:A4:3C": "Ubiquiti Networks",
    "44:D9:E7": "Ubiquiti Networks", "78:8A:20": "Ubiquiti Networks",
    "F0:9F:C2": "Ubiquiti Networks", "68:D7:9A": "Ubiquiti Networks",
    "00:15:6D": "Ubiquiti Networks",
    "00:14:6C": "Netgear", "00:1B:2F": "Netgear", "20:E5:2A": "Netgear",
    "A0:40:A0": "Netgear", "C0:3F:0E": "Netgear", "9C:3D:CF": "Netgear",
    "00:1D:7E": "Cisco-Linksys", "00:14:BF": "Linksys", "48:F8:B3": "Linksys",
    "C0:56:27": "Linksys", "94:10:3E": "Belkin/Linksys",
    "50:C7:BF": "TP-Link", "F4:F2:6D": "TP-Link", "A4:2B:B0": "TP-Link",
    "EC:08:6B": "TP-Link", "98:DA:C4": "TP-Link", "B0:4E:26": "TP-Link",
    "00:0C:42": "MikroTik", "4C:5E:0C": "MikroTik", "64:D1:54": "MikroTik",
    "00:0F:66": "Cisco Systems", "00:1B:D4": "Cisco Systems", "00:26:99": "Cisco Systems",
    "00:1E:58": "D-Link", "1C:7E:E5": "D-Link", "84:C9:B2": "D-Link",
    "00:0E:8F": "ASUSTek", "1C:87:2C": "ASUSTek", "AC:9E:17": "ASUSTek",
    "70:8B:CD": "ASUSTek", "B0:6E:BF": "ASUSTek",
    "00:17:88": "Philips Hue / Signify", "EC:B5:FA": "Philips Hue / Signify",
    "B4:E6:2D": "Amazon (Echo/Ring)", "68:37:E9": "Amazon", "44:65:0D": "Amazon",
    "F0:27:2D": "Amazon", "0C:47:C9": "Amazon", "AC:63:BE": "Amazon (Ring)",
    "34:14:5F": "Ring", "94:65:2D": "Ring",
    "48:D6:D5": "Google Nest", "1C:F2:9A": "Google Nest",
    "00:62:6E": "Arlo Technologies", "A0:04:60": "Arlo Technologies",
    "00:40:8C": "Axis Communications", "AC:CC:8E": "Axis Communications",
    "00:0F:7C": "Hikvision", "4C:BD:8F": "Hikvision", "A4:14:37": "Hikvision",
    "44:19:B6": "Wyze Labs", "2C:AA:8E": "Wyze Labs",
    "00:17:C8": "HP Inc.", "3C:D9:2B": "HP Inc.", "00:1F:29": "HP Inc.",
    "00:00:48": "Seiko Epson", "00:26:AB": "Seiko Epson",
    "00:80:92": "Brother Industries", "00:1B:A9": "Brother Industries",
    "00:00:85": "Canon", "00:1E:8F": "Canon",
    "00:03:93": "Apple", "3C:07:54": "Apple", "F0:18:98": "Apple",
    "A4:83:E7": "Apple", "DC:2B:2A": "Apple", "68:96:7B": "Apple",
    "AC:BC:32": "Apple", "F4:5C:89": "Apple",
    "3C:5A:37": "Samsung Electronics", "8C:71:F8": "Samsung Electronics",
    "5C:0A:5B": "Samsung Electronics", "E4:7D:BD": "Samsung Electronics",
    "00:16:6F": "Samsung Electronics",
    "DC:A6:32": "Raspberry Pi Foundation", "B8:27:EB": "Raspberry Pi Foundation",
    "E4:5F:01": "Raspberry Pi Foundation", "D8:3A:DD": "Raspberry Pi Foundation",
    "00:1E:C0": "Sonos", "5C:AA:FD": "Sonos", "94:9F:3E": "Sonos",
    "70:EE:50": "Netatmo",
    "00:24:E4": "Withings",
    "18:74:2E": "Rachio",
    "A0:CE:C8": "Espressif (ESP8266/ESP32 — many DIY/IoT devices)",
    "24:6F:28": "Espressif (ESP8266/ESP32 — many DIY/IoT devices)",
    "30:AE:A4": "Espressif (ESP8266/ESP32 — many DIY/IoT devices)",
    "EC:FA:BC": "Espressif (ESP8266/ESP32 — many DIY/IoT devices)",
}

# Substring (case-insensitive) match on vendor name -> category label.
# Order matters: first match wins.
CATEGORY_HINTS = [
    ("ring", "📷 Camera/doorbell"),
    ("arlo", "📷 Camera"),
    ("hikvision", "📷 Camera (CCTV)"),
    ("axis communications", "📷 Camera (CCTV)"),
    ("wyze", "📷 Camera"),
    ("nest", "🏠 Smart home"),
    ("philips hue", "💡 Smart lighting"),
    ("signify", "💡 Smart lighting"),
    ("sonos", "🔊 Smart speaker"),
    ("netatmo", "🏠 Smart home"),
    ("withings", "🏠 Smart home"),
    ("rachio", "🚿 Smart irrigation"),
    ("hp inc", "🖨️ Printer"),
    ("epson", "🖨️ Printer"),
    ("brother industries", "🖨️ Printer"),
    ("canon", "🖨️ Printer"),
    ("ubiquiti", "📡 Router/AP"),
    ("netgear", "📡 Router/AP"),
    ("linksys", "📡 Router/AP"),
    ("tp-link", "📡 Router/AP"),
    ("mikrotik", "📡 Router/AP"),
    ("cisco", "📡 Router/AP"),
    ("d-link", "📡 Router/AP"),
    ("asustek", "📡 Router/AP"),
    ("raspberry pi", "🔧 DIY/SBC"),
    ("espressif", "🔧 DIY/IoT"),
    ("amazon", "🗣️ Voice assistant / IoT"),
    ("apple", "📱 Phone/computer"),
    ("samsung", "📱 Phone/electronics"),
    ("google", "📱 Phone/electronics"),
]

_extra = {}


def load_extra_csv(path):
    """Optionally merge in the full IEEE OUI registry CSV (see module
    docstring for the download URL) for fuller coverage. Expects the
    standard columns IEEE publishes: Assignment, Organization Name, ..."""
    import csv
    loaded = 0
    try:
        with open(path, newline="", encoding="utf-8", errors="ignore") as f:
            reader = csv.reader(f)
            next(reader, None)  # header
            for row in reader:
                if len(row) < 3:
                    continue
                raw_oui, org = row[1].strip(), row[2].strip()
                if len(raw_oui) == 6:
                    key = ":".join(raw_oui[i:i+2] for i in range(0, 6, 2)).upper()
                    _extra[key] = org
                    loaded += 1
    except OSError:
        return 0
    return loaded


def vendor_for_mac(mac):
    if not mac or len(mac) < 8:
        return None
    prefix = mac.upper()[0:8]
    return _extra.get(prefix) or OUI_TABLE.get(prefix)


def category_for_vendor(vendor):
    if not vendor:
        return None
    v = vendor.lower()
    for needle, label in CATEGORY_HINTS:
        if needle in v:
            return label
    return None


def identify(mac):
    """Returns (vendor_or_None, category_or_None) for a MAC address."""
    vendor = vendor_for_mac(mac)
    return vendor, category_for_vendor(vendor)
