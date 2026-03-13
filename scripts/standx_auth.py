#!/usr/bin/env python3
"""standx_auth.py

StandX offchain auth helpers (EVM/BSC) + request signing scaffolding.

From StandX docs (under construction):
- Obtain JWT via wallet signature flow:
  1) POST https://api.standx.com/v1/offchain/prepare-signin?chain=bsc
     body: {"address": "0x...", "requestId": "uuid"}
     -> returns {success:true, signedData: <jwt>}
  2) Parse signedData JWT to get payload.message (EIP-4361 like text)
  3) Wallet signs message -> signature
  4) POST https://api.standx.com/v1/offchain/login?chain=bsc
     body: {"signature": "0x...", "signedData": <same jwt>}
     -> returns access token (JWT)

We do NOT implement wallet private key signing in this repo.
Instead we define interfaces so you can paste signature externally (e.g. from MetaMask or a signer).

Body signature:
- Docs mention ed25519 key pair + headers:
  x-request-timestamp
  x-request-signature
  x-request-sign-version
  x-request-id

Exact message format for body signing is not fully confirmed here; implement after verifying /perps-auth examples.

"""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests


@dataclass
class StandXAuthConfig:
    api_base: str = "https://api.standx.com"
    chain: str = "bsc"  # docs enum: bsc | solana


def prepare_signin(address: str, request_id: Optional[str] = None, cfg: StandXAuthConfig = StandXAuthConfig()) -> dict:
    request_id = request_id or str(uuid.uuid4())
    url = cfg.api_base.rstrip("/") + "/v1/offchain/prepare-signin"
    r = requests.post(url, params={"chain": cfg.chain}, json={"address": address, "requestId": request_id}, timeout=15)
    r.raise_for_status()
    return r.json()


def login(signature: str, signed_data_jwt: str, cfg: StandXAuthConfig = StandXAuthConfig()) -> dict:
    url = cfg.api_base.rstrip("/") + "/v1/offchain/login"
    r = requests.post(url, params={"chain": cfg.chain}, json={"signature": signature, "signedData": signed_data_jwt}, timeout=15)
    r.raise_for_status()
    return r.json()


def jwt_payload(jwt: str) -> dict:
    # naive JWT payload decode (no verification here)
    parts = jwt.split(".")
    if len(parts) < 2:
        raise ValueError("invalid jwt")
    b = parts[1]
    # pad base64url
    b += "=" * ((4 - len(b) % 4) % 4)
    raw = base64.urlsafe_b64decode(b.encode("utf-8"))
    return json.loads(raw)


def build_sign_headers_placeholder() -> Dict[str, str]:
    # placeholder values; fill with real body-signature algorithm later
    return {
        "x-request-timestamp": str(int(time.time())),
        "x-request-id": str(uuid.uuid4()),
        "x-request-sign-version": "1",
        "x-request-signature": "TODO",
    }
