#!/usr/bin/env python3
"""strategy3_btc_walkforward.py

Walk-forward validation for BTCUSDT strategy3 (trend): Supertrend Trend.

- Fetch BTCUSDT 15m from START_UTC to now
- Build supertrend and run across rolling test segments
- Report per-segment return/PF/DD/trades and summary stats

Env:
  START_UTC=2023-01-01
  TRAIN_DAYS=90
  TEST_DAYS=30
  STEP_DAYS=30

  # Supertrend params
  S3_ST_ATR_N=10
  S3_ST_MULT=3.0
  S3_ST_TP_R=2.0
  S3_ST_TIME_STOP=0

  # risk model
  INITIAL_EQUITY=10000
  LEVERAGE=20
  RISK_PCT=0.01
  MAX_MARGIN_PCT=0.20
  LIQ_BUFFER_PCT=0.005

"""

from __future__ import annotations

import math
import os
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

import requests

import sys
sys.path.append(os.path.dirname(__file__))
from strategy_lab import Candle, atr

SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
INTERVAL = os.getenv("INTERVAL", "15m")
BINANCE_FAPI = "https://fapi.binance.com"
START_UTC_STR = os.getenv("START_UTC", "2023-01-01")

INITIAL_EQUITY = float(os.getenv("INITIAL_EQUITY", "10000"))
TAKER_FEE = float(os.getenv("TAKER_FEE", "0.0004"))
SLIPPAGE = float(os.getenv("SLIPPAGE", "0.0001"))

LEVERAGE = int(float(os.getenv("LEVERAGE", "20")))
RISK_PCT = float(os.getenv("RISK_PCT", "0.01"))
MAX_MARGIN_PCT = float(os.getenv("MAX_MARGIN_PCT", "0.20"))
LIQ_BUFFER_PCT = float(os.getenv("LIQ_BUFFER_PCT", "0.005"))

TRAIN_DAYS = int(float(os.getenv("TRAIN_DAYS", "90")))
TEST_DAYS = int(float(os.getenv("TEST_DAYS", "30")))
STEP_DAYS = int(float(os.getenv("STEP_DAYS", str(TEST_DAYS))))

ST_ATR_N = int(float(os.getenv("S3_ST_ATR_N", "10")))
ST_MULT = float(os.getenv("S3_ST_MULT", "3.0"))
ST_TP_R = float(os.getenv("S3_ST_TP_R", "2.0"))
ST_TIME_STOP = int(float(os.getenv("S3_ST_TIME_STOP", "0")))


def parse_start(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
        time.sleep(0.1)
    dedup = {c.open_time_ms: c for c in out}
    return [dedup[k] for k in sorted(dedup)]


def supertrend(candles: Sequence[Candle], atr_n: int, mult: float) -> Tuple[List[Optional[float]], List[Optional[int]]]:
    n = len(candles)
    a = atr(list(candles), atr_n)
    st = [None] * n
    dir_ = [None] * n
    fub = [None] * n
    flb = [None] * n

    for i in range(n):
        if a[i] is None or a[i] <= 0:
            continue
        hl2 = (candles[i].high + candles[i].low) / 2
        ub = hl2 + mult * a[i]
        lb = hl2 - mult * a[i]

        if i == 0 or fub[i - 1] is None:
            fub[i] = ub
            flb[i] = lb
            dir_[i] = 1
            st[i] = lb
            continue

        fub[i] = ub if (ub < fub[i - 1] or candles[i - 1].close > fub[i - 1]) else fub[i - 1]
        flb[i] = lb if (lb > flb[i - 1] or candles[i - 1].close < flb[i - 1]) else flb[i - 1]

        prev = dir_[i - 1] or 1
        if candles[i].close > fub[i - 1]:
            dir_[i] = 1
        elif candles[i].close < flb[i - 1]:
            dir_[i] = -1
        else:
            dir_[i] = prev

        st[i] = flb[i] if dir_[i] == 1 else fub[i]

    return st, dir_


@dataclass
class Perf:
    ret_pct: float
    pf: float
    dd_pct: float
    trades: int


def backtest_segment(seg: Sequence[Candle], st_line: List[Optional[float]], st_dir: List[Optional[int]], atr14: List[Optional[float]]) -> Perf:
    equity = INITIAL_EQUITY
    peak = equity
    max_dd = 0.0

    pos = 0
    qty = 0.0
    entry = 0.0
    entry_i = 0
    sl = 0.0
    tp = None

    gross_win = 0.0
    gross_loss = 0.0
    trades = 0

    def fee(notional: float) -> float:
        return notional * TAKER_FEE

    def mtm(price: float) -> float:
        if pos == 0:
            return equity
        return equity + qty * (price - entry) if pos == 1 else equity + qty * (entry - price)

    def mark(price: float):
        nonlocal peak, max_dd
        cur = mtm(price)
        peak = max(peak, cur)
        dd = (peak - cur) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    for i in range(120, len(seg) - 1):
        bar = seg[i]
        nxt = seg[i + 1]
        mark(bar.close)

        if pos != 0:
            # follow ST line
            if st_line[i] is not None:
                sl = max(sl, st_line[i]) if pos == 1 else min(sl, st_line[i])

            exit_px = None
            if pos == 1:
                if bar.low <= sl:
                    exit_px = sl * (1 - SLIPPAGE)
                elif tp is not None and bar.high >= tp:
                    exit_px = tp * (1 - SLIPPAGE)
            else:
                if bar.high >= sl:
                    exit_px = sl * (1 + SLIPPAGE)
                elif tp is not None and bar.low <= tp:
                    exit_px = tp * (1 + SLIPPAGE)

            if exit_px is None and st_dir[i] is not None:
                if (pos == 1 and st_dir[i] == -1) or (pos == -1 and st_dir[i] == 1):
                    exit_px = bar.close * (1 - SLIPPAGE) if pos == 1 else bar.close * (1 + SLIPPAGE)

            if exit_px is None and ST_TIME_STOP > 0 and (i - entry_i) >= ST_TIME_STOP:
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
                sl = 0.0
                tp = None
                continue

        if pos != 0:
            continue

        if st_dir[i] is None or st_dir[i - 1] is None or st_line[i] is None:
            continue
        if atr14[i] is None or atr14[i] <= 0:
            continue
        if not ((st_dir[i] == 1 and st_dir[i - 1] == -1) or (st_dir[i] == -1 and st_dir[i - 1] == 1)):
            continue

        side = 1 if st_dir[i] == 1 else -1
        exec_px = nxt.open * (1 + SLIPPAGE) if side == 1 else nxt.open * (1 - SLIPPAGE)
        sl0 = st_line[i]
        stop_dist = abs(exec_px - sl0)
        if stop_dist <= 0:
            continue

        # liq buffer
        if side == 1:
            liq = exec_px * (1 - 1 / max(1.0, float(LEVERAGE)))
            if sl0 <= liq * (1 + LIQ_BUFFER_PCT):
                continue
        else:
            liq = exec_px * (1 + 1 / max(1.0, float(LEVERAGE)))
            if sl0 >= liq * (1 - LIQ_BUFFER_PCT):
                continue

        risk_usdt = equity * RISK_PCT
        qty0 = risk_usdt / stop_dist
        max_notional = equity * MAX_MARGIN_PCT * LEVERAGE
        notional = qty0 * exec_px
        if max_notional > 0 and notional > max_notional:
            notional = max_notional
            qty0 = notional / exec_px
        if qty0 <= 0:
            continue
        equity -= fee(qty0 * exec_px)

        pos = side
        qty = qty0
        entry = exec_px
        entry_i = i + 1
        sl = sl0
        tp = (entry + ST_TP_R * stop_dist) if (side == 1 and ST_TP_R > 0) else ((entry - ST_TP_R * stop_dist) if (side == -1 and ST_TP_R > 0) else None)

    final_eq = mtm(seg[-1].close)
    ret = (final_eq / INITIAL_EQUITY - 1) * 100
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    return Perf(ret, pf, max_dd * 100, trades)


def dt_str(ms_: int) -> str:
    return datetime.fromtimestamp(ms_ / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def main():
    start_dt = parse_start(START_UTC_STR)
    end_dt = datetime.now(timezone.utc)
    candles = fetch_klines(ms(start_dt), ms(end_dt))
    closes = [c.close for c in candles]
    atr14 = atr(list(candles), 14)
    st_line, st_dir = supertrend(candles, ST_ATR_N, ST_MULT)

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

    perfs: List[Perf] = []

    print(f"Symbol={SYMBOL} {INTERVAL} segments={len(segs)} train={TRAIN_DAYS}d test={TEST_DAYS}d step={STEP_DAYS}d")
    print(f"Supertrend params: ATR_N={ST_ATR_N} MULT={ST_MULT} TP_R={ST_TP_R} TIME_STOP={ST_TIME_STOP}")
    print(f"Risk: LEV={LEVERAGE} RISK_PCT={RISK_PCT} MAX_MARGIN_PCT={MAX_MARGIN_PCT} buffer={LIQ_BUFFER_PCT}\n")

    for si, (s, e) in enumerate(segs, 1):
        idx = [i for i, c in enumerate(candles) if c.open_time_ms >= s and c.close_time_ms <= e]
        if len(idx) < 400:
            continue
        a0, a1 = idx[0], idx[-1] + 1
        seg = candles[a0:a1]
        p = backtest_segment(seg, st_line[a0:a1], st_dir[a0:a1], atr14[a0:a1])
        perfs.append(p)
        print(f"Seg#{si:02d} TEST[{dt_str(s)}..{dt_str(e)}] ret={p.ret_pct:+7.2f}% pf={p.pf:>5.2f} dd={p.dd_pct:>5.2f}% trades={p.trades:>4d}")

    print("\n=== Walk-forward summary (TEST segments) ===")
    if not perfs:
        print("No segments")
        return
    rets=[p.ret_pct for p in perfs]
    pfs=[p.pf for p in perfs if math.isfinite(p.pf)]
    dds=[p.dd_pct for p in perfs]
    trs=[p.trades for p in perfs]

    print('segments_used:', len(perfs))
    print('ret avg:', round(statistics.mean(rets),2), 'median:', round(statistics.median(rets),2))
    if pfs:
        print('pf  avg:', round(statistics.mean(pfs),2), 'median:', round(statistics.median(pfs),2))
    else:
        print('pf  avg: n/a')
    print('dd  avg:', round(statistics.mean(dds),2), 'median:', round(statistics.median(dds),2))
    print('trades avg:', round(statistics.mean(trs),2), 'median:', round(statistics.median(trs),2))


if __name__ == '__main__':
    main()
