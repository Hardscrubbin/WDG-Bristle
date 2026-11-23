"""[merge-v3] Identity of this merged build, and the rules that stop upstream's
in-game updater from replacing it.

This tree is upstream edition 0.9.21.3 + Hardscrubbin's Kismet add-ons (v2) + selected
WDGoNG features. ``watchdogs.__version__`` stays the plain numeric upstream version
("0.9.21.3") because upstream's updater, release tooling and upload User-Agent all
require that exact form; the build suffix lives here and is only added for
display ("0.9.21.3+hardscrubbin-v3").

Why the install is blocked: SYSTEM > Update Game installs upstream's released file
tree over the installation. That tree has no Kismet plugin, no gpsd/MapTiler
support and none of the [merge-v3] edits, so an install would silently remove
them. The "new version available" notice is kept; only the install is refused,
in three places: the Update Game screen (no prepare is started), the app's
prepare/install workers, and GameUpdates.prepare()/install() themselves.

The presence of THIS FILE under ``<root>/watchdogs/`` is the build marker. A
GameUpdates instance pointed at any other tree (upstream's own test fixtures, a
stock checkout) is not affected.
"""

import copy
import http.client
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

# Seconds one HTTPS request of the notice-only check may take. Two requests
# at most (repository identity, then the feed), on a background thread, so a
# dead or captive network costs a few seconds nobody waits for. upstream's own check
# allows 12 s per request and then falls back to a 20 s `gh` subprocess.
NOTICE_TIMEOUT = 4.0

BUILD_TAG = "bristle"
BUILD_NAME = "WDG-Bristle"
BUILD_VERSION = "1.0"

# One line each, <= 83 characters: the Update Game window shows one log line.
INSTALL_BLOCKED = "Blocked: WDG-Bristle is updated by hand, never from an online release."
INSTALL_BLOCKED_HOW = "To update, install a new wdg-bristle.zip (see INSTALL_WDG_BRISTLE.md)."
NOTICE_AVAILABLE = "A newer base release exists. Notice only: this build is updated by hand."
NOTICE_NONE = "Updates: managed manually"

_MARKER = Path("watchdogs") / "merge_build.py"


def display_version(version) -> str:
    """'0.9.21.3' -> 'WDG-Bristle 1.0 (base 0.9.21.3)' (display only; never parsed)."""
    return f"{BUILD_NAME} {BUILD_VERSION} (base {version})"


def numeric_version(version) -> str:
    """The plain base version again, for anything that compares versions."""
    text = str(version)
    if "(base " in text:
        return text.partition("(base ")[2].rstrip(") ").strip()
    return text.partition("+")[0]


def release_page() -> str:
    """Where new builds are published, if you set one (WDG_UPDATE_PAGE in secrets.conf).

    It is only shown. This build never downloads or installs anything from it.
    """
    try:
        from .config import _secret
        return str(_secret("WDG_UPDATE_PAGE") or "").strip()[:60]
    except Exception:
        return ""


def is_merged_tree(root) -> bool:
    """True when ``root`` is an installation of this merged build."""
    try:
        return (Path(root) / _MARKER).is_file()
    except (TypeError, ValueError, OSError):
        return False


def install_blocked(updates) -> bool:
    """True when ``updates`` (a GameUpdates) manages a merged-build tree."""
    return is_merged_tree(getattr(updates, "root", None))


def kismet_bridge_active(plugins) -> bool:
    """True while a loaded plugin reports a running Kismet capture
    (plugins/kismet_wardrive.py exposes kismet_active()). Used by app.py to
    count a Kismet drive as "wardriving" for upstream's GPS trail."""
    for plugin in plugins or ():
        active = getattr(plugin, "kismet_active", None)
        if callable(active):
            try:
                if active():
                    return True
            except Exception:
                pass
    return False



def _resolve_source(updates, fetch, limit):
    """upstream's repository identity check (GameUpdates._resolve_source: pinned
    repository and owner IDs), fed by one short-timeout HTTPS request instead
    of GameUpdates._github_metadata(), which waits 12 s and then tries a 20 s
    ``gh api`` subprocess (through sudo when the game runs as root). The
    validation itself is upstream's code, run on a shallow copy so the shared
    GameUpdates object is never switched to another metadata source; the
    verified/unverified source it records is copied back for the O shortcut.
    """
    if getattr(updates, "_metadata", None) is not None:
        return updates._resolve_source()   # a caller-supplied source (tests)
    from . import game_updates as gu
    probe = copy.copy(updates)
    probe._metadata = lambda: json.loads(fetch(
        f"https://api.github.com/repositories/{gu.REPOSITORY_ID}", limit,
        "application/vnd.github+json"))
    try:
        return probe._resolve_source()
    finally:
        updates._source = probe._source


def notice_only_check(updates, fetch=None):
    """Tell the player a newer upstream release exists, without Git and without
    making anything installable.

    upstream's GameUpdates.check() needs a Git checkout (``.git``) and an unmodified
    tree; a zip-installed, merged build has neither, so check() can only ever
    answer "error" and the release notice would never appear. This reads the
    same published feed (updates/stable.json on upstream's release branch, resolved
    through the pinned repository ID exactly as upstream does) over HTTPS, validates
    it with upstream's own feed parser, and compares version numbers. Nothing is
    downloaded, staged or remembered as "verified current".

    Offline it fails quietly: every network or parse failure becomes an
    "error" result (shown only as the status line of SYSTEM > Update Game),
    nothing is raised, nothing is written to the terminal or HUD, and each
    request gives up after NOTICE_TIMEOUT seconds. app.py runs this on a
    daemon thread, so it cannot hold up startup or shutdown either way.

    ``fetch(url, limit[, accept]) -> str`` replaces the HTTPS reader (tests).

    Returns a game_updates.UpdateResult: "available" (newer version published),
    "unpublished" (nothing newer) or "error".
    """
    from . import game_updates as gu
    version = "unknown"
    if fetch is None:
        # [bristle] WDG-Bristle has no online update source: nothing is fetched, ever. The feed
        # logic below only runs when a caller hands in its own reader (the unit tests do).
        try:
            version = updates.installed_version()
        except Exception:
            pass
        page = release_page()
        return gu.UpdateResult("unpublished", version,
                               message=NOTICE_NONE + (". New builds: " + page if page else ""))
    try:
        version = updates.installed_version()
        repository = _resolve_source(updates, fetch, gu.MAX_FEED_BYTES)
        url = f"https://raw.githubusercontent.com/{repository}/{gu.BRANCH}/{gu.FEED_PATH}"
        text = fetch(url, gu.MAX_FEED_BYTES)
        release = updates._release(json.loads(text))
        if release.repository not in {gu.REPOSITORY, repository}:
            raise gu.UpdateError("The release does not belong to the verified update source.")
        if gu._numeric(release.version) > gu._numeric(version):
            return gu.UpdateResult("available", version, release, NOTICE_AVAILABLE)
        return gu.UpdateResult("unpublished", version, message=NOTICE_NONE)
    except gu.UpdateError as error:
        return gu.UpdateResult("error", version, message=str(error))
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError,
            TypeError, UnicodeError) as error:
        # Offline, DNS failure, timeout, captive portal HTML, truncated reply.
        log.debug("Notice-only update check failed: %r", error)
        return gu.UpdateResult(
            "error", version,
            message="Cannot check for updates. Check network and GitHub access.")
