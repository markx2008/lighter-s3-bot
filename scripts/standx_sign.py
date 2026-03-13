#!/usr/bin/env python3
"""standx_sign.py

StandX request body signing (ed25519) for "important operations".

Based on StandX Perps Auth EVM example (docs):
- version = "v1"
- requestId = random string
- timestamp = milliseconds
- payload = JSON string (compact, no spaces)

message = f"{version},{requestId},{timestamp},{payload}"

signature = ed25519.sign(message_bytes, ed25519PrivateKey)
header x-request-signature = base64(signature)

This key is NOT your wallet private key. It is a temporary signing key generated
by StandX session page and can be revoked.

Key formats supported:
- hex string (with/without 0x)
- base64 string
- raw 32-byte seed encoded as above

"""

from __future__ import annotations

import base64
import binascii
import os
import uuid
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _maybe_hex(s: str) -> Optional[bytes]:
    s = s.strip()
    if s.startswith("0x"):
        s = s[2:]
    if all(c in "0123456789abcdefABCDEF" for c in s) and len(s) % 2 == 0:
        try:
            return binascii.unhexlify(s)
        except Exception:
            return None
    return None


def parse_ed25519_private_key(key_str: str) -> bytes:
    """Return 32-byte seed."""
    key_str = key_str.strip()
    if not key_str:
        raise ValueError("empty ed25519 private key")

    hb = _maybe_hex(key_str)
    if hb is not None:
        raw = hb
    else:
        # base64
        raw = base64.b64decode(key_str)

    # Some libs export 64 bytes (seed+pubkey). StandX example uses 32-byte secret key.
    if len(raw) == 64:
        raw = raw[:32]
    if len(raw) != 32:
        raise ValueError(f"ed25519 key must be 32 bytes seed (got {len(raw)} bytes)")
    return raw


def sign_request(payload_json: str, ed25519_privkey_seed32: bytes, request_id: Optional[str] = None, timestamp_ms: Optional[int] = None) -> Dict[str, str]:
    import time

    version = "v1"
    request_id = request_id or uuid.uuid4().hex
    timestamp_ms = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)

    msg = f"{version},{request_id},{timestamp_ms},{payload_json}".encode("utf-8")
    pk = Ed25519PrivateKey.from_private_bytes(ed25519_privkey_seed32)
    sig = pk.sign(msg)

    return {
        "x-request-sign-version": version,
        "x-request-id": request_id,
        "x-request-timestamp": str(timestamp_ms),
        "x-request-signature": base64.b64encode(sig).decode("ascii"),
    }
