#!/usr/bin/env python3
"""strategy3_btc_monthly_report.py

BTCUSDT Strategy3 monthly performance report (UTC) + per-trade PnL for the selected trend strategy.

Current selected strategy: Supertrend Trend (flip entry, stop follows supertrend line, exit on flip/stop/optional TP).

Exports:
- exports/strategy3_btc_equity.csv   : per-bar equity (MTM) + running drawdown
- exports/strategy3_btc_monthly.csv  : per-month return + monthly maxDD + trades + realized PnL
- exports/strategy3_btc_trades_pnl.csv: per-trade PnL (gross/fee/net)

Env:
  SYMBOL=BTCUSDT
  INTERVAL=15m
  START_UTC=2023-01-01

Risk model:
  INITIAL_EQUITY=10000
  TAKER_FEE=0.0004
  SLIPPAGE=0.0001
  LEVERAGE=20
  RISK_PCT=0.01
  MAX_MARGIN_PCT=0.20
  LIQ_BUFFER_PCT=0.005

Supertrend params:
  S3_ST_ATR_N=10
  S3_ST_MULT=3.0
  S3_ST_TP_R=2.0    # 0 disables fixed TP
  S3_ST_TIME_STOP=0

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

import sys
sys.path.append(os.path.dirname(__file__))
from strategy_lab import Candle, atr, utc_ms_to_str

SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
INTERVAL = os.getenv("INTERVAL", "15m")
START_UTC_STR = os.getenv("START_UTC", "2023-01-01")
BINANCE_FAPI = "https://fapi.binance.com"

INITIAL_EQUITY = float(os.getenv("INITIAL_EQUITY", "10000"))
TAKER_FEE = float(os.getenv("TAKER_FEE", "0.0004"))
SLIPPAGE = float(os.getenv("SLIPPAGE", "0.0001"))

LEVERAGE = int(float(os.getenv("LEVERAGE", "20")))
RISK_PCT = float(os.getenv("RISK_PCT", "0.01"))
MAX_MARGIN_PCT = float(os.getenv("MAX_MARGIN_PCT", "0.20"))
LIQ_BUFFER_PCT = float(os.getenv("LIQ_BUFFER_PCT", "0.005"))

ST_ATR_N = int(float(os.getenv("S3_ST_ATR_N", "10")))
ST_MULT = float(os.getenv("S3_ST_MULT", "3.0"))
ST_TP_R = float(os.getenv("S3_ST_TP_R", "2.0"))
ST_TIME_STOP = int(float(os.getenv("S3_ST_TIME_STOP", "0")))

EXPORT_DIR = os.getenv("EXPORT_DIR", "/home/mark/.openclaw/workspace/exports")
EQUITY_CSV = os.path.join(EXPORT_DIR, "strategy3_btc_equity.csv")
MONTHLY_CSV = os.path.join(EXPORT_DIR, "strategy3_btc_monthly.csv")
TRADES_CSV = os.path.join(EXPORT_DIR, "strategy3_btc_trades_pnl.csv")


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
class Trade:
    entry_time_ms: int
    exit_time_ms: int
    side: int
    entry: float
    exit: float
    qty: float
    pnl: float
    fee: float
    reason: str


def month_key_utc(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m")


def write_csv(path: str, rows: List[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def write_trades_csv(path: str, trades: List[Trade]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "進場時間(UTC)",
            "出場時間(UTC)",
            "方向",
            "進場價",
            "出場價",
            "數量(qty)",
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
                f"{t.entry:.2f}",
                f"{t.exit:.2f}",
                f"{t.qty:.6f}",
                f"{t.pnl:.2f}",
                f"{t.fee:.2f}",
                f"{(t.pnl - t.fee):.2f}",
                t.reason,
            ])


def write_monthly_csv(path: str, equity_rows: List[dict], trades: List[Trade]) -> None:
    series: Dict[str, List[float]] = {}
    for r in equity_rows:
        t = r["時間(UTC)"]
        dt = datetime.strptime(t, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        mk = dt.strftime("%Y-%m")
        series.setdefault(mk, []).append(float(r["權益(含浮盈,USDT)"]))

    trade_pnl: Dict[str, float] = {}
    trade_cnt: Dict[str, int] = {}
    for tr in trades:
        mk = month_key_utc(tr.exit_time_ms)
        trade_pnl[mk] = trade_pnl.get(mk, 0.0) + (tr.pnl - tr.fee)
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
                "當月淨PnL(USDT)": f"{trade_pnl.get(mk, 0.0):.2f}",
            }
        )

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        if rows:
            w.writeheader()
            w.writerows(rows)


def backtest_supertrend_full(candles: Sequence[Candle]) -> Tuple[List[dict], List[Trade]]:
    atr14 = atr(list(candles), 14)
    st_line, st_dir = supertrend(candles, ST_ATR_N, ST_MULT)

    equity = INITIAL_EQUITY
    peak = equity

    pos = 0
    qty = 0.0
    entry = 0.0
    entry_t = 0
    entry_i = 0
    sl = 0.0
    tp = None  # type: Optional[float]
    init_stop_dist = 0.0

    equity_rows: List[dict] = []
    trades: List[Trade] = []

    def fee(notional: float) -> float:
        return notional * TAKER_FEE

    def mtm(price: float) -> float:
        if pos == 0:
            return equity
        return equity + qty * (price - entry) if pos == 1 else equity + qty * (entry - price)

    for i in range(120, len(candles) - 1):
        bar = candles[i]
        nxt = candles[i + 1]

        cur = mtm(bar.close)
        peak = max(peak, cur)
        dd = (peak - cur) / peak if peak > 0 else 0.0
        equity_rows.append(
            {
                "時間(UTC)": utc_ms_to_str(bar.close_time_ms),
                "收盤價": f"{bar.close:.2f}",
                "權益(含浮盈,USDT)": f"{cur:.2f}",
                "權益峰值(USDT)": f"{peak:.2f}",
                "回撤(%)": f"{dd*100:.4f}",
            }
        )

        if pos != 0:
            # follow st line (tighten only)
            if st_line[i] is not None:
                sl = max(sl, st_line[i]) if pos == 1 else min(sl, st_line[i])

            exit_px = None
            reason = None
            if pos == 1:
                if bar.low <= sl:
                    exit_px = sl * (1 - SLIPPAGE)
                    reason = "stop"
                elif tp is not None and bar.high >= tp:
                    exit_px = tp * (1 - SLIPPAGE)
                    reason = "tp"
            else:
                if bar.high >= sl:
                    exit_px = sl * (1 + SLIPPAGE)
                    reason = "stop"
                elif tp is not None and bar.low <= tp:
                    exit_px = tp * (1 + SLIPPAGE)
                    reason = "tp"

            if exit_px is None and st_dir[i] is not None:
                if (pos == 1 and st_dir[i] == -1) or (pos == -1 and st_dir[i] == 1):
                    exit_px = bar.close * (1 - SLIPPAGE) if pos == 1 else bar.close * (1 + SLIPPAGE)
                    reason = "flip"

            if exit_px is None and ST_TIME_STOP > 0 and (i - entry_i) >= ST_TIME_STOP:
                exit_px = bar.close * (1 - SLIPPAGE) if pos == 1 else bar.close * (1 + SLIPPAGE)
                reason = "time"

            if exit_px is not None:
                exit_fee = fee(qty * exit_px)
                equity -= exit_fee
                pnl = qty * (exit_px - entry) if pos == 1 else qty * (entry - exit_px)
                equity += pnl
                trades.append(Trade(entry_t, bar.close_time_ms, pos, entry, exit_px, qty, pnl, exit_fee, reason or "exit"))
                pos = 0
                qty = 0.0
                entry = 0.0
                sl = 0.0
                tp = None
                init_stop_dist = 0.0
                continue

        if pos != 0:
            continue

        # entry on flip
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

        # liquidation buffer
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

        entry_fee = fee(qty0 * exec_px)
        equity -= entry_fee

        pos = side
        qty = qty0
        entry = exec_px
        entry_t = nxt.open_time_ms
        entry_i = i + 1
        sl = sl0
        init_stop_dist = stop_dist
        if ST_TP_R > 0:
            tp = (entry + ST_TP_R * init_stop_dist) if pos == 1 else (entry - ST_TP_R * init_stop_dist)
        else:
            tp = None

    return equity_rows, trades


def main():
    start_dt = parse_start(START_UTC_STR)
    end_dt = datetime.now(timezone.utc)
    candles = fetch_klines(ms(start_dt), ms(end_dt))
    print(f"Fetched {len(candles)} candles ({SYMBOL} {INTERVAL})")

    equity_rows, trades = backtest_supertrend_full(candles)

    os.makedirs(EXPORT_DIR, exist_ok=True)
    write_csv(EQUITY_CSV, equity_rows)
    write_monthly_csv(MONTHLY_CSV, equity_rows, trades)
    write_trades_csv(TRADES_CSV, trades)

    print("Wrote:")
    print(" equity :", EQUITY_CSV)
    print(" monthly:", MONTHLY_CSV)
    print(" trades :", TRADES_CSV, "count", len(trades))


if __name__ == "__main__":
    main()
