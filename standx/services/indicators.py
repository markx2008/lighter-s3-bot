from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time_ms: int

    @property
    def bullish(self) -> bool:
        return self.close > self.open


def utc_ms_to_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def sma(values: List[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if n <= 0:
        return out
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema(values: List[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if n <= 0:
        return out
    alpha = 2.0 / (n + 1)
    current: Optional[float] = None
    for i, value in enumerate(values):
        current = value if current is None else (alpha * value) + (1 - alpha) * current
        if i >= n - 1:
            out[i] = current
    return out


def rsi(closes: List[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(closes)
    if n <= 0 or len(closes) < n + 1:
        return out
    gains = [0.0] * len(closes)
    losses = [0.0] * len(closes)
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains[i] = diff if diff > 0 else 0.0
        losses[i] = -diff if diff < 0 else 0.0
    avg_gain = sum(gains[1 : n + 1]) / n
    avg_loss = sum(losses[1 : n + 1]) / n
    rs = float("inf") if avg_loss == 0 else avg_gain / avg_loss
    out[n] = 100.0 - (100.0 / (1.0 + rs))
    for i in range(n + 1, len(closes)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
        rs = float("inf") if avg_loss == 0 else avg_gain / avg_loss
        out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def atr(candles: List[Candle], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(candles)
    if n <= 0 or len(candles) < 2:
        return out
    tr: List[float] = [0.0] * len(candles)
    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]
        tr[i] = max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close))
    if len(candles) <= n:
        return out
    value = sum(tr[1 : n + 1]) / n
    out[n] = value
    for i in range(n + 1, len(candles)):
        value = (value * (n - 1) + tr[i]) / n
        out[i] = value
    return out


@dataclass
class Fill:
    time_ms: int
    price: float
    qty: float
    fee: float


@dataclass
class Trade:
    entry_time_ms: int
    exit_time_ms: int
    side: str
    entry: float
    exit: float
    pnl_usdt: float
    reason: str


@dataclass
class BacktestResult:
    initial_equity: float
    final_equity: float
    return_pct: float
    trades: int
    win_rate_pct: float
    avg_pnl: float
    max_drawdown_pct: float
    profit_factor: float


SignalFn = Callable[[int, List[Candle], Dict[str, List[Optional[float]]]], int]


def backtest(candles: List[Candle], signal_fn: SignalFn, indicators: Dict[str, List[Optional[float]]], *, initial_equity: float = 10000.0, taker_fee: float = 0.0004, slippage: float = 0.0001) -> Tuple[BacktestResult, List[Trade]]:
    equity = initial_equity
    peak = equity
    max_dd = 0.0
    pos = 0
    qty = 0.0
    entry_price = 0.0
    entry_time = 0
    trades: List[Trade] = []

    def mark_to_market(price: float) -> None:
        nonlocal peak, max_dd
        current = equity
        if pos != 0:
            current = equity + qty * (price - entry_price) if pos == 1 else equity + qty * (entry_price - price)
        if current > peak:
            peak = current
        dd = (peak - current) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    for i in range(len(candles) - 1):
        candle = candles[i]
        next_candle = candles[i + 1]
        mark_to_market(candle.close)
        desired = signal_fn(i, candles, indicators)
        if desired not in (-1, 0, 1):
            desired = 0
        if desired == pos:
            continue
        open_px = next_candle.open
        if desired == 1:
            exec_px = open_px * (1 + slippage)
        elif desired == -1:
            exec_px = open_px * (1 - slippage)
        else:
            exec_px = open_px * (1 - slippage if pos == 1 else 1 + slippage)
        if pos != 0 and qty > 0:
            gross = qty * (exec_px - entry_price) if pos == 1 else qty * (entry_price - exec_px)
            fees = qty * exec_px * taker_fee
            net = gross - fees
            equity += net
            trades.append(Trade(entry_time_ms=entry_time, exit_time_ms=next_candle.open_time_ms, side="long" if pos == 1 else "short", entry=entry_price, exit=exec_px, pnl_usdt=net, reason="signal_flip"))
        pos = desired
        if desired == 0:
            qty = 0.0
            entry_price = 0.0
            entry_time = 0
            continue
        qty = equity / exec_px if exec_px > 0 else 0.0
        entry_price = exec_px
        entry_time = next_candle.open_time_ms
        equity -= qty * exec_px * taker_fee
    wins = [trade.pnl_usdt for trade in trades if trade.pnl_usdt > 0]
    losses = [-trade.pnl_usdt for trade in trades if trade.pnl_usdt < 0]
    result = BacktestResult(initial_equity=initial_equity, final_equity=equity, return_pct=((equity - initial_equity) / initial_equity * 100.0) if initial_equity else 0.0, trades=len(trades), win_rate_pct=(len(wins) / len(trades) * 100.0) if trades else 0.0, avg_pnl=(sum(t.pnl_usdt for t in trades) / len(trades)) if trades else 0.0, max_drawdown_pct=max_dd * 100.0, profit_factor=(sum(wins) / sum(losses)) if losses else float("inf"))
    return result, trades
