#!/usr/bin/env python3
"""standx_api_client_stub.py

StandX Perps API client skeleton (NOT LIVE).

Reason:
- User asked to evaluate whether StandX supports API trading.
- Docs show JWT-based auth (wallet signature) + body signature (ed25519) and HTTP order endpoints.

This file is a stub to be filled once we confirm:
- Base URL values
- Exact endpoints paths
- Exact headers for body signature
- JWT acquisition flow endpoints

We intentionally do not implement wallet signing or store private keys here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class StandXConfig:
    base_url: str
    jwt: Optional[str] = None


class StandXClient:
    def __init__(self, cfg: StandXConfig):
        self.cfg = cfg

    def create_order(self, payload: dict) -> dict:
        raise NotImplementedError("Need confirmed endpoint + auth/signature headers")

    def cancel_order(self, payload: dict) -> dict:
        raise NotImplementedError

    def query_positions(self) -> dict:
        raise NotImplementedError
