#!/usr/bin/env python3
"""standx_rounding.py

Utilities to round price/qty to StandX symbol constraints.

StandX symbol_info returns:
- price_tick_decimals (int)
- qty_tick_decimals (int)
- min_order_qty (decimal string)

We round DOWN for qty (safer for reduce_only) and round to tick decimals for price.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SymbolSpec:
    price_tick_decimals: int
    qty_tick_decimals: int
    min_order_qty: float


def round_price(px: float, spec: SymbolSpec) -> float:
    d = max(0, int(spec.price_tick_decimals))
    # nearest tick
    return round(px, d)


def floor_qty(qty: float, spec: SymbolSpec) -> float:
    d = max(0, int(spec.qty_tick_decimals))
    step = 10 ** (-d)
    q = math.floor(qty / step) * step
    if q < spec.min_order_qty:
        return 0.0
    # avoid -0.0
    return float(q)
