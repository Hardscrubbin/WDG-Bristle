"""[merge-v3] LoRa scenarios: upstream's real protocol-switch methods (taken from
app.py the way upstream's own tests do) over stand-in radios and a recording AIO
rail. One SX1262, one owner at a time; a missing chip is reported truthfully."""
from types import SimpleNamespace

from tools.sim.core import check, check_equal, scenario

METHODS = ("_active_lora_protocol", "_stop_radio_owner", "_switch_lora_protocol", "_toggle_lora",
           "_update_lora_protocol_picker")


class Radio:
    """Stand-in for LoRaManager / MeshtasticManager."""

    def __init__(self, name, log, chip=True, stop_ok=True):
        self.name, self.log, self.chip, self.stop_ok = name, log, chip, stop_ok
        self.running = self.ready = False
        self.mode = ""

    def _start(self, profile):
        self.log.append(("start", self.name))
        if not self.chip:
            return False                      # the SX1262 did not answer
        self.running = self.ready = True
        self.mode = self.name
        return True

    start = start_meshcore = _start

    def stop(self):
        self.log.append(("stop", self.name))
        if not self.stop_ok:
            return False                      # the worker did not release the radio in time
        self.running = self.ready = False
        return True

    def set_mc_channels(self, *_args):
        pass

    set_mc_active_channel = send_meshcore_advert = set_mc_channels


class Rails:
    """A HackerGadgets AIO v2 as the real AioPowerSession sees it."""

    def __init__(self, fail=()):
        self.level = dict(gps=False, lora=False, sdr=False, usb=True)
        self.fail = set(fail)

    def get_feature_state(self, feature):
        return self.level[feature]

    def toggle(self, feature, on):
        if feature in self.fail:
            return False
        self.level[feature] = on
        return True


class LoraRig:
    def __init__(self, chip=True, stop_ok=True, rail_fail=()):
        import test_v3_ng_plugins as ng
        from watchdogs.aio_manager import AioPowerSession
        self.log, self.hud = [], []
        self.pyxel = SimpleNamespace(KEY_LEFT="L", KEY_RIGHT="R", KEY_ESCAPE="E", KEY_RETURN="RET",
                                     frame_count=1, pressed=set())
        self.pyxel.btnp = lambda key, *a: key in self.pyxel.pressed
        game = ng.app_methods(*METHODS, pyxel=self.pyxel)()
        self.rails = Rails(rail_fail)
        game._aio_available = True
        game._aio_power = AioPowerSession(self.rails)
        game._lora = Radio("meshcore", self.log, chip, stop_ok)
        game._mt = Radio("meshtastic", self.log, chip, stop_ok)
        game._wd = None
        game._lora_enabled = False
        game._lora_protocol_picker = False
        game._lora_protocol_sel = 1
        game._radio_protocol = ""
        game._rf_profiles = None
        game._mt_profile, game._mc_region = "EU_868", "EU"
        game._mc_channels_list, game._mc_active_ch, game._mc_node_name = [], 0, "sim"
        game._mc_screen = game._mt_screen = False
        game._mc_bubbles = []
        game.menu_open = True
        game.glitch_timer = 0
        game.gps_fix, game.player_lat, game.player_lon = False, 0.0, 0.0
        game.state = SimpleNamespace(lora_running=False, lora_mode="")
        game.msg = lambda text, colour=7: self.hud.append(text)
        game._esc_consumed_frame = -1
        self.game = game

    def pick(self, protocol):
        """SYSTEM > LoRa > pick a protocol > ENTER."""
        self.game._toggle_lora() if not self.game._lora_enabled else None
        self.game._lora_protocol_picker = True
        self.game._lora_protocol_sel = 0 if protocol == "meshtastic" else 1
        self.pyxel.pressed = {"RET"}
        self.game._update_lora_protocol_picker()
        self.pyxel.pressed = set()

    def owners(self):
        return [radio.name for radio in (self.game._lora, self.game._mt) if radio.running]


@scenario("lora", "MeshCore <-> Meshtastic handover: one owner at a time")
def handover(ctx):
    rig = LoraRig()
    rig.pick("meshcore")
    check_equal(rig.owners(), ["meshcore"], "radio owner after choosing MeshCore")
    check(rig.rails.level["lora"], "the LoRa rail (GPIO 16) was not switched on")
    check_equal(rig.game._active_lora_protocol(), "meshcore", "protocol the HUD reports")
    for target in ("meshtastic", "meshcore", "meshtastic"):
        before = len(rig.log)
        check(rig.game._switch_lora_protocol(target), "switch to %s failed: %s" % (target, rig.hud[-2:]))
        steps = rig.log[before:]
        other = "meshcore" if target == "meshtastic" else "meshtastic"
        check_equal(steps, [("stop", other), ("start", target)], "order of the handover to %s" % target)
        check_equal(rig.owners(), [target], "radio owners after the handover")
        check_equal(rig.game._active_lora_protocol(), target, "protocol the HUD reports")
    before = len(rig.log)
    check(rig.game._switch_lora_protocol("meshtastic"), "re-selecting the active protocol")
    check_equal(rig.log[before:], [], "radio calls when the protocol is already active")
    rig.game._toggle_lora()                                  # LoRa OFF
    check_equal(rig.owners(), [], "radio owners after LoRa OFF")
    check(not rig.rails.level["lora"] and not rig.game._lora_enabled, "the rail stayed on after LoRa OFF")
    check("[LoRa] OFF" in rig.hud, "no OFF message")


@scenario("lora", "chip absent")
def chip_absent(ctx):
    rig = LoraRig(chip=False)
    rig.pick("meshcore")
    check("[LoRa] Radio initialization failed" in rig.hud, "a silent radio was not reported: %s" % rig.hud)
    check_equal(rig.owners(), [], "radio owners with no chip")
    check_equal(rig.game._active_lora_protocol(), "", "protocol the HUD reports with no chip")
    check(not rig.game._lora_enabled, "LoRa shown as enabled although the chip never answered")
    check(not rig.rails.level["lora"], "the rail was left on after a failed start it switched on itself")
    check(not rig.game.state.lora_running, "state says LoRa is running")
    rig.pick("meshtastic")
    check_equal(rig.hud.count("[LoRa] Radio initialization failed"), 2, "failure messages for two attempts")
    check(not rig.rails.level["lora"], "the rail after the second failed start")
    # the chip comes back (rail reseated): the next attempt works
    rig.game._lora.chip = rig.game._mt.chip = True
    rig.pick("meshcore")
    check_equal(rig.owners(), ["meshcore"], "radio owner once the chip answers")


@scenario("lora", "a stop that does not finish keeps ownership")
def stop_pending(ctx):
    rig = LoraRig()
    rig.pick("meshcore")
    rig.game._lora.stop_ok = False                           # the MeshCore worker is stuck
    check(not rig.game._switch_lora_protocol("meshtastic"), "switched although MeshCore never released the radio")
    check("[LoRa] Radio stop pending; cannot switch" in rig.hud, "no message: %s" % rig.hud[-2:])
    check_equal(rig.owners(), ["meshcore"], "radio owners (a second owner must never start)")
    check(("start", "meshtastic") not in rig.log, "Meshtastic was started on a radio still owned by MeshCore")
    rig.game._toggle_lora()
    check(rig.rails.level["lora"], "the rail was cut under a radio that is still owned")
    rig.game._lora.stop_ok = True                            # the worker lets go
    check(rig.game._switch_lora_protocol("meshtastic"), "no recovery once the stop went through")
    check_equal(rig.owners(), ["meshtastic"], "radio owners after recovery")
    failing = LoraRig(rail_fail=["lora"])
    failing.pick("meshcore")
    check("[LoRa] GPIO toggle failed" in failing.hud, "a failed rail write was not reported")
    check_equal(failing.owners(), [], "a radio was started although its rail could not be switched on")
