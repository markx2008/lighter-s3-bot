#!/usr/bin/env python3
"""strategy_lab.py

純 Python（無 numpy/pandas）的簡易回測框架，用於研究/驗證策略。

注意：這是研究工具，不構成投資建議；回測不代表未來績效。
"""

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
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(values: List[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if n <= 0:
        return out
    alpha = 2.0 / (n + 1)
    e: Optional[float] = None
    for i, v in enumerate(values):
        if e is None:
            e = v
        else:
            e = (alpha * v) + (1 - alpha) * e
        if i >= n - 1:
            out[i] = e
    return out


def rsi(closes: List[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(closes)
    if n <= 0 or len(closes) < n + 1:
        return out

    gains = [0.0] * len(closes)
    losses = [0.0] * len(closes)
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains[i] = d if d > 0 else 0.0
        losses[i] = (-d) if d < 0 else 0.0

    avg_gain = sum(gains[1 : n + 1]) / n
    avg_loss = sum(losses[1 : n + 1]) / n

    def calc_rs(ag: float, al: float) -> float:
        if al == 0:
            return float('inf')
        return ag / al

    rs = calc_rs(avg_gain, avg_loss)
    out[n] = 100.0 - (100.0 / (1.0 + rs))

    for i in range(n + 1, len(closes)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
        rs = calc_rs(avg_gain, avg_loss)
        out[i] = 100.0 - (100.0 / (1.0 + rs))

    return out


def atr(candles: List[Candle], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(candles)
    if n <= 0 or len(candles) < 2:
        return out

    tr: List[float] = [0.0] * len(candles)
    for i in range(1, len(candles)):
        c = candles[i]
        p = candles[i - 1]
        tr[i] = max(
            c.high - c.low,
            abs(c.high - p.close),
            abs(c.low - p.close),
        )

    # Wilder's smoothing
    if len(candles) <= n:
        return out
    a = sum(tr[1 : n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(candles)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
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
    side: str  # long|short
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
# signal: -1 short, 0 flat, +1 long at bar i (decision at close i, execute next open)


def backtest(
    candles: List[Candle],
    signal_fn: SignalFn,
    indicators: Dict[str, List[Optional[float]]],
    *,
    initial_equity: float = 10_000.0,
    taker_fee: float = 0.0004,
    slippage: float = 0.0001,
) -> Tuple[BacktestResult, List[Trade]]:
    """Very simple 1-position backtest.

    - Decide desired position at close of bar i
    - Execute at open of bar i+1 with slippage
    - Full equity notional
    - Fees on each fill
    """

    equity = initial_equity
    peak = equity
    max_dd = 0.0

    pos = 0  # -1 short, 0 flat, +1 long
    qty = 0.0
    entry_price = 0.0
    entry_time = 0

    trades: List[Trade] = []

    def mark_to_market(price: float):
        nonlocal peak, max_dd
        cur = equity
        if pos != 0:
            if pos == 1:
                cur = equity + qty * (price - entry_price)
            else:
                cur = equity + qty * (entry_price - price)
        if cur > peak:
            peak = cur
        dd = (peak - cur) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    for i in range(len(candles) - 1):
        c = candles[i]
        n = candles[i + 1]

        mark_to_market(c.close)

        desired = signal_fn(i, candles, indicators)
        if desired not in (-1, 0, 1):
            desired = 0

        if desired == pos:
            continue

        # Execute at next open
        open_px = n.open
        if desired == 1:
            exec_px = open_px * (1 + slippage)
        elif desired == -1:
            exec_px = open_px * (1 - slippage)
        else:
            # closing
            exec_px = open_px * (1 - slippage) if pos == 1 else open_px * (1 + slippage)

        t_ms = n.open_time_ms

        # Close existing
        if pos != 0:
            notional = qty * exec_px
            fee = notional * taker_fee
            if pos == 1:
                pnl = qty * (exec_px - entry_price) - fee
            else:
                pnl = qty * (entry_price - exec_px) - fee
            equity += pnl
            trades.append(
                Trade(
                    entry_time_ms=entry_time,
                    exit_time_ms=t_ms,
                    side="long" if pos == 1 else "short",
                    entry=entry_price,
                    exit=exec_px,
                    pnl_usdt=pnl,
                    reason="switch",
                )
            )
            pos = 0
            qty = 0.0

        # Open new
        if desired != 0 and equity > 0:
            notional = equity
            qty = notional / exec_px
            fee = (qty * exec_px) * taker_fee
            equity -= fee
            pos = desired
            entry_price = exec_px
            entry_time = t_ms

    # Close at end (last close)
    last = candles[-1]
    if pos != 0:
        exec_px = last.close * (1 - slippage) if pos == 1 else last.close * (1 + slippage)
        notional = qty * exec_px
        fee = notional * taker_fee
        if pos == 1:
            pnl = qty * (exec_px - entry_price) - fee
        else:
            pnl = qty * (entry_price - exec_px) - fee
        equity += pnl
        trades.append(
            Trade(
                entry_time_ms=entry_time,
                exit_time_ms=last.close_time_ms,
                side="long" if pos == 1 else "short",
                entry=entry_price,
                exit=exec_px,
                pnl_usdt=pnl,
                reason="eod",
            )
        )

    # stats
    ret = (equity / initial_equity - 1) * 100
    wins = [t for t in trades if t.pnl_usdt > 0]
    losses = [t for t in trades if t.pnl_usdt <= 0]
    win_rate = (len(wins) / len(trades) * 100) if trades else 0.0
    avg_pnl = (sum(t.pnl_usdt for t in trades) / len(trades)) if trades else 0.0

    gross_win = sum(t.pnl_usdt for t in wins)
    gross_loss = -sum(t.pnl_usdt for t in losses)  # positive
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float('inf')

    res = BacktestResult(
        initial_equity=initial_equity,
        final_equity=equity,
        return_pct=ret,
        trades=len(trades),
        win_rate_pct=win_rate,
        avg_pnl=avg_pnl,
        max_drawdown_pct=max_dd * 100,
        profit_factor=profit_factor,
    )
    return res, trades
