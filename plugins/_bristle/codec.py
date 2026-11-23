"""Bristle line codec: CRC-16, percent-encoding, building and parsing lines.

Pure Python, no dependencies. This is the reference implementation: the firmware's
Codec.cpp must produce byte-identical output (checked by the spec's test vectors).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import proto

_UNRESERVED = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~/"
)
_HEX = "0123456789ABCDEF"


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xor-out."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def pct_encode(value: str | bytes) -> str:
    """Encode every byte outside [A-Za-z0-9-_.~/] as %HH (upper-case)."""
    raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    out = []
    for b in raw:
        if b in _UNRESERVED:
            out.append(chr(b))
        else:
            out.append("%" + _HEX[b >> 4] + _HEX[b & 15])
    return "".join(out)


def pct_decode_bytes(text: str) -> bytes:
    """Inverse of pct_encode. Raises ValueError on a malformed escape."""
    out = bytearray()
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "%":
            hx = text[i + 1:i + 3]
            if len(hx) != 2 or any(c not in "0123456789abcdefABCDEF" for c in hx):
                raise ValueError("bad escape")
            out.append(int(hx, 16))
            i += 3
        else:
            out.append(ord(ch))
            i += 1
    return bytes(out)


def pct_decode(text: str) -> str:
    """Decode to str (UTF-8, invalid bytes replaced)."""
    return pct_decode_bytes(text).decode("utf-8", "replace")


class LineError(ValueError):
    """A line that must be dropped (bad CRC, malformed, too long, unknown type...)."""


@dataclass
class Message:
    type: str
    fields: dict[str, str] = field(default_factory=dict)   # decoded values

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.fields.get(key, default)

    def int(self, key: str, default: int | None = None) -> int | None:
        v = self.fields.get(key)
        if v is None or v == "":
            return default
        return int(v, 10)


def body(msg_type: str, fields: dict[str, object] | None = None) -> str:
    """The part of a line before '*': TYPE:k=v,k=v (values percent-encoded)."""
    if not msg_type.isupper() or not msg_type.isalpha():
        raise ValueError(f"bad type {msg_type!r}")
    parts = []
    for k, v in (fields or {}).items():
        if v is None:
            continue
        if isinstance(v, bool):
            v = int(v)
        parts.append(f"{k}={pct_encode(str(v))}")
    return f"{msg_type}:{','.join(parts)}"


def build(msg_type: str, fields: dict[str, object] | None = None) -> str:
    """A full line, with CRC and trailing newline."""
    b = body(msg_type, fields)
    line = f"{b}*{crc16(b.encode('ascii')):04X}\n"
    if len(line.encode("ascii")) > proto.LINE_MAX_BYTES:
        raise LineError(f"line too long ({len(line)} > {proto.LINE_MAX_BYTES})")
    return line


def parse_body(text: str) -> Message:
    """Parse 'TYPE:k=v,...' (no CRC). Used for ESP-NOW payloads (the frame HMAC covers them)."""
    if ":" not in text:
        raise LineError("no ':'")
    msg_type, _, rest = text.partition(":")
    if not (2 <= len(msg_type) <= 6) or not msg_type.isalpha() or not msg_type.isupper():
        raise LineError(f"bad type {msg_type!r}")
    fields: dict[str, str] = {}
    if rest:
        for part in rest.split(","):
            if "=" not in part:
                raise LineError(f"field without '=': {part!r}")
            k, _, v = part.partition("=")
            if not k:
                raise LineError("empty key")
            try:
                fields[k] = pct_decode(v)
            except ValueError as exc:
                raise LineError(str(exc)) from exc
    return Message(msg_type, fields)


def parse(line: str | bytes) -> Message:
    """Parse one full line (with or without the trailing newline). Raises LineError."""
    if isinstance(line, bytes):
        try:
            line = line.decode("ascii")
        except UnicodeDecodeError as exc:
            raise LineError("non-ASCII line") from exc
    line = line.rstrip("\r\n")
    if len(line) + 1 > proto.LINE_MAX_BYTES:
        raise LineError("line too long")
    star = line.rfind("*")
    if star < 0 or len(line) - star != 5:
        raise LineError("no CRC")
    b, crc_text = line[:star], line[star + 1:]
    try:
        want = int(crc_text, 16)
    except ValueError as exc:
        raise LineError("bad CRC text") from exc
    if crc16(b.encode("ascii")) != want:
        raise LineError("bad CRC")
    return parse_body(b)


def check_required(msg: Message) -> None:
    """Raise LineError if the message type is unknown or a required field is missing."""
    spec = proto.MESSAGES.get(msg.type)
    if spec is None:
        raise LineError(f"unknown type {msg.type}")
    missing = [k for k in spec["req"] if k not in msg.fields]
    if missing:
        raise LineError(f"{msg.type} missing {','.join(missing)}")


class LineReader:
    """Accumulates bytes from a stream and yields complete lines (bytes, without newline).

    Over-long input without a newline is discarded and counted, so a noisy port can't grow memory.
    """

    def __init__(self, max_bytes: int | None = None) -> None:
        self._buf = bytearray()
        self._max = max_bytes or proto.LINE_MAX_BYTES
        self.overflows = 0

    def feed(self, data: bytes) -> list[bytes]:
        out: list[bytes] = []
        for b in data:
            if b == 0x0A:  # \n
                if self._buf:
                    out.append(bytes(self._buf).rstrip(b"\r"))
                self._buf.clear()
            else:
                if len(self._buf) >= self._max:
                    self.overflows += 1
                    self._buf.clear()
                    continue
                self._buf.append(b)
        return out
