"""Bristle owner lock and ESP-NOW frame signing. Reference implementation (stdlib only)."""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
from dataclasses import dataclass

from . import proto


def _h(key: bytes, *parts: bytes) -> bytes:
    if len(key) != proto.AUTH_KEY_BYTES:
        raise ValueError(f"owner key must be {proto.AUTH_KEY_BYTES} bytes")
    return hmac.new(key, b"".join(parts), hashlib.sha256).digest()


def new_key() -> bytes:
    return os.urandom(proto.AUTH_KEY_BYTES)


def new_nonce() -> bytes:
    return os.urandom(proto.AUTH_NONCE_BYTES)


def owner_id(key: bytes) -> str:
    return _h(key, proto.LABEL_OWNER_ID.encode())[: proto.AUTH_OWNER_ID_BYTES].hex()


def node_proof(key: bytes, core_nonce: bytes, hwid: bytes) -> str:
    return _h(key, proto.LABEL_NODE.encode(), core_nonce, hwid)[: proto.AUTH_PROOF_BYTES].hex()


def core_proof(key: bytes, node_nonce: bytes, hwid: bytes) -> str:
    return _h(key, proto.LABEL_CORE.encode(), node_nonce, hwid)[: proto.AUTH_PROOF_BYTES].hex()


def proofs_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.lower(), b.lower())


# ------------------------------------------------------------------ ESP-NOW frames

_HDR = struct.Struct("<2sBBIBB")   # magic, ver, kind, ctr, frag, len  (10 bytes)


class FrameError(ValueError):
    pass


@dataclass
class Frame:
    kind: int
    ctr: int
    frag: int
    payload: bytes


def frame_build(key: bytes, kind: int, ctr: int, frag: int, payload: bytes) -> bytes:
    if len(payload) > proto.FRAME_MAX_BYTES - proto.FRAME_HEADER_BYTES - proto.FRAME_TAG_BYTES:
        raise FrameError("payload too long")
    head = _HDR.pack(proto.FRAME_MAGIC.encode(), proto.PROTOCOL_VERSION, kind,
                     ctr & 0xFFFFFFFF, frag & 0xFF, len(payload))
    tag = _h(key, proto.FRAME_HMAC_LABEL.encode(), head, payload)[: proto.FRAME_TAG_BYTES]
    return head + payload + tag


def frame_parse(key: bytes, data: bytes) -> Frame:
    if len(data) < proto.FRAME_HEADER_BYTES + proto.FRAME_TAG_BYTES:
        raise FrameError("short frame")
    magic, ver, kind, ctr, frag, plen = _HDR.unpack_from(data, 0)
    if magic != proto.FRAME_MAGIC.encode():
        raise FrameError("bad magic")
    if ver != proto.PROTOCOL_VERSION:
        raise FrameError("bad version")
    end = proto.FRAME_HEADER_BYTES + plen
    if len(data) != end + proto.FRAME_TAG_BYTES:
        raise FrameError("bad length")
    want = _h(key, proto.FRAME_HMAC_LABEL.encode(), data[:end])[: proto.FRAME_TAG_BYTES]
    if not hmac.compare_digest(want, data[end:]):
        raise FrameError("bad tag")
    return Frame(kind, ctr, frag, data[proto.FRAME_HEADER_BYTES:end])


class ReplayGuard:
    """Per-sender strictly increasing counter check."""

    def __init__(self) -> None:
        self._last: dict[bytes, int] = {}

    def accept(self, sender: bytes, ctr: int) -> bool:
        last = self._last.get(sender)
        if last is not None and ctr <= last:
            return False
        self._last[sender] = ctr
        return True
