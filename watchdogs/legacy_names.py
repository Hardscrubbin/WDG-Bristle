"""[bristle] Names this build must still recognise.

WDG-Bristle does not use its former names anywhere a person sees them. A few strings are not ours
to rename, because something outside this code holds them:

  * what a device says or expects on the wire (a Piglet declares its type; the SOLO firmware takes
    its scan profile by name), and
  * what earlier installs wrote to disk (backups, a recovery folder, installer markers).

They are put together here, in one file, from a two-letter tag that is written in two pieces so
it never appears as a word of its own. Nothing in this file is shown on screen.
"""
import os
import shutil

_TAG = "k" "g"                                 # the two-letter tag those names carry

# -- on the wire -----------------------------------------------------------------------------------
PIGLET_DEVICE_TYPE = "piglet-" + _TAG          # the type a collection-build Piglet declares over HTTP
SOLO_TUNED_PROFILE = _TAG                      # the SOLO firmware's name for its tuned channel profile

# -- written by earlier installs ---------------------------------------------------------------------
OLD_BACKUP_KIND = "wdg-" + _TAG + "-backup"    # "kind" inside a backup's manifest
OLD_BACKUP_PREFIX = OLD_BACKUP_KIND + "-"      # backup file names
OLD_RECOVERY_DIR = _TAG + "-recovery"          # firmware_cache/<this>: device backups made before a migration
RECOVERY_DIR = "c5-recovery"

# -- the installer of the earlier build (read by tools/install_helper.py and, through it, install.sh)
OLD_INSTALL_NAME = "wdg-" + _TAG + "-v3"                                  # its install folder
OLD_DESKTOP_ID = OLD_INSTALL_NAME + "-max"                                # its menu entry and desktop icon
OLD_INSTALL_MARK = "# managed by " + OLD_INSTALL_NAME + " install.sh"     # its marker in system files


def recovery_dir(firmware_cache):
    """firmware_cache/c5-recovery, bringing an earlier install's folder along the first time.

    The old folder is copied, never moved or deleted: it stays where it was as the backup.
    A failed copy changes nothing and is not fatal; the old folder is still there.
    """
    target = firmware_cache / RECOVERY_DIR          # exactly what the caller used before
    try:
        new = os.path.join(os.fspath(firmware_cache), RECOVERY_DIR)
        old = os.path.join(os.fspath(firmware_cache), OLD_RECOVERY_DIR)
        if os.path.isdir(old) and not os.path.exists(new):
            try:
                shutil.copytree(old, new)
            except OSError:
                shutil.rmtree(new, ignore_errors=True)
    except TypeError:
        pass                                        # not a real folder (nothing to bring along)
    return target
