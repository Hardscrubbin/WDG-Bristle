"""[merge-v3] app-level merge behaviour: background plugin tick, the Update
Game install block, the build version, MapTiler-aware map gates, credits.

The real app.py methods are compiled into bare classes (upstream's own test idiom),
so no pyxel window, radio, GPS or network is needed. Nothing here contacts
GitHub: the update feed and repository metadata are supplied by the test.
"""

import ast
import json
from pathlib import Path
from queue import Queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import watchdogs
from watchdogs import auto_map, game_updates, merge_build, tile_manager
from plugins.wardrive_upload import WardriveUpload
from plugins.janos_import import JanosImport
import plugins.kismet_wardrive as kismet

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "watchdogs" / "app.py"
TEXT = SOURCE.read_text(encoding="utf-8")
TREE = ast.parse(TEXT)
GAME = next(node for node in TREE.body
            if isinstance(node, ast.ClassDef) and node.name == "WatchDogsGame")


class Keys:
    """Minimal pyxel stand-in: named keys, a set of keys pressed this frame."""
    frame_count = 10

    def __init__(self):
        self.pressed = set()
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("KEY_"):
            return name
        return lambda *args: self.calls.append((name, args))

    def btnp(self, key, *args):
        return key in self.pressed


def app_methods(*names, **env):
    body = [node for node in GAME.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in body} == set(names), names
    module = ast.Module(body=[ast.ClassDef(name="Harness", bases=[], keywords=[],
                                           decorator_list=[], body=body)], type_ignores=[])
    namespace = dict(__name__="watchdogs.v3_method_test", __package__="watchdogs",
                     C_WARNING=10, C_ERROR=8, C_DIM=13, C_TEXT=7, C_HACK_CYAN=12, C_SUCCESS=11,
                     C_COAST=5, **env)
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), "exec"), namespace)
    return namespace["Harness"]


RELEASE = dict(schema=2, repository=game_updates.REPOSITORY, repository_id=game_updates.REPOSITORY_ID,
               channel=game_updates.CHANNEL, release_id="v9.9.9", version="9.9.9",
               source_commit="a" * 40, title="RELEASE 9.9.9", notes="1. SOMETHING\nNotes.")
METADATA = dict(id=game_updates.REPOSITORY_ID, full_name=game_updates.REPOSITORY,
                owner=dict(id=game_updates.OWNER_ID, login=game_updates.OWNER))


def merged_updates(home):
    """A real GameUpdates on this very tree. git must never be reached."""
    git = Mock(side_effect=AssertionError("git must not run in the merged build"))
    return game_updates.GameUpdates(root=ROOT, home=home, git=git, metadata=lambda: dict(METADATA)), git


class BackgroundTickTests(unittest.TestCase):
    def setUp(self):
        self.game = app_methods("_tick_background_plugins")()
        self.game._term_add = Mock()

    def plugin(self, **attributes):
        value = SimpleNamespace(on_update=Mock(), **attributes)
        return value

    def test_only_opted_in_plugins_tick_with_their_overlay_closed(self):
        bridge = self.plugin(BACKGROUND_TICK=True, overlay_active=False)
        sync = self.plugin(overlay_active=False)                      # upstream's Wars Sync
        declined = self.plugin(BACKGROUND_TICK=False, overlay_active=False)
        self.game._plugins = [sync, bridge, declined]
        for _ in range(3):
            self.game._tick_background_plugins()
        self.assertEqual(bridge.on_update.call_count, 3)
        sync.on_update.assert_not_called()
        declined.on_update.assert_not_called()

    def test_plugin_with_an_open_overlay_is_left_to_the_overlay_dispatch(self):
        bridge = self.plugin(BACKGROUND_TICK=True, overlay_active=True)
        self.game._plugins = [bridge]
        self.game._tick_background_plugins()
        bridge.on_update.assert_not_called()      # app.py's dispatch ticks it once

    def test_a_crashing_plugin_is_reported_and_does_not_stop_the_others(self):
        broken = self.plugin(BACKGROUND_TICK=True, NAME="Broken")
        broken.on_update.side_effect = RuntimeError("boom")
        healthy = self.plugin(BACKGROUND_TICK=True)
        self.game._plugins = [broken, healthy]
        self.game._tick_background_plugins()
        healthy.on_update.assert_called_once()
        line = self.game._term_add.call_args.args[0]
        self.assertIn("Broken", line)
        self.assertIn("boom", line)

    def test_no_plugin_list_yet_is_fine(self):
        self.game._tick_background_plugins()

    def test_which_real_plugins_opt_in(self):
        self.assertIs(kismet.KismetWardriveBridge.BACKGROUND_TICK, True)
        # upstream's plugins keep upstream's schedule (overlay-only), see the note in app.py
        self.assertFalse(getattr(WardriveUpload, "BACKGROUND_TICK", False))
        self.assertFalse(getattr(JanosImport, "BACKGROUND_TICK", False))

    def test_update_calls_the_tick_every_frame(self):
        update = next(node for node in GAME.body
                      if isinstance(node, ast.FunctionDef) and node.name == "update")
        segment = ast.get_source_segment(TEXT, update)
        self.assertEqual(segment.count('getattr(self, "_tick_background_plugins", None)'), 1)
        # with the other per-frame polls, before the per-screen early returns
        # (update notice, Update Game window, overlays), so it runs whatever
        # is on screen...
        self.assertLess(segment.index("self._poll_proximity()"), segment.index("_tick_plugins()"))
        self.assertLess(segment.index("_tick_plugins()"),
                        segment.index("self._maybe_show_game_update_notice()"))
        # ...but after upstream's "devices were stopped for an upgrade" return:
        # closed transports are never polled again, plugins included.
        self.assertLess(segment.index("_gameupd_closed_for_install"), segment.index("_tick_plugins()"))

    def test_wars_sync_does_background_work_before_its_overlay_check(self):
        # The reason the tick is opt-in. If upstream ever moves this work behind the
        # overlay check again, the opt-in is still harmless.
        with patch.object(WardriveUpload, "_load_state"):
            sync = WardriveUpload()
        sync.app = SimpleNamespace(loot=None)
        sync._request_sync_refresh = Mock()
        sync._overlay_active = False
        sync._next_cleanup = 0.0
        sync.on_update()
        sync._request_sync_refresh.assert_called_once()


class VersionTests(unittest.TestCase):
    def test_version_is_numeric_for_every_update_compare(self):
        self.assertEqual(watchdogs.__version__, "0.9.21.3")
        data = (ROOT / "watchdogs" / "__init__.py").read_bytes()
        self.assertEqual(game_updates.GameUpdates._version_bytes(data), "0.9.21.3")
        self.assertEqual(game_updates._numeric(watchdogs.__version__), (0, 9, 21, 3))
        feed = json.loads((ROOT / "updates" / "stable.json").read_text(encoding="utf-8"))
        self.assertEqual(game_updates._numeric(feed["version"]),
                         game_updates._numeric(watchdogs.__version__))

    def test_display_version_carries_the_build_tag_and_strips_back(self):
        shown = merge_build.display_version(watchdogs.__version__)
        self.assertEqual(shown, "WDG-Bristle 1.0 (base 0.9.21.3)")
        self.assertEqual(merge_build.numeric_version(shown), "0.9.21.3")
        self.assertEqual(game_updates._numeric(merge_build.numeric_version(shown)), (0, 9, 21, 3))

    def test_startup_line_and_credits(self):
        self.assertIn('f"[SYS] {merge_build.display_version(__version__)}"', TEXT)
        self.assertIn('_author = "by LOCOSP"', TEXT)
        self.assertIn('_modified = "WDG-Bristle 1.0"', TEXT)
        self.assertIn("NOTICE_NONE", TEXT.split("def _draw_gameupd_screen", 1)[1])
        self.assertNotIn("avatar.png", TEXT)

    def test_dropped_flash_boards_duplicate_left_one_definition(self):
        config = (ROOT / "watchdogs" / "config.py").read_text(encoding="utf-8")
        self.assertEqual(config.count("\nFLASH_BOARDS = {"), 1)
        from watchdogs import config as loaded
        self.assertEqual(sorted(loaded.FLASH_BOARDS), ["wroom", "xiao"])
        self.assertEqual(loaded.FLASH_OFFSETS, loaded.FLASH_BOARDS["wroom"]["offsets"])
        self.assertFalse(loaded.UPSTREAM_FIRMWARE_ENABLED)       # upstream 0.9.21's change kept
        self.assertEqual(loaded.FIRMWARE_RELEASE_URL, "")        # [bristle] no online firmware source


class UpdateBlockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.updates, self.git = merged_updates(self.home)

    def test_this_tree_is_recognised_and_other_trees_are_not(self):
        self.assertTrue(merge_build.is_merged_tree(ROOT))
        self.assertTrue(merge_build.install_blocked(self.updates))
        stock = self.home / "stock"
        (stock / "watchdogs").mkdir(parents=True)
        self.assertFalse(merge_build.is_merged_tree(stock))
        self.assertFalse(merge_build.install_blocked(game_updates.GameUpdates(root=stock, home=self.home)))
        for odd in (None, Mock(), SimpleNamespace(), SimpleNamespace(root=None)):
            self.assertFalse(merge_build.install_blocked(odd))

    def test_game_updates_refuses_to_stage_or_install_over_the_merge(self):
        release = self.updates._release(dict(RELEASE))
        with self.assertRaises(game_updates.UpdateError) as staged:
            self.updates.prepare(release)
        self.assertEqual(str(staged.exception), merge_build.INSTALL_BLOCKED)
        for quiescent in (True, False):
            with self.assertRaises(game_updates.UpdateError) as installed:
                self.updates.install(Mock(), quiescent=quiescent)
            self.assertEqual(str(installed.exception), merge_build.INSTALL_BLOCKED)
        self.git.assert_not_called()
        self.assertFalse((self.home / ".watchdogs_update_staging").exists())
        self.assertFalse((self.home / ".watchdogs_update_backups").exists())

    def test_stock_tree_still_reaches_upstreams_own_checks(self):
        stock = self.home / "stock"
        (stock / "watchdogs").mkdir(parents=True)
        plain = game_updates.GameUpdates(root=stock, home=self.home)
        with self.assertRaises(game_updates.UpdateError) as error:
            plain.install(Mock(), quiescent=False)
        self.assertEqual(str(error.exception), "Stop game operations before installing the update.")

    def test_message_names_the_kismet_add_ons_and_fits_the_window(self):
        self.assertIn("updated by hand", merge_build.INSTALL_BLOCKED)
        self.assertEqual(merge_build.NOTICE_NONE, "Updates: managed manually")
        for line in (merge_build.INSTALL_BLOCKED, merge_build.INSTALL_BLOCKED_HOW,
                     merge_build.NOTICE_AVAILABLE, merge_build.NOTICE_NONE):
            self.assertLessEqual(len("[UPD] " + line), 83, line)

    # ---- the notice is kept -------------------------------------------------

    def check(self, feed):
        fetched = []

        def fetch(url, limit):
            fetched.append((url, limit))
            if isinstance(feed, Exception):
                raise feed
            return feed

        return merge_build.notice_only_check(self.updates, fetch=fetch), fetched

    def test_newer_c5_release_is_reported_without_git(self):
        result, fetched = self.check(json.dumps(RELEASE))
        self.assertEqual((result.status, result.installed_version), ("available", "0.9.21.3"))
        self.assertEqual(result.release.version, "9.9.9")
        self.assertEqual(result.message, merge_build.NOTICE_AVAILABLE)
        self.assertEqual(fetched, [(
            f"https://raw.githubusercontent.com/{game_updates.REPOSITORY}/"
            f"{game_updates.BRANCH}/{game_updates.FEED_PATH}", game_updates.MAX_FEED_BYTES)])
        self.git.assert_not_called()
        self.assertTrue(self.updates.source_info()["verified"])
        self.assertFalse((self.home / ".watchdogs_game_updates.json").exists())  # nothing "remembered"

    def test_same_or_older_release_is_not_an_update(self):
        for version in ("0.9.21.3", "0.9.21", "0.9.20"):
            with self.subTest(version=version):
                result, _ = self.check(json.dumps(dict(RELEASE, version=version)))
                self.assertEqual((result.status, result.release), ("unpublished", None))
                self.assertEqual(result.message, merge_build.NOTICE_NONE)

    def test_bad_feed_wrong_source_and_offline_are_errors_not_updates(self):
        wrong_owner = dict(RELEASE, repository="Someone/Else", repository_id=0)
        for feed in ("not json", json.dumps(dict(RELEASE, version="9.9.9+x")), json.dumps(wrong_owner),
                     json.dumps([1]), OSError("offline"), ValueError("feed too large")):
            with self.subTest(feed=str(feed)[:30]):
                result, _ = self.check(feed)
                self.assertEqual((result.status, result.release), ("error", None))
        self.git.assert_not_called()

    def test_repository_identity_is_still_verified(self):
        impostor = game_updates.GameUpdates(
            root=ROOT, home=self.home, git=self.git,
            metadata=lambda: dict(METADATA, id=1, full_name="Someone/Else"))
        fetch = Mock()
        result = merge_build.notice_only_check(impostor, fetch=fetch)
        self.assertEqual(result.status, "error")
        fetch.assert_not_called()

    # ---- the app ------------------------------------------------------------

    def game(self, *methods, keys=None, **env):
        Harness = app_methods(*methods, pyxel=keys or Keys(), threading=threading, time=time,
                              logging=__import__("logging"), **env)
        game = Harness()
        game._game_updates = self.updates
        game._game_update_events = Queue()
        game._term_add = Mock()
        return game

    def test_check_worker_uses_the_notice_only_check_in_the_merged_build(self):
        game = self.game("_check_game_update")
        wanted = game_updates.UpdateResult("available", "0.9.21.3", self.updates._release(dict(RELEASE)), "x")
        with patch.object(merge_build, "notice_only_check", return_value=wanted) as notice, \
                patch.object(self.updates, "check", side_effect=AssertionError("git check ran")):
            game._check_game_update()
        notice.assert_called_once_with(self.updates)
        self.assertEqual(game._game_update_events.get_nowait(), ("check", wanted))

    def test_check_worker_keeps_upstreams_check_for_any_other_tree(self):
        game = self.game("_check_game_update")
        stock = SimpleNamespace(check=Mock(return_value="result"))
        game._game_updates = stock
        game._check_game_update()
        self.assertEqual(game._game_update_events.get_nowait(), ("check", "result"))

    def update_screen(self, key):
        keys = Keys()
        keys.pressed = {key}
        started = Mock()
        game = self.game("_update_gameupd_screen", "_gameupd_log_add", "_gameupd_note_lines", keys=keys)
        game._gameupd_open_frame = game._gameupd_install_frame = -1
        game._gameupd_running = game._gameupd_done = game._gameupd_confirm = False
        game._gameupd_closed_for_install = False
        game._gameupd_restart_at = None
        game._gameupd_log, game._gameupd_scroll = ["old"], 0
        game._gameupd_note_lines = lambda: ["note"]
        game._game_update_available, game._game_update_checking = True, False
        game._game_update_release = self.updates._release(dict(RELEASE))
        game._gameupd_prepare = started
        game._cleanup = Mock()
        with patch.object(threading, "Thread", Mock()) as thread:
            game._update_gameupd_screen()
        return game, thread, started

    def test_enter_on_an_available_update_starts_nothing(self):
        for key in ("KEY_RETURN", "KEY_I"):
            with self.subTest(key=key):
                game, thread, _ = self.update_screen(key)
                thread.assert_not_called()
                self.assertFalse(game._gameupd_running)
                self.assertFalse(game._gameupd_confirm)
                game._cleanup.assert_not_called()               # devices keep running
                self.assertEqual(game._gameupd_log, ["[UPD] " + merge_build.INSTALL_BLOCKED])
                said = [call.args[0] for call in game._term_add.call_args_list]
                self.assertIn("[UPD] " + merge_build.INSTALL_BLOCKED, said)
                self.assertIn("[UPD] " + merge_build.INSTALL_BLOCKED_HOW, said)
                self.assertTrue(game._game_update_available)    # the notice stays up

    def test_stock_tree_would_still_start_upstreams_prepare(self):
        keys = Keys()
        keys.pressed = {"KEY_RETURN"}
        game = self.game("_update_gameupd_screen", "_gameupd_log_add", keys=keys)
        game._game_updates = SimpleNamespace(root=self.home)       # no marker there
        game._gameupd_open_frame = game._gameupd_install_frame = -1
        game._gameupd_running = game._gameupd_done = game._gameupd_confirm = False
        game._gameupd_closed_for_install = False
        game._gameupd_restart_at = None
        game._gameupd_log, game._gameupd_scroll = [], 0
        game._gameupd_note_lines = lambda: ["note"]
        game._game_update_available, game._game_update_checking = True, False
        game._game_update_release = "release"
        game._gameupd_prepare = Mock()
        with patch.object(threading, "Thread", Mock()) as thread:
            game._update_gameupd_screen()
        thread.assert_called_once()
        self.assertTrue(game._gameupd_running)

    def test_prepare_and_install_workers_refuse_even_if_reached(self):
        game = self.game("_gameupd_prepare", "_gameupd_do", "_gameupd_log_add", "_gameupd_discard_plan")
        with patch.object(self.updates, "prepare", side_effect=AssertionError("prepare ran")), \
                patch.object(self.updates, "install", side_effect=AssertionError("install ran")), \
                self.assertLogs(level="ERROR"):
            game._gameupd_prepare(self.updates._release(dict(RELEASE)))
            event, message = game._game_update_events.get_nowait()
            self.assertEqual((event, message), ("prepare_error", merge_build.INSTALL_BLOCKED))
            game._gameupd_closed_for_install = game._cleanup_done = True
            game._cleanup_errors = ()
            game._gameupd_plan = None
            game._gameupd_log = []
            game._gameupd_do()
        self.assertFalse(game._gameupd_ok)
        self.assertIn("[UPD] Update failed: " + merge_build.INSTALL_BLOCKED, game._gameupd_log)


class MapProviderGateTests(unittest.TestCase):
    def secrets(self, **values):
        return patch.object(tile_manager, "_load_secret", side_effect=lambda name: values.get(name, ""))

    def test_provider_selection(self):
        cases = [({}, "", False),
                 (dict(STADIA_API_KEY="s"), "stadia", True),
                 (dict(MAPTILER_API_KEY="m"), "maptiler", True),
                 (dict(STADIA_API_KEY="s", MAPTILER_API_KEY="m"), "stadia", True),
                 (dict(STADIA_API_KEY="s", MAPTILER_API_KEY="m", TILE_PROVIDER="maptiler"), "maptiler", True),
                 (dict(STADIA_API_KEY="s", TILE_PROVIDER="maptiler"), "stadia", True)]
        for values, provider, configured in cases:
            with self.subTest(values=values), self.secrets(**values):
                self.assertEqual(tile_manager.active_tile_provider(), provider)
                self.assertIs(tile_manager.tile_key_configured(), configured)

    def test_maptiler_only_builds_a_maptiler_url(self):
        with self.secrets(MAPTILER_API_KEY="mt-key"):
            url = tile_manager._tile_url(12, 5, 6)
        self.assertEqual(url, "https://api.maptiler.com/maps/dataviz-v4-dark/12/5/6.png?key=mt-key")

    def test_code_that_substitutes_the_stadia_loader_still_steers_the_gate(self):
        # upstream's tests (and auto_map before the merge) patch _load_stadia_key
        with self.secrets(), patch.object(tile_manager, "_load_stadia_key", return_value="k"):
            self.assertTrue(tile_manager.tile_key_configured())
        with self.secrets(), patch.object(tile_manager, "_load_stadia_key", return_value=""):
            self.assertFalse(tile_manager.tile_key_configured())

    def manual(self):
        game = app_methods("_start_map_download", Path=Path, threading=threading,
                           download_tiles=Mock(), TileRenderer=Mock())()
        game.msg, game._term_add = Mock(), Mock()
        game.gps_fix, game.player_lat, game.player_lon = True, 12.5, 34.5
        game._app_dir = "unused"
        return game

    def test_manual_download_starts_with_only_a_maptiler_key(self):
        game = self.manual()
        with self.secrets(MAPTILER_API_KEY="m"), patch.object(threading, "Thread", Mock()) as thread:
            game._start_map_download()
        thread.assert_called_once()
        self.assertTrue(game._map_downloading)

    def test_manual_download_without_any_key_explains_both_options(self):
        game = self.manual()
        with self.secrets(), patch.object(threading, "Thread", Mock()) as thread:
            game._start_map_download()
        thread.assert_not_called()
        self.assertIn("STADIA_API_KEY", game.msg.call_args.args[0])
        said = " ".join(call.args[0] for call in game._term_add.call_args_list)
        self.assertIn("MAPTILER_API_KEY", said)

    def auto(self, **values):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        controller = auto_map.AutoMap(Path(directory.name))
        self.addCleanup(controller.close)
        controller.set_enabled(True)
        started = []

        class Worker:
            def __init__(self, target=None, args=(), **_kwargs):
                self.name = _kwargs.get("name", "")
                started.append((self.name, args))

            def start(self):
                pass

            def is_alive(self):
                return False

            def join(self, *_args):
                pass

        with self.secrets(**values), patch.object(auto_map.threading, "Thread", Worker):
            controller.poll(12.5, 34.5, True)
        return [name for name, _ in started if name == "wdg-auto-tiles"]

    def test_auto_map_requests_tiles_with_only_a_maptiler_key(self):
        self.assertEqual(self.auto(MAPTILER_API_KEY="m"), ["wdg-auto-tiles"])

    def test_auto_map_still_works_with_only_a_stadia_key(self):
        self.assertEqual(self.auto(STADIA_API_KEY="s"), ["wdg-auto-tiles"])

    def test_auto_map_without_any_key_requests_nothing(self):
        self.assertEqual(self.auto(), [])

    def test_auto_map_worker_downloads_through_the_provider_aware_url(self):
        seen = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                raise OSError("stop after the first request")

        def fake_urlopen(request, timeout=0):
            seen.append(request.full_url)
            return Response()

        with tempfile.TemporaryDirectory() as directory, self.secrets(MAPTILER_API_KEY="mt-key"), \
                patch.object(tile_manager, "urlopen", fake_urlopen), \
                patch.object(tile_manager.time, "sleep", lambda _s: None):
            manifest = tile_manager.download_tiles(12.5, 34.5, Path(directory), radius_km=10.0,
                                                   stop_on_error=True)
        self.assertEqual(len(seen), 1)                     # upstream's stop_on_error kept
        self.assertTrue(seen[0].startswith("https://api.maptiler.com/maps/dataviz-v4-dark/"))
        self.assertTrue(seen[0].endswith("?key=mt-key"))
        self.assertEqual(manifest["errors"], 1)


if __name__ == "__main__":
    unittest.main()
