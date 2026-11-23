# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""Firmware updates over a node link: the core's side.

One OtaSender per update. It is fed the node's answers (OTAS lines, RSP lines) and a clock tick,
and it calls ``send(fields)`` with the fields of each OTA command. It knows nothing about the
link, so the same code drives USB, ESP-NOW and Bluetooth.

    begin (size, sha256)  ->  node erases its spare app slot, answers  OTAS st=receiving,o=0
    chunk (o, d) ...      ->  node writes it, answers                  OTAS st=receiving,o=<next>
    verify                ->  node reads the slot back, checks sha256  OTAS st=ready
    commit                ->  node makes the slot the boot image and restarts

The node only ever takes the chunk it is waiting for; anything else it answers with the offset it
wants. So a lost line costs a resend from that offset, never a damaged image. Nothing changes what
the node boots until commit, so an update that is cut off leaves the node on its old firmware. A
committed image that does not come back and meet its owner is rolled back by the node itself.
"""
from __future__ import annotations

import base64
import hashlib
import time
from typing import Callable

from . import proto

IDLE, BEGIN, SENDING, VERIFY, COMMIT, DONE, FAILED = "idle", "begin", "sending", "verify", "commit", "done", "failed"

ESP_IMAGE_MAGIC = 0xE9
# Seconds without an answer before the last step is repeated, per link type.
ANSWER_TIMEOUT_S = {proto.LINK_USB: 2.0, proto.LINK_ESPNOW: 4 * proto.ESPNOW_VISIT_INTERVAL_MS / 1000,
                    proto.LINK_BLE: 4.0}


class OtaError(ValueError):
    pass


def check_image(data: bytes, slot_bytes: int = proto.FLASH_APP_SLOT_BYTES) -> None:
    """Refuse anything that is plainly not an app image for a node, before a byte is sent."""
    if len(data) < 1024:
        raise OtaError("the file is too small to be a firmware image")
    if data[0] != ESP_IMAGE_MAGIC:
        raise OtaError("not an ESP32 app image (it must be the -app.bin file, not the FULL image)")
    if len(data) > slot_bytes:
        raise OtaError("the image (%d bytes) does not fit a node's update slot (%d bytes)" % (len(data), slot_bytes))


def chunk_fields(offset: int, data: bytes) -> dict:
    return {"op": "chunk", "o": offset, "d": base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")}


class OtaSender:
    def __init__(self, image: bytes, send: Callable[[dict], None], chunk_bytes: int = proto.OTA_CHUNK_BYTES,
                 window: int = proto.OTA_WINDOW, timeout_s: float = 2.0, max_stalls: int = 12,
                 mono: Callable[[], float] = time.monotonic) -> None:
        check_image(image)
        self.image = image
        self.sha = hashlib.sha256(image).hexdigest()
        self._send = send
        self.chunk_bytes, self.window = chunk_bytes, max(1, window)
        self.timeout_s, self.max_stalls = timeout_s, max_stalls
        self._mono = mono
        self.state = IDLE
        self.error = ""
        self.acked = 0                   # the node holds every byte below this
        self._sent = 0                   # every byte below this has been sent at least once
        self._last_heard = 0.0
        self._stalls = 0
        self._rewound = -1               # the offset the sender last went back to
        self.resends = 0

    # ------------------------------------------------------------------ what the screen shows
    @property
    def active(self) -> bool:
        return self.state in (BEGIN, SENDING, VERIFY, COMMIT)

    @property
    def percent(self) -> int:
        return 100 if self.state in (COMMIT, DONE) else int(self.acked * 100 / max(1, len(self.image)))

    # ------------------------------------------------------------------ steps
    def start(self) -> None:
        self.state = BEGIN
        self.acked = self._sent = self._stalls = 0
        self._heard()
        self._send({"op": "begin", "size": len(self.image), "sha": self.sha})

    def abort(self, why: str = "stopped") -> None:
        if self.active:
            self._send({"op": "abort"})
        self._fail(why)

    def _fail(self, why: str) -> None:
        if self.state != DONE:
            self.state, self.error = FAILED, why

    def _heard(self) -> None:
        self._last_heard = self._mono()

    def _fill_window(self) -> None:
        limit = min(len(self.image), self.acked + self.window * self.chunk_bytes)
        while self._sent < limit:
            end = min(self._sent + self.chunk_bytes, len(self.image))
            self._send(chunk_fields(self._sent, self.image[self._sent:end]))
            self._sent = end

    def on_status(self, fields: dict) -> None:
        """An OTAS line from the node."""
        if not self.active:
            return
        status = fields.get("st", "")
        self._heard()
        if status == "error":
            self._fail(fields.get("e") or "the node reported an error")
            return
        if status == "receiving" and self.state in (BEGIN, SENDING):
            try:
                want = int(fields.get("o", "0"))
            except ValueError:
                return
            if want < 0 or want > len(self.image):
                self._fail("the node asked for a byte outside the image")
                return
            if want > self.acked or self.state == BEGIN:
                self._stalls = 0
            if want < self._sent and want == self.acked and self.state == SENDING and self._rewound != want:
                # It is still waiting for the same chunk: what came after was lost. Go back, once.
                self._sent = want
                self._rewound = want
                self.resends += 1
            self.acked = max(self.acked, want) if self.state == SENDING else want
            self._sent = max(self._sent, self.acked)
            self.state = SENDING
            if self.acked >= len(self.image):
                self.state = VERIFY
                self._send({"op": "verify"})
            else:
                self._fill_window()
        elif status == "ready" and self.state == VERIFY:
            self.state = COMMIT
            self._send({"op": "commit"})
        elif status == "idle" and self.state in (SENDING, VERIFY):
            self._fail("the node dropped the update (it restarted, or its link closed)")

    def on_rsp(self, ok: bool, error: str | None = None) -> None:
        """The RSP to an OTA command."""
        if not self.active:
            return
        self._heard()
        if not ok:
            self._fail(error or "the node refused the update")
        elif self.state == COMMIT:
            self.state = DONE            # it restarts into the new image now

    def link_lost(self) -> None:
        if self.state == COMMIT:
            self.state = DONE            # the restart closed the link before the answer got out
        elif self.active:
            self._fail("the link closed during the update: the node keeps its old firmware")

    def tick(self) -> None:
        if not self.active or self._mono() - self._last_heard < self.timeout_s:
            return
        self._stalls += 1
        self._rewound = -1
        self._heard()
        if self.state == COMMIT and self._stalls >= 2:
            self.state = DONE            # no answer after commit: it is restarting into the new image
            return
        if self._stalls > self.max_stalls:
            self.abort("the node stopped answering: it keeps its old firmware")
            return
        self.resends += 1
        if self.state == BEGIN:
            self._send({"op": "begin", "size": len(self.image), "sha": self.sha})
        elif self.state == SENDING:
            self._sent = self.acked
            self._fill_window()
        elif self.state == VERIFY:
            self._send({"op": "verify"})
        elif self.state == COMMIT:
            self._send({"op": "status"})
