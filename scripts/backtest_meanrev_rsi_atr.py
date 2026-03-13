#!/usr/bin/env python3
"""backtest_meanrev_rsi_atr.py

回测：币安人生USDT 15m
策略：RSI 均值回归 + SMA200 趋势过滤 + ATR 止损/止盈 + 可选 time stop

目标：
- 输出是否“看起来”有优势（并非保证）
- 统计交易次数（含每月单量），方便调参到 5~10 单/月

假设：
- 仅 1x 名目（不考虑资金费率/爆仓/盘口深度）
- 手续费 taker + 简单滑点
- 入场：信号在 close(i) 触发，下一根 open(i+1) 成交
- 出场：同一根内，保守先看止损再看止盈；否则 time stop 或到期收盘

⚠️ 回测不代表未来绩效。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Dict, Tuple

import requests

from strategy_lab import Candle, rsi, sma, atr, utc_ms_to_str

SYMBOL = "币安人生USDT"
INTERVAL = "15m"
START_UTC = datetime(2025, 10, 20, 0, 0, 0, tzinfo=timezone.utc)  # 依据实际上市时间附近

BINANCE_FAPI = "https://fapi.binance.com"

# ------- Defaults (可用环境变量覆盖) -------
INITIAL_EQUITY = float(os.getenv("INITIAL_EQUITY", "10000"))
TAKER_FEE = float(os.getenv("TAKER_FEE", "0.0004"))
SLIPPAGE = float(os.getenv("SLIPPAGE", "0.0001"))

RSI_N = int(float(os.getenv("RSI_N", "14")))
SMA_N = int(float(os.getenv("SMA_N", "200")))
ATR_N = int(float(os.getenv("ATR_N", "14")))

RSI_LONG = float(os.getenv("RSI_LONG", "30"))
RSI_SHORT = float(os.getenv("RSI_SHORT", "70"))

SL_ATR = float(os.getenv("SL_ATR", "1.2"))
TP_R = float(os.getenv("TP_R", "1.6"))

# time stop：最大持有 K 数（0=不启用）。15m * 96 = 1天
MAX_HOLD_BARS = int(float(os.getenv("MAX_HOLD_BARS", "0")))

# 交易频率控制（可选）：信号冷却 bars（0=不启用）
COOLDOWN_BARS = int(float(os.getenv("COOLDOWN_BARS", "0")))

# 盘整/低波动过滤（ATR%）
# 只有当 ATR_N / close >= MIN_ATR_PCT 才允许进场。
MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.012"))

# 槓桿/倉位（隔離、固定風險）
LEVERAGE = int(float(os.getenv("LEVERAGE", "20")))
RISK_PCT = float(os.getenv("RISK_PCT", "0.01"))          # 每單風險（打到 SL）佔 equity 比例
MAX_MARGIN_PCT = float(os.getenv("MAX_MARGIN_PCT", "0.20"))  # 隔離保證金最多用 equity 的比例（貼近你說的名目 1000~4000U）
LIQ_BUFFER_PCT = float(os.getenv("LIQ_BUFFER_PCT", "0.005")) # 距離預估爆倉價緩衝


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


@dataclass
class Trade:
    entry_time_ms: int
    exit_time_ms: int
    side: str  # long|short
    entry: float
    exit: float
    pnl_usdt: float
    reason: str


def backtest(candles: List[Candle]) -> Tuple[dict, List[Trade]]:
    closes = [c.close for c in candles]
    rsis = rsi(closes, RSI_N)
    smas = sma(closes, SMA_N)
    atrs = atr(candles, ATR_N)

    equity = INITIAL_EQUITY
    peak = equity
    max_dd = 0.0

    pos = 0  # 0 flat, 1 long, -1 short
    qty = 0.0
    entry = 0.0
    entry_i = 0
    entry_t = 0
    sl = None  # type: Optional[float]
    tp = None  # type: Optional[float]

    last_signal_i = -10_000
    last_signal_side = 0

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
        dd = (peak - cur) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    def fee(notional: float) -> float:
        return notional * TAKER_FEE

    for i in range(max(SMA_N, RSI_N, ATR_N) + 2, len(candles) - 1):
        c = candles[i]
        n = candles[i + 1]

        mark_to_market(c.close)

        # ----- Manage open position (intrabar conservative: SL first, then TP) -----
        if pos != 0 and sl is not None and tp is not None:
            exit_px = None
            reason = None

            if pos == 1:
                if c.low <= sl:
                    exit_px = sl * (1 - SLIPPAGE)
                    reason = "stop"
                elif c.high >= tp:
                    exit_px = tp * (1 - SLIPPAGE)
                    reason = "tp"
            else:
                if c.high >= sl:
                    exit_px = sl * (1 + SLIPPAGE)
                    reason = "stop"
                elif c.low <= tp:
                    exit_px = tp * (1 + SLIPPAGE)
                    reason = "tp"

            # time stop
            if exit_px is None and MAX_HOLD_BARS > 0 and (i - entry_i) >= MAX_HOLD_BARS:
                exit_px = c.close * (1 - SLIPPAGE) if pos == 1 else c.close * (1 + SLIPPAGE)
                reason = "time"

            if exit_px is not None:
                notional = qty * exit_px
                equity -= fee(notional)
                pnl = qty * (exit_px - entry) if pos == 1 else qty * (entry - exit_px)
                equity += pnl
                trades.append(Trade(entry_t, c.close_time_ms, "long" if pos == 1 else "short", entry, exit_px, pnl, reason))
                pos = 0
                qty = 0.0
                entry = 0.0
                sl = None
                tp = None

        # ----- Entry signal at close(i), execute at open(i+1) -----
        if pos != 0:
            continue

        r = rsis[i]
        s = smas[i]
        a = atrs[i]
        if r is None or s is None or a is None or a <= 0:
            continue

        price = c.close

        # 低波动过滤（避免盘整乱单）
        atr_pct = a / price
        if atr_pct < MIN_ATR_PCT:
            continue

        allow_long = price >= s
        allow_short = price < s

        desired = 0
        if allow_long and r <= RSI_LONG:
            desired = 1
        elif allow_short and r >= RSI_SHORT:
            desired = -1
        else:
            continue

        # cooldown
        if COOLDOWN_BARS > 0 and desired == last_signal_side and (i - last_signal_i) < COOLDOWN_BARS:
            continue

        # execute at next open with slippage
        open_px = n.open
        exec_px = open_px * (1 + SLIPPAGE) if desired == 1 else open_px * (1 - SLIPPAGE)

        if equity <= 0:
            break

        risk = SL_ATR * a

        # 先算 SL/TP
        if desired == 1:
            sl = exec_px - risk
            tp = exec_px + TP_R * risk
            liq = exec_px * (1.0 - 1.0 / max(1.0, float(LEVERAGE)))
            if sl <= liq * (1.0 + LIQ_BUFFER_PCT):
                continue
        else:
            sl = exec_px + risk
            tp = exec_px - TP_R * risk
            liq = exec_px * (1.0 + 1.0 / max(1.0, float(LEVERAGE)))
            if sl >= liq * (1.0 - LIQ_BUFFER_PCT):
                continue

        # 固定風險倉位：risk_usdt = equity * RISK_PCT
        stop_dist = abs(exec_px - sl)
        if stop_dist <= 0:
            continue
        risk_usdt = equity * RISK_PCT
        qty = risk_usdt / stop_dist

        # 隔離保證金上限：margin = notional/LEV <= equity * MAX_MARGIN_PCT
        max_notional = equity * MAX_MARGIN_PCT * LEVERAGE
        notional = qty * exec_px
        if notional > max_notional and max_notional > 0:
            notional = max_notional
            qty = notional / exec_px

        if qty <= 0:
            continue

        # 手續費按成交名目收
        equity -= fee(qty * exec_px)

        pos = desired
        entry = exec_px
        entry_i = i + 1
        entry_t = n.open_time_ms

        last_signal_i = i
        last_signal_side = desired

    # close at end
    if pos != 0:
        last = candles[-1]
        exit_px = last.close * (1 - SLIPPAGE) if pos == 1 else last.close * (1 + SLIPPAGE)
        notional = qty * exit_px
        equity -= fee(notional)
        pnl = qty * (exit_px - entry) if pos == 1 else qty * (entry - exit_px)
        equity += pnl
        trades.append(Trade(entry_t, last.close_time_ms, "long" if pos == 1 else "short", entry, exit_px, pnl, "eod"))

    ret = (equity / INITIAL_EQUITY - 1) * 100
    wins = [t for t in trades if t.pnl_usdt > 0]
    losses = [t for t in trades if t.pnl_usdt <= 0]
    win_rate = (len(wins) / len(trades) * 100) if trades else 0.0
    avg_pnl = (sum(t.pnl_usdt for t in trades) / len(trades)) if trades else 0.0

    gross_win = sum(t.pnl_usdt for t in wins)
    gross_loss = -sum(t.pnl_usdt for t in losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    summary = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "period_utc": f"{utc_ms_to_str(candles[0].open_time_ms)} -> {utc_ms_to_str(candles[-1].close_time_ms)}",
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
            "rsi_n": RSI_N,
            "sma_n": SMA_N,
            "atr_n": ATR_N,
            "rsi_long": RSI_LONG,
            "rsi_short": RSI_SHORT,
            "sl_atr": SL_ATR,
            "tp_r": TP_R,
            "max_hold_bars": MAX_HOLD_BARS,
            "cooldown_bars": COOLDOWN_BARS,
            "min_atr_pct": MIN_ATR_PCT,
            "leverage": LEVERAGE,
            "risk_pct": RISK_PCT,
            "max_margin_pct": MAX_MARGIN_PCT,
            "liq_buffer_pct": LIQ_BUFFER_PCT,
        },
    }

    return summary, trades


def month_key(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}"


def main():
    end = datetime.now(timezone.utc)
    candles = fetch_klines(ms(START_UTC), ms(end))
    if not candles:
        raise SystemExit("no candles")

    summary, trades = backtest(candles)

    print("=== MeanRev RSI + SMA Filter + ATR SL/TP Backtest ===")
    for k, v in summary.items():
        if k != "assumptions":
            print(f"{k}: {v}")
    print(f"assumptions: {summary['assumptions']}")

    # trades per month
    m: Dict[str, int] = {}
    for t in trades:
        m[month_key(t.entry_time_ms)] = m.get(month_key(t.entry_time_ms), 0) + 1

    print("\n=== Trades per month (entry count) ===")
    for mk in sorted(m.keys()):
        print(f"{mk}: {m[mk]}")

    print("\n=== Last 10 trades ===")
    for t in trades[-10:]:
        print(
            f"{utc_ms_to_str(t.entry_time_ms)} -> {utc_ms_to_str(t.exit_time_ms)} "
            f"{t.side:5s} {t.entry:.6f}->{t.exit:.6f} pnl={t.pnl_usdt:+.2f} reason={t.reason}"
        )


if __name__ == "__main__":
    main()
