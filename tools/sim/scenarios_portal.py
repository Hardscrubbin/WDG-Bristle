"""[merge-v3] WDGWars pairing + status scenarios: plugins/ng_wdgwars.py on top
of upstream's real Wars Sync client, against tools/sim/fake_web.py."""
import sys
import time
from types import SimpleNamespace

from tools.sim.core import check, check_equal, scenario
from tools.sim.fake_web import PAIR_KEY, FakeWeb
from tools.sim.scenarios_uploads import WarsRig

FRAME_LIMIT = 0.25


class PortalRig:
    def __init__(self, ctx, key="sim-key-not-real", auto_map=False, fix=None):
        import plugins.ng_wdgwars as ng_wdgwars
        import test_v3_ng_plugins as ng
        self.module, self.ng = ng_wdgwars, ng
        self.wars = WarsRig(ctx, sessions=())
        self.web = self.wars.web
        self.portal = self.wars.new_plugin(key=key)
        self.saved = []                                   # (name, value length): never the value
        self.portal._save_secret = lambda name, value: self.saved.append((name, len(value)))
        self.portal._start_auth_poll = lambda: None
        self.canvas = ng.Canvas()
        old = sys.modules.get("pyxel")
        sys.modules["pyxel"] = self.canvas
        ctx.cleanup(self._restore, old)
        ctx.patch(ng_wdgwars, "STATE_FIRST_DELAY", 0.0)
        ctx.patch(ng_wdgwars, "STATE_INTERVAL", 0.4)
        ctx.patch(ng_wdgwars, "STATE_MAX_BACKOFF", 1.2)
        ctx.patch(ng_wdgwars, "PAIR_POLL_SECONDS", 0.1)
        self.hud, self.term = [], []
        self.plugin = ng_wdgwars.NgWdgWars()
        self.app = SimpleNamespace(
            _plugins=[self.portal, self.plugin], _esc_consumed_frame=-1,
            _auto_map=SimpleNamespace(enabled=auto_map),
            gps=SimpleNamespace(wardrive_sample=lambda: fix),
            msg=lambda text, colour=7: self.hud.append(text),
            _term_add=lambda text, raw=False: self.term.append(text))
        self.plugin.on_load(self.app)
        ctx.cleanup(self.plugin.on_unload)
        self.frame_times = []

    @staticmethod
    def _restore(old):
        if old is None:
            sys.modules.pop("pyxel", None)
        else:
            sys.modules["pyxel"] = old

    def frame(self, *keys):
        self.canvas.pressed = set(keys)
        started = time.perf_counter()
        self.plugin.on_update()
        self.canvas.new_frame()
        self.plugin.draw(0, 0, self.ng.W, self.ng.H)
        self.plugin.draw_map_overlay(self.canvas, self.ng.W, 16, 234)
        self.frame_times.append(time.perf_counter() - started)
        check_equal(self.canvas.overlaps(), [], "texts that run into each other")
        self.canvas.pressed = set()

    def until(self, predicate, timeout=10.0):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            self.frame()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    def run(self, seconds):
        self.until(lambda: False, timeout=seconds)

    def text(self):
        return "\n".join(self.canvas.texts)

    def everything_shown(self):
        return "\n".join(self.term + self.hud) + "\n" + self.text()


@scenario("portal", "pairing by code")
def pairing(ctx):
    rig = PortalRig(ctx, key="")
    rig.plugin.open_overlay()
    rig.frame()
    check("not set -- press P to pair" in rig.text(), "the screen does not say that no key is set")
    check_equal(rig.web.log, [], "requests before anything was asked (no key: no state reads)")
    rig.frame("KEY_P")
    check(rig.until(lambda: "WDG-4821" in rig.text()), "the pairing code never appeared")
    check("Expires in" in rig.text(), "no countdown for the code")
    check(rig.until(lambda: rig.plugin._pair is None), "pairing never finished")
    check_equal(rig.saved, [("WARDRIVE_API_KEY", 64)], "key stored through C5's own adopt_api_key()")
    check_equal(rig.portal._api_key, PAIR_KEY, "key in use by the one portal client")
    check("Paired as sim-player" in rig.text(), "no 'paired' status")
    check(any("Paired as sim-player" in line for line in rig.hud), "no HUD message")
    check(PAIR_KEY not in rig.everything_shown(), "THE API KEY WAS SHOWN OR LOGGED")
    check_equal(rig.web.pair["requests"], 1, "pairing requests")
    check(rig.until(lambda: rig.plugin._summary is not None), "no state read after pairing")
    check("Player" in rig.text() and "sim-player   level 7" in rig.text(), "account not shown after pairing")
    # a key is set now: pairing again asks first
    rig.frame("KEY_P")
    check("A KEY IS ALREADY SET" in rig.text(), "no confirmation before replacing a key")
    rig.frame("KEY_N")
    check_equal(rig.web.pair["requests"], 1, "pairing requests after answering No")
    check(max(rig.frame_times) < FRAME_LIMIT, "the game loop stalled for %.2f s" % max(rig.frame_times))


@scenario("portal", "pairing: offline, cancelled, expired")
def pairing_faults(ctx):
    rig = PortalRig(ctx, key="")
    rig.plugin.open_overlay()
    rig.web.down()
    rig.frame("KEY_P")
    check(rig.until(lambda: rig.plugin._pair is None and "gave no pairing code" in rig.text()),
          "no message when the portal cannot be reached")
    rig.web.up()
    rig.web.pair["pending"] = 10 ** 6                      # nobody types the code
    rig.frame("KEY_P")
    check(rig.until(lambda: "WDG-4821" in rig.text()), "no code")
    rig.frame("KEY_X")
    check(rig.until(lambda: rig.plugin._pair is None), "X did not cancel the pairing")
    check("Pairing cancelled" in rig.text(), "no 'cancelled' status")
    polls = rig.web.pair["polls"]
    rig.run(0.5)
    check_equal(rig.web.pair["polls"], polls, "polls after cancelling")
    rig.web.pair["expires_in"] = 30
    rig.frame("KEY_P")
    check(rig.until(lambda: rig.plugin._pair is not None and rig.plugin._pair["state"] == "waiting"), "no code")
    rig.plugin._pair["expires"] = time.monotonic() - 1     # the code's lifetime is over
    check(rig.until(lambda: rig.plugin._pair is None), "an expired code kept being polled")
    check("expired" in rig.text(), "no 'expired' status")
    rig.web.script("pair", "html")
    rig.frame("KEY_P")
    check(rig.until(lambda: rig.plugin._pair is None and "gave no pairing code" in rig.text()),
          "a login page instead of a code was not handled")
    check_equal(rig.saved, [], "a key was stored although no pairing completed")
    check_equal(rig.portal._api_key, "", "the client's key changed although no pairing completed")
    rig.frame("KEY_P")
    rig.until(lambda: rig.plugin._pair is not None)
    rig.frame("KEY_ESCAPE")
    check(rig.until(lambda: rig.plugin._pair is None), "ESC left a pairing running in the background")


@scenario("portal", "rank / team / turf: one read a minute, quiet when offline")
def state_polling(ctx):
    rig = PortalRig(ctx)
    check(rig.until(lambda: rig.plugin._summary is not None), "the portal state was never read")
    check_equal(rig.term, ["[WDGWars] sim-player lvl 7 -- uConsole rank #12 of 340"], "terminal lines")
    check_equal(rig.plugin.chip_text(), "WDGWars #12 Blackout", "map chip")
    rig.run(1.5)
    reads = len(rig.web.state_requests)
    check(2 <= reads <= 6, "state reads in ~2 s at one per 0.4 s: %d" % reads)
    check_equal(len(rig.term), 1, "terminal lines while everything works (the first read only)")
    check_equal(rig.web.max_inflight["state"], 1, "state reads in flight at once")
    check(all(query == "" for query in rig.web.state_requests),
          "a position was sent although Auto Map + Turf is off: %s" % rig.web.state_requests[:3])
    # offline: silent, slower and slower, and the last state stays on screen
    rig.web.down()
    before = len(rig.term) + len(rig.hud)
    rig.run(2.5)
    check_equal(len(rig.term) + len(rig.hud), before, "messages while the portal is unreachable")
    check(rig.plugin._failures >= 1, "failures were not counted")
    check_equal(rig.plugin.chip_text(), "WDGWars #12 Blackout", "the chip while offline (last known state)")
    rig.plugin.open_overlay()
    rig.frame()
    check("not answered since" in rig.text() or "not answering" in rig.text(), "the screen does not say offline")
    rig.web.up()
    for behaviour in ("html", "empty", ("status", 500), "redirect"):
        rig.web.script("state", behaviour)
    failures = rig.plugin._failures
    check(rig.until(lambda: rig.plugin._failures == 0, timeout=15), "no recovery after the portal came back")
    check(failures >= 1, "non-JSON replies were not treated as 'not yet'")
    check_equal(len(rig.term), 1, "terminal lines after the recovery")
    check(max(rig.frame_times) < FRAME_LIMIT, "the game loop stalled for %.2f s" % max(rig.frame_times))


@scenario("portal", "never two requests at once; position only with Auto Map + Turf")
def one_client(ctx):
    fix = dict(lat=50.061234567, lon=19.938765432)
    rig = PortalRig(ctx, auto_map=True, fix=fix)
    rig.web.state["decay_nearby"] = [{"critical": True}, {"vulnerable": True}, {}, {}]
    check(rig.until(lambda: rig.plugin._summary is not None), "no state read")
    check_equal(rig.web.state_requests[0], "lat=50.061235&lng=19.938765", "position sent with Auto Map + Turf on")
    check_equal(rig.plugin.chip_text(), "WDGWars #12 Blackout turf 4 (2 at risk)", "chip with territory")
    rig.plugin.open_overlay()
    rig.frame()
    check("4 tile(s) held by your team; 1 critical, 1 vulnerable" in rig.text(), "territory line")
    # Wars Sync is uploading: the state reads wait their turn
    rig.portal._uploading = True
    reads = len(rig.web.state_requests)
    rig.run(1.2)
    check(len(rig.web.state_requests) <= reads + 1, "state reads while Wars Sync was uploading")
    reads = len(rig.web.state_requests)
    rig.run(0.6)
    check_equal(len(rig.web.state_requests), reads, "state reads while Wars Sync was uploading")
    rig.portal._uploading = False
    check(rig.until(lambda: len(rig.web.state_requests) > reads), "state reads did not resume after the upload")
    # pairing in progress: no state reads either
    rig.web.pair["pending"] = 10 ** 6
    rig.frame("KEY_P")
    rig.frame("KEY_Y")
    check(rig.until(lambda: rig.plugin._pair is not None and rig.plugin._pair["state"] == "waiting"), "no code")
    reads = len(rig.web.state_requests)
    rig.run(1.0)
    check_equal(len(rig.web.state_requests), reads, "state reads during a pairing")
    rig.frame("KEY_X")
    rig.until(lambda: rig.plugin._pair is None)
    check_equal(rig.web.max_inflight["state"], 1, "state reads in flight at once")
    for method, path, _endpoint in rig.web.log:
        check(path in ("/api/game-state", "/api/device-pair"),
              "this plugin caused a request it should not make: %s %s" % (method, path))
    # no Wars Sync plugin at all: the screen explains, nothing crashes
    rig.app._plugins = [rig.plugin]
    rig.frame("KEY_P")
    check("NOT LOADED" in rig.text(), "a missing portal client was not explained")
    rig.frame("KEY_H")
    check(rig.plugin.chip_text() is None, "H did not switch the map chip off")
