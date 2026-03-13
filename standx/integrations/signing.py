from __future__ import annotations

import base64
import binascii
import time
import uuid
from typing import Dict, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def _maybe_base58(value: str) -> Optional[bytes]:
    """Decode Base58 (Bitcoin alphabet). Returns None if not valid Base58."""
    value = value.strip()
    if not value:
        return None
    # Quick reject if contains non-base58 chars
    for ch in value:
        if ch not in _B58_INDEX:
            return None
    num = 0
    for ch in value:
        num = num * 58 + _B58_INDEX[ch]
    # Convert integer to bytes
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    # Handle leading zeros (Base58 '1')
    pad = 0
    for ch in value:
        if ch == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + raw


def _maybe_hex(value: str) -> Optional[bytes]:
    value = value.strip()
    if value.startswith("0x"):
        value = value[2:]
    if all(char in "0123456789abcdefABCDEF" for char in value) and len(value) % 2 == 0:
        try:
            return binascii.unhexlify(value)
        except Exception:
            return None
    return None


def parse_ed25519_private_key(key_str: str) -> bytes:
    key_str = key_str.strip()
    if not key_str:
        raise ValueError("empty ed25519 private key")

    raw = _maybe_hex(key_str)

    if raw is None:
        # Try Base58 (common for compact display)
        raw = _maybe_base58(key_str)

    if raw is None:
        # Accept base64 with or without padding
        b64 = key_str
        pad = (-len(b64)) % 4
        if pad:
            b64 = b64 + ("=" * pad)
        raw = base64.b64decode(b64)

    if len(raw) == 64:
        raw = raw[:32]
    if len(raw) != 32:
        raise ValueError(f"ed25519 key must be 32 bytes seed (got {len(raw)} bytes)")
    return raw


def sign_request(payload_json: str, ed25519_privkey_seed32: bytes, request_id: Optional[str] = None, timestamp_ms: Optional[int] = None) -> Dict[str, str]:
    version = "v1"
    request_id = request_id or uuid.uuid4().hex
    timestamp_ms = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)
    message = f"{version},{request_id},{timestamp_ms},{payload_json}".encode("utf-8")
    signature = Ed25519PrivateKey.from_private_bytes(ed25519_privkey_seed32).sign(message)
    return {
        "x-request-sign-version": version,
        "x-request-id": request_id,
        "x-request-timestamp": str(timestamp_ms),
        "x-request-signature": base64.b64encode(signature).decode("ascii"),
    }
