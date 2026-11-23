"""[bristle] Everything Bristle does away from the game thread.

One BristleService per game: Node Memory, the fleet, the USB scanner and its links, and a small
housekeeping thread. Serial reading, port scanning and file saving all happen on worker threads.
The game thread only reads plain values and drains two queues (finds, messages).
"""
from __future__ import annotations

import collections
import json
import os
import threading
import time
from pathlib import Path

from . import proto, settings
from .fleet import Fleet
from .link_usb import ASK, PROBE, PortPolicy, UsbLink, UsbScanner
from .registry import Registry

DATA_DIR = "bristle"             # inside loot/: kept by the installer and part of every loot backup
SAVE_EVERY_S = 5.0               # ack points, when they changed
MEMORY_EVERY_S = 60.0            # Node Memory (last seen, reported build)
CLOCK_JUMP_S = 2.0


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


class BristleService:
    def __init__(self, install_dir, loot_base, on_find, held_port=lambda: None) -> None:
        self.install_dir = Path(install_dir)
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
        self.fleet = Fleet(self.registry, key, region=self.conf["region"], on_find=on_find,
                           on_event=self._event, defer_acks=True)
        self._restore_acks()
        self.policy = PortPolicy(self.registry, exclude=settings.port_list(self.conf),
                                 auto=settings.on(self.conf, "auto_probe"),
                                 extra_ids=settings.usb_ids(self.conf))
        self._held_port = held_port                      # the port WDG's own ESP32 link has open
        self.links = {}                                  # device path -> UsbLink
        self.new_ports = {}                              # USB key -> (PortInfo, reason): waiting for you
        self.skipped = {}                                # USB key -> (PortInfo, reason)
        self.scanner = UsbScanner(self._open_link, self.policy, on_port=self._on_port,
                                  in_use=self._in_use)
        self._stop = threading.Event()
        self._thread = None
        self._acks_saved = {}
        self._clock_anchor = (time.time(), time.monotonic())
        self.started_by_tie = False

    # ------------------------------------------------------------------ start / stop
    def start(self) -> None:
        self.scanner.start()
        self._thread = threading.Thread(target=self._run, name="bristle-service", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.scanner.stop()
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
        last_save = last_memory = time.monotonic()
        while not self._stop.wait(0.1):
            try:
                while self._jobs:
                    job = self._jobs.popleft()
                    try:
                        job()
                    except Exception as exc:
                        self.say("[BRISTLE] That did not work: %s" % exc, "warn")
                self.fleet.tick()
                self._watch_clock()
                now = time.monotonic()
                if now - last_save >= SAVE_EVERY_S:
                    last_save = now
                    self._save(memory=now - last_memory >= MEMORY_EVERY_S)
                    if now - last_memory >= MEMORY_EVERY_S:
                        last_memory = now
            except Exception as exc:                     # never let housekeeping die quietly
                self.say("[BRISTLE] internal error: %s" % exc, "error")

    def _watch_clock(self) -> None:
        """Nodes carry this machine's clock. If it is corrected (GPS or NTP sets it mid-drive),
        tell every node at once instead of at the next ten-minute resync."""
        wall0, mono0 = self._clock_anchor
        wall, mono = time.time(), time.monotonic()
        if abs((wall - wall0) - (mono - mono0)) > CLOCK_JUMP_S:
            self._clock_anchor = (wall, mono)
            self.fleet.resync_time()
            self.say("[BRISTLE] The uConsole's clock changed: node clocks were set again", "warn")

    def _save(self, memory=True) -> None:
        try:
            acks = self.fleet.delivery.snapshot()
            if acks != self._acks_saved:
                _write_json(self.dir / "acks.json", acks)
                self._acks_saved = acks
            if memory:
                self.registry.save()
        except OSError as exc:
            self.say("[BRISTLE] Could not save Node Memory: %s" % exc, "error")

    def _restore_acks(self) -> None:
        try:
            saved = json.loads((self.dir / "acks.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
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

    # ------------------------------------------------------------------ ports and links
    def _in_use(self, device) -> bool:
        from .link_usb import port_in_use
        held = self._held_port()
        if held and os.path.realpath(str(held)) == os.path.realpath(device):
            return True                                   # WDG's own ESP32 link (same process)
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
            self.fleet.drop_session(link.session)

    def link_of(self, session):
        return next((l for l in list(self.links.values()) if l.session is session), None)

    def _event(self, kind, session, info) -> None:
        link = self.link_of(session)
        where = link.device if link else "?"
        if kind == "new_node":
            self.say("[BRISTLE] New node on %s (%s): Adopt it on the Nodes screen" % (where, session.board))
        elif kind == "new_node_wireless":
            self.say("[BRISTLE] A node with no owner is in range: plug it into USB to adopt it")
        elif kind == "authed":
            self.say("[BRISTLE] Node %s online (%s, build %s)" % (info.get("number"), session.board, session.build), "ok")
        elif kind == "no_answer":
            self.say("[BRISTLE] %s did not answer: not a Bristle node. Left alone until replugged." % where)
            if link:
                self.policy.went_quiet(link.port.key)
                link.close()
        elif kind == "silent":
            self.say("[BRISTLE] Node %s went quiet (%s s): reconnecting" % (session.number, info.get("seconds")), "warn")
        elif kind == "not_ours":
            self.say("[BRISTLE] %s has another owner: ignored" % where, "warn")
        elif kind == "forgotten":
            self.say("[BRISTLE] A node of yours that is not in Node Memory is on %s: Adopt to take it back" % where)
        elif kind == "rsp" and not info.get("ok"):
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
