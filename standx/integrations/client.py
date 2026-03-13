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

    def _headers(self, signed: bool = False, sign_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.cfg.jwt:
            headers["Authorization"] = f"Bearer {self.cfg.jwt}"
        if self.cfg.session_id:
            headers["x-session-id"] = self.cfg.session_id
        if signed and sign_headers:
            headers.update(sign_headers)
        return headers

    def _get(self, path: str, params: Optional[dict] = None, signed: bool = False, sign_headers: Optional[Dict[str, str]] = None) -> Any:
        url = self.cfg.base_url.rstrip("/") + path
        response = requests.get(url, params=params or {}, headers=self._headers(signed=signed, sign_headers=sign_headers), timeout=15)
        response.raise_for_status()
        try:
            return response.json()
        except Exception:
            return response.text

    def _post(self, path: str, payload: dict, signed: bool = False, sign_headers: Optional[Dict[str, str]] = None) -> Any:
        url = self.cfg.base_url.rstrip("/") + path
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        response = requests.post(url, data=body.encode("utf-8"), headers=self._headers(signed=signed, sign_headers=sign_headers), timeout=15)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            # Include response body for debugging (safe: does not include secrets).
            raise requests.HTTPError(f"{exc} | body={response.text[:2000]}", response=response) from None
        try:
            return response.json()
        except Exception:
            return response.text

    def server_time(self) -> int:
        return int(self._get("/api/kline/time"))

    def symbol_info(self):
        return self._get("/api/query_symbol_info")

    def symbol_price(self, symbol: str):
        return self._get("/api/query_symbol_price", params={"symbol": symbol})

    def create_order(self, payload: dict, sign_headers: Dict[str, str]) -> Any:
        return self._post("/api/new_order", payload, signed=True, sign_headers=sign_headers)

    def query_order(self, order_id: Optional[int] = None, cl_ord_id: Optional[str] = None) -> Any:
        params = {}
        if order_id is not None:
            params["order_id"] = int(order_id)
        if cl_ord_id is not None:
            params["cl_ord_id"] = str(cl_ord_id)
        # Some deployments return 404 if the order hasn't propagated yet.
        return self._get("/api/query_order", params=params)

    def query_open_orders(self, symbol: Optional[str] = None) -> Any:
        params = {}
        if symbol:
            params["symbol"] = symbol
        return self._get("/api/query_open_orders", params=params)

    def query_orders(self, sign_headers: Optional[Dict[str, str]] = None) -> Any:
        """Fetch user orders (if supported by backend)."""
        return self._get("/api/query_orders", params={}, signed=bool(sign_headers), sign_headers=sign_headers)

    def query_positions(self, sign_headers: Optional[Dict[str, str]] = None) -> Any:
        """Fetch user positions (if supported by backend).

        Per StandX docs: Query User Positions -> /api/query_positions
        """
        return self._get("/api/query_positions", params={}, signed=bool(sign_headers), sign_headers=sign_headers)
