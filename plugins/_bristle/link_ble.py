# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""Bluetooth LE link: nodes placed away from the core.

A node advertises Bristle's own service UUID with seven bytes of service data: its hwid and one
flag byte. The core connects only to nodes that are in Node Memory, on ONE adapter that is kept
for Bristle (never one Kismet uses), and to a few at a time: an adapter carries about seven links,
so the limit is a setting and the screen says when it is reached. A board with no owner is only
reported ("plug it into USB to adopt it"); a board with another owner is left alone.

The lines are the same lines as on USB: one per write to the command characteristic, one per
notification from the notify characteristic.

Needs the ``bleak`` package (imported when the link starts, so everything else works without it).
All of it runs on its own thread with its own event loop; nothing here is called by the game's
drawing code.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import codec, proto

SERVICE = proto.BLE_SERVICE_UUID.lower()
SEEN_FOR_S = 20.0            # an advertisement older than this is no longer "in range"
CONNECT_TIMEOUT_S = 15.0
RETRY_AFTER_S = 20.0


@dataclass
class Advert:
    hwid: str
    owned: bool
    rssi: int | None
    seen: float              # monotonic seconds
    device: object = None    # the bleak device, to connect to


def parse_service_data(data: bytes | None) -> tuple[str, bool] | None:
    """(hwid, owned) from a Bristle advertisement's service data; None if it is not one."""
    if not data or len(data) < 7:
        return None
    return bytes(data[:6]).hex(), bool(data[6] & proto.BLE_ADV_FLAG_OWNED)


def choose(adverts: dict, known: set, blocked: set, linked: set, on_usb: set, failed: dict, now: float,
           limit: int) -> tuple[list, list, list, int]:
    """Who to connect to next. Pure, so the rules can be tested.

    Returns (connect, unowned, strangers, waiting): hwids to connect to now (at most what the limit
    leaves room for, strongest first); boards with no owner (report only); boards owned by someone
    else (ignored); and how many of our nodes are in range but over the limit.
    """
    fresh = {h: a for h, a in adverts.items() if now - a.seen <= SEEN_FOR_S and h not in blocked}
    unowned = sorted(h for h, a in fresh.items() if not a.owned and h not in known)
    strangers = sorted(h for h, a in fresh.items() if a.owned and h not in known)
    ours = [a for h, a in fresh.items() if h in known and h not in linked and h not in on_usb
            and now - failed.get(h, -1e9) >= RETRY_AFTER_S]
    ours.sort(key=lambda a: a.rssi if a.rssi is not None else -200, reverse=True)
    room = max(0, limit - len(linked))
    return [a.hwid for a in ours[:room]], unowned, strangers, max(0, len(ours) - room)


class BleLinks:
    def __init__(self, adapter: str, new_session: Callable, drop_session: Callable, known: Callable[[], set],
                 blocked: Callable[[], set], on_usb: Callable[[], set], limit: int = proto.BLE_MAX_LINKS,
                 say: Callable[[str, str], None] = lambda text, kind: None) -> None:
        self.adapter = adapter
        self._new_session, self._drop_session = new_session, drop_session
        self._known, self._blocked, self._on_usb = known, blocked, on_usb
        self.limit = max(1, int(limit))
        self._say = say
        self.adverts: dict[str, Advert] = {}
        self.links: dict[str, object] = {}       # hwid -> bleak client
        self.sessions: dict[str, object] = {}
        self._failed: dict[str, float] = {}
        self.status = {"state": "off", "error": "", "linked": 0, "waiting": 0, "unowned": [], "strangers": 0}
        self._told: set = set()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()         # hunt mode borrows the radio's attention

    # ------------------------------------------------------------------ from other threads
    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="bristle-ble", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def pause(self, on: bool) -> None:
        """While paused nothing new is scanned for or connected (links that are up stay up)."""
        (self._paused.set if on else self._paused.clear)()

    def limit_hit(self) -> bool:
        return self.status["waiting"] > 0

    # ------------------------------------------------------------------ the link thread
    def _thread_main(self) -> None:
        try:
            import bleak  # noqa: F401
        except ImportError:
            self.status.update(state="off", error="the Python package 'bleak' is not installed")
            return
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._main())
        except Exception as exc:                  # the adapter went away, BlueZ is not running, ...
            self.status.update(state="off", error=str(exc) or type(exc).__name__)
            self._say("[BRISTLE] Bluetooth link stopped: %s" % self.status["error"], "warn")
        finally:
            for session in list(self.sessions.values()):
                self._drop_session(session)
            self.sessions.clear()
            self._loop.close()

    def _on_advert(self, device, adv) -> None:
        data = None
        for uuid, value in (getattr(adv, "service_data", None) or {}).items():
            if str(uuid).lower() == SERVICE:
                data = value
        parsed = parse_service_data(data)
        if parsed is not None:
            self.adverts[parsed[0]] = Advert(parsed[0], parsed[1], getattr(adv, "rssi", None), time.monotonic(), device)

    async def _main(self) -> None:
        from bleak import BleakScanner
        # No UUID filter: a node puts its service UUID in the service data (the advertisement has no
        # room for it twice), and _on_advert looks there.
        scanner = BleakScanner(detection_callback=self._on_advert, adapter=self.adapter)
        scanning = False
        self.status.update(state="scanning", error="")
        try:
            while not self._stop.is_set():
                want_scan = not self._paused.is_set()
                if want_scan and not scanning:
                    await scanner.start()
                    scanning = True
                elif not want_scan and scanning:
                    await scanner.stop()
                    scanning = False
                now = time.monotonic()
                connect, unowned, strangers, waiting = choose(
                    self.adverts, self._known(), self._blocked(), set(self.links), self._on_usb(), self._failed,
                    now, self.limit)
                self.status.update(linked=len(self.links), waiting=waiting, unowned=unowned, strangers=len(strangers),
                                   state="paused" if not want_scan else "scanning")
                for hwid in unowned:
                    if hwid not in self._told:
                        self._told.add(hwid)
                        self._say("[BRISTLE] A Bristle board with no owner is in Bluetooth range (%s): "
                                  "plug it into USB to adopt it" % hwid, "info")
                if waiting and "limit" not in self._told:
                    self._told.add("limit")
                    self._say("[BRISTLE] Bluetooth link limit reached (%d): %d more node(s) in range are waiting"
                              % (self.limit, waiting), "warn")
                if want_scan:
                    for hwid in connect[:1]:                  # one at a time: BlueZ dislikes parallel connects
                        await self._connect(hwid)
                await asyncio.sleep(1.0)
        finally:
            if scanning:
                try:
                    await scanner.stop()
                except Exception:
                    pass
            for client in list(self.links.values()):
                try:
                    await client.disconnect()
                except Exception:
                    pass

    async def _connect(self, hwid: str) -> None:
        from bleak import BleakClient
        advert = self.adverts.get(hwid)
        if advert is None:
            return
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        reader = codec.LineReader()
        holder = {}

        def gone(_client) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, None)

        client = BleakClient(advert.device, disconnected_callback=gone, adapter=self.adapter)
        try:
            await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT_S)

            def notified(_sender, data: bytearray) -> None:
                for line in reader.feed(bytes(data) + b"\n"):
                    holder["session"].feed_line(line)

            session = self._new_session(lambda line: loop.call_soon_threadsafe(queue.put_nowait, line),
                                        proto.LINK_BLE)
            holder["session"] = session
            await client.start_notify(proto.BLE_NOTIFY_UUID, notified)
        except Exception as exc:
            self._failed[hwid] = time.monotonic()
            if "session" in holder:
                self._drop_session(holder["session"])
            try:
                await client.disconnect()
            except Exception:
                pass
            self._say("[BRISTLE] Could not link %s over Bluetooth: %s" % (hwid, str(exc) or type(exc).__name__), "warn")
            return
        self.links[hwid], self.sessions[hwid] = client, session
        session.start()
        asyncio.ensure_future(self._writer(hwid, client, session, queue))

    async def _writer(self, hwid: str, client, session, queue: asyncio.Queue) -> None:
        try:
            while True:
                line = await queue.get()
                if line is None:
                    break
                await client.write_gatt_char(proto.BLE_COMMAND_UUID, line.encode("ascii"), response=False)
        except Exception:
            pass
        finally:
            self.links.pop(hwid, None)
            self.sessions.pop(hwid, None)
            self._failed[hwid] = time.monotonic() - RETRY_AFTER_S + 3.0     # try again in a few seconds
            self._told.discard("limit")
            self._drop_session(session)
            try:
                await client.disconnect()
            except Exception:
                pass
