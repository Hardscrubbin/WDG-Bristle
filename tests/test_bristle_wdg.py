"""[bristle] Bristle Nodes inside WDG: settings and the owner key, GPS placement, writing finds
through the real LootManager, the Nodes screen's text, and keeping WDG's own ESP32 code off node
ports. Pure logic and real files in a temporary folder: no simulated node, no simulated device.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugins._bristle import codec, crypto, ingest, proto, screen, settings, wdg_ports  # noqa: E402
from plugins._bristle import session as S  # noqa: E402
from plugins._bristle.delivery import DeliveryTracker  # noqa: E402
from plugins._bristle.fleet import Fleet  # noqa: E402
from plugins._bristle.gpsplace import Fix, GpsTrack  # noqa: E402
from plugins._bristle.registry import Registry  # noqa: E402
from watchdogs.loot_manager import LootManager  # noqa: E402

from test_scan_session_map import loot_at  # noqa: E402

KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
HW = "a0b1c2d3e4f5"
T0 = 1_795_300_000.0


class Temp(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)


class OwnerKey(Temp):
    def test_first_run_adds_one_line_and_keeps_every_other_line(self):
        path = self.root / "secrets.conf"
        before = "# my keys\nWIGLE_API_NAME=abc\nWIGLE_API_TOKEN=def\n"
        path.write_text(before, encoding="utf-8")
        key, source = settings.owner_key(path)
        self.assertEqual((len(key), source), (16, "new"))
        after = path.read_text(encoding="utf-8")
        self.assertTrue(after.startswith(before))
        self.assertIn("BRISTLE_OWNER_KEY=" + key.hex(), after)
        self.assertEqual(after.count("BRISTLE_OWNER_KEY="), 1)
        again, source = settings.owner_key(path)                    # second start: the same key, no rewrite
        self.assertEqual((again, source), (key, "secrets"))
        self.assertEqual(path.read_text(encoding="utf-8"), after)
        self.assertEqual([p.name for p in self.root.iterdir()], ["secrets.conf"])   # no stray temp file
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_a_file_without_a_final_newline_is_not_glued_to_the_key(self):
        path = self.root / "secrets.conf"
        path.write_text("WDGWARS_API_KEY=xyz", encoding="utf-8")
        key, _ = settings.owner_key(path)
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "WDGWARS_API_KEY=xyz")
        self.assertIn("BRISTLE_OWNER_KEY=" + key.hex(), lines)

    def test_the_bench_tools_key_is_taken_over(self):
        bench = self.root / ".bristle-bench"
        bench.mkdir()
        (bench / "owner.key").write_text(KEY.hex() + "\n")
        key, source = settings.owner_key(self.root / "secrets.conf", [self.root / "nowhere", bench])
        self.assertEqual((key, source), (KEY, "bench"))
        self.assertEqual(settings.owner_key(self.root / "secrets.conf"), (KEY, "secrets"))

    def test_a_damaged_key_line_stops_bristle_instead_of_replacing_the_key(self):
        path = self.root / "secrets.conf"
        path.write_text("BRISTLE_OWNER_KEY=not-hex\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            settings.owner_key(path)
        self.assertEqual(path.read_text(encoding="utf-8"), "BRISTLE_OWNER_KEY=not-hex\n")

    def test_the_example_files_name_every_setting_and_hold_no_real_key(self):
        example = settings.load(ROOT / "bristle.conf.example")
        self.assertEqual(example, settings.DEFAULTS)                # the example IS the defaults
        text = (ROOT / "bristle.conf.example").read_text(encoding="utf-8")
        for name in settings.DEFAULTS:
            self.assertIn("\n%s =" % name, text)
        secrets_example = (ROOT / "secrets.conf.example").read_text(encoding="utf-8")
        self.assertIn("# BRISTLE_OWNER_KEY=" + "0" * 32, secrets_example)
        self.assertNotIn("\nBRISTLE_OWNER_KEY=", secrets_example)


class Settings(Temp):
    def test_defaults_and_one_changed_setting(self):
        path = self.root / "bristle.conf"
        self.assertEqual(settings.load(path), settings.DEFAULTS)
        path.write_text("# mine\nregion = e\nhud = off   # quiet\nexclude_ports = /dev/ttyACM0, /dev/ttyACM9\n"
                        "extra_usb_ids = 1a86:55d4, junk\n", encoding="utf-8")
        conf = settings.load(path)
        self.assertEqual(conf["region"], "E")
        self.assertFalse(settings.on(conf, "hud"))
        self.assertTrue(settings.on(conf, "tie_to_kismet"))
        self.assertEqual(settings.port_list(conf), ["/dev/ttyACM0", "/dev/ttyACM9"])
        self.assertEqual(settings.usb_ids(conf), [(0x1A86, 0x55D4)])
        self.assertTrue(settings.remember(path, "hud", "on"))
        text = path.read_text(encoding="utf-8")
        self.assertIn("# mine\n", text)
        self.assertIn("hud = on\n", text)
        self.assertIn("region = e\n", text)
        self.assertTrue(settings.remember(path, "tie_to_kismet", "off"))
        self.assertFalse(settings.on(settings.load(path), "tie_to_kismet"))


class Placement(unittest.TestCase):
    def track(self, *times):
        track = GpsTrack()
        for index, t in enumerate(times):
            track.add(Fix(t, 51.0 + index * 0.001, -0.1, 30.0, 4.0))
        return track

    def test_between_two_fixes_is_placed(self):
        action, fix = ingest.decide(self.track(T0, T0 + 2), T0 + 1, now=T0 + 3)
        self.assertEqual(action, ingest.PLACE)
        self.assertAlmostEqual(fix.lat, 51.0005)

    def test_just_after_the_last_fix_waits_then_is_placed_or_dropped(self):
        track = self.track(T0)
        self.assertEqual(ingest.decide(track, T0 + 6, now=T0 + 6.5)[0], ingest.HOLD)
        track.add(Fix(T0 + 7, 51.002, -0.1))                                  # the GPS caught up
        self.assertEqual(ingest.decide(track, T0 + 6, now=T0 + 7.1)[0], ingest.PLACE)
        lost = self.track(T0)
        self.assertEqual(ingest.decide(lost, T0 + 6, now=T0 + 6 + ingest.WAIT_FOR_NEXT_FIX_S + 1),
                         (ingest.DROP, "no GPS fix"))

    def test_a_find_in_a_gps_gap_or_before_the_track_is_never_placed(self):
        track = self.track(T0, T0 + 60)                                        # a minute without a fix
        self.assertEqual(ingest.decide(track, T0 + 30, now=T0 + 61)[0], ingest.DROP)
        self.assertEqual(ingest.decide(track, T0 - 3600, now=T0 + 61)[0], ingest.DROP)
        self.assertEqual(ingest.decide(GpsTrack(), T0, now=T0 + 60)[0], ingest.DROP)

    def test_a_gps_sample_gets_the_cores_clock_time(self):
        self.assertAlmostEqual(ingest.fix_time({"observed_at": 100.0}, mono_now=101.5, wall_now=T0), T0 - 1.5)

    def test_names_and_macs_are_made_safe_for_the_csv(self):
        self.assertEqual(ingest.csv_safe('Cafe, "Free"\nWiFi'), "Cafe; 'Free' WiFi")
        self.assertEqual(ingest.colon_mac("a42bb0123456"), "A4:2B:B0:12:34:56")
        self.assertEqual(set(ingest.AUTH_NAMES), set(proto.SECURITY.values()) - {proto.SECURITY["UNKNOWN"]})


def find(kind, seq, t, **fields):
    return S.Find(HW, 3, kind, seq, t, 7, {k: str(v) for k, v in fields.items()})


class IntoLoot(Temp):
    """Finds go through the real LootManager into a real wardriving.csv."""

    def game(self):
        gps = SimpleNamespace(available=True, fix=SimpleNamespace(valid=False, latitude=0.0, longitude=0.0,
                                                                  altitude=0.0, hdop=99.0))
        loot = self.enterContext(loot_at(str(self.root), "2026-11-22T12:00:00", gps))
        xp, said = [], []
        app = SimpleNamespace(loot=loot, gps=None, state=SimpleNamespace(networks=[]), _known_wifi=set(),
                              _known_ble=set(), gain_xp=xp.append, msg=lambda text, colour=7: said.append(text),
                              wifi_networks=[], ble_devices=[])
        delivery = DeliveryTracker(defer=True)
        ing = ingest.Ingest(app, delivery)
        for index in range(11):                                              # the uConsole drove north
            ing.track.add(Fix(T0 + index, 51.5 + index * 0.0001, -0.12, 35.0, 5.0))
        return app, ing, delivery, xp, said

    def rows(self, loot):
        text = (Path(loot.session_path) / "wardriving.csv").read_text(encoding="utf-8")
        return [LootManager._wardrive_fields(line) for line in text.splitlines()[2:]]

    def accept(self, delivery, *finds):
        for f in finds:
            self.assertTrue(delivery.node(f.hwid).accept(f.seq))

    def test_finds_are_written_where_the_uconsole_was_at_their_time(self):
        app, ing, delivery, xp, said = self.game()
        ap = find("AP", 0, T0 + 2.5, b="a42bb0123456", c=36, r=-61, k=3, e="Home, sweet")
        bt = find("BT", 1, T0 + 8.0, m="c0ffee123456", r=-70, at=1, nm="Tag")
        self.accept(delivery, ap, bt)
        ing.add(ap)
        ing.add(bt)
        self.assertEqual(delivery.node(HW).ack_value(), 0)                    # received, not yet written
        ing.drain(budget_s=5.0, now=T0 + 11)
        wifi, ble = self.rows(app.loot)
        self.assertEqual(wifi[0], "A4:2B:B0:12:34:56")
        self.assertEqual(wifi[1], "Home; sweet")
        self.assertEqual(wifi[4:6], ["36", "-61"])
        self.assertAlmostEqual(float(wifi[6]), 51.50025, places=5)            # between the fixes at +2 and +3
        self.assertAlmostEqual(float(wifi[7]), -0.12, places=5)
        self.assertEqual(wifi[-1], "WIFI")
        self.assertTrue(wifi[3].startswith("2026-11-21") or wifi[3].startswith("2026-11-22"))   # the find's UTC time
        self.assertEqual((ble[0], ble[-1]), ("C0:FF:EE:12:34:56", "BLE"))
        self.assertAlmostEqual(float(ble[6]), 51.5008, places=5)
        self.assertEqual(delivery.node(HW).ack_value(), 2)                    # now the node may forget them
        self.assertEqual(xp, [15, 10])
        self.assertEqual(ing.counts["written_wifi"], 1)
        self.assertEqual(ing.counts["written_bt"], 1)
        self.assertEqual(ing.per_minute(), 2)
        self.assertEqual([n.bssid for n in app.state.networks], ["A4:2B:B0:12:34:56"])
        self.assertTrue((Path(app.loot.session_path) / "bt_devices.csv").exists())

    def test_no_position_means_counted_not_written_and_never_zero_zero(self):
        app, ing, delivery, xp, _said = self.game()
        old = find("AP", 0, T0 - 3600, b="020000000001", c=1, r=-50, k=0)        # long before the GPS track
        late = find("AP", 1, T0 + 500, b="020000000002", c=6, r=-50, k=0)        # after the track ended
        good = find("AP", 2, T0 + 5, b="020000000003", c=11, r=-50, k=3)
        self.accept(delivery, old, late, good)
        for f in (old, late, good):
            ing.add(f)
        ing.drain(budget_s=5.0, now=T0 + 501)                                    # late: still inside its wait
        self.assertEqual(len(ing.held), 1)
        self.assertEqual(delivery.node(HW).ack_value(), 1)                       # the held one holds the ack
        ing.drain(budget_s=5.0, now=T0 + 500 + ingest.WAIT_FOR_NEXT_FIX_S + 1)
        rows = self.rows(app.loot)
        self.assertEqual([r[0] for r in rows], ["02:00:00:00:00:03"])
        self.assertEqual(ing.counts["no_gps"], 2)
        self.assertEqual(delivery.node(HW).ack_value(), 3)
        for row in rows:
            self.assertFalse(LootManager._observation_position_missing(row[6], row[7]))
        self.assertEqual(xp, [15])

    def test_a_second_sighting_updates_the_same_row_like_any_other_source(self):
        app, ing, delivery, xp, _said = self.game()
        first = find("AP", 0, T0 + 1, b="020000000009", c=6, r=-80, k=3, e="Net")
        again = find("AP", 1, T0 + 4, b="020000000009", c=6, r=-55, k=3, e="Net")
        self.accept(delivery, first, again)
        ing.add(first)
        ing.add(again)
        ing.drain(budget_s=5.0, now=T0 + 11)
        rows = self.rows(app.loot)
        self.assertEqual(len(rows), 1)                                           # LootManager's own dedup
        self.assertEqual(rows[0][5], "-55")
        self.assertEqual(xp, [15, 1])

    def test_stopping_leaves_nothing_half_handled(self):
        app, ing, delivery, _xp, _said = self.game()
        waiting = find("AP", 0, T0 + 17, b="020000000004", c=1, r=-50, k=0)     # 7 s after the last fix
        self.accept(delivery, waiting)
        ing.add(waiting)
        ing.drain(budget_s=5.0, now=T0 + 17.5)
        self.assertEqual(len(ing.held), 1)
        ing.drain(final=True, now=T0 + 17.6)
        self.assertEqual((len(ing.held), ing.counts["no_gps"]), (0, 1))
        self.assertEqual(delivery.node(HW).ack_value(), 1)


class NodesScreen(Temp):
    def service(self, nodes=3):
        registry = Registry(self.root / "nodes.json")
        sent = []
        fleet = Fleet(registry, KEY, defer_acks=True)
        for number in range(1, nodes + 1):
            hwid = "a0b1c2d3e4%02x" % number
            registry.adopt(hwid, board="xiao_c5", build=2, cap_5g=True, number=number)
            s = fleet.new_session(sent.append, proto.LINK_USB)
            s.start()
            s.feed_line(codec.build("HELLO", {
                "v": 1, "hw": hwid, "bd": "xiao_c5", "b": 2, "cap": proto.HB_CAP_5G | proto.HB_CAP_BLE,
                "nn": "ffeeddccbbaa99887766554433221100",
                "np": crypto.node_proof(KEY, s._core_nonce, bytes.fromhex(hwid))}))
            s.feed_line(codec.build("HB", {"n": number, "f": 15, "ph": "", "m": 3, "s": 1, "lk": 1, "qb": 123456,
                                           "qd": 0, "qc": 2944, "qf": 100, "qn": 99, "qo": 0, "b": 2,
                                           "t": -12, "up": 987654, "ram": 210}))
        new = fleet.new_session(sent.append, proto.LINK_USB)
        new.start()
        new.feed_line(codec.build("HELLO", {"v": 1, "hw": "0c0c0c0c0c0c", "bd": "devkit_c5_uart", "b": 2,
                                            "cap": 48, "nn": "00" * 16}))
        registry.block("0b0b0b0b0b0b", "a Biscuit that kept showing up on the list")
        from plugins._bristle.link_usb import PortInfo
        port = PortInfo("/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_AA:AA:AA:AA:AA:01-if00",
                        0x303A, 0x1001, "AA:AA:AA:AA:AA:01")
        return SimpleNamespace(fleet=fleet, registry=registry, new_ports={port.key: (port, "new port")},
                               skipped={}, conf=dict(settings.DEFAULTS), messages=[])

    def test_every_line_fits_the_screen(self):
        service = self.service()
        registry = service.registry
        registry.nodes["a0b1c2d3e401"].name = "front bumper left side long name"
        registry.set_pin("a0b1c2d3e402", "5hi")
        service.fleet.replan()
        service.fleet.start()
        rows = screen.node_rows(service)
        self.assertEqual([r["kind"] for r in rows], ["node", "node", "node", "new", "port", "blocked"])
        for row in rows:
            self.assertLessEqual(len(row["text"]), screen.MAX_CHARS, row["text"])
        self.assertLessEqual(len(screen.HEADER), screen.MAX_CHARS)
        self.assertEqual(len(screen.HEADER), len(rows[0]["text"]))             # columns line up
        for line in (screen.KEYS_1, screen.KEYS_2):
            self.assertLessEqual(len(line), screen.MAX_CHARS)
        ing = ingest.Ingest(SimpleNamespace(), service.fleet.delivery)
        self.assertLessEqual(len(screen.totals_text(service, ing)), screen.MAX_CHARS)
        self.assertIn("SCANNING", screen.totals_text(service, ing))
        for text, _colour in screen.hud_lines(service, ing):
            self.assertLessEqual(len(text) * screen.FONT_W + 6, 300)           # stays in the left half of the map
        self.assertIn("pin 5 GHz high", rows[1]["text"])
        self.assertIn("[A] adopt", rows[3]["text"])
        self.assertIn("[X] not a node", rows[4]["text"])

    def test_channel_coverage_and_plan_text(self):
        service = self.service(nodes=2)
        service.fleet.replan()
        channels, cover = screen.coverage(service)
        self.assertEqual(len(channels), 36)
        self.assertTrue(all(cover[ch] == [1] for ch in channels))                # node 2 is on Bluetooth duty
        plans = {s.number: s.desired.plan for s in service.fleet.authed()}
        self.assertEqual(screen.channels_text(plans[1]), "1-11 36-165")
        self.assertEqual(screen.channels_text(plans[2]), "Bluetooth only")
        self.assertEqual(screen.channels_text(None, "24"), "pin 2.4 GHz: -")
        self.assertEqual([screen.duration(v) for v in (None, 5, 600, 7300, 400000)], ["-", "5s", "10m", "2h01", "4d"])

    def test_nothing_to_show_means_no_hud(self):
        registry = Registry(self.root / "empty.json")
        service = SimpleNamespace(fleet=Fleet(registry, KEY), registry=registry, new_ports={}, conf={}, messages=[])
        self.assertEqual(screen.hud_lines(service, ingest.Ingest(SimpleNamespace(), service.fleet.delivery)), [])


class NodePortsBelongToBristle(Temp):
    def test_no_node_memory_means_no_port_is_claimed_and_nothing_is_listed(self):
        with patch("plugins._bristle.link_usb.list_ports", side_effect=AssertionError("the port list was read")):
            self.assertEqual(wdg_ports.node_devices(self.root), set())

    def test_the_games_own_esp32_search_skips_node_ports(self):
        from plugins._bristle.link_usb import PortInfo
        from watchdogs import serial_manager
        registry = Registry(self.root / "loot" / "bristle" / "nodes.json")
        registry.adopt(HW)                                                    # native USB: serial number = hwid
        registry.adopt("0c0c0c0c0c0c")
        registry.touch("0c0c0c0c0c0c", usb_key="usb:1a86:55d3:5a47012345")   # a CH343 board
        registry.save()
        ports = [PortInfo("/dev/ttyACM0", 0x303A, 0x1001, "AA:AA:AA:AA:AA:01"),      # the game's own XIAO
                 PortInfo("/dev/ttyACM1", 0x303A, 0x1001, "A0:B1:C2:D3:E4:F5"),      # node
                 PortInfo("/dev/ttyACM2", 0x1A86, 0x55D3, "5A47012345")]             # node
        with patch("plugins._bristle.link_usb.list_ports", return_value=ports):
            self.assertEqual(wdg_ports.node_devices(self.root), {"/dev/ttyACM1", "/dev/ttyACM2"})
        listed = [("/dev/ttyACM1", "node", True), ("/dev/ttyACM0", "game", True), ("/dev/ttyACM2", "node", True)]
        with patch.object(serial_manager, "list_usb_serial_devices", return_value=listed), \
                patch.object(wdg_ports, "node_devices", return_value={"/dev/ttyACM1", "/dev/ttyACM2"}):
            self.assertEqual(serial_manager.detect_esp32_port(), "/dev/ttyACM0")
        with patch.object(serial_manager, "list_usb_serial_devices", return_value=listed[:1]), \
                patch.object(wdg_ports, "node_devices", return_value={"/dev/ttyACM1"}):
            self.assertIsNone(serial_manager.detect_esp32_port())             # only a node is plugged in


class ServiceLifecycle(Temp):
    """The real service with its real threads, on an empty install folder. It reads this machine's
    serial port list and, with nothing in Node Memory, must never open or write to any port."""

    def test_start_work_and_stop_on_an_empty_install(self):
        import time
        from plugins._bristle import link_usb, service
        opened = []
        real_open = link_usb.UsbLink.open
        (self.root / "secrets.conf").write_text("WIGLE_API_NAME=abc\n", encoding="utf-8")
        finds = []
        with patch.object(link_usb.UsbLink, "open", lambda link: opened.append(link.device) or real_open(link)), \
                patch.object(service, "bench_dirs", return_value=[]):
            svc = service.BristleService(self.root, self.root / "loot", finds.append)
            self.assertEqual(svc.key_source, "new")
            svc.start()
            ran = []
            svc.do(lambda: ran.append("job"))
            svc.do(lambda: 1 / 0)                              # a failing request is reported, not fatal
            svc.do(lambda: svc.set_running(True))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not (ran and svc.fleet.run_mask):
                time.sleep(0.05)
            self.assertEqual(ran, ["job"])
            self.assertTrue(svc.fleet.run_mask)
            self.assertTrue(any("did not work" in text for text, _kind in svc.messages))
            svc.remember("hud", "off")
            svc.stop()
        self.assertEqual(opened, [])                           # no port was opened: nothing is adopted here
        self.assertIn("BRISTLE_OWNER_KEY=", (self.root / "secrets.conf").read_text(encoding="utf-8"))
        self.assertTrue((self.root / "secrets.conf").read_text(encoding="utf-8").startswith("WIGLE_API_NAME=abc\n"))
        self.assertTrue((self.root / "loot" / "bristle" / "nodes.json").exists())
        again = service.BristleService(self.root, self.root / "loot", finds.append)
        self.assertEqual(again.key_source, "secrets")          # the same key on the next start
        self.assertEqual(again.fleet.owner_key, svc.fleet.owner_key)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and settings.on(settings.load(self.root / "bristle.conf"), "hud"):
            time.sleep(0.05)
        self.assertFalse(settings.on(settings.load(self.root / "bristle.conf"), "hud"))


SHIPS_FIRMWARE = (ROOT / "firmware" / "wardriving-solo" / "manifest.json").is_file()


class NoShippedFirmware(Temp):
    """A build without the firmware folder says so and can flash nothing."""

    def test_a_missing_firmware_folder_is_said_plainly_and_nothing_can_be_installed(self):
        from watchdogs import c5_firmware
        empty = self.root / "wardriving-solo"
        self.assertFalse(c5_firmware.bundled_available(empty))
        with self.assertRaises(c5_firmware.C5FirmwareError) as raised:
            c5_firmware.bundled_package(empty)
        self.assertEqual(str(raised.exception), "No bundled firmware in this build")
        self.assertEqual(c5_firmware.NO_BUNDLE, "No bundled firmware in this build")
        app = (ROOT / "watchdogs" / "app.py").read_text(encoding="utf-8")
        screen_code = app.split("def _draw_flash_screen", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("bundled_available()", screen_code)
        self.assertIn("latest = NO_BUNDLE", screen_code)

    @unittest.skipIf(SHIPS_FIRMWARE, "this build ships the firmware folder")
    def test_this_build_has_no_package_to_offer(self):
        from watchdogs import c5_firmware
        with self.assertRaises(c5_firmware.C5FirmwareError) as raised:
            c5_firmware.fetch_latest_package()                  # what the game calls
        self.assertEqual(str(raised.exception), c5_firmware.NO_BUNDLE)


@unittest.skipUnless(SHIPS_FIRMWARE, "No bundled firmware in this build")
class ShippedFirmware(Temp):
    """SYSTEM > Firmware installs the C5/SOLO image that ships in firmware/: no online source."""

    def test_the_shipped_package_is_the_pinned_one_and_needs_no_network(self):
        import socket
        from watchdogs import c5_firmware, config
        self.assertEqual(config.FIRMWARE_RELEASE_URL, "")

        def refuse(*_args, **_kwargs):
            raise AssertionError("the firmware check tried to use the network")

        with patch.object(c5_firmware, "_read_url", refuse), patch.object(socket, "create_connection", refuse):
            package = c5_firmware.fetch_latest_package()          # what the game calls
            self.assertEqual((package.version, package.release_tag), ("1.7.0", "bundled"))
            self.assertEqual(package.image_sha256, c5_firmware.LEGACY_IMAGE_SHA256)
            self.assertEqual(package.manifest_sha256, c5_firmware.LEGACY_MANIFEST_SHA256)
            copy = c5_firmware.download_image(package, self.root / "image.bin")
        shipped = ROOT / "firmware" / "wardriving-solo" / package.image_name
        self.assertEqual(copy.read_bytes(), shipped.read_bytes())
        info = c5_firmware.verify_image(package, copy)
        self.assertEqual(info.chip_id, 23)                         # ESP32-C5

    def test_a_changed_manifest_or_image_is_refused(self):
        import shutil
        from watchdogs import c5_firmware
        folder = self.root / "wardriving-solo"
        shutil.copytree(ROOT / "firmware" / "wardriving-solo", folder,
                        ignore=shutil.ignore_patterns("src", "tests", "build-inputs", "*.patch"))
        self.assertEqual(c5_firmware.bundled_package(folder).version, "1.7.0")
        manifest = folder / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b" ")
        with self.assertRaises(c5_firmware.C5FirmwareError):
            c5_firmware.bundled_package(folder)
        with self.assertRaises(c5_firmware.C5FirmwareError):
            c5_firmware.bundled_package(self.root / "nothing-here")
        fake = c5_firmware.C5Package("1.7.0", "bundled", "janos-1.7.0-solo-xiao-c5-app.bin", 2250896,
                                     "0" * 64, "bundled:janos-1.7.0-solo-xiao-c5-app.bin",
                                     c5_firmware.LEGACY_MANIFEST_SHA256)
        with self.assertRaises(c5_firmware.C5FirmwareError):
            c5_firmware.download_image(fake, self.root / "x.bin")   # not the pinned image: not "bundled"


class HardwareCheckRows(unittest.TestCase):
    def rows(self, kismet_uses):
        from plugins.hardware_check import C_ERR, HardwareCheck
        check = HardwareCheck()
        bristle = SimpleNamespace(NAME="Bristle: Nodes", service=SimpleNamespace(conf={"ble_adapter": "hci0"}))
        check.app = SimpleNamespace(_plugins=[bristle])
        kismet = SimpleNamespace(_ble_selected=set(kismet_uses), kismet_active=lambda: False)
        ble = [dict(id="hci0", likely_onboard=True), dict(id="hci1", guess="mt7921u")]
        return check._ble_rows(ble, kismet), C_ERR

    def test_the_reserved_adapter_is_marked_and_a_clash_with_kismet_is_loud(self):
        rows, error_colour = self.rows(kismet_uses=["hci1"])
        self.assertIn("kept for Bristle", rows[0][0])
        self.assertNotEqual(rows[0][1], error_colour)
        self.assertIn("hci1", rows[1][0])
        rows, error_colour = self.rows(kismet_uses=["hci0", "hci1"])
        self.assertIn("KISMET IS SET TO USE IT", rows[0][0])
        self.assertEqual(rows[0][1], error_colour)
        self.assertLessEqual(len(rows[0][0]), 62)                             # one Hardware Check column


class PluginFile(unittest.TestCase):
    def test_the_plugin_declares_what_the_game_looks_for(self):
        import plugins.bristle_core as core
        plugin = core.BristleNodes()
        self.assertTrue(core.BristleNodes.BACKGROUND_TICK)
        self.assertEqual(plugin.NAME, "Bristle: Nodes")
        self.assertEqual([(i.key, i.action) for i in plugin.menu_items()], [("n", "open_overlay")])
        self.assertTrue(callable(plugin.draw_map_overlay))
        self.assertFalse(plugin.overlay_active)
        plugin.on_update()                                                    # not started: nothing happens
        plugin.on_unload()

    def test_game_thread_code_never_touches_a_serial_port_or_sleeps(self):
        for name in ("bristle_core.py", "_bristle/screen.py", "_bristle/ingest.py"):
            text = (ROOT / "plugins" / name).read_text(encoding="utf-8")
            for banned in ("import serial", "time.sleep(", "ser.write", "open("):
                self.assertNotIn(banned, text, "%s: %s" % (name, banned))
        core = (ROOT / "plugins" / "bristle_core.py").read_text(encoding="utf-8")
        keys = core.split("def _keys", 1)[1].split("def _adopt", 1)[0]
        for call in ("session.identify(", "session.block(", "svc.probe(", "svc.not_a_node(", "set_pin(", "renumber()"):
            for line in keys.splitlines():
                if call in line:
                    self.assertIn("svc.do(", line, line.strip())            # node and file work: worker thread


if __name__ == "__main__":
    unittest.main()
