"""
war4_hardware.py — Revision 4 — hardware layer, rebuilt around STABLE
device identity.

THE PROBLEM THIS SOLVES: Linux assigns interface names (wlan0, wlan1)
and Bluetooth indices (hci0, hci1) in whatever order devices happen to
enumerate at boot — which changes every time you plug things in
differently, in a different USB port, or after any of them drop out
and reattach. Every earlier revision identified adapters by these
names directly, so "my capture dongle" silently became a different
physical device depending on plug order.

THE FIX: every WiFi and Bluetooth adapter's OWN HARDWARE MAC ADDRESS
never changes. detect_wifi()/detect_ble() return each adapter's MAC
alongside whatever name Linux currently gave it. The app stores your
selection by MAC. resolve_by_mac() re-scans and finds "whichever
current name has this MAC" fresh, every single time — so plug order,
port, or renumbering never matters again.

GPS devices are already stable via /dev/serial/by-id (a symlink keyed
to the device's own USB serial number, not an enumeration index) —
that part from the previous revision was already correct and is kept
as-is.

Monitor-mode switching stays SCOPED to one interface at a time (the
fix from the previous round, which correctly stopped it from taking
down NetworkManager / your desktop WiFi system-wide) — that part was
right and is kept unchanged.
"""

import glob
import os
import re
import subprocess
import time


def _run(cmd, timeout=20, sudo=False):
    if sudo and os.geteuid() != 0:
        cmd = ["sudo"] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"


def have(tool):
    return _run(["which", tool])[0] == 0


# ---------------- USB descriptor helpers (for the "what chipset is this" guess) ----------------

def _lsusb_map():
    rc, out, _ = _run(["lsusb"])
    result = {}
    if rc != 0:
        return result
    for line in out.splitlines():
        m = re.search(r"ID ([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s+(.*)", line)
        if m:
            result[m.group(1).lower()] = m.group(2).strip()
    return result


def _usbid_for_net(iface):
    path = f"/sys/class/net/{iface}/device/uevent"
    if not os.path.exists(path):
        return None
    try:
        content = open(path).read()
    except OSError:
        return None
    m = re.search(r"PRODUCT=([0-9a-fA-F]+)/([0-9a-fA-F]+)", content)
    if m:
        return f"{m.group(1).zfill(4)}:{m.group(2).zfill(4)}".lower()
    return None


def _driver_for_net(iface):
    path = f"/sys/class/net/{iface}/device/driver"
    if os.path.islink(path):
        return os.path.basename(os.readlink(path))
    return None


def _bus_for_net(iface):
    path = f"/sys/class/net/{iface}/device/subsystem"
    if os.path.islink(path):
        return os.path.basename(os.path.realpath(path))
    return "unknown"


def _mac_for_net(iface):
    path = f"/sys/class/net/{iface}/address"
    try:
        return open(path).read().strip().lower()
    except OSError:
        return None


def _usb_ids_from_device_path(device_path, max_up=6):
    """Walk up from a /sys/class/.../device symlink target looking for
    idVendor/idProduct files. For a USB-attached interface (WiFi or
    Bluetooth) those files live a few directory levels above the
    interface node itself, on the actual USB device node — this walks
    up until it finds them or gives up. Returns 'vvvv:pppp' or None
    (None for anything not USB-attached, e.g. onboard/UART/SDIO)."""
    try:
        cur = os.path.realpath(device_path)
    except OSError:
        return None
    for _ in range(max_up):
        vend_path = os.path.join(cur, "idVendor")
        prod_path = os.path.join(cur, "idProduct")
        if os.path.exists(vend_path) and os.path.exists(prod_path):
            try:
                vend = open(vend_path).read().strip().lower()
                prod = open(prod_path).read().strip().lower()
                return f"{vend}:{prod}"
            except OSError:
                return None
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _usbid_for_bt(hci):
    return _usb_ids_from_device_path(f"/sys/class/bluetooth/{hci}/device")


def _bus_for_bt(hci):
    path = f"/sys/class/bluetooth/{hci}/device/subsystem"
    if os.path.islink(path):
        return os.path.basename(os.path.realpath(path))
    return None


def _phy_supports_monitor(iface):
    rc, phy_out, _ = _run(["iw", "dev", iface, "info"])
    m = re.search(r"wiphy\s+(\d+)", phy_out)
    if not m:
        return True
    rc2, info, _ = _run(["iw", "phy", f"phy{m.group(1)}", "info"])
    if rc2 != 0:
        return True
    block = info.split("Supported interface modes", 1)
    if len(block) < 2:
        return True
    return "* monitor" in block[1].split("Supported", 1)[0]


def is_monitor(iface):
    """[bristle] True when the interface is in monitor mode right now, False when it is not, None
    when that cannot be read. Read only."""
    rc, info, _ = _run(["iw", "dev", iface, "info"])
    if rc != 0 or not info:
        return None
    return "type monitor" in info


def phy_info(iface):
    """[bristle] The text of `iw phy <phy> info` for this interface ("" when it cannot be read).
    It lists every frequency the card can tune; plugins/_kismet_extras.parse_iw_channels reads it.
    Read only: nothing about the card is changed. Call it off the game thread."""
    rc, phy_out, _ = _run(["iw", "dev", iface, "info"])
    m = re.search(r"wiphy\s+(\d+)", phy_out)
    if not m:
        return ""
    rc2, info, _ = _run(["iw", "phy", f"phy{m.group(1)}", "info"])
    return info if rc2 == 0 else ""


# ---------------- static interface pinning (udev + NetworkManager) ----------------
# This is the actual fix for the Alfa card "popping in and out" on the
# Kismet adapter picker: Kismet's own retry=true (see kismet_wardrive.py's
# engine.start()) can only reopen an interface if it comes back under the
# SAME name. A USB drop/reconnect normally re-enumerates it under a new
# name, so retry silently has nothing to reopen -- the adapter just sits
# dropped until you back out of the menu and wait. Pinning the card to a
# fixed name by its MAC address removes that failure mode: the name never
# changes again regardless of USB port or how many times it reconnects.

_UDEV_RULE_PATH = "/etc/udev/rules.d/70-persistent-net.rules"
_NM_UNMANAGED_PATH = "/etc/NetworkManager/conf.d/unmanaged-wifi.conf"


def _udev_pins():
    """Parse existing MAC->fixed-name pins already in the udev rules file.
    Returns {mac_lower: name}."""
    pins = {}
    if not os.path.exists(_UDEV_RULE_PATH):
        return pins
    try:
        content = open(_UDEV_RULE_PATH).read()
    except OSError:
        return pins
    for line in content.splitlines():
        m = re.search(r'ATTR\{address\}=="([0-9a-fA-F:]+)".*NAME="([^"]+)"', line)
        if m:
            pins[m.group(1).lower()] = m.group(2)
    return pins


def is_pinned(mac):
    """True if this MAC already has a static-name udev rule."""
    return bool(mac) and mac.lower() in _udev_pins()


def pin_adapter_static(mac, name="warwifi0", log=print):
    """Write a udev rule pinning `mac` to the fixed interface name `name`,
    plus a NetworkManager unmanaged-devices entry so NM can't grab it back
    into managed mode when it re-enumerates. Requires root -- goes through
    `sudo tee`, same privilege level every other write in this module
    already assumes (kill_interferers, set_monitor, etc. all use sudo=True
    the same way).

    Idempotent: if this MAC is already pinned, returns immediately rather
    than appending a duplicate rule line.

    Returns (ok: bool, message: str)."""
    if not mac:
        return False, "No MAC address given"
    mac = mac.lower()
    existing = _udev_pins()
    if mac in existing:
        return True, f"{mac} already pinned as {existing[mac]}"
    # [v2] a second adapter pinned with the default name would collide
    # with the first; take the next free warwifiN instead.
    used = set(existing.values())
    if name in used:
        m = re.match(r"^(.*?)(\d+)$", name)
        stem, n = (m.group(1), int(m.group(2))) if m else (name, 0)
        while f"{stem}{n}" in used:
            n += 1
        name = f"{stem}{n}"

    rule_line = (f'SUBSYSTEM=="net", ACTION=="add", '
                 f'ATTR{{address}}=="{mac}", NAME="{name}"\n')
    try:
        rc = subprocess.run(
            ["sudo", "tee", "-a", _UDEV_RULE_PATH],
            input=rule_line, text=True, capture_output=True, timeout=10)
        if rc.returncode != 0:
            return False, f"Could not write udev rule: {rc.stderr.strip()}"
    except Exception as e:
        return False, f"Could not write udev rule: {e}"
    log(f"[HW] Pinned {mac} -> {name} in {_UDEV_RULE_PATH}")

    nm_block = f"[keyfile]\nunmanaged-devices=mac:{mac}\n"
    # [v2] one conf file PER MAC. `tee` (no -a) on a single shared file
    # replaced it each time, so pinning a second adapter silently made the
    # first one NetworkManager-managed again.
    nm_path = os.path.join(os.path.dirname(_NM_UNMANAGED_PATH),
                           f"unmanaged-wdg-{mac.replace(':', '')}.conf")
    try:
        subprocess.run(["sudo", "mkdir", "-p",
                       os.path.dirname(nm_path)],
                      capture_output=True, timeout=10)
        rc2 = subprocess.run(
            ["sudo", "tee", nm_path],
            input=nm_block, text=True, capture_output=True, timeout=10)
        if rc2.returncode != 0:
            log(f"[HW] WARNING: udev rule written but NetworkManager "
                f"unmanaged-devices entry failed: {rc2.stderr.strip()}")
    except Exception as e:
        log(f"[HW] WARNING: udev rule written but NetworkManager "
            f"unmanaged-devices entry failed: {e}")

    _run(["udevadm", "control", "--reload-rules"], sudo=True)
    _run(["systemctl", "restart", "NetworkManager"], sudo=True)
    log(f"[HW] udev rules reloaded, NetworkManager restarted -- "
        f"unplug/replug {mac} once for the new name to take effect")
    return True, (f"{mac} pinned as {name}. Unplug and replug the "
                  f"adapter once for it to take effect.")


# ---------------- detection (returns current name + STABLE mac together) ----------------

def detect_wifi():
    """Every WiFi interface right now, each tagged with its stable MAC."""
    rc, out, _ = _run(["iw", "dev"])
    ifaces = re.findall(r"Interface\s+(\S+)", out)
    lsusb = _lsusb_map()
    results = []
    for iface in ifaces:
        mac = _mac_for_net(iface)
        usbid = _usbid_for_net(iface)
        driver = _driver_for_net(iface)
        bus = _bus_for_net(iface)
        desc = lsusb.get(usbid) if usbid else None
        guess = desc or (f"driver: {driver}" if driver else "unknown chipset")

        rc2, info, _ = _run(["iw", "dev", iface, "info"])
        mode = "monitor" if "type monitor" in info else "managed"
        can_monitor = _phy_supports_monitor(iface)

        likely_onboard = bus in ("pci", "sdio", "mmc") or (iface == "wlan0" and bus == "unknown")
        note = ""
        if not can_monitor:
            note = "can't do monitor mode"
        elif likely_onboard:
            note = "likely onboard WiFi — prefer a USB dongle for capture"

        results.append({
            "kind": "wifi", "id": iface, "mac": mac, "driver": driver, "bus": bus,
            "usbid": usbid, "guess": guess, "mode": mode,
            "can_monitor": can_monitor, "note": note, "likely_onboard": likely_onboard,
            "pinned": is_pinned(mac),
        })
    results.sort(key=lambda w: (w.get("likely_onboard", False), not w["can_monitor"]))
    return results


def detect_ble():
    """Every Bluetooth adapter right now, each tagged with its stable BD
    (MAC) address AND, new in Rev 4.5, an actual identity guess — the
    same treatment WiFi adapters already got. Before this, every BLE
    adapter just said "Bluetooth (usb)" or "Bluetooth (uart)", which is
    useless once you have more than one and can't tell them apart.

    USB adapters get looked up in lsusb by their real vendor:product ID
    (same mechanism as WiFi). The UART one is virtually always the
    board's own onboard/internal Bluetooth radio (paired with onboard
    WiFi) rather than anything you plugged in, so it's flagged as
    likely onboard — same "likely_onboard" convention as WiFi, so the
    UI can treat it consistently."""
    rc, out, _ = _run(["hciconfig", "-a"])
    lsusb = _lsusb_map()
    results = []
    for block in re.split(r"\n(?=hci\d+:)", out):
        m = re.match(r"(hci\d+):", block)
        if not m:
            continue
        hci = m.group(1)
        bus_m = re.search(r"Bus:\s*(\S+)", block)
        mac_m = re.search(r"BD Address:\s*([0-9A-Fa-f:]+)", block)
        up = "UP RUNNING" in block
        bus = (bus_m.group(1) if bus_m else _bus_for_bt(hci)) or "?"
        usbid = _usbid_for_bt(hci) if bus.upper() == "USB" else None
        desc = lsusb.get(usbid) if usbid else None
        likely_onboard = bus.upper() in ("UART", "SDIO")

        if desc:
            guess = desc
        elif bus.upper() == "USB":
            guess = "USB Bluetooth (chipset unknown — try 'lsusb' to identify)"
        elif likely_onboard:
            guess = f"onboard Bluetooth ({bus}) — likely built into this device"
        else:
            guess = f"Bluetooth ({bus})"

        results.append({
            "kind": "ble", "id": hci,
            "mac": mac_m.group(1).lower() if mac_m else None,
            "bus": bus, "usbid": usbid,
            "up": up, "guess": guess, "likely_onboard": likely_onboard,
        })
    results.sort(key=lambda b: b.get("likely_onboard", False))
    return results


def detect_gps():
    """GPS devices — already stable via /dev/serial/by-id (keyed to the
    device's own USB serial number). The by-id path itself IS the
    durable identity here; no MAC involved.

    Each candidate is also tagged with a best-effort likely_gps guess,
    read from the same USB descriptor string already in the by-id
    name — matching known GPS vendor names (u-blox, SiRF, GlobalSat,
    Garmin) vs. known generic-serial-bridge chips and dev-board names
    (CH340, CP210x, FTDI, ESP32, Arduino) that show up identically on
    /dev/ttyACM*/ttyUSB* but are NOT a GPS. This never blocks picking a
    device either way — it's a hint shown alongside the raw name, same
    as the WiFi vendor guesses elsewhere in this file, not a filter."""
    GPS_VENDOR_HINTS = ("u-blox", "ublox", "sirf", "globalsat", "garmin", "gps")
    NOT_GPS_HINTS = ("ch340", "cp210", "ftdi", "esp32", "esp-32", "arduino",
                     "qinheng", "silicon_labs", "wch.cn")

    def _classify(descriptor):
        d = descriptor.lower()
        if any(h in d for h in GPS_VENDOR_HINTS):
            return True, None
        if any(h in d for h in NOT_GPS_HINTS):
            return False, "descriptor matches a generic USB-serial chip / dev board, not a known GPS vendor"
        return None, "vendor not recognized either way — could be either"

    results = []
    seen = set()
    for link in sorted(glob.glob("/dev/serial/by-id/*")):
        target = os.path.realpath(link)
        seen.add(target)
        name = os.path.basename(link).replace("usb-", "").replace("_", " ")
        likely_gps, note = _classify(name)
        results.append({"kind": "gps", "id": target, "by_id_path": link, "guess": name,
                        "likely_gps": likely_gps, "note": note})
    for dev in sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")):
        if dev not in seen:
            results.append({"kind": "gps", "id": dev, "by_id_path": None,
                           "guess": "serial device (no stable by-id link — may renumber)",
                           "likely_gps": None, "note": "no USB descriptor to check — could be anything"})
    # Show likely-GPS candidates first, unlikely ones last, unknowns in between —
    # so the real GPS is usually the first thing you see, not buried under an
    # ESP32 dev board that happened to enumerate first.
    results.sort(key=lambda g: {True: 0, None: 1, False: 2}[g["likely_gps"]])
    return results


def detect_all():
    return {"wifi": detect_wifi(), "ble": detect_ble(), "gps": detect_gps()}


def resolve_wifi_by_mac(mac):
    """Given a MAC you selected earlier, find whatever interface name
    currently has it (or None if that adapter isn't plugged in right
    now). This is what makes device selection survive replugging."""
    for w in detect_wifi():
        if w["mac"] == mac:
            return w
    return None


def resolve_ble_by_mac(mac):
    for b in detect_ble():
        if b["mac"] == mac:
            return b
    return None


# ---------------- monitor mode toggle (scoped to ONE interface — kept from the previous fix) ----------------

def kill_interferers(iface, log=print):
    """
    Release ONE interface from NetworkManager so it can switch to
    monitor mode — WITHOUT touching any other interface or killing
    NetworkManager itself. (The previous system-wide 'airmon-ng check
    kill' was what silently took out desktop WiFi as collateral damage;
    this scoped version is what replaced it and is kept unchanged here.)
    """
    if have("nmcli"):
        _run(["nmcli", "device", "set", iface, "managed", "no"], sudo=True)
        log(f"Released {iface} from NetworkManager (only this interface)")
    rc, out, _ = _run(["pgrep", "-af", f"wpa_supplicant.*{iface}"])
    if rc == 0:
        for line in out.splitlines():
            pid = line.split()[0]
            _run(["kill", pid], sudo=True)
        log(f"Stopped wpa_supplicant attached to {iface}")


def release_interface_back(iface, log=print):
    if have("nmcli"):
        _run(["nmcli", "device", "set", iface, "managed", "yes"], sudo=True)
        log(f"Handed {iface} back to NetworkManager")


def set_monitor(iface, enable, log=print):
    """Put ONE interface into/out of monitor mode via plain `iw` (not
    airmon-ng start, which renames interfaces and would break MAC-based
    resolution's assumption that the interface stays addressable by
    the same name for the rest of this run)."""
    target = "monitor" if enable else "managed"
    log(f"Switching {iface} -> {target} mode...")
    _run(["ip", "link", "set", iface, "down"], sudo=True)
    rc, out, err = _run(["iw", "dev", iface, "set", "type", target], sudo=True)
    _run(["ip", "link", "set", iface, "up"], sudo=True)

    if rc != 0:
        low = err.lower()
        if "-95" in err or "not supported" in low:
            return False, f"{iface} can't do {target} mode — driver doesn't support it"
        if "busy" in low or "-16" in err:
            return False, f"{iface} is busy — try again, or unplug/replug it"
        return False, f"{iface} failed: {err or 'unknown error'}"

    rc2, info, _ = _run(["iw", "dev", iface, "info"])
    actual = "monitor" if "type monitor" in info else "managed"
    if actual == target:
        log(f"{iface} confirmed in {target} mode")
        return True, f"{iface} is now in {target} mode"
    return False, f"{iface} did not confirm {target} mode (reads as '{actual}')"


def bring_up_ble(hci, log=print):
    rc, out, _ = _run(["hciconfig", hci])
    if rc != 0:
        return False, f"Bluetooth adapter {hci} not found"
    _run(["hciconfig", hci, "up"], sudo=True)
    rc, out, _ = _run(["hciconfig", hci])
    if "UP RUNNING" in out:
        return True, f"{hci} up"
    return False, f"{hci} would not come up"


def start_gpsd(gps_dev, log=print):
    if not os.path.exists(gps_dev):
        return False, f"GPS device {gps_dev} not found"
    _run(["systemctl", "stop", "gpsd.socket", "gpsd"], sudo=True)
    _run(["gpsd", gps_dev, "-F", "/var/run/gpsd.sock"], sudo=True)
    time.sleep(2)
    if _run(["pgrep", "-x", "gpsd"])[0] == 0:
        return True, f"gpsd started on {gps_dev}"
    return False, f"gpsd did not start on {gps_dev}"


def check_gps_fix(gps_dev=None, log=print, timeout=8):
    """Standalone GPS check — talks to gpsd directly, no Kismet involved."""
    if gps_dev and not os.path.exists(gps_dev):
        return False, f"GPS device {gps_dev} not found"
    if _run(["pgrep", "-x", "gpsd"])[0] != 0:
        if not gps_dev:
            return False, "gpsd isn't running and no GPS device was given to start it"
        ok, msg = start_gpsd(gps_dev, log=log)
        if not ok:
            return False, msg
        time.sleep(2)
    if not have("gpspipe"):
        return False, "gpspipe not installed (part of gpsd-clients)"
    rc, out, _ = _run(["timeout", str(timeout), "gpspipe", "-w", "-n", "10"])
    for line in out.splitlines():
        if '"class":"TPV"' in line and '"lat"' in line:
            m_lat = re.search(r'"lat":([\-\d\.]+)', line)
            m_lon = re.search(r'"lon":([\-\d\.]+)', line)
            if m_lat and m_lon:
                return True, f"GPS fix acquired: {m_lat.group(1)}, {m_lon.group(1)}"
    if _run(["pgrep", "-x", "gpsd"])[0] == 0:
        return False, "gpsd running but no fix yet — needs clear sky, can take a minute"
    return False, "Could not get a reading from gpsd"


def wait_for_first_gps_fix(gps_dev, log=print, max_wait=75, poll_interval=4):
    """Block (from a worker thread — never call this from the UI thread)
    until gpsd reports its FIRST real fix, or max_wait seconds pass.

    This exists because of a well-documented Kismet quirk (confirmed
    against Kismet's own issue tracker, not a guess): if Kismet is
    started before GPS has ever had a fix, it can get permanently stuck
    reporting no fix for the rest of that run — even after gpsd goes on
    to get a perfectly good lock a minute later. The only known
    workaround is to have a fix BEFORE Kismet starts. Rather than make
    that a manual ritual ("wait outside before hitting Start"), this
    makes the app do the waiting itself, once, right after gpsd comes
    up and before Kismet is launched.

    Returns (got_fix: bool, message: str). On timeout it returns False
    but does NOT stop the calling code from proceeding — a session
    without a fix yet still captures WiFi/BLE fine, it just means GPS
    tagging may not kick in until a restart. The caller decides what to
    do with that."""
    log(f"[INFO] Waiting for GPS's first fix before starting Kismet (up to {max_wait}s) — "
        f"this avoids a known Kismet bug where starting before any fix exists can make it "
        f"never show one, even after gpsd locks on later")
    waited = 0
    while waited < max_wait:
        ok, msg = check_gps_fix(gps_dev, log=lambda m: None, timeout=min(6, max_wait - waited))
        if ok:
            log(f"[OK] {msg} — safe to start Kismet now")
            return True, msg
        waited += poll_interval
        if waited < max_wait:
            time.sleep(poll_interval)
    log(f"[WARN] No GPS fix after {max_wait}s — starting anyway without one. "
        f"WiFi/BLE capture will work fine, but GPS tagging may not start working "
        f"this session even once you do get a fix outside. If that happens, "
        f"Stop and Start again once you have a fix.")
    return False, f"no fix after {max_wait}s"


def unblock_and_rescan(log=print):
    if have("rfkill"):
        _run(["rfkill", "unblock", "all"], sudo=True)
        log("Cleared rfkill soft-blocks")
    if have("nmcli"):
        _run(["nmcli", "radio", "wifi", "on"], sudo=True)
    log("Re-scanning for adapters...")


def restore_all_wifi(log=print):
    """Undo monitor mode on ALL WiFi interfaces and bring normal WiFi
    back — kills Kismet, sets every interface back to managed, restarts
    NetworkManager."""
    _run(["pkill", "-INT", "-x", "kismet"], sudo=True)
    time.sleep(1)
    _run(["pkill", "-KILL", "-x", "kismet"], sudo=True)
    rc, out, _ = _run(["iw", "dev"])
    ifaces = re.findall(r"Interface\s+(\S+)", out)
    for iface in ifaces:
        rc2, info, _ = _run(["iw", "dev", iface, "info"])
        if "type monitor" in info:
            log(f"Returning {iface} to managed mode")
            _run(["ip", "link", "set", iface, "down"], sudo=True)
            _run(["iw", "dev", iface, "set", "type", "managed"], sudo=True)
            _run(["ip", "link", "set", iface, "up"], sudo=True)
    if have("nmcli"):
        _run(["nmcli", "radio", "wifi", "on"], sudo=True)
        for iface in ifaces:
            _run(["nmcli", "device", "set", iface, "managed", "yes"], sudo=True)
    if have("systemctl"):
        _run(["systemctl", "restart", "NetworkManager"], sudo=True)
    log("All Wi-Fi radios restored to normal — desktop Wi-Fi should work again")


def find_usb_mount(hint="", log=print):
    if hint and os.path.isdir(hint) and os.access(hint, os.W_OK):
        return hint, f"Using configured USB path: {hint}"
    candidates = []
    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or ""
    for base in (f"/media/{user}", "/media", "/mnt", "/run/media/" + user):
        if os.path.isdir(base):
            for entry in glob.glob(os.path.join(base, "*")):
                if os.path.isdir(entry):
                    candidates.append(entry)
    root_dev = os.stat("/").st_dev
    for path in candidates:
        try:
            if os.stat(path).st_dev == root_dev:
                continue
            if os.access(path, os.W_OK):
                return path, f"Auto-detected USB mount: {path}"
        except OSError:
            continue
    return None, "No writable USB mount found"


def resolve_log_dir(cfg, log=print):
    if cfg.get("usb_auto_detect", True):
        mount, msg = find_usb_mount(cfg.get("usb_mount_hint", ""), log=log)
        log(msg)
        if mount:
            log_dir = os.path.join(mount, "wardriver-logs")
            try:
                os.makedirs(log_dir, exist_ok=True)
                return log_dir
            except OSError as e:
                log(f"Could not create {log_dir}: {e} — falling back")
    fallback = cfg.get("fallback_log_dir") or os.path.expanduser("~/wardriver-logs")
    os.makedirs(fallback, exist_ok=True)
    log(f"Logging to fallback (NOT USB): {fallback}")
    return fallback


def internet_available():
    rc, _, _ = _run(["ping", "-c", "1", "-W", "3", "1.1.1.1"], timeout=6)
    return rc == 0


# Rev 6.2: honest system diagnostics for the SYSTEM_DIAGNOSTICS panel in
# the rebuilt uCONSOLE_WARDv1.3-style layout -- CPU%, temp, free storage,
# and battery %, read directly from /proc and /sys the same way the rest
# of this module reads adapter info, with no extra dependency (psutil
# may not be on the DragonOS image). Anything this particular board
# doesn't expose comes back as None so the UI shows the wireframe's own
# "(---)" placeholder instead of a made-up number.
_cpu_last = {"total": None, "idle": None}


def read_system_diagnostics():
    result = {"cpu_pct": None, "temp_c": None, "storage_free_gb": None, "battery_pct": None}

    # CPU%: delta of /proc/stat's aggregate "cpu" line between two calls
    # a tick apart -- the same trick `top`/`vmstat` use, no dependency.
    try:
        with open("/proc/stat") as f:
            fields = f.readline().split()[1:8]
        total = sum(int(x) for x in fields)
        idle = int(fields[3])
        prev_total, prev_idle = _cpu_last["total"], _cpu_last["idle"]
        if prev_total is not None:
            dt = total - prev_total
            di = idle - prev_idle
            if dt > 0:
                result["cpu_pct"] = round(100.0 * (dt - di) / dt, 1)
        _cpu_last["total"], _cpu_last["idle"] = total, idle
    except (OSError, ValueError, IndexError):
        pass

    # Temp: the standard Pi/CM4 thermal zone.
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            result["temp_c"] = round(int(f.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        pass

    # Storage: free space where the app itself lives.
    try:
        import shutil
        usage = shutil.disk_usage(os.path.dirname(os.path.abspath(__file__)))
        result["storage_free_gb"] = round(usage.free / (1024 ** 3), 1)
    except OSError:
        pass

    # Battery: standard power_supply sysfs capacity file -- present on
    # boards with a fuel-gauge-backed battery, absent on others.
    try:
        base = "/sys/class/power_supply"
        for name in os.listdir(base):
            cap_path = os.path.join(base, name, "capacity")
            if os.path.exists(cap_path):
                with open(cap_path) as f:
                    result["battery_pct"] = int(f.read().strip())
                break
    except OSError:
        pass

    return result
