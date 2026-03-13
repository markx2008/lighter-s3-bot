#!/usr/bin/env python3
"""strategy3_btc_backtest.py

Strategy3 (BTCUSDT UM Perp, 15m): trend-first.

Implements multiple BTCUSDT strategies (non-RSI, non-strategy2-boll clone) and
backtests with our shared risk model:
- 20x isolated
- fixed risk per trade (RISK_PCT)
- max margin cap (MAX_MARGIN_PCT)
- liquidation buffer check (LIQ_BUFFER_PCT)

Outputs summary + per-trade CSV + monthly report via separate script.

Strategies included:
1) Supertrend Breakout (ATR-based trend following)
2) EMA Pullback Trend (EMA21/EMA55 trend + pullback entry)
3) Donchian 55 breakout (classic, longer)  [optional baseline]

NOTE: BTC has lots of data; we fetch from START_UTC to now.

"""

from __future__ import annotations

import csv
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

# local import
import sys
sys.path.append(os.path.dirname(__file__))
from strategy_lab import Candle, atr, ema, sma, utc_ms_to_str

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

EXPORT_DIR = os.getenv("EXPORT_DIR", "/home/mark/.openclaw/workspace/exports")
SUMMARY_CSV = os.path.join(EXPORT_DIR, "strategy3_btc_summary.csv")
TRADES_CSV = os.path.join(EXPORT_DIR, "strategy3_btc_trades.csv")

# ---- Strategy params (tunable) ----
# Supertrend (trend-follow with dynamic stop)
ST_ATR_N = int(float(os.getenv("S3_ST_ATR_N", "10")))
ST_MULT = float(os.getenv("S3_ST_MULT", "3.0"))
ST_TP_R = float(os.getenv("S3_ST_TP_R", "2.0"))
ST_TIME_STOP = int(float(os.getenv("S3_ST_TIME_STOP", "0")))

# EMA Pullback (trend + pullback to EMA)
EP_FAST = int(float(os.getenv("S3_EP_FAST", "21")))
EP_SLOW = int(float(os.getenv("S3_EP_SLOW", "55")))
EP_PULL_ATR = float(os.getenv("S3_EP_PULL_ATR", "0.6"))
EP_STOP_ATR = float(os.getenv("S3_EP_STOP_ATR", "1.5"))
EP_TP_R = float(os.getenv("S3_EP_TP_R", "2.2"))
EP_TIME_STOP = int(float(os.getenv("S3_EP_TIME_STOP", "192")))

# Turtle / Donchian breakout (55 entry, 20 exit, ATR fail-safe)
DC_ENTRY_LEN = int(float(os.getenv("S3_DC_ENTRY_LEN", "55")))
DC_EXIT_LEN = int(float(os.getenv("S3_DC_EXIT_LEN", "20")))
DC_ATR_STOP = float(os.getenv("S3_DC_ATR_STOP", "2.0"))
DC_TP_R = float(os.getenv("S3_DC_TP_R", "0"))  # 0 = no fixed TP (let trailing exit), else TP=R*stop


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
        time.sleep(0.15)
    dedup = {c.open_time_ms: c for c in out}
    return [dedup[k] for k in sorted(dedup)]


def rolling_extreme(values: Sequence[float], window: int, use_max: bool) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if window <= 0 or len(values) < window:
        return out
    for i in range(len(values)):
        if i >= window:
            xs = values[i - window : i]
            out[i] = max(xs) if use_max else min(xs)
    return out


def supertrend(candles: Sequence[Candle], atr_n: int, mult: float) -> Tuple[List[Optional[float]], List[Optional[int]]]:
    """Returns (st_line, direction) where direction=1 uptrend, -1 downtrend."""
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

        # final bands
        fub[i] = ub if (ub < fub[i - 1] or candles[i - 1].close > fub[i - 1]) else fub[i - 1]
        flb[i] = lb if (lb > flb[i - 1] or candles[i - 1].close < flb[i - 1]) else flb[i - 1]

        prev_dir = dir_[i - 1]
        if prev_dir is None:
            prev_dir = 1

        if candles[i].close > fub[i - 1]:
            dir_[i] = 1
        elif candles[i].close < flb[i - 1]:
            dir_[i] = -1
        else:
            dir_[i] = prev_dir

        st[i] = flb[i] if dir_[i] == 1 else fub[i]

    return st, dir_


@dataclass
class Trade:
    strategy: str
    dataset: str
    entry_time_ms: int
    exit_time_ms: int
    side: str
    entry: float
    exit: float
    pnl: float
    fee: float
    reason: str


@dataclass
class Perf:
    ret_pct: float
    final_equity: float
    max_dd_pct: float
    pf: float
    trades: int
    win_rate: float
    avg_pnl: float


def backtest_supertrend(
    candles: Sequence[Candle],
    dataset: str,
    st_line: List[Optional[float]],
    st_dir: List[Optional[int]],
    atr14: List[Optional[float]],
) -> Tuple[Perf, List[Trade]]:
    """Supertrend flip entry, stop follows supertrend line. Optional fixed TP via ST_TP_R."""

    equity = INITIAL_EQUITY
    peak = equity
    max_dd = 0.0

    pos = 0
    qty = 0.0
    entry = 0.0
    entry_t = 0
    entry_i = 0

    sl = 0.0
    tp = None  # type: Optional[float]
    init_stop_dist = 0.0

    trades: List[Trade] = []
    gross_win = 0.0
    gross_loss = 0.0
    wins = 0

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

    for i in range(120, len(candles) - 1):
        bar = candles[i]
        nxt = candles[i + 1]
        mark(bar.close)

        # manage open
        if pos != 0:
            # update stop to follow supertrend line (only tighten)
            if st_line[i] is not None:
                if pos == 1:
                    sl = max(sl, st_line[i])
                else:
                    sl = min(sl, st_line[i])

            exit_px = None
            reason = None

            # stop first then tp
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

            # direction flip exit (at close)
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
                trades.append(
                    Trade("Supertrend Trend", dataset, entry_t, bar.close_time_ms, "long" if pos == 1 else "short", entry, exit_px, pnl, exit_fee, reason or "exit")
                )
                if pnl > 0:
                    wins += 1
                    gross_win += pnl
                else:
                    gross_loss += -pnl

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

        # initial stop uses st_line at signal bar
        sl0 = st_line[i]
        if sl0 is None:
            continue
        stop_dist = abs(exec_px - sl0)
        if stop_dist <= 0:
            continue

        # liquidation buffer check
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
        tp = (entry + ST_TP_R * init_stop_dist) if side == 1 and ST_TP_R > 0 else (entry - ST_TP_R * init_stop_dist if side == -1 and ST_TP_R > 0 else None)

    final_eq = mtm(candles[-1].close)
    ret = (final_eq / INITIAL_EQUITY - 1) * 100
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    wr = wins / len(trades) * 100 if trades else 0.0
    avg = sum(t.pnl for t in trades) / len(trades) if trades else 0.0

    return Perf(ret, final_eq, max_dd * 100, pf, len(trades), wr, avg), trades


def make_strategies(candles: Sequence[Candle]):
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]

    atr14 = atr(list(candles), 14)
    st_line, st_dir = supertrend(candles, ST_ATR_N, ST_MULT)

    ema_fast = ema(closes, EP_FAST)
    ema_slow = ema(closes, EP_SLOW)

    # (deprecated) old DC_LEN variables removed; use DC_ENTRY_LEN/DC_EXIT_LEN below

    def st_entry(i: int, candles_: Sequence[Candle]):
        # enter on supertrend direction flip; stop uses supertrend line (dynamic)
        if i < 2:
            return None
        if st_dir[i] is None or st_dir[i - 1] is None:
            return None
        if atr14[i] is None or atr14[i] <= 0:
            return None
        # we will use risk_dist from entry to st_line as 1R
        if st_line[i] is None:
            return None
        close = candles_[i].close
        if st_dir[i] == 1 and st_dir[i - 1] == -1:
            risk = abs(close - st_line[i])
            if risk <= 0:
                return None
            return (1, st_line[i], ST_TP_R, risk)
        if st_dir[i] == -1 and st_dir[i - 1] == 1:
            risk = abs(close - st_line[i])
            if risk <= 0:
                return None
            return (-1, st_line[i], ST_TP_R, risk)
        return None

    def ep_entry(i: int, candles_: Sequence[Candle]):
        if atr14[i] is None or atr14[i] <= 0:
            return None
        f = ema_fast[i]
        s = ema_slow[i]
        if f is None or s is None:
            return None
        close = candles_[i].close
        a = atr14[i]
        # trend filter
        if f > s:
            # buy pullback: close below fast EMA by pull_atr*ATR then recover above EMA (use previous close)
            if i >= 1 and candles_[i - 1].close < (f - EP_PULL_ATR * a) and close > f:
                return (1, None, EP_TP_R, EP_STOP_ATR * a)
        elif f < s:
            if i >= 1 and candles_[i - 1].close > (f + EP_PULL_ATR * a) and close < f:
                return (-1, None, EP_TP_R, EP_STOP_ATR * a)
        return None

    dc_entry_high = rolling_extreme(highs, DC_ENTRY_LEN, True)
    dc_entry_low = rolling_extreme(lows, DC_ENTRY_LEN, False)

    def dc_entry(i: int, candles_: Sequence[Candle]):
        if atr14[i] is None or atr14[i] <= 0:
            return None
        hi = dc_entry_high[i]
        lo = dc_entry_low[i]
        if hi is None or lo is None:
            return None
        close = candles_[i].close
        a = atr14[i]
        if close > hi:
            return (1, None, DC_TP_R, DC_ATR_STOP * a)
        if close < lo:
            return (-1, None, DC_TP_R, DC_ATR_STOP * a)
        return None

    return {
        "atr14": atr14,
        "st_line": st_line,
        "st_dir": st_dir,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "dc_entry_high": dc_entry_high,
        "dc_entry_low": dc_entry_low,
        "strategies": [
            ("EMA Pullback Trend", ep_entry),
            ("Donchian 55 Breakout", dc_entry),
        ],
    }


def main():
    start_dt = parse_start(START_UTC_STR)
    end_dt = datetime.now(timezone.utc)
    candles = fetch_klines(ms(start_dt), ms(end_dt))
    print(f"Fetched {len(candles)} candles ({SYMBOL} {INTERVAL}) from {START_UTC_STR} to {end_dt.strftime('%Y-%m-%d %H:%M')} UTC")

    split = int(len(candles) * 0.7)
    ins = candles[:split]
    oos = candles[split:]

    rows = []
    all_trades: List[Trade] = []

    ctx = make_strategies(candles)
    atr14 = ctx["atr14"]
    st_line = ctx["st_line"]
    st_dir = ctx["st_dir"]

    # --- Supertrend ---
    for ds_name, ds_candles, ds_slice in [
        ("in-sample", ins, slice(0, split)),
        ("oos", oos, slice(split, len(candles))),
        ("full", candles, slice(0, len(candles))),
    ]:
        sl = st_line[ds_slice]
        sd = st_dir[ds_slice]
        a14 = atr14[ds_slice]
        p, t = backtest_supertrend(ds_candles, ds_name, list(sl), list(sd), list(a14))
        if ds_name == "full":
            all_trades.extend(t)
        rows.append({
            "strategy": "Supertrend Trend",
            "dataset": ds_name,
            "return_pct": f"{p.ret_pct:.2f}",
            "final_equity": f"{p.final_equity:.2f}",
            "max_drawdown_pct": f"{p.max_dd_pct:.2f}",
            "profit_factor": f"{p.pf}",
            "trades": str(p.trades),
            "win_rate_pct": f"{p.win_rate:.2f}",
            "avg_pnl": f"{p.avg_pnl:.2f}",
            "taker_fee": f"{TAKER_FEE:.5f}",
            "slippage": f"{SLIPPAGE:.5f}",
        })
        if ds_name == "oos":
            print(f"{'Supertrend Trend':22s} | OOS ret {p.ret_pct:+.2f}% PF {p.pf:.3f} DD {p.max_dd_pct:.2f}% trades {p.trades}")

    # --- other strategies (entry-based) ---
    def backtest_entry_based(candles_, dataset, name, fn):
        # simple wrapper: reuse the old stop/tp model embedded in fn
        # (kept minimal; can be expanded later)
        equity = INITIAL_EQUITY
        peak = equity
        max_dd = 0.0
        pos = 0
        qty = 0.0
        entry = 0.0
        entry_t = 0
        entry_i = 0
        sl = 0.0
        tp = 0.0
        risk_dist = 0.0
        trades_ = []
        gross_win = 0.0
        gross_loss = 0.0
        wins_ = 0

        def fee(notional):
            return notional * TAKER_FEE

        def mtm(price):
            if pos == 0:
                return equity
            return equity + qty * (price - entry) if pos == 1 else equity + qty * (entry - price)

        def mark(price):
            nonlocal peak, max_dd
            cur = mtm(price)
            peak = max(peak, cur)
            dd = (peak - cur) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)

        for i in range(120, len(candles_) - 1):
            bar = candles_[i]
            nxt = candles_[i + 1]
            mark(bar.close)
            if pos != 0:
                exit_px=None; reason=None
                if pos==1:
                    if bar.low <= sl:
                        exit_px = sl*(1-SLIPPAGE); reason='stop'
                    elif bar.high >= tp:
                        exit_px = tp*(1-SLIPPAGE); reason='tp'
                else:
                    if bar.high >= sl:
                        exit_px = sl*(1+SLIPPAGE); reason='stop'
                    elif bar.low <= tp:
                        exit_px = tp*(1+SLIPPAGE); reason='tp'
                if exit_px is None and name=="EMA Pullback Trend" and EP_TIME_STOP>0 and (i-entry_i)>=EP_TIME_STOP:
                    exit_px = bar.close*(1-SLIPPAGE) if pos==1 else bar.close*(1+SLIPPAGE); reason='time'
                if exit_px is not None:
                    exit_fee = fee(qty*exit_px)
                    equity -= exit_fee
                    pnl = qty*(exit_px-entry) if pos==1 else qty*(entry-exit_px)
                    equity += pnl
                    trades_.append(Trade(name,dataset,entry_t,bar.close_time_ms,'long' if pos==1 else 'short',entry,exit_px,pnl,exit_fee,reason or 'exit'))
                    if pnl>0:
                        wins_ += 1; gross_win += pnl
                    else:
                        gross_loss += -pnl
                    pos=0; qty=0.0; entry=0.0
                    continue
            if pos!=0:
                continue
            plan = fn(i, candles_)
            if not plan:
                continue
            side, _sl_px, tp_r, risk_dist = plan
            exec_px = nxt.open*(1+SLIPPAGE) if side==1 else nxt.open*(1-SLIPPAGE)
            if side==1:
                sl = exec_px - risk_dist
                tp = exec_px + (tp_r if tp_r>0 else EP_TP_R) * risk_dist
                liq = exec_px*(1-1/max(1.0,float(LEVERAGE)))
                if sl <= liq*(1+LIQ_BUFFER_PCT):
                    continue
            else:
                sl = exec_px + risk_dist
                tp = exec_px - (tp_r if tp_r>0 else EP_TP_R) * risk_dist
                liq = exec_px*(1+1/max(1.0,float(LEVERAGE)))
                if sl >= liq*(1-LIQ_BUFFER_PCT):
                    continue
            stop_dist=abs(exec_px-sl)
            if stop_dist<=0:
                continue
            risk_usdt = equity*RISK_PCT
            qty = risk_usdt/stop_dist
            max_notional = equity*MAX_MARGIN_PCT*LEVERAGE
            notional=qty*exec_px
            if max_notional>0 and notional>max_notional:
                notional=max_notional
                qty=notional/exec_px
            if qty<=0:
                continue
            equity -= fee(qty*exec_px)
            pos=side; entry=exec_px; entry_t=nxt.open_time_ms; entry_i=i+1

        final_eq = mtm(candles_[-1].close)
        ret = (final_eq/INITIAL_EQUITY-1)*100
        pf = (gross_win/gross_loss) if gross_loss>0 else float('inf')
        wr = wins_/len(trades_)*100 if trades_ else 0.0
        avg = sum(t.pnl for t in trades_)/len(trades_) if trades_ else 0.0
        return Perf(ret, final_eq, max_dd*100, pf, len(trades_), wr, avg), trades_

    for name, fn in ctx["strategies"]:
        p_ins, _ = backtest_entry_based(ins, "in-sample", name, fn)
        p_oos, _ = backtest_entry_based(oos, "oos", name, fn)
        p_full, t_full = backtest_entry_based(candles, "full", name, fn)
        all_trades.extend(t_full)
        for ds, p in [("in-sample", p_ins), ("oos", p_oos), ("full", p_full)]:
            rows.append({
                "strategy": name,
                "dataset": ds,
                "return_pct": f"{p.ret_pct:.2f}",
                "final_equity": f"{p.final_equity:.2f}",
                "max_drawdown_pct": f"{p.max_dd_pct:.2f}",
                "profit_factor": f"{p.pf}",
                "trades": str(p.trades),
                "win_rate_pct": f"{p.win_rate:.2f}",
                "avg_pnl": f"{p.avg_pnl:.2f}",
                "taker_fee": f"{TAKER_FEE:.5f}",
                "slippage": f"{SLIPPAGE:.5f}",
            })
        print(f"{name:22s} | OOS ret {p_oos.ret_pct:+.2f}% PF {p_oos.pf:.3f} DD {p_oos.max_dd_pct:.2f}% trades {p_oos.trades}")

    os.makedirs(EXPORT_DIR, exist_ok=True)
    with open(SUMMARY_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    with open(TRADES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["strategy","dataset","side","entry_time","exit_time","entry_price","exit_price","pnl_usdt","fee_usdt","reason"])
        for t in all_trades:
            w.writerow([
                t.strategy,
                t.dataset,
                t.side,
                utc_ms_to_str(t.entry_time_ms),
                utc_ms_to_str(t.exit_time_ms),
                f"{t.entry:.2f}",
                f"{t.exit:.2f}",
                f"{t.pnl:.2f}",
                f"{t.fee:.2f}",
                t.reason,
            ])

    print("Summary written:", SUMMARY_CSV)
    print("Trades written:", TRADES_CSV)


if __name__ == "__main__":
    main()
