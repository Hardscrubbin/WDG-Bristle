#!/usr/bin/env python3
"""[merge-v3] File contents for install.sh / uninstall.sh. Standard library only.

install.sh stays a thin wrapper: everything that has to be exactly right in a
file -- /etc/default/gpsd, the desktop entry, the launcher, the RTL-SDR
blacklist -- is produced here, where it can be tested without a uConsole.
Nothing in this module writes outside the paths it is given.

    python3 tools/install_helper.py gpsd-defaults <current file or -> <devices...>
    python3 tools/install_helper.py desktop <install dir> <name> <icon path>
    python3 tools/install_helper.py launcher <install dir>
    python3 tools/install_helper.py blacklist
    python3 tools/install_helper.py gpsd-devices <hwdetect json>
    python3 tools/install_helper.py old-name install-dir|desktop-id   (names of the earlier build)
"""
import json
import os
import re
import sys

DESKTOP_ID = "wdg-bristle"
DISPLAY_NAME = "WDG-Bristle"
MARK = "# managed by wdg-bristle install.sh"
# [bristle] What the installer of this build's earlier name wrote. A block that starts with its
# marker is ours too, and is replaced instead of being left next to a second one. The old names
# live in one place, watchdogs/legacy_names.py; this script reads them from there.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from watchdogs import legacy_names as _legacy
    OLD_MARKS = (_legacy.OLD_INSTALL_MARK,)
    OLD_NAMES = {"install-dir": _legacy.OLD_INSTALL_NAME, "desktop-id": _legacy.OLD_DESKTOP_ID}
except Exception:                      # run from somewhere odd: nothing old is recognised, nothing breaks
    OLD_MARKS, OLD_NAMES = (), {}
BLACKLIST_MODULES = ("dvb_usb_rtl28xxu", "rtl2832", "rtl2830", "rtl2832_sdr", "dvb_usb_rtl2832u",
                     "dvb_usb_v2", "r820t")
TERMINALS = ("lxterminal", "qterminal", "xfce4-terminal", "mate-terminal", "gnome-terminal",
             "x-terminal-emulator", "xterm")


def gpsd_defaults(current: str, devices) -> str:
    """/etc/default/gpsd with gpsd owning ``devices`` and started with -n.

    Lines this installer does not own are kept as they are (comments, other
    settings). DEVICES, GPSD_OPTIONS, START_DAEMON and USBAUTO are set:
      DEVICES       the receivers found on this machine, the UART first
      GPSD_OPTIONS  the existing options plus -n (poll the receivers without
                    waiting for a client, so a fix is ready when the game starts)
      START_DAEMON  true
      USBAUTO       true (a dongle plugged in later is picked up by gpsd)
    """
    devices = [device for device in devices if device and device.startswith("/")]
    kept, options = [], ""
    for line in (current or "").splitlines():
        match = re.match(r"\s*(DEVICES|GPSD_OPTIONS|START_DAEMON|USBAUTO)\s*=\s*(.*?)\s*$", line)
        if not match:
            if line.strip() not in (MARK,) + OLD_MARKS:
                kept.append(line)
            continue
        if match.group(1) == "GPSD_OPTIONS":
            value = match.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            options = value
    words = [word for word in options.split() if word]
    if "-n" not in words:
        words.insert(0, "-n")
    while kept and not kept[-1].strip():
        kept.pop()
    block = [MARK,
             'START_DAEMON="true"',
             'USBAUTO="true"',
             'DEVICES="%s"' % " ".join(devices),
             'GPSD_OPTIONS="%s"' % " ".join(words)]
    return "\n".join(kept + ([""] if kept else []) + block) + "\n"


def desktop_entry(install_dir: str, name: str = DISPLAY_NAME, icon: str = "") -> str:
    """A .desktop file with real absolute paths (no ~, no variables)."""
    install_dir = os.path.abspath(install_dir)
    for value in (install_dir, name, icon):
        if "\n" in value or "\r" in value:
            raise ValueError("a line break in a desktop entry value")
    launcher = os.path.join(install_dir, "wdg-launch.sh")
    quoted = '"%s"' % launcher.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return "\n".join([
        "[Desktop Entry]", "Version=1.0", "Type=Application", "Name=%s" % name,
        "Comment=WDG-Bristle: Watch Dogs Go with Kismet wardriving and Bristle Nodes (installed in %s)" % install_dir,
        "Path=%s" % install_dir, "Exec=%s" % quoted,
        "Icon=%s" % (icon or os.path.join(install_dir, "assets", "icon-%s.png" % DESKTOP_ID)),
        "Terminal=false", "Categories=Game;", "StartupNotify=true", ""])


def launcher_script() -> str:
    """wdg-launch.sh: what the desktop icon runs. upstream's own launcher needs
    lxterminal; this one uses whichever terminal the desktop has, and tells
    the player what went wrong instead of closing."""
    cases = "\n".join(
        '    elif command -v %s >/dev/null 2>&1; then TERM_CMD="%s"' % (name, name) for name in TERMINALS[1:])
    return r"""#!/bin/bash
# """ + MARK + r""" -- written at install time; re-run install.sh after moving the folder
cd "$(dirname "$(readlink -f "$0")")" || exit 1
export PYTHONDONTWRITEBYTECODE=1
LOG_DIR="$HOME/.watchdogs"
mkdir -p "$LOG_DIR" 2>/dev/null || true
INNER='
if command -v meshtasticd >/dev/null 2>&1; then
    sudo -n pkill -9 meshtasticd 2>/dev/null; sudo -n systemctl stop meshtasticd 2>/dev/null
fi
sudo -n find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
if [ ! -x .venv/bin/python3 ]; then
    echo "The game is not set up yet (no .venv). Run:  ./install.sh"
    read -r -p "Press Enter to close..." _; exit 1
fi
sudo -E PYTHONDONTWRITEBYTECODE=1 XDG_RUNTIME_DIR=/run/user/$(id -u) .venv/bin/python3 -m watchdogs 2>>"$HOME/.watchdogs/launcher.log"
CODE=$?
if [ $CODE -ne 0 ]; then
    echo ""
    echo "The game ended with an error (code $CODE). Logs:"
    echo "  ~/.watchdogs/last_run.log   the game log"
    echo "  ~/.watchdogs/launcher.log   what the launcher saw"
    read -r -p "Press Enter to close..." _
fi
'
if   command -v lxterminal >/dev/null 2>&1; then TERM_CMD="lxterminal"
""" + cases + r"""
else TERM_CMD=""
fi
case "$TERM_CMD" in
    lxterminal)        exec lxterminal --title="Watch Dogs Go v3 MAX" --no-remote -e bash -c "$INNER" ;;
    qterminal)         exec qterminal -e bash -c "$INNER" ;;
    xfce4-terminal)    exec xfce4-terminal --title="Watch Dogs Go v3 MAX" -x bash -c "$INNER" ;;
    mate-terminal|gnome-terminal) exec "$TERM_CMD" -- bash -c "$INNER" ;;
    x-terminal-emulator|xterm)    exec "$TERM_CMD" -e bash -c "$INNER" ;;
    *) bash -c "$INNER" ;;
esac
"""


def blacklist_conf() -> str:
    lines = [MARK,
             "# The kernel's DVB-T TV driver grabs the RTL2832U before rtl_test / dump1090 /",
             "# rtl_433 can use it as an SDR. Blacklisting it leaves the dongle to them."]
    return "\n".join(lines + ["blacklist %s" % module for module in BLACKLIST_MODULES]) + "\n"


def devices_from_detect(path: str) -> list:
    with open(path, encoding="utf-8") as fh:
        return [str(item) for item in json.load(fh).get("gpsd_devices", [])]


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 2
    command, rest = args[0], args[1:]
    if command == "gpsd-defaults":
        current = ""
        if rest and rest[0] != "-":
            try:
                with open(rest[0], encoding="utf-8", errors="replace") as fh:
                    current = fh.read()
            except OSError:
                current = ""
        sys.stdout.write(gpsd_defaults(current, rest[1:]))
    elif command == "desktop":
        sys.stdout.write(desktop_entry(rest[0], rest[1] if len(rest) > 1 else DISPLAY_NAME,
                                       rest[2] if len(rest) > 2 else ""))
    elif command == "launcher":
        sys.stdout.write(launcher_script())
    elif command == "blacklist":
        sys.stdout.write(blacklist_conf())
    elif command == "gpsd-devices":
        print(" ".join(devices_from_detect(rest[0])))
    elif command == "old-name":
        print(OLD_NAMES.get(rest[0] if rest else "", ""))
    else:
        print("unknown command: %s" % command)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
