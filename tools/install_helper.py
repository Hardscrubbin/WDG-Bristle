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
    python3 tools/install_helper.py bring-data <install dir> <home folder> [--from=FOLDER]
                                    (first install: find earlier installs, copy the player's data)
"""
import json
import os
import re
import shutil
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
    OLD_INSTALL_NAMES = tuple(_legacy.OLD_INSTALL_NAMES)
except Exception:                      # run from somewhere odd: nothing old is recognised, nothing breaks
    OLD_MARKS, OLD_NAMES, OLD_INSTALL_NAMES = (), {}, ()
# [bristle] The player's data, as an earlier install kept it in its folder: what a first install
# copies over (loot, maps, keys, the whitelist, device backups, the Kismet adapter choice and the
# record of what has already been uploaded).
DATA_ITEMS = ("loot", "maps", "secrets.conf", "whitelist.json", "firmware_cache", "kismet_adapters.json",
              "plugins/.wardrive_state.json", "plugins/.wardrive_uploaded.json")
SEARCH_DEPTH = 4                       # how many folders deep under the home folder to look
SEARCH_LIMIT = 20000                   # folders looked at before the search gives up
SEARCH_SKIP = {"node_modules", "__pycache__", "venv", "site-packages", "snap", "proc", "sys", "dev",
               "loot", "maps", "firmware_cache", "lost+found"}
COPY_RECORD = "copied-from-earlier-install.txt"
PARTIAL = ".copying"
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
    """wdg-launch.sh: what the desktop icon runs. The game's own launcher needs
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
    lxterminal)        exec lxterminal --title="WDG-Bristle" --no-remote -e bash -c "$INNER" ;;
    qterminal)         exec qterminal -e bash -c "$INNER" ;;
    xfce4-terminal)    exec xfce4-terminal --title="WDG-Bristle" -x bash -c "$INNER" ;;
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


# ---------------------------------------------------------------------------
# [bristle] First install: find earlier installs under the home folder and copy the player's data
# ---------------------------------------------------------------------------
def is_old_name(name: str, names=None) -> bool:
    """True for a folder name an earlier install used: the name itself, any letter case, or the
    name with something added after it ("<name>-old", "<name> (1)", "<name>.bak")."""
    names = OLD_INSTALL_NAMES if names is None else names
    low = name.lower()
    for old in names:
        old = old.lower()
        if low == old or (low.startswith(old) and not low[len(old)].isalnum()):
            return True
    return False


def data_items(folder: str) -> list:
    """Which of DATA_ITEMS this folder holds."""
    return [item for item in DATA_ITEMS if os.path.lexists(os.path.join(folder, *item.split("/")))]


def has_data(folder: str) -> bool:
    """An install with a player's data in it: a loot folder or a secrets.conf."""
    return os.path.isdir(os.path.join(folder, "loot")) or os.path.isfile(os.path.join(folder, "secrets.conf"))


def newest_data(folder: str) -> float:
    """When the player's data in this folder last changed (the newest of secrets.conf, loot/ and
    what is directly inside loot/). 0 if nothing can be read."""
    newest = 0.0
    loot = os.path.join(folder, "loot")
    paths = [os.path.join(folder, "secrets.conf"), loot]
    try:
        paths += [os.path.join(loot, name) for name in os.listdir(loot)]
    except OSError:
        pass
    for path in paths:
        try:
            newest = max(newest, os.lstat(path).st_mtime)
        except OSError:
            pass
    return newest


def find_old_installs(home: str, here: str, names=None, depth: int = SEARCH_DEPTH, limit: int = SEARCH_LIMIT) -> tuple:
    """(installs, complete): every folder under ``home`` (at most ``depth`` folders deep) whose
    name is an earlier install's and that holds a player's data, and the folders with such a name
    next to this install. Newest data first. Nothing is opened for writing; links are not followed.
    ``complete`` is False when the search stopped at ``limit`` folders."""
    names = OLD_INSTALL_NAMES if names is None else names
    here = os.path.realpath(here)
    found, seen, looked, complete = [], set(), 0, True

    def consider(path):
        real = os.path.realpath(path)
        if real == here or real in seen or not os.path.isdir(real) or os.path.islink(path):
            return
        if is_old_name(os.path.basename(path), names) and has_data(real):
            seen.add(real)
            found.append({"path": path, "newest": newest_data(real), "items": data_items(real)})

    if names:
        parent = os.path.dirname(here)
        try:
            for name in sorted(os.listdir(parent)):
                consider(os.path.join(parent, name))
        except OSError:
            pass
        level = [home] if home and os.path.isdir(home) else []
        for _deep in range(depth):
            following = []
            for folder in level:
                try:
                    entries = sorted(os.listdir(folder))
                except OSError:
                    continue
                for name in entries:
                    path = os.path.join(folder, name)
                    if name.startswith(".") or name in SEARCH_SKIP or os.path.islink(path) or not os.path.isdir(path):
                        continue
                    looked += 1
                    if looked > limit:
                        complete = False
                        break
                    if is_old_name(name, names):
                        consider(path)          # an install: not searched inside
                    elif os.path.realpath(path) != here:
                        following.append(path)
                if not complete:
                    break
            if not complete:
                break
            level = following
    found.sort(key=lambda entry: -entry["newest"])
    return found, complete


def _own_like(source: str, target: str) -> None:
    """Give the copy the owner of the original where this process may (it runs under sudo on the
    uConsole, as the copy did before). Not being allowed to is not an error."""
    try:
        info = os.lstat(source)
        os.chown(target, info.st_uid, info.st_gid, follow_symlinks=False)
    except (AttributeError, NotImplementedError, OSError):
        pass


def _copy(source: str, target: str) -> tuple:
    """Copy a file, a link or a whole folder to a path that does not exist yet. The original is only
    read. Returns (files, bytes)."""
    if os.path.islink(source):
        os.symlink(os.readlink(source), target)
        _own_like(source, target)
        return 1, 0
    if os.path.isdir(source):
        os.mkdir(target)
        files = size = 0
        for name in sorted(os.listdir(source)):
            more = _copy(os.path.join(source, name), os.path.join(target, name))
            files, size = files + more[0], size + more[1]
        shutil.copystat(source, target)
        _own_like(source, target)
        return files, size
    shutil.copy2(source, target)
    _own_like(source, target)
    return 1, os.path.getsize(target)


def copy_data(old: str, new: str) -> list:
    """Copy every data item ``old`` has and ``new`` does not: [(item, status, detail)], status one
    of "copied", "kept" (already in the new folder: never overwritten), "absent", "failed".

    Each item is copied under a temporary name next to its place and renamed when it is whole, so
    an install that is interrupted leaves no half-copied loot behind; the next run starts that
    item again. Nothing in ``old`` is moved, changed or deleted."""
    rows = []
    for item in DATA_ITEMS:
        source = os.path.join(old, *item.split("/"))
        target = os.path.join(new, *item.split("/"))
        if not os.path.lexists(source):
            rows.append((item, "absent", ""))
            continue
        if os.path.lexists(target):
            rows.append((item, "kept", "already in this folder, left as it is"))
            continue
        partial = target + PARTIAL
        try:
            if os.path.lexists(partial):            # ours, from a run that was cut off
                shutil.rmtree(partial) if os.path.isdir(partial) and not os.path.islink(partial) else os.remove(partial)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            files, size = _copy(source, partial)
            os.replace(partial, target)
            rows.append((item, "copied", "%d file%s, %.1f MB" % (files, "" if files == 1 else "s", size / 1e6)))
        except (OSError, shutil.Error) as exc:
            rows.append((item, "failed", "%s: %s" % (type(exc).__name__, exc)))
    return rows


def bring_data(install_dir: str, home: str, chosen: str = "", out=None, names=None) -> int:
    """The installer's "bring your data over" step. Says what it found and what it copied, writes
    the same lines to COPY_RECORD in the install folder, and returns 0, or 1 if a copy failed."""
    import time as _time
    lines = []

    def say(tag, text):
        line = "  [%s] %s" % (tag, text)
        lines.append(line)
        (out or print)(line)

    def stamp(when):
        return _time.strftime("%Y-%m-%d %H:%M", _time.localtime(when)) if when else "unknown"

    install_dir = os.path.abspath(install_dir)
    if os.path.lexists(os.path.join(install_dir, "loot")) or os.path.lexists(os.path.join(install_dir, "secrets.conf")):
        say(" .. ", "This folder already has player data: nothing is looked for and nothing is copied.")
        return 0
    found, complete = find_old_installs(home, install_dir, names)
    if not complete:
        say(" !! ", "The search under %s was stopped after %d folders: an earlier install deeper down may "
                    "have been missed (name it with --from=FOLDER)." % (home, SEARCH_LIMIT))
    source = None
    if chosen:
        chosen = os.path.abspath(chosen)
        if os.path.realpath(chosen) == os.path.realpath(install_dir) or not has_data(chosen):
            say("FAIL", "--from=%s: no loot/ and no secrets.conf there. Nothing was copied." % chosen)
            return 1
        source = {"path": chosen, "newest": newest_data(chosen), "items": data_items(chosen)}
    if not found and source is None:
        say(" .. ", "No earlier install found under %s (folders named %s)." % (
            home, ", ".join(names if names is not None else OLD_INSTALL_NAMES) or "-"))
        return 0
    if found:
        say(" .. ", "Earlier install%s found: %d" % ("" if len(found) == 1 else "s", len(found)))
        for entry in found:
            say(" .. ", "    %s   newest data %s   has: %s" % (entry["path"], stamp(entry["newest"]),
                                                               ", ".join(entry["items"])))
    if source is None:
        source = found[0]
        why = "the only one" if len(found) == 1 else "the one with the newest data"
    else:
        why = "named with --from"
    say(" .. ", "Copying from %s (%s). Nothing in that folder is changed." % (source["path"], why))
    failed = copied = 0
    for item, status, detail in copy_data(source["path"], install_dir):
        if status == "copied":
            copied += 1
            say(" OK ", "copied: %s (%s)" % (item, detail))
        elif status == "kept":
            say(" .. ", "not copied: %s (%s)" % (item, detail))
        elif status == "failed":
            failed += 1
            say("FAIL", "could not copy %s (%s). It is untouched in %s." % (item, detail, source["path"]))
    if failed:
        say("FAIL", "%d item(s) could not be copied. Run ./install.sh again to try them again." % failed)
    else:
        say(" OK ", "%d item(s) copied. %s was not changed: keep it as the backup." % (copied, source["path"]))
    others = [entry["path"] for entry in found if os.path.realpath(entry["path"]) != os.path.realpath(source["path"])]
    for path in others:
        say(" .. ", "Not used: %s. Its data is not merged in. To start from it instead, unzip a fresh "
                    "folder and run ./install.sh --from=%s" % (path, path))
    try:
        with open(os.path.join(install_dir, COPY_RECORD), "a", encoding="utf-8") as handle:
            handle.write("%s  install.sh, bringing data over from an earlier install\n" % stamp(_time.time()))
            handle.write("\n".join(lines) + "\n\n")
        _own_like(install_dir, os.path.join(install_dir, COPY_RECORD))
    except OSError:
        pass
    return 1 if failed else 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 2
    command, rest = args[0], args[1:]
    if command == "bring-data":
        chosen = ""
        for word in list(rest):
            if word.startswith("--from="):
                chosen = word[len("--from="):]
                rest.remove(word)
        if len(rest) != 2:
            print("bring-data needs: <install dir> <home folder> [--from=FOLDER]")
            return 2
        return bring_data(rest[0], rest[1], chosen)
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
