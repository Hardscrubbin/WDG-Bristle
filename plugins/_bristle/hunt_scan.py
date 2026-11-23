# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""[bristle] Hunt mode's own listener: a passive Bluetooth LE scan on one adapter.

Passive means the adapter only receives. It sends no scan requests and never connects, so the
device being hunted cannot tell it is being listened to. Every advertisement from the hunted MAC
gives one signal reading.

It talks to the adapter directly (a raw HCI socket), because BlueZ's ordinary discovery is an
active scan. That needs Linux and root rights (the game normally has them), and the adapter must
not be scanning for anyone else at that moment: the caller pauses Bristle's own Bluetooth link
scan, or borrows the adapter from Kismet, first. If any of that is missing start() says why and
the hunt goes on with the readings Kismet and the nodes give.

Bluetooth Classic devices cannot be listened to this way: a Classic device that is not connected
transmits only when it answers an inquiry. For those the hunt uses Kismet's readings.

The packet formats are from the Bluetooth Core Specification, Vol 4 Part E, section 7.7.65
(LE Advertising Report, LE Extended Advertising Report) and 7.8 (LE controller commands).
"""
from __future__ import annotations

import socket
import struct
import threading
import time

HCI_COMMAND, HCI_EVENT = 0x01, 0x04
EVT_CMD_COMPLETE, EVT_CMD_STATUS, EVT_LE_META = 0x0E, 0x0F, 0x3E
SUB_ADV_REPORT, SUB_EXT_ADV_REPORT = 0x02, 0x0D
OGF_LE = 0x08
OCF_SET_SCAN_PARAMS, OCF_SET_SCAN_ENABLE = 0x000B, 0x000C
OCF_SET_EXT_SCAN_PARAMS, OCF_SET_EXT_SCAN_ENABLE = 0x0041, 0x0042
SCAN_PASSIVE = 0x00
INTERVAL = WINDOW = 0x0060          # 60 ms and 60 ms: listening all the time


def opcode(ogf: int, ocf: int) -> int:
    return (ogf << 10) | ocf


def command(ogf: int, ocf: int, params: bytes = b"") -> bytes:
    return struct.pack("<BHB", HCI_COMMAND, opcode(ogf, ocf), len(params)) + params


def scan_params_command(extended: bool) -> bytes:
    if extended:
        # own address type, filter policy, PHYs (1M only), then per PHY: type, interval, window
        return command(OGF_LE, OCF_SET_EXT_SCAN_PARAMS,
                       struct.pack("<BBBBHH", 0x00, 0x00, 0x01, SCAN_PASSIVE, INTERVAL, WINDOW))
    return command(OGF_LE, OCF_SET_SCAN_PARAMS, struct.pack("<BHHBB", SCAN_PASSIVE, INTERVAL, WINDOW, 0x00, 0x00))


def scan_enable_command(extended: bool, on: bool) -> bytes:
    if extended:
        # enable, filter duplicates (off: every advertisement counts), duration, period
        return command(OGF_LE, OCF_SET_EXT_SCAN_ENABLE, struct.pack("<BBHH", int(on), 0x00, 0, 0))
    return command(OGF_LE, OCF_SET_SCAN_ENABLE, struct.pack("<BB", int(on), 0x00))


def mac_text(raw: bytes) -> str:
    """The six address bytes as they come over HCI (last byte first) -> 12 hex, printed order."""
    return bytes(reversed(raw)).hex()


def parse_le_reports(packet: bytes) -> list:
    """[(mac, rssi)] from one HCI packet as read from the socket; [] for anything else."""
    out = []
    if len(packet) < 5 or packet[0] != HCI_EVENT or packet[1] != EVT_LE_META:
        return out
    body = packet[3:3 + packet[2]]
    if len(body) < 2:
        return out
    sub, count, at = body[0], body[1], 2
    try:
        for _ in range(count):
            if sub == SUB_ADV_REPORT:
                mac = mac_text(body[at + 2:at + 8])
                length = body[at + 8]
                rssi = struct.unpack_from("b", body, at + 9 + length)[0]
                at += 10 + length
            elif sub == SUB_EXT_ADV_REPORT:
                mac = mac_text(body[at + 3:at + 9])
                rssi = struct.unpack_from("b", body, at + 13)[0]
                length = body[at + 23]
                at += 24 + length
            else:
                return out
            if len(mac) == 12 and rssi != 127:           # 127: the controller has no reading
                out.append((mac, rssi))
    except (IndexError, struct.error):
        pass
    return out


def command_result(packet: bytes, wanted_opcode: int):
    """The status byte if this packet answers the command with that opcode; else None."""
    if len(packet) < 3 or packet[0] != HCI_EVENT:
        return None
    if packet[1] == EVT_CMD_COMPLETE and len(packet) >= 7 and struct.unpack_from("<H", packet, 4)[0] == wanted_opcode:
        return packet[6]
    if packet[1] == EVT_CMD_STATUS and len(packet) >= 7 and struct.unpack_from("<H", packet, 5)[0] == wanted_opcode:
        return packet[3]
    return None


def adapter_index(adapter: str) -> int:
    if not adapter.startswith("hci") or not adapter[3:].isdigit():
        raise ValueError("not a Bluetooth adapter name: %r" % adapter)
    return int(adapter[3:])


class PassiveScanner:
    """Owns the socket and a reader thread. on_reading(rssi) is called on that thread."""

    def __init__(self, adapter: str, target_mac: str, on_reading) -> None:
        self.adapter = adapter
        self.target = "".join(c for c in target_mac.lower() if c in "0123456789abcdef")
        self._on_reading = on_reading
        self._sock = None
        self._extended = False
        self._stop = threading.Event()
        self._thread = None
        self.readings = 0
        self.error = ""

    def _send(self, sock, packet: bytes, timeout: float = 2.0) -> int:
        """Send one command and wait for its answer. Returns the HCI status (0 = done)."""
        wanted = struct.unpack_from("<H", packet, 1)[0]
        sock.send(packet)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                answer = sock.recv(260)
            except socket.timeout:
                continue
            status = command_result(answer, wanted)
            if status is not None:
                return status
        return -1

    def start(self) -> bool:
        """False (with self.error set) if this machine or this adapter cannot do it."""
        if not hasattr(socket, "AF_BLUETOOTH") or not hasattr(socket, "BTPROTO_HCI"):
            self.error = "this system has no raw Bluetooth sockets"
            return False
        try:
            sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
            sock.bind((adapter_index(self.adapter),))
            sock.settimeout(0.25)
            events = (1 << EVT_CMD_COMPLETE) | (1 << EVT_CMD_STATUS)
            sock.setsockopt(socket.SOL_HCI, socket.HCI_FILTER,
                            struct.pack("<IIIH", 1 << HCI_EVENT, events, 1 << (EVT_LE_META - 32), 0))
        except PermissionError:
            self.error = "listening on %s needs root rights" % self.adapter
            return False
        except (OSError, ValueError) as exc:
            self.error = "%s: %s" % (self.adapter, exc)
            return False
        for extended in (True, False):
            if self._send(sock, scan_params_command(extended)) == 0 and \
                    self._send(sock, scan_enable_command(extended, True)) == 0:
                self._sock, self._extended = sock, extended
                self._thread = threading.Thread(target=self._run, name="bristle-hunt-scan", daemon=True)
                self._thread.start()
                return True
        sock.close()
        self.error = "%s is busy scanning for another program (or refused a passive scan)" % self.adapter
        return False

    def _run(self) -> None:
        sock = self._sock
        while not self._stop.is_set():
            try:
                packet = sock.recv(512)
            except socket.timeout:
                continue
            except OSError:
                break
            for mac, rssi in parse_le_reports(packet):
                if mac == self.target:
                    self.readings += 1
                    try:
                        self._on_reading(rssi)
                    except Exception:
                        pass

    def stop(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if self._thread is not None:
            self._thread.join(1.0)
        if sock is not None:
            try:
                sock.send(scan_enable_command(self._extended, False))
                time.sleep(0.05)
            except OSError:
                pass
            sock.close()
