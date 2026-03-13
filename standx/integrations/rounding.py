from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolSpec:
    price_tick_decimals: int
    qty_tick_decimals: int
    min_order_qty: float


def round_price(px: float, spec: SymbolSpec) -> float:
    return round(px, max(0, int(spec.price_tick_decimals)))


def floor_qty(qty: float, spec: SymbolSpec) -> float:
    decimals = max(0, int(spec.qty_tick_decimals))
    step = 10 ** (-decimals)
    rounded = math.floor(qty / step) * step
    if rounded < spec.min_order_qty:
        return 0.0
    return float(rounded)
