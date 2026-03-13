#!/usr/bin/env python3
"""strategy2_walkforward.py

Walk-forward validation for Strategy2 (currently focuses on Bollinger Squeeze Breakout tuned params).

Why:
- A single 70/30 split can be lucky.
- Walk-forward tests robustness across multiple regimes.

Method:
- Rolling windows: train window then test window.
- For now, we do NOT re-optimize per window (to avoid look-ahead). We evaluate
  the tuned parameter set across every test segment.

Env:
  TRAIN_DAYS=45
  TEST_DAYS=15
  STEP_DAYS=15

  # Strategy params (same as strategy2_backtest defaults)
  S2_BOLL_SQ_LEN=12
  S2_BOLL_BW=0.05
  S2_BOLL_STOP=1.0
  S2_BOLL_TP=2.2
  S2_BOLL_TRAIL=0.7
  S2_BOLL_TRAIL_START=2.2
  S2_BOLL_TIME_STOP=96

Risk model env (shared):
  INITIAL_EQUITY=10000
  TAKER_FEE=0.0004
  SLIPPAGE=0.0001
  LEVERAGE=20
  RISK_PCT=0.01
  MAX_MARGIN_PCT=0.20
  LIQ_BUFFER_PCT=0.005
  MIN_ATR_PCT=0.012

Output:
- per-segment perf: ret, PF, DD, trades
- aggregated: avg/median ret, PF, DD

"""

from __future__ import annotations

import math
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Sequence

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

# tuned boll params
S2_BOLL_SQ_LEN = int(float(os.getenv("S2_BOLL_SQ_LEN", "12")))
S2_BOLL_BW = float(os.getenv("S2_BOLL_BW", "0.05"))
S2_BOLL_STOP = float(os.getenv("S2_BOLL_STOP", "1.0"))
S2_BOLL_TP = float(os.getenv("S2_BOLL_TP", "2.2"))
S2_BOLL_TRAIL = float(os.getenv("S2_BOLL_TRAIL", "0.7"))
S2_BOLL_TRAIL_START = float(os.getenv("S2_BOLL_TRAIL_START", "2.2"))
S2_BOLL_TIME_STOP = int(float(os.getenv("S2_BOLL_TIME_STOP", "96")))
S2_BOLL_VOL_FACTOR = float(os.getenv("S2_BOLL_VOL_FACTOR", "1.20"))
S2_BOLL_EMA55_FILTER = os.getenv("S2_BOLL_EMA55", "0") not in ("0", "false", "False")
S2_BOLL_BREAK_ATR = float(os.getenv("S2_BOLL_BREAK_ATR", "0.20"))


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


def backtest_segment(candles: Sequence[Candle]) -> Perf:
    if len(candles) < 400:
        return Perf(0.0, 0.0, 0.0, 0)

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

    start = max(40, 20) + 2
    for i in range(start, n - 1):
        bar = candles[i]
        nxt = candles[i + 1]
        a = atr14[i]
        if a is None or a <= 0:
            continue

        mark(bar.close)

        # manage
        if pos != 0:
            best_px = max(best_px, bar.high) if pos == 1 else min(best_px, bar.low)
            move = (best_px - entry) / a if pos == 1 else (entry - best_px) / a
            if move >= S2_BOLL_TRAIL_START:
                trail_on = True
            if trail_on:
                tstop = best_px - S2_BOLL_TRAIL * a if pos == 1 else best_px + S2_BOLL_TRAIL * a
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

            if exit_px is None and S2_BOLL_TIME_STOP > 0 and (i - entry_i) >= S2_BOLL_TIME_STOP:
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

        # entry
        if bw[i] is None or upper[i] is None or lower[i] is None:
            continue
        if not has_recent_squeeze(i, bw, S2_BOLL_SQ_LEN, S2_BOLL_BW):
            continue

        close = bar.close
        if (a / close) < MIN_ATR_PCT:
            continue

        # volume filter
        vol = bar.volume
        # compute vol_sma20 on the fly (segment-local)
        if i >= 20:
            vol_sma = sum(c.volume for c in candles[i-20:i]) / 20.0
            if vol_sma > 0 and vol < vol_sma * S2_BOLL_VOL_FACTOR:
                continue

        side = 0
        ema55 = None
        # compute ema55 on the fly (segment-local) using 55-period EMA of closes
        # simple recursive EMA
        if i >= 55:
            k = 2 / (55 + 1)
            ema = closes[i-55]
            for j in range(i-55+1, i+1):
                ema = closes[j] * k + ema * (1 - k)
            ema55 = ema

        if close > upper[i]:
            if S2_BOLL_EMA55_FILTER and ema55 is not None and close < ema55:
                continue
            if S2_BOLL_BREAK_ATR > 0 and close < (upper[i] + S2_BOLL_BREAK_ATR * a):
                continue
            side = 1
        elif close < lower[i]:
            if S2_BOLL_EMA55_FILTER and ema55 is not None and close > ema55:
                continue
            if S2_BOLL_BREAK_ATR > 0 and close > (lower[i] - S2_BOLL_BREAK_ATR * a):
                continue
            side = -1
        else:
            continue

        exec_px = nxt.open * (1 + SLIPPAGE) if side == 1 else nxt.open * (1 - SLIPPAGE)
        risk_dist = S2_BOLL_STOP * a

        if side == 1:
            sl = exec_px - risk_dist
            tp = exec_px + S2_BOLL_TP * risk_dist
            liq = exec_px * (1 - 1 / max(1.0, float(LEVERAGE)))
            if sl <= liq * (1 + LIQ_BUFFER_PCT):
                continue
        else:
            sl = exec_px + risk_dist
            tp = exec_px - S2_BOLL_TP * risk_dist
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
        segs.append((cur - train_ms, cur, cur, cur + test_ms))
        cur += step_ms

    perfs: List[Perf] = []

    print(f"Symbol={SYMBOL} {INTERVAL} segments={len(segs)} train={TRAIN_DAYS}d test={TEST_DAYS}d step={STEP_DAYS}d")
    print(f"Boll params: SQ_LEN={S2_BOLL_SQ_LEN} BW={S2_BOLL_BW} STOP={S2_BOLL_STOP} TP={S2_BOLL_TP} TRAIL={S2_BOLL_TRAIL} TSTART={S2_BOLL_TRAIL_START} TSTOP={S2_BOLL_TIME_STOP}")
    print(f"Risk: LEV={LEVERAGE} RISK_PCT={RISK_PCT} MAX_MARGIN_PCT={MAX_MARGIN_PCT} MIN_ATR_PCT={MIN_ATR_PCT} buffer={LIQ_BUFFER_PCT}\n")

    used = 0
    for i, (tr_s, tr_e, te_s, te_e) in enumerate(segs, 1):
        seg = [c for c in candles if c.open_time_ms >= te_s and c.close_time_ms <= te_e]
        if len(seg) < 400:
            continue
        p = backtest_segment(seg)
        perfs.append(p)
        used += 1
        print(f"Seg#{i:02d} TEST[{dt_str(te_s)}..{dt_str(te_e)}] ret={p.ret_pct:+6.2f}% pf={p.pf:>5.2f} dd={p.dd_pct:>5.2f}% trades={p.trades:>3d}")

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
        print('pf  avg: n/a (no trades)')
    print('dd  avg:', round(statistics.mean(dds),2), 'median:', round(statistics.median(dds),2))
    print('trades avg:', round(statistics.mean(trs),2), 'median:', round(statistics.median(trs),2))


if __name__ == '__main__':
    main()
