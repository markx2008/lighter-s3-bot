#!/usr/bin/env python3
"""standx_client.py

Minimal StandX Perps HTTP client.

We can access public endpoints without auth:
- /api/kline/time
- /api/query_symbol_info
- /api/query_symbol_price?symbol=BTC-USD
- /api/query_depth_book?symbol=BTC-USD&limit=...

Private endpoints require:
- JWT (Authorization header)
- body signature headers (ed25519) for some endpoints
- optional session_id headers to correlate with WS order response stream

Because the docs are still "under construction", this module is designed to be
filled incrementally. For now we focus on public endpoints + request scaffolding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests


@dataclass
class StandXConfig:
    base_url: str = "https://perps.standx.com"
    jwt: Optional[str] = None
    session_id: Optional[str] = None


class StandXClient:
    def __init__(self, cfg: StandXConfig):
        self.cfg = cfg

    # ---------- helpers ----------
    def _headers(self, signed: bool = False, sign_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h: Dict[str, str] = {"Content-Type": "application/json"}
        if self.cfg.jwt:
            h["Authorization"] = f"Bearer {self.cfg.jwt}"
        if self.cfg.session_id:
            h["x-session-id"] = self.cfg.session_id
        if signed and sign_headers:
            h.update(sign_headers)
        return h

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = self.cfg.base_url.rstrip("/") + path
        r = requests.get(url, params=params or {}, timeout=15)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return r.text

    def _post(self, path: str, payload: dict, signed: bool = False, sign_headers: Optional[Dict[str, str]] = None) -> Any:
        url = self.cfg.base_url.rstrip("/") + path
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        r = requests.post(
            url,
            data=body.encode("utf-8"),
            headers=self._headers(signed=signed, sign_headers=sign_headers),
            timeout=15,
        )
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return r.text

    # ---------- public ----------
    def server_time(self) -> int:
        # returns unix seconds
        t = self._get("/api/kline/time")
        return int(t)

    def symbol_info(self):
        return self._get("/api/query_symbol_info")

    def symbol_price(self, symbol: str):
        return self._get("/api/query_symbol_price", params={"symbol": symbol})

    def depth_book(self, symbol: str, limit: int = 50):
        return self._get("/api/query_depth_book", params={"symbol": symbol, "limit": int(limit)})

    # ---------- private ----------
    def create_order(self, payload: dict, sign_headers: Dict[str, str]) -> Any:
        # docs: Authentication Required • Body Signature Required
        return self._post("/api/new_order", payload, signed=True, sign_headers=sign_headers)

    def cancel_order(self, payload: dict, sign_headers: Dict[str, str]) -> Any:
        return self._post("/api/cancel_order", payload, signed=True, sign_headers=sign_headers)

    def query_positions(self, symbol: Optional[str] = None) -> Any:
        params = {"symbol": symbol} if symbol else {}
        return self._get("/api/query_positions", params=params)

    def query_order(self, order_id: Optional[int] = None, cl_ord_id: Optional[str] = None) -> Any:
        params = {}
        if order_id is not None:
            params["order_id"] = int(order_id)
        if cl_ord_id is not None:
            params["cl_ord_id"] = str(cl_ord_id)
        return self._get("/api/query_order", params=params)

    def query_open_orders(self, symbol: Optional[str] = None) -> Any:
        params = {"symbol": symbol} if symbol else {}
        return self._get("/api/query_open_orders", params=params)

    def query_balance(self) -> Any:
        return self._get("/api/query_balance")
