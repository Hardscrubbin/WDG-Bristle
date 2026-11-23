"""Kismet Wardrive Bridge -- plugs Hardscrubbin's standalone Kismet-based
wardriving engine (built for the "Wardriver 5" uConsole project) into
Watch Dogs Go as a passive network-discovery data source.

WHAT THIS PLUGIN DOES
----------------------
- Launches a real `kismet` process against whatever WiFi/BLE adapters
  are plugged in (auto-detected, same MAC-based identification used by
  Wardriver 5 -- interface names get renumbered by Linux, MAC addresses
  don't).
- Polls Kismet's own REST API for devices it has seen and turns each
  Wi-Fi access point into the game's own `Network` record, which is fed
  into two places that already exist in the base game:
    * `AppState.networks` -- so it shows up in the existing SCAN menu
      exactly like an ESP32 scan result would.
    * `LootManager.save_wardriving_network()` / `save_bt_device()` /
      `save_wardriving_bt()` -- so it's geo-tagged with the game's
      already-open GPS fix, logged to this session's `wardriving.csv`
      (and `bt_devices.csv` for BLE), and dedup-checked against
      `WatchDogsGame._known_wifi` / `_known_ble` (loaded from ALL past
      sessions) exactly like the ESP32 serial-scan path does -- a
      genuinely new device earns full XP + a map marker, a repeat earns
      the same token 1 XP the base game gives a repeat.
    * A passively-observed WPA handshake/PMKID (Kismet just records
      whatever 4-way handshake happens to cross the air during normal
      association -- it never deauths or injects to provoke one) fires
      the same `_trigger_hs_event()` recognition (200 XP, handshake
      badge, map ping) the ESP32 handshake-capture path fires.
- Adds a "Kismet: Upload to WiGLE.net" action that finally makes use of
  the `WDG_WIGLE_NAME` / `WDG_WIGLE_TOKEN` secrets.conf slot that
  `watchdogs/config.py` already defines but that nothing in the base
  game currently reads.

Because the WiFi/BLE records land in the same `wardriving.csv` the
game already writes, the existing "Watch Dogs Go Wars Sync" plugin
(`wardrive_upload.py`) picks them up and syncs them to wdgwars.pl on
its own -- nothing extra is needed here for that.

WHAT THIS PLUGIN DELIBERATELY DOES NOT DO
------------------------------------------
This is a PASSIVE scanning bridge only. It does not start, stop, wire
into, extend, or read from any of the game's attack features -- Evil
Twin, captive portals, MITM, Dragon Drain, RACE, BlueDucky, or the
WPA-sec handshake-cracking upload path. Those modules are untouched by
this file, and this plugin has no code path that calls into them.

VERSION HISTORY
----------------
2.6 -- v2 follow-up (all tagged [v2]):
       * Final poll at stop: the stop worker does one last delta poll
         before shutting Kismet down, and a batch the poll thread had
         published but the game hadn't consumed is processed instead of
         dropped. Held-record flush + summary run after that.
       * K kill-switch only fires on the main map / loot screen, not
         while typing (input_mode), in the TAB menu, in another screen or
         another plugin's overlay.
       * Auto-rotation is now drive-aware: drive time, distance, last
         good fix and held (no-GPS-yet) records carry across segments;
         the timer is per segment. New memory cap: WDG_KISMET_ROTATE_MB
         restarts Kismet when its RAM reaches the cap (Kismet keeps
         every device in memory for the life of the process).
       * Stats line shows Kismet RAM; summary shows segment/drive split,
         peak SoC temp, throttling + under-voltage seen, Kismet peak RAM.
       * The "no device matched" warning is session-wide (a delta batch
         of only Wi-Fi clients is normal and no longer warns).
       * Self-Test adds SoC temp / under-voltage / throttle checks.
       * The HUD CPU/temp/throttle readout + bottom-bar KISMET tag are
         drawn by app.py now (see watchdogs/sys_monitor.py).
       * Repeat sightings: a device is only passed on when Kismet's own
         last_time for it advanced, and the delta window starts at the
         previous poll's second (overlap <= ~1s instead of a flat 2s).
       * Frame budget: device saves are queued and processed at most
         FRAME_BUDGET_S (8 ms) per frame; held records flush through the
         same queue as save-only items (no double XP). Stop/rotation
         drains everything first.
       * Poll thread waits on its stop event (stop returns in <1 ms).
2.5 -- v2 revision (WDG-Merged-v2), from a Wardriver 4/5 vs plugin
       comparison. Every change is tagged [v2] in the source.
       * Delta polling: the poll now asks Kismet only for devices active
         since the last poll (/devices/last-time/<ts>/devices.json), not
         the whole session's device list every 2s. The old full poll
         re-processed every device ever seen on every poll, which gave
         +1 XP per already-known device every 2 seconds and re-ran a
         save for each one. Falls back to the full view if this Kismet
         build has no last-time endpoint.
       * bt_devices.csv: save_bt_device() reads the whole file on every
         call. The plugin now remembers which MACs it already saved this
         loot session and only calls it for new ones.
       * Thread safety: the poll thread no longer mutates the
         handshake set or the auth counts in place while the main thread
         reads them ("Set/dict changed size during iteration" could kill
         a frame's processing). It builds new objects and swaps them in.
         Unconsumed poll results are merged, not overwritten, and each
         poll thread has its own stop event + generation, so a slow old
         thread can't block or pollute a restarted session.
       * Non-blocking start/stop (Wardriver 5 ran these on a worker
         thread). Kismet startup can take up to ~45s and stop up to
         ~10s; that used to freeze the whole game loop, and session
         rotation froze it for both.
       * Adapters are resolved by MAC at launch (Wardriver 5 pattern),
         so a USB re-enumeration between picking and launching, or
         before an auto-rotation relaunch, no longer launches Kismet on
         a stale interface name. A WiFi adapter whose monitor switch
         fails is left out instead of launched anyway (W5 behavior), and
         BLE adapters are brought up with hciconfig before launch (W5).
       * Kismet gets GPS from gpsd (W5's --override file method) when
         gpsd is reachable, so the .kismet master record is geotagged
         too. Set WDG_KISMET_GPSD=0 in secrets.conf to turn it off.
       * On stop, the newest .kismet is converted to a .wiglecsv backup
         with kismetdb_to_wiglecsv (W5), in the background. Backup only
         -- the WiGLE upload still uses wardriving.csv, so nothing is
         uploaded twice.
       * Hardware manifest written at each launch (W5).
       * Auth-type breakdown in the stats line / summary is now
         session-cumulative (it was "last poll only").
2.4 -- Stability/lag round, driven by real multi-hour wardriving runs:
       * Adapter picker no longer auto-rescans every 2s -- that fixed
         timer was just catching real, harmless USB enumeration churn
         and made it look like the picker was randomly bouncing
         adapters in and out while you tried to lock in a selection.
         'R' already rescans on demand; that's now the only trigger.
       * REST polling against Kismet's API moved off the main pyxel
         thread onto its own background thread -- a slow/busy Kismet
         answering a poll could previously stall the whole game's
         render loop for up to 8s (the poll's own timeout), every poll.
       * Kill-switch (K) stop sequence no longer does an unconditional
         2s sleep when `kismet` already exited cleanly from SIGTERM --
         that sleep existed for the SIGTERM-didn't-work case only, and
         was being paid on every single stop.
       * New MAC-based adapter allowlist: once a session has launched
         successfully, the MACs of whatever's selected are saved
         (`kismet_adapters.json` in the loot dir). From then on, a
         newly-appeared adapter on a rescan is only auto-ticked if its
         MAC is on that saved list -- fixes every adapter that happens
         to be plugged in getting auto-selected and fighting over USB
         power, when the actual intent is "always relaunch with just
         these two." First-ever run (no saved prefs yet) keeps the old
         prefer-monitor-capable-else-select-everything default.
       * New optional session auto-rotation (off by default --
         KISMET_SESSION_ROTATE_MINUTES / WDG_KISMET_ROTATE_MIN): stops
         and immediately relaunches Kismet on the same saved adapters
         on a timer, so wardriving.csv/bt_devices.csv and the eventual
         kill-switch flush never grow unbounded across one very long
         drive -- each rotation's stop is small instead of one huge
         one at the end.
1.9 -- Crash/Ctrl+C safety. Traced app.py's _cleanup() (the only place
       that calls every plugin's on_unload() -- which is what runs
       this plugin's _restore_adapters()) and found it has exactly one
       call site in the whole file, gated behind the in-game quit
       menu action. watchdogs/__main__.py's main() wraps the game in
       try/except Exception/finally, but KeyboardInterrupt (what
       Ctrl+C raises) is not an Exception subclass, so it skips the
       except entirely and _cleanup() never runs. Net effect: quitting
       any way other than the in-game menu -- Ctrl+C in the terminal,
       closing the terminal/SSH session, a crash, a power cut --
       leaves this plugin's WiFi adapter(s) stuck in monitor mode with
       NetworkManager never told to take them back, and the Kismet
       process itself left running in the background. Confirmed this
       is a known failure class for this exact kind of bridge: the
       community's own wdg_wifi_bridge.py / hs_capture.py (kept in
       this repo's root as reference from Issue #3) already both
       install a SIGTERM handler for precisely this reason.
       Fixed entirely on this plugin's side (no app.py or
       sdr_manager.py change) by installing our own SIGTERM/SIGINT
       handlers plus an atexit hook in on_load(), all pointing at one
       idempotent _emergency_cleanup() that stops the Kismet process
       and restores every adapter this plugin forced into monitor
       mode -- so at minimum, a Kismet session started through this
       plugin always cleans up after itself, however the game exits.
       Any previously-installed handler for the same signal is chained
       to afterward so normal Python/game behavior on that signal is
       preserved, just with our cleanup run first. The one thing nothing
       can catch is `kill -9` (SIGKILL) -- no process can intercept
       that signal, so that specific case still needs a manual
       `sudo iw dev <iface> set type managed` after the fact.
1.8 -- Two data-integrity fixes found during a full-codebase audit:
       * Whitelist parity: every stock ESP32 save path in app.py checks
         `self._whitelist.is_blocked(mac)` before a device is counted
         or saved -- this plugin never did, so anything you'd
         whitelisted (your own AP, your own phone) was still captured,
         counted, and written to wardriving.csv/bt_devices.csv by
         Kismet. poll_networks() now takes an `is_blocked` callback and
         skips a device entirely (no count, no save) before it's
         classified, matching the stock behavior exactly.
       * CSV-safety: loot_manager.save_wardriving_network/
         save_wardriving_bt/save_bt_device build their CSV rows with a
         raw f-string, not csv.writer -- a comma anywhere in an SSID or
         BLE device name shifts every column after it (AuthMode,
         timestamp, lat/lon can all end up in the wrong field), and
         since dedup on the same file re-parses it the same naive way,
         one bad row can throw off matching for rows after it too.
         Rather than patch loot_manager.py itself (core file, not
         mine), SSID/name are sanitized once, right where Kismet's raw
         strings first enter this plugin (_csv_safe()), so nothing with
         a stray comma, quote, or newline in it ever reaches a CSV row
         built that way. A comma becomes a semicolon rather than being
         dropped, so the name stays readable.
1.4 -- Ported two more hard-won Wardriver 5 lessons:
       * NetworkManager hygiene: `hw.kill_interferers()` now runs
         before `hw.set_monitor(..., True)` for each selected WiFi
         adapter when a session starts (scoped `nmcli device set
         <iface> managed no` -- NOT the old system-wide
         `airmon-ng check kill`, which once took the whole desktop's
         WiFi down as collateral damage). On stop (manual "Kismet:
         Stop", or the plugin unloading/game closing), every adapter
         that was forced into monitor mode is now explicitly taken
         back out and handed back to NetworkManager
         (`set_monitor(..., False)` + `release_interface_back()`),
         so an adapter can't get stuck in monitor mode after a
         session.
       * WiGLE upload retry queue: if "Upload to WiGLE.net" fails
         (no internet, WiGLE down, etc.) the CSV is queued instead of
         just failing silently, and retried automatically every 60s
         once `hw.internet_available()` is true again -- same pattern
         as Wardriver 5's own `queue_upload`/`process_upload_queue`.
1.3 -- CPU%/temperature added to the top-right HUD (chained off the
       existing Battery/Zoom position, not guessed coordinates).
       Storage-free reading now follows wherever loot data actually
       lives (`app._app_dir`, i.e. whatever `loot_path` points at --
       SD card or USB), not the plugin file's own directory. A
       "KISMET" tag is appended to the bottom bar's existing tools
       list while a session is active.
1.2 -- Fixed a real XP/badge/map-marker parity gap: Kismet-discovered
       devices were being written straight to loot with no XP, no
       badge, and no map marker, because the base game's reward logic
       lives inside the ESP32 serial-scan handler and this plugin
       bypassed it entirely. Now replicates that logic exactly
       (cross-session known-device sets, XP amounts, marker spawning,
       handshake recognition event).
1.1 -- Added the adapter picker overlay (select one or more WiFi/BLE
       adapters by name/MAC, per-adapter monitor-mode toggle) and
       MapTiler as an alternative tile provider to Stadia Maps.
       NOTE: this version's first pass at a HUD status readout
       ("WIFI BLE GPS" text drawn at a hardcoded screen position)
       directly overlapped the base game's own bottom-bar BLE counter
       drawn at that same spot -- discovered and removed in 1.3.
1.0 -- Initial bridge: launch Kismet, poll its REST API, feed
       AppState.networks + LootManager, WiGLE.net upload action.
"""

import atexit
import json
import math
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False

from plugins.plugin_base import PluginBase, PluginMenuItem
from plugins import _w5_hardware as hw

try:
    from plugins import _oui_lookup as oui_lookup
    HAVE_OUI = True
except ImportError:
    HAVE_OUI = False

from watchdogs.app_state import Network

try:
    from watchdogs.config import WIGLE_API_URL, WIGLE_API_NAME, WIGLE_API_TOKEN, _secret
except ImportError:
    # Older checkouts without the WiGLE constants yet -- upload action
    # will just report "not configured" rather than crash plugin load.
    WIGLE_API_URL = "https://api.wigle.net/api/v2/file/upload"
    WIGLE_API_NAME = ""
    WIGLE_API_TOKEN = ""
    def _secret(*keys, default=""):
        return default

try:
    from watchdogs.config import KISMET_SESSION_ROTATE_MINUTES
except ImportError:
    # Older checkouts without the rotation setting yet -- rotation stays
    # off (0) rather than crash plugin load.
    KISMET_SESSION_ROTATE_MINUTES = 0

# v2.1: optional scoped, READONLY Kismet API key, set as
# WDG_KISMET_API_KEY=... in secrets.conf. Generate one in Kismet's own
# web UI under Settings -> API Keys, role "readonly" -- this plugin only
# ever does read-only GETs (device polling, datasource status), so a
# readonly key is enough and is real least-privilege hardening over the
# admin httpd_username/httpd_password _discover_credentials() falls back
# to below. Reuses config.py's generic _secret() lookup rather than
# adding a new named constant there, so this needed no core-file patch.
KISMET_API_KEY = _secret("WDG_KISMET_API_KEY")

# [v2] Hand Kismet the same gpsd the game reads (Wardriver 5's method), so
# the .kismet master record is geotagged. On by default; WDG_KISMET_GPSD=0
# in secrets.conf turns it off (e.g. if kismet_site.conf already has gps=).
try:
    from watchdogs.config import GPSD_HOST, GPSD_PORT
except ImportError:
    GPSD_HOST, GPSD_PORT = "127.0.0.1", 2947
KISMET_GPSD_ENABLED = (_secret("WDG_KISMET_GPSD", default="1") or "1").strip() != "0"

# [v2] optional memory-based auto-rotation: restart Kismet (same adapters,
# drive carries on) once its resident memory reaches this many MB.
# 0 = off. Set WDG_KISMET_ROTATE_MB=... in secrets.conf.
try:
    KISMET_ROTATE_RSS_MB = int(_secret("WDG_KISMET_ROTATE_MB", default="0") or 0)
except (TypeError, ValueError):
    KISMET_ROTATE_RSS_MB = 0


# [merge-v3] Two Wi-Fi adapters capturing at once (Alfa + AC1200): "band"
# gives 2.4 GHz to one and 5/6 GHz to the other (plugins/_kismet_extras.py);
# "off" leaves both hopping everything. WDG_KISMET_SPLIT in secrets.conf.
from plugins import _kismet_extras as kx
from plugins import _gpsd_proxy

try:  # [bristle] Bristle reads along from each poll (nearby alerts, skimmer suspects). Nothing changes here without it.
    from plugins._bristle import kismet_tap as _bristle_tap
except Exception:
    class _bristle_tap:  # noqa: N801
        @staticmethod
        def bt_device(*_args):
            pass

        @staticmethod
        def poll(*_args):
            pass
KISMET_SPLIT_MODE = (_secret("WDG_KISMET_SPLIT", default="band") or "band").strip().lower()
if KISMET_SPLIT_MODE not in ("band", "off"):
    KISMET_SPLIT_MODE = "band"
# [merge-v3] With two GPS receivers behind gpsd, Kismet reads the one the game
# follows through a local filter (plugins/_gpsd_proxy.py). 0 = point Kismet
# straight at gpsd even then.
KISMET_GPS_PROXY = (_secret("WDG_KISMET_GPS_PROXY", default="1") or "1").strip() != "0"
KISMET_GPS_PROXY_PORT = 2948
# [merge-v3] A Kismet that dies mid-capture is restarted on the same adapters,
# at most this many times within the window; then the bridge stops and says so.
KISMET_MAX_RESTARTS = 3
KISMET_RESTART_WINDOW = 600.0
DATASOURCE_CHECK_INTERVAL = 10.0
POLL_ERROR_REPEAT = 30.0
# [merge-v3] At stop, how long the stop worker waits for a device poll that is
# still in flight before it does the final poll (a poll times out after 8 s).
POLL_HANDOVER_WAIT = 10.0


def _gpsd_reachable(timeout=0.5) -> bool:
    """[v2] True if something is listening on gpsd's port right now."""
    import socket
    try:
        with socket.create_connection((GPSD_HOST, int(GPSD_PORT)), timeout=timeout):
            return True
    except OSError:
        return False

KISMET_URL = "http://localhost:2501"

# Kismet's own crypt string -> the simple labels LootManager._AUTH_MAP
# expects ("OPEN", "WPA2", ...). Anything not matched falls back to
# "OPEN" with the raw string appended so it's still visible in logs
# rather than silently mis-tagged.
_AUTH_SIMPLE = (
    ("wpa3", "WPA3"),
    ("sae", "WPA3"),
    ("wpa2", "WPA2"),
    ("wpa", "WPA"),
    ("wep", "WEP"),
)


def _simple_auth(crypt: str) -> str:
    c = (crypt or "").lower()
    for needle, label in _AUTH_SIMPLE:
        if needle in c:
            return label
    if c in ("", "none", "open"):
        return "OPEN"
    return "OPEN"


def _band_for_channel(channel) -> str:
    try:
        ch = int(channel)
    except (TypeError, ValueError):
        return ""
    return "2.4GHz" if ch <= 14 else "5GHz"


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in meters between two lat/lon points.
    Used to accumulate a live "distance traveled this session" stat
    from successive GPS fixes -- Wardriver 5 tracked this to give a
    rough coverage/mileage number without needing a real odometer."""
    try:
        lat1, lon1, lat2, lon2 = float(lat1), float(lon1), float(lat2), float(lon2)
    except (TypeError, ValueError):
        return 0.0
    r = 6371000.0  # mean Earth radius, meters
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _fmt_distance(meters: float) -> str:
    """Human-friendly distance string -- meters under 1km, else km to
    2 decimal places (matches the precision of the m/2h/etc. duration
    formatting already used in _write_session_summary)."""
    if meters < 1000:
        return f"{meters:.0f} m"
    return f"{meters / 1000.0:.2f} km"


def _csv_safe(s: str) -> str:
    """Make a string safe to drop into loot_manager's hand-built
    wardriving.csv/bt_devices.csv rows (plain f-string interpolation,
    NOT csv.writer -- see v1.8 note at the top of this file). Commas
    shift columns, embedded quotes and newlines can break a row outright
    or confuse the naive `line.split(",")` dedup parsing on re-read.
    A comma becomes a semicolon (visually similar, keeps the name
    readable) rather than being silently dropped; quotes/newlines are
    just stripped since they have no safe single-character stand-in."""
    if not s:
        return s
    return (s.replace(",", ";")
             .replace('"', "'")
             .replace("\n", " ")
             .replace("\r", " "))


class KismetBridgeEngine:
    """Minimal, GPS-independent Kismet driver.

    Trimmed down from Hardscrubbin's original `war5_kismet.py`: this version
    does not manage its own GPS override or its own WiGLE/wdgwars
    upload queue, because Watch Dogs Go's own GPS + LootManager +
    Wars Sync plugin already do that job once the data lands in
    `wardriving.csv` -- duplicating it here would just be two things
    trying to own the same file.
    """

    def __init__(self, log_dir, log=print):
        self.log_dir = log_dir
        self.log = log
        try:
            os.makedirs(log_dir, exist_ok=True)
        except OSError:
            pass
        self.proc = None
        self.running = False
        self.cfg = {"kismet_user": "", "kismet_password": ""}
        # v2.1: when a WDG_KISMET_API_KEY is configured, every request
        # goes through this session with the key pre-attached as Kismet's
        # documented ?KISMET=<key> query param, instead of the admin
        # basic-auth credentials _discover_credentials() would otherwise
        # fall back to reading out of kismet_httpd.conf.
        self._using_api_key = bool(HAVE_REQUESTS and KISMET_API_KEY)
        self._session = requests.Session() if HAVE_REQUESTS else None
        if self._using_api_key:
            self._session.params = {"KISMET": KISMET_API_KEY}
        self.seen_bssids = set()
        self.handshake_macs = set()
        self.last_raw_count = 0  # devices in Kismet's last poll reply, any type
        # v1.6: counted the instant poll_networks() classifies a device --
        # completely separate from AppState.networks / app.wifi_networks /
        # app.ble_devices, and from the known-device/XP/pyxel-sprite path
        # in on_update(). If anything ever breaks further down that chain
        # again, these two still reflect exactly what Kismet + this
        # plugin's own classifier saw, so the HUD number is never hostage
        # to a bug somewhere else. (v1.8: a whitelisted device is
        # excluded before it ever reaches these sets -- see
        # poll_networks()'s is_blocked parameter.)
        self.seen_wifi_macs = set()
        self.seen_bt_macs = set()
        self._stdout_file = None
        self._stdout_path = None
        # v2.1: live auth-type breakdown + strongest-current-signal, ported
        # from Wardriver 5's own live UI ("Open: 39 · WPA2: 75 · WPA3: 9",
        # "-27 dBm strongest signal now (antenna aim)"). Both reset every
        # poll -- this is a snapshot of the CURRENT poll, not a
        # cumulative-ever count, since "strongest signal right now" is
        # only useful for antenna aiming if it reflects what's actually
        # being seen this instant, not the best you ever saw all session.
        self.auth_counts: dict = {}
        self.strongest_signal = None  # int dBm, or None if nothing seen this poll
        # [v2] delta polling: Kismet-clock timestamp to ask "what changed
        # since" (0 = full list), and whether this build supports it.
        # _auth_by_mac makes the auth breakdown session-cumulative now
        # that a single poll only contains recently-active devices.
        self._since_ts = 0
        self._delta_supported = True
        self._auth_by_mac: dict = {}
        self._last_time_by_mac: dict = {}

    def _auth(self):
        if self._using_api_key:
            # No basic-auth header at all when an API key is active --
            # sending an empty/garbage Authorization header alongside a
            # valid ?KISMET= key is untested against real Kismet and not
            # worth the risk when "send nothing" is unambiguous.
            return None
        return (self.cfg.get("kismet_user", ""), self.cfg.get("kismet_password", ""))

    def _get(self, url, **kwargs):
        """Route every Kismet GET through the shared session so the API
        key (if configured) is automatically attached."""
        return self._session.get(url, **kwargs)

    def _post(self, url, **kwargs):
        return self._session.post(url, **kwargs)

    def _api_up(self):
        if not HAVE_REQUESTS:
            return False, "requests library not installed"
        try:
            r = self._get(f"{KISMET_URL}/system/status.json", auth=self._auth(), timeout=3)
            if r.status_code in (200, 401):
                return True, None
            return False, f"unexpected HTTP {r.status_code}"
        except requests.exceptions.ConnectionError:
            return False, "connection refused (Kismet not listening yet)"
        except requests.exceptions.Timeout:
            return False, "timed out"
        except requests.RequestException as e:
            return False, str(e)

    def _api_authed(self):
        if not HAVE_REQUESTS:
            return False
        try:
            r = self._get(f"{KISMET_URL}/system/status.json", auth=self._auth(), timeout=3)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def _discover_credentials(self):
        if self._using_api_key:
            self.log("[KISMET] Using scoped API key from secrets.conf "
                     "(WDG_KISMET_API_KEY) -- not reading admin credentials")
            return
        if self._api_authed():
            return
        paths = [os.path.expanduser("~/.kismet/kismet_httpd.conf"),
                 "/root/.kismet/kismet_httpd.conf"]
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            paths.append(f"/home/{sudo_user}/.kismet/kismet_httpd.conf")
        for p in paths:
            if not os.path.exists(p):
                continue
            try:
                user = passwd = None
                for line in open(p):
                    line = line.strip()
                    if line.startswith("httpd_username="):
                        user = line.split("=", 1)[1].strip()
                    elif line.startswith("httpd_password="):
                        passwd = line.split("=", 1)[1].strip()
                if user and passwd:
                    self.cfg["kismet_user"] = user
                    self.cfg["kismet_password"] = passwd
                    self.log(f"[KISMET] Found login in {os.path.basename(p)}")
                    return
            except OSError:
                continue
        self.log("[KISMET] Couldn't auto-find Kismet's login -- stats may not authenticate.")

    def start(self, wifi_ifaces, ble_adapters, gps_override=False, gps_endpoint=None, channel_locks=None):
        # [bristle] channel_locks: {interface: channel} for cards that stay on one channel
        self.channel_locks = dict(channel_locks or {})
        self.locked = {}                 # uuid -> channel, filled once Kismet confirms a lock
        if not wifi_ifaces and not ble_adapters:
            self.log("[KISMET] No capture sources selected -- nothing to start")
            return False
        # [v2] fresh Kismet process = fresh device list, so the delta
        # cursor starts over with one full poll.
        self._since_ts = 0
        self._delta_supported = True
        self._auth_by_mac = {}
        self.auth_counts = {}
        self._last_time_by_mac = {}

        rc, _, _ = hw._run(["pgrep", "-x", "kismet"])
        if rc == 0:
            self.log("[KISMET] A Kismet process is already running -- stopping it first")
            hw._run(["pkill", "-INT", "-x", "kismet"])
            time.sleep(3)
            hw._run(["pkill", "-KILL", "-x", "kismet"])
            time.sleep(1)

        os.makedirs(self.log_dir, exist_ok=True)
        cmd = ["kismet", "--no-ncurses", "--no-line-wrap"]
        # [v2] Wardriver 5 Rev 4.4 lesson: --override only takes a flavor
        # name or a config FILE path, never "key=value". So the gps= line
        # goes in a tiny real file and --override points at that file.
        if gps_override:
            override_path = os.path.join(self.log_dir, "wdg_gps_override.conf")
            try:
                # [merge-v3] gps_endpoint: the local one-receiver filter when
                # gpsd serves two receivers (see _kismet_gps_endpoint()).
                gps_host, gps_port = gps_endpoint or (GPSD_HOST, GPSD_PORT)
                with open(override_path, "w") as f:
                    f.write(f"gps=gpsd:host={gps_host},port={gps_port},reconnect=true\n")
                cmd += ["--override", override_path]
                self.log(f"[KISMET] Kismet will read GPS from gpsd {gps_host}:{gps_port}")
            except OSError as e:
                self.log(f"[KISMET] Could not write GPS override ({e}) -- "
                         f"starting without it")
        for iface in wifi_ifaces:
            lock = self.channel_locks.get(iface)
            if lock:                     # [bristle] this card stays on one channel
                cmd += ["-c", f"{iface}:channel_hop=false,channel={lock},retry=true"]
                self.log(f"[KISMET] {iface} is locked to channel {lock} (no hopping)")
                continue
            cmd += ["-c", f"{iface}:channel_hop=true,retry=true"]
        for hci in ble_adapters:
            cmd += ["-c", f"{hci}:name={hci}_bt,retry=true"]

        self.log(f"[KISMET] Launching: {len(wifi_ifaces)} WiFi + {len(ble_adapters)} BLE source(s)")
        self._stdout_path = os.path.join(self.log_dir, "kismet_stdout.log")
        self._stdout_file = open(self._stdout_path, "w")
        self.proc = subprocess.Popen(
            cmd, cwd=self.log_dir, stdout=self._stdout_file,
            stderr=subprocess.STDOUT)

        alive = False
        last_reason = None
        for attempt in range(20):
            time.sleep(2)
            if self.proc.poll() is not None:
                last_reason = f"Kismet process exited immediately (code {self.proc.poll()})"
                break
            ok, reason = self._api_up()
            if ok:
                alive = True
                break
            last_reason = reason
            self.log(f"[KISMET] Waiting for API... ({attempt + 1}/20) -- {reason}")

        if not alive:
            self.log(f"[KISMET] FAIL: {last_reason}. See {self._stdout_path} for Kismet's own output.")
            # [v2] if the process is still alive but its API never came
            # up, don't leave it running (and holding the adapters) in
            # the background.
            if self.proc is not None and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=10)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
            if self._stdout_file:
                try:
                    self._stdout_file.close()
                except Exception:
                    pass
                self._stdout_file = None
            return False

        self.log("[KISMET] Up and running")
        self._discover_credentials()
        self._check_datasources()
        self.running = True
        return True

    def _check_datasources(self):
        """v2.0: ported from Wardriver 5's own launch-validation warning
        ("Kismet reports ZERO datasources -- the -c flags may not have
        matched a valid interface"). The API-up check above only proves
        Kismet's REST server answered -- it says nothing about whether
        any of the -c source flags actually attached to a real adapter.
        A stale/renamed interface name (USB WiFi dongle re-enumerated as
        wlan2 instead of wlan1, say) can "successfully" launch Kismet
        with zero working sources and no warning at all -- polling would
        just silently return empty results forever. Best-effort only:
        never blocks start() on this, just makes the failure loud."""
        try:
            r = self._get(f"{KISMET_URL}/datasource/all_sources.json",
                             auth=self._auth(), timeout=5)
            if r.status_code != 200:
                return
            sources = r.json()
            if not isinstance(sources, list) or not sources:
                self.log("[KISMET] WARNING: Kismet reports ZERO datasources -- "
                          "the -c flags may not have matched a valid interface. "
                          "Check adapter names haven't changed (USB dongles can "
                          "re-enumerate) before assuming nothing's being seen.")
                return
            running = [s for s in sources
                       if s.get("kismet.datasource.running")]
            if not running:
                self.log(f"[KISMET] WARNING: {len(sources)} datasource(s) "
                          f"configured but NONE are running -- Kismet started "
                          f"but couldn't actually open any of them. Check "
                          f"{self._stdout_path} for the real reason.")
        except requests.RequestException:
            pass  # best-effort diagnostic only, never fail start() over this

    def datasources(self):
        """[merge-v3] (sources, error): Kismet's datasources as plain dicts
        (plugins/_kismet_extras.parse_sources), for the adapter watch and
        the channel split. Never raises."""
        if not HAVE_REQUESTS:
            return [], "requests library not installed"
        try:
            r = self._get(f"{KISMET_URL}/datasource/all_sources.json", auth=self._auth(), timeout=5)
            if r.status_code != 200:
                return [], f"HTTP {r.status_code}"
            rows = kx.parse_sources(r.json())
            locked = getattr(self, "locked", None) or {}
            for row in rows:             # [bristle] a card held on one channel says so
                if row["uuid"] in locked:
                    row["lock"] = locked[row["uuid"]]
            return rows, None
        except (requests.RequestException, ValueError) as e:
            return [], str(e)

    def set_channel(self, uuid, channel):
        """[bristle] Hold one datasource on ``channel`` (no hopping). (ok, reason), as
        set_hop_channels: the same Kismet command with a single channel."""
        if not HAVE_REQUESTS:
            return False, "requests library not installed"
        try:
            r = self._post(f"{KISMET_URL}/datasource/by-uuid/{uuid}/set_channel.cmd",
                           auth=self._auth(), data={"json": json.dumps({"channel": str(channel)})}, timeout=5)
        except requests.RequestException as e:
            return False, str(e)
        if r.status_code == 200:
            return True, None
        if r.status_code in (401, 403):
            return False, f"HTTP {r.status_code}: this Kismet login may not change datasources"
        return False, f"HTTP {r.status_code}"

    def set_hop_channels(self, uuid, channels, rate=5.0):
        """[merge-v3] Tell one datasource to hop over ``channels`` only.
        (ok, reason). Needs a Kismet login that may change sources: with a
        read-only API key Kismet answers 401/403 and nothing changes."""
        if not HAVE_REQUESTS:
            return False, "requests library not installed"
        command = json.dumps({"channels": list(channels), "hoprate": rate, "shuffle": 1})
        try:
            r = self._post(f"{KISMET_URL}/datasource/by-uuid/{uuid}/set_channel.cmd",
                           auth=self._auth(), data={"json": command}, timeout=5)
        except requests.RequestException as e:
            return False, str(e)
        if r.status_code == 200:
            return True, None
        if r.status_code in (401, 403):
            return False, f"HTTP {r.status_code}: this Kismet login may not change datasources"
        return False, f"HTTP {r.status_code}"

    def stop(self):
        self.running = False
        exited_cleanly = False
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
                exited_cleanly = True
            except Exception:
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=3)
                except Exception:
                    pass
        # v-next: this pkill/sleep(2)/pkill fallback used to run
        # UNCONDITIONALLY on every single stop -- paying a flat 2-second
        # sleep every time, even when self.proc.wait() just above
        # already confirmed the exact Kismet process we launched had
        # exited cleanly. That 2s is real, user-visible freeze time on
        # the kill switch (K), stacked on top of however long Kismet's
        # own shutdown/flush took, every single time regardless of
        # whether anything was actually still running. Now it only runs
        # when we DON'T already have positive confirmation the process
        # is gone (self.proc was never set, or it didn't die cleanly
        # above) -- the common fast path skips straight past this.
        if not exited_cleanly:
            hw._run(["pkill", "-INT", "-x", "kismet"])
            time.sleep(2)
            if hw._run(["pgrep", "-x", "kismet"])[0] == 0:
                hw._run(["pkill", "-KILL", "-x", "kismet"])
        if self._stdout_file:
            try:
                self._stdout_file.close()
            except Exception:
                pass
            self._stdout_file = None
        self.log("[KISMET] Stopped")

    def poll_networks(self, is_blocked=None):
        """Poll Kismet for devices and return (networks, bt_devices, error).

        networks: list[Network] -- Wi-Fi access points only.
        bt_devices: list[(mac, rssi, name)] -- Bluetooth/BLE devices.
        error: str or None -- set (with a real reason) if the poll failed;
        networks/bt_devices are empty in that case, never partially wrong.

        is_blocked: optional callable(mac) -> bool, normally
        app._whitelist.is_blocked. A whitelisted device is skipped
        before it's classified at all -- not counted in
        seen_wifi_macs/seen_bt_macs, not returned, not saved -- exactly
        the "silently skip" behavior the stock ESP32 save paths in
        app.py already give whitelisted devices (v1.8; this plugin used
        to ignore the whitelist entirely).

        v1.5: this used to POST a field-simplified request (a "fields"
        list using Kismet's path/alias slash syntax) to cut down the
        response size. On a real device that came back 200 OK every
        time -- so on_update() never saw an error and never logged
        anything -- but the alias names it asked for
        ("kismet.common.signal.last_signal", the two-segment dot11
        paths) didn't line up with the keys this Kismet build actually
        returns, so every device's signal/handshake lookups quietly
        came back None/missing. Confirmed by hand with curl against the
        live server: an unfiltered GET returns full device objects with
        the fields below in their normal, nested locations, so this
        just reads the real structure instead of a guessed alias.
        """
        if not self.running or not HAVE_REQUESTS:
            return [], [], None
        # [v2] Delta polling. The "all" view returns every device Kismet
        # has seen this session, every poll -- so poll cost, the XP loop
        # and the save loop all grew with drive length. last-time/<ts>
        # returns the same full device objects (same parsing as before),
        # but only devices active since <ts>. Kismet runs on this machine,
        # so time.time() is on Kismet's clock; the 2s overlap means a
        # device active right at the boundary is never missed (repeats
        # are harmless: every save path downstream dedups).
        poll_started = time.time()
        use_delta = self._delta_supported and self._since_ts > 0
        if use_delta:
            url = f"{KISMET_URL}/devices/last-time/{int(self._since_ts)}/devices.json"
        else:
            url = f"{KISMET_URL}/devices/views/all/devices.json"
        try:
            r = self._get(url, auth=self._auth(), timeout=8)
        except requests.exceptions.ConnectionError:
            return [], [], "devices poll: connection refused"
        except requests.exceptions.Timeout:
            return [], [], "devices poll: timed out"
        except requests.RequestException as e:
            return [], [], f"devices poll: {e}"

        if use_delta and r.status_code == 404:
            self._delta_supported = False
            return [], [], ("devices poll: this Kismet has no last-time endpoint "
                            "-- falling back to full polls")
        if r.status_code != 200:
            return [], [], f"devices poll: HTTP {r.status_code}"
        try:
            devices = r.json()
        except ValueError as e:
            return [], [], f"devices poll: bad JSON ({e})"
        if not isinstance(devices, list):
            devices = []
        # [v2.6] Kismet's last_time is whole seconds. Asking for "> start
        # second - 1" on the NEXT poll returns everything seen from the
        # second this poll began, so nothing is missed between polls and
        # the overlap is at most ~1s (was a flat 2s, i.e. a full extra poll
        # of payload at the default 2s interval). Repeats inside the overlap
        # are dropped by the last_time check below.
        self._since_ts = int(poll_started) - 1

        networks = []
        bt_devices = []
        self.last_raw_count = len(devices)
        # [v2] built locally and swapped in at the end, never mutated in
        # place: the main thread reads handshake_macs / auth_counts while
        # this runs on the poll thread.
        new_handshakes = set()
        strongest = None
        for d in devices:
            if not isinstance(d, dict):
                continue
            phy = d.get("kismet.device.base.phyname", "") or ""
            mac = d.get("kismet.device.base.macaddr")
            if mac and is_blocked and is_blocked(mac):
                continue  # whitelisted -- not counted, not saved, not returned
            name = _csv_safe(d.get("kismet.device.base.commonname") or "")
            # The base device object nests signal info under its own
            # sub-object rather than a flat aliased key.
            sig_obj = d.get("kismet.device.base.signal")
            sig = None
            if isinstance(sig_obj, dict):
                sig = (sig_obj.get("kismet.common.signal.last_signal")
                       or sig_obj.get("kismet.common.signal.last_signal_dbm"))
            dtype = (d.get("kismet.device.base.type") or "")
            is_wifi = "802.11" in phy or "wifi" in phy.lower()
            is_bt = "bluetooth" in phy.lower() or "btle" in phy.lower()
            if not mac:
                continue
            # [v2.6] only pass a device on when Kismet's own last_time for it
            # has moved. The delta window overlaps by ~2s (so nothing at the
            # boundary is missed), which would otherwise hand the same
            # sighting to the game twice (= double repeat XP, double save
            # attempt). Also makes the full-view fallback cheap downstream.
            lt = d.get("kismet.device.base.last_time")
            if isinstance(lt, (int, float)):
                prev_lt = self._last_time_by_mac.get(mac)
                if prev_lt is not None and lt <= prev_lt:
                    continue
                self._last_time_by_mac[mac] = lt

            if mac not in self.seen_bssids:
                self.seen_bssids.add(mac)
            # Same story as signal: dot11 fields live inside the
            # device's own "dot11.device" sub-object, not at the top
            # level and not under a renamed alias key.
            dot11 = d.get("dot11.device")
            if isinstance(dot11, dict) and (
                    dot11.get("dot11.device.wpa_handshake_list")
                    or dot11.get("dot11.device.pmkid_packet")):
                new_handshakes.add(mac)

            if is_wifi and "AP" in dtype:
                self.seen_wifi_macs.add(mac)
                channel = d.get("kismet.device.base.channel") or ""
                crypt = d.get("kismet.device.base.crypt") or ""
                auth_label = _simple_auth(crypt)
                self._auth_by_mac[mac] = auth_label
                if isinstance(sig, (int, float)):
                    sig_i = int(sig)
                    if strongest is None or sig_i > strongest:
                        strongest = sig_i
                vendor = ""
                if HAVE_OUI:
                    v, _ = oui_lookup.identify(mac)
                    vendor = v or ""
                networks.append(Network(
                    index=str(len(networks)),
                    ssid=name or "<hidden>",
                    vendor=vendor,
                    bssid=mac,
                    channel=str(channel),
                    auth=_simple_auth(crypt),
                    rssi=str(int(sig)) if isinstance(sig, (int, float)) else "",
                    band=_band_for_channel(channel),
                ))
            elif is_bt:
                self.seen_bt_macs.add(mac)
                rssi = int(sig) if isinstance(sig, (int, float)) else -100
                bt_devices.append((mac, rssi, name or mac))
                _bristle_tap.bt_device(d, mac, name, rssi)   # [bristle] nearby alerts and skimmer suspects read along

        # [v2] publish by rebinding (atomic in CPython), never in-place.
        if new_handshakes - self.handshake_macs:
            self.handshake_macs = self.handshake_macs | new_handshakes
        counts = {}
        for label in list(self._auth_by_mac.values()):
            counts[label] = counts.get(label, 0) + 1
        self.auth_counts = counts
        self.strongest_signal = strongest
        return networks, bt_devices, None


def positioned_wigle_export(csv_path):
    """[merge-v3] The bytes of ``csv_path`` that may be sent to WiGLE.

    Returns (data, kept, dropped). upstream 0.9.20+ writes observations made without
    a fix into wardriving.csv on purpose, as lat=0.0/lon=0.0 rows that SOLO's
    startup backfill may fill in later. upstream's own WDGWars uploader skips rows
    that are still 0,0 when it parses the file; this plugin used to POST the
    raw file, so such rows would have reached WiGLE as "seen at 0,0". The two
    header lines are kept verbatim; a data row is kept only if it parses as a
    WiGLE row and has a real position, judged by the same
    LootManager._observation_position_missing() check the rest of this build uses.

    Kept rows are re-serialised from the parsed fields with the name passed
    through LootManager._csv_safe(): upstream stores names exactly as received, so
    a row written by upstream's own scanners may hold a raw comma or quote in the
    SSID; upstream's parser copes with that, WiGLE's would see shifted columns.
    Every exported row therefore has exactly eleven fields.
    """
    from watchdogs.loot_manager import LootManager
    out, kept, dropped = [], 0, 0
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        for index, line in enumerate(fh):
            fields = LootManager._wardrive_fields(line)
            if fields is None:
                if index < 2:
                    out.append(line)  # WiGLE pre-header + column header
                elif line.strip():
                    dropped += 1
                continue
            if LootManager._observation_position_missing(fields[6], fields[7]):
                dropped += 1
                continue
            fields = [str(value) for value in fields]
            fields[1] = LootManager._csv_safe(fields[1])
            if len(fields) != 11 or any("," in value or "\n" in value or "\r" in value
                                        for value in fields):
                dropped += 1  # cannot be expressed as one clean WiGLE row
                continue
            out.append(",".join(fields) + "\n")
            kept += 1
    return "".join(out).encode("utf-8"), kept, dropped


class KismetWardriveBridge(PluginBase):
    NAME = "Kismet Wardrive Bridge"
    VERSION = "2.7"

    # [merge-v3] app.py's _tick_background_plugins() only ticks plugins that
    # ask for it (see the note there: upstream 0.9.21's Wars Sync must not be ticked
    # with its overlay closed). This bridge has to run with its menu closed.
    BACKGROUND_TICK = True

    # v1.9: class-level guard so process-wide signal handlers are only
    # installed once, even if the plugin loader ever creates more than
    # one instance in a session.
    _crash_safety_installed = False
    AUTHOR = "Hardscrubbin (Wardriver 5)"

    POLL_INTERVAL = 2.0  # seconds between REST polls -- Kismet's own poll, not the 30 FPS game loop

    def __init__(self):
        super().__init__()
        self.has_overlay = True
        self.engine = None
        self._last_poll = 0.0
        self._last_diag = 0.0
        self._low_storage_warned = False
        self._known_handshake_macs = set()
        self._monitor_forced = []

        # v-next: poll_networks() does a blocking HTTP GET to Kismet's
        # REST API (timeout=8) right inside on_update() -- i.e. on the
        # pyxel main thread. A busy Kismet (lots of devices) answering
        # slowly, or just answering late, could stall the whole game's
        # render loop for up to 8 seconds, every poll. The actual HTTP
        # call + device classification now happens on this background
        # thread instead; on_update() only ever reads whatever it most
        # recently published, never blocks on the network itself. See
        # _poll_worker()/_start_poll_thread()/_stop_poll_thread().
        self._poll_thread = None
        self._poll_stop_flag = threading.Event()
        self._poll_result_lock = threading.Lock()
        # [merge-v3] One device poll at a time, and no poll's result is ever
        # thrown away. Held by the poll thread around "poll + publish" and by
        # the stop worker around "final poll + collect what was published".
        # Never taken on the game thread. See _poll_worker / _stop_worker.
        self._poll_io_lock = threading.Lock()
        self._latest_poll_result = ([], [], None)  # (networks, bt_devices, err)
        self._poll_seq = 0
        self._last_consumed_seq = 0

        # v1.7: GPS-gated wardriving.csv logging. WiGLE data with no real
        # fix is worse than useless -- it gets written as lat=0.0/lon=0.0
        # (see loot_manager.save_wardriving_network/save_wardriving_bt),
        # which is exactly what happens today on the *stock* ESP32 path
        # too, not something unique to Kismet. Rather than also write
        # that garbage from here, a device seen with no fix yet is held
        # (keyed so re-seeing it just refreshes what's held, not an
        # unbounded queue) and only actually logged to wardriving.csv
        # once app.gps_fix goes True, at which point it's logged with
        # the real fix that's current at that moment.
        self._held_networks = {}   # bssid -> Network
        self._held_bt = {}         # mac -> (rssi, name)
        self._last_no_fix_notice = 0.0
        self._gps_announced_this_session = False

        # Adapter picker overlay state
        self._overlay_active = False
        self._menu_sel = 0
        self._wifi_all = []          # list of dicts from hw.detect_wifi()
        self._ble_all = []           # list of dicts from hw.detect_ble()
        self._wifi_selected = set()  # ids
        self._ble_selected = set()   # ids
        self._wifi_monitor = {}      # id -> bool, force monitor mode before launch

        # v2.1: live distance-traveled + periodic stats terminal log.
        self._distance_m = 0.0
        self._last_pos = None        # (lat, lon) of the last accumulated fix
        self._last_good_fix = None   # (lat, lon, alt, accuracy) -- see on_update();
                                      # lets stop_wardrive() flush held devices
                                      # honestly if the live fix has since lapsed
        self._last_stats_log = 0.0

        # [v2] start/stop lifecycle. Kismet launch (up to ~45s of API
        # waits) and shutdown (up to ~10s) run on a worker thread, as in
        # Wardriver 5, instead of freezing the pyxel loop. The worker
        # posts its result here; on_update() applies it on the main
        # thread (poll thread, loot writes, msg()).
        self._lifecycle = "idle"      # idle | starting | running | stopping
        self._lifecycle_result = None
        self._relaunch_after_stop = False
        self._poll_gen = 0
        # [v2] MACs already written to bt_devices.csv this loot session --
        # save_bt_device() re-reads the whole file on every call.
        self._bt_saved = set()
        self._bt_saved_session = None
        # [v2.6] devices waiting for main-thread processing (see FRAME_BUDGET_S)
        import collections
        self._backlog = collections.deque()
        self._saved_since_log = 0

        # [merge-v3] WiGLE uploads -- "Upload to WiGLE.net" and the queued
        # retries -- run on one worker thread, like [v2]'s Kismet start/stop.
        # Until now they ran inside on_update()/the menu action, i.e. on the
        # pyxel thread: the HTTP POST (120 s timeout) plus the 1.1.1.1 ping
        # froze the whole game for as long as WiGLE took to answer. The
        # worker never touches the HUD; it posts (hud, colour, terminal)
        # tuples here and on_update() shows them (_drain_wigle_events).
        self._wigle_lock = threading.RLock()   # queue file + the busy flag
        self._wigle_busy = False               # one upload job at a time
        self._wigle_thread = None
        self._wigle_events = collections.deque()
        self._wigle_manual_path = None         # file of a running manual upload
        self._wigle_started = 0.0
        self._wigle_noted = 0.0

        # [merge-v3] Kismet health: crashes, refused logins, adapters that
        # stop capturing, and the two-adapter channel split. The poll thread
        # posts (level, text) here; on_update() shows them.
        self._kismet_events = collections.deque()
        self._source_prev = {}                 # uuid -> running, from the last check
        self._source_checked = 0.0
        self._split_mode = KISMET_SPLIT_MODE   # "band" / "off"; S in the picker
        self._split_applied = None             # frozenset of uuids the split was made for
        self._split_original = {}              # uuid -> (hop channels, rate) before a split
        self._split_report = "not started"     # for Self-Test / Hardware Check
        # [bristle] per-card channel lock (LEFT/RIGHT on a Wi-Fi row in the picker)
        self._wifi_lock = {}                   # picker: interface id -> channel, for cards that do not hop
        self._wifi_channels = {}               # interface id -> channels the card lists (read on a worker)
        self._launch_locks = {}                # (mac or id) -> channel, as the launch was confirmed
        self._locks_wanted = {}                # interface -> channel, for the Kismet that is running
        self._locks_said = set()               # (uuid, channel) already reported as not possible
        self._plan_cache = (None, [])          # the picker's per-card lines, and what they were made from
        self._crash_times = []
        self._poll_fail_count = 0
        self._poll_fail_text = ""
        self._poll_fail_logged = 0.0
        self._auth_hinted = False
        self._auth_retry_at = 0.0
        self._gps_proxy = None
        self._rescan_due = []                  # times of automatic rescans (rail just powered)

    def menu_items(self):
        return [
            PluginMenuItem(key="k", label="Kismet: Configure & Start", action="start_wardrive"),
            PluginMenuItem(key="j", label="Kismet: Stop", action="stop_wardrive"),
            PluginMenuItem(key="u", label="Kismet: Upload to WiGLE.net", action="upload_wigle"),
            PluginMenuItem(key="t", label="Kismet: Self-Test", action="self_test"),
        ]

    def self_test(self):
        """v2.1: ported from Wardriver 5's own "Self-Test (check
        everything)" button. All the individual checks this runs already
        existed somewhere in this plugin (start_wardrive() checks kismet
        is installed, poll_networks() checks credentials, stop_wardrive()
        checks GPS state) but scattered across different code paths, so
        the only way to know "is everything actually ready" was to just
        try starting a session and see what broke. This runs every check
        in one place, on demand, with a clear pass/fail line per item --
        nothing here changes any state, it's read-only diagnostics."""
        results = []

        results.append(("kismet installed", hw.have("kismet")))

        wifi = hw.detect_wifi()
        ble = hw.detect_ble()
        results.append((f"WiFi adapter(s) detected ({len(wifi)} found)", bool(wifi)))
        results.append((f"BLE adapter(s) detected ({len(ble)} found)", bool(ble)))

        gps = getattr(self.app, "gps", None)
        gps_ok = bool(gps and getattr(gps, "available", False))
        gps_fix = bool(getattr(self.app, "gps_fix", False))
        results.append(("GPS device available", gps_ok))
        results.append(("GPS has a current fix", gps_fix))

        wigle_ok = bool(WIGLE_API_NAME and WIGLE_API_TOKEN
                        and WIGLE_API_TOKEN != "CHANGE_ME")
        results.append(("WiGLE credentials configured", wigle_ok))

        already_running = bool(self.engine and self.engine.running) or self._lifecycle != "idle"
        results.append(("Kismet not already running (clean start)",
                        not already_running))
        results.append(("gpsd reachable (Kismet geotags the .kismet file)",
                        _gpsd_reachable()))
        results.append(("kismetdb_to_wiglecsv installed (stop-time backup CSV)",
                        hw.have("kismetdb_to_wiglecsv")))
        # [v2] thermal / power health from app.py's SystemMonitor
        snap = getattr(getattr(self.app, "_sysmon", None), "snapshot", None) or {}
        t = snap.get("temp_c")
        if t is not None:
            from watchdogs.sys_monitor import TEMP_WARN_C
            results.append((f"SoC temp {t:.0f}C below warn ({TEMP_WARN_C:.0f}C)", t < TEMP_WARN_C))
        if snap.get("throttle_raw") is not None:
            results.append(("No under-voltage now (USB radio power OK)", not snap.get("undervolt_now")))
            results.append(("No under-voltage since boot", not snap.get("undervolt_ever")))
            results.append(("Not throttled now", not snap.get("throttled_now")))

        self.term("[KISMET] ---- Self-Test ----")
        passed = 0
        for label, ok in results:
            mark = "PASS" if ok else "FAIL"
            self.term(f"[KISMET]   [{mark}] {label}")
            if ok:
                passed += 1
        self.term(f"[KISMET] ---- {passed}/{len(results)} checks passed ----")
        self.msg(f"Kismet self-test: {passed}/{len(results)} passed "
                 f"(see terminal for details)",
                 11 if passed == len(results) else 8)
        return results

    def on_load(self, app):
        self.app = app
        log_dir = str(Path(app._app_dir) / "loot" / "kismet_logs")
        self.engine = KismetBridgeEngine(log_dir, log=self.term)
        self._install_crash_safety()
        self._register_upload_hold()

    def _register_upload_hold(self):
        """[merge-v3] Tell LootManager that a wardriving.csv waiting in the
        WiGLE retry queue must not be deleted by upstream's Auto-clean, manual D,
        no-GPS cleanup or duplicate-export removal (they all ask
        LootManager.prepare_upload_log_discard() first). Re-run whenever the
        app swaps its LootManager; on_load() can run before app.loot exists."""
        loot = getattr(getattr(self, "app", None), "loot", None)
        if loot is None or loot is getattr(self, "_hold_loot", None):
            return
        add = getattr(loot, "add_upload_log_hold", None)
        if callable(add):
            add(self._wigle_queue_hold)
            self._hold_loot = loot

    @staticmethod
    def _path_key(path) -> str:
        return os.path.normcase(os.path.abspath(str(path)))

    def _wigle_queue_hold(self, path) -> str:
        """Hold reason for LootManager.add_upload_log_hold(); "" = not held."""
        if not self.engine:
            return ""
        key = self._path_key(path)
        if any(self._path_key(item) == key for item in self._load_wigle_queue()):
            return "queued for WiGLE upload by the Kismet bridge"
        return ""

    def _kismet_proc_alive(self) -> bool:
        """[v2] True if we own a Kismet process that hasn't exited --
        covers the window where a start worker launched Kismet but
        engine.running isn't True yet."""
        eng = self.engine
        if not eng:
            return False
        if eng.running:
            return True
        proc = getattr(eng, "proc", None)
        return proc is not None and proc.poll() is None

    def on_unload(self):
        # [merge-v3] Quitting while a manual WiGLE upload is still in flight
        # (impossible before: the game was frozen until it ended). The worker
        # is a daemon thread and dies with the game, so queue the file now;
        # the retry queue sends it on the next run.
        pending = self._wigle_manual_path
        if pending is not None and self._wigle_busy and self.engine:
            self._queue_wigle_upload(pending)
        if self._kismet_proc_alive():
            self._stop_poll_thread()
            self.engine.stop()
            self._restore_adapters()
        self._stop_gps_proxy()   # [merge-v3]

    # ------------------------------------------------------------------
    # v1.9: crash/Ctrl+C safety net -- see VERSION HISTORY above for why
    # this exists. on_unload() above is the "clean exit via game menu"
    # path; everything below is the "anything else" path.
    # ------------------------------------------------------------------

    def _safe_term(self, text: str) -> None:
        """v2.3: term()/PluginBase.term() -> app._term_add() assumes
        app.py's own __init__ has already gotten far enough to create
        self._term_lock -- but app.py calls every plugin's on_load()
        (which is where this plugin installs crash safety) BEFORE it
        reaches the line that creates that lock further down its own
        __init__. Any plugin logging anything during on_load() -- not
        just this one -- crashes the entire game on startup with
        AttributeError: 'WatchDogsGame' object has no attribute
        '_term_lock' before it ever gets on screen. This wraps every
        on_load()-time (and crash-path) log line so it can never take
        the whole app down just trying to report something: falls back
        to plain stdout (still visible in the launching terminal, just
        not the in-game OUTPUT panel) whenever the real terminal isn't
        safely callable yet."""
        if self.app is not None and hasattr(self.app, "_term_lock"):
            try:
                self.term(text)
                return
            except AttributeError:
                pass
        print(text)

    def _install_crash_safety(self):
        """Install SIGTERM/SIGINT handlers + an atexit hook, all routed
        through the same idempotent _emergency_cleanup(). Only installs
        once per process. Safe no-op if called from a non-main thread
        (signal.signal requires the main thread; atexit alone still
        covers normal interpreter shutdown in that case)."""
        if KismetWardriveBridge._crash_safety_installed:
            return
        KismetWardriveBridge._crash_safety_installed = True
        self._cleanup_done = False

        atexit.register(self._emergency_cleanup)

        self._prev_sigterm = None
        self._prev_sigint = None
        try:
            self._prev_sigterm = signal.getsignal(signal.SIGTERM)
            self._prev_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGTERM, self._handle_signal)
            signal.signal(signal.SIGINT, self._handle_signal)
            # v2.3: called from on_load(), before app.py's __init__ has
            # necessarily finished -- see _safe_term() above.
            self._safe_term("[KISMET] Crash-safety installed "
                             "(SIGTERM/SIGINT will clean up adapters first)")
        except (ValueError, OSError) as e:
            # ValueError: not the main thread -- Python only allows
            # signal handlers there. atexit still covers a normal exit;
            # only a raw signal delivery would miss cleanup in that case.
            self._safe_term(f"[KISMET] Could not install signal handlers "
                             f"({e}) -- atexit cleanup still active, but a "
                             f"Ctrl+C/SIGTERM may not restore adapters")

    def _handle_signal(self, signum, frame):
        """Run cleanup, then chain to whatever handler (if any) was
        installed before ours, so we only ADD behavior on this signal
        rather than replacing how the game already responds to it."""
        name = signal.Signals(signum).name if hasattr(signal, "Signals") else signum
        self._safe_term(f"[KISMET] Caught {name} -- restoring adapters before exit")
        self._emergency_cleanup()

        prev = self._prev_sigterm if signum == signal.SIGTERM else self._prev_sigint
        if callable(prev):
            prev(signum, frame)
        else:
            # No previous handler (or it was SIG_DFL/SIG_IGN) -- restore
            # the OS default for this signal and re-raise it at ourself,
            # so the process ends the normal way a signal ends it
            # (correct exit status, no atexit double-run) instead of us
            # swallowing it or calling sys.exit() in its place.
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    def _emergency_cleanup(self):
        """The actual cleanup, shared by SIGTERM, SIGINT, and atexit.
        Idempotent -- safe to call more than once (e.g. SIGINT's default
        Python handler raises KeyboardInterrupt, which unwinds to a
        normal interpreter exit and fires atexit too; without this
        guard that would mean two cleanup attempts)."""
        if getattr(self, "_cleanup_done", False):
            return
        self._cleanup_done = True
        try:
            self._stop_poll_thread()
        except Exception:
            pass  # best-effort -- never let this block the real cleanup below
        try:
            if self._kismet_proc_alive():
                self.engine.stop()
        except Exception as e:
            self._safe_term(f"[KISMET] Emergency stop error: {e}")
        try:
            self._restore_adapters()
        except Exception as e:
            self._safe_term(f"[KISMET] Emergency adapter-restore error: {e}")
        try:
            self._stop_gps_proxy()   # [merge-v3]
        except Exception:
            pass

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    # ------------------------------------------------------------------
    # Menu actions
    # ------------------------------------------------------------------

    def start_wardrive(self):
        """Opens the adapter picker overlay instead of guessing which
        adapters to use -- Hardscrubbin's own Wardriver 5 always shows detected
        adapters by name/MAC for manual confirm rather than silently
        auto-selecting, and multiple adapters (several WiFi dongles, or
        WiFi + a separate BLE adapter) need to be selectable together."""
        if self.engine.running or self._lifecycle != "idle":
            self.msg(f"Kismet is {self._lifecycle if self._lifecycle != 'idle' else 'running'}")
            return
        if not hw.have("kismet"):
            self.msg("kismet is not installed on this system")
            return

        # Start from a clean slate and let _rescan_adapters() do the
        # actual detection + default-selection -- see that method for
        # why: this used to be a one-shot hw.detect_wifi()/detect_ble()
        # call made only here, with nothing re-running it while the
        # picker stayed open (v2.2 fix).
        self._wifi_all = []
        self._ble_all = []
        self._wifi_selected = set()
        self._wifi_monitor = {}
        self._ble_selected = set()
        self._wifi_lock = {}             # [bristle] filled again from the saved choice, per adapter
        self._wifi_channels = {}
        # [merge-v3] An adapter on the AIO's internal USB-C port (the AC1200)
        # only exists while that rail (GPIO 23) is on. Power it before looking.
        powered = self._power_internal_usb()
        self._rescan_adapters(initial=True)
        if not self._wifi_all and not self._ble_all and not powered:
            self.msg("No WiFi or Bluetooth adapters found")
            return

        self._menu_sel = 0
        self._overlay_active = True
        self._last_adapter_scan = time.time()
        # a device behind a rail that was just switched on needs a few
        # seconds to enumerate: look again by itself, twice
        self._rescan_due = [time.time() + 4.0, time.time() + 9.0] if powered else []

    def _power_internal_usb(self) -> bool:
        """[merge-v3] Switch the AIO v2 internal-USB rail (GPIO 23) on if this
        is an AIO board and the rail is off. True only when it was switched
        on just now. Goes through the game's own power session, so the game
        knows it owns the rail and turns it off again on exit."""
        app = self.app
        if app is None or not getattr(app, "_aio_available", False):
            return False
        power = getattr(app, "_aio_power", None)
        if power is None:
            return False
        try:
            from watchdogs.aio_manager import AioManager
            if AioManager.get_feature_state("usb") is not False:
                return False              # already on, or unreadable: leave it alone
            if not power.toggle("usb", True):
                self.term("[KISMET] Could not power the AIO internal USB rail (GPIO 23)")
                return False
        except Exception as e:
            self.term(f"[KISMET] AIO internal USB rail check failed: {e}")
            return False
        app._usb_enabled = True
        self.term("[KISMET] AIO internal USB rail (GPIO 23) switched on -- an adapter on "
                  "that port appears in a few seconds (the list rescans by itself)")
        return True

    def _stop_gps_proxy(self) -> None:
        proxy, self._gps_proxy = getattr(self, "_gps_proxy", None), None
        if proxy is not None:
            proxy.stop()

    def _kismet_gps_endpoint(self):
        """[merge-v3] Where Kismet should read GPS from: None = gpsd itself
        (one receiver, exactly as v2), or (host, port) of the local filter
        that passes on only the receiver the game follows (two receivers)."""
        if not KISMET_GPS_PROXY:
            return None
        gps = getattr(self.app, "gps", None)
        health = getattr(gps, "health", None)
        if not callable(health):
            return None
        try:
            state = health()
        except Exception:
            return None
        if max(len(state.get("devices", ())), len(state.get("configured", ()))) < 2:
            return None
        proxy = getattr(self, "_gps_proxy", None)
        if proxy is None or not proxy.running:
            try:
                proxy = _gpsd_proxy.GpsdDeviceProxy(
                    lambda: getattr(gps, "chosen_device", None),
                    upstream=(GPSD_HOST, GPSD_PORT), port=KISMET_GPS_PROXY_PORT)
                proxy.start()
            except OSError as e:
                self.term(f"[KISMET] GPS filter could not start ({e}) -- Kismet reads gpsd "
                          f"directly; with two receivers its own geotags may jump")
                return None
            self._gps_proxy = proxy
        self.term(f"[KISMET] Two GPS receivers: Kismet reads only the one in use, "
                  f"through the local filter on port {proxy.port}")
        return ("127.0.0.1", proxy.port)

    def _adapters_pref_path(self) -> Path:
        return Path(self.app._app_dir) / "kismet_adapters.json"

    def _load_adapter_prefs(self):
        """Last-confirmed adapter selection, by MAC (not interface name/
        id -- those get renumbered by Linux on replug; MACs don't).
        Returns {"wifi_macs": [...], "ble_macs": [...]} or None if
        nothing's been saved yet (first-ever run)."""
        try:
            with open(self._adapters_pref_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "wifi_macs" in data and "ble_macs" in data:
                return data
        except (OSError, ValueError):
            pass
        return None

    def _save_adapter_prefs(self):
        """Called right after a successful launch -- persists the MACs
        of whatever's selected right now, so the NEXT time the picker
        opens (or an auto-rotation restarts, see start_wardrive's
        AUTO_ROTATE_INTERVAL) it defaults to exactly these adapters
        instead of auto-selecting everything it happens to see. This is
        the direct fix for having to race a rescan to re-pick the Alfa +
        UB500 combo every time before it reconnects by itself."""
        wifi_macs = sorted({w["mac"] for w in self._wifi_all
                             if w["id"] in self._wifi_selected and w.get("mac")})
        ble_macs = sorted({b["mac"] for b in self._ble_all
                            if b["id"] in self._ble_selected and b.get("mac")})
        if not wifi_macs and not ble_macs:
            return
        # [bristle] the channel lock is kept per adapter (by MAC), also for an adapter that is
        # not plugged in today: only the cards in front of us are changed.
        locks = dict((self._load_adapter_prefs() or {}).get("wifi_locks") or {})
        chosen = getattr(self, "_wifi_lock", None) or {}
        for w in self._wifi_all:
            if not w.get("mac"):
                continue
            if chosen.get(w["id"]):
                locks[w["mac"]] = str(chosen[w["id"]])
            else:
                locks.pop(w["mac"], None)
        try:
            with open(self._adapters_pref_path(), "w", encoding="utf-8") as fh:
                json.dump({"wifi_macs": wifi_macs, "ble_macs": ble_macs, "wifi_locks": locks}, fh)
        except OSError as exc:
            self.term(f"[KISMET] Could not save adapter preference: {exc}")

    # ------------------------------------------------------------------
    # [bristle] Per-card channel lock and the per-card channel plan
    # ------------------------------------------------------------------

    def _read_card_channels(self, ids):
        """Worker thread: ask each card which channels it can tune (`iw phy`, read only)."""
        for iface in ids:
            try:
                text = hw.phy_info(iface)
            except Exception:
                text = ""
            self._wifi_channels[iface] = kx.parse_iw_channels(text)

    def _step_lock(self, iface, step):
        """LEFT/RIGHT on a Wi-Fi row: hop -> the card's first channel -> ... -> its last -> hop."""
        choices = [None] + kx.lock_choices(self._wifi_channels.get(iface))
        current = self._wifi_lock.get(iface)
        index = choices.index(current) if current in choices else 0
        chosen = choices[(index + step) % len(choices)]
        if chosen is None:
            self._wifi_lock.pop(iface, None)
        else:
            self._wifi_lock[iface] = chosen

    def _plan_rows(self):
        """[(interface, text)]: what each ticked Wi-Fi card will do. Made again only when the
        selection, a lock, a card's channel list or the split switch changes."""
        channels = getattr(self, "_wifi_channels", {})
        locks = getattr(self, "_wifi_lock", {})
        picked = [w for w in self._wifi_all if w["id"] in self._wifi_selected]
        key = (tuple((w["id"], locks.get(w["id"]), tuple(channels.get(w["id"]) or ())) for w in picked),
               self._split_mode)
        if getattr(self, "_plan_cache", (None,))[0] != key:
            cards = [(w["id"], w["id"], channels.get(w["id"]) or []) for w in picked]
            wanted = {w["id"]: locks[w["id"]] for w in picked if locks.get(w["id"])}
            self._plan_cache = (key, kx.picker_plan(cards, wanted, split_on=self._split_mode == "band"))
        return self._plan_cache[1]

    def _rescan_adapters(self, initial: bool = False):
        """v2.2: re-run hw.detect_wifi()/detect_ble() while the adapter
        picker is open, instead of only once when it was first opened.

        Before v2.2, an adapter unplugged and replugged (or plugged in
        for the first time after the menu was already open) simply
        never appeared -- the picker's row list was built once, in
        start_wardrive(), from whatever hw.detect_wifi()/detect_ble()
        saw at that single instant, and nothing ever called them again
        until the picker was closed (ESC) and reopened. Reported
        exactly as: adapter missing at open time -> unplug/replug ->
        still missing -> has to back all the way out and reopen the
        menu to see it. v2.2 fixed that by also re-running this on a
        short automatic timer while the picker was open, on top of the
        'R' key. v-next removed that automatic timer again (see
        _picker_on_update) -- on a rig with several USB radios sharing
        a bus, it was fast enough to surface real, transient USB
        enumeration churn as the picker's own rows bouncing in and out.
        'R' alone still covers the original replug case this was built
        for; it's just no longer automatic.

        Preserves the user's own in-progress choices for adapters that
        were already in the list (an already-ticked/unticked box, or a
        toggled monitor-mode preference, is never silently reset by a
        rescan) -- it only adds newly-appeared adapters (with the same
        default-selection logic start_wardrive() used to apply once at
        open time) and drops ones that disappeared (unplugged) from the
        selected-sets and monitor-mode map, so a since-vanished
        interface can't get "launched" against.
        """
        new_wifi = hw.detect_wifi()
        new_ble = hw.detect_ble()

        old_wifi_ids = {w["id"] for w in self._wifi_all}
        old_ble_ids = {b["id"] for b in self._ble_all}
        new_wifi_ids = {w["id"] for w in new_wifi}
        new_ble_ids = {b["id"] for b in new_ble}

        added_wifi_ids = new_wifi_ids - old_wifi_ids
        removed_wifi_ids = old_wifi_ids - new_wifi_ids
        added_ble_ids = new_ble_ids - old_ble_ids
        removed_ble_ids = old_ble_ids - new_ble_ids

        self._wifi_all = new_wifi
        self._ble_all = new_ble

        # Unplugged -- drop from selection + monitor-mode state so a
        # gone interface is never carried into _launch_from_picker().
        self._wifi_selected -= removed_wifi_ids
        self._ble_selected -= removed_ble_ids
        for iface in removed_wifi_ids:
            self._wifi_monitor.pop(iface, None)
            self._wifi_lock.pop(iface, None)          # [bristle]
            self._wifi_channels.pop(iface, None)

        # Newly appeared -- v-next: if a MAC allowlist was saved from a
        # prior successful launch (_save_adapter_prefs(), written right
        # after engine.start() succeeds), ONLY auto-select a newly-
        # appeared adapter whose MAC is on that list. This is what makes
        # "only ever reconnect with my Alfa + UB500" stick across USB
        # replugs/renumbering instead of every new interface the kernel
        # happens to expose getting auto-ticked and fighting each other
        # for power on the picker screen. Falls back to the original
        # "prefer monitor-capable, else select everything" behavior only
        # on a first-ever run, before any prefs have been saved.
        prefs = self._load_adapter_prefs()
        if added_wifi_ids:
            added_wifi = [w for w in new_wifi if w["id"] in added_wifi_ids]
            for w in added_wifi:
                self._wifi_monitor[w["id"]] = w["can_monitor"]
            # [bristle] a card comes back with the channel lock saved for it, and its channel
            # list is read on a worker thread (the plan lines fill in when it arrives)
            saved_locks = (prefs or {}).get("wifi_locks") or {}
            for w in added_wifi:
                if w.get("mac") and saved_locks.get(w["mac"]):
                    self._wifi_lock[w["id"]] = str(saved_locks[w["mac"]])
            threading.Thread(target=self._read_card_channels, args=([w["id"] for w in added_wifi],),
                             name="kismet-card-channels", daemon=True).start()
            if prefs is not None:
                allowed_macs = set(prefs.get("wifi_macs") or [])
                keep = {w["id"] for w in added_wifi if w.get("mac") in allowed_macs}
                self._wifi_selected |= keep
            else:
                can_monitor_added = [w for w in added_wifi if w["can_monitor"]]
                default_added = can_monitor_added or added_wifi
                self._wifi_selected |= {w["id"] for w in default_added}
        if added_ble_ids:
            if prefs is not None:
                allowed_macs = set(prefs.get("ble_macs") or [])
                added_ble = [b for b in new_ble if b["id"] in added_ble_ids]
                keep = {b["id"] for b in added_ble if b.get("mac") in allowed_macs}
                self._ble_selected |= keep
            else:
                self._ble_selected |= added_ble_ids

        if not initial and (added_wifi_ids or removed_wifi_ids
                             or added_ble_ids or removed_ble_ids):
            parts = []
            if added_wifi_ids:
                parts.append(f"+{len(added_wifi_ids)} WiFi")
            if added_ble_ids:
                parts.append(f"+{len(added_ble_ids)} BLE")
            if removed_wifi_ids:
                parts.append(f"-{len(removed_wifi_ids)} WiFi")
            if removed_ble_ids:
                parts.append(f"-{len(removed_ble_ids)} BLE")
            self.term(f"[KISMET] Adapter list changed: {', '.join(parts)}")

        # The row list may have shrunk (an adapter disappeared) -- keep
        # the cursor on a real row rather than pointing past the end.
        rows = self._picker_rows()
        if rows and self._menu_sel >= len(rows):
            self._menu_sel = len(rows) - 1

    def _picker_rows(self):
        """Build the picker's row list: (kind, id_or_None, label)."""
        rows = []
        for w in self._wifi_all:
            rows.append(("wifi", w["id"], w))
        for b in self._ble_all:
            rows.append(("ble", b["id"], b))
        rows.append(("start", None, None))
        rows.append(("cancel", None, None))
        return rows

    def _ensure_gps(self) -> bool:
        """[merge-v3] GPS auto-on at wardrive start, after WDGoNG 0.10.0's
        ensure_gps (wdgong_ui/backend.py; Copyright (c) 2025-2026 LOCOSP,
        PolyForm Noncommercial 1.0.0, see plugins/_ng/NOTICE): a wardrive
        that starts with GPS switched off switches it on, and says whether a
        fix is there yet. True when GPS is on (a fix may still be pending:
        rows are held and geotagged once it locks, as before).

        It goes through the game's own GPS toggle, so the AIO rail, the
        HUD state and the messages are exactly those of SYSTEM > GPS. When
        gpsd is merely not up yet the game is already looking for it every
        5 s (GpsManager.gpsd_reconnect_tick); nothing is toggled then, which
        also keeps the toggle's blocking serial-port probe out of this path."""
        app = self.app
        gps = getattr(app, "gps", None)
        if gps is None:
            return False
        if getattr(gps, "gpsd_retrying", False):
            self.term("[KISMET] GPS: gpsd is not answering -- still checking every 5 s; "
                      "rows are held until there is a fix")
            return False
        if not getattr(app, "_gps_enabled", False):
            toggle = getattr(app, "_toggle_gps", None)
            if not callable(toggle):
                return False
            self.term("[KISMET] Wardriving needs GPS -- switching it on...")
            try:
                toggle()
            except Exception as e:
                self.term(f"[KISMET] GPS could not be switched on: {e}")
                return False
        if not getattr(app, "_gps_enabled", False) or not getattr(gps, "available", False):
            self.term("[KISMET] GPS is not running yet -- rows are held until there is a fix")
            return False
        if not getattr(app, "gps_fix", False):
            self.term("[KISMET] GPS is on, searching for a fix -- rows are held and "
                      "geotagged once it locks")
        return True

    def _launch_from_picker(self, rotation=False):
        """[v2] Main-thread half of a launch: snapshot the selection (by
        MAC as well as current name), reset session counters, hand the
        slow part to _start_worker() on a thread.

        rotation=True (auto-rotation relaunch): the DRIVE carries on, so
        drive-level state is kept -- start time, distance, last position,
        last good fix, and any records still held for a GPS fix. Only the
        per-Kismet-segment counters restart."""
        if self._lifecycle != "idle":
            self.msg(f"Kismet is {self._lifecycle}")
            return
        wifi_sel = [(w["id"], w.get("mac"), bool(self._wifi_monitor.get(w["id"])))
                    for w in self._wifi_all if w["id"] in self._wifi_selected]
        ble_sel = [(b["id"], b.get("mac"))
                   for b in self._ble_all if b["id"] in self._ble_selected]
        if not wifi_sel and not ble_sel:
            self.msg("Select at least one adapter first")
            return
        # [bristle] the channel locks go with the launch, by MAC where the card has one (a card
        # may come back under another name), by interface name otherwise
        chosen = getattr(self, "_wifi_lock", None) or {}
        self._launch_locks = {(mac or iface): str(chosen[iface])
                              for iface, mac, _mon in wifi_sel if chosen.get(iface)}

        self._overlay_active = False
        if not rotation:
            self._ensure_gps()   # [merge-v3] GPS auto-on at wardrive start
            self._gps_announced_this_session = False
            self._session_start_time = time.time()
            self._distance_m = 0.0
            self._last_pos = None
            self._last_good_fix = None
            self._segment_no = 0
            self._held_networks.clear()
            self._held_bt.clear()
            self._backlog.clear()
        self._segment_no = getattr(self, "_segment_no", 0) + 1
        self._segment_start_time = time.time()
        self._seg_peak_temp = None
        self._seg_throttled = False
        self._seg_undervolt = False
        self._seg_peak_rss = None
        self._last_rss_check = 0.0
        self._last_stats_log = 0.0
        self._lifecycle = "starting"
        self.msg("Kismet starting... (game stays live, see terminal)"
                 if not rotation else "Kismet rotating segment...")
        threading.Thread(target=self._start_worker, args=(wifi_sel, ble_sel),
                         name="kismet-start", daemon=True).start()

    def _start_worker(self, wifi_sel, ble_sel):
        """[v2] Off-main-thread launch, following Wardriver 5's
        _start_worker: resolve every adapter by MAC right now, set
        monitor mode (leave out an adapter that won't switch), bring BLE
        up, write the hardware manifest, then start Kismet. Only logs via
        term() (lock-protected); the result goes to on_update()."""
        forced, wifi_ready, ble_ready = [], [], []
        ok = False
        locks = {}                       # [bristle] interface -> channel, for this launch
        launch_locks = dict(getattr(self, "_launch_locks", None) or {})
        try:
            for iface, mac, want_mon in wifi_sel:
                cur = iface
                if mac:
                    found = hw.resolve_wifi_by_mac(mac)
                    if not found:
                        self.term(f"[KISMET] WiFi {mac} isn't plugged in right now -- skipping")
                        continue
                    cur = found["id"]
                    if cur != iface:
                        self.term(f"[KISMET] {mac} is now {cur} (was {iface}) -- using {cur}")
                if want_mon:
                    # Scoped release of JUST this interface (W5 lesson:
                    # the old system-wide `airmon-ng check kill` took the
                    # desktop's WiFi down too).
                    self.term(f"[KISMET] Releasing {cur} from NetworkManager...")
                    try:
                        hw.kill_interferers(cur, log=self.term)
                    except Exception as e:
                        self.term(f"[KISMET] Could not release {cur}: {e}")
                    try:
                        mon_ok, mon_msg = hw.set_monitor(cur, True, log=self.term)
                    except Exception as e:
                        mon_ok, mon_msg = False, str(e)
                    if not mon_ok:
                        self.term(f"[KISMET] {mon_msg} -- leaving {cur} out of this run")
                        try:
                            hw.release_interface_back(cur, log=self.term)
                        except Exception:
                            pass
                        continue
                    forced.append(cur)
                wifi_ready.append(cur)
                wanted = launch_locks.get(mac or iface)
                if wanted:               # [bristle] can this card tune the channel it is locked to?
                    try:
                        listed = kx.parse_iw_channels(hw.phy_info(cur))
                    except Exception:
                        listed = []
                    if listed and kx.lock_name(wanted, listed) is None:
                        self.term(f"[KISMET] {cur} cannot tune channel {wanted} -- the lock is "
                                  f"dropped for this run and it hops like the others")
                    else:
                        locks[cur] = wanted

            for hci, mac in ble_sel:
                cur = hci
                if mac:
                    found = hw.resolve_ble_by_mac(mac)
                    if not found:
                        self.term(f"[KISMET] BLE {mac} isn't present right now -- skipping")
                        continue
                    cur = found["id"]
                up_ok, up_msg = hw.bring_up_ble(cur, log=self.term)
                self.term(f"[KISMET] {up_msg}")
                if up_ok:
                    ble_ready.append(cur)

            if not wifi_ready and not ble_ready:
                self.term("[KISMET] No usable adapters after setup -- not starting")
            else:
                self._write_hardware_manifest(wifi_ready, ble_ready)
                use_gps = KISMET_GPSD_ENABLED and _gpsd_reachable()
                if KISMET_GPSD_ENABLED and not use_gps:
                    self.term("[KISMET] gpsd not reachable -- Kismet starts without "
                              "its own GPS (game geotagging is unaffected)")
                self.term(f"[KISMET] Starting with WiFi: {wifi_ready or '(none)'}, "
                          f"BLE: {ble_ready or '(none)'}")
                endpoint = self._kismet_gps_endpoint() if use_gps else None   # [merge-v3]
                # [bristle] only named when there is one, like gps_endpoint: an engine without
                # the argument (an older one, a stand-in) is called exactly as before
                extra = {}
                if endpoint:
                    extra["gps_endpoint"] = endpoint
                if locks:
                    extra["channel_locks"] = locks
                self._locks_wanted = dict(locks)
                self._locks_said = set()
                ok = self.engine.start(wifi_ready, ble_ready, gps_override=use_gps, **extra)
        except Exception as e:
            self.term(f"[KISMET] Start error: {e}")
            ok = False
        with self._poll_result_lock:
            self._lifecycle_result = ("start", ok, forced)

    def _write_hardware_manifest(self, wifi_ifaces, ble_adapters):
        """[v2] Ported from Wardriver 5: a snapshot of exactly what was
        plugged in and used at launch, saved next to the .kismet file."""
        try:
            ts = time.strftime("%Y-%m-%d_%H-%M-%S")
            path = os.path.join(self.engine.log_dir, f"hardware-{ts}.txt")
            _, lsusb_out, _ = hw._run(["lsusb"])
            _, iw_out, _ = hw._run(["iw", "dev"])
            _, hci_out, _ = hw._run(["hciconfig", "-a"])
            gps_dev = getattr(getattr(self.app, "gps", None), "device", None)
            with open(path, "w") as f:
                f.write("Kismet Wardrive Bridge -- hardware manifest\n")
                f.write(f"Written: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                f.write(f"WiFi interfaces used: {', '.join(wifi_ifaces) or '(none)'}\n")
                f.write(f"Bluetooth adapters used: {', '.join(ble_adapters) or '(none)'}\n")
                f.write(f"Game GPS device: {gps_dev or '(none)'}\n\n")
                f.write("--- lsusb ---\n" + lsusb_out + "\n\n")
                f.write("--- iw dev ---\n" + iw_out + "\n\n")
                f.write("--- hciconfig -a ---\n" + hci_out + "\n")
        except Exception as e:
            self.term(f"[KISMET] Could not write hardware manifest: {e}")

    def _stop_worker(self, forced):
        """[v2] Off-main-thread shutdown: stop Kismet, restore adapters,
        report back, THEN (still off-thread) convert the newest .kismet
        to a backup .wiglecsv. The result is posted before the export so
        an auto-rotation relaunch never waits on a slow conversion."""
        newest = None
        final = None
        # [v2] One last delta poll while Kismet is still up, so devices
        # seen since the previous poll (up to POLL_INTERVAL + the stop
        # latency) reach wardriving.csv instead of only the .kismet file.
        #
        # [merge-v3] ...and after the poll thread's own last poll, not next to
        # it. A poll that was in flight when Stop was pressed used to finish
        # on the poll thread, mark its devices as seen (poll_networks() drops
        # a device whose last_time has not moved) and then be thrown away
        # because the stop flag was set; this final poll then skipped the
        # same devices as "already seen". They reached the .kismet file but
        # never wardriving.csv (found by tools/sim: 60 of 260 networks
        # missing, about one run in a hundred). Now the poll thread publishes
        # every poll it finishes, this worker waits for it (the lock), does
        # the final poll, and takes whatever was published and not yet used.
        # This is a worker thread: waiting here never stalls the game.
        handed_over = self._poll_io_lock.acquire(timeout=POLL_HANDOVER_WAIT)
        try:
            try:
                if self.engine.running:
                    wl = getattr(self.app, "_whitelist", None)
                    n, b, err = self.engine.poll_networks(
                        is_blocked=wl.is_blocked if wl is not None else None)
                    if err:
                        self.term(f"[KISMET] Final poll before stop failed: {err}")
                    else:
                        final = (n, b)
            except Exception as e:
                self.term(f"[KISMET] Final poll before stop failed: {e}")
            leftover = self._take_unconsumed_poll()
            if leftover:
                final = (leftover[0] + list(final[0] if final else []),
                         leftover[1] + list(final[1] if final else []))
        finally:
            if handed_over:
                self._poll_io_lock.release()
        try:
            self.engine.stop()
            self._restore_adapters(forced)
            import glob
            logs = sorted(glob.glob(os.path.join(self.engine.log_dir, "*.kismet")),
                          key=os.path.getmtime, reverse=True)
            newest = logs[0] if logs else None
        except Exception as e:
            self.term(f"[KISMET] Stop error: {e}")
        with self._poll_result_lock:
            self._lifecycle_result = ("stop", True, {"final": final, "finish": True})
        if newest and hw.have("kismetdb_to_wiglecsv"):
            out_csv = os.path.splitext(newest)[0] + ".wiglecsv"
            # W5 Rev 5.1: 240s, and safe to redo later -- the .kismet is
            # never touched by a failed conversion.
            rc, out, err = hw._run(["kismetdb_to_wiglecsv", "--in", newest,
                                    "--out", out_csv], timeout=240)
            if rc == 0 and os.path.exists(out_csv):
                self.term(f"[KISMET] Backup WiGLE CSV from the .kismet log: "
                          f"{os.path.basename(out_csv)} (not uploaded)")
            else:
                self.term(f"[KISMET] Backup CSV export failed ({err or out}) -- "
                          f"{os.path.basename(newest)} is intact")

    def _apply_lifecycle_result(self):
        """[v2] Main thread: apply whatever a start/stop worker posted."""
        with self._poll_result_lock:
            res = self._lifecycle_result
            self._lifecycle_result = None
        if not res:
            return
        kind, ok, payload = res
        if kind == "start":
            forced = payload
            self._monitor_forced = list(forced or [])
            if ok:
                self._lifecycle = "running"
                # [merge-v3] a fresh Kismet: its sources are checked (and the
                # channel split made) from scratch by the poll thread
                self._source_prev = {}
                self._source_checked = 0.0
                self._split_applied = None
                self._split_original = {}
                self._poll_fail_count = 0
                self._poll_fail_text = ""
                self._start_poll_thread()
                self._save_adapter_prefs()
                self.msg("Kismet wardriving started")
            else:
                self.msg("Kismet failed to start -- see terminal log")
                self._lifecycle = "stopping"
                to_restore = self._monitor_forced
                self._monitor_forced = []
                threading.Thread(target=self._restore_then_idle, args=(to_restore,),
                                 name="kismet-restore", daemon=True).start()
        elif kind == "stop":
            payload = payload or {}
            relaunch = self._relaunch_after_stop
            if payload.get("finish"):
                final = payload.get("final")
                if final:
                    n, b = final
                    if n or b:
                        self._consume_batch(n, b)
                        self.term(f"[KISMET] Final poll at stop: {len(n)} WiFi + "
                                  f"{len(b)} BLE device(s) processed")
                self._finish_stop(rotating=relaunch)
            self._lifecycle = "idle"
            if relaunch:
                self._relaunch_after_stop = False
                self._launch_from_picker(rotation=True)
            else:
                self._relaunch_after_stop = False
                self._stop_gps_proxy()   # [merge-v3] the drive is over
                self.msg("Kismet wardriving stopped")

    def _restore_then_idle(self, forced):
        try:
            self._restore_adapters(forced)
        finally:
            with self._poll_result_lock:
                self._lifecycle_result = ("stop", True, {"finish": False})

    def stop_wardrive(self):
        if self._lifecycle != "running" or not self.engine.running:
            self.msg("Kismet is not running" if self._lifecycle == "idle"
                     else f"Kismet is {self._lifecycle}")
            return
        self._lifecycle = "stopping"
        self._stop_poll_thread()
        # [v2] a batch the poll thread published but on_update() hadn't
        # consumed yet used to be dropped here. Consume it now.
        with self._poll_result_lock:
            pending = self._poll_seq != self._last_consumed_seq
            n, b, err = self._latest_poll_result
            self._last_consumed_seq = self._poll_seq
        if pending and not err and (n or b):
            self._consume_batch(n, b)
        # [v2] held-record flush + summary now run in _finish_stop(), after
        # the stop worker's final poll has been processed.
        forced = list(self._monitor_forced)
        self._monitor_forced = []
        self.msg("Kismet stopping...")
        threading.Thread(target=self._stop_worker, args=(forced,),
                         name="kismet-stop", daemon=True).start()

    def _finish_stop(self, rotating=False):
        """[v2] Main thread, after Kismet has stopped and the final poll is
        in: flush held records under the last good fix (or carry them into
        the next segment on a rotation), then write the segment summary."""
        # [v2.6] finish every queued device first (no frame budget at stop),
        # so the held set and the summary reflect everything captured.
        self._drain_backlog(None)
        if self._saved_since_log:
            self.term(f"[KISMET] +{self._saved_since_log} network(s) logged to wardriving.csv")
            self._saved_since_log = 0
        # v-next: anything still held here used to be discarded outright
        # on stop -- real devices Kismet genuinely captured, thrown away
        # just because the live GPS fix wasn't current at this exact
        # instant. That's too aggressive: a fix picked up earlier in the
        # SAME session (self._last_good_fix) is still an honest, real
        # position -- a few minutes stale at worst -- and using it to
        # flush these held devices beats losing the data outright. Only
        # devices seen before a fix EVER arrived this session (no
        # _last_good_fix at all) are genuinely unrecoverable and still
        # reported as lost, same as before.
        held = len(self._held_networks) + len(self._held_bt)
        if held and rotating:
            # [v2] a rotation is not the end of the drive: keep them held so
            # the next live fix writes them with a CURRENT position (or the
            # final stop uses the last good fix). Before v2, a rotation with
            # no fix yet threw them away.
            self.term(f"[KISMET] Rotation: carrying {held} held record(s) "
                      f"(waiting for GPS) into the next segment")
            self._write_session_summary(0, carried=held)
            return
        if held and self._last_good_fix is not None:
            flushed_w = 0
            # [merge-v3] positioned_only/append_new: see _save_wifi_or_hold().
            for held_net in self._held_networks.values():
                if self.app.loot and self.app.loot.save_wardriving_network(
                        held_net, override_fix=self._last_good_fix,
                        positioned_only=True, append_new=True):
                    flushed_w += 1
            flushed_b = 0
            for held_mac, (held_rssi, held_name) in self._held_bt.items():
                if self.app.loot and self.app.loot.save_wardriving_bt(
                        held_mac, held_rssi, held_name,
                        override_fix=self._last_good_fix,
                        positioned_only=True, append_new=True):
                    flushed_b += 1
            self.term(f"[KISMET] GPS fix not current at stop -- wrote "
                      f"{flushed_w} WiFi + {flushed_b} BLE held record(s) "
                      f"to wardriving.csv using the last known fix this session")
            held = 0
        elif held:
            self.term(f"[KISMET] Session ended with no GPS fix at all -- "
                      f"{held} device(s) were never written to wardriving.csv")
        self._write_session_summary(held)
        self._held_networks.clear()
        self._held_bt.clear()

    def _write_session_summary(self, held_no_fix: int, carried: int = 0):
        """v2.0: ported from Wardriver 5's own "Session summary saved"
        file, written unconditionally on stop even if a later CSV
        export/WiGLE upload fails -- so there's always a plain-text
        record of what a session actually captured, independent of
        whatever happens to the upload pipeline afterward. Best-effort;
        never blocks stop_wardrive() over a write failure."""
        try:
            start = getattr(self, "_session_start_time", None)
            duration_s = int(time.time() - start) if start else 0
            mins, secs = divmod(duration_s, 60)
            w = len(self.engine.seen_wifi_macs) if self.engine else 0
            b = len(self.engine.seen_bt_macs) if self.engine else 0
            hs = len(self._known_handshake_macs)
            ts = time.strftime("%Y-%m-%d_%H-%M-%S")
            # v2.1: auth-type breakdown + strongest signal seen are kept
            # on the engine and only reflect the MOST RECENT poll (each
            # poll's counts get reset in poll_networks(), not accumulated
            # across the session) -- good enough as a "what does the air
            # look like right now" snapshot at stop time, called out as
            # such rather than implied to be a full-session total.
            auth_counts = getattr(self.engine, "auth_counts", None) or {}
            auth_str = (", ".join(f"{label}:{count}"
                                   for label, count in sorted(auth_counts.items()))
                        or "n/a")
            sig = getattr(self.engine, "strongest_signal", None)
            sig_str = f"{sig} dBm" if sig is not None else "n/a"
            # [v2] segment = one Kismet process; drive = since you pressed
            # Start (auto-rotation keeps one drive across many segments).
            seg_start = getattr(self, "_segment_start_time", None) or start
            seg_s = int(time.time() - seg_start) if seg_start else 0
            seg_m, seg_sec = divmod(seg_s, 60)
            peak_t = getattr(self, "_seg_peak_temp", None)
            peak_rss = getattr(self, "_seg_peak_rss", None)
            lines = [
                f"Kismet Wardrive Bridge -- session summary",
                f"Ended: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"Segment: {getattr(self, '_segment_no', 1)} "
                f"({seg_m}m {seg_sec}s this Kismet run)",
                f"Drive so far: {mins}m {secs}s, {_fmt_distance(self._distance_m)}",
                f"WiFi networks seen (cumulative): {w}",
                f"BLE devices seen (cumulative): {b}",
                f"Handshakes recorded: {hs}",
                f"Held with no GPS fix (never written): {held_no_fix}",
                f"Held, carried into next segment: {carried}",
                f"Auth-type breakdown (this segment): {auth_str}",
                f"Strongest signal (last poll): {sig_str}",
                f"Peak SoC temp (this segment): "
                f"{f'{peak_t:.1f} C' if peak_t is not None else 'n/a'}",
                f"Throttling seen: {'YES' if getattr(self, '_seg_throttled', False) else 'no'}",
                f"Under-voltage seen: {'YES' if getattr(self, '_seg_undervolt', False) else 'no'}",
                f"Kismet peak RAM: {f'{peak_rss} MB' if peak_rss is not None else 'n/a'}",
                "",
            ]
            log_dir = self.engine.log_dir if self.engine else None
            if not log_dir:
                return
            path = os.path.join(log_dir, f"summary-{ts}.txt")
            with open(path, "w") as f:
                f.write("\n".join(lines))
            self.term(f"[KISMET] Session summary saved: {path}")
        except Exception as e:
            self.term(f"[KISMET] Could not write session summary: {e}")

    def _restore_adapters(self, forced=None):
        """Undo exactly what _launch_from_picker did -- set every
        interface THIS plugin put into monitor mode back to managed and
        hand it back to NetworkManager, so normal WiFi works again
        without a reboot. Never touches an interface it didn't force
        into monitor mode itself. [v2] takes an explicit list when a
        worker thread is doing the restore."""
        if forced is None:
            forced = list(getattr(self, "_monitor_forced", []))
            self._monitor_forced = []
        for iface in forced:
            try:
                hw.set_monitor(iface, False, log=self.term)
            except Exception as e:
                self.term(f"[KISMET] Could not restore {iface} to managed mode: {e}")
            try:
                hw.release_interface_back(iface, log=self.term)
            except Exception as e:
                self.term(f"[KISMET] Could not hand {iface} back to NetworkManager: {e}")

    def _maybe_rotate_session(self) -> bool:
        """v-next: optional session auto-rotation
        (KISMET_SESSION_ROTATE_MINUTES in config.py, WDG_KISMET_ROTATE_MIN
        in secrets.conf; 0 = off, the default). A single very long
        capture means an ever-growing wardriving.csv/bt_devices.csv --
        every future "is this BSSID already logged" scan gets slower --
        and a bigger final flush for stop_wardrive()/the kill switch (K)
        to wait on. When enabled, this stops Kismet and immediately
        relaunches it on the exact same adapters, no picker screen and no
        re-selecting anything (stop_wardrive() never touches
        self._wifi_selected/self._ble_selected), so each rotation's stop
        is small and fast instead of one huge one whenever you eventually
        hit K. Returns True if a rotation happened this frame (caller
        should skip the rest of on_update() for this frame, same as the
        kill-switch path above), False otherwise.

        [v2] Two changes. (1) The stop runs on a worker thread and the
        relaunch happens when it reports back (_apply_lifecycle_result),
        so a rotation no longer freezes the game for the stop + the new
        launch. The relaunch resolves adapters by MAC, so a renamed
        interface is picked up. (2) The premise above is mostly gone: the
        wardriving.csv slowdown was fixed in loot_manager (in-memory dedup
        index), and wardriving.csv/bt_devices.csv belong to the GAME
        session, so a Kismet rotation never shrank them anyway. Rotation
        now mainly caps the size of each .kismet file. Leave it off unless
        you want that.
        """
        if KISMET_SESSION_ROTATE_MINUTES <= 0 and KISMET_ROTATE_RSS_MB <= 0:
            return False
        if self._lifecycle != "running" or not self.engine or not self.engine.running:
            return False
        # [v2] timer is per SEGMENT (one Kismet run), not since the drive began,
        # otherwise every frame after the first rotation would rotate again.
        start = getattr(self, "_segment_start_time", None) or getattr(self, "_session_start_time", None)
        if start is None:
            return False
        reason = None
        if (KISMET_SESSION_ROTATE_MINUTES > 0
                and time.time() - start >= KISMET_SESSION_ROTATE_MINUTES * 60):
            reason = f"{KISMET_SESSION_ROTATE_MINUTES} min segment length"
        elif KISMET_ROTATE_RSS_MB > 0 and time.time() - start >= 60:
            # [v2] memory cap. Kismet keeps every device it has ever seen in
            # RAM for the life of the process; on a long, dense drive that
            # grows without bound. A restart resets it. Checked every 30s,
            # never within the first minute of a segment (no restart loops).
            if time.time() - getattr(self, "_last_rss_check", 0.0) >= 30.0:
                self._last_rss_check = time.time()
                rss = self._kismet_rss_mb()
                if rss is not None and rss >= KISMET_ROTATE_RSS_MB:
                    reason = f"Kismet RAM {rss} MB >= {KISMET_ROTATE_RSS_MB} MB cap"
        if reason is None:
            return False
        self.term(f"[KISMET] Auto-rotating ({reason}) -- relaunching on the "
                  f"same adapters; the drive's held records and stats carry over")
        self._relaunch_after_stop = True
        self.stop_wardrive()
        return True

    def upload_wigle(self):
        """Manual, immediate upload attempt. On failure the CSV is
        queued (see _queue_wigle_upload below) so a later automatic
        retry -- once you're back online -- doesn't require you to
        remember to click this again. This is the resilience Wardriver
        5's own upload queue had that this plugin was missing at first:
        a one-shot upload with no memory of having failed."""
        if not WIGLE_API_NAME or not WIGLE_API_TOKEN or WIGLE_API_TOKEN == "CHANGE_ME":
            self.msg("No WiGLE credentials -- set WDG_WIGLE_NAME / WDG_WIGLE_TOKEN in secrets.conf")
            return
        if not HAVE_REQUESTS:
            self.msg("'requests' library not installed -- can't upload")
            return
        # v-next: session_path is a @property on LootManager, not a
        # method -- calling it as session_path() raised
        # "TypeError: 'str' object is not callable" every time, and
        # (see app.py's now-fixed missing `log` definition) that error
        # had nowhere safe to land, crashing the whole game instead of
        # just failing the upload with a terminal message.
        csv_path = Path(self.app.loot.session_path) / "wardriving.csv"
        if not csv_path.is_file():
            self.msg("No wardriving.csv for this session yet")
            return
        # [merge-v3] The attempt itself runs on the WiGLE worker; this menu
        # action returns at once and the result arrives as a HUD message.
        if not self._start_wigle_job(self._manual_wigle_worker, csv_path, manual_path=csv_path):
            self.msg("A WiGLE upload is already running -- try again when it finishes")
            return
        self.msg("Uploading to WiGLE.net in the background...")

    def _manual_wigle_worker(self, csv_path):
        """[merge-v3] WiGLE worker thread: the body upload_wigle() used to run
        on the game thread. Same outcomes and wording; HUD lines go through
        _wigle_post()."""
        ok, detail = self._do_wigle_upload(csv_path)
        if ok is None:  # [merge-v3] nothing with a position in the file
            self._dequeue_wigle_upload(csv_path)
            self._wigle_post("WiGLE: no positioned rows to upload yet", 7,
                             f"[KISMET] WiGLE upload skipped ({detail})")
        elif ok:
            # A copy queued by an earlier failed attempt has now been sent;
            # leaving it queued would post the same file again a minute later.
            self._dequeue_wigle_upload(csv_path)
            self._wigle_post("Uploaded to WiGLE.net", 11, "[KISMET] WiGLE upload OK")
        else:
            retry, note = self._wigle_failure_policy(csv_path)   # [merge-v3]
            if not retry:
                self._dequeue_wigle_upload(csv_path)
                self._wigle_post("WiGLE refused the file -- see terminal", 8,
                                 f"[KISMET] WiGLE upload failed ({detail}). {note}")
                return
            self._queue_wigle_upload(csv_path)
            self._wigle_post("WiGLE upload failed -- queued, will retry automatically", 8,
                             f"[KISMET] WiGLE upload failed ({detail}) -- queued for retry"
                             + (f". {note}" if note else ""))

    # ------------------------------------------------------------------
    # [merge-v3] WiGLE worker thread
    # ------------------------------------------------------------------

    def _wigle_post(self, hud=None, colour=7, term=None):
        """Any thread: hand a result to the game thread (deque.append is atomic)."""
        self._wigle_events.append((hud, colour, term))

    def _drain_wigle_events(self):
        """Game thread, every frame: show what the WiGLE worker reported, and
        while a manual upload is still running say so every 30 s (the HUD
        line it started with fades after a few seconds)."""
        while self._wigle_events:
            hud, colour, term = self._wigle_events.popleft()
            if term:
                self.term(term)
            if hud:
                self.msg(hud, colour)
        if self._wigle_busy and self._wigle_manual_path is not None:
            now = time.time()
            if now - self._wigle_noted >= 30.0:
                self._wigle_noted = now
                self.msg(f"WiGLE upload still running ({int(now - self._wigle_started)} s)...")

    def _start_wigle_job(self, target, *args, manual_path=None) -> bool:
        """Game thread: run target(*args) on the WiGLE worker. False (and
        nothing started) while another upload job is still running."""
        with self._wigle_lock:
            if self._wigle_busy:
                return False
            self._wigle_busy = True
        self._wigle_manual_path = manual_path
        self._wigle_started = self._wigle_noted = time.time()
        thread = threading.Thread(target=self._wigle_job, args=(target, args),
                                  name="kismet-wigle", daemon=True)
        self._wigle_thread = thread
        try:
            thread.start()
        except RuntimeError:  # no thread could be started: stay usable
            self._wigle_manual_path = None
            with self._wigle_lock:
                self._wigle_busy = False
            return False
        return True

    def _wigle_job(self, target, args):
        try:
            target(*args)
        except Exception as e:  # never let the worker die silently, or stay "busy"
            self._wigle_post("WiGLE upload error (see terminal)", 8,
                             f"[KISMET] WiGLE upload worker error: {e}")
        finally:
            self._wigle_manual_path = None
            with self._wigle_lock:
                self._wigle_busy = False

    def _do_wigle_upload(self, csv_path):
        """One attempt. Returns (ok, detail) -- detail is a human
        reason on failure, never raises. [merge-v3] Blocks for up to the
        120 s timeout below: call it from the WiGLE worker only, never from
        the game thread.

        [merge-v3] What is sent is positioned_wigle_export(csv_path), not the
        raw file: rows still without a position (0,0) are left out. ok is
        None (not False) when no positioned row exists, so the caller neither
        reports success nor queues a file that has nothing to send."""
        try:
            data, kept, dropped = positioned_wigle_export(csv_path)
        except (OSError, UnicodeError) as e:
            return False, f"cannot read {csv_path.name}: {e}"
        if not kept:
            return None, f"{csv_path.name}: no rows with a GPS position ({dropped} left out)"
        self.term(f"[KISMET] Uploading {csv_path.name} to WiGLE.net "
                  f"({kept} row(s); {dropped} without a position left out)...")
        self._wigle_last = None   # [merge-v3] (status, Retry-After) of this attempt
        try:
            r = requests.post(
                WIGLE_API_URL,
                auth=(WIGLE_API_NAME, WIGLE_API_TOKEN),
                files={"file": (csv_path.name, data)},
                data={"donate": "false"},
                timeout=120,
            )
            headers = getattr(r, "headers", None) or {}
            self._wigle_last = (r.status_code, headers.get("Retry-After"))
            if r.status_code == 200:
                # [merge-v3] A 200 that is not JSON is a web page (login,
                # captive portal, redirect target): "not yet", said plainly
                # instead of a JSON parser's error text.
                try:
                    body = r.json()
                except ValueError:
                    return False, ("WiGLE answered with a web page, not JSON "
                                   "(login page or redirect?) -- not uploaded yet")
                if isinstance(body, dict) and body.get("success"):
                    return True, None
            return False, f"HTTP {r.status_code} {r.text[:200]}"
        except Exception as e:
            return False, str(e)

    def _wigle_failure_policy(self, csv_path):
        """[merge-v3] After a failed attempt: when may WiGLE be asked again,
        and is this file worth retrying at all? Returns (retry, note):
        retry False = do not keep the file in the retry queue.

          429 / 503   wait as long as WiGLE's Retry-After says (30 s .. 1 h;
                      15 min / 2 min when it says nothing), instead of posting
                      the same file again every minute while rate-limited
          401 / 403   the credentials are refused: wait 30 min, say so
          413         the file is too large for WiGLE and always will be: not
                      retried, the CSV stays on disk
        Anything else (timeouts, 5xx, no network, a web page) keeps the old
        behaviour: queued, retried on the normal one-minute schedule."""
        code, retry_after = getattr(self, "_wigle_last", None) or (None, None)
        now = time.time()
        if code in (429, 503):
            try:
                wait = int(str(retry_after).strip())
            except (TypeError, ValueError):
                wait = 900 if code == 429 else 120
            wait = max(30, min(wait, 3600))
            self._wigle_not_before = now + wait
            return True, f"WiGLE asked us to wait: next try in {max(1, round(wait / 60))} min"
        if code in (401, 403):
            self._wigle_not_before = now + 1800
            return True, ("WiGLE refused the login: check WDG_WIGLE_NAME / WDG_WIGLE_TOKEN "
                          "in secrets.conf (next try in 30 min)")
        if code == 413:
            return False, ("WiGLE says the file is too large (HTTP 413): not retried; "
                           "the CSV stays on disk")
        return True, ""

    # ------------------------------------------------------------------
    # WiGLE upload retry queue -- persisted to disk so a failed upload
    # survives a game restart, retried automatically once internet is
    # back rather than requiring you to remember and click Upload again.
    # ------------------------------------------------------------------

    def _queue_file(self):
        return Path(self.engine.log_dir) / "wigle_upload_queue.json"

    # [merge-v3] The queue file is now read and written from two threads (the
    # game thread: queueing, upstream's delete hold; the WiGLE worker: retries), so
    # every access takes _wigle_lock, entries are added/removed one at a time
    # instead of saving back a list that was read before a 120 s upload, and
    # the file is replaced atomically so a reader never sees half of it (an
    # unreadable queue reads as empty, which would release the delete hold).

    def _load_wigle_queue(self):
        with self._wigle_lock:
            try:
                value = json.loads(self._queue_file().read_text())
            except (OSError, ValueError):
                return []
            return value if isinstance(value, list) else []

    def _save_wigle_queue(self, q):
        with self._wigle_lock:
            target = self._queue_file()
            temporary = target.with_name(target.name + ".tmp")
            try:
                temporary.write_text(json.dumps(q))
                os.replace(temporary, target)
            except OSError:
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def queue_wigle_upload(self, csv_path):
        """[bristle] Public name for other plugins: put one wardriving.csv into the WiGLE upload
        queue (the same queue a Kismet drive's file goes into). Safe from any thread."""
        self._queue_wigle_upload(csv_path)

    def _queue_wigle_upload(self, csv_path):
        with self._wigle_lock:
            q = self._load_wigle_queue()
            path_str = str(csv_path)
            if path_str not in q:
                q.append(path_str)
                self._save_wigle_queue(q)

    def _dequeue_wigle_upload(self, csv_path):
        """[merge-v3] Remove one file from the queue (any spelling of its path)."""
        with self._wigle_lock:
            q = self._load_wigle_queue()
            key = self._path_key(csv_path)
            kept = [item for item in q if self._path_key(item) != key]
            if len(kept) != len(q):
                self._save_wigle_queue(kept)

    def _retry_wigle_queue(self):
        """Game thread, once a minute: start a retry pass if there is anything
        to retry. [merge-v3] Only the cheap checks happen here; the ping and
        the uploads run on the WiGLE worker (_retry_wigle_worker)."""
        if self._wigle_busy:
            return  # a manual upload or the previous pass is still running
        if time.time() < getattr(self, "_wigle_not_before", 0.0):
            return  # [merge-v3] WiGLE said when to come back (_wigle_failure_policy)
        q = self._load_wigle_queue()
        if not q:
            return
        if not WIGLE_API_NAME or not WIGLE_API_TOKEN or WIGLE_API_TOKEN == "CHANGE_ME":
            return  # nothing to retry with -- stays queued until credentials are set
        if not HAVE_REQUESTS:
            return
        self._start_wigle_job(self._retry_wigle_worker)

    def _retry_wigle_worker(self):
        """[merge-v3] WiGLE worker thread: one pass over the queue, the loop
        _retry_wigle_queue() used to run on the game thread. An entry leaves
        the queue only after its own outcome is known, so quitting mid-pass
        loses nothing."""
        if not hw.internet_available():
            return
        for path_str in self._load_wigle_queue():
            csv_path = Path(path_str)
            if not csv_path.is_file():
                # [merge-v3] Gone (moved, or deleted outside the game). upstream's
                # own cleanup cannot do this while the entry is queued -- see
                # _register_upload_hold() -- so say so instead of dropping the
                # entry silently, and keep the rest of the queue intact.
                self._dequeue_wigle_upload(csv_path)
                self._wigle_post(term=f"[KISMET] Queued WiGLE file is gone, dropped from "
                                      f"the queue: {csv_path.parent.name}/{csv_path.name}")
                continue
            ok, detail = self._do_wigle_upload(csv_path)
            if ok is None:
                # [merge-v3] no positioned rows: nothing to send, ever, unless
                # the file changes -- do not retry it every minute.
                self._dequeue_wigle_upload(csv_path)
                self._wigle_post(term=f"[KISMET] Queued WiGLE upload dropped ({detail})")
            elif ok:
                self._dequeue_wigle_upload(csv_path)
                self._wigle_post(f"WiGLE: queued upload sent ({csv_path.parent.name})", 11,
                                 f"[KISMET] Queued WiGLE upload succeeded: {csv_path.name}")
            else:
                retry, note = self._wigle_failure_policy(csv_path)   # [merge-v3]
                if not retry:
                    self._dequeue_wigle_upload(csv_path)
                self._wigle_post(term=f"[KISMET] Queued WiGLE retry still failing "
                                      f"({detail}): {csv_path.name}"
                                      + (f". {note}" if note else ""))
                if time.time() < getattr(self, "_wigle_not_before", 0.0):
                    return   # told to wait: do not post the rest of the queue either

    # ------------------------------------------------------------------
    # Adapter picker overlay -- input + draw
    # ------------------------------------------------------------------

    def _picker_on_update(self):
        import pyxel

        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._overlay_active = False
            if self.app:
                self.app._esc_consumed_frame = pyxel.frame_count
            return

        # v2.2 added a short automatic rescan timer (every
        # ADAPTER_RESCAN_INTERVAL) alongside on-demand 'R' so a
        # replugged adapter would show up without having to back out
        # and reopen the picker. On a rig with several USB radios
        # sharing a bus, that 2s timer was fast enough to catch real,
        # transient USB enumeration churn as adapters briefly drop off
        # and come back -- which looked like the picker itself randomly
        # bouncing devices in and out, right when you're trying to lock
        # in a specific selection before it scrolls away. 'R' already
        # does the exact same rescan on demand, so the automatic timer
        # added no capability, just a fixed 2s window for that churn to
        # show up uninvited. Removed -- the adapter list now only ever
        # changes when you press 'R' yourself.
        if pyxel.btnp(pyxel.KEY_R):
            self._last_adapter_scan = time.time()
            self._rescan_adapters()
        # [merge-v3] one-off rescans after the internal USB rail was switched
        # on for this picker (not the old free-running timer)
        due = getattr(self, "_rescan_due", None)
        if due and time.time() >= due[0]:
            due.pop(0)
            self._last_adapter_scan = time.time()
            self._rescan_adapters()
        if pyxel.btnp(pyxel.KEY_S):
            self._split_mode = "off" if self._split_mode == "band" else "band"

        rows = self._picker_rows()
        if pyxel.btnp(pyxel.KEY_UP) and self._menu_sel > 0:
            self._menu_sel -= 1
        if pyxel.btnp(pyxel.KEY_DOWN):
            self._menu_sel = min(self._menu_sel + 1, len(rows) - 1)

        kind, row_id, _ = rows[self._menu_sel]

        toggled = pyxel.btnp(pyxel.KEY_SPACE) or pyxel.btnp(pyxel.KEY_RETURN)
        if kind == "wifi" and toggled:
            if row_id in self._wifi_selected:
                self._wifi_selected.discard(row_id)
            else:
                self._wifi_selected.add(row_id)
        elif kind == "ble" and toggled:
            if row_id in self._ble_selected:
                self._ble_selected.discard(row_id)
            else:
                self._ble_selected.add(row_id)
        elif kind == "start" and pyxel.btnp(pyxel.KEY_RETURN):
            self._launch_from_picker()
        elif kind == "cancel" and pyxel.btnp(pyxel.KEY_RETURN):
            self._overlay_active = False

        # 'M' toggles the monitor-mode preference for the currently
        # highlighted WiFi row only -- separate key from
        # select/deselect (SPACE/RETURN) so the two don't collide.
        if kind == "wifi" and pyxel.btnp(pyxel.KEY_M):
            self._wifi_monitor[row_id] = not self._wifi_monitor.get(row_id, False)
        # [bristle] LEFT/RIGHT step the highlighted Wi-Fi card through "hop" and each channel it
        # can be locked to (held down, it repeats); L lets it hop again.
        if kind == "wifi":
            if pyxel.btnp(pyxel.KEY_RIGHT, 14, 3):
                self._step_lock(row_id, 1)
            elif pyxel.btnp(pyxel.KEY_LEFT, 14, 3):
                self._step_lock(row_id, -1)
            elif pyxel.btnp(pyxel.KEY_L):
                self._wifi_lock.pop(row_id, None)

    def draw(self, x: int, y: int, w: int, h: int) -> None:
        if not self._overlay_active:
            return
        import pyxel

        pyxel.rect(x, y, w, h, 0)
        pyxel.rectb(x + 4, y + 4, w - 8, h - 8, 5)
        pyxel.text(x + 12, y + 12, "KISMET WARDRIVE -- SELECT ADAPTERS", 10)
        pyxel.line(x + 12, y + 22, x + w - 12, y + 22, 5)
        pyxel.text(x + 12, y + h - 20,
                    "UP/DOWN move  SPACE/ENTER toggle  M monitor mode  LEFT/RIGHT lock channel  L hop  "
                    "R rescan  S channel split  ESC cancel", 13)
        # [merge-v3] what two Wi-Fi adapters will do with the channels
        # [bristle] 1.2: any number of cards; the lines under the list say what each one gets
        picked = sum(1 for wifi in self._wifi_all if wifi["id"] in self._wifi_selected)
        if picked >= 2:
            split = ("Channel split ON: each card gets its own share of the channels (listed above)  [S] off"
                     if self._split_mode == "band" else
                     "Channel split OFF: every adapter hops every channel  [S] on")
            pyxel.text(x + 12, y + h - 32, split, 11 if self._split_mode == "band" else 13)
        if getattr(self, "_rescan_due", None):
            pyxel.text(x + 12, y + h - 44, "Internal USB rail just switched on: looking again "
                                           "for adapters in a few seconds...", 10)

        rows = self._picker_rows()
        row_y = y + 32
        locks = getattr(self, "_wifi_lock", None) or {}     # [bristle]
        for i, (kind, row_id, obj) in enumerate(rows):
            sel = (i == self._menu_sel)
            color = 10 if sel else 7
            prefix = "> " if sel else "  "
            if kind == "wifi":
                mark = "X" if row_id in self._wifi_selected else " "
                mon = " (monitor)" if self._wifi_monitor.get(row_id) else ""
                note = f" -- {obj['note']}" if obj.get("note") else ""
                label = f"{prefix}[{mark}] WIFI {row_id}  {obj.get('guess', '')}{mon}{note}"
            elif kind == "ble":
                mark = "X" if row_id in self._ble_selected else " "
                label = f"{prefix}[{mark}] BLE  {row_id}  {obj.get('guess', '')}"
            elif kind == "start":
                label = f"{prefix}>>> START WARDRIVING <<<"
                color = 11 if sel else color
            else:
                label = f"{prefix}CANCEL"
                color = 8 if sel else color
            pyxel.text(x + 16, row_y, label[:78], color)
            if kind == "wifi" and locks.get(row_id):     # [bristle]
                pyxel.text(x + 420, row_y, f"LOCKED: channel {locks[row_id]}"[:40], 9)
            row_y += 10
        # [bristle] what each ticked Wi-Fi card will do: its share of the split, or its lock
        if picked >= 2 or any(locks.get(wifi["id"]) for wifi in self._wifi_all
                              if wifi["id"] in self._wifi_selected):
            row_y += 6
            if row_y < y + h - 66:
                pyxel.text(x + 16, row_y, "CHANNEL PLAN", 10)
                row_y += 10
            for card, text in self._plan_rows():
                if row_y >= y + h - 56:
                    break
                pyxel.text(x + 16, row_y, f"{card}: {text}"[:120], 11 if "cannot tune" not in text else 8)
                row_y += 10

    # ------------------------------------------------------------------
    # Persistent HUD status (WiFi / BLE / GPS)
    # ------------------------------------------------------------------

    # NOTE on an earlier version of this method: it used to draw its own
    # "WIFI BLE GPS" text directly at a guessed (4, 344) corner. That
    # position overlaps the base game's own bottom HUD bar, which
    # already draws "BLE:<n>" starting at that exact spot (see
    # `_draw_hud_bottom` in app.py) -- so it was silently drawing on top
    # of an existing counter. There's no pyxel/display available in the
    # sandbox this was built in to catch that visually, only by reading
    # app.py's own layout code closely enough to notice the collision.
    # Fixed by not drawing free-floating text at all: WiFi/BLE liveness
    # now piggybacks on the bottom bar's own "tools" tag list (see the
    # small app.py patch), GPS liveness is already shown by the base
    # game's own GPS readout, and CPU/Temp are written onto plain
    # attributes here for app.py's existing top-right HUD row to render
    # next to Battery -- using ITS coordinate math (chained off of the
    # already-working Battery/Zoom position), not a guess of my own.
    DIAG_INTERVAL = 3.0  # seconds between system-diagnostics reads

    def _refresh_diag(self):
        """Keeps app._cpu_pct / app._temp_c / app._storage_free_gb fresh;
        the actual per-frame draw call lives in app.py's own top-right
        HUD row, next to Battery."""
        if not self.engine or not self.engine.running:
            return
        now = time.time()
        if now - getattr(self, "_last_diag", 0.0) < self.DIAG_INTERVAL:
            return
        self._last_diag = now
        # [v2] app.py now owns a SystemMonitor (watchdogs/sys_monitor.py)
        # that feeds the HUD all the time, not just while Kismet runs. Read
        # its snapshot for this segment's peaks instead of sampling /proc a
        # second time; fall back to the old self-sampling on an app without it.
        snap = getattr(getattr(self.app, "_sysmon", None), "snapshot", None)
        if snap:
            t = snap.get("temp_c")
            if t is not None and (self._seg_peak_temp_get() is None or t > self._seg_peak_temp):
                self._seg_peak_temp = t
            if snap.get("throttled_now"):
                self._seg_throttled = True
            if snap.get("undervolt_now"):
                self._seg_undervolt = True
        else:
            try:
                diag = hw.read_system_diagnostics()
            except Exception:
                diag = {}
            self.app._cpu_pct = diag.get("cpu_pct")
            self.app._temp_c = diag.get("temp_c")
            t = diag.get("temp_c")
            if t is not None and (self._seg_peak_temp_get() is None or t > self._seg_peak_temp):
                self._seg_peak_temp = t
        rss = self._kismet_rss_mb()
        if rss is not None and (getattr(self, "_seg_peak_rss", None) is None or rss > self._seg_peak_rss):
            self._seg_peak_rss = rss
        # Storage free space on wherever loot/logs actually live -- NOT
        # necessarily the same drive the game code is installed on, so
        # this reflects a USB drive once `_app_dir` points at one (see
        # __main__.py's `loot_path` argument / run.sh).
        try:
            import shutil
            usage = shutil.disk_usage(self.app._app_dir)
            self.app._storage_free_gb = round(usage.free / (1024 ** 3), 1)
        except OSError:
            self.app._storage_free_gb = None
        # Low-storage warning -- printed to the terminal log rather than
        # fighting for HUD space, since it only matters occasionally.
        free_gb = self.app._storage_free_gb
        if free_gb is not None and free_gb < 1.0 and not getattr(self, "_low_storage_warned", False):
            self._low_storage_warned = True
            self.term(f"[KISMET] Low storage: {free_gb} GB free on {self.app._app_dir}")
        elif free_gb is not None and free_gb >= 1.0:
            self._low_storage_warned = False

    def _seg_peak_temp_get(self):
        return getattr(self, "_seg_peak_temp", None)

    def kismet_active(self) -> bool:
        """Used by app.py's bottom-bar tools tag list -- True while
        Kismet is actually capturing on at least one source."""
        return bool(self.engine and self.engine.running
                    and (self._wifi_selected_active() or self._ble_selected_active()))

    def kismet_hud_label(self) -> str:
        """v1.6: the bottom-bar tag text for Kismet, with its OWN
        WiFi/BLE counts baked in -- counted the instant poll_networks()
        classifies a device (see seen_wifi_macs/seen_bt_macs), not
        routed through AppState.networks / app.wifi_networks /
        app.ble_devices or the known-device/XP path on_update() also
        updates. Two independent counters, side by side: the stock
        WiFi:/BLE: HUD numbers (fed by app.wifi_networks/ble_devices,
        same as an ESP32 scan) and this plugin's own "KISMET W:n B:n",
        fed straight from what Kismet itself reported. If they ever
        disagree, that split is itself the diagnostic -- it means the
        stock-counter feed broke somewhere, while Kismet's own count
        here still reflects reality."""
        w = len(self.engine.seen_wifi_macs) if self.engine else 0
        b = len(self.engine.seen_bt_macs) if self.engine else 0
        # v1.7: fold the GPS-gate state in here too, so "is this data
        # actually going to be any good on WiGLE" is answered by the
        # same glance as the counts themselves, not something you have
        # to go dig for separately.
        if bool(getattr(self.app, "gps_fix", False)):
            return f"KISMET W:{w} B:{b} GPS:OK"
        held = len(self._held_networks) + len(self._held_bt)
        return f"KISMET W:{w} B:{b} GPS:NOFIX({held}held)"

    def _wifi_selected_active(self):
        return getattr(self.engine, "running", False) and bool(self._wifi_selected)

    def _ble_selected_active(self):
        return getattr(self.engine, "running", False) and bool(self._ble_selected)

    # ------------------------------------------------------------------
    # Background REST poll thread (see __init__ for why this exists)
    # ------------------------------------------------------------------

    def _start_poll_thread(self):
        """[v2] Each poll thread gets its OWN stop event and a generation
        number. The old version refused to start if the previous thread
        was still alive (e.g. stuck in an 8s HTTP call at stop), which
        would silently leave a rotated/restarted session with no poller.
        Now a straggler just finds its own event set, and anything it
        publishes late is dropped because its generation is stale."""
        self._poll_stop_flag.set()  # retire any previous thread
        self._poll_gen += 1
        gen = self._poll_gen
        stop_evt = threading.Event()
        self._poll_stop_flag = stop_evt
        with self._poll_result_lock:
            self._latest_poll_result = ([], [], None)
            self._poll_seq = 0
            self._last_consumed_seq = 0
        t = threading.Thread(target=self._poll_worker, args=(gen, stop_evt),
                             name="kismet-poll", daemon=True)
        t.start()
        self._poll_thread = t

    def _take_unconsumed_poll(self):
        """[merge-v3] A batch the poll thread published that nobody has used
        yet, marked as used: (networks, bt_devices), or None. Any thread."""
        with self._poll_result_lock:
            pending = self._poll_seq != self._last_consumed_seq
            networks, bt_devices, err = self._latest_poll_result
            self._last_consumed_seq = self._poll_seq
        if pending and not err and (networks or bt_devices):
            return list(networks), list(bt_devices)
        return None

    def _stop_poll_thread(self):
        self._poll_stop_flag.set()
        t = self._poll_thread
        if t is not None and t.is_alive():
            # [v2] short join only: the thread is daemon, has its own
            # stop event, and a late result is discarded by generation.
            # [merge-v3] 0.05 s, was 0.5 s: with a poll in flight against a
            # Kismet that hangs or has just died, this join froze the game
            # for the full half second (found by tools/sim, Kismet crash).
            t.join(timeout=0.05)
        self._poll_thread = None

    def _poll_worker(self, gen, stop_evt):
        """Runs on its own thread for as long as Kismet is running.
        Does exactly what on_update() used to do inline: one blocking
        poll_networks() call every POLL_INTERVAL, published for the main
        thread to pick up on its own schedule -- see on_update()."""
        while not stop_evt.is_set():
            if not self.engine or not self.engine.running:
                return
            whitelist = getattr(self.app, "_whitelist", None)
            is_blocked = whitelist.is_blocked if whitelist is not None else None
            # [merge-v3] poll and publish as ONE step under _poll_io_lock.
            # Before, a poll that finished after Stop was pressed was dropped
            # here ("if stop_evt.is_set(): return") although poll_networks()
            # had already marked its devices as seen, so the stop worker's
            # final poll skipped them and they never reached wardriving.csv.
            # Now: no poll starts once stop is set (the stop worker does the
            # last one), and a poll that did start is always published; the
            # stop worker collects it (see _stop_worker).
            with self._poll_io_lock:
                if stop_evt.is_set():
                    return
                try:
                    networks, bt_devices, err = self.engine.poll_networks(is_blocked=is_blocked)
                except Exception as e:  # [v2] never let the poller die silently
                    networks, bt_devices, err = [], [], f"devices poll: {e}"
                with self._poll_result_lock:
                    if gen != self._poll_gen:
                        return
                    # [v2] with delta polling each result holds only what's
                    # NEW, so a result the main thread hasn't consumed yet
                    # must be merged, never overwritten (the old full poll
                    # could drop one; the next poll repeated everything).
                    pending = self._poll_seq > self._last_consumed_seq
                    prev_n, prev_b, prev_err = self._latest_poll_result
                    if pending and not prev_err and err:
                        pass  # keep the unconsumed data; next failure reports
                    else:
                        if pending and not prev_err and not err:
                            networks = list(prev_n) + list(networks)
                            bt_devices = list(prev_b) + list(bt_devices)
                        self._latest_poll_result = (networks, bt_devices, err)
                        self._poll_seq += 1
            if stop_evt.is_set():
                return
            # [merge-v3] on this thread, never the game's: adapters that
            # stopped capturing, the two-adapter channel split, and a fresh
            # look for the login after Kismet refused it.
            try:
                self._source_check(err)
            except Exception:
                pass  # a watch that fails must never stop the polling itself
            _bristle_tap.poll(self.engine, KISMET_URL)   # [bristle] Kismet's alerts and source list, on this thread
            # [v2.6] wake immediately on stop instead of sleeping in 0.1s
            # slices (the join in _stop_poll_thread no longer waits ~100ms).
            if stop_evt.wait(self.POLL_INTERVAL):
                return

    def _source_check(self, poll_error) -> None:
        """[merge-v3] Poll-thread half of the Kismet health watch."""
        now = time.time()
        if poll_error and "HTTP 401" in poll_error and now >= getattr(self, "_auth_retry_at", 0.0):
            # the login may have been created since launch (first run of
            # Kismet's web UI): look for it again, at most twice a minute
            self._auth_retry_at = now + 30.0
            self.engine._discover_credentials()
        if poll_error or now - getattr(self, "_source_checked", 0.0) < DATASOURCE_CHECK_INTERVAL:
            return
        self._source_checked = now
        sources, error = self.engine.datasources()
        if error:
            return
        for level, text in kx.source_changes(self._source_prev, sources):
            self._kismet_events.append((level, "[KISMET] " + text))
        self._source_prev = {(s["uuid"] or s["name"]): s["running"] for s in sources}
        wifi = [s for s in sources if s["kind"] == "wifi"]
        if wifi and not any(s["running"] for s in wifi) and not any(
                s["running"] for s in sources if s["kind"] == "bt"):
            if not getattr(self, "_all_down_said", False):
                self._all_down_said = True
                self._kismet_events.append(("alarm", "[KISMET] No adapter is capturing right now"))
        else:
            self._all_down_said = False
        self._apply_split(sources)

    def _apply_split(self, sources) -> None:
        """[merge-v3] Make (or undo) the channel split for the Wi-Fi sources
        that are capturing right now. Poll thread."""
        running = frozenset(s["uuid"] for s in sources
                            if s["kind"] == "wifi" and s["running"] and s["uuid"])
        if running == self._split_applied:
            return
        by_uuid = {s["uuid"]: s for s in sources}
        # [bristle] cards locked to one channel first: they are not part of the split
        locks = self._settle_locks(sources, by_uuid, running)
        lock_note = "".join("; %s locked on channel %s" % (by_uuid[uuid]["name"], channel)
                            for uuid, channel in sorted(locks.items()))
        if self._split_mode != "band":
            self._split_applied = running
            self._split_report = "off (WDG_KISMET_SPLIT=off or switched off in the picker)" + lock_note
            return
        # a source keeps its pre-split hop list as "original", once
        for uuid in running:
            source = by_uuid[uuid]
            if uuid in locks:
                # [bristle] a locked card's own list is everything it can tune (what it hops
                # again if the lock is ever taken off), never the one channel it sits on
                self._split_original.setdefault(uuid, (list(source["channels"] or source["hop_channels"]),
                                                       source["hop_rate"]))
                continue
            self._split_original.setdefault(uuid, (list(source["hop_channels"] or source["channels"]),
                                                   source["hop_rate"]))
        originals = [dict(by_uuid[uuid], hop_channels=self._split_original[uuid][0])
                     for uuid in sorted(running, key=lambda u: by_uuid[u]["name"])]
        plan, why = kx.plan_split(originals, locks)
        if not plan:
            # one adapter left (the other was unplugged), or no split possible:
            # whoever is still capturing goes back to all of its channels
            for uuid in running:
                if uuid in locks:
                    continue             # [bristle] a locked card stays where it is
                channels, rate = self._split_original[uuid]
                if channels and list(by_uuid[uuid]["hop_channels"]) != channels:
                    self.engine.set_hop_channels(uuid, channels, rate)
            self._split_applied = running
            self._split_report = "no split: " + why
            if len(self._split_original) > 1:
                self._kismet_events.append(("info", "[KISMET] Channel split off: " + why))
            return
        # [bristle] a card that is not in this plan (it had a share before, and now only the
        # locked channel is taken out of the others' lists) goes back to all of its channels
        for uuid in running:
            if uuid not in plan and uuid not in locks:
                channels, rate = self._split_original[uuid]
                if channels and list(by_uuid[uuid]["hop_channels"]) != channels:
                    self.engine.set_hop_channels(uuid, channels, rate)
        for uuid, channels in plan.items():
            ok, reason = self.engine.set_hop_channels(uuid, channels, self._split_original[uuid][1])
            if not ok:
                self._split_applied = running
                self._split_report = "not applied: " + str(reason)
                self._kismet_events.append(("warn", "[KISMET] Channel split not applied (%s) -- both "
                                                    "adapters keep hopping every channel" % reason))
                return
        self._split_applied = running
        self._split_report = "band split: " + why
        self._kismet_events.append(("ok", "[KISMET] Channels split between the adapters -- " + why))

    def _settle_locks(self, sources, by_uuid, running) -> dict:
        """[bristle] Poll thread: put every card that was launched with a channel lock on that
        channel, going by what Kismet itself says the card can tune. Returns {uuid: channel} for
        the locks that are in force. A card that cannot be held there (Kismet does not list the
        channel for it, or refuses the command) is sent back to hopping all of its channels, and
        that is said once in the terminal and on the HUD."""
        wanted = getattr(self, "_locks_wanted", None) or {}
        engine_locked = {}
        if wanted:
            asked = {}
            for source in sources:
                if source["kind"] == "wifi" and source["uuid"] in running:
                    channel = wanted.get(source["interface"]) or wanted.get(source["name"])
                    if channel:
                        asked[source["uuid"]] = channel
            ok, refused, unknown = kx.resolve_locks(sources, asked)
            failed = dict(refused)
            ok.update(unknown)           # no channel list from Kismet: ask, and go by its answer
            for uuid, name in ok.items():
                source = by_uuid[uuid]
                if source.get("hopping") is False and kx.same_channel(source.get("channel") or "", name):
                    engine_locked[uuid] = name       # the launch already put it there
                    continue
                good, reason = self.engine.set_channel(uuid, name)
                if good:
                    engine_locked[uuid] = name
                else:
                    failed[uuid] = "%s (%s)" % (asked[uuid], reason)
            said = getattr(self, "_locks_said", None)
            if said is None:
                said = self._locks_said = set()
            for uuid, channel in failed.items():
                source = by_uuid[uuid]
                channels = list(source["channels"] or source["hop_channels"])
                if channels:
                    self.engine.set_hop_channels(uuid, channels, source["hop_rate"])
                if (uuid, channel) not in said:
                    said.add((uuid, channel))
                    self._kismet_events.append(("warn", "[KISMET] %s cannot be locked to channel %s -- "
                                                        "it hops all of its channels instead"
                                                % (source["name"], channel)))
        try:
            self.engine.locked = engine_locked       # read by datasources(): the node planner
        except Exception:                            # then counts these channels as covered
            pass
        return engine_locked

    def _drain_kismet_events(self) -> None:
        """[merge-v3] Game thread: show what the poll thread found."""
        events = getattr(self, "_kismet_events", None)
        while events:
            try:
                level, text = events.popleft()
            except IndexError:
                return
            self.term(text)
            if level in ("warn", "alarm", "ok"):
                self.msg(text[:120], {"warn": 10, "alarm": 8, "ok": 11}[level])

    def _watch_kismet_process(self, now) -> bool:
        """[merge-v3] Kismet itself died while capturing: restart it on the
        same adapters (the drive, its held records and stats carry on, as in
        a rotation), at most KISMET_MAX_RESTARTS times in
        KISMET_RESTART_WINDOW seconds. True when a stop was started."""
        proc = getattr(self.engine, "proc", None)
        if proc is None:
            return False
        try:
            code = proc.poll()
        except Exception:
            return False
        if not isinstance(code, int) or isinstance(code, bool):
            return False          # still running (None), or not a real process
        self._crash_times = [t for t in getattr(self, "_crash_times", []) if now - t < KISMET_RESTART_WINDOW]
        self._crash_times.append(now)
        attempt = len(self._crash_times)
        if attempt <= KISMET_MAX_RESTARTS:
            self.term(f"[KISMET] Kismet exited by itself (code {code}) -- restarting it on the same "
                      f"adapters ({attempt}/{KISMET_MAX_RESTARTS}); captured data is kept")
            self.msg("Kismet crashed -- restarting it", 10)
            self._relaunch_after_stop = True
        else:
            self.term(f"[KISMET] Kismet exited by itself {attempt} times in "
                      f"{int(KISMET_RESTART_WINDOW // 60)} minutes -- not restarting again. "
                      f"Everything captured so far is saved; see kismet_stdout.log, then start "
                      f"it from PLUGINS when ready")
            self.msg("Kismet keeps crashing -- stopped. See terminal", 8)
            self._relaunch_after_stop = False
        self.stop_wardrive()
        return True

    def _note_poll_error(self, err, now) -> None:
        """[merge-v3] A failed poll, said once and then every POLL_ERROR_REPEAT
        seconds with a count -- not on every 2 s poll as before."""
        if "falling back to full polls" in err:
            self.term(f"[KISMET] {err}")          # a one-time notice, not a failure
            return
        self._poll_fail_count = getattr(self, "_poll_fail_count", 0) + 1
        if err != getattr(self, "_poll_fail_text", "") or now - self._poll_fail_logged >= POLL_ERROR_REPEAT:
            count = self._poll_fail_count
            self.term(f"[KISMET] {err}" + (f" ({count} failed polls in a row)" if count > 1 else ""))
            self._poll_fail_text, self._poll_fail_logged = err, now
        if "HTTP 401" in err and not getattr(self, "_auth_hinted", False):
            self._auth_hinted = True
            self.term("[KISMET] Kismet refused the login (HTTP 401). Open http://localhost:2501 once "
                      "and set its login, or put a key in WDG_KISMET_API_KEY (secrets.conf). "
                      "The bridge keeps trying; nothing captured so far is lost")
            self.msg("Kismet login refused (401) -- see terminal", 10)

    def _note_poll_ok(self) -> None:
        count = getattr(self, "_poll_fail_count", 0)
        if count:
            self.term(f"[KISMET] Polling works again after {count} failed poll(s)")
            self.msg("Kismet polling recovered", 11)
        self._poll_fail_count = 0
        self._poll_fail_text = ""
        self._auth_hinted = False

    # ------------------------------------------------------------------
    # Per-frame update
    # ------------------------------------------------------------------

    WIGLE_RETRY_INTERVAL = 60.0  # seconds -- pings 1.1.1.1 each try, keep this infrequent

    def on_update(self):
        # [v2] apply any finished background start/stop first.
        self._apply_lifecycle_result()
        self._register_upload_hold()  # [merge-v3] cheap no-op once registered
        self._drain_wigle_events()    # [merge-v3] results from the WiGLE worker
        self._drain_kismet_events()   # [merge-v3] adapter / split notices
        if self._overlay_active:
            self._picker_on_update()
            return

        # Queued WiGLE uploads retry on their own schedule, independent
        # of whether Kismet is currently capturing -- a session queued
        # yesterday should still go out once you're back online today.
        now = time.time()
        if self.engine and now - getattr(self, "_last_wigle_retry", 0.0) >= self.WIGLE_RETRY_INTERVAL:
            self._last_wigle_retry = now
            self._retry_wigle_queue()

        if not self.engine or not self.engine.running or self._lifecycle != "running":
            return

        # v-next: global kill switch. Until now the only way to stop a
        # Kismet session was TAB into the PLUGINS menu and select "Kismet:
        # Stop" -- fine at a desk, a real pain mid-drive when you just
        # need it off NOW. K is free everywhere else in this codebase (no
        # other global hotkey or text-entry field uses it), and this plugin
        # is now ticked every frame regardless of what screen is showing
        # (see app.py's _tick_background_plugins()), so this works from the
        # main map view, with the loot screen open, anywhere -- no menu
        # required. Does nothing if Kismet isn't running (the check above
        # already returned in that case), so it can't be hit by accident
        # when there's nothing to stop.
        # [v2] ...but only when nothing else owns the keyboard. Before v2, K
        # also fired while typing in a text box (input_mode), inside the TAB
        # menu, or in another plugin's overlay, so typing a "k" anywhere
        # would kill the capture.
        try:
            import pyxel
            if pyxel.btnp(pyxel.KEY_K) and self._kill_switch_allowed():
                self.stop_wardrive()
                return
        except ImportError:
            pass

        if self._watch_kismet_process(now):   # [merge-v3] Kismet died by itself
            return

        if self._maybe_rotate_session():
            return

        self._refresh_diag()

        # v2.1: live distance-traveled. Accumulated every frame (not just
        # every poll) so it stays smooth regardless of POLL_INTERVAL, but
        # only while a real GPS fix is current -- same gate the CSV-write
        # path uses, so a stale/no-fix position (0,0 or a frozen last
        # value) never gets counted as "movement".
        if bool(getattr(self.app, "gps_fix", False)):
            lat = getattr(self.app, "player_lat", None)
            lon = getattr(self.app, "player_lon", None)
            if lat is not None and lon is not None:
                if self._last_pos is not None:
                    leg = _haversine_m(self._last_pos[0], self._last_pos[1], lat, lon)
                    # Ignore implausible single-frame jumps (GPS glitch /
                    # fix reacquired far from the last known point) rather
                    # than let one bad reading inflate the whole session
                    # total -- 200 m in one ~2s-or-less frame is already
                    # well past any realistic driving speed for this gap.
                    if leg < 200.0:
                        self._distance_m += leg
                self._last_pos = (lat, lon)

                # v-next: also keep the fuller last-known-good fix (alt +
                # accuracy, not just lat/lon) so stop_wardrive() can flush
                # any still-held devices under a real, honest position
                # instead of discarding them if the fix has since lapsed
                # -- see loot_manager.save_wardriving_network()'s
                # override_fix for why this exists.
                gps_obj = getattr(self.app, "gps", None)
                fix_obj = getattr(gps_obj, "fix", None)
                if fix_obj is not None:
                    alt = getattr(fix_obj, "altitude", 0.0) or 0.0
                    hdop = getattr(fix_obj, "hdop", 99.9)
                    accuracy = round(hdop * 5.0, 1) if hdop < 99 else 0.0
                    self._last_good_fix = (lat, lon, round(alt, 1), accuracy)
                else:
                    self._last_good_fix = (lat, lon, 0.0, 0.0)

        # [v2.6] keep working through queued devices every frame (bounded),
        # including the held-record flush the moment the GPS fix returns.
        if self._backlog or (bool(getattr(self.app, "gps_fix", False))
                             and (self._held_networks or self._held_bt)):
            self._drain_backlog(self.FRAME_BUDGET_S)

        # v-next: the actual HTTP poll now happens on _poll_worker's own
        # thread (started in _launch_from_picker, stopped in
        # stop_wardrive/_emergency_cleanup/on_unload) -- this just reads
        # whatever it most recently published, never blocks on the
        # network itself. If the background thread hasn't produced a
        # new result since the last frame we processed one, there's
        # nothing new to do yet.
        with self._poll_result_lock:
            seq = self._poll_seq
            networks, bt_devices, err = self._latest_poll_result
            if seq == self._last_consumed_seq:
                return
            # [v2] marked consumed inside the lock so the poll thread
            # never merges a batch the main thread already took.
            self._last_consumed_seq = seq
        self._last_poll = time.time()
        if err:
            self._note_poll_error(err, now)   # [merge-v3] throttled; was every poll
            return
        self._note_poll_ok()                  # [merge-v3]

        # A poll that succeeds (HTTP 200, valid JSON) but classifies
        # nothing is exactly the failure mode v1.5 fixed -- Kismet
        # capturing real devices while the HUD counters sit at 0, with
        # no error ever printed because nothing actually raised. Surface
        # it instead of staying silent, throttled to once a minute so it
        # doesn't spam the OUTPUT panel.
        # [v2] with delta polls a batch can legitimately hold only non-AP
        # devices (Wi-Fi clients, bridges), so warn on the session-wide
        # classification count, not on one empty batch.
        if (self.engine.last_raw_count > 0
                and not self.engine.seen_wifi_macs and not self.engine.seen_bt_macs
                and now - getattr(self, "_last_zero_warn", 0.0) >= 60.0):
            self._last_zero_warn = now
            self.term(f"[KISMET] Kismet has {self.engine.last_raw_count} device(s) "
                      f"but none matched the WiFi/BLE filter this poll -- "
                      f"check phyname/type field names if this repeats")

        # v2.1: periodic compact stats line -- auth-type breakdown,
        # strongest AP signal seen this poll, and distance traveled so
        # far this session. The HUD tag (_diag_line, below) is already
        # packed ("KISMET W:12 B:47 GPS:OK") so this is logged to the
        # terminal instead, throttled the same way the "no fix" reminder
        # already is, rather than crowding the HUD.
        if (self.engine.auth_counts
                and now - self._last_stats_log >= 30.0):
            self._last_stats_log = now
            auth_str = ", ".join(
                f"{label}:{count}"
                for label, count in sorted(self.engine.auth_counts.items()))
            sig = self.engine.strongest_signal
            sig_str = f"{sig} dBm" if sig is not None else "n/a"
            rss = self._kismet_rss_mb()
            rss_str = f" KismetRAM:{rss}MB" if rss is not None else ""
            self.term(f"[KISMET] Stats -- Auth[{auth_str}] "
                      f"Strongest:{sig_str} "
                      f"Distance:{_fmt_distance(self._distance_m)}{rss_str}")

        self._consume_batch(networks, bt_devices, now)

    # [v2] app.py screens that take keyboard input. K is only honoured on the
    # main map view or the loot screen (no text entry there).
    _KEYBOARD_OWNERS = (
        "input_mode", "menu_open", "_et_net_screen", "_portal_select_screen",
        "_mitm_screen", "_flipper_screen", "_subghz_screen", "_flash_screen",
        "_watch_screen", "_wl_screen", "_lora_protocol_picker",
        "_mt_profile_screen", "_mt_screen", "_mc_screen", "_wd_screen",
        "_mc_region_screen", "_prox_screen", "_cluster_popup",
        "_game_update_notice", "_gameupd_screen",
        # [merge-v3] screens upstream added in 0.9.20 to app.py's overlay dispatch
        "_solo_profile_picker", "_piglet_collection_screen",
    )

    def _kill_switch_allowed(self) -> bool:
        app = self.app
        if app is None:
            return False
        for attr in self._KEYBOARD_OWNERS:
            val = getattr(app, attr, None)
            if val is not None and val is not False and val != 0:
                return False
        for p in getattr(app, "_plugins", ()) or ():
            if p is not self and getattr(p, "overlay_active", False):
                return False
        return True

    def _kismet_rss_mb(self):
        """[v2] Kismet's resident memory in MB (Linux /proc), or None."""
        proc = getattr(self.engine, "proc", None) if self.engine else None
        if proc is None or proc.poll() is not None:
            return None
        try:
            with open(f"/proc/{proc.pid}/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) // 1024
        except (OSError, ValueError, IndexError, AttributeError):
            pass
        return None

    # [v2.6] Main-thread time budget for device processing per frame. Each
    # new device is one or two fsync'd CSV appends; on an SD card that's
    # several ms each, so a dense poll (or the held-record flush when GPS
    # comes back) used to freeze one frame for hundreds of ms. Work is now
    # queued and drained at most FRAME_BUDGET_S per frame (always >= 1 item,
    # so it can't stall); a stop / rotation drains everything first.
    FRAME_BUDGET_S = 0.008

    def _consume_batch(self, networks, bt_devices, now=None):
        """[v2] Everything done with one poll's devices: GPS-gated saves,
        held-record flush, SCAN list, XP, map sprites, handshake events.
        Split out of on_update() so the final poll at stop (and a batch
        left unconsumed when the poll thread stops) go through exactly the
        same path instead of being dropped.

        [v2.6] Devices are queued here and processed by _drain_backlog()
        under a per-frame time budget."""
        if now is None:
            now = time.time()
        gps_fix = bool(getattr(self.app, "gps_fix", False))
        if not self._gps_announced_this_session:
            self._gps_announced_this_session = True
            gps_dev = getattr(getattr(self.app, "gps", None), "device", None)
            if gps_dev:
                self.term(f"[KISMET] Geotagging from the game's GPS on {gps_dev} "
                          f"(fix: {'OK' if gps_fix else 'waiting'})")
            else:
                self.term("[KISMET] No GPS detected by the game -- WiFi/BLE will be "
                          "held, not written to wardriving.csv, until one connects")
        if not gps_fix and (self._held_networks or self._held_bt) and \
                now - self._last_no_fix_notice >= 60.0:
            # Devices ARE being captured (that's what the "KISMET W:/B:"
            # tag already shows) -- this just confirms periodically that
            # holding them for a fix is a deliberate wait, not a stall.
            self._last_no_fix_notice = now
            self.term(f"[KISMET] Still no GPS fix -- holding "
                      f"{len(self._held_networks)} WiFi + {len(self._held_bt)} "
                      f"BLE device(s), none written to wardriving.csv yet")

        backlog = self._backlog
        for net in networks:
            backlog.append(("w", net))
        for item in bt_devices:
            backlog.append(("b", item))
        if len(backlog) > 5000 and now - getattr(self, "_last_backlog_warn", 0.0) >= 60.0:
            self._last_backlog_warn = now
            self.term(f"[KISMET] Device backlog {len(backlog)} -- storage writes are "
                      f"slower than capture (slow SD card?); nothing is dropped")

        # Passive handshake detection -- Kismet just records the 4-way
        # handshake/PMKID frames it happens to see in normal traffic, the
        # same passive capture WPA-sec-style tools do; it never deauths
        # or injects anything to provoke one. Award the same recognition
        # the ESP32 path gives a captured handshake.
        trigger_hs = getattr(self.app, "_trigger_hs_event", None)
        if trigger_hs is not None:
            new_handshakes = self.engine.handshake_macs - self._known_handshake_macs
            for _ in new_handshakes:
                try:
                    trigger_hs()
                except Exception:
                    pass
            self._known_handshake_macs |= new_handshakes

        self.app.state.wardriving_running = True
        self.app.state.wardriving_networks = len(self.engine.seen_bssids)
        self.app.state.scan_done = True

        if self._saved_since_log:
            self.term(f"[KISMET] +{self._saved_since_log} network(s) logged to wardriving.csv")
            self._saved_since_log = 0
        self._drain_backlog(self.FRAME_BUDGET_S)

    def _drain_backlog(self, budget_s=None):
        """[v2.6] Process queued devices until the queue is empty or budget_s
        has been used (None = no limit). When the GPS fix is back, held
        records are pushed to the FRONT of the queue as save-only items (they
        already got their XP when first seen)."""
        gps_fix = bool(getattr(self.app, "gps_fix", False))
        if gps_fix and (self._held_networks or self._held_bt):
            n_w, n_b = len(self._held_networks), len(self._held_bt)
            for mac, (rssi, name) in self._held_bt.items():
                self._backlog.appendleft(("hb", (mac, rssi, name)))
            for net in self._held_networks.values():
                self._backlog.appendleft(("hw", net))
            self._held_networks.clear()
            self._held_bt.clear()
            self.term(f"[KISMET] GPS fix acquired -- writing {n_w} WiFi + "
                      f"{n_b} BLE held record(s) to wardriving.csv")
        if not self._backlog:
            return
        deadline = None if budget_s is None else time.perf_counter() + budget_s
        ctx = self._drain_ctx()
        processed = 0
        while self._backlog:
            if deadline is not None and processed and time.perf_counter() >= deadline:
                break
            kind, item = self._backlog.popleft()
            try:
                if kind == "w":
                    self._process_wifi(item, gps_fix, ctx)
                elif kind == "b":
                    self._process_bt(item, gps_fix, ctx)
                elif kind == "hw":
                    self._save_wifi_or_hold(item, gps_fix)
                elif kind == "hb":
                    self._save_bt_or_hold(item[0], item[1], item[2], gps_fix)
            except Exception as e:  # one bad device never stalls the queue
                self.term(f"[KISMET] Device processing error ({kind}): {e}")
            processed += 1

    def _drain_ctx(self):
        pyxel = None
        try:
            import pyxel as _pyxel
            pyxel = _pyxel
        except ImportError:
            pass
        return {
            # Cross-session "have I ever seen this before" sets + XP, exactly
            # the same path the ESP32 serial-scan handler uses (app.py's
            # _known_wifi / _known_ble + gain_xp calls).
            "known_wifi": getattr(self.app, "_known_wifi", None),
            "known_ble": getattr(self.app, "_known_ble", None),
            "gain_xp": getattr(self.app, "gain_xp", None),
            "pyxel": pyxel,
            "scan_bssids": None,  # built lazily, once per drain
        }

    # [merge-v3] Every wardriving.csv write from this bridge passes
    #   positioned_only=True  upstream 0.9.20+ writes a 0,0 row when there is no
    #                         fix (SOLO backfills those later). v2's
    #                         LootManager skipped the write instead; that is
    #                         now this opt-in flag, so app.gps_fix being True
    #                         while LootManager's own fix has just lapsed can
    #                         never put a 0,0 row in the file from here;
    #   append_new=True       a new device is one fsync'd append instead of
    #                         upstream's whole-file rewrite (see MERGE_NOTES_V3.md
    #                         for the 5,000-device measurement).
    def _save_wifi_or_hold(self, net, gps_fix):
        if gps_fix:
            if self.app.loot and self.app.loot.save_wardriving_network(
                    net, positioned_only=True, append_new=True):
                self._saved_since_log += 1
        else:
            # Held, not written -- writing now would mean lat=0.0/lon=0.0
            # in wardriving.csv. Keyed by bssid so re-seeing the same AP
            # while waiting on a fix refreshes what's held, no growing queue.
            self._held_networks[net.bssid] = net

    def _save_bt_or_hold(self, mac, rssi, name, gps_fix):
        if not self.app.loot:
            return
        if gps_fix:
            self.app.loot.save_wardriving_bt(mac, rssi, name,
                                             positioned_only=True, append_new=True)
        else:
            self._held_bt[mac] = (rssi, name)

    def _process_wifi(self, net, gps_fix, ctx):
        self._save_wifi_or_hold(net, gps_fix)
        if ctx["scan_bssids"] is None:
            ctx["scan_bssids"] = {n.bssid for n in self.app.state.networks}
        if net.bssid not in ctx["scan_bssids"]:
            self.app.state.networks.append(net)
            ctx["scan_bssids"].add(net.bssid)
        known_wifi, gain_xp, pyxel = ctx["known_wifi"], ctx["gain_xp"], ctx["pyxel"]
        if known_wifi is None or gain_xp is None:
            return
        bssid_upper = net.bssid.upper()
        if bssid_upper in known_wifi:
            gain_xp(1)  # seen again: minimal XP, same as ESP32 path
            return
        known_wifi.add(bssid_upper)
        gain_xp(15)  # new network: full XP, same as ESP32 path
        self.msg(f"[WiFi] {net.ssid} Ch:{net.channel}")
        if pyxel is None:
            return
        try:
            from watchdogs.app import WifiNetwork
            try:
                ch = int(net.channel)
            except (ValueError, TypeError):
                ch = 0
            try:
                rssi_v = int(net.rssi)
            except (ValueError, TypeError):
                rssi_v = -80
            n = WifiNetwork(self.app.player_lat, self.app.player_lon,
                            net.bssid, net.ssid, ch, rssi_v)
            n.spawn_frame = pyxel.frame_count
            self.app.wifi_networks.append(n)
        except Exception as e:
            # v1.6: surfaced instead of a silent pass, so a constructor drift
            # never looks like "Kismet captures but the HUD never moves".
            self.term(f"[KISMET] HUD sprite error (WiFi): {e}")

    def _process_bt(self, item, gps_fix, ctx):
        mac, rssi, name = item
        if self.app.loot:
            self._save_bt_or_hold(mac, rssi, name, gps_fix)
            # bt_devices.csv is the cross-session known-BLE dedup set (what
            # get_known_devices() reads), not what WiGLE gets. [v2] only
            # call save_bt_device() for MACs not yet saved this loot session.
            sess = getattr(self.app.loot, "session_path", None)
            if sess != self._bt_saved_session:
                self._bt_saved_session = sess
                self._bt_saved = set()
            if mac not in self._bt_saved:
                # [merge-v3] append_new: v2's MAC cache + O(1) append +
                # batched loot-DB rescan, now opt-in in LootManager.
                self.app.loot.save_bt_device(mac, rssi, name, False, False,
                                             append_new=True)
                self._bt_saved.add(mac)
        known_ble, gain_xp, pyxel = ctx["known_ble"], ctx["gain_xp"], ctx["pyxel"]
        if known_ble is None or gain_xp is None:
            return
        mac_upper = mac.upper()
        if mac_upper in known_ble:
            gain_xp(1)
            return
        known_ble.add(mac_upper)
        gain_xp(10)
        self.msg(f"[BLE] {name} {mac[-8:]} {rssi}dBm")
        if pyxel is None:
            return
        try:
            from watchdogs.app import BleDevice
            d = BleDevice(self.app.player_lat, self.app.player_lon, mac, name, rssi)
            d.spawn_frame = pyxel.frame_count
            self.app.ble_devices.append(d)
        except Exception as e:
            self.term(f"[KISMET] HUD sprite error (BLE): {e}")
