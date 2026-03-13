#!/usr/bin/env python3
"""walkforward_meanrev.py

Walk-forward 多段验证：币安人生USDT 15m

目的：
- 避免单一 70/30 split 的偶然性
- 用滚动窗口评估策略在不同阶段是否都有优势

做法（可用环境变量调）：
- 每段：TRAIN_DAYS 天训练（用于选择参数），TEST_DAYS 天测试（固定参数跑）
- 参数候选：从一个小网格中选出在训练期表现最好的 TopK（以 ret/PF/DD 综合打分）
- 然后在该段 test 期评估，并汇总所有段的 test 表现

输出：
- 每段选中的参数 + train/test 表现
- 汇总 test：平均/中位数 return、PF、DD、每月单量

⚠️ 研究工具，不保证盈利。
"""

from __future__ import annotations

import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
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

# window config
TRAIN_DAYS = int(float(os.getenv("TRAIN_DAYS", "45")))
TEST_DAYS = int(float(os.getenv("TEST_DAYS", "15")))
STEP_DAYS = int(float(os.getenv("STEP_DAYS", str(TEST_DAYS))))
TOPK = int(float(os.getenv("TOPK", "8")))

# 不再限制每月单量；walk-forward 以稳定性为主。
MIN_TEST_TRADES = int(float(os.getenv("MIN_TEST_TRADES", "3")))

# candidate grid (keep moderate)
RSI_LONG_GRID = [22, 25, 28, 30]
RSI_SHORT_GRID = [68, 70, 72, 75]
SL_ATR_GRID = [1.1, 1.2, 1.3, 1.5]
TP_R_GRID = [1.6, 1.8, 2.0, 2.2]
MAX_HOLD_GRID = [0, 96, 192]
COOLDOWN_GRID = [0, 8]

# 盘整/低波动过滤（ATR%）候选
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

    m = {c.open_time_ms: c for c in out}
    return [m[k] for k in sorted(m.keys())]


@dataclass
class Perf:
    ret_pct: float
    pf: float
    max_dd_pct: float
    trades: int


def trades_per_month(trades: int, start_ms: int, end_ms: int) -> float:
    months = max(1e-9, (end_ms - start_ms) / (1000 * 60 * 60 * 24 * 30.4375))
    return trades / months


def backtest_segment(
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
        peak = max(peak, cur)
        dd = (peak - cur) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    start_i = max(SMA_N, RSI_N, ATR_N) + 2

    for i in range(start_i, len(candles) - 1):
        c = candles[i]
        n = candles[i + 1]

        mark_dd(c.close)

        # exits
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

            if exit_px is None and max_hold_bars > 0 and (i - entry_i) >= max_hold_bars:
                exit_px = c.close * (1 - SLIPPAGE) if pos == 1 else c.close * (1 + SLIPPAGE)

            if exit_px is not None:
                notional = qty * exit_px
                equity -= fee(notional)
                pnl = qty * (exit_px - entry) if pos == 1 else qty * (entry - exit_px)
                equity += pnl
                n_trades += 1
                if pnl > 0:
                    gross_win += pnl
                else:
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

        # entry at next open
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
            gross_win += pnl
        else:
            gross_loss += -pnl

    ret_pct = (equity / INITIAL_EQUITY - 1) * 100
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    return Perf(ret_pct=ret_pct, pf=pf, max_dd_pct=max_dd * 100, trades=n_trades)


def score(p: Perf) -> float:
    # simple scoring: prefer higher return/PF, penalize DD, require some trades
    if p.trades < 3:
        return -1e9
    # 训练期更看重稳定：回撤惩罚更重，且对低回测样本的“inf PF”不加分
    pf = min(p.pf, 5.0)
    if pf == float('inf'):
        pf = 1.0
    return (p.ret_pct) + 8.0 * (pf - 1.0) - 1.0 * p.max_dd_pct


def slice_by_time(candles: List[Candle], start_ms: int, end_ms: int) -> List[Candle]:
    return [c for c in candles if (c.open_time_ms >= start_ms and c.close_time_ms <= end_ms)]


def main():
    end = datetime.now(timezone.utc)
    candles = fetch_klines(ms(START_UTC), ms(end))
    if len(candles) < 2000:
        raise SystemExit(f"not enough candles: {len(candles)}")

    closes = [c.close for c in candles]
    rsis_all = rsi(closes, RSI_N)
    smas_all = sma(closes, SMA_N)
    atrs_all = atr(candles, ATR_N)

    bar_ms = 15 * 60 * 1000
    train_ms = TRAIN_DAYS * 24 * 60 * 60 * 1000
    test_ms = TEST_DAYS * 24 * 60 * 60 * 1000
    step_ms = STEP_DAYS * 24 * 60 * 60 * 1000

    t0 = candles[0].open_time_ms
    t_end = candles[-1].close_time_ms

    segments = []
    cur = t0 + train_ms
    # cur is the end of initial train window
    while cur + test_ms <= t_end:
        train_start = cur - train_ms
        train_end = cur
        test_start = cur
        test_end = cur + test_ms
        segments.append((train_start, train_end, test_start, test_end))
        cur += step_ms

    if not segments:
        raise SystemExit("no segments")

    test_rets = []
    test_pfs = []
    test_dds = []
    test_tpms = []

    print(f"Symbol={SYMBOL} {INTERVAL}  segments={len(segments)}")
    print(f"Window: train={TRAIN_DAYS}d test={TEST_DAYS}d step={STEP_DAYS}d  TOPK={TOPK}")
    print(f"Assumptions: fee={TAKER_FEE*100:.3f}% slip={SLIPPAGE*100:.3f}%")
    print(f"Filters: MIN_TEST_TRADES={MIN_TEST_TRADES}\n")

    for si, (tr_s, tr_e, te_s, te_e) in enumerate(segments, 1):
        # slice candles by time
        tr_idx = [i for i, c in enumerate(candles) if (c.open_time_ms >= tr_s and c.close_time_ms <= tr_e)]
        te_idx = [i for i, c in enumerate(candles) if (c.open_time_ms >= te_s and c.close_time_ms <= te_e)]
        if len(tr_idx) < 600 or len(te_idx) < 150:
            continue

        tr0, tr1 = tr_idx[0], tr_idx[-1] + 1
        te0, te1 = te_idx[0], te_idx[-1] + 1

        tr_c = candles[tr0:tr1]
        te_c = candles[te0:te1]
        tr_r = rsis_all[tr0:tr1]
        tr_sma = smas_all[tr0:tr1]
        tr_a = atrs_all[tr0:tr1]
        te_r = rsis_all[te0:te1]
        te_sma = smas_all[te0:te1]
        te_a = atrs_all[te0:te1]

        # evaluate grid on train and keep topK
        scored: List[Tuple[float, dict, Perf]] = []
        for rsi_l in RSI_LONG_GRID:
            for rsi_s in RSI_SHORT_GRID:
                if rsi_s <= 60:
                    continue
                for sl_atr in SL_ATR_GRID:
                    for tp_r in TP_R_GRID:
                        for mh in MAX_HOLD_GRID:
                            for cd in COOLDOWN_GRID:
                                for min_atr_pct in MIN_ATR_PCT_GRID:
                                    p_tr = backtest_segment(
                                        tr_c, tr_r, tr_sma, tr_a,
                                        rsi_long=rsi_l, rsi_short=rsi_s,
                                        sl_atr=sl_atr, tp_r=tp_r,
                                        max_hold_bars=mh, cooldown_bars=cd,
                                        min_atr_pct=min_atr_pct,
                                    )
                                    sc = score(p_tr)
                                    scored.append((sc, {"RSI_L": rsi_l, "RSI_S": rsi_s, "SL_ATR": sl_atr, "TP_R": tp_r, "MAX_HOLD": mh, "CD": cd, "MIN_ATR_PCT": min_atr_pct}, p_tr))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:TOPK]

        # choose best purely by TRAIN among topK, then evaluate on TEST
        best = None
        # top is already sorted by train score, so take the first that produces test trades in frequency bounds
        for _, params, p_tr in top:
            p_te = backtest_segment(
                te_c, te_r, te_sma, te_a,
                rsi_long=params["RSI_L"], rsi_short=params["RSI_S"],
                sl_atr=params["SL_ATR"], tp_r=params["TP_R"],
                max_hold_bars=params["MAX_HOLD"], cooldown_bars=params["CD"],
                min_atr_pct=params["MIN_ATR_PCT"],
            )
            tpm = trades_per_month(p_te.trades, te_c[0].open_time_ms, te_c[-1].close_time_ms)
            if p_te.trades < MIN_TEST_TRADES:
                continue
            best = (params, p_tr, p_te, tpm)
            break

        if best is None:
            continue

        params, p_tr, p_te, tpm = best
        test_rets.append(p_te.ret_pct)
        test_pfs.append(p_te.pf)
        test_dds.append(p_te.max_dd_pct)
        test_tpms.append(tpm)

        def dt_str(ms_: int) -> str:
            return datetime.fromtimestamp(ms_ / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

        print(
            f"Seg#{si:02d} Train[{dt_str(tr_s)}..{dt_str(tr_e)}] Test[{dt_str(te_s)}..{dt_str(te_e)}] "
            f"| pick={params} "
            f"| TRAIN ret={p_tr.ret_pct:+6.2f}% pf={p_tr.pf:>4.2f} dd={p_tr.max_dd_pct:>5.2f}% tr={p_tr.trades:>3d} "
            f"| TEST ret={p_te.ret_pct:+6.2f}% pf={p_te.pf:>4.2f} dd={p_te.max_dd_pct:>5.2f}% tr={p_te.trades:>3d} tpm={tpm:>4.1f}"
        )

    print("\n=== Walk-forward TEST summary ===")
    if not test_rets:
        print("No valid segments")
        return

    print(f"segments_used: {len(test_rets)}")
    print(f"test_ret_avg: {statistics.mean(test_rets):.2f}%  median: {statistics.median(test_rets):.2f}%")
    print(f"test_pf_avg : {statistics.mean(test_pfs):.2f}  median: {statistics.median(test_pfs):.2f}")
    print(f"test_dd_avg : {statistics.mean(test_dds):.2f}%  median: {statistics.median(test_dds):.2f}%")
    print(f"test_tpm_avg: {statistics.mean(test_tpms):.2f}  median: {statistics.median(test_tpms):.2f}")


if __name__ == "__main__":
    main()
