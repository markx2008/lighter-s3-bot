#!/usr/bin/env python3
"""Backtest: 币安人生USDT 15m reversal strategy

期間: 2026-01-01 00:00:00 UTC -> now
訊號: 連漲/連跌 >= MIN_CONSECUTIVE 後，第一根反轉收盤
交易規則(簡單版):
  - long_to_short 觸發：若無倉/多倉 -> 於該K收盤價平多並開空
  - short_to_long 觸發：若無倉/空倉 -> 於該K收盤價平空並開多
  - 僅持有單一方向倉位
  - 1x，全資金換算名目(USDT)；忽略槓桿、資金費率
  - 手續費: taker_fee 每次成交收一次 (entry + exit)

輸出: 簡易績效指標
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import requests

SYMBOL = "币安人生USDT"
INTERVAL = "15m"
MIN_CONSECUTIVE = 3

START_UTC = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

INITIAL_EQUITY = 10_000.0  # USDT
TAKER_FEE = 0.0004  # 0.04% per fill

BINANCE_FAPI = "https://fapi.binance.com"


@dataclass
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


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fetch_klines(start_ms: int, end_ms: int) -> List[Candle]:
    """Paginate klines. Binance futures klines limit 1500."""
    out: List[Candle] = []
    limit = 1500
    cur = start_ms

    while True:
        params = {
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "startTime": cur,
            "endTime": end_ms,
            "limit": limit,
        }
        r = requests.get(f"{BINANCE_FAPI}/fapi/v1/klines", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not data:
            break

        for k in data:
            out.append(
                Candle(
                    open_time_ms=int(k[0]),
                    open=float(k[1]),
                    high=float(k[2]),
                    low=float(k[3]),
                    close=float(k[4]),
                    volume=float(k[5]),
                    close_time_ms=int(k[6]),
                )
            )

        last_open = int(data[-1][0])
        # advance 1ms past last open time to avoid duplicates
        cur = last_open + 1

        if len(data) < limit:
            break

        # be gentle to API
        time.sleep(0.2)

    # Deduplicate by open_time
    dedup = {}
    for c in out:
        dedup[c.open_time_ms] = c
    return [dedup[k] for k in sorted(dedup.keys())]


def detect_reversal_direction(candles: List[Candle], idx_last_closed: int) -> Tuple[int, Optional[str]]:
    """Return (count, direction) at a given closed candle index.

    direction:
      - 'long_to_short' if current candle is bearish and preceded by N bullish
      - 'short_to_long' if current candle is bullish and preceded by N bearish
    count is the number of preceding same-direction candles (bullish for long_to_short, bearish for short_to_long)
    """
    cur = candles[idx_last_closed]

    if cur.bullish:
        # count preceding bearish
        n = 0
        j = idx_last_closed - 1
        while j >= 0 and (not candles[j].bullish):
            n += 1
            j -= 1
        return n, ("short_to_long" if n > 0 else None)
    else:
        # count preceding bullish
        n = 0
        j = idx_last_closed - 1
        while j >= 0 and candles[j].bullish:
            n += 1
            j -= 1
        return n, ("long_to_short" if n > 0 else None)


@dataclass
class Trade:
    entry_time_ms: int
    exit_time_ms: int
    side: str  # 'long' | 'short'
    entry: float
    exit: float
    pnl_usdt: float
    pnl_pct: float
    reason: str


def backtest(candles: List[Candle]):
    equity = INITIAL_EQUITY
    peak = equity
    max_dd = 0.0

    position = None  # (side, qty, entry_price, entry_time)
    trades: List[Trade] = []

    def mark_to_market(price: float):
        nonlocal equity, peak, max_dd
        # equity already includes realized; unrealized tracked via position value
        cur_equity = equity
        if position:
            side, qty, entry_price, _ = position
            if side == "long":
                cur_equity = equity + qty * (price - entry_price)
            else:
                cur_equity = equity + qty * (entry_price - price)
        peak = max(peak, cur_equity)
        dd = (peak - cur_equity) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Iterate over closed candles; we can act on close
    for i in range(1, len(candles)):
        c = candles[i]
        # update DD with close
        mark_to_market(c.close)

        count, direction = detect_reversal_direction(candles, i)
        if not direction or count < MIN_CONSECUTIVE:
            continue

        # signal at this candle close
        price = c.close
        t_ms = c.close_time_ms

        want_side = "short" if direction == "long_to_short" else "long"

        # if already in that side, ignore
        if position and position[0] == want_side:
            continue

        # Close existing position if any
        if position:
            side, qty, entry_price, entry_t = position
            # fee on exit
            fee = (abs(qty) * price) * TAKER_FEE
            if side == "long":
                pnl = qty * (price - entry_price) - fee
            else:
                pnl = qty * (entry_price - price) - fee
            equity += pnl
            trades.append(
                Trade(
                    entry_time_ms=entry_t,
                    exit_time_ms=t_ms,
                    side=side,
                    entry=entry_price,
                    exit=price,
                    pnl_usdt=pnl,
                    pnl_pct=pnl / (equity - pnl) if (equity - pnl) != 0 else 0,
                    reason="reverse",
                )
            )
            position = None

        # Open new position with full equity notionally
        if equity <= 0:
            break

        qty = equity / price
        # fee on entry
        fee = (qty * price) * TAKER_FEE
        equity -= fee
        position = (want_side, qty, price, t_ms)

    # Close at end
    if position:
        side, qty, entry_price, entry_t = position
        last = candles[-1]
        price = last.close
        t_ms = last.close_time_ms
        fee = (qty * price) * TAKER_FEE
        if side == "long":
            pnl = qty * (price - entry_price) - fee
        else:
            pnl = qty * (entry_price - price) - fee
        equity += pnl
        trades.append(
            Trade(
                entry_time_ms=entry_t,
                exit_time_ms=t_ms,
                side=side,
                entry=entry_price,
                exit=price,
                pnl_usdt=pnl,
                pnl_pct=pnl / (equity - pnl) if (equity - pnl) != 0 else 0,
                reason="eod",
            )
        )

    # stats
    total_return = (equity / INITIAL_EQUITY) - 1
    wins = sum(1 for tr in trades if tr.pnl_usdt > 0)
    losses = sum(1 for tr in trades if tr.pnl_usdt <= 0)
    win_rate = wins / len(trades) if trades else 0
    avg_pnl = sum(tr.pnl_usdt for tr in trades) / len(trades) if trades else 0

    return {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "start": candles[0].open_time_ms if candles else None,
        "end": candles[-1].close_time_ms if candles else None,
        "initial": INITIAL_EQUITY,
        "final": equity,
        "return_pct": total_return * 100,
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate * 100,
        "avg_pnl": avg_pnl,
        "max_drawdown_pct": max_dd * 100,
        "taker_fee": TAKER_FEE,
    }, trades


def fmt_ms(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    end = datetime.now(timezone.utc)
    candles = fetch_klines(ms(START_UTC), ms(end))
    if not candles:
        raise SystemExit("no candles fetched")

    summary, trades = backtest(candles)

    print("=== Summary ===")
    print(f"Symbol: {summary['symbol']}  Interval: {summary['interval']}")
    print(f"Period(UTC): {fmt_ms(summary['start'])} -> {fmt_ms(summary['end'])}")
    print(f"Initial: {summary['initial']:.2f}  Final: {summary['final']:.2f}")
    print(f"Return: {summary['return_pct']:.2f}%")
    print(f"Trades: {summary['trades']}  WinRate: {summary['win_rate_pct']:.1f}%  AvgPnL: {summary['avg_pnl']:.2f}")
    print(f"MaxDD: {summary['max_drawdown_pct']:.2f}%  Fee(taker): {summary['taker_fee']*100:.3f}%")

    # show last 5 trades
    print("\n=== Last 5 trades ===")
    for tr in trades[-5:]:
        print(
            f"{fmt_ms(tr.entry_time_ms)} -> {fmt_ms(tr.exit_time_ms)} {tr.side:5s} "
            f"{tr.entry:.6f}->{tr.exit:.6f} pnl={tr.pnl_usdt:+.2f}"
        )


if __name__ == "__main__":
    main()
