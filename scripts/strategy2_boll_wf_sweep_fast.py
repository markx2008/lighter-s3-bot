#!/usr/bin/env python3
"""strategy2_boll_wf_sweep_fast.py

Fast walk-forward sweep for Strategy2 Bollinger Squeeze Breakout.

We evaluate parameter combos across multiple 15-day test segments (walk-forward)
without subprocesses.

Focus: tuning the new filters
- S2_BOLL_VOL_FACTOR (volume expansion)
- S2_BOLL_BREAK_ATR (breakout magnitude)

Fixed (current best):
- SQ_LEN=12, BW=0.05, STOP=1.0, TP=2.2, TRAIL=0.7, TSTART=2.2, TSTOP=96
- 20x isolated fixed-risk sizing

Env:
  TRAIN_DAYS=45
  TEST_DAYS=15
  STEP_DAYS=15
  TOP_N=15
  MIN_TOTAL_TRADES=30

  VOL_GRID=1.0,1.1,1.2,1.3,1.4
  BREAK_GRID=0.10,0.15,0.20,0.25,0.30

"""

from __future__ import annotations

import math
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

import requests

from strategy_lab import Candle, atr, sma

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

TRAIN_DAYS = int(float(os.getenv("TRAIN_DAYS", "45")))
TEST_DAYS = int(float(os.getenv("TEST_DAYS", "15")))
STEP_DAYS = int(float(os.getenv("STEP_DAYS", str(TEST_DAYS))))

TOP_N = int(float(os.getenv("TOP_N", "15")))
MIN_TOTAL_TRADES = int(float(os.getenv("MIN_TOTAL_TRADES", "30")))

VOL_GRID = [float(x) for x in os.getenv("VOL_GRID", "1.0,1.1,1.2,1.3,1.4").split(",")]
BREAK_GRID = [float(x) for x in os.getenv("BREAK_GRID", "0.10,0.15,0.20,0.25,0.30").split(",")]

# fixed core params
SQ_LEN = int(float(os.getenv("S2_BOLL_SQ_LEN", "12")))
BW_THR = float(os.getenv("S2_BOLL_BW", "0.05"))
STOP_ATR = float(os.getenv("S2_BOLL_STOP", "1.0"))
TP_ATR = float(os.getenv("S2_BOLL_TP", "2.2"))
TRAIL_ATR = float(os.getenv("S2_BOLL_TRAIL", "0.7"))
TRAIL_START = float(os.getenv("S2_BOLL_TRAIL_START", "2.2"))
TIME_STOP = int(float(os.getenv("S2_BOLL_TIME_STOP", "96")))


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fetch_klines(start_ms: int, end_ms: int) -> List[Candle]:
    out: List[Candle] = []
    limit = 1500
    cur = start_ms
    while True:
        r = requests.get(
            f"{BINANCE_FAPI}/fapi/v1/klines",
            params={"symbol": SYMBOL, "interval": INTERVAL, "startTime": cur, "endTime": end_ms, "limit": limit},
            timeout=30,
        )
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


def backtest_segment(candles: Sequence[Candle], vol_factor: float, break_atr: float) -> Perf:
    if len(candles) < 250:
        return Perf(0.0, float("inf"), 0.0, 0)

    closes = [c.close for c in candles]
    atr14 = atr(list(candles), 14)
    sma20 = sma(closes, 20)
    std20 = rolling_std(closes, 20)

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

    gross_win = 0.0
    gross_loss = 0.0
    trades = 0

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

    start = 60
    for i in range(start, n - 1):
        bar = candles[i]
        nxt = candles[i + 1]
        a = atr14[i]
        if a is None or a <= 0:
            continue

        mark(bar.close)

        # manage open
        if pos != 0:
            best_px = max(best_px, bar.high) if pos == 1 else min(best_px, bar.low)
            move = (best_px - entry) / a if pos == 1 else (entry - best_px) / a
            if move >= TRAIL_START:
                trail_on = True
            if trail_on:
                tstop = best_px - TRAIL_ATR * a if pos == 1 else best_px + TRAIL_ATR * a
                stop_px = max(stop_px, tstop) if pos == 1 else min(stop_px, tstop)

            exit_px = None
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

            if exit_px is None and TIME_STOP > 0 and (i - entry_i) >= TIME_STOP:
                exit_px = bar.close * (1 - SLIPPAGE) if pos == 1 else bar.close * (1 + SLIPPAGE)

            if exit_px is not None:
                equity -= fee(qty * exit_px)
                pnl = qty * (exit_px - entry) if pos == 1 else qty * (entry - exit_px)
                equity += pnl
                trades += 1
                if pnl > 0:
                    gross_win += pnl
                else:
                    gross_loss += -pnl
                pos = 0
                qty = 0.0
                entry = 0.0
                trail_on = False
                continue

        # entry conditions
        if bw[i] is None or upper[i] is None or lower[i] is None:
            continue
        if not has_recent_squeeze(i, bw, SQ_LEN, BW_THR):
            continue

        close = bar.close
        if (a / close) < MIN_ATR_PCT:
            continue

        # volume filter (vol_sma20)
        if i >= 20 and vol_factor > 0:
            vol_sma = sum(c.volume for c in candles[i - 20 : i]) / 20.0
            if vol_sma > 0 and bar.volume < vol_sma * vol_factor:
                continue

        side = 0
        if close > upper[i]:
            if break_atr > 0 and close < (upper[i] + break_atr * a):
                continue
            side = 1
        elif close < lower[i]:
            if break_atr > 0 and close > (lower[i] - break_atr * a):
                continue
            side = -1
        else:
            continue

        exec_px = nxt.open * (1 + SLIPPAGE) if side == 1 else nxt.open * (1 - SLIPPAGE)
        risk_dist = STOP_ATR * a

        if side == 1:
            sl = exec_px - risk_dist
            tp = exec_px + TP_ATR * risk_dist
            liq = exec_px * (1 - 1 / max(1.0, float(LEVERAGE)))
            if sl <= liq * (1 + LIQ_BUFFER_PCT):
                continue
        else:
            sl = exec_px + risk_dist
            tp = exec_px - TP_ATR * risk_dist
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

    ret = (equity / INITIAL_EQUITY - 1) * 100
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    return Perf(ret, pf, max_dd * 100, trades)


@dataclass
class Cand:
    vol_factor: float
    break_atr: float
    seg_rets: List[float]
    seg_pfs: List[float]
    seg_dds: List[float]
    seg_trades: List[int]

    @property
    def total_trades(self) -> int:
        return sum(self.seg_trades)

    @property
    def worst_ret(self) -> float:
        return min(self.seg_rets) if self.seg_rets else 0.0

    @property
    def max_dd(self) -> float:
        return max(self.seg_dds) if self.seg_dds else 0.0

    @property
    def median_pf(self) -> float:
        finite = [p for p in self.seg_pfs if math.isfinite(p)]
        return statistics.median(finite) if finite else 0.0

    @property
    def median_ret(self) -> float:
        return statistics.median(self.seg_rets) if self.seg_rets else 0.0


def dt_str(ms_: int) -> str:
    return datetime.fromtimestamp(ms_ / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def main():
    end = datetime.now(timezone.utc)
    candles = fetch_klines(ms(START_UTC), ms(end))

    train_ms = TRAIN_DAYS * 24 * 60 * 60 * 1000
    test_ms = TEST_DAYS * 24 * 60 * 60 * 1000
    step_ms = STEP_DAYS * 24 * 60 * 60 * 1000

    t0 = candles[0].open_time_ms
    t_end = candles[-1].close_time_ms

    segs = []
    cur = t0 + train_ms
    while cur + test_ms <= t_end:
        segs.append((cur, cur + test_ms))
        cur += step_ms

    print(f"segments={len(segs)} train={TRAIN_DAYS}d test={TEST_DAYS}d step={STEP_DAYS}d")
    print(f"Fixed: SQ_LEN={SQ_LEN} BW={BW_THR} STOP={STOP_ATR} TP={TP_ATR} TRAIL={TRAIL_ATR} TSTART={TRAIL_START} TSTOP={TIME_STOP}")
    print(f"Grid: VOL={VOL_GRID} BREAK_ATR={BREAK_GRID}\n")

    cands: List[Cand] = []

    for vol_factor in VOL_GRID:
        for break_atr in BREAK_GRID:
            seg_rets=[]; seg_pfs=[]; seg_dds=[]; seg_trades=[]
            for (s,e) in segs:
                seg = [c for c in candles if c.open_time_ms >= s and c.close_time_ms <= e]
                if len(seg) < 250:
                    continue
                p = backtest_segment(seg, vol_factor=vol_factor, break_atr=break_atr)
                seg_rets.append(p.ret_pct)
                seg_pfs.append(p.pf)
                seg_dds.append(p.dd_pct)
                seg_trades.append(p.trades)

            cand = Cand(vol_factor, break_atr, seg_rets, seg_pfs, seg_dds, seg_trades)
            if cand.total_trades < MIN_TOTAL_TRADES:
                continue
            cands.append(cand)

    # rank by robustness: median_pf, median_ret, worst_ret, lower max_dd
    cands.sort(key=lambda c: (c.median_pf, c.median_ret, c.worst_ret, -c.max_dd), reverse=True)

    print(f"candidates={len(cands)} (MIN_TOTAL_TRADES={MIN_TOTAL_TRADES})\n")
    for c in cands[:TOP_N]:
        print(
            f"VOL={c.vol_factor:.2f} BREAK={c.break_atr:.2f} | "
            f"medPF={c.median_pf:.2f} medRet={c.median_ret:+.2f}% worstRet={c.worst_ret:+.2f}% "
            f"maxDD={c.max_dd:.2f}% trades={c.total_trades}"
        )


if __name__ == '__main__':
    main()
