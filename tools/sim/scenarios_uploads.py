"""[merge-v3] Upload scenarios: the real WiGLE path (Kismet bridge) and upstream's
real Wars Sync upload against tools/sim/fake_web.py. Real sockets, real
timeouts, local only: nothing here can reach wigle.net or wdgwars.pl."""
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.sim.core import check, check_equal, scenario
from tools.sim.fake_kismet import KismetRig, _Forward
from tools.sim.fake_web import FakeWeb

FRAME_LIMIT = 0.25
SERVER_HANG = 2.5          # longer than the shortened client timeouts below


# ----------------------------------------------------------------------
# WiGLE (plugins/kismet_wardrive.py)
# ----------------------------------------------------------------------

class WigleRig:
    def __init__(self, ctx, web=None, rows=25, online=True):
        from plugins import kismet_wardrive as kismet
        import test_v3_loot_kismet as lk
        self.kismet = kismet
        ctx.patch(kismet.KismetWardriveBridge, "_crash_safety_installed", True)
        self.web = web or FakeWeb().up()
        ctx.cleanup(self.web.down)
        self.rig = KismetRig(ctx)
        self.plugin = self.rig.plugin
        self.online = online
        ctx.patch(kismet, "WIGLE_API_URL", self.web.wigle_url)
        ctx.patch(kismet, "WIGLE_API_NAME", "sim-name")
        ctx.patch(kismet, "WIGLE_API_TOKEN", "sim-token")
        kismet.hw.__dict__["internet_available"] = lambda: self.online
        loot = self.rig.loot
        for n in range(rows):
            loot.save_wardriving_network(lk.net("02:00:00:00:%02X:%02X" % (n >> 8, n & 255), ssid="W%d" % n),
                                         observed_at=lk.T0 + n)
        self.csv = Path(loot.session_path) / "wardriving.csv"
        self.rows = rows

    def short_timeout(self, ctx, seconds=1.0):
        """Keep the plugin's 120 s request timeout in the code, shorten it here."""
        real = self.kismet.requests

        def post(*args, **kwargs):
            kwargs["timeout"] = seconds
            return real.post(*args, **kwargs)
        ctx.patch(self.kismet, "requests", _Forward(real, post=post))

    def settle(self, timeout=20.0):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            self.rig.frame()
            if not self.plugin._wigle_busy and not self.plugin._wigle_events:
                self.rig.frame()
                return
            time.sleep(0.005)
        check(False, "the WiGLE worker never finished")

    def manual(self):
        self.plugin.upload_wigle()
        self.settle()

    def retry(self):
        self.plugin._retry_wigle_queue()
        self.settle()

    def queue(self):
        return self.plugin._load_wigle_queue()

    def hud(self, fragment):
        return [line for line in self.rig.hud if fragment in line]


def assert_one_wigle_file(rig):
    check_equal(len(rig.web.wigle_files), 1, "files WiGLE accepted (a second one is a double post)")
    name, _digest, rows = rig.web.wigle_files[0]
    check_equal((name, len(rows)), ("wardriving.csv", rig.rows), "file WiGLE received (name, rows)")
    check_equal(rig.queue(), [], "the retry queue after a successful upload")


@scenario("uploads", "WiGLE: success")
def wigle_success(ctx):
    rig = WigleRig(ctx)
    rig.manual()
    check(rig.hud("Uploading to WiGLE.net in the background...") and rig.hud("Uploaded to WiGLE.net"),
          "HUD messages: %s" % rig.rig.hud)
    assert_one_wigle_file(rig)
    rig.retry()
    check_equal(rig.web.wigle_attempts, 1, "posts after success (the retry pass must not post again)")
    check(rig.rig.max_frame() < FRAME_LIMIT, "the game loop stalled for %.2f s" % rig.rig.max_frame())


@scenario("uploads", "WiGLE: 401, 413, 5xx")
def wigle_status_codes(ctx):
    rig = WigleRig(ctx)
    rig.web.script("wigle", ("status", 401))
    rig.manual()
    check(rig.hud("WiGLE upload failed -- queued"), "no failure message for 401")
    check(rig.rig.said("check WDG_WIGLE_NAME / WDG_WIGLE_TOKEN"), "401 did not point at the credentials")
    check_equal(len(rig.queue()), 1, "queue after a 401")
    rig.retry()
    check_equal(rig.web.wigle_attempts, 0, "accepted/normal posts while WiGLE refuses the login")
    check_equal(len(rig.web.log), 1, "requests to WiGLE inside the 30-minute credential back-off")
    rig.plugin._wigle_not_before = 0.0                 # 30 minutes later, credentials fixed
    rig.web.script("wigle", ("status", 500), ("status", 502))
    rig.retry()
    rig.retry()
    check_equal(len(rig.rig.said("Queued WiGLE retry still failing (HTTP 50")), 2, "5xx retry lines")
    check_equal(len(rig.queue()), 1, "queue during 5xx answers")
    rig.retry()
    check(rig.hud("WiGLE: queued upload sent"), "no message when the queued upload finally went out")
    assert_one_wigle_file(rig)
    # 413: this file can never be sent; it must not sit in the queue forever
    big = WigleRig(ctx, rows=5)
    big.web.script("wigle", ("status", 413))
    big.manual()
    check(big.rig.said("too large (HTTP 413): not retried"), "413 was not explained")
    check_equal(big.queue(), [], "queue after a 413")
    check(big.csv.is_file(), "the CSV must stay on disk after a 413")


@scenario("uploads", "WiGLE: 429 with Retry-After")
def wigle_rate_limited(ctx):
    rig = WigleRig(ctx)
    rig.web.script("wigle", ("status", 429, {"Retry-After": "120"}))
    rig.manual()
    check(rig.rig.said("WiGLE asked us to wait: next try in 2 min"), "Retry-After was not honoured in words")
    wait = rig.plugin._wigle_not_before - time.time()
    check(110 < wait <= 120, "back-off should be the 120 s WiGLE asked for, is %.0f s" % wait)
    for _ in range(5):                                  # five "one minute" retry passes inside the wait
        rig.retry()
    check_equal(len(rig.web.log), 1, "requests to WiGLE while it said to wait")
    rig.plugin._wigle_not_before = time.time() - 1      # the wait is over
    rig.retry()
    assert_one_wigle_file(rig)
    plain = WigleRig(ctx, rows=3)
    plain.web.script("wigle", ("status", 429), ("status", 429, {"Retry-After": "99999"}))
    plain.manual()
    check(880 < plain.plugin._wigle_not_before - time.time() <= 900, "429 without Retry-After: 15 min")
    plain.plugin._wigle_not_before = 0.0
    plain.retry()
    check(plain.plugin._wigle_not_before - time.time() <= 3600, "an absurd Retry-After must be capped at 1 h")


@scenario("uploads", "WiGLE: timeout and web page instead of JSON")
def wigle_timeout_and_html(ctx):
    rig = WigleRig(ctx)
    rig.short_timeout(ctx)
    rig.web.script("wigle", ("hang", SERVER_HANG))
    started = time.perf_counter()
    rig.plugin.upload_wigle()
    ticks = 0
    while rig.plugin._wigle_busy and time.perf_counter() - started < 10:
        rig.rig.frame()
        ticks += 1
        time.sleep(0.005)
    rig.settle()
    check(ticks > 50, "the game loop did not keep ticking during the hung upload (%d frames)" % ticks)
    check(rig.rig.max_frame() < FRAME_LIMIT, "the game loop stalled for %.2f s" % rig.rig.max_frame())
    check(rig.hud("WiGLE upload failed -- queued"), "a timed-out upload was not queued")
    time.sleep(SERVER_HANG)                              # let the hung request drain
    rig.web.wigle_files.clear()                          # the hung post may have landed late
    rig.web.script("wigle", "html", "redirect", "empty")
    for _ in range(3):
        rig.retry()
    check(len(rig.rig.said("web page, not JSON")) >= 3, "login page / redirect / empty body: %s"
          % rig.rig.said("Queued WiGLE retry"))
    check_equal(len(rig.queue()), 1, "queue while WiGLE serves a login page")
    rig.retry()
    assert_one_wigle_file(rig)


@scenario("uploads", "WiGLE: network down, restart, network back")
def wigle_network_down(ctx):
    web = FakeWeb().up()
    rig = WigleRig(ctx, web=web)
    web.down()
    rig.online = False
    rig.manual()
    check(rig.hud("WiGLE upload failed -- queued"), "no failure message with the network down")
    check_equal(len(rig.queue()), 1, "queue with the network down")
    for _ in range(3):
        rig.retry()                                      # offline: the pass must stop at the ping
    queue_file = rig.plugin._queue_file()
    saved = json.loads(queue_file.read_text())
    check_equal(len(saved), 1, "entries in the queue file on disk")
    # the game is quit and started again while still offline
    again = WigleRig(ctx, web=web, rows=0, online=False)
    again.rows = rig.rows
    check_equal(again.queue(), saved, "the queue after a restart")
    again.retry()
    web.up()
    again.online = True
    again.retry()
    check(again.hud("WiGLE: queued upload sent"), "no message when the network came back")
    assert_one_wigle_file(again)
    again.retry()
    check_equal(web.wigle_attempts, 1, "posts in total (one file, once)")


# ----------------------------------------------------------------------
# WDGWars (upstream's plugins/wardrive_upload.py, unchanged)
# ----------------------------------------------------------------------

WIFI_HEADER = ("MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,"
               "AltitudeMeters,AccuracyMeters,Type")


class WarsRig:
    def __init__(self, ctx, web=None, sessions=(("2026-11-20_10-00-00", 12), ("2026-11-20_18-00-00", 9))):
        import plugins.wardrive_upload as wars
        self.ctx, self.wars = ctx, wars
        self.web = web or FakeWeb().up()
        ctx.cleanup(self.web.down)
        self.base = Path(ctx.tmp) / "loot"
        self.base.mkdir(exist_ok=True)
        self.state_path = Path(ctx.tmp) / "wars-state.json"
        self.total = 0
        for name, count in sessions:
            self.write_session(name, count)
        ctx.patch(wars, "UPLOAD_POST_INTERVAL", 0.0)
        self.term = []
        self.frame_times = []
        self.plugin = self.new_plugin()

    def write_session(self, name, count):
        folder = self.base / name
        folder.mkdir(exist_ok=True)
        lines = ["WigleWifi-1.4,appRelease=sim,model=sim", WIFI_HEADER]
        for n in range(count):
            mac = "0A:%02X:%02X:%02X:%02X:%02X" % (len(name) & 255, hash(name) & 255, n >> 8, n & 255, 1)
            lines.append("%s,Net %d,[WPA2-PSK-CCMP][ESS],2026-11-20 10:00:%02d,%d,-60,50.06%04d,19.94%04d,210,5,WIFI"
                         % (mac, n, n % 60, 1 + n % 11, n, n))
        (folder / "wardriving.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.total += count

    def new_plugin(self, key="sim-key-not-real"):
        wars = self.wars
        with patch.object(wars.WardriveUpload, "_state_file", return_value=self.state_path), \
                patch.object(wars, "_load_secrets_conf", return_value={
                    "WARDRIVE_API_URL": self.web.upload_url, "WARDRIVE_API_KEY": key}):
            plugin = wars.WardriveUpload()
        plugin._state_file = lambda: self.state_path
        plugin._save_secret = lambda *args: None            # never touch a real secrets.conf
        plugin._badge_sync_worker = lambda *a, **k: None
        plugin._handle_upload_badges = lambda *a, **k: None
        plugin.app = SimpleNamespace(
            loot=SimpleNamespace(_base=str(self.base), session_dir=str(self.base / "active-session"),
                                 prepare_upload_log_discard=lambda *_args: True),
            _term_add=lambda text, raw=False: self.term.append(text), xp=0)
        self.ctx.cleanup(plugin._sync_stop.set)
        return plugin

    def short_timeout(self, seconds=1.0):
        wars = self.wars
        real = wars.urlopen
        self.ctx.patch(wars, "urlopen", lambda request, timeout=30, **kw: real(request, timeout=min(timeout, seconds)))

    def upload(self, plugin=None, timeout=60.0):
        plugin = plugin or self.plugin
        plugin._start_upload(True)
        deadline = time.perf_counter() + timeout
        seen_busy = False
        while time.perf_counter() < deadline:
            started = time.perf_counter()
            plugin._sync_snapshot_view()                 # what the Sync screen reads every frame
            busy = plugin._uploading or plugin._sync_upload_pending
            self.frame_times.append(time.perf_counter() - started)
            seen_busy = seen_busy or busy
            if seen_busy and not busy and plugin._sync_idle.is_set():
                return
            time.sleep(0.005)
        check(False, "the Wars Sync upload never finished: %s" % self.term[-4:])

    def said(self, fragment):
        return [line for line in self.term if fragment in line]

    def uploaded(self, plugin=None):
        return sorted((plugin or self.plugin)._uploaded_sessions)

    def assert_exactly_once(self):
        check_equal(len(self.web.known), self.total, "records the portal has")
        twice = [key for key, count in self.web.imported_new.items() if count != 1]
        check_equal(twice, [], "records the portal counted as NEW more than once")


@scenario("uploads", "WDGWars: success, and nothing is posted twice")
def wars_success(ctx):
    rig = WarsRig(ctx)
    rig.upload()
    check_equal(len(rig.uploaded()), 2, "sessions marked uploaded: %s" % rig.term[-6:])
    rig.assert_exactly_once()
    posts = rig.web.upload_attempts
    rig.upload()                                           # pressing Upload again
    check_equal(rig.web.upload_attempts, posts, "posts on a second Upload with nothing new")
    restarted = rig.new_plugin()                           # the game is restarted
    check_equal(rig.uploaded(restarted), rig.uploaded(), "uploaded sessions after a restart")
    rig.upload(restarted)
    check_equal(rig.web.upload_attempts, posts, "posts after a restart with nothing new")
    check(max(rig.frame_times) < FRAME_LIMIT, "the UI thread stalled for %.2f s" % max(rig.frame_times))


@scenario("uploads", "WDGWars: 401, 5xx, 429 with Retry-After")
def wars_status_codes(ctx):
    rig = WarsRig(ctx, sessions=(("2026-11-20_10-00-00", 8),))
    rig.web.script("upload", ("status", 401))
    rig.upload()
    check(rig.said("HTTP 401; left pending"), "401 was not reported: %s" % rig.term[-4:])
    check_equal(rig.uploaded(), [], "sessions marked uploaded after a 401")
    rig.web.script("upload", ("status", 503))
    rig.upload()
    check(rig.said("HTTP 503; left pending"), "503 was not reported")
    check_equal(len(rig.web.known), 0, "records accepted so far")
    rig.web.script("upload", ("status", 429, {"Retry-After": "1"}))
    started = time.perf_counter()
    rig.upload()
    check(rig.said("Rate limit; retry in 1s"), "Retry-After was not honoured: %s" % rig.term[-4:])
    check(time.perf_counter() - started >= 1.0, "the retry did not wait the second the portal asked for")
    check_equal(len(rig.uploaded()), 1, "sessions marked uploaded after the rate limit passed")
    rig.assert_exactly_once()
    check(max(rig.frame_times) < FRAME_LIMIT, "the UI thread stalled for %.2f s" % max(rig.frame_times))


@scenario("uploads", "WDGWars: 413 is split into smaller batches")
def wars_too_large(ctx):
    rig = WarsRig(ctx, sessions=(("2026-11-20_10-00-00", 40),))
    rig.web.script("upload", ("status", 413))
    rig.upload()
    check_equal(len(rig.uploaded()), 1, "session confirmed through smaller batches: %s" % rig.term[-5:])
    check(rig.web.upload_attempts >= 2, "the upload was not split after the 413")
    rig.assert_exactly_once()


@scenario("uploads", "WDGWars: login page, redirect and empty replies are 'not yet'")
def wars_not_json(ctx):
    rig = WarsRig(ctx, sessions=(("2026-11-20_10-00-00", 6),))
    for behaviour in ("html", "redirect", "empty"):
        rig.web.script("upload", behaviour)
        rig.upload()
        check_equal(rig.uploaded(), [], "sessions marked uploaded after a %s reply" % behaviour)
    check_equal(len(rig.said("Invalid server response; left pending")), 3,
                "'left pending' lines for the three non-JSON replies: %s" % rig.term[-8:])
    check(not rig.said("Traceback") and not rig.said("Upload error"), "a non-JSON reply was treated as a crash")
    rig.upload()
    check_equal(len(rig.uploaded()), 1, "session uploaded once the portal answered properly")
    rig.assert_exactly_once()


@scenario("uploads", "WDGWars: timeout with a lost receipt, then recovery")
def wars_timeout(ctx):
    rig = WarsRig(ctx, sessions=(("2026-11-20_10-00-00", 10),))
    rig.short_timeout()
    rig.web.script("upload", ("hang", SERVER_HANG), ("hang", SERVER_HANG), ("hang", SERVER_HANG),
                   ("hang", SERVER_HANG), ("hang", SERVER_HANG))
    rig.upload()
    check(rig.said("timed out"), "the timeout was not reported: %s" % rig.term[-4:])
    check_equal(rig.uploaded(), [], "sessions marked uploaded although every request timed out")
    time.sleep(SERVER_HANG + 0.3)
    rig.web._scripts["upload"].clear()
    check(max(rig.frame_times) < FRAME_LIMIT, "the UI thread stalled for %.2f s" % max(rig.frame_times))
    # the hung requests did reach the server: it holds the records, the game holds no receipt.
    # The next upload must end with the session confirmed and nothing counted as new twice.
    rig.upload()
    check_equal(len(rig.uploaded()), 1, "session confirmed after the portal recovered: %s" % rig.term[-5:])
    rig.assert_exactly_once()


@scenario("uploads", "WDGWars: network down, restart, network back")
def wars_network_down(ctx):
    web = FakeWeb().up()
    rig = WarsRig(ctx, web=web)
    web.down()
    rig.upload()
    check(rig.said("No confirmed response; left pending"), "no message with the network down: %s" % rig.term[-4:])
    check_equal(rig.uploaded(), [], "sessions marked uploaded with the network down")
    restarted = rig.new_plugin()                           # quit and start again, still offline
    check_equal(rig.uploaded(restarted), [], "uploaded sessions after an offline restart")
    rig.upload(restarted)
    web.up()
    rig.upload(restarted)
    check_equal(len(rig.uploaded(restarted)), 2, "sessions uploaded once the network was back")
    rig.assert_exactly_once()
    posts = web.upload_attempts
    final = rig.new_plugin()
    rig.upload(final)
    check_equal(web.upload_attempts, posts, "posts after another restart (must be none)")
    check(os.path.exists(rig.state_path), "the upload state file")
