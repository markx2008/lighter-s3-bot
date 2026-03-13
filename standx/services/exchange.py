from __future__ import annotations

import json
import time
from typing import Optional

from standx.config.runtime import RuntimeConfig
from standx.integrations.client import StandXClient, StandXConfig
from standx.integrations.rounding import SymbolSpec


def _format_decimal(value: float, decimals: int) -> str:
    # Use fixed decimals as required by StandX docs (decimal parameters expected as JSON strings).
    return f"{float(value):.{max(0, int(decimals))}f}"


def _floor_to_decimals(value: float, decimals: int) -> float:
    step = 10 ** (-max(0, int(decimals)))
    return (int(value / step)) * step


def _ceil_to_decimals(value: float, decimals: int) -> float:
    step = 10 ** (-max(0, int(decimals)))
    return (int((value + step - 1e-18) / step)) * step
from standx.integrations.signing import parse_ed25519_private_key, sign_request
from standx.services.indicators import Candle


class ExchangeGateway:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.client = StandXClient(StandXConfig(base_url=config.standx_base_url, jwt=config.standx_jwt or None))
        self._symbol_spec: Optional[SymbolSpec] = None

    def symbol_spec(self) -> SymbolSpec:
        if self._symbol_spec is not None:
            return self._symbol_spec
        info = self.client.symbol_info()
        if isinstance(info, list):
            for item in info:
                if item.get("symbol") == self.config.strategy.symbol:
                    self._symbol_spec = SymbolSpec(price_tick_decimals=int(item.get("price_tick_decimals", 2)), qty_tick_decimals=int(item.get("qty_tick_decimals", 4)), min_order_qty=float(item.get("min_order_qty", 0.0)))
                    break
        if self._symbol_spec is None:
            self._symbol_spec = SymbolSpec(price_tick_decimals=2, qty_tick_decimals=4, min_order_qty=0.0)
        return self._symbol_spec

    def fetch_candles(self) -> list[Candle]:
        now = int(time.time())
        start = now - self.config.strategy.lookback_days * 86400
        # StandX kline endpoint defaults to a very small window unless countback is provided.
        # Strategy3 needs a long history (>=200 candles) for indicators.
        data = self.client._get(
            "/api/kline/history",
            params={
                "symbol": self.config.strategy.symbol,
                "resolution": self.config.strategy.resolution,
                "from": start,
                "to": now,
                "countback": 500,
            },
        )
        if not isinstance(data, dict) or data.get("s") != "ok":
            return []
        candles: list[Candle] = []
        bar_ms = int(int(self.config.strategy.resolution) * 60 * 1000)
        for index in range(len(data["t"])):
            open_ms = int(data["t"][index]) * 1000
            candles.append(Candle(open_time_ms=open_ms, open=float(data["o"][index]), high=float(data["h"][index]), low=float(data["l"][index]), close=float(data["c"][index]), volume=float(data["v"][index]), close_time_ms=open_ms + bar_ms - 1))
        return candles

    def current_price(self) -> float:
        price = self.client.symbol_price(self.config.strategy.symbol)
        return float(price.get("mark_price") or price.get("last_price") or price.get("mid_price"))

    def create_order(self, payload: dict) -> object:
        if not self.config.live:
            raise RuntimeError("create_order requires LIVE=1")
        if not self.config.standx_jwt or not self.config.standx_ed25519_privkey:
            raise RuntimeError("LIVE=1 requires STANDX_JWT and STANDX_ED25519_PRIVKEY")

        spec = self.symbol_spec()

        # Normalize decimal fields to strings with correct tick decimals.
        normalized = dict(payload)

        # qty: always string at qty_tick_decimals
        if "qty" in normalized and normalized["qty"] is not None:
            qty_f = float(normalized["qty"])
            normalized["qty"] = _format_decimal(_floor_to_decimals(qty_f, spec.qty_tick_decimals), spec.qty_tick_decimals)

        # price-like fields: align to price tick and stringify
        for key in ("price", "tp_price", "sl_price"):
            if key in normalized and normalized[key] is not None:
                px = float(normalized[key])
                # For limit entry price: buy floors, sell ceils. For TP/SL triggers we just round to tick.
                if key == "price":
                    if str(normalized.get("side", "")).lower() == "buy":
                        px = _floor_to_decimals(px, spec.price_tick_decimals)
                    elif str(normalized.get("side", "")).lower() == "sell":
                        px = _ceil_to_decimals(px, spec.price_tick_decimals)
                    else:
                        px = round(px, spec.price_tick_decimals)
                else:
                    px = round(px, spec.price_tick_decimals)
                normalized[key] = _format_decimal(px, spec.price_tick_decimals)

        payload_json = json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)
        return self.client.create_order(normalized, sign_request(payload_json, parse_ed25519_private_key(self.config.standx_ed25519_privkey)))

    def query_order(self, cl_ord_id: str) -> object:
        return self.client.query_order(cl_ord_id=cl_ord_id)

    def query_open_orders(self) -> object:
        return self.client.query_open_orders(symbol=self.config.strategy.symbol)
