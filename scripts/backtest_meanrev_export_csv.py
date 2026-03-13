#!/usr/bin/env python3
"""backtest_meanrev_export_csv.py

長期回測 + 匯出每筆交易明細 CSV（含 MFE/MAE、每筆 trade 的 maxDD 指標）

策略：RSI 均值回歸 + SMA200 趨勢濾網 + ATR 止損止盈 + ATR% 盤整濾網
參數預設與訊號腳本一致（可用 env 覆蓋）

輸出：
- trades.csv：每筆交易明細（entry/exit/side/pnl、MAE/MFE、R、持倉時間）
- equity.csv：逐根K 的 equity 曲線與 running drawdown

可選：
- 直接用 openclaw 發到 Telegram：設定 SEND_TG=1

例：
  python3 backtest_meanrev_export_csv.py
  START_UTC=2025-01-01 SEND_TG=1 python3 backtest_meanrev_export_csv.py

⚠️ 研究用途，不構成投資建議。
"""

from __future__ import annotations

import csv
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import requests

from strategy_lab import Candle, rsi, sma, atr, utc_ms_to_str

SYMBOL = os.getenv("SYMBOL", "币安人生USDT")
INTERVAL = os.getenv("INTERVAL", "15m")

# default start (ISO like 2025-01-01)
START_UTC_STR = os.getenv("START_UTC", "2025-10-20")

INITIAL_EQUITY = float(os.getenv("INITIAL_EQUITY", "10000"))
TAKER_FEE = float(os.getenv("TAKER_FEE", "0.0004"))
SLIPPAGE = float(os.getenv("SLIPPAGE", "0.0001"))

RSI_N = int(float(os.getenv("RSI_N", "14")))
SMA_N = int(float(os.getenv("SMA_N", "200")))
ATR_N = int(float(os.getenv("ATR_N", "14")))

RSI_LONG = float(os.getenv("RSI_LONG", "30"))
RSI_SHORT = float(os.getenv("RSI_SHORT", "70"))

SL_ATR = float(os.getenv("SL_ATR", "1.3"))
TP_R = float(os.getenv("TP_R", "2.2"))

MAX_HOLD_BARS = int(float(os.getenv("MAX_HOLD_BARS", "0")))
COOLDOWN_BARS = int(float(os.getenv("COOLDOWN_BARS", "0")))

MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.010"))

# 槓桿/倉位（隔離、固定風險）
LEVERAGE = int(float(os.getenv("LEVERAGE", "20")))
RISK_PCT = float(os.getenv("RISK_PCT", "0.01"))
MAX_MARGIN_PCT = float(os.getenv("MAX_MARGIN_PCT", "0.20"))
LIQ_BUFFER_PCT = float(os.getenv("LIQ_BUFFER_PCT", "0.005"))

OUT_DIR = os.getenv("OUT_DIR", "/home/mark/.openclaw/workspace/exports")

SEND_TG = os.getenv("SEND_TG", "0") in ("1", "true", "yes", "y")
TELEGRAM_TARGET = os.getenv("TELEGRAM_TARGET", "-5170271645")
OPENCLAW_BIN = os.getenv("OPENCLAW_BIN", "/home/mark/.npm-global/bin/openclaw")

BINANCE_FAPI = "https://fapi.binance.com"


def parse_start(s: str) -> datetime:
    # supports YYYY-MM-DD
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        # fallback
        y, m, d = [int(x) for x in s.split("-")]
        return datetime(y, m, d, 0, 0, 0, tzinfo=timezone.utc)


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
class TradeRow:
    entry_time_ms: int
    exit_time_ms: int
    side: str
    entry: float
    exit: float
    pnl_usdt: float
    ret_pct: float
    bars_held: int
    fee_paid: float

    # risk/exchange assumptions
    leverage: int
    margin_used: float
    notional: float
    liq_est: float

    # excursion
    mae_pct: float
    mfe_pct: float

    # risk metrics
    r_multiple: float

    # trade-level drawdown: peak->trough within holding window, as pct of peak equity
    trade_maxdd_pct: float


def backtest_export(candles: List[Candle]) -> Tuple[dict, List[TradeRow], List[dict]]:
    closes = [c.close for c in candles]
    rsis = rsi(closes, RSI_N)
    smas = sma(closes, SMA_N)
    atrs = atr(candles, ATR_N)

    equity = INITIAL_EQUITY
    equity_peak = equity
    max_dd = 0.0

    # position state
    pos = 0
    qty = 0.0
    entry = 0.0
    entry_i = 0
    entry_t = 0
    sl = None  # type: Optional[float]
    tp = None  # type: Optional[float]
    risk_dist = 0.0
    liq = None  # type: Optional[float]

    last_sig_i = -10_000
    last_sig_side = 0

    # for export
    trades: List[TradeRow] = []
    equity_curve: List[dict] = []

    # for trade-level maxdd
    trade_peak_equity = None  # type: Optional[float]
    trade_trough_equity = None  # type: Optional[float]

    def fee(notional: float) -> float:
        return notional * TAKER_FEE

    def mark_to_market(price: float) -> float:
        if pos == 0:
            return equity
        if pos == 1:
            return equity + qty * (price - entry)
        return equity + qty * (entry - price)

    start_i = max(SMA_N, RSI_N, ATR_N) + 2

    for i in range(start_i, len(candles) - 1):
        c = candles[i]
        n = candles[i + 1]

        # update equity curve at close
        mtm = mark_to_market(c.close)
        nonlocal_dd = 0.0
        if mtm > equity_peak:
            equity_peak = mtm
        nonlocal_dd = (equity_peak - mtm) / equity_peak if equity_peak > 0 else 0.0
        if nonlocal_dd > max_dd:
            max_dd = nonlocal_dd

        equity_curve.append(
            {
                "時間(UTC)": utc_ms_to_str(c.close_time_ms),
                "收盤價": f"{c.close:.6f}",
                "權益(含浮盈,USDT)": f"{mtm:.2f}",
                "權益峰值(USDT)": f"{equity_peak:.2f}",
                "回撤(%)": f"{nonlocal_dd*100:.4f}",
            }
        )

        # manage trade-level maxdd trackers
        if pos != 0:
            if trade_peak_equity is None:
                trade_peak_equity = mtm
                trade_trough_equity = mtm
            else:
                trade_peak_equity = max(trade_peak_equity, mtm)
                trade_trough_equity = min(trade_trough_equity, mtm)

        # exits (intrabar conservative)
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

            if exit_px is None and MAX_HOLD_BARS > 0 and (i - entry_i) >= MAX_HOLD_BARS:
                exit_px = c.close * (1 - SLIPPAGE) if pos == 1 else c.close * (1 + SLIPPAGE)
                reason = "time"

            if exit_px is not None:
                notional = qty * exit_px
                fee_exit = fee(notional)
                equity -= fee_exit
                pnl = qty * (exit_px - entry) if pos == 1 else qty * (entry - exit_px)
                equity += pnl

                # trade-level dd
                if trade_peak_equity is None or trade_trough_equity is None or trade_peak_equity <= 0:
                    trade_dd_pct = 0.0
                else:
                    trade_dd_pct = (trade_peak_equity - trade_trough_equity) / trade_peak_equity * 100

                # excursions within holding window
                window = candles[entry_i : i + 1]
                if pos == 1:
                    worst = min(x.low for x in window)
                    best = max(x.high for x in window)
                    mae = (worst - entry) / entry * 100
                    mfe = (best - entry) / entry * 100
                else:
                    worst = max(x.high for x in window)  # adverse for short
                    best = min(x.low for x in window)    # favorable for short
                    mae = (entry - worst) / entry * 100
                    mfe = (entry - best) / entry * 100

                # R multiple
                r_mult = (pnl / (qty * risk_dist)) if (qty > 0 and risk_dist > 0) else 0.0

                ret_pct = pnl / (equity - pnl) * 100 if (equity - pnl) != 0 else 0.0

                trades.append(
                    TradeRow(
                        entry_time_ms=entry_t,
                        exit_time_ms=c.close_time_ms,
                        side="long" if pos == 1 else "short",
                        entry=entry,
                        exit=exit_px,
                        pnl_usdt=pnl,
                        ret_pct=ret_pct,
                        bars_held=(i - entry_i + 1),
                        fee_paid=(fee_exit),
                        leverage=int(LEVERAGE),
                        margin_used=(qty * entry) / max(1.0, float(LEVERAGE)),
                        notional=(qty * entry),
                        liq_est=float(liq) if liq is not None else 0.0,
                        mae_pct=mae,
                        mfe_pct=mfe,
                        r_multiple=r_mult,
                        trade_maxdd_pct=trade_dd_pct,
                    )
                )

                # reset pos
                pos = 0
                qty = 0.0
                entry = 0.0
                entry_i = 0
                entry_t = 0
                sl = None
                tp = None
                risk_dist = 0.0
                liq = None
                trade_peak_equity = None
                trade_trough_equity = None

        if pos != 0:
            continue

        # entries
        r = rsis[i]
        s = smas[i]
        a = atrs[i]
        if r is None or s is None or a is None or a <= 0:
            continue

        price = c.close
        if (a / price) < MIN_ATR_PCT:
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

        if COOLDOWN_BARS > 0 and desired == last_sig_side and (i - last_sig_i) < COOLDOWN_BARS:
            continue

        # execute at next open
        open_px = n.open
        exec_px = open_px * (1 + SLIPPAGE) if desired == 1 else open_px * (1 - SLIPPAGE)
        if equity <= 0:
            break

        risk_dist = SL_ATR * a

        # 先算 SL/TP + 爆倉保護（隔離近似）
        if desired == 1:
            sl = exec_px - risk_dist
            tp = exec_px + TP_R * risk_dist
            liq = exec_px * (1.0 - 1.0 / max(1.0, float(LEVERAGE)))
            if sl <= liq * (1.0 + LIQ_BUFFER_PCT):
                continue
        else:
            sl = exec_px + risk_dist
            tp = exec_px - TP_R * risk_dist
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

        fee_entry = fee(qty * exec_px)
        equity -= fee_entry

        pos = desired
        entry = exec_px
        entry_i = i + 1
        entry_t = n.open_time_ms

        last_sig_i = i
        last_sig_side = desired

        trade_peak_equity = mark_to_market(c.close)
        trade_trough_equity = trade_peak_equity

    # close at end (mark-to-market only; for simplicity do nothing)

    # equity_curve 欄位已改中文，這裡用最後一筆的 "權益(含浮盈,USDT)"
    last_equity = float(equity_curve[-1]["權益(含浮盈,USDT)"]) if equity_curve else equity

    summary = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "period_utc": f"{utc_ms_to_str(candles[0].open_time_ms)} -> {utc_ms_to_str(candles[-1].close_time_ms)}",
        "initial": INITIAL_EQUITY,
        "final_equity_mtm": last_equity,
        "return_pct_mtm": (last_equity / INITIAL_EQUITY - 1) * 100 if INITIAL_EQUITY else 0.0,
        "closed_trades": len(trades),
        "max_drawdown_pct": max_dd * 100,
        "params": {
            "rsi_long": RSI_LONG,
            "rsi_short": RSI_SHORT,
            "sl_atr": SL_ATR,
            "tp_r": TP_R,
            "min_atr_pct": MIN_ATR_PCT,
            "leverage": LEVERAGE,
            "risk_pct": RISK_PCT,
            "max_margin_pct": MAX_MARGIN_PCT,
            "liq_buffer_pct": LIQ_BUFFER_PCT,
            "rsi_n": RSI_N,
            "sma_n": SMA_N,
            "atr_n": ATR_N,
        },
    }

    return summary, trades, equity_curve


def write_csv_trades(path: str, trades: List[TradeRow]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        # 中文欄位（方便直接看/做表）
        w.writerow(
            [
                "進場時間(UTC)",
                "出場時間(UTC)",
                "方向",
                "進場價",
                "出場價",
                "盈虧(USDT)",
                "報酬率(%)",
                "持倉K數",
                "手續費(USDT)",
                "槓桿",
                "保證金佔用(USDT)",
                "名目價值(USDT)",
                "預估爆倉價",
                "MAE(%)",
                "MFE(%)",
                "R倍數",
                "單內最大回撤(%)",
            ]
        )
        for t in trades:
            w.writerow(
                [
                    utc_ms_to_str(t.entry_time_ms),
                    utc_ms_to_str(t.exit_time_ms),
                    t.side,
                    f"{t.entry:.6f}",
                    f"{t.exit:.6f}",
                    f"{t.pnl_usdt:.2f}",
                    f"{t.ret_pct:.4f}",
                    t.bars_held,
                    f"{t.fee_paid:.2f}",
                    t.leverage,
                    f"{t.margin_used:.2f}",
                    f"{t.notional:.2f}",
                    f"{t.liq_est:.6f}",
                    f"{t.mae_pct:.4f}",
                    f"{t.mfe_pct:.4f}",
                    f"{t.r_multiple:.4f}",
                    f"{t.trade_maxdd_pct:.4f}",
                ]
            )


def write_csv_equity(path: str, rows: List[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def tg_send_file(path: str, caption: str) -> None:
    cmd = [
        OPENCLAW_BIN,
        "message",
        "send",
        "--channel",
        "telegram",
        "--target",
        TELEGRAM_TARGET,
        "--media",
        path,
        "--message",
        caption,
    ]
    subprocess.run(cmd, check=False)


def main():
    start_dt = parse_start(START_UTC_STR)
    end_dt = datetime.now(timezone.utc)

    candles = fetch_klines(ms(start_dt), ms(end_dt))
    if len(candles) < 500:
        raise SystemExit(f"not enough candles: {len(candles)}")

    summary, trades, equity_curve = backtest_export(candles)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = f"{SYMBOL}-{INTERVAL}-{START_UTC_STR}-{ts}".replace("/", "_")

    trades_csv = os.path.join(OUT_DIR, f"trades-{base}.csv")
    equity_csv = os.path.join(OUT_DIR, f"equity-{base}.csv")

    write_csv_trades(trades_csv, trades)
    write_csv_equity(equity_csv, equity_curve)

    print("=== Summary ===")
    for k, v in summary.items():
        print(f"{k}: {v}")
    print(f"trades_csv: {trades_csv}")
    print(f"equity_csv: {equity_csv}")

    if SEND_TG:
        cap = (
            f"Backtest CSV {SYMBOL} {INTERVAL} start={START_UTC_STR} "
            f"closed_trades={summary['closed_trades']} maxDD={summary['max_drawdown_pct']:.2f}%\n"
            f"params={summary['params']}"
        )
        tg_send_file(trades_csv, cap)
        tg_send_file(equity_csv, cap)


if __name__ == "__main__":
    main()
