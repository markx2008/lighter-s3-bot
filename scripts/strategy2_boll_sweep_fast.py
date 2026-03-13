#!/usr/bin/env python3
"""strategy2_boll_sweep_fast.py

Fast sweep for Bollinger Squeeze Breakout without spawning subprocesses.

We load candles once, compute indicators once, then run the strategy with varying
parameters.

Outputs top candidates by OOS PF / return / DD.

Env:
  TOP_N=20
  MIN_OOS_TRADES=30

Note: uses the same execution assumptions as strategy2_backtest.py.
"""

from __future__ import annotations

import math
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from strategy_lab import Candle, atr, sma, utc_ms_to_str

SYMBOL = "币安人生USDT"
INTERVAL = "15m"
START_UTC = datetime(2025, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
BINANCE_FAPI = "https://fapi.binance.com"

INITIAL_EQUITY = float(os.getenv("INITIAL_EQUITY", "10000"))
TAKER_FEE = float(os.getenv("TAKER_FEE", "0.0004"))
SLIPPAGE = float(os.getenv("SLIPPAGE", "0.0001"))

LEVERAGE = int(float(os.getenv("LEVERAGE", "20")))
RISK_PCT = float(os.getenv("RISK_PCT", "0.01"))
MAX_MARGIN_PCT = float(os.getenv("MAX_MARGIN_PCT", "0.20"))
LIQ_BUFFER_PCT = float(os.getenv("LIQ_BUFFER_PCT", "0.005"))
MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.012"))

TOP_N = int(float(os.getenv("TOP_N", "20")))
MIN_OOS_TRADES = int(float(os.getenv("MIN_OOS_TRADES", "30")))


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
        cur = int(data[-1][0]) + 1
        if len(data) < limit:
            break
    dedup = {c.open_time_ms: c for c in out}
    return [dedup[k] for k in sorted(dedup)]


def rolling_std(values: Sequence[float], window: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if window <= 0 or len(values) < window:
        return out
    s = 0.0
    ss = 0.0
    for i, v in enumerate(values):
        s += v
        ss += v * v
        if i >= window:
            old = values[i - window]
            s -= old
            ss -= old * old
        if i >= window - 1:
            mean = s / window
            var = ss / window - mean * mean
            out[i] = math.sqrt(max(0.0, var))
    return out


def has_recent_squeeze(idx: int, bw: List[Optional[float]], length: int, thr: float) -> bool:
    if length <= 0 or idx < length:
        return False
    for j in range(idx - length, idx):
        v = bw[j]
        if v is None or v > thr:
            return False
    return True


@dataclass
class Perf:
    ret_pct: float
    pf: float
    dd_pct: float
    trades: int
    win_rate: float


def backtest_boll(
    candles: Sequence[Candle],
    atr14: List[Optional[float]],
    sma20: List[Optional[float]],
    std20: List[Optional[float]],
    *,
    boll_len: int,
    squeeze_len: int,
    bw_thr: float,
    stop_atr: float,
    tp_atr: float,
    trail_atr: float,
    trail_start: float,
    time_stop: int,
) -> Perf:
    # precompute bands + bandwidth
    n = len(candles)
    upper = [None] * n
    lower = [None] * n
    bw = [None] * n
    for i in range(n):
        mid = sma20[i]
        dev = std20[i]
        if mid is None or dev is None or mid == 0:
            continue
        up = mid + 2.0 * dev
        lo = mid - 2.0 * dev
        upper[i] = up
        lower[i] = lo
        bw[i] = (up - lo) / mid

    equity = INITIAL_EQUITY
    peak = equity
    max_dd = 0.0

    pos = 0
    qty = 0.0
    entry = 0.0
    entry_i = 0
    stop_px = 0.0
    tp_px = 0.0
    best_px = 0.0
    trail_on = False

    wins = 0
    losses = 0
    gross_win = 0.0
    gross_loss = 0.0

    def fee(notional: float) -> float:
        return notional * TAKER_FEE

    def mark(price: float):
        nonlocal peak, max_dd
        cur = equity
        if pos != 0:
            cur = equity + qty * (price - entry) if pos == 1 else equity + qty * (entry - price)
        peak = max(peak, cur)
        dd = (peak - cur) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    start = max(boll_len, 20) + 2

    for i in range(start, n - 1):
        bar = candles[i]
        nxt = candles[i + 1]
        a = atr14[i]
        if a is None or a <= 0:
            continue

        mark(bar.close)

        # manage open position
        if pos != 0:
            best_px = max(best_px, bar.high) if pos == 1 else min(best_px, bar.low)
            move = (best_px - entry) / a if pos == 1 else (entry - best_px) / a
            if move >= trail_start:
                trail_on = True
            if trail_on:
                tstop = best_px - trail_atr * a if pos == 1 else best_px + trail_atr * a
                stop_px = max(stop_px, tstop) if pos == 1 else min(stop_px, tstop)

            exit_px = None
            # conservative: stop first then tp
            if pos == 1:
                if bar.low <= stop_px:
                    exit_px = stop_px * (1 - SLIPPAGE)
                elif bar.high >= tp_px:
                    exit_px = tp_px * (1 - SLIPPAGE)
            else:
                if bar.high >= stop_px:
                    exit_px = stop_px * (1 + SLIPPAGE)
                elif bar.low <= tp_px:
                    exit_px = tp_px * (1 + SLIPPAGE)

            if exit_px is None and time_stop > 0 and (i - entry_i) >= time_stop:
                exit_px = bar.close * (1 - SLIPPAGE) if pos == 1 else bar.close * (1 + SLIPPAGE)

            if exit_px is not None:
                equity -= fee(qty * exit_px)
                pnl = qty * (exit_px - entry) if pos == 1 else qty * (entry - exit_px)
                equity += pnl

                if pnl > 0:
                    wins += 1
                    gross_win += pnl
                else:
                    losses += 1
                    gross_loss += -pnl

                pos = 0
                qty = 0.0
                entry = 0.0
                trail_on = False
                continue

        # entries
        if bw[i] is None or upper[i] is None or lower[i] is None:
            continue
        if not has_recent_squeeze(i, bw, squeeze_len, bw_thr):
            continue

        close = bar.close
        atr_pct = a / close if close > 0 else 0.0
        if atr_pct < MIN_ATR_PCT:
            continue

        side = 0
        if close > upper[i]:
            side = 1
        elif close < lower[i]:
            side = -1
        else:
            continue

        exec_px = nxt.open * (1 + SLIPPAGE) if side == 1 else nxt.open * (1 - SLIPPAGE)
        risk_dist = stop_atr * a

        if side == 1:
            sl = exec_px - risk_dist
            tp = exec_px + tp_atr * risk_dist
            liq = exec_px * (1 - 1 / max(1.0, float(LEVERAGE)))
            if sl <= liq * (1 + LIQ_BUFFER_PCT):
                continue
        else:
            sl = exec_px + risk_dist
            tp = exec_px - tp_atr * risk_dist
            liq = exec_px * (1 + 1 / max(1.0, float(LEVERAGE)))
            if sl >= liq * (1 - LIQ_BUFFER_PCT):
                continue

        stop_dist = abs(exec_px - sl)
        if stop_dist <= 0:
            continue

        risk_usdt = equity * RISK_PCT
        qty = risk_usdt / stop_dist
        max_notional = equity * MAX_MARGIN_PCT * LEVERAGE
        notional = qty * exec_px
        if max_notional > 0 and notional > max_notional:
            notional = max_notional
            qty = notional / exec_px
        if qty <= 0:
            continue

        equity -= fee(qty * exec_px)

        pos = side
        entry = exec_px
        entry_i = i + 1
        stop_px = sl
        tp_px = tp
        best_px = entry
        trail_on = False

    # stats
    ret_pct = (equity / INITIAL_EQUITY - 1) * 100
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    trades = wins + losses
    win_rate = wins / trades * 100 if trades else 0.0
    return Perf(ret_pct=ret_pct, pf=pf, dd_pct=max_dd * 100, trades=trades, win_rate=win_rate)


@dataclass
class Cand:
    params: Dict[str, Any]
    oos: Perf


def main():
    end = datetime.now(timezone.utc)
    candles = fetch_klines(ms(START_UTC), ms(end))
    closes = [c.close for c in candles]

    atr14 = atr(list(candles), 14)
    sma20 = sma(closes, 20)
    std20 = rolling_std(closes, 20)

    split = int(len(candles) * 0.7)
    tr = candles[:split]
    te = candles[split:]

    atr_tr = atr14[:split]
    sma_tr = sma20[:split]
    std_tr = std20[:split]

    atr_te = atr14[split:]
    sma_te = sma20[split:]
    std_te = std20[split:]

    # grids
    SQUEEZE_LEN_GRID = [6, 8, 10, 12]
    BW_THRESH_GRID = [0.040, 0.050, 0.055, 0.060]
    STOP_ATR_GRID = [1.0, 1.2, 1.3, 1.5]
    TP_ATR_GRID = [2.0, 2.2, 2.4, 2.8]
    TRAIL_ATR_GRID = [0.7, 0.9, 1.1]
    TRAIL_START_GRID = [1.5, 1.8, 2.2]
    TIME_STOP_GRID = [96, 144, 192]

    cands: List[Cand] = []

    it = 0
    for sq in SQUEEZE_LEN_GRID:
        for bw in BW_THRESH_GRID:
            for stop in STOP_ATR_GRID:
                for tp in TP_ATR_GRID:
                    if tp <= stop:
                        continue
                    for trail in TRAIL_ATR_GRID:
                        for tstart in TRAIL_START_GRID:
                            for tstop in TIME_STOP_GRID:
                                it += 1
                                oos = backtest_boll(
                                    te,
                                    atr_te,
                                    sma_te,
                                    std_te,
                                    boll_len=20,
                                    squeeze_len=sq,
                                    bw_thr=bw,
                                    stop_atr=stop,
                                    tp_atr=tp,
                                    trail_atr=trail,
                                    trail_start=tstart,
                                    time_stop=tstop,
                                )
                                if oos.trades < MIN_OOS_TRADES:
                                    continue
                                if oos.dd_pct > 25:
                                    continue
                                if oos.pf < 1.1:
                                    continue
                                cands.append(Cand(params={
                                    'S2_BOLL_SQ_LEN': sq,
                                    'S2_BOLL_BW': bw,
                                    'S2_BOLL_STOP': stop,
                                    'S2_BOLL_TP': tp,
                                    'S2_BOLL_TRAIL': trail,
                                    'S2_BOLL_TRAIL_START': tstart,
                                    'S2_BOLL_TIME_STOP': tstop,
                                }, oos=oos))

    cands.sort(key=lambda c: (c.oos.pf, c.oos.ret_pct, -c.oos.dd_pct), reverse=True)

    print(f"iters={it} candidates={len(cands)} MIN_OOS_TRADES={MIN_OOS_TRADES}")
    for c in cands[:TOP_N]:
        p=c.params; o=c.oos
        print(
            f"OOS PF={o.pf:.3f} ret={o.ret_pct:+.2f}% DD={o.dd_pct:.2f}% trades={o.trades:>4d} win={o.win_rate:>5.1f}% | "
            f"SQ_LEN={p['S2_BOLL_SQ_LEN']} BW={p['S2_BOLL_BW']} STOP={p['S2_BOLL_STOP']} TP={p['S2_BOLL_TP']} "
            f"TRAIL={p['S2_BOLL_TRAIL']} TSTART={p['S2_BOLL_TRAIL_START']} TSTOP={p['S2_BOLL_TIME_STOP']}"
        )


if __name__ == '__main__':
    main()
