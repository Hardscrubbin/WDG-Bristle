#!/bin/bash
# =============================================================================
# WDG-Bristle : installer
#
# Unzip wdg-bristle.zip anywhere, then from inside the "wdg-bristle" folder:
#
#     ./install.sh
#
# Run it as your NORMAL user. It calls sudo itself where root is needed.
# The game is installed IN PLACE: this folder is the install.
#
#   ./install.sh                  install / update
#   ./install.sh --clean          also WIPE loot/, maps/, secrets.conf and
#                                 whitelist.json first (asks you to type DELETE)
#   ./install.sh --no-apt         do not install system packages
#   ./install.sh --no-gpsd        do not touch /etc/default/gpsd
#   ./install.sh --no-rails       do not switch AIO rails on while looking for hardware
#   ./install.sh --passwordless-launch
#                                 let the desktop icon start the game without
#                                 asking for the sudo password (adds one sudoers
#                                 rule for this folder's Python only)
#
# What it keeps: loot/, maps/, secrets.conf, whitelist.json are never touched
# unless you pass --clean. Code is overwritten freely.
# What it changes outside this folder (each one is said out loud when it happens):
#   apt packages; /etc/default/gpsd (backed up first); a modprobe blacklist for
#   the RTL-SDR's TV driver; your user's groups; the menu + desktop entry.
# =============================================================================
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { printf '  %b[ OK ]%b %s\n' "$GREEN" "$NC" "$1"; }
warn() { printf '  %b[ !! ]%b %s\n' "$YELLOW" "$NC" "$1"; WARNINGS=$((WARNINGS + 1)); }
fail() { printf '  %b[FAIL]%b %s\n' "$RED" "$NC" "$1"; ERRORS=$((ERRORS + 1)); }
info() { printf '  %b[ .. ]%b %s\n' "$CYAN" "$NC" "$1"; }
step() { printf '\n%b== %s%b\n' "$CYAN" "$1" "$NC"; }

ERRORS=0
WARNINGS=0
CLEAN=0
DO_APT=1
DO_GPSD=1
RAIL_FLAG=""
PASSWORDLESS=0
DESKTOP_ID="wdg-bristle"
DISPLAY_NAME="WDG-Bristle"

for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=1 ;;
        --no-apt) DO_APT=0 ;;
        --no-gpsd) DO_GPSD=0 ;;
        --no-rails) RAIL_FLAG="--no-rails" ;;
        --passwordless-launch) PASSWORDLESS=1 ;;
        -h|--help) sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg   (try ./install.sh --help)"; exit 2 ;;
    esac
done

echo ""
echo "========================================================================"
echo "  $DISPLAY_NAME  -  installer"
echo "  Install folder: $DIR"
echo "========================================================================"

if [ "$(id -u)" -eq 0 ]; then
    fail "Run this as your normal user, not with sudo. It uses sudo itself where needed."
    exit 1
fi
if [ ! -f "$DIR/watchdogs/__main__.py" ] || [ ! -f "$DIR/tools/hwdetect.py" ] || [ ! -f "$DIR/setup.sh" ]; then
    fail "This does not look like the unzipped wdg-bristle folder (run install.sh from inside it)."
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 is missing. Install it first:  sudo apt install python3"
    exit 1
fi
USER_NAME="$(id -un)"
# [bristle] The earlier build's folder and desktop id (kept in watchdogs/legacy_names.py). Used only
# to bring your data over from an old install and to remove its icon. Empty if they can't be read:
# then nothing old is looked for.
OLD_INSTALL_NAME="$(python3 "$DIR/tools/install_helper.py" old-name install-dir 2>/dev/null || true)"
OLD_DESKTOP_ID="$(python3 "$DIR/tools/install_helper.py" old-name desktop-id 2>/dev/null || true)"
info "sudo is needed for packages, gpsd and hardware detection. You may be asked for your password."
if ! sudo -v; then
    fail "sudo is not available for $USER_NAME."
    exit 1
fi

# -----------------------------------------------------------------------------
step "1/9  Hardware on this machine"
# -----------------------------------------------------------------------------
DETECT_JSON="$(mktemp /tmp/wdg-detect-XXXXXX.json)"
if sudo python3 "$DIR/tools/hwdetect.py" --json "$DETECT_JSON" $RAIL_FLAG; then
    sudo chown "$USER_NAME" "$DETECT_JSON" 2>/dev/null || true
else
    warn "The hardware look did not finish. The install continues; gpsd will not be configured."
    DO_GPSD=0
fi
BOARD="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["board"]["kind"])' "$DETECT_JSON" 2>/dev/null || echo unknown)"
case "$BOARD" in
    CM5) ok "Board: Compute Module 5 (CM5)" ;;
    CM4) ok "Board: Compute Module 4 (CM4)" ;;
    *)   warn "Board: $BOARD - not a CM4/CM5. The game installs, AIO features may not apply." ;;
esac

# -----------------------------------------------------------------------------
step "2/9  Your data"
# -----------------------------------------------------------------------------
if [ "$CLEAN" -eq 1 ]; then
    echo "  --clean WIPES this install's player data:"
    for item in loot maps secrets.conf whitelist.json; do
        if [ -e "$DIR/$item" ]; then echo "      $DIR/$item"; fi
    done
    printf "  This cannot be undone. Type DELETE to wipe them, anything else to keep them: "
    read -r ANSWER
    if [ "$ANSWER" = "DELETE" ]; then
        sudo rm -rf -- "$DIR/loot" "$DIR/maps"
        sudo rm -f -- "$DIR/secrets.conf" "$DIR/whitelist.json"
        ok "Player data wiped."
    else
        ok "Nothing wiped."
    fi
else
    # [bristle] First install of WDG-Bristle next to an install under the old name: COPY your data
    # over. Nothing is moved or deleted; the old folder stays exactly as it is, as the backup.
    if [ -n "$OLD_INSTALL_NAME" ] && [ ! -e "$DIR/loot" ] && [ ! -e "$DIR/secrets.conf" ]; then
        OLD_DIR=""
        for candidate in "$(dirname "$DIR")/$OLD_INSTALL_NAME" "$HOME/$OLD_INSTALL_NAME"; do
            if [ "$candidate" != "$DIR" ] && { [ -d "$candidate/loot" ] || [ -f "$candidate/secrets.conf" ]; }; then
                OLD_DIR="$candidate"; break
            fi
        done
        if [ -n "$OLD_DIR" ]; then
            info "Found your earlier install: $OLD_DIR"
            COPY_FAILED=0
            # loot, maps, keys, the whitelist, device backups, the Kismet adapter choice and the
            # record of what has already been uploaded
            for item in loot maps secrets.conf whitelist.json firmware_cache kismet_adapters.json \
                        plugins/.wardrive_state.json plugins/.wardrive_uploaded.json; do
                if [ -e "$OLD_DIR/$item" ] && [ ! -e "$DIR/$item" ]; then
                    if sudo cp -a -- "$OLD_DIR/$item" "$DIR/$item"; then
                        ok "copied over: $item"
                    else
                        fail "could not copy $item from $OLD_DIR (it is untouched there)"
                        COPY_FAILED=1
                    fi
                fi
            done
            if [ "$COPY_FAILED" -eq 0 ]; then
                ok "Your loot, keys and settings are here now. $OLD_DIR was not changed: keep it as the backup."
            fi
        fi
    fi
    for item in loot maps secrets.conf whitelist.json; do
        if [ -e "$DIR/$item" ]; then ok "kept: $item"; fi
    done
    if [ ! -e "$DIR/loot" ]; then info "no loot/ yet: this is a new player (see INSTALL_WDG_BRISTLE.md to bring older data over)"; fi
fi

# -----------------------------------------------------------------------------
step "3/9  System packages"
# -----------------------------------------------------------------------------
if [ "$DO_APT" -eq 1 ] && command -v apt-get >/dev/null 2>&1; then
    WANTED="kismet gpsd gpsd-clients rtl-sdr rtl-433 python3-venv python3-dev git unzip usbutils iw psmisc network-manager bluez"
    if [ -f /sys/firmware/devicetree/base/model ] && grep -qi 'raspberry\|clockwork' /sys/firmware/devicetree/base/model 2>/dev/null; then
        WANTED="$WANTED raspi-utils"
    fi
    MISSING=""
    for pkg in $WANTED; do
        if dpkg -s "$pkg" >/dev/null 2>&1; then :; else MISSING="$MISSING $pkg"; fi
    done
    if [ -z "$MISSING" ]; then
        ok "All packages already installed."
    else
        info "Installing:$MISSING"
        sudo apt-get update >/tmp/wdg-apt.log 2>&1 || warn "apt-get update reported errors (see /tmp/wdg-apt.log)"
        # one at a time, so one unavailable package (kismet on some images) does not block the rest
        for pkg in $MISSING; do
            if sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$pkg" >>/tmp/wdg-apt.log 2>&1; then
                ok "installed $pkg"
            else
                fail "could not install $pkg (see /tmp/wdg-apt.log)"
            fi
        done
    fi
    if ! command -v kismet >/dev/null 2>&1; then
        warn "kismet is not installed. Kismet wardriving will not work until it is (kismetwireless.net has packages)."
    fi
else
    info "Skipped (--no-apt, or this system has no apt-get)."
fi

# -----------------------------------------------------------------------------
step "4/9  Game environment (upstream's setup.sh: venv, Python packages, dump1090, AIO tools)"
# -----------------------------------------------------------------------------
chmod +x "$DIR/setup.sh" "$DIR/run.sh" "$DIR/watchdogs-launcher" "$DIR/selftest.sh" "$DIR/uninstall.sh" 2>/dev/null || true
if bash "$DIR/setup.sh"; then
    ok "setup.sh finished."
else
    fail "setup.sh reported errors (shown above). Fix them and run ./install.sh again."
fi
if [ -x "$DIR/.venv/bin/python3" ] && "$DIR/.venv/bin/python3" -c "import pyxel, serial, requests, PIL" 2>/dev/null; then
    ok "The game's Python environment works (pyxel, pyserial, requests, Pillow)."
else
    fail "The game's Python environment is incomplete. The game will not start until setup.sh succeeds."
fi

# -----------------------------------------------------------------------------
step "5/9  Permissions"
# -----------------------------------------------------------------------------
ADDED=""
for group in dialout kismet plugdev bluetooth gpio spi i2c netdev; do
    if getent group "$group" >/dev/null 2>&1; then
        if id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx "$group"; then :; else
            if sudo usermod -aG "$group" "$USER_NAME"; then ADDED="$ADDED $group"; fi
        fi
    fi
done
if [ -n "$ADDED" ]; then
    ok "Added $USER_NAME to:$ADDED  (log out and in again for it to apply)"
else
    ok "Group memberships already in place."
fi
if command -v pinctrl >/dev/null 2>&1; then
    ok "pinctrl present: the game can switch the AIO v2 rails (GPIO 27 / 16 / 7 / 23)."
else
    warn "pinctrl is missing (package raspi-utils): the AIO v2 rails cannot be switched."
fi
SUDOERS_FILE="/etc/sudoers.d/$DESKTOP_ID"
if [ "$PASSWORDLESS" -eq 1 ]; then
    RULE="$USER_NAME ALL=(root) NOPASSWD:SETENV: $DIR/.venv/bin/python3 -m watchdogs"
    TMP_RULE="$(mktemp)"
    printf '# managed by wdg-bristle install.sh\n%s\n' "$RULE" > "$TMP_RULE"
    if sudo visudo -cf "$TMP_RULE" >/dev/null 2>&1; then
        sudo install -m 440 -o root -g root "$TMP_RULE" "$SUDOERS_FILE"
        ok "The desktop icon can start the game without a password (rule in $SUDOERS_FILE)."
    else
        fail "The sudoers rule did not validate; nothing was installed."
    fi
    rm -f "$TMP_RULE"
elif sudo -n true 2>/dev/null; then
    ok "sudo works without a password here: the desktop icon starts the game directly."
else
    info "The game runs as root. Its window will ask for your sudo password at each start."
    info "To avoid that:  ./install.sh --passwordless-launch"
fi

# -----------------------------------------------------------------------------
step "6/9  gpsd (it owns the GPS receivers; the game reads gpsd)"
# -----------------------------------------------------------------------------
if [ "$DO_GPSD" -eq 1 ] && command -v gpsd >/dev/null 2>&1; then
    GPS_DEVICES="$(python3 "$DIR/tools/install_helper.py" gpsd-devices "$DETECT_JSON" 2>/dev/null || true)"
    if [ -z "$GPS_DEVICES" ]; then
        warn "No GPS receiver produced NMEA, so /etc/default/gpsd was left as it is."
        info "Plug in the dongle / check the GP-02, then run ./install.sh again."
    else
        NEW_DEFAULTS="$(mktemp)"
        # shellcheck disable=SC2086
        python3 "$DIR/tools/install_helper.py" gpsd-defaults /etc/default/gpsd $GPS_DEVICES > "$NEW_DEFAULTS"
        if [ -f /etc/default/gpsd ] && cmp -s "$NEW_DEFAULTS" /etc/default/gpsd; then
            ok "/etc/default/gpsd already lists: $GPS_DEVICES"
        else
            if [ -f /etc/default/gpsd ]; then
                BACKUP="/etc/default/gpsd.wdg-backup-$(date +%Y%m%d-%H%M%S)"
                sudo cp -p /etc/default/gpsd "$BACKUP" && ok "Backup of the old gpsd settings: $BACKUP"
            fi
            if sudo install -m 644 "$NEW_DEFAULTS" /etc/default/gpsd; then
                ok "gpsd now owns: $GPS_DEVICES  (started with -n)"
            else
                fail "Could not write /etc/default/gpsd."
            fi
        fi
        rm -f "$NEW_DEFAULTS"
        sudo systemctl enable gpsd.socket gpsd.service >/dev/null 2>&1 || true
        sudo systemctl restart gpsd.socket >/dev/null 2>&1 || true
        sudo systemctl restart gpsd.service >/dev/null 2>&1 || warn "gpsd did not restart (sudo systemctl status gpsd)"
        if command -v gpspipe >/dev/null 2>&1 && timeout 10 gpspipe -w -n 4 2>/dev/null | grep -q '"class":"DEVICES"\|"class":"VERSION"'; then
            ok "gpsd answers on port 2947."
        else
            warn "gpsd did not answer within 10 s. Check:  sudo systemctl status gpsd ; gpsmon"
        fi
        info "The GP-02 only sends data while its rail (GPIO 27) is on. The game switches it on at start."
    fi
else
    info "Skipped (--no-gpsd, the hardware look failed, or gpsd is not installed)."
fi

# -----------------------------------------------------------------------------
step "7/9  RTL-SDR driver"
# -----------------------------------------------------------------------------
if grep -rqs "blacklist dvb_usb_rtl28xxu" /etc/modprobe.d /lib/modprobe.d /usr/lib/modprobe.d 2>/dev/null; then
    ok "The DVB-T TV driver is already blacklisted (the RTL-SDR is free for SDR programs)."
else
    BLACKLIST_FILE="/etc/modprobe.d/wdg-rtlsdr-blacklist.conf"
    TMP_BL="$(mktemp)"
    python3 "$DIR/tools/install_helper.py" blacklist > "$TMP_BL"
    if sudo install -m 644 "$TMP_BL" "$BLACKLIST_FILE"; then
        ok "Wrote $BLACKLIST_FILE (keeps the TV driver off the RTL-SDR)."
        sudo modprobe -r dvb_usb_rtl28xxu >/dev/null 2>&1 || true
    else
        fail "Could not write $BLACKLIST_FILE."
    fi
    rm -f "$TMP_BL"
fi

# -----------------------------------------------------------------------------
step "8/9  Settings, launcher and icon"
# -----------------------------------------------------------------------------
mkdir -p "$DIR/loot" "$DIR/maps" 2>/dev/null || sudo mkdir -p "$DIR/loot" "$DIR/maps"
if [ -f "$DIR/secrets.conf" ]; then
    ok "secrets.conf kept (your keys were not touched)."
elif [ -f "$DIR/secrets.conf.example" ]; then
    cp "$DIR/secrets.conf.example" "$DIR/secrets.conf" && chmod 600 "$DIR/secrets.conf"
    ok "secrets.conf created from the example. It has no keys yet (see next steps)."
fi
python3 "$DIR/tools/install_helper.py" launcher > "$DIR/wdg-launch.sh" && chmod +x "$DIR/wdg-launch.sh"
APPS="$HOME/.local/share/applications"
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
mkdir -p "$APPS" "$DESK"
# [bristle] one icon, not two: remove the menu entry and desktop icon of the old name (only those).
for old_entry in "$APPS/$OLD_DESKTOP_ID.desktop" "$DESK/$OLD_DESKTOP_ID.desktop"; do
    if [ -n "$OLD_DESKTOP_ID" ] && [ -f "$old_entry" ]; then
        rm -f -- "$old_entry" && ok "removed the old icon: $old_entry"
    fi
done
ICON="$DIR/assets/icon-$DESKTOP_ID.png"
ENTRY="$APPS/$DESKTOP_ID.desktop"
if python3 "$DIR/tools/install_helper.py" desktop "$DIR" "$DISPLAY_NAME" "$ICON" > "$ENTRY"; then
    chmod +x "$ENTRY"
    cp -f "$ENTRY" "$DESK/$DESKTOP_ID.desktop"
    chmod +x "$DESK/$DESKTOP_ID.desktop"
    # mark the desktop copy trusted so a click launches it (GNOME / Cinnamon / MATE, then Xfce)
    gio set "$DESK/$DESKTOP_ID.desktop" metadata::trusted true >/dev/null 2>&1 || true
    if command -v sha256sum >/dev/null 2>&1; then
        gio set "$DESK/$DESKTOP_ID.desktop" metadata::xfce-exe-checksum \
            "$(sha256sum "$DESK/$DESKTOP_ID.desktop" | cut -d' ' -f1)" >/dev/null 2>&1 || true
    fi
    update-desktop-database "$APPS" >/dev/null 2>&1 || true
    ok "Menu entry and desktop icon: \"$DISPLAY_NAME\""
    ok "They start: $DIR/wdg-launch.sh"
else
    fail "Could not write the desktop entry."
fi
if [ ! -f "$ICON" ]; then warn "The icon file is missing: $ICON (the entry works, without a picture)"; fi

# -----------------------------------------------------------------------------
step "9/9  Check"
# -----------------------------------------------------------------------------
if [ -x "$DIR/.venv/bin/python3" ]; then
    if [ ! -f "$DIR/tools/sim/run.py" ]; then    # [bristle] a build without the simulation scenarios
        ok "The self-test is ready (test suite). Run it with ./selftest.sh"
    else
    SCENARIOS="$("$DIR/.venv/bin/python3" -B "$DIR/tools/sim/run.py" --list 2>/dev/null | wc -l)"
    if [ "${SCENARIOS:-0}" -gt 50 ]; then
        ok "The self-test is ready ($SCENARIOS scenarios). Run it with ./selftest.sh"
    else
        warn "The self-test did not list its scenarios; ./selftest.sh will say why."
    fi
    fi
fi
rm -f "$DETECT_JSON" 2>/dev/null || true

echo ""
echo "========================================================================"
if [ "$ERRORS" -eq 0 ]; then
    printf '  %bInstalled.%b  %s warning(s).\n' "$GREEN" "$NC" "$WARNINGS"
else
    printf '  %bFinished with %s error(s)%b and %s warning(s). See the [FAIL] lines above.\n' "$RED" "$ERRORS" "$NC" "$WARNINGS"
fi
echo "========================================================================"
echo ""
echo "  NEXT STEPS"
echo "   1. Keys:      nano \"$DIR/secrets.conf\"      (WiGLE, WDGWars, map tiles - see INSTALL_WDG_BRISTLE.md)"
echo "   2. Kismet:    if you never set its login, run 'kismet' once, open http://localhost:2501,"
echo "                 set a user and password, then stop it with Ctrl+C."
echo "   3. Self-test: ./selftest.sh                 (about 10 minutes; prints pass/fail per feature)"
echo "   4. Play:      click \"$DISPLAY_NAME\" on the desktop or in the menu,"
echo "                 or run  \"$DIR/run.sh\""
echo "   5. In game:   PLUGINS > Hardware Check, then PLUGINS > GPS: Health."
if [ -n "$ADDED" ]; then
    echo "   !  Log out and back in once (new groups:$ADDED)."
fi
echo "   Uninstall:    ./uninstall.sh   (keeps your loot unless you pass --purge)"
echo ""
if [ "$ERRORS" -eq 0 ]; then exit 0; else exit 1; fi
