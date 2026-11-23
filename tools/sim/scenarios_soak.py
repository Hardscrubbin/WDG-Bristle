"""[merge-v3] Soak: a simulated 8-hour drive at accelerated time.

Real code under load, together: the Kismet bridge's device path
(_consume_batch -> the per-frame drain -> the real LootManager, fsync'd
appends and all), the real GpsManager with two receivers and scripted faults,
and the real WiGLE worker against a server that fails for half of every hour.

  22,000 unique devices (14,000 Wi-Fi + 8,000 BLE) over the first 6 hours,
  then 2 hours of re-sightings only -- that last stretch is where memory must
  stay flat: nothing may grow with time alone.
  14,400 Kismet polls (one per simulated 2 s), 3 game frames per poll.
  GPS faults: one receiver drops its fix every 20 min, both every hour, the
  dongle is unplugged for 5 min in hour 3, the antenna shorts in hour 5.
  A WiGLE upload every 30 min; the server answers 500 / 429 / a login page in
  the first half of each hour.

Reported: frame time p50 / p95 / max, traced Python memory per hour, process
memory at start and end, GPS switches, uploads. Run it with --soak.
"""
import os
import sys
import time
import tracemalloc
from pathlib import Path

from tools.sim.core import SimClock, check, check_equal, scenario
from tools.sim.fake_gpsd import nmea
from tools.sim.fake_kismet import KismetRig, _Forward
from tools.sim.fake_web import FakeWeb

HOURS = 8
POLL_SECONDS = 2
FRAMES_PER_POLL = 3
WIFI, BLE = 14_000, 8_000
DISCOVERY_HOURS = 6
AIO, USB = "/dev/ttyAMA0", "/dev/ttyACM0"


def process_memory_mb():
    """Resident memory of this process in MB, or None when it cannot be read."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/self/statm") as fh:
                return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return counters.WorkingSetSize / 1e6
    except Exception:
        pass
    return None


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


@scenario("soak", "8-hour drive: 22,000 devices, GPS switching, uploads failing and recovering",
          limit=3600, soak=True)
def eight_hour_drive(ctx):
    from plugins import kismet_wardrive as kismet
    from watchdogs import gps_manager as gm
    ctx.patch(kismet.KismetWardriveBridge, "_crash_safety_installed", True)
    rig = KismetRig(ctx)
    plugin, app = rig.plugin, rig.app
    counters = dict(hud=0, term=0)
    alerts = []

    def hud(text, colour=7):
        counters["hud"] += 1

    def term(text, raw=False):
        counters["term"] += 1
        if text.startswith("[GPS]"):
            alerts.append(text)
    app.msg, app._term_add = hud, term              # bounded: the real game keeps 8 / a scrollback

    # ---- GPS: the real manager, fed gpsd reports at 1 Hz of simulated time
    clock = SimClock(10_000.0)
    clock.install(ctx, gm)
    ctx.patch(gm, "GPS_ANTENNA_WATCH", False)
    gps = gm.GpsManager(device="never-open-sim-device")
    gps._using_gpsd = gps._available = True
    app.gps = gps
    rig.loot._gps = gps if hasattr(rig.loot, "_gps") else None
    for name in ("gps", "_gps_manager", "gps_manager"):
        if hasattr(rig.loot, name):
            setattr(rig.loot, name, gps)

    # ---- WiGLE: real worker, real HTTP, a server that fails half of every hour
    web = FakeWeb().up()
    web.keep_rows = False
    ctx.cleanup(web.down)
    ctx.patch(kismet, "WIGLE_API_URL", web.wigle_url)
    ctx.patch(kismet, "WIGLE_API_NAME", "sim-name")
    ctx.patch(kismet, "WIGLE_API_TOKEN", "sim-token")
    kismet.hw.__dict__["internet_available"] = lambda: True
    sim_epoch = [1_800_000_000.0]
    ctx.patch(kismet, "time", _Forward(time, time=lambda: sim_epoch[0], sleep=lambda s: time.sleep(min(s, 0.01))))

    engine = plugin.engine
    engine.running = True
    plugin._lifecycle = "running"
    plugin._session_start_time = plugin._segment_start_time = sim_epoch[0]
    plugin._gps_announced_this_session = False

    def make_wifi(n):
        return kismet.Network(index="0", ssid="Net %d" % n, vendor="", bssid="02:%02X:%02X:%02X:%02X:01"
                              % (n >> 24 & 255, n >> 16 & 255, n >> 8 & 255, n & 255),
                              channel=str(1 + n % 11), auth="WPA2", rssi=str(-40 - n % 50), band="2.4")

    def make_ble(n):
        return ("06:%02X:%02X:%02X:%02X:02" % (n >> 24 & 255, n >> 16 & 255, n >> 8 & 255, n & 255),
                -50 - n % 40, "Tag %d" % n)

    polls = HOURS * 3600 // POLL_SECONDS
    discovery_polls = DISCOVERY_HOURS * 3600 // POLL_SECONDS
    frame_times, hourly, switches_before = [], [], gps._multi.switches
    slow = []                                # (ms, simulated hour, frame in poll) of frames over 33 ms
    wifi_made = ble_made = 0
    recent_wifi, recent_ble = [], []
    state = {AIO: dict(mode=3, used=9, seen=12, hdop=1.0, plugged=True),
             USB: dict(mode=3, used=8, seen=11, hdop=1.1, plugged=True)}
    second = 0
    uploads_started = 0
    lat = 50.0

    def gps_epoch(sim_second):
        nonlocal lat
        minute = sim_second // 60
        in_hour = sim_second % 3600
        state[AIO]["mode"] = 1 if (minute % 20 == 5 and sim_second % 60 < 60) else 3
        if in_hour < 30:                                       # both lose the fix for 30 s every hour
            state[AIO]["mode"] = state[USB]["mode"] = 1
        else:
            state[USB]["mode"] = 3
        state[USB]["plugged"] = not (3 * 3600 + 600 <= sim_second < 3 * 3600 + 900)
        lat += 0.00002
        stamp = "2026-11-21T%02d:%02d:%02d.000Z" % (sim_second // 3600 % 24, sim_second // 60 % 60, sim_second % 60)
        for path, receiver in state.items():
            if not receiver["plugged"]:
                continue
            fixed = receiver["mode"] >= 2
            gps._apply_gpsd_report({"class": "SKY", "device": path, "hdop": receiver["hdop"],
                                    "satellites": [{"PRN": n, "ss": 30.0, "used": fixed and n < receiver["used"]}
                                                   for n in range(receiver["seen"])]})
            report = {"class": "TPV", "device": path, "mode": receiver["mode"], "time": stamp}
            if fixed:
                report.update(lat=lat + (0.0001 if path == USB else 0.0), lon=19.0)
            gps._apply_gpsd_report(report)
        if sim_second == 5 * 3600 + 120:
            gps._multi.feed_nmea(AIO, nmea("GPTXT,01,01,01,ANTENNA SHORT"), clock.now)
        if sim_second == 5 * 3600 + 300:
            gps._multi.feed_nmea(AIO, nmea("GPTXT,01,01,01,ANTENNA OK"), clock.now)
        gps._health_tick()
        for _level, text in gps.drain_health_events():
            term(text)
        app.gps_fix = bool(gps.fix.valid)
        if gps.fix.valid:
            app.player_lat, app.player_lon = gps.fix.latitude, gps.fix.longitude

    tracemalloc.start()
    memory_start = process_memory_mb()
    started = time.perf_counter()
    for poll in range(polls):
        sim_second = poll * POLL_SECONDS
        sim_epoch[0] = 1_800_000_000.0 + sim_second
        # ---- what Kismet reports for this poll
        networks, bts = [], []
        if poll < discovery_polls:
            want_wifi = (poll + 1) * WIFI // discovery_polls
            want_ble = (poll + 1) * BLE // discovery_polls
            while wifi_made < want_wifi:
                networks.append(make_wifi(wifi_made))
                wifi_made += 1
            while ble_made < want_ble:
                bts.append(make_ble(ble_made))
                ble_made += 1
        for k in range(12):                                     # re-sightings of devices nearby
            if wifi_made:
                networks.append(make_wifi((poll * 7 + k * 13) % wifi_made if poll >= discovery_polls
                                          else max(0, wifi_made - 1 - (k * 5) % min(wifi_made, 400))))
        for k in range(8):
            if ble_made:
                bts.append(make_ble((poll * 5 + k * 11) % ble_made if poll >= discovery_polls
                                    else max(0, ble_made - 1 - (k * 3) % min(ble_made, 300))))
        for item in networks:
            engine.seen_bssids.add(item.bssid)
        # ---- the game's frames for this poll
        for frame in range(FRAMES_PER_POLL):
            began = time.perf_counter()
            if frame == 0:
                for step in range(POLL_SECONDS):
                    clock.advance(1.0)
                    gps_epoch(sim_second + step)
                plugin._consume_batch(networks, bts, now=sim_epoch[0])
            else:
                plugin._drain_backlog(plugin.FRAME_BUDGET_S)
            plugin._drain_wigle_events()
            plugin._drain_kismet_events()
            took = time.perf_counter() - began
            frame_times.append(took)
            if took > 0.033:
                slow.append((round(took * 1000), round(sim_second / 3600, 2), frame))
        # ---- uploads: one every 30 min, a retry pass every minute
        if sim_second % 1800 == 900:
            in_hour = sim_second % 3600
            if in_hour < 1800:                                  # first half of each hour: WiGLE misbehaves
                web.script("wigle", [("status", 500), ("status", 429, {"Retry-After": "30"}), "html"][
                    (sim_second // 3600) % 3])
            plugin.upload_wigle()
            uploads_started += 1
        elif sim_second % 60 == 0:
            if sim_second % 600 == 0:
                plugin._wigle_not_before = 0.0                  # simulated minutes have passed
            plugin._retry_wigle_queue()
        if sim_second % 3600 == 3598:
            hourly.append(tracemalloc.get_traced_memory()[0] / 1e6)
    elapsed = time.perf_counter() - started

    # ---- the drive ends: stop, flush, last upload
    deadline = time.perf_counter() + 30
    while plugin._wigle_busy and time.perf_counter() < deadline:
        time.sleep(0.01)
    plugin._finish_stop()
    web._scripts["wigle"].clear()
    plugin._wigle_not_before = 0.0
    plugin._retry_wigle_queue()
    deadline = time.perf_counter() + 60
    while (plugin._wigle_busy or plugin._load_wigle_queue()) and time.perf_counter() < deadline:
        plugin._drain_wigle_events()
        if not plugin._wigle_busy:
            plugin._retry_wigle_queue()
        time.sleep(0.02)
    traced_end = tracemalloc.get_traced_memory()[0] / 1e6
    tracemalloc.stop()
    memory_end = process_memory_mb()

    # ---- results
    rows = rig.csv_rows()
    keys = [(row[0].upper(), row[10]) for row in rows]
    p50, p95, worst = (percentile(frame_times, 0.50) * 1000, percentile(frame_times, 0.95) * 1000,
                       max(frame_times) * 1000)
    steady_growth = hourly[-1] - hourly[DISCOVERY_HOURS - 1]
    switches = gps._multi.switches - switches_before
    for key, value in (("simulated hours", HOURS), ("wall seconds", round(elapsed, 1)),
                       ("frames", len(frame_times)), ("frame ms p50", round(p50, 3)),
                       ("frame ms p95", round(p95, 3)), ("frame ms max", round(worst, 1)),
                       ("frames over 33 ms", len(slow)),
                       ("frames over 100 ms", len([item for item in slow if item[0] > 100])),
                       ("slowest frames (ms, hour, frame in poll)", sorted(slow, reverse=True)[:8]),
                       ("devices", wifi_made + ble_made), ("csv rows", len(rows)),
                       ("traced MB per hour", [round(value, 1) for value in hourly]),
                       ("traced MB growth, last 2 h (no new devices)", round(steady_growth, 2)),
                       ("process MB start/end", [memory_start and round(memory_start, 1),
                                                 memory_end and round(memory_end, 1)]),
                       ("gps switches", switches), ("gps alert lines", len(alerts)),
                       ("wigle uploads started", uploads_started), ("wigle files accepted", len(web.wigle_files)),
                       ("wigle posts", len([1 for entry in web.log if entry[2] == "wigle"])),
                       ("xp", rig.xp)):
        ctx.note(key, value)
    print("       soak: frames over 33 ms: %d, over 100 ms: %d; slowest (ms, hour, frame in poll): %s"
          % (len(slow), len([item for item in slow if item[0] > 100]), sorted(slow, reverse=True)[:8]), flush=True)
    print("       soak: %d frames in %.0f s, frame p50 %.2f ms / p95 %.2f ms / max %.0f ms; traced MB by hour %s; "
          "last-2h growth %.2f MB; process MB %s -> %s; %d GPS switches; %d of %d WiGLE uploads accepted"
          % (len(frame_times), elapsed, p50, p95, worst, [round(v, 1) for v in hourly], steady_growth,
             memory_start and round(memory_start), memory_end and round(memory_end), switches,
             len(web.wigle_files), uploads_started), flush=True)

    check_equal(wifi_made + ble_made, WIFI + BLE, "devices generated")
    check_equal(len(keys), len(set(keys)), "wardriving.csv rows vs distinct devices (duplicates)")
    check_equal(sum(1 for row in rows if row[10] == "WIFI"), WIFI, "Wi-Fi rows (none lost)")
    check_equal(sum(1 for row in rows if row[10] == "BLE"), BLE, "BLE rows (none lost)")
    check(all(len(row) == 11 for row in rows), "a row does not have 11 columns")
    check(not [row for row in rows if float(row[6]) == 0.0 and float(row[7]) == 0.0], "a 0,0 row was written")
    check(rig.xp >= WIFI * 15 + BLE * 10, "XP is below what the new devices alone are worth")
    check(not plugin._backlog and not plugin._held_networks and not plugin._held_bt, "devices left unprocessed")
    check(p95 < 50.0, "frame time p95 is %.1f ms (a frame at 30 fps is 33 ms)" % p95)
    check(worst < 1500.0, "one frame took %.0f ms" % worst)
    check(abs(steady_growth) < max(3.0, 0.05 * hourly[DISCOVERY_HOURS - 1]),
          "memory grew %.2f MB in two hours with no new devices: %s" % (steady_growth, hourly))
    check(8 <= switches <= 80, "GPS source switches over 8 h of scripted faults: %d" % switches)
    check(len(alerts) < 400, "GPS alert lines: %d (alerts must be once per event)" % len(alerts))
    check_equal(len([a for a in alerts if "ANTENNA SHORT" in a and "!!!" not in a]), 1, "antenna SHORT alarms")
    check_equal(plugin._load_wigle_queue(), [], "the WiGLE retry queue at the end of the drive")
    check(len(web.wigle_files) >= HOURS, "WiGLE files accepted: %d" % len(web.wigle_files))
    check(len(gps._health_events) <= gm.GPS_HEALTH_EVENTS_MAX, "GPS event queue grew")
    check(len(gps._multi.devices) == 2, "GPS receivers tracked: %d" % len(gps._multi.devices))
    check(counters["term"] < 60_000, "terminal lines: %d" % counters["term"])
    assert Path
