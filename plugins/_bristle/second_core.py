# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""A second core: carry the owner key and Node Memory to another install, locked with a passphrase.

The file is JSON: the scrypt settings, a salt, a nonce and the sealed data (AES-256-GCM, from the
``cryptography`` package the game already needs). The key is stretched with scrypt, so a short
passphrase still costs real work to guess, and the header is bound into the seal, so nothing in the
file can be changed without the import failing. The owner key itself is never shown or logged.

Only one core runs the fleet at a time. That is settled on the nodes, not here: a node that one
core is running answers "busy" to another (see session.BUSY).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time

FORMAT = "bristle-core-export"
VERSION = 1
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
MIN_PASSPHRASE = 8
FILE_NAME = "bristle-core-export.json"


class ExportError(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _stretch(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32,
                          maxmem=256 * 1024 * 1024)


def _aead():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:                  # the game lists it in requirements.txt
        raise ExportError("the Python package 'cryptography' is not installed") from exc
    return AESGCM


def _header(salt: bytes, n: int, r: int, p: int) -> dict:
    return {"format": FORMAT, "version": VERSION, "kdf": "scrypt", "n": n, "r": r, "p": p, "salt": _b64(salt),
            "cipher": "AES-256-GCM"}


def _bound(header: dict) -> bytes:
    return json.dumps(header, sort_keys=True, separators=(",", ":")).encode("ascii")


def export_bundle(owner_key: bytes, node_memory: dict, passphrase: str, made_at: float | None = None,
                  n: int = SCRYPT_N) -> bytes:
    """The export file's bytes. ``node_memory`` is the content of nodes.json."""
    if len(owner_key) != 16:
        raise ExportError("no owner key to export")
    if len(passphrase) < MIN_PASSPHRASE:
        raise ExportError("the passphrase needs at least %d characters" % MIN_PASSPHRASE)
    salt, nonce = os.urandom(16), os.urandom(12)
    header = _header(salt, n, SCRYPT_R, SCRYPT_P)
    inner = json.dumps({"owner_key": owner_key.hex(), "node_memory": node_memory,
                        "made_at": int(made_at if made_at is not None else time.time())}).encode("utf-8")
    sealed = _aead()(_stretch(passphrase, salt, n, SCRYPT_R, SCRYPT_P)).encrypt(nonce, inner, _bound(header))
    return (json.dumps(dict(header, nonce=_b64(nonce), data=_b64(sealed)), indent=1) + "\n").encode("ascii")


def import_bundle(data: bytes, passphrase: str) -> tuple[bytes, dict, int]:
    """(owner key, node memory, when it was made). Raises ExportError with a plain reason."""
    try:
        outer = json.loads(data.decode("ascii"))
        if outer.get("format") != FORMAT:
            raise ValueError
        if outer.get("version") != VERSION or outer.get("kdf") != "scrypt" or outer.get("cipher") != "AES-256-GCM":
            raise ExportError("this export was made by a newer version: update this install first")
        n, r, p = int(outer["n"]), int(outer["r"]), int(outer["p"])
        salt, nonce, sealed = (base64.b64decode(outer[k]) for k in ("salt", "nonce", "data"))
    except ExportError:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise ExportError("not a Bristle core export file") from exc
    if n > 2 ** 20 or r > 16 or p > 4 or n < 2 ** 10:
        raise ExportError("not a Bristle core export file")      # never spend minutes on a forged header
    header = _header(salt, n, r, p)
    try:
        inner = _aead()(_stretch(passphrase, salt, n, r, p)).decrypt(nonce, sealed, _bound(header))
        content = json.loads(inner.decode("utf-8"))
        key = bytes.fromhex(content["owner_key"])
        memory = content["node_memory"]
    except ExportError:
        raise
    except Exception as exc:                    # InvalidTag and anything a damaged file can cause
        raise ExportError("wrong passphrase, or the file is damaged") from exc
    if len(key) != 16 or not isinstance(memory, dict):
        raise ExportError("the file does not hold an owner key and Node Memory")
    return key, memory, int(content.get("made_at", 0))


def merge_node_memory(mine: dict, theirs: dict) -> tuple[dict, int, int]:
    """Node Memory after an import: every node of the export, plus what this install already had.

    A node both know keeps this install's record. A node number that is taken here moves the
    imported node to the next free number (the node is told its new number when it connects).
    Returns (merged, nodes added, nodes renumbered).
    """
    merged = {"version": mine.get("version", 1), "nodes": [dict(n) for n in mine.get("nodes", [])],
              "blocked": dict(mine.get("blocked", {})), "not_nodes": dict(mine.get("not_nodes", {}))}
    have = {n.get("hwid") for n in merged["nodes"]}
    used = {n.get("number") for n in merged["nodes"]}
    added = moved = 0
    for node in theirs.get("nodes", []):
        if not isinstance(node, dict) or not node.get("hwid") or node["hwid"] in have:
            continue
        if node["hwid"] in merged["blocked"]:
            continue                              # blocked here stays blocked
        node = dict(node)
        node["usb_key"] = ""                      # a USB port on the other machine means nothing here
        if node.get("number") in used or not isinstance(node.get("number"), int):
            number = 1
            while number in used:
                number += 1
            node["number"] = number
            moved += 1
        used.add(node["number"])
        have.add(node["hwid"])
        merged["nodes"].append(node)
        added += 1
    for hwid, note in theirs.get("blocked", {}).items():
        if hwid not in have:
            merged["blocked"].setdefault(hwid, note)
    merged["nodes"].sort(key=lambda n: n["number"])
    return merged, added, moved
