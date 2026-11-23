#!/usr/bin/env python3
"""[merge-v3] Find the hardware of the uConsole rig -- for install.sh.

    sudo python3 tools/hwdetect.py [--json FILE] [--no-probe] [--no-rails]

Prints a found / not found table and (with --json) writes what install.sh
needs: the board, the GPS receivers gpsd should own, and what is missing.
Standard library only (it runs before the game's venv exists).

Nothing is guessed. A GPS receiver counts as found only when NMEA sentences
were actually read from its port; a Wi-Fi adapter only when the kernel shows
an interface for it. What this script may change, and puts back:
  * with an AIO v2 board, the GPS / SDR / internal-USB rails are switched on
    for the look and returned to how they were (--no-rails: leave them alone,
    parts behind a rail that is off are then "not found (rail off)");
  * gpsd is stopped while serial ports are read (two readers on one port
    break both) and started again afterwards if it was running.
Ports that are not GPS are left alone: the Bluetooth UART, a serial console,
and USB serial devices that identify as an ESP32, a Flipper or a debug probe.
"""
import glob
import json
import os
import re
import subprocess
import sys
import time

RAILS = (("gps", 27), ("lora", 16), ("sdr", 7), ("usb", 23))
RTL_SDR_IDS = ("0bda:2838", "0bda:2832")
UB500_IDS = ("2357:0604",)
ALFA_AXML_IDS = ("0e8d:7961",)
ESP32_IDS = ("303a:1001", "303a:1002")
AC1200_IDS = ("0bda:8812", "0bda:881a", "0bda:881b", "0bda:881c", "0bda:b812", "0bda:b82c",
              "2357:0101", "2357:0103", "2357:010d", "2357:0115", "2357:012d", "7392:a822",
              "0b05:17d2", "050d:1109", "2001:3315", "0846:9052", "2604:0012")
AC1200_NAME = re.compile(r"8812\s*au|88x2\s*bu|8822\s*bu|8812\s*bu|ac1200", re.IGNORECASE)
MONITOR_DRIVERS = ("8812au", "88XXau", "rtl8812au", "88x2bu", "rtw88_8812au", "rtw_8812au", "rtw88_8822bu")
NOT_GPS = re.compile(r"espressif|flipper|black_magic|stmicro|jtag|esp32|cmsis|j-link", re.IGNORECASE)
NMEA = re.compile(rb"\$(?:G[NPLABQ]|BD)(?:GGA|RMC|GSV|GSA|GLL|VTG|TXT|ZDA),")
UART_CANDIDATES = ("/dev/serial0", "/dev/ttyAMA0", "/dev/ttyS0", "/dev/ttyAMA1", "/dev/ttyAMA2")


class System:
    """Every look at the machine goes through here (tests replace it)."""

    def run(self, command, timeout=8):
        try:
            done = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, timeout=timeout)
            return done.returncode, done.stdout or ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, str(exc)

    def read(self, path, limit=65536):
        try:
            with open(path, "rb") as fh:
                return fh.read(limit).decode("utf-8", "replace")
        except OSError:
            return ""

    def exists(self, path):
        return os.path.exists(path)

    def realpath(self, path):
        return os.path.realpath(path)

    def glob(self, pattern):
        return sorted(glob.glob(pattern))

    def readlink_name(self, path):
        try:
            return os.path.basename(os.readlink(path))
        except OSError:
            return ""

    def sleep(self, seconds):
        time.sleep(seconds)

    def read_serial(self, device, baud, seconds):
        """Raw bytes read from a serial port, without changing what it is
        (no modem-line games beyond what opening a tty does)."""
        import termios
        speeds = {4800: termios.B4800, 9600: termios.B9600, 38400: termios.B38400, 115200: termios.B115200}
        try:
            fd = os.open(device, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError:
            return b""
        data = b""
        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0                                          # iflag: raw
            attrs[1] = 0                                          # oflag
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] = 0                                          # lflag
            attrs[4] = attrs[5] = speeds[baud]
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIFLUSH)
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and len(data) < 8192:
                try:
                    chunk = os.read(fd, 1024)
                except BlockingIOError:
                    chunk = b""
                except OSError:
                    break
                if chunk:
                    data += chunk
                    if NMEA.search(data):
                        break
                else:
                    time.sleep(0.05)
        except (OSError, termios.error, KeyError):
            pass
        finally:
            os.close(fd)
        return data


# ----------------------------------------------------------------------
def board(system) -> dict:
    model = ""
    for path in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        model = system.read(path).replace("\x00", "").strip()
        if model:
            break
    match = re.search(r"Compute Module (\d)", model)
    kind = "CM%s" % match.group(1) if match else ("Raspberry Pi" if "Raspberry Pi" in model else "unknown")
    config = system.read("/boot/firmware/config.txt") or system.read("/boot/config.txt")
    uconsole = bool(re.search(r"^\s*dtoverlay=.*clockworkpi-uconsole", config, re.MULTILINE)
                    or "uconsole" in model.lower())
    return dict(model=model, kind=kind, uconsole=uconsole)


def os_info(system) -> dict:
    text = system.read("/etc/os-release")
    fields = dict(re.findall(r'^([A-Z_]+)="?([^"\n]*)"?$', text, re.MULTILINE))
    dragon = any("dragon" in system.read(path).lower() for path in ("/etc/os-release", "/etc/issue",
                                                                    "/etc/dragonos-release"))
    dragon = dragon or system.exists("/usr/share/dragonos") or system.exists("/etc/dragonos-release")
    return dict(pretty=fields.get("PRETTY_NAME", "unknown"), codename=fields.get("VERSION_CODENAME", ""),
                version=fields.get("VERSION_ID", ""), dragonos=dragon)


def usb_devices(system) -> dict:
    code, out = system.run(["lsusb"])
    found = {}
    if code == 0:
        for line in out.splitlines():
            match = re.search(r"ID ([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s*(.*)", line)
            if match:
                found[match.group(1).lower()] = match.group(2).strip()
    return found


def rail_levels(system):
    """{name: True/False/None} or None when pinctrl is missing (no AIO tools)."""
    levels = {}
    for name, pin in RAILS:
        code, out = system.run(["pinctrl", "get", str(pin)], timeout=4)
        if code == 127:
            return None
        match = re.search(r"^\s*%d:\s*[^|\n]*\|\s*(hi|lo)\b" % pin, out, re.MULTILINE)
        levels[name] = (match.group(1) == "hi") if code == 0 and match else None
    return levels


def set_rail(system, name, on) -> bool:
    pin = dict(RAILS)[name]
    return system.run(["pinctrl", "set", str(pin), "op", "dh" if on else "dl"], timeout=4)[0] == 0


def console_uarts(system) -> set:
    """UARTs the kernel or a login prompt uses: never probe or hand to gpsd."""
    busy = set()
    for name in re.findall(r"console=(tty(?:AMA|S)\d+|serial\d)", system.read("/proc/cmdline")):
        busy.add(system.realpath("/dev/" + name))
    code, out = system.run(["systemctl", "list-units", "--type=service", "--state=running", "--no-legend",
                            "serial-getty@*"], timeout=5)
    if code == 0:
        for name in re.findall(r"serial-getty@([\w.-]+)\.service", out):
            busy.add(system.realpath("/dev/" + name))
    return busy


def uart_candidates(system) -> list:
    """Header UARTs worth a look, as real device paths, in a fixed order."""
    bluetooth = system.realpath("/dev/serial1") if system.exists("/dev/serial1") else ""
    busy = console_uarts(system)
    seen, out = set(), []
    for path in UART_CANDIDATES:
        if not system.exists(path):
            continue
        real = system.realpath(path)
        if real in seen or real == bluetooth or real in busy:
            continue
        seen.add(real)
        out.append(real)
    return out


def usb_serial_candidates(system) -> list:
    """/dev/serial/by-id entries that could be a GPS dongle."""
    return [path for path in system.glob("/dev/serial/by-id/*") if not NOT_GPS.search(os.path.basename(path))]


def probe_nmea(system, device, seconds=3.0):
    """The baud rate at which NMEA was read from ``device``, or None."""
    for baud in (9600, 115200, 38400, 4800):
        if NMEA.search(system.read_serial(device, baud, seconds)):
            return baud
    return None


def find_gps(system, probe=True) -> dict:
    """dict(uart=..., usb=[...]) of receivers that really produce NMEA."""
    result = dict(uart=None, uart_baud=None, usb=[], skipped=[], candidates=[])
    uarts, dongles = uart_candidates(system), usb_serial_candidates(system)
    result["candidates"] = uarts + dongles
    for busy in sorted(console_uarts(system)):
        result["skipped"].append("%s is a serial console: left alone" % busy)
    if not probe:
        return result
    for device in uarts:
        baud = probe_nmea(system, device)
        if baud:
            result["uart"], result["uart_baud"] = device, baud
            break
    for device in dongles:
        if probe_nmea(system, device, seconds=2.5):
            result["usb"].append(device)
    return result


def wifi_adapters(system, usb) -> list:
    code, out = system.run(["iw", "dev"])
    rows = []
    for iface in re.findall(r"Interface\s+(\S+)", out if code == 0 else ""):
        driver = system.readlink_name("/sys/class/net/%s/device/driver" % iface)
        uevent = system.read("/sys/class/net/%s/device/uevent" % iface)
        match = re.search(r"PRODUCT=([0-9a-fA-F]+)/([0-9a-fA-F]+)", uevent)
        usb_id = "%s:%s" % (match.group(1).zfill(4), match.group(2).zfill(4)) if match else ""
        usb_id = usb_id.lower()
        phy = re.search(r"wiphy\s+(\d+)", system.run(["iw", "dev", iface, "info"])[1])
        monitor = None
        if phy:
            info = system.run(["iw", "phy", "phy" + phy.group(1), "info"])[1]
            block = info.split("Supported interface modes", 1)
            monitor = "* monitor" in block[1].split("Supported", 1)[0] if len(block) > 1 else None
        name = usb.get(usb_id, "")
        if usb_id in ALFA_AXML_IDS or driver.startswith("mt7921"):
            kind = "Alfa AWUS036AXML"
        elif usb_id in AC1200_IDS or AC1200_NAME.search(name) or driver in MONITOR_DRIVERS:
            kind = "AC1200 (Realtek)"
        elif not usb_id:
            kind = "built-in"
        else:
            kind = "USB Wi-Fi"
        rows.append(dict(iface=iface, driver=driver or "?", usb_id=usb_id, name=name, kind=kind, monitor=monitor))
    return rows


def bluetooth(system, usb) -> list:
    rows = []
    for path in system.glob("/sys/class/bluetooth/hci*"):
        hci = os.path.basename(path)
        if ":" in hci:
            continue
        uevent = system.read(path + "/device/uevent")
        match = re.search(r"PRODUCT=([0-9a-fA-F]+)/([0-9a-fA-F]+)", uevent)
        usb_id = ("%s:%s" % (match.group(1).zfill(4), match.group(2).zfill(4))).lower() if match else ""
        rows.append(dict(hci=hci, usb_id=usb_id,
                         kind="UB500" if usb_id in UB500_IDS else "USB Bluetooth" if usb_id else "built-in",
                         name=usb.get(usb_id, "")))
    return rows


def ac1200_driver(system) -> str:
    for module in MONITOR_DRIVERS:
        if system.run(["modinfo", module], timeout=5)[0] == 0:
            return module
    return ""


def tools(system) -> dict:
    found = {}
    for tool in ("kismet", "gpsd", "gpspipe", "rtl_test", "rtl_433", "rtl_power", "dump1090", "pinctrl",
                 "iw", "hciconfig", "lxterminal"):
        found[tool] = system.run(["sh", "-c", "command -v %s" % tool], timeout=4)[0] == 0
    return found


def detect(system, probe=True, use_rails=True) -> dict:
    result = dict(board=board(system), os=os_info(system), notes=[])
    before = rail_levels(system)
    result["aio"] = before is not None
    switched = []
    gpsd_was_running = False
    try:
        if before is not None and use_rails:
            for name in ("gps", "sdr", "usb"):
                if before.get(name) is False and set_rail(system, name, True):
                    switched.append(name)
            if switched:
                result["notes"].append("rails switched on for the look and put back afterwards: "
                                       + ", ".join(switched))
                system.sleep(6.0)                    # USB devices behind the rail need time to enumerate
        if probe:
            gpsd_was_running = system.run(["systemctl", "is-active", "--quiet", "gpsd"], timeout=5)[0] == 0
            if gpsd_was_running:
                system.run(["systemctl", "stop", "gpsd.socket", "gpsd.service"], timeout=15)
                result["notes"].append("gpsd was stopped while the GPS ports were read, then started again")
        usb = usb_devices(system)
        result["usb"] = usb
        result["rails"] = before
        result["gps"] = find_gps(system, probe=probe)
        result["wifi"] = wifi_adapters(system, usb)
        result["bluetooth"] = bluetooth(system, usb)
        result["sdr"] = next((usb[key] for key in RTL_SDR_IDS if key in usb), "")
        result["esp32"] = [path for path in system.glob("/dev/serial/by-id/*")
                           if re.search(r"espressif|esp32", os.path.basename(path), re.IGNORECASE)]
        if not result["esp32"] and any(key in usb for key in ESP32_IDS):
            result["esp32"] = ["(on USB, no serial port yet)"]
        result["lora_spi"] = system.exists("/dev/spidev1.0")
        result["ac1200_on_usb"] = [key for key, name in usb.items() if key in AC1200_IDS or AC1200_NAME.search(name)]
        result["ac1200_driver"] = ac1200_driver(system)
        result["tools"] = tools(system)
    finally:
        for name in switched:
            set_rail(system, name, False)
        if gpsd_was_running:
            system.run(["systemctl", "start", "gpsd.socket"], timeout=15)
    return result


def gpsd_devices(result) -> list:
    """The device list for /etc/default/gpsd: the UART first, then each dongle
    by its stable /dev/serial/by-id name."""
    gps = result.get("gps", {})
    return ([gps["uart"]] if gps.get("uart") else []) + list(gps.get("usb", []))


def table(result) -> str:
    rows = []

    def row(what, found, detail=""):
        rows.append("  %-9s %-34s %s" % ("FOUND" if found else "NOT FOUND", what, detail))

    b, o, gps = result["board"], result["os"], result["gps"]
    rails = result.get("rails")
    lines = ["", "Hardware found on this machine", "=" * 78,
             "  Board      %s  (%s)%s" % (b["kind"], b["model"] or "model not readable",
                                         "  uConsole" if b["uconsole"] else ""),
             "  System     %s%s" % (o["pretty"], "  [DragonOS]" if o["dragonos"] else ""), ""]
    row("HackerGadgets AIO v2 (pinctrl)", result["aio"],
        "rails now: " + ", ".join("%s=%s" % (name.upper(), {True: "ON", False: "off", None: "?"}[rails.get(name)])
                                  for name, _pin in RAILS) if rails else "no pinctrl tool: rails cannot be read")
    rail_off = rails is not None and rails.get("gps") is False and "gps" not in " ".join(result["notes"])
    row("AIO GP-02 GPS (UART)", bool(gps["uart"]),
        "%s at %s baud" % (gps["uart"], gps["uart_baud"]) if gps["uart"] else
        ("rail off: not looked at" if rail_off else "no NMEA on %s" % (", ".join(
            c for c in gps["candidates"] if c.startswith("/dev/tty")) or "any header UART")))
    row("USB GPS dongle", bool(gps["usb"]), ", ".join(gps["usb"]) if gps["usb"] else
        "no USB serial device produced NMEA (plugged in?)")
    row("RTL-SDR (RTL2832U)", bool(result["sdr"]), result["sdr"])
    row("SX1262 LoRa (SPI device node)", result["lora_spi"], "/dev/spidev1.0" if result["lora_spi"] else
        "no /dev/spidev1.0 (SPI1 not enabled?)")
    alfa = [w for w in result["wifi"] if w["kind"] == "Alfa AWUS036AXML"]
    row("Alfa AWUS036AXML", bool(alfa), ", ".join("%s (%s, monitor %s)" % (
        w["iface"], w["driver"], {True: "yes", False: "NO", None: "?"}[w["monitor"]]) for w in alfa))
    ac = [w for w in result["wifi"] if w["kind"] == "AC1200 (Realtek)"]
    if ac:
        row("AC1200 (RTL8812AU)", True, ", ".join("%s (%s, monitor %s)" % (
            w["iface"], w["driver"], {True: "yes", False: "NO", None: "?"}[w["monitor"]]) for w in ac))
    elif result["ac1200_on_usb"]:
        row("AC1200 (RTL8812AU)", False, "on USB (%s) but NO DRIVER bound: monitor-mode driver not installed"
            % ", ".join(result["ac1200_on_usb"]))
    else:
        row("AC1200 (RTL8812AU)", False, "not plugged in" + (
            "; a monitor driver (%s) is installed" % result["ac1200_driver"] if result["ac1200_driver"] else ""))
    ub500 = [h for h in result["bluetooth"] if h["kind"] == "UB500"]
    row("UB500 Bluetooth dongle", bool(ub500), ", ".join(h["hci"] for h in ub500))
    builtin = [h for h in result["bluetooth"] if h["kind"] == "built-in"]
    row("Built-in Bluetooth", bool(builtin), ", ".join(h["hci"] for h in builtin))
    row("ESP32 on USB (optional)", bool(result["esp32"]), ", ".join(os.path.basename(p) for p in result["esp32"]))
    lines += rows
    other = [w for w in result["wifi"] if w["kind"] in ("built-in", "USB Wi-Fi")]
    if other:
        lines.append("  %-9s %-34s %s" % ("", "other Wi-Fi", ", ".join("%s (%s)" % (w["iface"], w["driver"])
                                                                         for w in other)))
    missing = [name for name, present in result["tools"].items() if not present]
    lines += ["", "  Programs   " + ("all present" if not missing else "missing: " + ", ".join(missing))]
    for note in result["notes"] + gps["skipped"]:
        lines.append("  Note       " + note)
    lines.append("=" * 78)
    return "\n".join(lines)


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    target = None
    if "--json" in args:
        target = args[args.index("--json") + 1]
    result = detect(System(), probe="--no-probe" not in args, use_rails="--no-rails" not in args)
    result["gpsd_devices"] = gpsd_devices(result)
    print(table(result))
    if target:
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
