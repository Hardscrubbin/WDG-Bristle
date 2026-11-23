"""[merge-v3] Kismet scenarios: the real bridge plugin and the real LootManager
against tools/sim/fake_kismet.py (REST API, process, adapter layer)."""
import json
import os
import socket
import threading
import time

from tools.sim.core import check, check_equal, scenario
from tools.sim.fake_kismet import CH_24, CH_5, CH_6, KismetRig

ALFA = KismetRig.adapter("wlan1", "00:c0:ca:00:00:01", "Alfa AWUS036AXML (mt7921u)")
AC1200 = KismetRig.adapter("wlan2", "00:e0:4c:00:00:02", "AC1200 (rtl8812au)")
FRAME_LIMIT = 0.25     # a blocked game loop sits for seconds, not a quarter of one


def rig_for(ctx, **kwargs):
    from plugins import kismet_wardrive
    ctx.patch(kismet_wardrive.KismetWardriveBridge, "_crash_safety_installed", True)
    return KismetRig(ctx, **kwargs)


def aps(rig, count, start=0):
    for n in range(start, start + count):
        rig.server.add_ap("02:00:00:%02X:%02X:%02X" % (n >> 16 & 255, n >> 8 & 255, n & 255),
                          ssid="Net,%d" % n, channel=str(1 + n % 11), handshake=(n % 50 == 0))


def bts(rig, count, start=0):
    for n in range(start, start + count):
        rig.server.add_bt("06:00:00:%02X:%02X:%02X" % (n >> 16 & 255, n >> 8 & 255, n & 255), name="Tag %d" % n)


def assert_clean_csv(rig, wifi, ble):
    rows = rig.csv_rows()
    keys = [(row[0].upper(), row[10]) for row in rows]
    check_equal(len(keys), len(set(keys)), "wardriving.csv rows vs distinct devices (duplicates)")
    check_equal(sum(1 for row in rows if row[10] == "WIFI"), wifi, "Wi-Fi rows in wardriving.csv")
    check_equal(sum(1 for row in rows if row[10] == "BLE"), ble, "BLE rows in wardriving.csv")
    check(all(len(row) == 11 for row in rows), "a row does not have 11 columns (a comma got through)")
    check(not [row for row in rows if float(row[6]) == 0.0 and float(row[7]) == 0.0], "a 0,0 row was written")


@scenario("kismet", "normal capture and stop")
def normal(ctx):
    rig = rig_for(ctx)
    rig.server.add_source("wlan1", "wifi", CH_24 + CH_5)
    rig.server.add_source("hci1", "bt")
    aps(rig, 200)
    bts(rig, 80)
    rig.server.add_client("0a:00:00:00:00:01")
    check(rig.start() and rig.plugin._lifecycle == "running", "Kismet did not start: %s" % rig.term[-3:])
    check(rig.until(lambda: len(rig.csv_rows()) >= 280), "devices never reached wardriving.csv")
    aps(rig, 60, start=1000)                       # seen a moment before stop: the final poll's job
    check(rig.stop(), "stop did not finish")
    assert_clean_csv(rig, 260, 80)
    check_equal(rig.xp, 260 * 15 + 80 * 10, "XP for new devices (no repeat XP for one sighting each)")
    check(rig.handshakes >= 4, "handshake events were lost")
    check(("wlan1", False) in rig.monitor_calls, "the adapter was not put back to managed mode")
    check(rig.said("Kismet wardriving") or "Kismet wardriving stopped" in rig.hud, "no stop message")
    check(rig.max_frame() < FRAME_LIMIT, "the game loop stalled for %.2f s" % rig.max_frame())
    command = rig.launches[0]
    check("wlan1:channel_hop=true,retry=true" in command and "hci1:name=hci1_bt,retry=true" in command,
          "unexpected kismet command line: %s" % command)


@scenario("kismet", "a poll in flight when Stop is pressed loses nothing")
def stop_during_poll(ctx):
    """Found by one failed run of the scenario above (60 of 260 networks
    missing): a device poll that was still in flight when Stop was pressed
    finished, marked its devices as seen and was then thrown away, so the
    final poll skipped them. Forced here on every run: one poll of the poll
    thread has read its devices from Kismet but has not handed them to the
    game yet at the moment Stop is pressed."""
    rig = rig_for(ctx)
    rig.server.add_source("wlan1", "wifi", CH_24 + CH_5)
    aps(rig, 20)
    bts(rig, 5)
    check(rig.start() and rig.plugin._lifecycle == "running", "Kismet did not start: %s" % rig.term[-3:])
    check(rig.until(lambda: len(rig.csv_rows()) >= 25), "first devices never reached wardriving.csv")
    engine = rig.plugin.engine
    real_poll = engine.poll_networks
    armed, holding, release = threading.Event(), threading.Event(), threading.Event()
    ctx.cleanup(release.set)

    def poll(is_blocked=None):
        mine = armed.is_set() and threading.current_thread().name == "kismet-poll"
        if mine:
            armed.clear()                          # this poll, started after the new devices appeared
        result = real_poll(is_blocked=is_blocked)
        if mine:
            holding.set()                          # devices read and marked as seen, not handed over yet
            release.wait(20)
        return result
    ctx.patch(engine, "poll_networks", poll)
    aps(rig, 60, start=1000)                       # new networks: only that one poll will have them
    bts(rig, 10, start=1000)
    armed.set()
    check(rig.until(holding.is_set, timeout=10), "the poll thread never polled")
    rig.plugin.stop_wardrive()                     # Stop, with that poll still in flight
    check_equal(rig.plugin._lifecycle, "stopping", "state right after Stop")
    rig.frames(0.4)                                # the game keeps running meanwhile
    release.set()                                  # the poll thread carries on
    check(rig.until(lambda: rig.plugin._lifecycle == "idle"), "stop did not finish")
    assert_clean_csv(rig, 80, 15)
    check_equal(rig.xp, 80 * 15 + 15 * 10, "XP (every device once)")
    check(rig.max_frame() < FRAME_LIMIT, "the game loop stalled for %.2f s while stopping" % rig.max_frame())
    # the same at the next capture: nothing of the old one leaks in, nothing is counted twice
    aps(rig, 10, start=2000)
    check(rig.start(), "second start")
    check(rig.until(lambda: len(rig.csv_rows()) >= 105), "second capture: devices missing")
    check(rig.stop(), "second stop")
    assert_clean_csv(rig, 90, 15)


@scenario("kismet", "login refused (401), then accepted")
def refused_login(ctx):
    rig = rig_for(ctx)
    rig.server.add_source("wlan1", "wifi", CH_24)
    aps(rig, 20)
    check(rig.start(), "start")
    check(rig.until(lambda: len(rig.csv_rows()) >= 20), "first devices")
    rig.server.mode = "401"
    aps(rig, 30, start=500)                        # captured by Kismet while the game is locked out
    rig.frames(2.0)
    check_equal(len(rig.said("Kismet refused the login (HTTP 401)")), 1, "401 explanations in the terminal")
    check_equal(rig.hud.count("Kismet login refused (401) -- see terminal"), 1, "401 HUD messages")
    polls = rig.server.requests["devices"]
    check(polls >= 10, "the bridge stopped polling during the 401 (%d polls)" % polls)
    check(len(rig.said("HTTP 401")) <= 6, "the terminal was flooded: %d lines for %d failed polls"
          % (len(rig.said("HTTP 401")), polls))
    rig.server.mode = "ok"
    check(rig.until(lambda: len(rig.csv_rows()) >= 50), "devices seen during the 401 never arrived")
    check_equal(len(rig.said("Polling works again")), 1, "recovery lines")
    check(rig.stop(), "stop")
    assert_clean_csv(rig, 50, 0)


@scenario("kismet", "slow and broken responses")
def slow_responses(ctx):
    rig = rig_for(ctx)
    rig.server.add_source("wlan1", "wifi", CH_24)
    check(rig.start(), "start")
    rig.server.delay = 0.6
    aps(rig, 40)
    check(rig.until(lambda: len(rig.csv_rows()) >= 40, timeout=20), "devices never arrived over a slow API")
    check(rig.max_frame() < FRAME_LIMIT, "a slow Kismet stalled the game loop for %.2f s" % rig.max_frame())
    rig.server.delay = 0.0
    for mode in ("500", "garbage"):
        rig.server.mode = mode
        rig.frames(0.8)
    rig.server.mode = "ok"
    aps(rig, 10, start=900)
    check(rig.until(lambda: len(rig.csv_rows()) >= 50), "no recovery after HTTP 500 / broken JSON")
    check(rig.said("HTTP 500") and rig.said("bad JSON"), "the failures were not reported")
    check(rig.stop(), "stop")
    assert_clean_csv(rig, 50, 0)


@scenario("kismet", "no last-time endpoint (old Kismet)")
def no_delta_endpoint(ctx):
    rig = rig_for(ctx)
    rig.server.delta = False
    rig.server.add_source("wlan1", "wifi", CH_24)
    aps(rig, 30)
    check(rig.start(), "start")
    check(rig.until(lambda: len(rig.csv_rows()) >= 30), "first devices")
    aps(rig, 15, start=300)
    check(rig.until(lambda: len(rig.csv_rows()) >= 45), "devices after the fallback to full polls")
    check_equal(len(rig.said("no last-time endpoint")), 1, "fallback notices")
    rig.frames(0.5)
    check_equal(rig.xp, 45 * 15, "XP (a full poll must not pay repeat XP for unchanged devices)")
    check(rig.stop(), "stop")


@scenario("kismet", "Kismet crashes mid-capture")
def crash_mid_capture(ctx):
    rig = rig_for(ctx)
    rig.server.add_source("wlan1", "wifi", CH_24)
    aps(rig, 50)
    check(rig.start(), "start")
    check(rig.until(lambda: len(rig.csv_rows()) >= 50), "first devices")
    rig.procs[-1].crash()
    check(rig.until(lambda: len(rig.procs) == 2 and rig.plugin._lifecycle == "running"),
          "Kismet was not restarted after it died: %s" % rig.term[-4:])
    check_equal(len(rig.said("Kismet exited by itself (code -11) -- restarting")), 1, "restart lines")
    check("Kismet crashed -- restarting it" in rig.hud, "no HUD message about the crash")
    aps(rig, 25, start=700)
    check(rig.until(lambda: len(rig.csv_rows()) >= 75), "nothing captured after the restart")
    check(("wlan1", False) in rig.monitor_calls and rig.monitor_calls[-1] == ("wlan1", True),
          "the adapter was not re-prepared for the restarted Kismet")
    for _ in range(3):                             # it keeps dying: three more times within the window
        rig.procs[-1].crash()
        before = len(rig.procs)
        rig.until(lambda: len(rig.procs) > before and rig.plugin._lifecycle == "running"
                  or rig.plugin._lifecycle == "idle" and rig.said("not restarting again"), timeout=20)
    check(rig.until(lambda: rig.plugin._lifecycle == "idle"), "the bridge never gave up")
    check_equal(len(rig.said("not restarting again")), 1, "'gave up' lines")
    check_equal(len(rig.procs), 4, "Kismet launches (1 + 3 restarts)")
    check(rig.monitor_calls[-1] == ("wlan1", False), "the adapter was left in monitor mode")
    assert_clean_csv(rig, 75, 0)
    check(rig.max_frame() < FRAME_LIMIT, "the game loop stalled for %.2f s" % rig.max_frame())


@scenario("kismet", "adapter vanishes (USB unplug) and returns")
def adapter_vanishes(ctx):
    rig = rig_for(ctx, wifi=[ALFA, AC1200])
    rig.server.add_source("wlan1", "wifi", CH_24 + CH_5 + CH_6)
    rig.server.add_source("wlan2", "wifi", CH_24 + CH_5)
    check(rig.start(), "start")
    check(rig.until(lambda: rig.said("Channels split between the adapters")), "no split with two adapters")
    rig.server.set_source("wlan2", False, "interface wlan2 vanished")
    check(rig.until(lambda: rig.said("wlan2 stopped capturing")), "the lost adapter was not reported")
    rig.frames(1.0)
    check_equal(len(rig.said("wlan2 stopped capturing")), 1, "'stopped capturing' lines")
    check(any("unplugged?" in line for line in rig.hud), "no HUD warning for the lost adapter")
    check(rig.until(lambda: sorted(rig.server.source("wlan1")["kismet.datasource.hop_channels"])
                    == sorted(CH_24 + CH_5 + CH_6)),
          "the surviving adapter was not given all of its channels back")
    aps(rig, 20)
    check(rig.until(lambda: len(rig.csv_rows()) >= 20), "capture stopped when one adapter went away")
    rig.server.set_source("wlan2", True)
    check(rig.until(lambda: rig.said("wlan2 is capturing again")), "the returning adapter was not reported")
    check(rig.until(lambda: len(rig.said("Channels split between the adapters")) == 2),
          "the split was not made again when the adapter returned")
    rig.server.set_source("wlan1", False, "gone")
    rig.server.set_source("wlan2", False, "gone")
    check(rig.until(lambda: rig.said("No adapter is capturing right now")), "all adapters down was not reported")
    check(rig.stop(), "stop")


@scenario("kismet", "both adapters: channels are split")
def both_adapters(ctx):
    rig = rig_for(ctx, wifi=[ALFA, AC1200])
    alfa = rig.server.add_source("wlan1", "wifi", CH_24 + CH_5 + CH_6)
    ac = rig.server.add_source("wlan2", "wifi", CH_24 + CH_5)
    check(rig.start(), "start")
    check(len([arg for arg in rig.launches[0] if arg.endswith("channel_hop=true,retry=true")]) == 2,
          "Kismet was not started with both adapters: %s" % rig.launches[0])
    check(rig.until(lambda: len(rig.server.set_channel_calls) >= 2), "no channel split was sent to Kismet")
    plan = dict(rig.server.set_channel_calls)
    check_equal(sorted(plan[ac]["channels"]), sorted(CH_24), "AC1200 channels (2.4 GHz only)")
    check_equal(sorted(plan[alfa]["channels"]), sorted(CH_5 + CH_6), "Alfa channels (5 and 6 GHz)")
    check_equal(sorted(plan[ac]["channels"] + plan[alfa]["channels"]), sorted(CH_24 + CH_5 + CH_6),
                "channels covered after the split (nothing dropped)")
    rig.frames(0.6)
    check_equal(len(rig.server.set_channel_calls), 2, "split commands (must not be re-sent every check)")
    prefs = json.load(open(os.path.join(ctx.tmp, "kismet_adapters.json"), encoding="utf-8"))
    check_equal(prefs["wifi_macs"], sorted([ALFA["mac"], AC1200["mac"]]), "MAC adapter preferences")
    check(rig.stop(), "stop")
    check_equal(sorted(rig.monitor_calls[-2:]), [("wlan1", False), ("wlan2", False)], "adapters restored")


@scenario("kismet", "channel split refused or switched off")
def split_refused(ctx):
    rig = rig_for(ctx, wifi=[ALFA, AC1200])
    rig.server.readonly = True
    rig.server.add_source("wlan1", "wifi", CH_24 + CH_5 + CH_6)
    rig.server.add_source("wlan2", "wifi", CH_24 + CH_5)
    aps(rig, 10)
    check(rig.start(), "start")
    check(rig.until(lambda: rig.said("Channel split not applied")), "a refused split was not reported")
    check(rig.until(lambda: len(rig.csv_rows()) >= 10), "capture must carry on without the split")
    rig.frames(0.6)
    check_equal(len(rig.said("Channel split not applied")), 1, "'not applied' lines")
    check(rig.stop(), "stop")
    off = rig_for(ctx, wifi=[ALFA, AC1200])
    off.plugin._split_mode = "off"
    off.server.add_source("wlan1", "wifi", CH_24 + CH_5)
    off.server.add_source("wlan2", "wifi", CH_24 + CH_5)
    check(off.start(), "start with the split off")
    off.frames(0.8)
    check_equal(off.server.set_channel_calls, [], "split commands with the split switched off")
    check(off.stop(), "stop")


@scenario("kismet", "monitor mode fails on one adapter")
def monitor_fails_on_one(ctx):
    rig = rig_for(ctx, wifi=[ALFA, AC1200], monitor_fails=["wlan2"])
    rig.server.add_source("wlan1", "wifi", CH_24 + CH_5)
    aps(rig, 15)
    check(rig.start(), "start")
    command = rig.launches[0]
    check(any(arg.startswith("wlan1:") for arg in command), "the working adapter was not used")
    check(not any(arg.startswith("wlan2:") for arg in command), "the failed adapter was handed to Kismet")
    check(rig.said("leaving wlan2 out of this run"), "the player was not told which adapter was left out")
    check(rig.until(lambda: len(rig.csv_rows()) >= 15), "no capture on the remaining adapter")
    check(rig.stop(), "stop")
    check(("wlan2", False) not in rig.monitor_calls[2:], "an adapter never switched was 'restored'")
    both = rig_for(ctx, wifi=[ALFA], ble=[], monitor_fails=["wlan1"])
    check(both.start() or both.plugin._lifecycle == "idle", "start with no usable adapter hung")
    check(both.until(lambda: both.plugin._lifecycle == "idle"), "lifecycle after a start with no adapter")
    check(both.said("No usable adapters after setup"), "no message when every adapter failed")
    check_equal(both.launches, [], "Kismet launches with no usable adapter")


@scenario("kismet", "GPS dropout holds records, then writes them")
def gps_dropout(ctx):
    rig = rig_for(ctx)
    rig.server.add_source("wlan1", "wifi", CH_24)
    check(rig.start(), "start")
    aps(rig, 20)
    check(rig.until(lambda: len(rig.csv_rows()) >= 20), "first devices")
    rig.app.gps_fix = False
    rig.position.fix.valid = False
    aps(rig, 30, start=400)
    bts(rig, 10)
    check(rig.until(lambda: len(rig.plugin._held_networks) == 30 and len(rig.plugin._held_bt) == 10),
          "devices seen without a fix were not held")
    check_equal(len(rig.csv_rows()), 20, "rows written while there was no fix")
    rig.app.gps_fix = True
    rig.position.fix.valid = True
    check(rig.until(lambda: len(rig.csv_rows()) >= 60), "held records were not written when the fix returned")
    check(rig.said("GPS fix acquired -- writing 30 WiFi + 10 BLE held record(s)"), "no message for the flush")
    check(rig.stop(), "stop")
    assert_clean_csv(rig, 50, 10)


@scenario("kismet", "two GPS receivers: Kismet reads only the one in use")
def kismet_gps_filter(ctx):
    from plugins import kismet_wardrive
    from tools.sim.fake_gpsd import FakeGpsd
    gpsd = FakeGpsd()
    gpsd.add("/dev/ttyAMA0", lat=50.0).add("/dev/ttyACM0", lat=60.0)
    gpsd.start()
    ctx.cleanup(gpsd.stop)
    chosen = {"path": "/dev/ttyAMA0"}
    rig = rig_for(ctx)
    ctx.patch(kismet_wardrive, "KISMET_GPSD_ENABLED", True)
    ctx.patch(kismet_wardrive, "GPSD_HOST", "127.0.0.1")
    ctx.patch(kismet_wardrive, "GPSD_PORT", gpsd.port)
    ctx.patch(kismet_wardrive, "KISMET_GPS_PROXY_PORT", 0)
    ctx.patch(kismet_wardrive, "_gpsd_reachable", lambda timeout=0.5: True)
    rig.position.health = lambda: dict(devices=[{}, {}], configured=[])
    rig.position.chosen_device = chosen["path"]
    rig.server.add_source("wlan1", "wifi", CH_24)
    check(rig.start(), "start")
    proxy = rig.plugin._gps_proxy
    check(proxy is not None and proxy.running, "no GPS filter with two receivers")
    override = open(os.path.join(rig.plugin.engine.log_dir, "wdg_gps_override.conf")).read()
    check_equal(override, "gps=gpsd:host=127.0.0.1,port=%d,reconnect=true\n" % proxy.port,
                "Kismet's GPS override file")
    client = socket.create_connection(("127.0.0.1", proxy.port), timeout=3)
    ctx.cleanup(client.close)
    client.settimeout(3)
    check(b'"VERSION"' in client.recv(4096), "the filter did not greet like gpsd")
    client.sendall(b'?WATCH={"json":true};\n')
    check(gpsd.wait_clients(1), "the filter never connected to gpsd")

    def read_reports(seconds=0.6):
        client.settimeout(0.1)
        data, deadline = b"", time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            try:
                data += client.recv(65536)
            except socket.timeout:
                pass
        return [json.loads(line) for line in data.splitlines() if line.startswith(b'{"class": "TPV"')
                or line.startswith(b'{"class":"TPV"')]
    time.sleep(0.2)
    for _ in range(5):
        gpsd.emit()
    seen = read_reports()
    check(seen and {r["device"] for r in seen} == {"/dev/ttyAMA0"},
          "Kismet was sent the other receiver's positions: %s" % {r.get("device") for r in seen})
    rig.position.chosen_device = "/dev/ttyACM0"          # the game switches receiver
    for _ in range(5):
        gpsd.emit()
    seen = read_reports()
    check(seen and {r["device"] for r in seen} == {"/dev/ttyACM0"} and seen[-1]["lat"] == 60.0,
          "the filter did not follow the game's switch")
    gpsd.restart()                                       # gpsd restarts under the filter
    check(gpsd.wait_clients(1, timeout=8), "the filter did not reconnect to gpsd")
    time.sleep(0.2)
    for _ in range(3):
        gpsd.emit()
    check(read_reports(), "no positions through the filter after gpsd restarted")
    check(rig.stop(), "stop")
    check(not proxy.running and rig.plugin._gps_proxy is None, "the filter outlived the capture")
    # one receiver: straight to gpsd, exactly as v2
    os.unlink(os.path.join(rig.plugin.engine.log_dir, "wdg_gps_override.conf"))
    single = rig_for(ctx)
    ctx.patch(kismet_wardrive, "KISMET_GPSD_ENABLED", True)
    single.position.health = lambda: dict(devices=[{}], configured=[])
    single.server.add_source("wlan1", "wifi", CH_24)
    check(single.start(), "start with one receiver")
    override = open(os.path.join(single.plugin.engine.log_dir, "wdg_gps_override.conf")).read()
    check_equal(override, "gps=gpsd:host=127.0.0.1,port=%d,reconnect=true\n" % gpsd.port,
                "override with one receiver (must point at gpsd itself)")
    check(single.plugin._gps_proxy is None, "a filter was started for a single receiver")
    check(single.stop(), "stop")


@scenario("kismet", "AC1200 on the AIO internal USB: rail is powered first")
def internal_usb_rail(ctx):
    from watchdogs import aio_manager
    rig = rig_for(ctx, wifi=[ALFA], aio=True)
    rail = {"usb": False}
    writes = []

    def toggle(feature, on):
        writes.append((feature, on))
        rail[feature] = on
        return True
    rig.app._aio_power = type("Power", (), {"toggle": staticmethod(toggle)})()
    ctx.patch(aio_manager.AioManager, "get_feature_state", staticmethod(lambda feature: rail.get(feature)))
    rig.plugin.start_wardrive()
    check_equal(writes, [("usb", True)], "rail writes when the picker opened")
    check(rig.app._usb_enabled, "the game was not told the USB rail is on")
    check(rig.said("AIO internal USB rail (GPIO 23) switched on"), "no message about the rail")
    check_equal([w["id"] for w in rig.plugin._wifi_all], ["wlan1"], "adapters before the AC1200 enumerates")
    rig.wifi.append(AC1200)                               # it enumerates a few seconds later
    rig.plugin._rescan_due = [time.time() + 0.1]
    check(rig.until(lambda: len(rig.plugin._wifi_all) == 2, timeout=5),
          "the picker did not rescan by itself after powering the rail")
    rig.plugin._overlay_active = False
    rig.plugin.start_wardrive()                           # rail already on: nothing is written
    check_equal(writes, [("usb", True)], "rail writes when it was already on")
    rail["usb"] = None                                    # unreadable: leave it alone
    rig.plugin._overlay_active = False
    rig.plugin.start_wardrive()
    check_equal(writes, [("usb", True)], "rail writes when its state could not be read")
