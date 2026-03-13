#!/usr/bin/env python3
"""strategy_research.py

研究是否有「在歷史上至少看起來可賺」的簡單策略（示範用）。

- 不會動到原本通知/排程。
- 目標：用回測 + out-of-sample split 初步篩選策略。

注意：回測≠保證、任何策略都可能失效。
"""

from __future__ import annotations

import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

from strategy_lab import Candle, atr, backtest, ema, rsi, sma, utc_ms_to_str

SYMBOL = "币安人生USDT"
INTERVAL = "15m"
START_UTC = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

INITIAL = 10_000.0
TAKER_FEE = 0.0004
SLIPPAGE = 0.0001
MIN_BARS = 500

BINANCE_FAPI = "https://fapi.binance.com"


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fetch_klines(start_ms: int, end_ms: int) -> List[Candle]:
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
        cur = last_open + 1
        if len(data) < limit:
            break
        time.sleep(0.2)

    # dedup by open_time
    m = {c.open_time_ms: c for c in out}
    return [m[k] for k in sorted(m.keys())]


def build_indicators(candles: List[Candle]) -> Dict[str, List[Optional[float]]]:
    closes = [c.close for c in candles]
    return {
        "ema20": ema(closes, 20),
        "ema50": ema(closes, 50),
        "ema100": ema(closes, 100),
        "sma200": sma(closes, 200),
        "rsi14": rsi(closes, 14),
        "atr14": atr(candles, 14),
    }


# ---------- Strategy candidates ----------

def strat_ema_cross(i: int, candles: List[Candle], ind: Dict[str, List[Optional[float]]]) -> int:
    fast = ind["ema20"][i]
    slow = ind["ema50"][i]
    if fast is None or slow is None:
        return 0
    return 1 if fast > slow else -1


def strat_ema_cross_trend_filter(i: int, candles: List[Candle], ind: Dict[str, List[Optional[float]]]) -> int:
    fast = ind["ema20"][i]
    slow = ind["ema50"][i]
    trend = ind["ema100"][i]
    if fast is None or slow is None or trend is None:
        return 0
    # long only if price above ema100; short only if below
    price = candles[i].close
    if fast > slow and price > trend:
        return 1
    if fast < slow and price < trend:
        return -1
    return 0


def strat_rsi_meanrev_with_sma(i: int, candles: List[Candle], ind: Dict[str, List[Optional[float]]]) -> int:
    r = ind["rsi14"][i]
    base = ind["sma200"][i]
    if r is None or base is None:
        return 0
    price = candles[i].close
    # only long in uptrend, short in downtrend
    if price >= base:
        if r < 30:
            return 1
        if r > 55:
            return 0
    else:
        if r > 70:
            return -1
        if r < 45:
            return 0
    return 0


def split_walkforward(candles: List[Candle], split_ratio: float = 0.7) -> Tuple[List[Candle], List[Candle]]:
    k = int(len(candles) * split_ratio)
    return candles[:k], candles[k:]


def run_one(name: str, candles: List[Candle], ind: Dict[str, List[Optional[float]]], fn) -> dict:
    res, _ = backtest(
        candles,
        fn,
        ind,
        initial_equity=INITIAL,
        taker_fee=TAKER_FEE,
        slippage=SLIPPAGE,
    )
    d = asdict(res)
    d["name"] = name
    return d


def main():
    end = datetime.now(timezone.utc)
    candles = fetch_klines(ms(START_UTC), ms(end))
    if len(candles) < MIN_BARS:
        raise SystemExit(f"not enough bars: {len(candles)}")

    ind = build_indicators(candles)

    train, test = split_walkforward(candles, 0.7)
    ind_train = build_indicators(train)
    ind_test = build_indicators(test)

    strategies = [
        ("ema20/50_cross", strat_ema_cross),
        ("ema20/50_cross+ema100_filter", strat_ema_cross_trend_filter),
        ("rsi14_meanrev+sma200_filter", strat_rsi_meanrev_with_sma),
    ]

    print(f"Symbol={SYMBOL} Interval={INTERVAL}")
    print(f"Period(UTC): {utc_ms_to_str(candles[0].open_time_ms)} -> {utc_ms_to_str(candles[-1].close_time_ms)}")
    print(f"Bars: {len(candles)}  Train: {len(train)}  Test: {len(test)}")
    print(f"Assumptions: initial={INITIAL} taker_fee={TAKER_FEE*100:.3f}% slippage={SLIPPAGE*100:.3f}%")

    print("\n=== Full period ===")
    full_rows = [run_one(n, candles, ind, fn) for n, fn in strategies]
    for r in sorted(full_rows, key=lambda x: x["return_pct"], reverse=True):
        print(
            f"{r['name']:<30} ret={r['return_pct']:>7.2f}%  maxDD={r['max_drawdown_pct']:>6.2f}%  trades={r['trades']:>4d}  PF={r['profit_factor']:.2f}"
        )

    print("\n=== Train ===")
    train_rows = [run_one(n, train, ind_train, fn) for n, fn in strategies]
    for r in sorted(train_rows, key=lambda x: x["return_pct"], reverse=True):
        print(
            f"{r['name']:<30} ret={r['return_pct']:>7.2f}%  maxDD={r['max_drawdown_pct']:>6.2f}%  trades={r['trades']:>4d}  PF={r['profit_factor']:.2f}"
        )

    print("\n=== Test (out-of-sample) ===")
    test_rows = [run_one(n, test, ind_test, fn) for n, fn in strategies]
    for r in sorted(test_rows, key=lambda x: x["return_pct"], reverse=True):
        print(
            f"{r['name']:<30} ret={r['return_pct']:>7.2f}%  maxDD={r['max_drawdown_pct']:>6.2f}%  trades={r['trades']:>4d}  PF={r['profit_factor']:.2f}"
        )


if __name__ == "__main__":
    main()
