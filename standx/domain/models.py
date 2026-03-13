from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from standx.integrations.rounding import SymbolSpec
from standx.services.indicators import Candle


@dataclass
class PositionState:
    side: str
    qty: float
    entry_ref: float
    sl: Optional[float]
    tp: Optional[float]
    bar_close_time_ms: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Optional["PositionState"]:
        if not data:
            return None
        return cls(
            side=str(data["side"]),
            qty=float(data["qty"]),
            entry_ref=float(data.get("entry_ref", 0.0)),
            sl=float(data["sl"]) if data.get("sl") is not None else None,
            tp=float(data["tp"]) if data.get("tp") is not None else None,
            bar_close_time_ms=int(data.get("bar_close_time_ms", 0)),
        )


@dataclass(frozen=True)
class StrategySignal:
    direction: str
    standx_side: str
    entry_ref: float
    stop_loss: float
    take_profit: Optional[float]
    qty: float
    risk_usdt: float
    notional: float
    margin: float
    bar: Candle
    symbol_spec: SymbolSpec


@dataclass(frozen=True)
class StrategyDecision:
    signal: StrategySignal
    flip_up: bool
    flip_down: bool
    previous_position: Optional[PositionState]


@dataclass(frozen=True)
class GuardDecision:
    kind: str
    level: float
    price: float
    close_side: str
