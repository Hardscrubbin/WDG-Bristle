"""[merge-v3] The install package: hardware detection (tools/hwdetect.py) over
a scripted machine, the file contents install.sh writes
(tools/install_helper.py), the self-test's grouping, and a static check of
the three shell scripts.

The shell scripts themselves cannot be run on the build PC (no bash, no apt,
no systemd): what is checked here is their text -- line endings, quoting,
balanced blocks, and that nothing deletes player data outside the confirmed
--clean / --purge branches. Their first real run is on the uConsole.
"""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import hwdetect  # noqa: E402
import install_helper  # noqa: E402
import selftest  # noqa: E402

GGA = b"$GNGGA,120000.00,5003.6,N,01956.4,E,1,09,1.0,210.0,M,40.0,M,,*5A\r\n"
DONGLE = "/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00"
ESP32 = "/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_AA:BB:CC:DD:EE:FF-if00"
IW_DEV = "phy#1\n\tInterface wlan1\n\t\ttype managed\nphy#2\n\tInterface wlan2\nphy#0\n\tInterface wlan0\n"
PHY_MONITOR = "\tSupported interface modes:\n\t\t * managed\n\t\t * monitor\n\tSupported commands:\n"
PHY_PLAIN = "\tSupported interface modes:\n\t\t * managed\n\tSupported commands:\n"


class Machine(hwdetect.System):
    """A scripted uConsole: every probe answers from these tables."""

    def __init__(self, **changes):
        self.calls, self.serial_reads, self.slept = [], [], 0.0
        self.rails = dict(gps=False, lora=False, sdr=False, usb=True)
        self.pinctrl = True
        self.gpsd_running = True
        self.model = "Raspberry Pi Compute Module 5 Rev 1.0\x00"
        self.cmdline = "console=tty1 root=/dev/mmcblk0p2 rootwait"
        self.links = {"/dev/serial0": "/dev/ttyAMA0", "/dev/serial1": "/dev/ttyAMA1"}
        self.nodes = {"/dev/serial0", "/dev/serial1", "/dev/ttyAMA0", "/dev/ttyAMA1", "/dev/spidev1.0"}
        self.nmea = {"/dev/ttyAMA0": 9600, DONGLE: 9600}       # ports that speak NMEA, and at which baud
        self.by_id = [DONGLE, ESP32]
        self.lsusb = ("Bus 001 Device 004: ID 0bda:2838 Realtek Semiconductor Corp. RTL2838 DVB-T\n"
                      "Bus 001 Device 005: ID 0e8d:7961 MediaTek Inc. Wireless_Device\n"
                      "Bus 001 Device 006: ID 0bda:8812 Realtek Semiconductor Corp. RTL8812AU 802.11a/b/g/n/ac\n"
                      "Bus 001 Device 007: ID 2357:0604 TP-Link UB500 Adapter\n"
                      "Bus 001 Device 008: ID 1546:01a7 U-Blox AG [u-blox 7]\n")
        self.drivers = {"wlan1": "mt7921u", "wlan2": "rtl8812au", "wlan0": "brcmfmac"}
        self.products = {"wlan1": "e8d/7961/100", "wlan2": "bda/8812/0", "hci1": "2357/604/200"}
        self.phys = {"wlan1": ("1", PHY_MONITOR), "wlan2": ("2", PHY_MONITOR), "wlan0": ("0", PHY_PLAIN)}
        self.iw_dev = IW_DEV
        self.hcis = ["hci0", "hci1"]
        self.modules = {"8812au"}
        self.programs = {"kismet", "gpsd", "gpspipe", "rtl_test", "rtl_433", "rtl_power", "dump1090", "pinctrl",
                         "iw", "hciconfig", "lxterminal"}
        self.os_release = 'PRETTY_NAME="DragonOS Pi64 (Debian GNU/Linux 12 bookworm)"\nVERSION_CODENAME=bookworm\n'
        for name, value in changes.items():
            setattr(self, name, value)

    def run(self, command, timeout=8):
        self.calls.append(list(command))
        if command[0] == "pinctrl":
            if not self.pinctrl:
                return 127, "not found"
            pin = int(command[2])
            name = {27: "gps", 16: "lora", 7: "sdr", 23: "usb"}[pin]
            if command[1] == "set":
                self.rails[name] = command[4] == "dh"
                return 0, ""
            return 0, "%d: op dh pd | %s // GPIO%d = output\n" % (pin, "hi" if self.rails[name] else "lo", pin)
        if command[:2] == ["systemctl", "is-active"]:
            return (0 if self.gpsd_running else 3), ""
        if command[:2] == ["systemctl", "stop"]:
            self.gpsd_running = False
            return 0, ""
        if command[:2] == ["systemctl", "start"]:
            self.gpsd_running = True
            return 0, ""
        if command[:2] == ["systemctl", "list-units"]:
            return 0, getattr(self, "gettys", "")
        if command == ["lsusb"]:
            return 0, self.lsusb
        if command == ["iw", "dev"]:
            return 0, self.iw_dev
        if command[:2] == ["iw", "dev"]:
            return 0, "Interface %s\n\twiphy %s\n" % (command[2], self.phys[command[2]][0])
        if command[:2] == ["iw", "phy"]:
            number = command[2][3:]
            return 0, next(text for index, text in self.phys.values() if index == number)
        if command[0] == "modinfo":
            return (0 if command[1] in self.modules else 1), ""
        if command[:2] == ["sh", "-c"]:
            return (0 if command[2].split()[-1] in self.programs else 1), ""
        return 1, ""

    def read(self, path, limit=65536):
        if path.endswith("/model"):
            return self.model
        if path == "/proc/cmdline":
            return self.cmdline
        if path == "/etc/os-release":
            return self.os_release
        match = re.match(r"/sys/class/(?:net|bluetooth)/(\w+)/device/uevent", path)
        if match and match.group(1) in self.products:
            return "DEVTYPE=usb_interface\nPRODUCT=%s\n" % self.products[match.group(1)]
        return ""

    def exists(self, path):
        return path in self.nodes or path in self.by_id

    def realpath(self, path):
        return self.links.get(path, path)

    def glob(self, pattern):
        if pattern == "/dev/serial/by-id/*":
            return sorted(self.by_id)
        if pattern == "/sys/class/bluetooth/hci*":
            return ["/sys/class/bluetooth/" + hci for hci in self.hcis]
        return []

    def readlink_name(self, path):
        return self.drivers.get(path.split("/")[4], "")

    def sleep(self, seconds):
        self.slept += seconds

    def read_serial(self, device, baud, seconds):
        self.serial_reads.append((device, baud, dict(self.rails), self.gpsd_running))
        return GGA if self.nmea.get(device) == baud else b"\x00\xff garbage"


class DetectTests(unittest.TestCase):
    def test_the_full_rig_is_found_and_everything_is_put_back(self):
        machine = Machine()
        result = hwdetect.detect(machine)
        self.assertEqual((result["board"]["kind"], result["os"]["dragonos"], result["aio"]), ("CM5", True, True))
        self.assertEqual((result["gps"]["uart"], result["gps"]["uart_baud"]), ("/dev/ttyAMA0", 9600))
        self.assertEqual(result["gps"]["usb"], [DONGLE])
        self.assertEqual(hwdetect.gpsd_devices(result), ["/dev/ttyAMA0", DONGLE])        # the UART first
        self.assertTrue(result["sdr"].startswith("Realtek"))
        self.assertEqual([w["kind"] for w in result["wifi"]], ["Alfa AWUS036AXML", "AC1200 (Realtek)", "built-in"])
        self.assertEqual([w["monitor"] for w in result["wifi"]], [True, True, False])
        self.assertEqual(sorted(h["kind"] for h in result["bluetooth"]), ["UB500", "built-in"])
        self.assertEqual(result["esp32"], [ESP32])
        self.assertTrue(result["lora_spi"])
        # the rails that were off were switched on for the look, and are off again
        self.assertEqual(machine.rails, dict(gps=False, lora=False, sdr=False, usb=True))
        self.assertIn(["pinctrl", "set", "27", "op", "dh"], machine.calls)
        self.assertIn(["pinctrl", "set", "27", "op", "dl"], machine.calls)
        self.assertNotIn(["pinctrl", "set", "16", "op", "dh"], machine.calls)            # LoRa is not needed
        self.assertGreaterEqual(machine.slept, 5)
        # gpsd was stopped while the ports were read, and runs again
        self.assertTrue(machine.gpsd_running)
        self.assertTrue(machine.serial_reads)
        for _device, _baud, rails, gpsd_running in machine.serial_reads:
            self.assertFalse(gpsd_running)
            self.assertTrue(rails["gps"])
        table = hwdetect.table(result)
        for line in ("FOUND     AIO GP-02 GPS (UART)", "FOUND     USB GPS dongle", "FOUND     RTL-SDR (RTL2832U)",
                     "FOUND     Alfa AWUS036AXML", "FOUND     AC1200 (RTL8812AU)", "FOUND     UB500 Bluetooth dongle",
                     "FOUND     ESP32 on USB (optional)", "Board      CM5", "[DragonOS]", "Programs   all present"):
            self.assertIn(line, table)
        json.dumps(result)                                                              # what install.sh reads

    def test_ports_that_are_not_gps_are_never_opened(self):
        machine = Machine(cmdline="console=serial0,115200 console=tty1")
        result = hwdetect.detect(machine)
        opened = {device for device, _b, _r, _g in machine.serial_reads}
        self.assertNotIn(ESP32, opened)                              # the ESP32's port is left alone
        self.assertNotIn("/dev/ttyAMA1", opened)                     # the Bluetooth UART (/dev/serial1)
        self.assertNotIn("/dev/ttyAMA0", opened)                     # a serial console lives there
        self.assertIsNone(result["gps"]["uart"])
        self.assertIn("serial console", hwdetect.table(result))
        getty = Machine(gettys="serial-getty@ttyAMA0.service loaded active running Serial Getty\n")
        self.assertNotIn("/dev/ttyAMA0", {d for d, _b, _r, _g in (hwdetect.detect(getty), getty.serial_reads)[1]})

    def test_missing_parts_are_reported_not_guessed(self):
        machine = Machine(nmea={}, by_id=[], lsusb="Bus 001 Device 006: ID 0bda:8812 Realtek RTL8812AU\n",
                          iw_dev="phy#0\n\tInterface wlan0\n", hcis=["hci0"], modules=set(), gpsd_running=False,
                          nodes={"/dev/serial0", "/dev/ttyAMA0"}, programs={"iw"})
        result = hwdetect.detect(machine)
        table = hwdetect.table(result)
        self.assertEqual(hwdetect.gpsd_devices(result), [])          # nothing is handed to gpsd on a guess
        for line in ("NOT FOUND AIO GP-02 GPS (UART)", "NOT FOUND USB GPS dongle", "NOT FOUND RTL-SDR (RTL2832U)",
                     "NOT FOUND SX1262 LoRa", "NOT FOUND Alfa AWUS036AXML", "NOT FOUND UB500 Bluetooth dongle",
                     "NO DRIVER bound: monitor-mode driver not installed", "missing: kismet"):
            self.assertIn(line, table)
        self.assertFalse(machine.gpsd_running)                       # it was not running: it is not started
        self.assertNotIn(["systemctl", "start", "gpsd.socket"], machine.calls)

    def test_other_boards_and_options(self):
        other = Machine(pinctrl=False, model="Raspberry Pi Compute Module 4 Rev 1.1\x00",
                        os_release='PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n')
        result = hwdetect.detect(other)
        self.assertEqual((result["board"]["kind"], result["aio"], result["os"]["dragonos"]), ("CM4", False, False))
        self.assertIn("NOT FOUND HackerGadgets AIO v2", hwdetect.table(result))
        quiet = Machine()
        result = hwdetect.detect(quiet, probe=False, use_rails=False)
        self.assertEqual(quiet.serial_reads, [])
        self.assertTrue(quiet.gpsd_running)
        self.assertNotIn(["systemctl", "stop", "gpsd.socket", "gpsd.service"], quiet.calls)
        self.assertFalse(any(call[:2] == ["pinctrl", "set"] for call in quiet.calls))
        fast = Machine(nmea={"/dev/ttyAMA0": 115200})
        self.assertEqual(hwdetect.detect(fast)["gps"]["uart_baud"], 115200)
        self.assertEqual(hwdetect.board(Machine(model=""))["kind"], "unknown")

    def test_a_probe_that_raises_still_restores_rails_and_gpsd(self):
        class Broken(Machine):
            def glob(self, pattern):
                raise OSError("sysfs went away")
        machine = Broken()
        with self.assertRaises(OSError):
            hwdetect.detect(machine)
        self.assertEqual(machine.rails, dict(gps=False, lora=False, sdr=False, usb=True))
        self.assertTrue(machine.gpsd_running)


class HelperTests(unittest.TestCase):
    def test_gpsd_defaults_keep_what_is_not_ours(self):
        current = ('# Devices gpsd should collect to at boot time.\nSTART_DAEMON="false"\n'
                   'DEVICES=""\nGPSD_OPTIONS="-G -b"\nUSBAUTO="false"\nGPSD_SOCKET="/var/run/gpsd.sock"\n')
        text = install_helper.gpsd_defaults(current, ["/dev/ttyAMA0", DONGLE])
        self.assertIn("# Devices gpsd should collect to at boot time.", text)
        self.assertIn('GPSD_SOCKET="/var/run/gpsd.sock"', text)
        self.assertIn('DEVICES="/dev/ttyAMA0 %s"' % DONGLE, text)
        self.assertIn('GPSD_OPTIONS="-n -G -b"', text)
        self.assertIn('START_DAEMON="true"', text)
        self.assertIn('USBAUTO="true"', text)
        for key in ("DEVICES", "GPSD_OPTIONS", "START_DAEMON", "USBAUTO"):
            self.assertEqual(len(re.findall(r"^%s=" % key, text, re.MULTILINE)), 1, key)
        self.assertEqual(install_helper.gpsd_defaults(text, ["/dev/ttyAMA0", DONGLE]), text)   # idempotent
        fresh = install_helper.gpsd_defaults("", ["/dev/ttyS0", "relative", ""])
        self.assertIn('DEVICES="/dev/ttyS0"', fresh)
        self.assertIn('GPSD_OPTIONS="-n"', fresh)
        self.assertTrue(text.endswith("\n") and "\r" not in text)

    def test_the_game_reads_back_what_the_installer_writes(self):
        from watchdogs import gps_manager
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "gpsd")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(install_helper.gpsd_defaults('GPSD_OPTIONS=""\n', ["/dev/ttyAMA0", DONGLE]))
            self.assertEqual(gps_manager.gpsd_configured_devices(path), ["/dev/ttyAMA0", DONGLE])

    def test_desktop_entry_uses_real_absolute_paths(self):
        folder = os.path.join(os.sep, "home", "hardscrubbin", "My Games", "wdg-bristle")
        text = install_helper.desktop_entry(folder)
        lines = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        self.assertEqual(lines["Name"], "WDG-Bristle")
        self.assertEqual(lines["Type"], "Application")
        self.assertEqual(lines["Path"], os.path.abspath(folder))
        self.assertEqual(lines["Exec"].replace("\\\\", "\\"),         # (backslashes only exist on the build PC)
                         '"%s"' % os.path.join(os.path.abspath(folder), "wdg-launch.sh"))
        self.assertTrue(lines["Icon"].endswith(os.path.join("assets", "icon-wdg-bristle.png")))
        self.assertTrue(text.startswith("[Desktop Entry]\n"))
        for forbidden in ("~", "$HOME", "$DIR"):
            self.assertNotIn(forbidden, text)
        with self.assertRaises(ValueError):
            install_helper.desktop_entry("/tmp/x\nExec=evil")
        self.assertTrue((ROOT / "assets" / "icon-wdg-bristle.png").is_file(), "the icon PNG is not in assets/")

    def test_launcher_and_blacklist(self):
        script = install_helper.launcher_script()
        self.assertTrue(script.startswith("#!/bin/bash\n"))
        inner = script.split("INNER='", 1)[1].split("\n'\n", 1)[0]
        self.assertNotIn("'", inner)                                  # it lives in one single-quoted string
        self.assertIn("sudo -E PYTHONDONTWRITEBYTECODE=1", inner)
        self.assertIn(".venv/bin/python3 -m watchdogs", inner)
        self.assertEqual(len(re.findall(r"\bsudo (?!-n|-E)", inner)), 0)   # only the game itself may prompt
        for terminal in install_helper.TERMINALS:
            self.assertIn(terminal, script)
        self.assertEqual(script.count("\nesac\n"), 1)
        self.assertNotIn("\r", script)
        blacklist = install_helper.blacklist_conf()
        self.assertIn("blacklist dvb_usb_rtl28xxu\n", blacklist)
        self.assertEqual(install_helper.main(["nonsense"]), 2)


def shell_lint(text):
    """Problems a typo would cause: an unclosed quote on a line, unbalanced
    if/fi, case/esac, for|while/done."""
    problems = []
    blocks = dict(if_=0, case=0, loop=0)
    for number, line in enumerate(text.split("\n"), 1):
        single = double = False
        index = 0
        bare = ""                                   # the line without its quoted text and comments
        while index < len(line):
            char = line[index]
            if single:
                single = char != "'"
            elif char == "\\":
                index += 1
            elif double:
                double = char != '"'
            elif char == "'":
                single = True
            elif char == '"':
                double = True
            elif char == "#" and (index == 0 or line[index - 1] in " \t;"):
                break
            else:
                bare += char
            index += 1
        if single or double:
            problems.append("line %d: unclosed quote: %s" % (number, line.strip()[:60]))
        code = bare.strip()
        if not code:
            continue
        words = re.split(r"[;\s]+", code)
        blocks["if_"] += words.count("if") - words.count("fi")
        blocks["case"] += (1 if words[:1] == ["case"] else 0) - (1 if words[:1] == ["esac"] else 0)
        blocks["loop"] += (1 if words[:1] in (["for"], ["while"]) else 0) - words.count("done")
    for name, count in blocks.items():
        if count:
            problems.append("unbalanced %s blocks: %+d" % (name.rstrip("_"), count))
    return problems


class ShellScriptTests(unittest.TestCase):
    SCRIPTS = ("install.sh", "selftest.sh", "uninstall.sh")

    def text(self, name):
        data = (ROOT / name).read_bytes()
        self.assertNotIn(b"\r", data, "%s has Windows line endings: bash would fail on the uConsole" % name)
        self.assertTrue(data.startswith(b"#!/bin/bash\n"), name)
        self.assertTrue(data.endswith(b"\n"), name)
        return data.decode("utf-8")

    def test_scripts_are_well_formed(self):
        self.assertEqual(shell_lint('echo "ok"\nif true; then echo \'a "b"\'; fi\n'), [])
        self.assertTrue(shell_lint('echo "broken\n'))
        self.assertTrue(shell_lint("if true; then\n  echo x\n"))
        for name in self.SCRIPTS:
            with self.subTest(script=name):
                self.assertEqual(shell_lint(self.text(name)), [])
        self.assertEqual(shell_lint(install_helper.launcher_script().split("INNER='", 1)[0]), [])

    def test_they_refuse_root_and_the_wrong_folder(self):
        for name in self.SCRIPTS:
            text = self.text(name)
            self.assertIn('if [ "$(id -u)" -eq 0 ]; then', text, name)
            self.assertIn('DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"', text, name)
        self.assertIn("watchdogs/__main__.py", self.text("install.sh"))
        self.assertIn("watchdogs/__main__.py", self.text("uninstall.sh"))

    def test_player_data_is_deleted_only_after_typing_delete(self):
        for name, flag in (("install.sh", "--clean"), ("uninstall.sh", "--purge")):
            text = self.text(name)
            self.assertIn(flag, text)
            lines = text.split("\n")
            deleting = [index for index, line in enumerate(lines)
                        if re.search(r"\brm\b", line) and re.search(r"loot|maps|secrets\.conf|whitelist", line)]
            self.assertTrue(deleting, name)
            for index in deleting:
                before = "\n".join(lines[max(0, index - 4):index])
                self.assertIn('if [ "$ANSWER" = "DELETE" ]; then', before, "%s line %d" % (name, index + 1))
            self.assertEqual(text.count('read -r ANSWER'), 1, name)
        install = self.text("install.sh")
        self.assertIn('if [ -f "$DIR/secrets.conf" ]; then\n    ok "secrets.conf kept', install)
        self.assertNotIn("rm -rf \"$DIR\"\n", install)

    def test_install_backs_up_gpsd_and_validates_sudoers(self):
        text = self.text("install.sh")
        backup = text.index('sudo cp -p /etc/default/gpsd "$BACKUP"')
        write = text.index('sudo install -m 644 "$NEW_DEFAULTS" /etc/default/gpsd')
        self.assertLess(backup, write)                               # the backup comes first
        self.assertIn("visudo -cf", text)
        self.assertLess(text.index("visudo -cf"), text.index('"$SUDOERS_FILE"\n'))
        self.assertIn('if [ "$PASSWORDLESS" -eq 1 ]; then', text)    # the sudoers rule is opt-in
        self.assertIn("gio set", text)
        self.assertIn("metadata::trusted true", text)
        for step in range(1, 10):
            self.assertIn('step "%d/9' % step, text)
        self.assertIn("NEXT STEPS", text)

    def test_selftest_groups_and_parses(self):
        self.assertEqual(selftest.feature_of("test_v3_gps_multi.py"), "GPS (gpsd, dual receivers)")
        self.assertEqual(selftest.feature_of("test_v3_kismet_extras.py"), "Kismet bridge")
        self.assertEqual(selftest.feature_of("test_upload_recovery.py"), "Uploads (WiGLE, WDGWars)")
        self.assertEqual(selftest.feature_of("test_something_else.py"), "Other game tests")
        self.assertTrue((ROOT / "selftest.sh").read_text(encoding="utf-8").strip().endswith(
            'exec "$PY" -B "$DIR/tools/selftest.py" "$@"'))
        self.assertEqual(selftest.feature_of("test_v3_sim_harness.py"), "Simulation harness")
        self.assertEqual(selftest.feature_of("test_gps_uart_power.py"), "GPS (gpsd, dual receivers)")

    # ---- the log: disk-full noise cut down, summary at both ends ----------

    FULL = "Cannot persist wardrive CSV /tmp/wdg-sim-x/loot/s/%s: [Errno 28] No space left on device"

    def test_disk_full_lines_are_cut_to_three_per_scenario(self):
        noise = [self.FULL % ("wardriving.csv" if n % 2 else "bt_devices.csv") for n in range(60)]
        text = "\n".join(
            ["[PASS] kismet: normal capture and stop (0.77 s)"] + noise
            + ["[PASS] storage: disk full during CSV writes (0.20 s)"] + noise[:2]
            + ["[FAIL] backup: a full disk leaves no partial backup (0.05 s)",
               "       OSError: [Errno 28] No space left on device",
               "       backup.zip.part was left behind"]
            + noise[:5] + ["[PASS] backup: corrupt archives are refused (0.07 s)", ""])
        lines = selftest.squeeze_noise(text).splitlines()
        self.assertEqual(lines[:4], ["[PASS] kismet: normal capture and stop (0.77 s)"] + noise[:3])
        self.assertEqual(lines[4], '       (57 more "No space left on device" lines suppressed)')
        self.assertEqual(lines[5], "[PASS] storage: disk full during CSV writes (0.20 s)")
        self.assertEqual(lines[6:8], noise[:2])                       # 2 lines: nothing to suppress
        self.assertEqual(lines[8:11], ["[FAIL] backup: a full disk leaves no partial backup (0.05 s)",
                                       "       OSError: [Errno 28] No space left on device",
                                       "       backup.zip.part was left behind"])   # a failure's detail is kept
        self.assertEqual(lines[11:14], noise[:3])
        self.assertEqual(lines[14], '       (2 more "No space left on device" lines suppressed)')
        self.assertEqual(lines[15], "[PASS] backup: corrupt archives are refused (0.07 s)")
        self.assertEqual(len(lines), 16)
        self.assertEqual(sum("suppressed)" in line for line in lines), 2)

    def test_disk_full_lines_are_cut_per_test_in_the_suite_output(self):
        noise = [self.FULL % "wardriving.csv"] * 10
        text = "\n".join(
            ["test_disk_full (test_x.StorageTests.test_disk_full) ... " + noise[0]] + noise[1:] + ["ok"]
            + ["test_next (test_x.StorageTests.test_next) ... " + noise[0]] + noise[1:4] + ["ok"]
            + ["test_plain (test_x.StorageTests.test_plain) ... ok", "",
               "======================================================================",
               "ERROR: test_broken (test_x.StorageTests.test_broken)",
               "Traceback (most recent call last):",
               '  File "x.py", line 1, in test_broken',
               "OSError: [Errno 28] No space left on device", "", "Ran 4 tests in 0.1s", ""])
        lines = selftest.squeeze_noise(text).splitlines()
        self.assertEqual(lines[0], "test_disk_full (test_x.StorageTests.test_disk_full) ... " + noise[0])
        self.assertEqual(lines[1:3], noise[1:3])
        self.assertEqual(lines[3:5], ["ok", '       (7 more "No space left on device" lines suppressed)'])
        self.assertEqual(lines[5], "test_next (test_x.StorageTests.test_next) ... " + noise[0])
        self.assertEqual(lines[8:10], ["ok", '       (1 more "No space left on device" lines suppressed)'])
        self.assertIn("OSError: [Errno 28] No space left on device", lines)   # the real error's own line
        self.assertIn("ERROR: test_broken (test_x.StorageTests.test_broken)", lines)
        self.assertEqual(selftest.squeeze_noise("no noise here\nat all\n"), "no noise here\nat all\n")
        self.assertEqual(selftest.squeeze_noise(""), "")

    def test_the_summary_is_at_the_top_and_at_the_bottom_of_the_log(self):
        with tempfile.TemporaryDirectory() as folder:
            log = os.path.join(folder, "selftest.log")
            with open(log, "w", encoding="utf-8") as fh:
                fh.write("SUMMARY: this run did not finish (the summary is written here when it does)\n"
                         "folder x | python 3 | started now\n\n===== tests/test_a.py =====\ndetail line\n")
            summary = "TEST SUITE  feature  passed failed\n  PASS  GPS  10  0\nRESULT: PASS\n"
            selftest.finish_log(log, summary)
            text = Path(log).read_text(encoding="utf-8")
        self.assertTrue(text.startswith(selftest.RULE + "\nSUMMARY (repeated at the end of this file)\n"
                                        + selftest.RULE + "\n" + summary))
        self.assertTrue(text.endswith("SUMMARY (the same as at the top)\n" + selftest.RULE + "\n" + summary))
        self.assertEqual(text.count(summary), 2)
        self.assertNotIn("did not finish", text)
        self.assertNotIn("EVERY FAILURE IN FULL", text)               # nothing failed
        self.assertLess(text.index("DETAILS"), text.index("===== tests/test_a.py ====="))
        self.assertIn("detail line\n", text)

    def test_failures_are_listed_in_full_right_under_the_top_summary(self):
        rule, dash = "=" * 70, "-" * 70
        output = "\n".join([
            "test_a (test_x.T.test_a) ... ok", "test_b (test_x.T.test_b) ... FAIL",
            "test_c (test_x.T.test_c) ... ERROR", "",
            rule, "FAIL: test_b (test_x.T.test_b)", dash, "Traceback (most recent call last):",
            '  File "test_x.py", line 9, in test_b', "AssertionError: 0 != 2", "",
            rule, "ERROR: test_c (test_x.T.test_c)", dash, "Traceback (most recent call last):",
            '  File "test_x.py", line 12, in test_c', "OSError: [Errno 98] Address already in use", "",
            dash, "Ran 3 tests in 0.010s", "", "FAILED (failures=1, errors=1)", ""])
        blocks = selftest.failure_blocks(output)
        self.assertEqual(len(blocks), 2)
        self.assertTrue(blocks[0].startswith("FAIL: test_b (test_x.T.test_b)"))
        self.assertTrue(blocks[0].endswith("AssertionError: 0 != 2"))
        self.assertTrue(blocks[1].endswith("OSError: [Errno 98] Address already in use"))
        self.assertEqual(selftest.failure_blocks("test_a (test_x.T.test_a) ... ok\n\n%s\nRan 1 test in 0.1s\n\nOK\n"
                                                 % dash), [])
        with tempfile.TemporaryDirectory() as folder:
            log = os.path.join(folder, "selftest.log")
            with open(log, "w", encoding="utf-8") as fh:
                fh.write("SUMMARY: this run did not finish\n\n===== tests/test_x.py =====\n" + output)
            selftest.finish_log(log, "RESULT: FAIL\n", "----- tests/test_x.py -----\n" + "\n".join(blocks))
            text = Path(log).read_text(encoding="utf-8")
        top = text[:text.index("DETAILS (every test file")]
        self.assertIn("RESULT: FAIL", top)
        self.assertIn("EVERY FAILURE IN FULL", top)
        self.assertIn("OSError: [Errno 98] Address already in use", top)
        self.assertIn("AssertionError: 0 != 2", top)

    def test_the_run_does_not_depend_on_this_machines_gpsd(self):
        text = (ROOT / "tools" / "selftest.py").read_text(encoding="utf-8")
        for pinned in ('WDG_GPSD_DEFAULTS=os.path.join(scratch_home, "no-gpsd-defaults")',
                       'WDG_GPSD_HOST="127.0.0.1", WDG_GPSD_PORT=CLOSED_PORT',
                       '"WDG_GPS_DEVICE"', '"JANOS_GPSD_PORT"'):
            self.assertIn(pinned, text)
        self.assertEqual(selftest.CLOSED_PORT, "1")
        self.assertIn("not used by the tests", selftest.gpsd_on_this_machine())


if __name__ == "__main__":
    unittest.main()
