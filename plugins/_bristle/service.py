"""[bristle] Everything Bristle does away from the game thread.

One BristleService per game: Node Memory, the fleet, the three links (USB, the ESP-NOW bridge,
Bluetooth LE), the GPS track on disk, nearby alerts and skimmer suspects, firmware updates and
flashing, the second-core export, and a small housekeeping thread. Serial and radio work, port
scanning and every file read or write happen on worker threads. The game thread only reads plain
values, appends to queues and drains two of them (finds, messages).
"""
from __future__ import annotations

import collections
import json
import os
import threading
import time
from pathlib import Path

from . import espnow, flasher, kismet_tap, ota, plan, proto, second_core, settings, skimmer, wdg_ports
from .fleet import Fleet
from .gpsplace import ClockMap, Fix, GpsTrack
from .link_espnow import EspNowHub
from .link_usb import ASK, PROBE, SKIP, PortPolicy, UsbLink, UsbScanner
from .nearby_engine import NearbyEngine
from .registry import Registry
from .session import AUTHED, CLOSED

DATA_DIR = "bristle"             # inside loot/: kept by the installer and part of every loot backup
SAVE_EVERY_S = 5.0               # ack points, the GPS track, suspects, the alert log: when they changed
MEMORY_EVERY_S = 60.0            # Node Memory (last seen, reported build)
KISMET_EVERY_S = 15.0            # look at which channels Kismet is on
TRACK_KEEP_S = 24 * 3600.0       # how much GPS track is kept on disk and in memory
TRACK_FILE, EPOCH_FILE = "gps_track.csv", "gps_epochs.json"


def data_dir(loot_base) -> Path:
    return Path(loot_base) / DATA_DIR


def bench_dirs() -> list:
    """Where the bench tool kept its key and Node Memory (the game may run under sudo)."""
    homes = [Path.home()]
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        homes.append(Path("/home") / sudo_user)
    return [home / ".bristle-bench" for home in homes]


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class BristleService:
    def __init__(self, install_dir, loot_base, on_find, held_port=lambda: None) -> None:
        self.install_dir = Path(install_dir)
        self.loot_base = Path(loot_base)
        self.conf_path = self.install_dir / "bristle.conf"
        self.conf = settings.load(self.conf_path)
        self.dir = data_dir(loot_base)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.messages = collections.deque(maxlen=200)    # (text, kind): the last ones, for the screen
        self.terminal = collections.deque(maxlen=500)    # the same texts, taken by the game's terminal
        self._jobs = collections.deque()                 # things the screen asked for (run on our thread)
        self.key_source = ""
        key, self.key_source = settings.owner_key(self.install_dir / "secrets.conf", bench_dirs())
        self.registry = Registry(self.dir / "nodes.json")
        if self.key_source == "bench":
            self._import_bench_nodes()
        self.core_id = settings.core_id(self.dir)
        self.fleet = Fleet(self.registry, key, region=self.conf["region"], on_find=on_find,
                           on_event=self._event, defer_acks=True, core_id=self.core_id)
        self.fleet.ble_active = str(self.conf.get("ble_scan", "")).strip().lower() == "active"
        self.fleet.set_repeat(int(settings.number(self.conf, "repeat_ap_s", 5, 86400) * 1000),
                              int(settings.number(self.conf, "repeat_ble_s", 1, 86400) * 1000))
        self.fleet.set_control_channel(int(settings.number(self.conf, "control_channel", 1, 13)))
        self._restore_acks()
        # ---- links
        self.policy = PortPolicy(self.registry, exclude=settings.port_list(self.conf),
                                 auto=settings.on(self.conf, "auto_probe"),
                                 extra_ids=settings.usb_ids(self.conf))
        self._held_port = held_port                      # the port WDG's own ESP32 link has open
        self.links = {}                                  # device path -> UsbLink
        self.new_ports = {}                              # USB key -> (PortInfo, reason): waiting for you
        self.skipped = {}                                # USB key -> (PortInfo, reason)
        self.scanner = UsbScanner(self._open_link, self.policy, on_port=self._on_port,
                                  in_use=self._in_use, beacons=wdg_ports.BEACONS)
        self._counters_dirty = False
        self.counters = espnow.CounterStore(_read_json(self.dir / "espnow.json", {}), self._counters_changed)
        self.hub = EspNowHub(self.fleet.new_session, self.fleet.drop_session, key, self.counters,
                             is_blocked=self.registry.is_blocked)
        self.ble = None
        self.ble_note = "off (ble_link = off)"
        # ---- GPS track (kept on disk so a restart mid-drive still places the nodes' backlog)
        self.clock = ClockMap(history=_read_json(self.dir / EPOCH_FILE, []))
        self.track = GpsTrack(keep_seconds=TRACK_KEEP_S)
        self.track_ready = False                         # the saved track is loaded (worker thread)
        self.gps_out = collections.deque()               # new fixes, to be appended to the file
        self.clock_jumped = False                        # set by the game thread, handled here
        self._epochs_saved = None
        # ---- nearby alerts, suspects, hunt
        self.nearby = NearbyEngine(self.conf, loot_base, self.registry, self.place, self.here, self.say)
        self.hunt_note = ""
        self._hunt_scanner = None
        self._hunt_borrowed = None                       # the Kismet source closed for the hunt
        # ---- firmware
        self.ota = None                                  # flasher.OtaJob
        self.flash = None                                # flasher.FlashJob
        self._ota_waiting = {}                           # hwid -> OtaJob that is restarting
        self._stop = threading.Event()
        self._thread = None
        self._acks_saved = {}
        self._kismet_note = ""
        self.started_by_tie = False

    # ------------------------------------------------------------------ start / stop
    def start(self) -> None:
        kismet_tap.enabled.set()
        self.scanner.start()
        if settings.on(self.conf, "ble_link"):
            from .link_ble import BleLinks
            adapter = str(self.conf.get("ble_adapter", "hci0")).strip() or "hci0"
            self.ble = BleLinks(
                adapter, self.fleet.new_session, self.fleet.drop_session,
                known=lambda: {h for h, r in list(self.registry.nodes.items()) if not r.role},
                blocked=lambda: set(self.registry.blocked),
                on_usb=lambda: {s.hwid for s in list(self.fleet.sessions)
                                if s.link_type == proto.LINK_USB and s.state == AUTHED},
                limit=int(settings.number(self.conf, "ble_max_links", 1, 7)), say=self.say)
            self.ble.start()
            self.ble_note = adapter
        self._thread = threading.Thread(target=self._run, name="bristle-service", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        kismet_tap.enabled.clear()
        self.scanner.stop()
        try:
            self._hunt_end()
        except Exception:
            pass
        if self.ble is not None:
            self.ble.stop()
        for link in list(self.links.values()):
            link.close()
        self._save()

    def say(self, text, kind="info") -> None:
        self.messages.append((text, kind))
        self.terminal.append(text)

    def do(self, job) -> None:
        """Run job() on the service thread: anything that writes to a node or a file."""
        self._jobs.append(job)

    # ------------------------------------------------------------------ worker thread
    def _run(self) -> None:
        self._load_track()
        last_save = last_memory = last_kismet = time.monotonic()
        while not self._stop.wait(0.1):
            try:
                while self._jobs:
                    job = self._jobs.popleft()
                    try:
                        job()
                    except Exception as exc:
                        self.say("[BRISTLE] That did not work: %s" % exc, "warn")
                self.fleet.tick()
                self.hub.tick()
                self._ota_tick()
                self.nearby.process()
                if self.clock_jumped:
                    self.clock_jumped = False
                    self.fleet.resync_time()
                    self.say("[BRISTLE] The uConsole's clock changed: node clocks were set again", "warn")
                now = time.monotonic()
                if now - last_kismet >= KISMET_EVERY_S:
                    last_kismet = now
                    self._kismet_channels(now)
                if now - last_save >= SAVE_EVERY_S:
                    last_save = now
                    self._save(memory=now - last_memory >= MEMORY_EVERY_S)
                    if now - last_memory >= MEMORY_EVERY_S:
                        last_memory = now
            except Exception as exc:                     # never let housekeeping die quietly
                self.say("[BRISTLE] internal error: %s" % exc, "error")

    def _kismet_channels(self, now) -> None:
        """Channels Kismet's adapters stay on get the shortest dwell on the nodes. Kismet's own
        polling thread fetched the source list; nothing is asked of Kismet here."""
        fresh = now - kismet_tap.sources["t"] < 3 * kismet_tap.SOURCES_EVERY_S
        heavy, note = plan.kismet_heavy_channels(kismet_tap.sources["list"] if fresh else [])
        self.fleet.set_kismet_channels(heavy)
        if note != self._kismet_note:
            if self._kismet_note or heavy:
                self.say("[BRISTLE] %s" % note)
            self._kismet_note = note

    def kismet_note(self) -> str:
        return self._kismet_note

    def _counters_changed(self) -> None:
        self._counters_dirty = True

    def _save(self, memory=True) -> None:
        try:
            acks = self.fleet.delivery.snapshot()
            if acks != self._acks_saved:
                _write_json(self.dir / "acks.json", acks)
                self._acks_saved = acks
            if self._counters_dirty:
                self._counters_dirty = False
                _write_json(self.dir / "espnow.json", self.counters.data)
            self._save_track()
            if memory:
                self.registry.save()
        except OSError as exc:
            self.say("[BRISTLE] Could not save Node Memory: %s" % exc, "error")
        self.nearby.save()

    def _restore_acks(self) -> None:
        saved = _read_json(self.dir / "acks.json", {})
        for hwid, value in saved.items():
            if isinstance(value, int) and self.registry.known(hwid) is not None:
                self.fleet.delivery.restore(hwid, value)
        self._acks_saved = dict(saved)

    def _import_bench_nodes(self) -> None:
        """Nodes adopted with the bench tool are owned by the bench key we just took over."""
        if self.registry.nodes:
            return
        for folder in bench_dirs():
            try:
                data = json.loads((folder / "nodes.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            count = 0
            for record in data.get("nodes", []):
                try:
                    self.registry.adopt(record["hwid"], board=record.get("board", ""),
                                        build=int(record.get("build", 0)), cap_5g=bool(record.get("cap_5g")),
                                        number=int(record["number"]))
                    if record.get("usb_key"):
                        self.registry.touch(record["hwid"], usb_key=record["usb_key"])
                    count += 1
                except (KeyError, ValueError, PermissionError):
                    continue
            for key, note in dict(data.get("not_nodes", {})).items():
                try:
                    self.registry.mark_not_node(key, note)
                except (ValueError, PermissionError):
                    pass
            self.registry.save()
            self.say("[BRISTLE] Took over the bench tool's key and %d node(s)" % count)
            return

    # ------------------------------------------------------------------ the GPS track
    def _load_track(self) -> None:
        """Worker thread, once: read the saved track back and drop what is older than a day."""
        path = self.dir / TRACK_FILE
        kept = []
        try:
            cutoff = time.time() - TRACK_KEEP_S
            with open(path, "r", encoding="ascii") as handle:
                for line in handle:
                    parts = line.strip().split(",")
                    try:
                        when = float(parts[0])
                        if when < cutoff:
                            continue
                        fix = Fix(when, float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]))
                    except (ValueError, IndexError):
                        continue
                    if self.track.add(fix):
                        kept.append(line.strip())
            temporary = path.with_name("." + path.name + ".tmp")
            with open(temporary, "w", encoding="ascii", newline="\n") as handle:
                handle.write("".join(row + "\n" for row in kept))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError:
            pass                                         # no track yet, or it cannot be read: start empty
        self.track_ready = True

    def _save_track(self) -> None:
        if self.gps_out:
            rows = []
            while self.gps_out:
                f = self.gps_out.popleft()
                rows.append("%.3f,%.7f,%.7f,%.1f,%.1f" % (f.t, f.lat, f.lon, f.alt or 0.0, f.acc or 0.0))
            with open(self.dir / TRACK_FILE, "a", encoding="ascii", newline="\n") as handle:
                handle.write("\n".join(rows) + "\n")
        epochs = self.clock.snapshot()
        if epochs and (self._epochs_saved is None or len(epochs) != len(self._epochs_saved)
                       or epochs[-1][1] - self._epochs_saved[-1][1] > 30.0):
            _write_json(self.dir / EPOCH_FILE, epochs)
            self._epochs_saved = epochs

    def place(self, wall_time):
        """Where the uConsole was at this (node or Kismet) time: (lat, lon), or None."""
        when = self.clock.to_track(wall_time)
        fix = self.track.place(when) if when is not None else None
        return (fix.lat, fix.lon) if fix is not None else None

    def here(self):
        """Where the uConsole is now, if a fix is fresh: (lat, lon), or None."""
        latest = self.track.latest_time()
        if latest is None:
            return None
        try:
            if self.clock.track_time(time.monotonic()) - latest > 10.0:
                return None
        except RuntimeError:
            return None
        fix = self.track.place(latest)
        return (fix.lat, fix.lon) if fix is not None else None

    def heading(self):
        """Which way the uConsole is moving (degrees, 0 = north), from the last seconds of track;
        None while it stands still or has no fix."""
        latest = self.track.latest_time()
        if latest is None:
            return None
        before, now = self.track.place(latest - 3.0), self.track.place(latest)
        if before is None or now is None or skimmer.distance_m(before.lat, before.lon, now.lat, now.lon) < 3.0:
            return None
        return skimmer.bearing_deg(before.lat, before.lon, now.lat, now.lon)

    # ------------------------------------------------------------------ ports and links
    def _in_use(self, device) -> bool:
        from .link_usb import port_in_use
        held = self._held_port()
        if held and os.path.realpath(str(held)) == os.path.realpath(device):
            return True                                   # WDG's own ESP32 link (same process)
        if self.flash is not None and self.flash.state in ("waiting", "erasing", "writing") \
                and self.flash.port == device:
            return True                                   # esptool has it
        return port_in_use(device)

    def _on_port(self, port, action, reason) -> None:
        self.new_ports.pop(port.key, None)
        self.skipped.pop(port.key, None)
        if action == ASK:
            self.new_ports[port.key] = (port, reason)
            self.say("[BRISTLE] New ESP32 port %s: open PLUGINS > Bristle: Nodes to check it" % port.device)
        elif action != PROBE:
            self.skipped[port.key] = (port, reason)

    def _open_link(self, port) -> None:
        link = UsbLink(port, lambda send: self.fleet.new_session(send, proto.LINK_USB), on_closed=self._closed)
        self.links[port.device] = link
        try:
            link.open()
        except Exception:
            self.links.pop(port.device, None)
            raise

    def _closed(self, link) -> None:
        self.links.pop(link.device, None)
        self.scanner.released(link.device)
        if link.session is not None:
            self.hub.detach(link.session)                 # if it was the bridge, its nodes go with it
            self.fleet.drop_session(link.session)

    def link_of(self, session):
        return next((l for l in list(self.links.values()) if l.session is session), None)

    def _event(self, kind, session, info) -> None:
        if kind == "erx":
            self.hub.on_erx(session, info)
            return
        if kind == "bst":
            self.hub.on_bst(info)
            return
        if kind == "seen":
            try:
                self.nearby.hunt_readings.append((int(info.get("r", "")), "node %s" % session.number))
            except ValueError:
                pass
            return
        if kind == "ota":
            if self.ota is not None and session is self.ota.session:
                self.ota.sender.on_status(info)
            return
        link = self.link_of(session)
        where = link.device if link else {proto.LINK_ESPNOW: "the bridge", proto.LINK_BLE: "Bluetooth"}.get(
            session.link_type, "?")
        if kind == "new_node":
            self.say("[BRISTLE] New node on %s (%s): Adopt it on the Nodes screen" % (where, session.board))
        elif kind == "new_node_wireless":
            self.say("[BRISTLE] A node with no owner is in range: plug it into USB to adopt it")
        elif kind == "authed":
            if session.is_bridge:
                if session.link_type == proto.LINK_USB:
                    self.hub.attach(session)
                    self.say("[BRISTLE] Bridge online on %s (%s, build %s): nodes in range link through it"
                             % (where, session.board, session.build), "ok")
            else:
                self.say("[BRISTLE] Node %s online over %s (%s, build %s)" % (
                    info.get("number"), {proto.LINK_USB: "USB", proto.LINK_ESPNOW: "ESP-NOW",
                                         proto.LINK_BLE: "Bluetooth"}.get(session.link_type, "?"),
                    session.board, session.build), "ok")
            waiting = self._ota_waiting.pop(session.hwid, None)
            if waiting is not None:
                took = session.build == waiting.build
                waiting.result = ("Node %s now runs build %s" % (session.number, session.build) if took else
                                  "Node %s is back on build %s: the update did not take (it rolled back)"
                                  % (session.number, session.build))
                self.say("[BRISTLE] " + waiting.result, "ok" if took else "warn")
        elif kind == "no_answer":
            if link:
                self.say("[BRISTLE] %s did not answer: not a Bristle node. Left alone until replugged." % where)
                self.policy.went_quiet(link.port.key)
                link.close()
        elif kind == "busy":
            self.say("[BRISTLE] Node %s is being run by another core: left alone (asking again every %d s)"
                     % (session.hwid, proto.BUSY_RETRY_MS // 1000), "warn")
        elif kind == "silent":
            self.say("[BRISTLE] Node %s went quiet (%s s): reconnecting" % (session.number, info.get("seconds")), "warn")
        elif kind == "not_ours":
            self.say("[BRISTLE] %s has another owner: ignored" % where, "warn")
        elif kind == "forgotten":
            self.say("[BRISTLE] A node of yours that is not in Node Memory is on %s: Adopt to take it back" % where)
        elif kind == "rsp":
            if info.get("c") == "OTA" and self.ota is not None and session is self.ota.session:
                self.ota.sender.on_rsp(bool(info.get("ok")), info.get("e"))
            elif not info.get("ok"):
                self.say("[BRISTLE] Node %s refused %s: %s" % (session.number, info.get("c"), info.get("e")), "warn")

    # ------------------------------------------------------------------ what the screen asks for
    def probe(self, key) -> None:
        self.policy.approve(key)
        self.new_ports.pop(key, None)
        self.scanner.rescan()

    def not_a_node(self, key, note="") -> None:
        port = (self.new_ports.pop(key, None) or self.skipped.get(key) or (None, ""))[0]
        self.registry.mark_not_node(key, note)
        if port is not None and port.device in self.links:
            self.links[port.device].close()
        self.scanner.rescan()

    def set_running(self, on, by_tie=False) -> None:
        if on:
            session = self.fleet.start()
            self.started_by_tie = by_tie
            self.say("[BRISTLE] Scanning started (session %d)" % session, "ok")
        else:
            self.fleet.stop()
            self.started_by_tie = False
            self.say("[BRISTLE] Scanning stopped")

    def remember(self, key, value) -> None:
        self.conf[key] = value
        threading.Thread(target=settings.remember, args=(self.conf_path, key, value),
                         name="bristle-conf", daemon=True).start()

    def set_bridge(self, session, on: bool) -> None:
        """Worker thread: make a board on USB the ESP-NOW bridge, or a node again."""
        session.set_role(proto.ROLE_BRIDGE if on else proto.ROLE_NODE)
        self.registry.touch(session.hwid, role=proto.ROLE_BRIDGE if on else proto.ROLE_NODE)
        self.registry.save()
        self.say("[BRISTLE] Node %s is restarting as %s" % (session.number, "the bridge" if on else "a node"), "ok")

    # ------------------------------------------------------------------ firmware
    def firmware(self) -> list:
        return flasher.releases(self.install_dir, str(self.conf.get("firmware_dir", "")).strip())

    def start_ota(self, hwid) -> None:
        """Worker thread: update one node over the link it is on."""
        if self.ota is not None and self.ota.sender.active:
            raise RuntimeError("node %s is being updated: one at a time" % self.ota.number)
        session = next((s for s in self.fleet.authed() + self.fleet.bridges() if s.hwid == hwid), None)
        if session is None:
            raise RuntimeError("that node is offline")
        found = self.firmware()
        if not found:
            raise RuntimeError("no node firmware found (bristle/firmware/releases, or firmware_dir in bristle.conf)")
        job = flasher.OtaJob(session, found[0], session.board)
        if job.build is not None and session.build == job.build:
            self.say("[BRISTLE] Node %s already runs build %s: sending it again anyway" % (session.number, job.build))
        self.ota = job
        job.sender.start()
        self.say("[BRISTLE] Updating node %s to build %s over %s. It keeps its old firmware unless the "
                 "whole image arrives and checks out." % (session.number, job.build, job.link_name), "ok")

    def stop_ota(self) -> None:
        if self.ota is not None and self.ota.sender.active:
            self.ota.sender.abort("stopped by you")
            self.say("[BRISTLE] Update stopped: node %s keeps its old firmware" % self.ota.number, "warn")

    def _ota_tick(self) -> None:
        job = self.ota
        if job is None or job.finished:
            return
        sender = job.sender
        if sender.active:
            if job.session.state == CLOSED or job.session not in self.fleet.sessions:
                sender.link_lost()
            else:
                sender.tick()
        if not sender.active:
            job.finished = time.monotonic()
            if sender.state == ota.DONE:
                self._ota_waiting[job.hwid] = job
                self.say("[BRISTLE] Node %s has the new image and is restarting. If it does not come back on "
                         "build %s it went back to its old firmware." % (job.number, job.build), "ok")
            else:
                self.say("[BRISTLE] " + job.text(), "warn")

    def start_flash(self, key, board, first=True) -> None:
        """Worker thread: write firmware to the board on the USB device with this key."""
        if self.flash is not None and self.flash.state in ("waiting", "erasing", "writing"):
            raise RuntimeError("a board is being flashed: one at a time")
        port = next((p for p, _action, _reason in self.scanner.seen.values() if p.key == key), None)
        if port is None:
            port = next((l.port for l in list(self.links.values()) if l.port.key == key), None)
        if port is None:
            raise RuntimeError("that port is gone: plug the board in again")
        action, reason = self.policy.decide(port)
        if action == SKIP and reason != "did not answer (unplug it and plug it in again to retry)":
            raise RuntimeError("%s: %s. Not flashed." % (port.device, reason))
        held = self._held_port()
        if held and os.path.realpath(str(held)) == os.path.realpath(port.device):
            raise RuntimeError("%s is the game's own ESP32: not flashed" % port.device)
        found = self.firmware()
        if not found:
            raise RuntimeError("no node firmware found (bristle/firmware/releases, or firmware_dir in bristle.conf)")
        path = flasher.image(found[0], board, "FULL" if first else "app")
        link = self.links.get(port.device)
        if link is not None:
            link.close()                                  # esptool needs the port to itself
        self.scanner.active.add(port.device)              # and the scanner stays off it meanwhile

        def done(job, port=port):
            self.scanner.released(port.device)
            if job.state == "done":
                self.say("[BRISTLE] %s now holds %s (%s). It shows up as a new node in a moment."
                         % (port.device, found[0].name, flasher.BOARD_NAMES.get(board, board)), "ok")
                self.policy.approve(port.key)             # it is a Bristle board now: say hello
            else:
                self.say("[BRISTLE] Flashing %s failed: %s" % (port.device, job.error), "error")
            self.scanner.rescan()

        self.flash = flasher.FlashJob(port.device, path, first, done=done)
        self.say("[BRISTLE] Writing %s to %s (%s)%s" % (path.name, port.device, flasher.BOARD_NAMES.get(board, board),
                                                       ": the whole flash is erased first" if first else ""), "ok")
        self.flash.start()

    # ------------------------------------------------------------------ second core
    def export_path(self) -> Path:
        return self.install_dir / second_core.FILE_NAME

    def export_core(self, passphrase: str) -> None:
        """Worker thread: write the owner key and Node Memory, locked with the passphrase."""
        self.registry.save()
        memory = _read_json(self.dir / "nodes.json", {"version": 1, "nodes": [], "blocked": {}, "not_nodes": {}})
        memory["not_nodes"] = {}                          # USB ports of this machine mean nothing elsewhere
        data = second_core.export_bundle(self.fleet.owner_key, memory, passphrase)
        path = self.export_path()
        temporary = path.with_name("." + path.name + ".tmp")
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        self.say("[BRISTLE] Export written: %s (%d nodes). Copy it to the other install's folder and "
                 "import it there with the same passphrase." % (path, len(memory.get("nodes", []))), "ok")

    def import_core(self, passphrase: str) -> None:
        """Worker thread: take the owner key and Node Memory from an export file in the install folder."""
        path = self.export_path()
        try:
            data = path.read_bytes()
        except OSError:
            raise RuntimeError("put %s into %s first" % (second_core.FILE_NAME, self.install_dir)) from None
        key, memory, made = second_core.import_bundle(data, passphrase)
        self.registry.save()
        mine = _read_json(self.dir / "nodes.json", {"version": 1, "nodes": [], "blocked": {}, "not_nodes": {}})
        merged, added, moved = second_core.merge_node_memory(mine, memory)
        _write_json(self.dir / "nodes.json", merged)
        self.registry.load()
        same_key = key == self.fleet.owner_key
        if not same_key:
            settings.replace_owner_key(self.install_dir / "secrets.conf", key)
        self.fleet.replan()
        self.say("[BRISTLE] Import done: %d node(s) added%s (export made %s). %s" % (
            added, ", %d renumbered" % moved if moved else "",
            time.strftime("%Y-%m-%d", time.gmtime(made)) if made else "?",
            "This install already had that owner key." if same_key else
            "The owner key was replaced (the old one is kept in secrets.conf): RESTART THE GAME to use it."),
            "ok")

    # ------------------------------------------------------------------ hunt mode
    def hunt_start(self, mac: str) -> None:
        """Worker thread. Listening only: the hunted device is never connected to, paired with or
        asked for anything (the nodes' watch and the hunt's own listener only receive)."""
        self._hunt_end()
        self.nearby.hunt = skimmer.Hunt(mac)
        told = self.fleet.watch(mac, proto.WATCH_MAX_S)
        notes = ["%d node(s) listening" % told] if told else []
        adapter = str(self.conf.get("hunt_adapter", "")).strip()
        if adapter:
            from . import hunt_scan
            if self.ble is not None and adapter == self.ble.adapter:
                self.ble.pause(True)                      # its scan and ours cannot run at once
            source = next((s for s in kismet_tap.bluetooth_sources()
                           if adapter in (s.get("interface"), s.get("name"))), None)
            if source is not None:
                ok, why = kismet_tap.borrow(source["uuid"], back=False)
                if ok:
                    self._hunt_borrowed = source["uuid"]
                    notes.append("%s borrowed from Kismet" % adapter)
                else:
                    notes.append("could not borrow %s from Kismet (%s)" % (adapter, why))
            scanner = hunt_scan.PassiveScanner(adapter, mac, lambda rssi: self.nearby.hunt_readings.append((rssi, adapter)))
            if scanner.start():
                self._hunt_scanner = scanner
                notes.append("listening on %s" % adapter)
            else:
                notes.append("no fast listening: %s" % scanner.error)
                self._give_back()
        self.hunt_note = "; ".join(notes) or "using Kismet's readings"
        self.say("[BRISTLE] Hunt started for %s (%s). Listening only." % (skimmer.colon(mac), self.hunt_note), "ok")

    def _give_back(self) -> None:
        if self._hunt_borrowed is not None:
            ok, why = kismet_tap.borrow(self._hunt_borrowed, back=True)
            if not ok:
                self.say("[BRISTLE] Could not hand the Bluetooth adapter back to Kismet (%s): stop and start "
                         "the Kismet wardrive to get it back" % why, "error")
            self._hunt_borrowed = None
        if self.ble is not None:
            self.ble.pause(False)

    def _hunt_end(self) -> None:
        hunt = self.nearby.hunt
        if hunt is None and self._hunt_scanner is None and self._hunt_borrowed is None:
            return
        if self._hunt_scanner is not None:
            self._hunt_scanner.stop()
            self._hunt_scanner = None
        self._give_back()
        if hunt is not None:
            self.fleet.watch(hunt.mac, 0)
            self.nearby.suspects.hunt_best(hunt.mac, hunt.best)
            self.nearby.hunt = None

    def hunt_stop(self) -> None:
        self._hunt_end()
        self.hunt_note = ""
        self.say("[BRISTLE] Hunt ended")
