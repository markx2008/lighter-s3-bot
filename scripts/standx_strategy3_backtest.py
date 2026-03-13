#!/usr/bin/env python3
"""standx_strategy3_backtest.py

Backtest Strategy3 (Supertrend Trend) on StandX perps data (BTC-USD).

Goal: align with LIVE logic (Model A market entry) so the backtest is meaningful.

Data source:
- StandX public kline endpoint: /api/kline/history

Execution model (Model A):
- Entry on Supertrend direction flip at NEXT bar open price
- SL follows Supertrend line (tighten only)
- Exit priority per bar: stop -> tp -> flip (close) -> time

Fees:
- Use StandX taker_fee (symbol_info) for both entry and exit (market model)

Sizing:
- Risk-based: risk_usdt = equity * RISK_PCT
- Qty = risk_usdt / stop_dist
- Cap by margin: notional <= equity * MAX_MARGIN_PCT * LEVERAGE
- Optional MAX_NOTIONAL_USDT (0 disables)

Exports:
- exports/standx_s3_summary.csv
- exports/standx_s3_trades.csv
- exports/standx_s3_monthly.csv

Env:
  SYMBOL=BTC-USD
  RESOLUTION=15
  START_UTC=2025-07-10

  INITIAL_EQUITY=10000
  LEVERAGE=20
  RISK_PCT=0.01
  MAX_MARGIN_PCT=0.20
  MAX_NOTIONAL_USDT=0

  S3_ST_ATR_N=10
  S3_ST_MULT=3.0
  S3_ST_TP_R=2.0
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

from standx_client import StandXClient, StandXConfig
from standx_rounding import SymbolSpec
from strategy_lab import Candle, atr

SYMBOL = os.getenv("SYMBOL", "BTC-USD")
RESOLUTION = os.getenv("RESOLUTION", "15")
START_UTC_STR = os.getenv("START_UTC", "2025-07-10")

INITIAL_EQUITY = float(os.getenv("INITIAL_EQUITY", "10000"))
LEVERAGE = int(float(os.getenv("LEVERAGE", "20")))
RISK_PCT = float(os.getenv("RISK_PCT", "0.01"))
MAX_MARGIN_PCT = float(os.getenv("MAX_MARGIN_PCT", "0.20"))
MAX_NOTIONAL_USDT = float(os.getenv("MAX_NOTIONAL_USDT", "0"))

ST_ATR_N = int(float(os.getenv("S3_ST_ATR_N", "10")))
ST_MULT = float(os.getenv("S3_ST_MULT", "3.0"))
ST_TP_R = float(os.getenv("S3_ST_TP_R", "2.0"))
ST_TIME_STOP = int(float(os.getenv("S3_ST_TIME_STOP", "0")))

EXPORT_DIR = "/home/mark/.openclaw/workspace/exports"
SUMMARY_CSV = os.path.join(EXPORT_DIR, "standx_s3_summary.csv")
TRADES_CSV = os.path.join(EXPORT_DIR, "standx_s3_trades.csv")
MONTHLY_CSV = os.path.join(EXPORT_DIR, "standx_s3_monthly.csv")


def parse_start(s: str) -> int:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return int(dt.timestamp())


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
    entry_time: int
    exit_time: int
    side: str
    entry: float
    exit: float
    qty: float
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


def fetch_standx_candles(client: StandXClient, start_sec: int, end_sec: int) -> List[Candle]:
    """StandX kline history seems to return a limited window unless countback is used.

    We'll request by countback and then filter to start_sec..end_sec.
    """
    # estimate countback needed
    res_min = int(RESOLUTION)
    bars = int(((end_sec - start_sec) / 60) / res_min) + 500
    bars = max(600, min(bars, 200000))

    data = client._get(
        "/api/kline/history",
        params={
            "symbol": SYMBOL,
            "resolution": RESOLUTION,
            "from": int(start_sec),
            "to": int(end_sec),
            "countback": int(bars),
        },
    )
    if not isinstance(data, dict) or data.get("s") != "ok":
        return []

    t = data["t"]; o = data["o"]; h = data["h"]; l = data["l"]; c = data["c"]; v = data["v"]
    out: List[Candle] = []
    bar_ms = int(res_min * 60 * 1000)
    for i in range(len(t)):
        ts = int(t[i])
        if ts < start_sec or ts > end_sec:
            continue
        open_ms = ts * 1000
        close_ms = open_ms + bar_ms - 1
        out.append(
            Candle(
                open_time_ms=open_ms,
                open=float(o[i]),
                high=float(h[i]),
                low=float(l[i]),
                close=float(c[i]),
                volume=float(v[i]),
                close_time_ms=close_ms,
            )
        )
    return out


def month_key_utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m")


def backtest(candles: Sequence[Candle], taker_fee: float) -> Tuple[Perf, List[Trade], List[Tuple[int, float]]]:
    st_line, st_dir = supertrend(candles, ST_ATR_N, ST_MULT)

    equity = INITIAL_EQUITY
    peak = equity
    max_dd = 0.0

    pos = 0  # 1 long, -1 short
    qty = 0.0
    entry = 0.0
    entry_i = 0
    sl = 0.0
    tp = None  # type: Optional[float]
    init_stop_dist = 0.0

    trades: List[Trade] = []
    gross_win = 0.0
    gross_loss = 0.0
    wins = 0

    equity_curve: List[Tuple[int, float]] = []  # (close_time_ms, mtm equity)

    def fee(notional: float) -> float:
        return notional * taker_fee

    def mtm(price: float) -> float:
        if pos == 0:
            return equity
        return equity + qty * (price - entry) if pos == 1 else equity + qty * (entry - price)

    for i in range(120, len(candles) - 2):
        bar = candles[i]
        nxt = candles[i + 1]
        cur = mtm(bar.close)
        peak = max(peak, cur)
        dd = (peak - cur) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        equity_curve.append((bar.close_time_ms, cur))

        # manage position: trailing SL follows st_line (tighten only)
        if pos != 0:
            if st_line[i] is not None:
                if pos == 1:
                    sl = max(sl, st_line[i])
                else:
                    sl = min(sl, st_line[i])

            exit_px = None
            reason = None

            # stop then tp
            if pos == 1:
                if bar.low <= sl:
                    exit_px = sl
                    reason = "stop"
                elif tp is not None and bar.high >= tp:
                    exit_px = tp
                    reason = "tp"
            else:
                if bar.high >= sl:
                    exit_px = sl
                    reason = "stop"
                elif tp is not None and bar.low <= tp:
                    exit_px = tp
                    reason = "tp"

            # flip exit at close
            if exit_px is None and st_dir[i] is not None:
                if (pos == 1 and st_dir[i] == -1) or (pos == -1 and st_dir[i] == 1):
                    exit_px = bar.close
                    reason = "flip"

            if exit_px is None and ST_TIME_STOP > 0 and (i - entry_i) >= ST_TIME_STOP:
                exit_px = bar.close
                reason = "time"

            if exit_px is not None:
                exit_fee = fee(qty * exit_px)
                equity -= exit_fee
                pnl = qty * (exit_px - entry) if pos == 1 else qty * (entry - exit_px)
                equity += pnl
                trades.append(Trade(entry_time=candles[entry_i].open_time_ms, exit_time=bar.close_time_ms, side="long" if pos == 1 else "short", entry=entry, exit=exit_px, qty=qty, pnl=pnl, fee=exit_fee, reason=reason or "exit"))
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

        # entry on flip at next bar open (market model)
        if st_dir[i] is None or st_dir[i - 1] is None or st_line[i] is None:
            continue
        flip_up = st_dir[i] == 1 and st_dir[i - 1] == -1
        flip_dn = st_dir[i] == -1 and st_dir[i - 1] == 1
        if not (flip_up or flip_dn):
            continue

        side = 1 if flip_up else -1
        exec_px = nxt.open
        sl0 = st_line[i]
        stop_dist = abs(exec_px - sl0)
        if stop_dist <= 0:
            continue

        # sizing
        risk_usdt = equity * RISK_PCT
        qty0 = risk_usdt / stop_dist
        max_notional = equity * MAX_MARGIN_PCT * LEVERAGE
        notional = qty0 * exec_px
        if max_notional > 0 and notional > max_notional:
            notional = max_notional
            qty0 = notional / exec_px
        if MAX_NOTIONAL_USDT and MAX_NOTIONAL_USDT > 0:
            notional = min(notional, MAX_NOTIONAL_USDT)
            qty0 = notional / exec_px
        if qty0 <= 0:
            continue

        entry_fee = fee(qty0 * exec_px)
        equity -= entry_fee

        pos = side
        qty = qty0
        entry = exec_px
        entry_i = i + 1
        sl = float(sl0)
        init_stop_dist = stop_dist
        if ST_TP_R > 0:
            tp = (entry + ST_TP_R * init_stop_dist) if pos == 1 else (entry - ST_TP_R * init_stop_dist)
        else:
            tp = None

    final_eq = mtm(candles[-1].close)
    ret = (final_eq / INITIAL_EQUITY - 1) * 100
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    wr = wins / len(trades) * 100 if trades else 0.0
    avg = sum(t.pnl for t in trades) / len(trades) if trades else 0.0

    return Perf(ret, final_eq, max_dd * 100, pf, len(trades), wr, avg), trades, equity_curve


def write_csv(path: str, rows: List[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main():
    os.makedirs(EXPORT_DIR, exist_ok=True)

    client = StandXClient(StandXConfig())

    # symbol info -> taker fee
    info = client.symbol_info()
    taker_fee = 0.0004
    if isinstance(info, list):
        for it in info:
            if it.get("symbol") == SYMBOL:
                taker_fee = float(it.get("taker_fee", taker_fee))
                break

    start_sec = parse_start(START_UTC_STR)
    end_sec = int(time.time())

    candles = fetch_standx_candles(client, start_sec, end_sec)
    print(f"Fetched {len(candles)} StandX candles {SYMBOL} res={RESOLUTION} from {START_UTC_STR} to now")
    if len(candles) < 300:
        print("Not enough data")
        return

    perf, trades, eq = backtest(candles, taker_fee)

    # summary
    write_csv(
        SUMMARY_CSV,
        [
            {
                "symbol": SYMBOL,
                "resolution": RESOLUTION,
                "start_utc": START_UTC_STR,
                "end_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "initial_equity": f"{INITIAL_EQUITY:.2f}",
                "final_equity": f"{perf.final_equity:.2f}",
                "return_pct": f"{perf.ret_pct:.2f}",
                "max_drawdown_pct": f"{perf.max_dd_pct:.2f}",
                "profit_factor": f"{perf.pf}",
                "trades": str(perf.trades),
                "win_rate_pct": f"{perf.win_rate:.2f}",
                "avg_pnl": f"{perf.avg_pnl:.4f}",
                "taker_fee": f"{taker_fee:.6f}",
                "st_atr_n": str(ST_ATR_N),
                "st_mult": str(ST_MULT),
                "tp_r": str(ST_TP_R),
            }
        ],
    )

    # trades
    tr_rows = []
    for t in trades:
        tr_rows.append(
            {
                "entry_utc": datetime.fromtimestamp(t.entry_time / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "exit_utc": datetime.fromtimestamp(t.exit_time / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "side": t.side,
                "entry": f"{t.entry:.2f}",
                "exit": f"{t.exit:.2f}",
                "qty": f"{t.qty:.6f}",
                "pnl": f"{t.pnl:.4f}",
                "fee": f"{t.fee:.4f}",
                "reason": t.reason,
            }
        )
    write_csv(TRADES_CSV, tr_rows)

    # monthly from equity curve
    by_month: Dict[str, List[float]] = {}
    for ts, v in eq:
        mk = month_key_utc(ts)
        by_month.setdefault(mk, []).append(v)

    mo_rows = []
    for mk in sorted(by_month.keys()):
        xs = by_month[mk]
        if not xs:
            continue
        start = xs[0]
        end = xs[-1]
        ret = (end / start - 1) * 100 if start > 0 else 0.0
        peak = xs[0]
        mdd = 0.0
        for vv in xs:
            peak = max(peak, vv)
            dd = (peak - vv) / peak if peak > 0 else 0.0
            mdd = max(mdd, dd)
        mo_rows.append({"month": mk, "start_eq": f"{start:.2f}", "end_eq": f"{end:.2f}", "return_pct": f"{ret:.2f}", "max_dd_pct": f"{mdd*100:.2f}"})

    write_csv(MONTHLY_CSV, mo_rows)

    print(f"Perf: ret={perf.ret_pct:+.2f}% final={perf.final_equity:.2f} DD={perf.max_dd_pct:.2f}% PF={perf.pf:.3f} trades={perf.trades}")
    print("Wrote:")
    print(" summary:", SUMMARY_CSV)
    print(" trades :", TRADES_CSV)
    print(" monthly:", MONTHLY_CSV)


if __name__ == "__main__":
    main()
