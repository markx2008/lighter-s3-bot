#!/usr/bin/env python3
"""backtest_reversal_atr_trailing.py

回測：使用「反轉訊號」進出場，並加上
- 初始停損（ATR 倍數）
- 移動停利（ATR trailing / Chandelier 類）
- 進場：下一根開盤

不會動到你現有的通知/排程腳本。

⚠️ 研究用途：回測不代表未來績效。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import requests

from strategy_lab import Candle, atr, utc_ms_to_str

# --------- Config ---------
SYMBOL = "币安人生USDT"
INTERVAL = "15m"
START_UTC = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

INITIAL_EQUITY = 10_000.0
TAKER_FEE = 0.0004
SLIPPAGE = 0.0001  # 0.01% 模擬滑點

MIN_CONSECUTIVE = 3
ATR_N = 14
SL_ATR = 1.0      # 初始停損：1.0 * ATR
TRAIL_ATR = 2.0   # 移動停利：2.0 * ATR

# 若持倉中出現反向訊號：在下一根開盤「平倉+反手」
FLIP_ON_SIGNAL = True

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

    # dedup
    m = {c.open_time_ms: c for c in out}
    return [m[k] for k in sorted(m.keys())]


def reversal_signal_at_close(candles: List[Candle], i: int) -> Tuple[int, Optional[str], int]:
    """在第 i 根（已收盤）判斷反轉訊號。

    return (desired_pos, direction, count)
      desired_pos: +1 long, -1 short, 0 none
      direction:
        - 'short_to_long'（連跌後轉漲） -> desired long
        - 'long_to_short'（連漲後轉跌） -> desired short
      count: 反轉前連續根數

    規則：
      - 若當根是陽線：往前數連續陰線 count
      - 若當根是陰線：往前數連續陽線 count
      - count >= MIN_CONSECUTIVE 才算訊號
    """
    cur = candles[i]
    if cur.bullish:
        # count consecutive bearish before
        cnt = 0
        j = i - 1
        while j >= 0 and (not candles[j].bullish):
            cnt += 1
            j -= 1
        if cnt >= MIN_CONSECUTIVE:
            return 1, "short_to_long", cnt
        return 0, None, cnt
    else:
        cnt = 0
        j = i - 1
        while j >= 0 and candles[j].bullish:
            cnt += 1
            j -= 1
        if cnt >= MIN_CONSECUTIVE:
            return -1, "long_to_short", cnt
        return 0, None, cnt


@dataclass
class Trade:
    entry_time_ms: int
    exit_time_ms: int
    side: str  # long|short
    entry: float
    exit: float
    pnl_usdt: float
    reason: str


def apply_fee(equity: float, notional: float) -> float:
    return equity - (notional * TAKER_FEE)


def backtest(candles: List[Candle]):
    closes = [c.close for c in candles]
    atr14 = atr(candles, ATR_N)

    equity = INITIAL_EQUITY
    peak = equity
    max_dd = 0.0

    pos = 0  # +1 long, -1 short, 0 flat
    qty = 0.0
    entry = 0.0
    entry_time = 0

    stop = None  # type: Optional[float]
    hh = None    # highest high since entry
    ll = None    # lowest low since entry

    trades: List[Trade] = []

    def mark_to_market(price: float):
        nonlocal peak, max_dd
        cur = equity
        if pos != 0:
            if pos == 1:
                cur = equity + qty * (price - entry)
            else:
                cur = equity + qty * (entry - price)
        peak = max(peak, cur)
        dd = (peak - cur) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    for i in range(ATR_N + 2, len(candles) - 1):
        c = candles[i]
        n = candles[i + 1]

        mark_to_market(c.close)

        # 1) 先處理持倉的停損 / trailing（用當根 high/low）
        if pos != 0 and stop is not None:
            # conservative: stop hit first
            if pos == 1 and c.low <= stop:
                exit_px = stop * (1 - SLIPPAGE)
                notional = qty * exit_px
                equity = apply_fee(equity, notional)
                pnl = qty * (exit_px - entry)
                equity += pnl
                trades.append(Trade(entry_time, c.close_time_ms, "long", entry, exit_px, pnl, "stop"))
                pos = 0
                qty = 0.0
                stop = None
                hh = None
                ll = None
            elif pos == -1 and c.high >= stop:
                exit_px = stop * (1 + SLIPPAGE)
                notional = qty * exit_px
                equity = apply_fee(equity, notional)
                pnl = qty * (entry - exit_px)
                equity += pnl
                trades.append(Trade(entry_time, c.close_time_ms, "short", entry, exit_px, pnl, "stop"))
                pos = 0
                qty = 0.0
                stop = None
                hh = None
                ll = None

        # 2) 更新 trailing stop（在 bar close 更新，供下一根使用）
        if pos != 0:
            a = atr14[i]
            if a is not None:
                if pos == 1:
                    hh = c.high if hh is None else max(hh, c.high)
                    trail = hh - TRAIL_ATR * a
                    stop = trail if stop is None else max(stop, trail)
                else:
                    ll = c.low if ll is None else min(ll, c.low)
                    trail = ll + TRAIL_ATR * a
                    stop = trail if stop is None else min(stop, trail)

        # 3) 訊號：在 close i 產生，於 open i+1 執行（若已被 stop 打掉，pos=0）
        desired, direction, cnt = reversal_signal_at_close(candles, i)
        if desired == 0:
            continue

        if pos == desired:
            continue

        if pos != 0 and not FLIP_ON_SIGNAL:
            continue

        # 執行 flip/entry 於 next open
        open_px = n.open
        exec_px = open_px * (1 + SLIPPAGE) if desired == 1 else open_px * (1 - SLIPPAGE)
        t_ms = n.open_time_ms

        # close existing at open
        if pos != 0:
            # close at open
            exit_px = open_px * (1 - SLIPPAGE) if pos == 1 else open_px * (1 + SLIPPAGE)
            notional = qty * exit_px
            equity = apply_fee(equity, notional)
            pnl = qty * (exit_px - entry) if pos == 1 else qty * (entry - exit_px)
            equity += pnl
            trades.append(Trade(entry_time, t_ms, "long" if pos == 1 else "short", entry, exit_px, pnl, "flip"))
            pos = 0
            qty = 0.0
            stop = None
            hh = None
            ll = None

        if equity <= 0:
            break

        # open new
        notional = equity
        qty = notional / exec_px
        equity = apply_fee(equity, notional)
        pos = desired
        entry = exec_px
        entry_time = t_ms

        # init stop based on ATR at signal candle i
        a = atr14[i]
        if a is None:
            stop = None
        else:
            if pos == 1:
                stop = entry - SL_ATR * a
                hh = entry
                ll = None
            else:
                stop = entry + SL_ATR * a
                ll = entry
                hh = None

    # close at end
    if pos != 0:
        last = candles[-1]
        exit_px = last.close * (1 - SLIPPAGE) if pos == 1 else last.close * (1 + SLIPPAGE)
        notional = qty * exit_px
        equity = apply_fee(equity, notional)
        pnl = qty * (exit_px - entry) if pos == 1 else qty * (entry - exit_px)
        equity += pnl
        trades.append(Trade(entry_time, last.close_time_ms, "long" if pos == 1 else "short", entry, exit_px, pnl, "eod"))

    # stats
    ret = (equity / INITIAL_EQUITY - 1) * 100
    wins = [t for t in trades if t.pnl_usdt > 0]
    losses = [t for t in trades if t.pnl_usdt <= 0]
    win_rate = (len(wins) / len(trades) * 100) if trades else 0.0
    avg_pnl = (sum(t.pnl_usdt for t in trades) / len(trades)) if trades else 0.0

    gross_win = sum(t.pnl_usdt for t in wins)
    gross_loss = -sum(t.pnl_usdt for t in losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float('inf')

    return {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "start": utc_ms_to_str(candles[0].open_time_ms),
        "end": utc_ms_to_str(candles[-1].close_time_ms),
        "initial": INITIAL_EQUITY,
        "final": equity,
        "return_pct": ret,
        "trades": len(trades),
        "win_rate_pct": win_rate,
        "avg_pnl": avg_pnl,
        "max_drawdown_pct": max_dd * 100,
        "profit_factor": pf,
        "assumptions": {
            "taker_fee": TAKER_FEE,
            "slippage": SLIPPAGE,
            "min_consecutive": MIN_CONSECUTIVE,
            "atr_n": ATR_N,
            "sl_atr": SL_ATR,
            "trail_atr": TRAIL_ATR,
            "flip_on_signal": FLIP_ON_SIGNAL,
            "entry": "next_open",
            "stop_priority": "stop_first_conservative",
        },
    }, trades


def main():
    # 允許用環境變數快速調參，不影響既有通知/排程。
    global SL_ATR, TRAIL_ATR, FLIP_ON_SIGNAL, TAKER_FEE, SLIPPAGE
    try:
        SL_ATR = float(os.getenv('SL_ATR', str(SL_ATR)))
        TRAIL_ATR = float(os.getenv('TRAIL_ATR', str(TRAIL_ATR)))
        FLIP_ON_SIGNAL = os.getenv('FLIP_ON_SIGNAL', str(FLIP_ON_SIGNAL)).lower() in ('1','true','yes','y')
        TAKER_FEE = float(os.getenv('TAKER_FEE', str(TAKER_FEE)))
        SLIPPAGE = float(os.getenv('SLIPPAGE', str(SLIPPAGE)))
    except Exception:
        pass

    end = datetime.now(timezone.utc)
    candles = fetch_klines(ms(START_UTC), ms(end))
    if not candles:
        raise SystemExit("no candles")

    summary, trades = backtest(candles)
    print("=== Reversal + SL(ATR) + ATR Trailing Backtest ===")
    print(f"Symbol: {summary['symbol']}  Interval: {summary['interval']}")
    print(f"Period(UTC): {summary['start']} -> {summary['end']}")
    print(f"Initial: {summary['initial']:.2f}  Final: {summary['final']:.2f}")
    print(f"Return: {summary['return_pct']:.2f}%")
    print(
        f"Trades: {summary['trades']}  WinRate: {summary['win_rate_pct']:.1f}%  "
        f"PF: {summary['profit_factor']:.2f}  AvgPnL: {summary['avg_pnl']:.2f}"
    )
    print(f"MaxDD: {summary['max_drawdown_pct']:.2f}%")
    print(f"Assumptions: {summary['assumptions']}")

    print("\n=== Last 8 trades ===")
    for t in trades[-8:]:
        print(
            f"{utc_ms_to_str(t.entry_time_ms)} -> {utc_ms_to_str(t.exit_time_ms)} "
            f"{t.side:5s} {t.entry:.6f}->{t.exit:.6f} pnl={t.pnl_usdt:+.2f} reason={t.reason}"
        )


if __name__ == "__main__":
    main()
