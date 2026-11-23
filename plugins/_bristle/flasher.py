# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""[bristle] Node firmware on this machine: which images there are, checking them, writing one to
a board plugged into the uConsole with esptool, and updating a node over its link.

Images come from a release folder (bristle/firmware/releases/<release>/ in the install folder, or
the folder named by firmware_dir in bristle.conf):

    <board>/bristle-<board>-FULL.bin    the whole flash, written at 0x0 after a full erase
    <board>/bristle-<board>-app.bin     the app alone
    SHA256SUMS                          one line per image

An image is used only if its SHA-256 matches its line in SHA256SUMS.

First flash of a board: erase everything, then the FULL image at 0x0. That puts the Bristle flash
layout on it (two update slots and the find queue).
Later flash over USB: the app image at 0x10000, and the two sectors that say which slot boots are
cleared so the board starts the image just written (after an update over the air it may be
running from the other slot). Owner, node number, settings and stored finds are kept.

All of it runs on worker threads. Nothing is written to a port the port rules would not let
Bristle say hello to (link_usb.PortPolicy), and never to the port the game's own ESP32 is on.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import ota, proto

BOARDS = ("xiao_c5", "devkit_c5", "devkit_c5_uart")
BOARD_NAMES = {"xiao_c5": "Seeed XIAO ESP32-C5", "devkit_c5": "ESP32-C5 board, native USB port",
               "devkit_c5_uart": "ESP32-C5 board, CH343/UART port"}
CHIP = "esp32c5"
FLASH_BAUD = "921600"
APP_OFFSET, OTADATA_OFFSET, OTADATA_BYTES = "0x10000", "0xe000", "0x2000"
_BUILD_RE = re.compile(r"build(\d+)")


class FirmwareError(Exception):
    pass


def build_of(release_name: str):
    """The build number in a release folder's name ("v1-build4-dev" -> 4), or None."""
    found = _BUILD_RE.search(release_name)
    return int(found.group(1)) if found else None


def releases(install_dir, override: str = "") -> list:
    """Release folders, newest build first."""
    base = Path(override) if override else Path(install_dir) / "bristle" / "firmware" / "releases"
    try:
        found = [p for p in base.iterdir() if p.is_dir() and build_of(p.name) is not None
                 and (p / "SHA256SUMS").is_file()]
    except OSError:
        return []
    return sorted(found, key=lambda p: build_of(p.name), reverse=True)


def image(release: Path, board: str, kind: str) -> Path:
    """kind: "app" or "FULL". Raises FirmwareError unless the file is there and its hash matches."""
    if board not in BOARDS:
        raise FirmwareError("no firmware for a board called %r" % board)
    name = "%s/bristle-%s-%s.bin" % (board, board, kind)
    path = release / board / ("bristle-%s-%s.bin" % (board, kind))
    wanted = None
    try:
        for line in (release / "SHA256SUMS").read_text(encoding="ascii").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*").replace("\\", "/") == name:
                wanted = parts[0].lower()
        data = path.read_bytes()
    except (OSError, UnicodeError) as exc:
        raise FirmwareError("%s is missing in %s (%s)" % (name, release.name, exc)) from exc
    if wanted is None:
        raise FirmwareError("%s is not listed in %s/SHA256SUMS" % (name, release.name))
    if hashlib.sha256(data).hexdigest() != wanted:
        raise FirmwareError("%s does not match its SHA-256 in SHA256SUMS: not used" % name)
    return path


def guess_board(vid, pid) -> str:
    """A first guess for the flash screen; you can change it there."""
    return "devkit_c5_uart" if (vid, pid) in ((0x1A86, 0x55D3), (0x1A86, 0x7523), (0x10C4, 0xEA60)) else "xiao_c5"


def flash_commands(python: str, port: str, path: str, first: bool) -> list:
    """The esptool runs for one flash, in order (esptool 5 command names)."""
    base = [python, "-m", "esptool", "--chip", CHIP, "--port", port, "--baud", FLASH_BAUD]
    if first:
        return [base + ["erase-flash"], base + ["write-flash", "0x0", path]]
    return [base + ["erase-region", OTADATA_OFFSET, OTADATA_BYTES], base + ["write-flash", APP_OFFSET, path]]


def _run(command: list, on_line) -> None:
    """One esptool run, through the game's own supervised runner when it is there."""
    try:
        from watchdogs.esp32_flash import run_esptool
    except Exception:
        run_esptool = None
    if run_esptool is not None:
        run_esptool(command, on_line)
        return
    done = subprocess.run(command, capture_output=True, text=True, timeout=600)
    for line in (done.stdout or "").splitlines()[-6:]:
        on_line(line)
    if done.returncode != 0:
        raise FirmwareError((done.stderr or done.stdout or "esptool failed").strip().splitlines()[-1])


class FlashJob:
    """Writes one image to one port, on its own thread. Read ``state``, ``line`` and ``error``."""

    def __init__(self, port: str, path: Path, first: bool, done=lambda job: None, run=_run,
                 python: str = sys.executable) -> None:
        self.port, self.path, self.first = port, Path(path), first
        self.commands = flash_commands(python, port, str(path), first)
        self._done, self._run = done, run
        self.state = "waiting"               # waiting | erasing | writing | done | failed
        self.line = ""
        self.error = ""
        self.thread = threading.Thread(target=self._work, name="bristle-flash", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _note(self, text) -> None:
        text = str(text).strip()
        if text:
            self.line = text[-110:]

    def _work(self) -> None:
        try:
            for index, command in enumerate(self.commands):
                self.state = "erasing" if index == 0 else "writing"
                self._run(command, self._note)
            self.state = "done"
        except Exception as exc:
            self.state, self.error = "failed", (str(exc) or type(exc).__name__)[:200]
        finally:
            self._done(self)


class OtaJob:
    """One node being updated over its link. The service feeds it the node's answers."""

    def __init__(self, session, release: Path, board: str) -> None:
        self.session = session
        self.hwid, self.number = session.hwid, session.number
        self.release = release
        self.build = build_of(release.name)
        self.path = image(release, board, "app")
        self.sender = ota.OtaSender(self.path.read_bytes(), session.ota,
                                    timeout_s=ota.ANSWER_TIMEOUT_S.get(session.link_type, 3.0))
        self.started = time.monotonic()
        self.finished = 0.0
        self.result = ""                     # filled in when the node is back: took / rolled back

    @property
    def link_name(self) -> str:
        return {proto.LINK_USB: "USB", proto.LINK_ESPNOW: "ESP-NOW", proto.LINK_BLE: "Bluetooth"}.get(
            self.session.link_type, "?")

    def text(self) -> str:
        s = self.sender
        if s.state == ota.FAILED:
            return "Node %s update failed: %s" % (self.number, s.error)
        if s.state == ota.DONE:
            return self.result or "Node %s took build %s and is restarting" % (self.number, self.build)
        return "Node %s: build %s over %s, %d%% (%s)" % (self.number, self.build, self.link_name, s.percent, s.state)
