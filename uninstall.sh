#!/bin/bash
# =============================================================================
# Watch Dogs Go - WDG-Bristle : uninstaller
#
#     ./uninstall.sh             remove the desktop icon and the menu entry
#     ./uninstall.sh --venv      also remove the Python environment (.venv)
#     ./uninstall.sh --purge     also DELETE loot/, maps/, secrets.conf and
#                                whitelist.json (asks you to type DELETE)
#
# Without --purge your loot, XP, maps and keys stay exactly where they are.
# System settings other builds also use are left alone and listed at the end
# (gpsd settings, the RTL-SDR blacklist, apt packages, your groups).
# =============================================================================
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1
DESKTOP_ID="wdg-bristle"
REMOVE_VENV=0
PURGE=0
for arg in "$@"; do
    case "$arg" in
        --venv) REMOVE_VENV=1 ;;
        --purge) PURGE=1 ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg   (try ./uninstall.sh --help)"; exit 2 ;;
    esac
done
if [ "$(id -u)" -eq 0 ]; then
    echo "Run ./uninstall.sh as your normal user, not with sudo."
    exit 1
fi
if [ ! -f "$DIR/watchdogs/__main__.py" ]; then
    echo "This does not look like the game folder. Nothing was removed."
    exit 1
fi
echo ""
echo "Uninstalling from: $DIR"

APPS="$HOME/.local/share/applications"
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
for entry in "$APPS/$DESKTOP_ID.desktop" "$DESK/$DESKTOP_ID.desktop"; do
    if [ -f "$entry" ]; then
        rm -f -- "$entry" && echo "  removed $entry"
    fi
done
update-desktop-database "$APPS" >/dev/null 2>&1 || true
rm -f -- "$DIR/wdg-launch.sh"

SUDOERS_FILE="/etc/sudoers.d/$DESKTOP_ID"
if sudo -n test -f "$SUDOERS_FILE" 2>/dev/null || [ -f "$SUDOERS_FILE" ]; then
    echo "  removing the passwordless-launch rule $SUDOERS_FILE (sudo)"
    sudo rm -f -- "$SUDOERS_FILE"
fi

if [ "$REMOVE_VENV" -eq 1 ] && [ -d "$DIR/.venv" ]; then
    rm -rf -- "$DIR/.venv" 2>/dev/null || sudo rm -rf -- "$DIR/.venv"
    echo "  removed .venv"
fi

if [ "$PURGE" -eq 1 ]; then
    echo ""
    echo "  --purge DELETES this install's player data:"
    for item in loot maps secrets.conf whitelist.json; do
        if [ -e "$DIR/$item" ]; then echo "      $DIR/$item"; fi
    done
    ls -d "$DIR"/loot.pre-restore-* "$DIR"/secrets.conf.pre-restore-* "$DIR"/whitelist.json.pre-restore-* 2>/dev/null | sed 's/^/      /'
    printf "  This cannot be undone. Type DELETE to delete them, anything else to keep them: "
    read -r ANSWER
    if [ "$ANSWER" = "DELETE" ]; then
        sudo rm -rf -- "$DIR/loot" "$DIR/maps" "$DIR"/loot.pre-restore-*
        sudo rm -f -- "$DIR/secrets.conf" "$DIR/whitelist.json" "$DIR"/secrets.conf.pre-restore-* "$DIR"/whitelist.json.pre-restore-*
        echo "  player data deleted"
    else
        echo "  nothing deleted"
    fi
else
    echo "  kept: loot/, maps/, secrets.conf, whitelist.json (use --purge to delete them)"
fi

echo ""
echo "Left in place (other builds use them too):"
echo "  - gpsd settings      /etc/default/gpsd   (old versions: ls /etc/default/gpsd.wdg-backup-*)"
echo "                       to go back:  sudo cp /etc/default/gpsd.wdg-backup-<date> /etc/default/gpsd"
echo "                                    sudo systemctl restart gpsd.socket gpsd"
echo "  - RTL-SDR blacklist  /etc/modprobe.d/wdg-rtlsdr-blacklist.conf   (sudo rm it to undo)"
echo "  - apt packages, your group memberships, ~/.watchdogs (shared settings and logs)"
echo "  - the game folder itself: delete it by hand when you are sure:  rm -rf \"$DIR\""
echo ""
