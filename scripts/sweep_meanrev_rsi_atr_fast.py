#!/usr/bin/env python3
"""sweep_meanrev_rsi_atr_fast.py

更快的参数扫描（一次抓数据 + 一次算指标，然后批量回测）。

目标：
- 找到符合「每月 5~10 单」左右的参数组合
- 重点看 out-of-sample（后 30%）表现，避免只吃到单一时期运气

输出：
- Top N 组合（按 test return / PF / DD 排序）

⚠️ 研究工具，不保证盈利。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Dict, Tuple

import requests

from strategy_lab import Candle, rsi, sma, atr

SYMBOL = "币安人生USDT"
INTERVAL = "15m"
START_UTC = datetime(2025, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
BINANCE_FAPI = "https://fapi.binance.com"

INITIAL_EQUITY = 10_000.0
TAKER_FEE = 0.0004
SLIPPAGE = 0.0001

RSI_N = 14
SMA_N = 200
ATR_N = 14

# 不再限制每月单量；改以稳定性为主。
# 仍会输出 tpm（trades per month）供参考。
TOP_N = int(float(os.getenv("TOP_N", "30")))

# 每段/样本至少要有多少笔交易才纳入排名（避免样本太少导致 PF/ret 失真）
MIN_TEST_TRADES = int(float(os.getenv("MIN_TEST_TRADES", "5")))

# Grid（可以再加大；先从小网格开始）
RSI_LONG_GRID = [20, 22, 25, 28, 30, 32]
RSI_SHORT_GRID = [68, 70, 72, 75, 78, 80]
SL_ATR_GRID = [0.9, 1.1, 1.2, 1.3, 1.5]
TP_R_GRID = [1.2, 1.4, 1.6, 1.8, 2.2]
MAX_HOLD_GRID = [0, 96, 192]  # 0 / 1天 / 2天
COOLDOWN_GRID = [0, 8, 16]

# 盘整/低波动过滤（ATR%）候选（偏少单：从 0.6% 往上扫；过高会导致完全无候选）
MIN_ATR_PCT_GRID = [0.006, 0.008, 0.010, 0.012]


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
        time.sleep(0.15)

    m = {c.open_time_ms: c for c in out}
    return [m[k] for k in sorted(m.keys())]


@dataclass
class Trade:
    entry_time_ms: int
    exit_time_ms: int
    side: int  # 1 long, -1 short
    entry: float
    exit: float
    pnl: float


@dataclass
class Perf:
    ret_pct: float
    pf: float
    max_dd_pct: float
    trades: int
    win_rate_pct: float


def trades_per_month(trades: int, start_ms: int, end_ms: int) -> float:
    # 使用平均月长 30.4375 天
    months = max(1e-9, (end_ms - start_ms) / (1000 * 60 * 60 * 24 * 30.4375))
    return trades / months


def backtest_with_ind(
    candles: List[Candle],
    rsis: List[Optional[float]],
    smas: List[Optional[float]],
    atrs: List[Optional[float]],
    *,
    rsi_long: float,
    rsi_short: float,
    sl_atr: float,
    tp_r: float,
    max_hold_bars: int,
    cooldown_bars: int,
    min_atr_pct: float,
) -> Perf:
    equity = INITIAL_EQUITY
    peak = equity
    max_dd = 0.0

    pos = 0
    qty = 0.0
    entry = 0.0
    entry_i = 0
    sl = None  # type: Optional[float]
    tp = None  # type: Optional[float]

    last_sig_i = -10_000
    last_sig_side = 0

    wins = 0
    losses = 0
    gross_win = 0.0
    gross_loss = 0.0
    n_trades = 0

    def fee(notional: float) -> float:
        return notional * TAKER_FEE

    def mark_dd(price: float):
        nonlocal peak, max_dd
        cur = equity
        if pos != 0:
            if pos == 1:
                cur = equity + qty * (price - entry)
            else:
                cur = equity + qty * (entry - price)
        if cur > peak:
            peak = cur
        dd = (peak - cur) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    start_i = max(SMA_N, RSI_N, ATR_N) + 2

    for i in range(start_i, len(candles) - 1):
        c = candles[i]
        n = candles[i + 1]

        mark_dd(c.close)

        # manage position intrabar: SL first then TP
        if pos != 0 and sl is not None and tp is not None:
            exit_px = None

            if pos == 1:
                if c.low <= sl:
                    exit_px = sl * (1 - SLIPPAGE)
                elif c.high >= tp:
                    exit_px = tp * (1 - SLIPPAGE)
            else:
                if c.high >= sl:
                    exit_px = sl * (1 + SLIPPAGE)
                elif c.low <= tp:
                    exit_px = tp * (1 + SLIPPAGE)

            # time stop at close
            if exit_px is None and max_hold_bars > 0 and (i - entry_i) >= max_hold_bars:
                exit_px = c.close * (1 - SLIPPAGE) if pos == 1 else c.close * (1 + SLIPPAGE)

            if exit_px is not None:
                notional = qty * exit_px
                equity -= fee(notional)
                pnl = qty * (exit_px - entry) if pos == 1 else qty * (entry - exit_px)
                equity += pnl

                n_trades += 1
                if pnl > 0:
                    wins += 1
                    gross_win += pnl
                else:
                    losses += 1
                    gross_loss += -pnl

                pos = 0
                qty = 0.0
                entry = 0.0
                sl = None
                tp = None

        if pos != 0:
            continue

        r = rsis[i]
        s = smas[i]
        a = atrs[i]
        if r is None or s is None or a is None or a <= 0:
            continue

        price = c.close
        # 低波动过滤
        if (a / price) < min_atr_pct:
            continue

        allow_long = price >= s
        allow_short = price < s

        desired = 0
        if allow_long and r <= rsi_long:
            desired = 1
        elif allow_short and r >= rsi_short:
            desired = -1
        else:
            continue

        if cooldown_bars > 0 and desired == last_sig_side and (i - last_sig_i) < cooldown_bars:
            continue

        # execute at next open
        open_px = n.open
        exec_px = open_px * (1 + SLIPPAGE) if desired == 1 else open_px * (1 - SLIPPAGE)
        if equity <= 0:
            break

        qty = equity / exec_px
        notional = qty * exec_px
        equity -= fee(notional)

        pos = desired
        entry = exec_px
        entry_i = i + 1

        risk = sl_atr * a
        if pos == 1:
            sl = entry - risk
            tp = entry + tp_r * risk
        else:
            sl = entry + risk
            tp = entry - tp_r * risk

        last_sig_i = i
        last_sig_side = desired

    # close at end
    if pos != 0:
        last = candles[-1]
        exit_px = last.close * (1 - SLIPPAGE) if pos == 1 else last.close * (1 + SLIPPAGE)
        notional = qty * exit_px
        equity -= fee(notional)
        pnl = qty * (exit_px - entry) if pos == 1 else qty * (entry - exit_px)
        equity += pnl

        n_trades += 1
        if pnl > 0:
            wins += 1
            gross_win += pnl
        else:
            losses += 1
            gross_loss += -pnl

    ret_pct = (equity / INITIAL_EQUITY - 1) * 100
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    win_rate = (wins / n_trades * 100) if n_trades else 0.0

    return Perf(ret_pct=ret_pct, pf=pf, max_dd_pct=max_dd * 100, trades=n_trades, win_rate_pct=win_rate)


def main():
    end = datetime.now(timezone.utc)
    candles = fetch_klines(ms(START_UTC), ms(end))
    if len(candles) < 1200:
        raise SystemExit(f"not enough candles: {len(candles)}")

    closes = [c.close for c in candles]
    rsis = rsi(closes, RSI_N)
    smas = sma(closes, SMA_N)
    atrs = atr(candles, ATR_N)

    split = int(len(candles) * 0.7)
    train = candles[:split]
    test = candles[split:]

    rsis_tr = rsis[:split]
    smas_tr = smas[:split]
    atrs_tr = atrs[:split]

    rsis_te = rsis[split:]
    smas_te = smas[split:]
    atrs_te = atrs[split:]

    rows: List[dict] = []

    t0 = time.time()
    it = 0
    for rsi_long in RSI_LONG_GRID:
        for rsi_short in RSI_SHORT_GRID:
            if rsi_short <= 60:
                continue
            for sl_atr in SL_ATR_GRID:
                for tp_r in TP_R_GRID:
                    for max_hold in MAX_HOLD_GRID:
                        for cooldown in COOLDOWN_GRID:
                            it += 1
                            # 扫低波动过滤阈值（偏少单）
                            for min_atr_pct in MIN_ATR_PCT_GRID:
                                p_tr = backtest_with_ind(
                                    train, rsis_tr, smas_tr, atrs_tr,
                                    rsi_long=rsi_long, rsi_short=rsi_short,
                                    sl_atr=sl_atr, tp_r=tp_r,
                                    max_hold_bars=max_hold, cooldown_bars=cooldown,
                                    min_atr_pct=min_atr_pct,
                                )
                                p_te = backtest_with_ind(
                                    test, rsis_te, smas_te, atrs_te,
                                    rsi_long=rsi_long, rsi_short=rsi_short,
                                    sl_atr=sl_atr, tp_r=tp_r,
                                    max_hold_bars=max_hold, cooldown_bars=cooldown,
                                    min_atr_pct=min_atr_pct,
                                )

                                # trades/month (用 test 时间跨度估算)
                                tpm = trades_per_month(
                                    p_te.trades,
                                    test[0].open_time_ms,
                                    test[-1].close_time_ms,
                                )

                                    # hard filters（稳定性为主）
                                if p_te.trades < MIN_TEST_TRADES:
                                    continue
                                if p_te.pf < 1.15:
                                    continue
                                if p_te.max_dd_pct > 25:
                                    continue

                                # anti-overfit: 训练期别太烂（避免 test 只是碰巧）
                                if p_tr.ret_pct < -5.0:
                                    continue
                                if p_tr.pf < 0.95:
                                    continue

                                rows.append(
                                    {
                                        "test_ret": p_te.ret_pct,
                                        "test_pf": p_te.pf,
                                        "test_dd": p_te.max_dd_pct,
                                        "test_trades": p_te.trades,
                                        "test_tpm": tpm,
                                        "train_ret": p_tr.ret_pct,
                                        "train_pf": p_tr.pf,
                                        "train_dd": p_tr.max_dd_pct,
                                        "params": {
                                            "RSI_LONG": rsi_long,
                                            "RSI_SHORT": rsi_short,
                                            "SL_ATR": sl_atr,
                                            "TP_R": tp_r,
                                            "MAX_HOLD_BARS": max_hold,
                                            "COOLDOWN_BARS": cooldown,
                                            "MIN_ATR_PCT": min_atr_pct,
                                        },
                                    }
                                )

    dt = time.time() - t0

    # 排序偏好：优先看 test 表现，但也要求 train 不要太差（防止纯运气）
    rows.sort(
        key=lambda x: (
            x["test_ret"],
            x["test_pf"],
            x["train_ret"],
            x["train_pf"],
            -x["test_dd"],
            x["test_tpm"],
        ),
        reverse=True,
    )

    print(f"Symbol={SYMBOL} Interval={INTERVAL} Bars={len(candles)} split=70/30")
    print(f"Assumptions: taker_fee={TAKER_FEE*100:.3f}% slippage={SLIPPAGE*100:.3f}%")
    print(f"Grid iters={it}  candidates={len(rows)}  elapsed={dt:.2f}s")
    print(f"Filters: MIN_TEST_TRADES={MIN_TEST_TRADES}  TopN={TOP_N}\n")

    for r in rows[:TOP_N]:
        p = r["params"]
        print(
            f"TEST ret={r['test_ret']:>7.2f}% pf={r['test_pf']:.2f} dd={r['test_dd']:.2f}% "
            f"tpm={r['test_tpm']:.1f} trades={r['test_trades']:>3d} | "
            f"TRAIN ret={r['train_ret']:>7.2f}% pf={r['train_pf']:.2f} dd={r['train_dd']:.2f}% | "
            f"RSI_L={p['RSI_LONG']} RSI_S={p['RSI_SHORT']} SL_ATR={p['SL_ATR']} TP_R={p['TP_R']} "
            f"MIN_ATR_PCT={p['MIN_ATR_PCT']} MAX_HOLD={p['MAX_HOLD_BARS']} CD={p['COOLDOWN_BARS']}"
        )


if __name__ == "__main__":
    main()
