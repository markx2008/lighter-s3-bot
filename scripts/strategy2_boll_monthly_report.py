#!/usr/bin/env python3
"""strategy2_boll_monthly_report.py

Monthly performance + monthly MaxDD report for Strategy2 Bollinger Squeeze Breakout ONLY.

- Uses 币安人生USDT UM perpetual 15m klines
- Uses the same 20x isolated fixed-risk sizing model we use elsewhere
- Exports:
  - exports/strategy2_boll_equity.csv (UTC): per-bar equity (MTM) + running DD
  - exports/strategy2_boll_monthly.csv: per-month return + maxDD + trades + pnl

This is intended to answer: "每月績效 maxdd如何".

Env (defaults are the tuned A setup):
  START_UTC=2025-10-20
  INITIAL_EQUITY=10000
  TAKER_FEE=0.0004
  SLIPPAGE=0.0001

  LEVERAGE=20
  RISK_PCT=0.01
  MAX_MARGIN_PCT=0.20
  LIQ_BUFFER_PCT=0.005
  MIN_ATR_PCT=0.012

  # Bollinger tuned
  S2_BOLL_SQ_LEN=12
  S2_BOLL_BW=0.05
  S2_BOLL_STOP=1.0
  S2_BOLL_TP=2.2
  S2_BOLL_TRAIL=0.7
  S2_BOLL_TRAIL_START=2.2
  S2_BOLL_TIME_STOP=96
  S2_BOLL_VOL_FACTOR=1.2
  S2_BOLL_BREAK_ATR=0.30

"""

from __future__ import annotations

import csv
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import requests

from strategy_lab import Candle, atr, sma, utc_ms_to_str

SYMBOL = os.getenv("SYMBOL", "币安人生USDT")
INTERVAL = os.getenv("INTERVAL", "15m")
BINANCE_FAPI = "https://fapi.binance.com"

START_UTC_STR = os.getenv("START_UTC", "2025-10-20")

INITIAL_EQUITY = float(os.getenv("INITIAL_EQUITY", "10000"))
TAKER_FEE = float(os.getenv("TAKER_FEE", "0.0004"))
SLIPPAGE = float(os.getenv("SLIPPAGE", "0.0001"))

LEVERAGE = int(float(os.getenv("LEVERAGE", "20")))
RISK_PCT = float(os.getenv("RISK_PCT", "0.01"))
MAX_MARGIN_PCT = float(os.getenv("MAX_MARGIN_PCT", "0.20"))
LIQ_BUFFER_PCT = float(os.getenv("LIQ_BUFFER_PCT", "0.005"))
MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.012"))

# Bollinger tuned defaults (A)
SQ_LEN = int(float(os.getenv("S2_BOLL_SQ_LEN", "12")))
BW_THR = float(os.getenv("S2_BOLL_BW", "0.05"))
STOP_ATR = float(os.getenv("S2_BOLL_STOP", "1.0"))
TP_ATR = float(os.getenv("S2_BOLL_TP", "2.2"))
TRAIL_ATR = float(os.getenv("S2_BOLL_TRAIL", "0.7"))
TRAIL_START = float(os.getenv("S2_BOLL_TRAIL_START", "2.2"))
TIME_STOP = int(float(os.getenv("S2_BOLL_TIME_STOP", "96")))
VOL_FACTOR = float(os.getenv("S2_BOLL_VOL_FACTOR", "1.2"))
BREAK_ATR = float(os.getenv("S2_BOLL_BREAK_ATR", "0.30"))

# Exit structure (new): partial take-profit + move SL to breakeven
PARTIAL_TP_R = float(os.getenv("S2_BOLL_PARTIAL_TP_R", "1.0"))   # take partial at +1R
PARTIAL_PCT = float(os.getenv("S2_BOLL_PARTIAL_PCT", "0.50"))     # close 50%
MOVE_SL_TO_BE = os.getenv("S2_BOLL_MOVE_SL_BE", "1") not in ("0", "false", "False")
BE_BUFFER_ATR = float(os.getenv("S2_BOLL_BE_BUFFER_ATR", "0.00")) # optional BE + buffer*ATR

EXPORT_DIR = os.getenv("EXPORT_DIR", "/home/mark/.openclaw/workspace/exports")
EQUITY_CSV = os.path.join(EXPORT_DIR, "strategy2_boll_equity.csv")
MONTHLY_CSV = os.path.join(EXPORT_DIR, "strategy2_boll_monthly.csv")
TRADES_CSV = os.path.join(EXPORT_DIR, "strategy2_boll_trades.csv")
LEGS_CSV = os.path.join(EXPORT_DIR, "strategy2_boll_legs.csv")


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
        time.sleep(0.2)
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


def month_key_utc(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m")


@dataclass
class Trade:
    entry_time_ms: int
    exit_time_ms: int
    side: int
    entry: float
    exit: float
    pnl: float
    fee: float
    reason: str


@dataclass
class Leg:
    entry_time_ms: int
    exit_time_ms: int
    side: int
    entry: float
    exit: float
    qty: float
    pnl: float
    fee: float
    reason: str


def backtest_full(candles: Sequence[Candle]) -> Tuple[List[dict], List[Trade], List[Leg]]:
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
    entry_t = 0
    entry_fee = 0.0
    risk_dist = 0.0
    partial_px = 0.0
    partial_done = False
    stop_px = 0.0
    tp_px = 0.0
    best_px = 0.0
    trail_on = False
    realized_pnl = 0.0
    realized_fee = 0.0

    trades: List[Trade] = []            # position-level
    legs: List[Leg] = []                # leg-level (partial/full exits)
    equity_rows: List[dict] = []

    def fee(notional: float) -> float:
        return notional * TAKER_FEE

    def mtm(price: float) -> float:
        if pos == 0:
            return equity
        return equity + qty * (price - entry) if pos == 1 else equity + qty * (entry - price)

    start = 60

    for i in range(start, n - 1):
        bar = candles[i]
        nxt = candles[i + 1]
        a = atr14[i]
        if a is None or a <= 0:
            continue

        cur = mtm(bar.close)
        peak = max(peak, cur)
        dd = (peak - cur) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

        equity_rows.append(
            {
                "時間(UTC)": utc_ms_to_str(bar.close_time_ms),
                "收盤價": f"{bar.close:.6f}",
                "權益(含浮盈,USDT)": f"{cur:.2f}",
                "權益峰值(USDT)": f"{peak:.2f}",
                "回撤(%)": f"{dd*100:.4f}",
            }
        )

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
            reason = None

            # conservative: stop first then full tp
            if pos == 1:
                if bar.low <= stop_px:
                    exit_px = stop_px * (1 - SLIPPAGE)
                    reason = "stop"
                elif bar.high >= tp_px:
                    exit_px = tp_px * (1 - SLIPPAGE)
                    reason = "tp"
            else:
                if bar.high >= stop_px:
                    exit_px = stop_px * (1 + SLIPPAGE)
                    reason = "stop"
                elif bar.low <= tp_px:
                    exit_px = tp_px * (1 + SLIPPAGE)
                    reason = "tp"

            # partial take profit (only if not already fully exited)
            if exit_px is None and (not partial_done) and PARTIAL_PCT > 0 and PARTIAL_PCT < 1 and PARTIAL_TP_R > 0:
                if pos == 1 and bar.high >= partial_px:
                    leg_qty = qty * PARTIAL_PCT
                    leg_exit = partial_px * (1 - SLIPPAGE)
                    leg_fee = fee(leg_qty * leg_exit)
                    equity -= leg_fee
                    leg_pnl = leg_qty * (leg_exit - entry)
                    equity += leg_pnl
                    qty -= leg_qty
                    realized_pnl += leg_pnl
                    realized_fee += leg_fee
                    legs.append(Leg(entry_t, bar.close_time_ms, pos, entry, leg_exit, leg_qty, leg_pnl, leg_fee, "tp1"))
                    partial_done = True
                    # move SL to breakeven
                    if MOVE_SL_TO_BE:
                        be = entry + BE_BUFFER_ATR * risk_dist
                        stop_px = max(stop_px, be)
                elif pos == -1 and bar.low <= partial_px:
                    leg_qty = qty * PARTIAL_PCT
                    leg_exit = partial_px * (1 + SLIPPAGE)
                    leg_fee = fee(leg_qty * leg_exit)
                    equity -= leg_fee
                    leg_pnl = leg_qty * (entry - leg_exit)
                    equity += leg_pnl
                    qty -= leg_qty
                    realized_pnl += leg_pnl
                    realized_fee += leg_fee
                    legs.append(Leg(entry_t, bar.close_time_ms, pos, entry, leg_exit, leg_qty, leg_pnl, leg_fee, "tp1"))
                    partial_done = True
                    if MOVE_SL_TO_BE:
                        be = entry - BE_BUFFER_ATR * risk_dist
                        stop_px = min(stop_px, be)

            if exit_px is None and TIME_STOP > 0 and (i - entry_i) >= TIME_STOP:
                exit_px = bar.close * (1 - SLIPPAGE) if pos == 1 else bar.close * (1 + SLIPPAGE)
                reason = "time"

            # full exit (remaining qty)
            if exit_px is not None:
                exit_fee = fee(qty * exit_px)
                equity -= exit_fee
                pnl = qty * (exit_px - entry) if pos == 1 else qty * (entry - exit_px)
                equity += pnl

                realized_pnl += pnl
                realized_fee += exit_fee
                legs.append(Leg(entry_t, bar.close_time_ms, pos, entry, exit_px, qty, pnl, exit_fee, reason or "exit"))

                # position-level record includes entry+exit fees
                trades.append(Trade(entry_t, bar.close_time_ms, pos, entry, exit_px, realized_pnl, entry_fee + realized_fee, reason or "exit"))

                pos = 0
                qty = 0.0
                entry = 0.0
                entry_fee = 0.0
                risk_dist = 0.0
                partial_px = 0.0
                partial_done = False
                realized_pnl = 0.0
                realized_fee = 0.0
                trail_on = False
                continue

        # entry
        if bw[i] is None or upper[i] is None or lower[i] is None:
            continue
        if not has_recent_squeeze(i, bw, SQ_LEN, BW_THR):
            continue

        close = bar.close
        if (a / close) < MIN_ATR_PCT:
            continue

        # volume filter
        if VOL_FACTOR > 0 and i >= 20:
            vol_sma = sum(c.volume for c in candles[i - 20 : i]) / 20.0
            if vol_sma > 0 and bar.volume < vol_sma * VOL_FACTOR:
                continue

        side = 0
        if close > upper[i]:
            if BREAK_ATR > 0 and close < (upper[i] + BREAK_ATR * a):
                continue
            side = 1
        elif close < lower[i]:
            if BREAK_ATR > 0 and close > (lower[i] - BREAK_ATR * a):
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

        entry_fee = fee(qty * exec_px)
        equity -= entry_fee

        pos = side
        entry = exec_px
        entry_i = i + 1
        entry_t = nxt.open_time_ms
        # store risk distance (1R)
        risk_dist = STOP_ATR * a
        partial_px = entry + PARTIAL_TP_R * risk_dist if pos == 1 else entry - PARTIAL_TP_R * risk_dist
        partial_done = False
        realized_pnl = 0.0
        realized_fee = 0.0

        stop_px = sl
        tp_px = tp
        best_px = entry
        trail_on = False

    return equity_rows, trades, legs


def write_csv(path: str, rows: List[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_trades(path: str, trades: List[Trade]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "進場時間(UTC)",
            "出場時間(UTC)",
            "方向",
            "進場價",
            "出場價",
            "盈虧(USDT)",
            "手續費(USDT)",
            "淨盈虧(USDT)",
            "出場原因",
        ])
        for t in trades:
            w.writerow([
                utc_ms_to_str(t.entry_time_ms),
                utc_ms_to_str(t.exit_time_ms),
                "做多" if t.side == 1 else "做空",
                f"{t.entry:.6f}",
                f"{t.exit:.6f}",
                f"{t.pnl:.2f}",
                f"{t.fee:.2f}",
                f"{(t.pnl - t.fee):.2f}",
                t.reason,
            ])


def write_legs(path: str, legs: List[Leg]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "進場時間(UTC)",
            "出場時間(UTC)",
            "方向",
            "腿別",
            "進場價",
            "出場價",
            "數量(qty)",
            "盈虧(USDT)",
            "手續費(USDT)",
            "淨盈虧(USDT)",
        ])
        for leg in legs:
            w.writerow([
                utc_ms_to_str(leg.entry_time_ms),
                utc_ms_to_str(leg.exit_time_ms),
                "做多" if leg.side == 1 else "做空",
                leg.reason,
                f"{leg.entry:.6f}",
                f"{leg.exit:.6f}",
                f"{leg.qty:.4f}",
                f"{leg.pnl:.2f}",
                f"{leg.fee:.2f}",
                f"{(leg.pnl - leg.fee):.2f}",
            ])


def write_monthly(path: str, equity_rows: List[dict], trades: List[Trade]) -> None:
    # equity_rows equity is mtm; compute monthly return and monthly maxDD
    # parse back equity as float
    per_month: Dict[str, Dict[str, float]] = {}
    series: Dict[str, List[float]] = {}

    for r in equity_rows:
        t = r["時間(UTC)"]
        # t format: YYYY-MM-DD HH:MM
        dt = datetime.strptime(t, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        mk = dt.strftime("%Y-%m")
        eq = float(r["權益(含浮盈,USDT)"])
        series.setdefault(mk, []).append(eq)

    # trade pnl/count by month (exit month)
    trade_pnl: Dict[str, float] = {}
    trade_cnt: Dict[str, int] = {}
    for tr in trades:
        mk = month_key_utc(tr.exit_time_ms)
        trade_pnl[mk] = trade_pnl.get(mk, 0.0) + tr.pnl
        trade_cnt[mk] = trade_cnt.get(mk, 0) + 1

    rows = []
    for mk in sorted(series.keys()):
        xs = series[mk]
        if not xs:
            continue
        start = xs[0]
        end = xs[-1]
        ret = (end / start - 1) * 100 if start > 0 else 0.0
        peak = xs[0]
        mdd = 0.0
        for v in xs:
            peak = max(peak, v)
            dd = (peak - v) / peak if peak > 0 else 0.0
            mdd = max(mdd, dd)
        rows.append(
            {
                "月份(UTC)": mk,
                "月初權益": f"{start:.2f}",
                "月末權益": f"{end:.2f}",
                "月報酬(%)": f"{ret:.2f}",
                "當月最大回撤(%)": f"{mdd*100:.2f}",
                "當月交易筆數": str(trade_cnt.get(mk, 0)),
                "當月已實現PnL(USDT)": f"{trade_pnl.get(mk, 0.0):.2f}",
            }
        )

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        if rows:
            w.writeheader()
            w.writerows(rows)


def main():
    start_dt = parse_start(START_UTC_STR)
    end_dt = datetime.now(timezone.utc)

    candles = fetch_klines(ms(start_dt), ms(end_dt))
    if len(candles) < 800:
        raise SystemExit(f"not enough candles: {len(candles)}")

    equity_rows, trades, legs = backtest_full(candles)

    write_csv(EQUITY_CSV, equity_rows)
    write_monthly(MONTHLY_CSV, equity_rows, trades)
    write_trades(TRADES_CSV, trades)
    write_legs(LEGS_CSV, legs)

    print("Bollinger report written:")
    print(" equity :", EQUITY_CSV)
    print(" monthly:", MONTHLY_CSV)
    print(" trades :", TRADES_CSV)
    print(" legs   :", LEGS_CSV)
    print(" trades count:", len(trades))


if __name__ == "__main__":
    main()
