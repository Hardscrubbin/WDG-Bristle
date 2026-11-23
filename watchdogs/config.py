"""Constants and configuration for Watch Dogs Go."""

import os
import re
import sys
from pathlib import Path

# A commented-out line that still LOOKS like a real KEY=VALUE secret (a
# stray leading "#" left over from copying secrets.conf.example, or a
# line accidentally re-commented while editing) used to be silently
# treated as a comment and ignored with zero warning. That's the exact
# root cause behind a real user report (GitHub Issue #14, "Can't
# download maps"): a Stadia key with a leading "#" silently did nothing,
# no error, no hint why. _load_secrets() below now flags this loudly.
_COMMENTED_KEY_RE = re.compile(r'^#+\s*([A-Z][A-Z0-9_]*)\s*=')


def _load_secrets() -> dict[str, str]:
    """Load KEY=VALUE pairs from secrets.conf at the project root."""
    secrets: dict[str, str] = {}
    conf = Path(__file__).resolve().parent.parent / "secrets.conf"
    if not conf.is_file():
        return secrets
    try:
        for lineno, raw_line in enumerate(
                conf.read_text(encoding="utf-8").splitlines(), 1):
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#"):
                m = _COMMENTED_KEY_RE.match(line)
                if m:
                    print(
                        f"[secrets.conf] WARNING line {lineno}: "
                        f"'{m.group(1)}' looks like a real key but the "
                        f"line starts with '#', so it's being treated as "
                        f"a comment and IGNORED. Remove the leading '#' "
                        f"if this key should actually be set.",
                        file=sys.stderr)
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                secrets[key.strip()] = value.strip()
    except OSError:
        pass
    return secrets


def _secret(*keys: str, default: str = "") -> str:
    """Look up a secret by trying keys in order. Used for backward-compat
    when migrating env/config names (e.g. JANOS_* → WDG_*)."""
    for k in keys:
        v = _secrets.get(k)
        if v:
            return v
    return default


def _env(*keys: str, default: str = "") -> str:
    """Look up an env var by trying keys in order (new name first)."""
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v
    return default


_secrets = _load_secrets()

# v-next: plugins you never use (WDGoWars sync, Janos import, etc.) used
# to still get instantiated and ticked every frame once app.py started
# calling on_update() on every plugin regardless of which menu is open
# (the fix that made the Kismet bridge's background polling actually
# work -- see _tick_background_plugins() in app.py). Each unused
# plugin's own on_update() already self-guards to an immediate no-op
# when its menu isn't open, so this was never a real performance cost --
# but there's no reason to even load/instantiate a plugin you'll never
# open. DISABLED_PLUGINS in secrets.conf takes a comma-separated list
# of plugin file names (without .py), e.g.:
#   DISABLED_PLUGINS=janos_import,wardrive_upload
# and plugin_loader.discover_plugins() skips those files entirely --
# not created, not in the PLUGINS menu, on_update() never called on
# them at all.
DISABLED_PLUGINS = [
    name.strip() for name in _secret("DISABLED_PLUGINS").split(",") if name.strip()
]

BAUD_RATE = 115200
SCAN_TIMEOUT = 15
READ_TIMEOUT = 2
SNIFFER_UPDATE_INTERVAL = 1
PORTAL_UPDATE_INTERVAL = 2
EVIL_TWIN_UPDATE_INTERVAL = 2
HS_RESCAN_INTERVAL = 45  # seconds between handshake auto-rescan cycles (no selection)

# ESP32 serial commands
CMD_SCAN_NETWORKS = "scan_networks"
CMD_SHOW_SCAN_RESULTS = "show_scan_results"
CMD_START_SNIFFER = "start_sniffer"
CMD_START_SNIFFER_NOSCAN = "start_sniffer_noscan"
CMD_PACKET_MONITOR = "packet_monitor"
CMD_CHANNEL_VIEW = "channel_view"
CMD_SHOW_SNIFFER_RESULTS = "show_sniffer_results"
CMD_SHOW_SNIFFER_RESULTS_VENDOR = "show_sniffer_results_vendor"
CMD_CLEAR_SNIFFER_RESULTS = "clear_sniffer_results"
CMD_SHOW_PROBES = "show_probes"
CMD_SHOW_PROBES_VENDOR = "show_probes_vendor"
CMD_LIST_PROBES = "list_probes"
CMD_LIST_PROBES_VENDOR = "list_probes_vendor"
CMD_SNIFFER_DEBUG = "sniffer_debug"
CMD_START_SNIFFER_DOG = "start_sniffer_dog"
CMD_DEAUTH_DETECTOR = "deauth_detector"
CMD_SELECT_NETWORKS = "select_networks"
CMD_UNSELECT_NETWORKS = "unselect_networks"
CMD_SELECT_STATIONS = "select_stations"
CMD_UNSELECT_STATIONS = "unselect_stations"
CMD_START_EVIL_TWIN = "start_evil_twin"
CMD_START_DEAUTH = "start_deauth"
CMD_START_HANDSHAKE = "start_handshake"
CMD_START_HANDSHAKE_SERIAL = "start_handshake_serial"
CMD_SAVE_HANDSHAKE = "save_handshake"
CMD_SAE_OVERFLOW = "sae_overflow"
CMD_START_BLACKOUT = "start_blackout"
CMD_START_GPS_RAW = "start_gps_raw"
CMD_GPS_SET = "gps_set"
CMD_START_WARDRIVE = "start_wardrive"
CMD_START_PORTAL = "start_portal"
CMD_START_KARMA = "start_karma"
CMD_VENDOR = "vendor"
CMD_BOOT_BUTTON = "boot_button"
CMD_LED = "led"
CMD_CHANNEL_TIME = "channel_time"
CMD_DOWNLOAD = "download"
CMD_STOP = "stop"
CMD_WIFI_CONNECT = "wifi_connect"
CMD_LIST_HOSTS = "list_hosts"
CMD_LIST_HOSTS_VENDOR = "list_hosts_vendor"
CMD_ARP_BAN = "arp_ban"
CMD_REBOOT = "reboot"
CMD_PING = "ping"
CMD_LIST_SD = "list_sd"
CMD_SHOW_PASS = "show_pass"
CMD_LIST_DIR = "list_dir"
CMD_LIST_SSID = "list_ssid"
CMD_FILE_DELETE = "file_delete"
CMD_SELECT_HTML = "select_html"
CMD_SET_HTML = "set_html"
CMD_SET_HTML_BEGIN = "set_html_begin"
CMD_SET_HTML_END = "set_html_end"
CMD_SCAN_BT = "scan_bt"
CMD_SCAN_AIRTAG = "scan_airtag"

# Auto-update check (Watch Dogs Go)
APP_UPDATE_URL = "https://raw.githubusercontent.com/LOCOSP/esp32-watch-dogs/master/watchdogs/__init__.py"

# Firmware discovery uses this build's own C5/SOLO manifest and application
# image. Firmware versions come from the manifest, not the game release tag.
# The stock (LOCOSP) download branch is kept only for isolated legacy safety
# fixtures; production uses the C5/SOLO path and never a stock or Piglet fallback.
UPSTREAM_FIRMWARE_ENABLED = False
# [bristle] No online firmware source: the C5/SOLO image is the one shipped in firmware/, checked
# against the SHA256 pinned in c5_firmware.py. The setting exists and is empty; an online source
# would need its own verified identity before it could be switched on (docs/REBRAND.md).
FIRMWARE_RELEASE_URL = ""
# Retained transport profiles. The C5/SOLO installer supports only its approved
# XIAO-C5 target and writes the verified application image, never these offsets.
FLASH_CHIP = "esp32c5"
FLASH_MODE = "dio"
FLASH_FREQ = "80m"
FIRMWARE_DIR = "/tmp/wdg-firmware"

# Board-specific flash profiles
FLASH_BOARDS = {
    "wroom": {
        "label": "ESP32-C5 WROOM-1 (Dev Kit)",
        "baud": 460800,
        "before": "default-reset",
        "bin_name": "projectZerobyLOCOSP.bin",
        "offsets": {
            "bootloader.bin": "0x2000",
            "partition-table.bin": "0x8000",
            "projectZerobyLOCOSP.bin": "0x20000",
        },
    },
    "xiao": {
        "label": "XIAO ESP32-C5 (Seeed Studio, USB-JTAG)",
        "baud": 460800,
        "before": "usb-reset",
        "bin_name": "projectZerobyLOCOSP-xiao.bin",
        "offsets": {
            "bootloader.bin": "0x2000",
            "partition-table.bin": "0x8000",
            "projectZerobyLOCOSP-xiao.bin": "0x20000",
        },
    },
}

# Legacy aliases (backward compat)
FLASH_BAUD = FLASH_BOARDS["wroom"]["baud"]
FLASH_OFFSETS = FLASH_BOARDS["wroom"]["offsets"]

# ESP32 auto-detect fallback. If no port matches a known ESP32 USB chip ID,
# serial_manager.py used to blindly probe every /dev/ttyACM0-3 and
# /dev/ttyUSB0-3 node anyway -- which, with no ESP32 plugged in, could grab
# a GPS dongle, a Flipper, or any other serial device sitting on the bus.
# On by default (unchanged behavior); set WDG_ESP32_FALLBACK=0 in
# secrets.conf to disable the blind-scan fallback entirely and only ever
# use real VID/PID-matched ESP32 detection.
ESP32_FALLBACK_PROBE = _env("WDG_ESP32_FALLBACK", "JANOS_ESP32_FALLBACK", default="1") != "0"

# GPS module — default: AIO UART on uConsole
# Override with env vars: WDG_GPS_DEVICE, WDG_GPS_BAUD (legacy: JANOS_GPS_*)
GPS_DEVICE = _env("WDG_GPS_DEVICE", "JANOS_GPS_DEVICE", default="/dev/ttyAMA0")
GPS_BAUD_RATE = int(_env("WDG_GPS_BAUD", "JANOS_GPS_BAUD", default="9600"))
GPS_PRIVACY_NOISE_DEG = 0.01  # ±0.01° ≈ ±1.1km randomization in private mode

# gpsd — preferred GPS source. On rigs where udev/systemd auto-start gpsd
# the moment a GPS is plugged in (common on Kali/DragonOS wardriving
# builds), gpsd holds the serial device exclusively, so GpsManager talks
# to gpsd's own local JSON protocol instead of opening the port directly.
# Falls back to direct serial (above) only if gpsd isn't reachable.
# Override with env vars: WDG_GPSD_HOST, WDG_GPSD_PORT (legacy: JANOS_GPSD_*)
GPSD_HOST = _env("WDG_GPSD_HOST", "JANOS_GPSD_HOST", default="127.0.0.1")
GPSD_PORT = int(_env("WDG_GPSD_PORT", "JANOS_GPSD_PORT", default="2947"))

# Kismet session auto-rotation. A single multi-hour capture makes
# wardriving.csv/bt_devices.csv bigger and bigger, which makes every future
# "existing BSSID" scan slower and makes the kill-switch (K) stop sequence
# wait on a bigger final flush. Set to a number of minutes to have the
# Kismet bridge automatically stop and relaunch itself on that interval,
# reusing the exact same adapters (no picker screen, no re-prompting) --
# each rotation's stop is small/fast instead of one huge one at the end.
# 0 (default) disables rotation entirely -- a session only ever ends when
# you hit the stop/kill-switch yourself.
# [v2] was env-var only (_env), although the plugin documents it as a
# secrets.conf key -- setting it in secrets.conf silently did nothing. Now
# secrets.conf first, env var second; a malformed value means "off" instead
# of crashing config import. Fractional minutes are allowed.
try:
    KISMET_SESSION_ROTATE_MINUTES = float(
        _secret("WDG_KISMET_ROTATE_MIN", "JANOS_KISMET_ROTATE_MIN")
        or _env("WDG_KISMET_ROTATE_MIN", "JANOS_KISMET_ROTATE_MIN", default="0"))
except ValueError:
    KISMET_SESSION_ROTATE_MINUTES = 0.0

# WiGLE API (wardriving upload)
WIGLE_API_URL = "https://api.wigle.net/api/v2/file/upload"
WIGLE_API_NAME = _secret("WDG_WIGLE_NAME", "JANOS_WIGLE_NAME")
WIGLE_API_TOKEN = _secret("WDG_WIGLE_TOKEN", "JANOS_WIGLE_TOKEN")

# WPA-sec (handshake upload + password download)
WPASEC_URL = "https://wpa-sec.stanev.org/?submit"
WPASEC_DL_URL = "https://wpa-sec.stanev.org/?api&dl=1"
WPASEC_KEY = _secret("WDG_WPASEC_KEY", "JANOS_WPASEC_KEY")

# NOTE: FIRMWARE_RELEASE_URL and FLASH_BOARDS were previously defined a
# second time here, verbatim, duplicating the definitions above (line
# ~116/123). Harmless today since both copies were byte-identical, but a
# dormant landmine: editing one copy (e.g. adding a new board profile)
# without knowing about the other would silently do nothing, since this
# second definition always overwrites the first at import time. Removed
# the duplicate rather than the original so line numbers referenced
# elsewhere (FLASH_BAUD/FLASH_OFFSETS derive from the first FLASH_BOARDS,
# just above GPS_DEVICE) stay correct.

# Survival / crisis offline services (local, running on the uConsole itself).
# [merge] Carried over from the base edition so SYSTEM > Survival features keep working.
# Kiwix serves offline Wikipedia + survival ZIMs on :8080; the ctOS PMTiles map
# server runs on :8082; Organic Maps is an offline-navigation flatpak app.
SURVIVAL_BROWSER = "chromium"
SURVIVAL_WIKI_URL = "http://localhost:8080"
SURVIVAL_CTOS_MAP_URL = "http://localhost:8082/index.html"
SURVIVAL_NAV_CMD = ["flatpak", "run", "app.organicmaps.desktop"]

# [merge-v3] conflict: upstream 0.9.21.3 edited the second FLASH_BOARDS copy that
# used to sit here (dropped its stock FIRMWARE_RELEASE_URL line and kept the
# dict as a "legacy alias"); v2 had deleted the whole duplicate. Resolved as
# v2's deletion: upstream's second dict was identical to the first FLASH_BOARDS above
# (compared at merge time with merge-tools/work/check_flash_boards.py; the
# surviving definition is checked by tests/test_v3_app_merge.py), and upstream's
# FIRMWARE_RELEASE_URL / UPSTREAM_FIRMWARE_ENABLED change is kept in the first
# block.

# Sound notifications (terminal bell)
SOUND_ENABLED = _env("WDG_SOUND", "JANOS_SOUND", default="1") != "0"

# Firmware crash signatures (only true crash indicators, not normal boot messages)
CRASH_KEYWORDS = (
    "Guru Meditation",
    "Core  0 panic",
    "assert failed:",
)
