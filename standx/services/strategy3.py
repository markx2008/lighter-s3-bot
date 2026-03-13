from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from standx.config.runtime import RuntimeConfig
from standx.domain.models import GuardDecision, PositionState, StrategyDecision, StrategySignal
from standx.integrations.rounding import floor_qty
from standx.services.indicators import Candle, atr


def supertrend(candles: list[Candle], atr_n: int, mult: float):
    count = len(candles)
    atr_values = atr(candles, atr_n)
    st_line = [None] * count
    direction = [None] * count
    final_upper = [None] * count
    final_lower = [None] * count
    for index in range(count):
        if atr_values[index] is None or atr_values[index] <= 0:
            continue
        hl2 = (candles[index].high + candles[index].low) / 2
        upper = hl2 + mult * atr_values[index]
        lower = hl2 - mult * atr_values[index]
        if index == 0 or final_upper[index - 1] is None:
            final_upper[index] = upper
            final_lower[index] = lower
            direction[index] = 1
            st_line[index] = lower
            continue
        final_upper[index] = upper if (upper < final_upper[index - 1] or candles[index - 1].close > final_upper[index - 1]) else final_upper[index - 1]
        final_lower[index] = lower if (lower > final_lower[index - 1] or candles[index - 1].close < final_lower[index - 1]) else final_lower[index - 1]
        previous = direction[index - 1] or 1
        if candles[index].close > final_upper[index - 1]:
            direction[index] = 1
        elif candles[index].close < final_lower[index - 1]:
            direction[index] = -1
        else:
            direction[index] = previous
        st_line[index] = final_lower[index] if direction[index] == 1 else final_upper[index]
    return st_line, direction


@dataclass(frozen=True)
class QuantityPlan:
    qty: float
    notional: float
    margin: float
    risk_usdt: float
    stop_dist: float


class Strategy3Service:
    def __init__(self, config: RuntimeConfig):
        self.config = config

    def calc_qty(self, entry: float, sl: float) -> Optional[QuantityPlan]:
        stop_dist = abs(entry - sl)
        if stop_dist <= 0:
            return None
        risk_usdt = self.config.account_equity * self.config.risk_pct
        qty = risk_usdt / stop_dist
        notional = qty * entry
        max_notional_by_margin = self.config.account_equity * self.config.max_margin_pct * self.config.leverage
        if notional > max_notional_by_margin and entry > 0:
            notional = max_notional_by_margin
            qty = notional / entry
        if self.config.max_notional_usdt > 0 and notional > self.config.max_notional_usdt and entry > 0:
            notional = self.config.max_notional_usdt
            qty = notional / entry
        margin = notional / self.config.leverage if self.config.leverage > 0 else 0.0
        return QuantityPlan(qty=qty, notional=notional, margin=margin, risk_usdt=risk_usdt, stop_dist=stop_dist)

    def evaluate_at_index(self, candles: list[Candle], symbol_spec, previous_position: PositionState | None, index: int) -> StrategyDecision | None:
        """Evaluate Supertrend flip at a specific candle index.

        Caller should pass an index that refers to a fully-closed candle.
        """
        if len(candles) < 200:
            return None
        if index <= 0 or index >= len(candles):
            return None
        st_line, st_direction = supertrend(candles, self.config.strategy.supertrend_atr_n, self.config.strategy.supertrend_mult)
        if st_direction[index] is None or st_direction[index - 1] is None or st_line[index] is None:
            return None
        flip_up = st_direction[index] == 1 and st_direction[index - 1] == -1
        flip_down = st_direction[index] == -1 and st_direction[index - 1] == 1
        if not (flip_up or flip_down):
            return None
        bar = candles[index]
        entry_ref = bar.close
        stop_loss = float(st_line[index])
        quantity_plan = self.calc_qty(entry_ref, stop_loss)
        if not quantity_plan:
            return None
        qty = floor_qty(quantity_plan.qty, symbol_spec)
        if qty <= 0:
            return None
        take_profit = None
        if self.config.strategy.tp_r_multiple > 0:
            take_profit = entry_ref + self.config.strategy.tp_r_multiple * quantity_plan.stop_dist if flip_up else entry_ref - self.config.strategy.tp_r_multiple * quantity_plan.stop_dist
        return StrategyDecision(
            signal=StrategySignal(
                direction="LONG" if flip_up else "SHORT",
                standx_side="buy" if flip_up else "sell",
                entry_ref=entry_ref,
                stop_loss=stop_loss,
                take_profit=take_profit,
                qty=qty,
                risk_usdt=quantity_plan.risk_usdt,
                notional=quantity_plan.notional,
                margin=quantity_plan.margin,
                bar=bar,
                symbol_spec=symbol_spec,
            ),
            flip_up=flip_up,
            flip_down=flip_down,
            previous_position=previous_position,
        )

    def evaluate(self, candles: list[Candle], symbol_spec, previous_position: PositionState | None) -> StrategyDecision | None:
        # Backwards-compatible default: use the previous candle to avoid forming bars.
        return self.evaluate_at_index(candles, symbol_spec, previous_position, index=len(candles) - 2)

    @staticmethod
    def tighten_stop(previous_stop: float | None, next_stop: float, side: str) -> float:
        if previous_stop is None:
            return next_stop
        return max(previous_stop, next_stop) if side == "long" else min(previous_stop, next_stop)

    @staticmethod
    def evaluate_guard(position: PositionState, current_price: float) -> GuardDecision | None:
        if position.side == "long":
            if position.sl is not None and current_price <= position.sl:
                return GuardDecision(kind="SL", level=position.sl, price=current_price, close_side="sell")
            if position.tp is not None and current_price >= position.tp:
                return GuardDecision(kind="TP", level=position.tp, price=current_price, close_side="sell")
            return None
        if position.sl is not None and current_price >= position.sl:
            return GuardDecision(kind="SL", level=position.sl, price=current_price, close_side="buy")
        if position.tp is not None and current_price <= position.tp:
            return GuardDecision(kind="TP", level=position.tp, price=current_price, close_side="buy")
        return None
