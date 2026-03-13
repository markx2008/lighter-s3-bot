#!/usr/bin/env python3
"""strategy3_btc_signal.py

TG 訊號：策略3 - BTCUSDT Supertrend Trend（UM 永續，15m）

策略（與回測一致的核心邏輯）：
- 以 Supertrend(ATR_N, MULT) 判斷方向
- 進場：方向翻轉（-1→+1 做多, +1→-1 做空）
- 出場：
  - SL：使用 Supertrend 線（隨時間跟隨，僅收緊不放寬）
  - flip：方向再次翻轉（收盤價出場）
  - 可選 TP：以 entry→初始SL 的距離為 1R，TP=ST_TP_R * R

訊號腳本僅負責：
- 有「新 flip 訊號」才通知（避免刷屏）
- 計算並顯示：entry(參考)、SL、TP、RR、以及以 1000U (EQUITY_DEMO) 的倉位示例

Env (default):
  SYMBOL=BTCUSDT
  INTERVAL=15m
  LIMIT=600

Supertrend:
  S3_ST_ATR_N=10
  S3_ST_MULT=3.0
  S3_ST_TP_R=2.0   # 0 disables fixed TP

Risk model (for sizing demo):
  EQUITY_DEMO=1000
  LEVERAGE=20
  RISK_PCT=0.01
  MAX_MARGIN_PCT=0.20
  LIQ_BUFFER_PCT=0.005
  MAX_NOTIONAL_USDT=4000

Telegram:
  TELEGRAM_TARGET=-5170271645
  STATE_PATH=/home/mark/.openclaw/workspace/state_strategy3_btc_signal.json

"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

import requests

from strategy_lab import Candle, atr

# ---------------- Market / Data ----------------
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
INTERVAL = os.getenv("INTERVAL", "15m")
LIMIT = int(float(os.getenv("LIMIT", "600")))
BINANCE_FAPI = "https://fapi.binance.com"

# ---------------- Strategy params ----------------
ST_ATR_N = int(float(os.getenv("S3_ST_ATR_N", "10")))
ST_MULT = float(os.getenv("S3_ST_MULT", "3.0"))
ST_TP_R = float(os.getenv("S3_ST_TP_R", "2.0"))

# ---------------- Risk model (signal sizing demo) ----------------
LEVERAGE = int(float(os.getenv("LEVERAGE", "20")))
RISK_PCT = float(os.getenv("RISK_PCT", "0.01"))
MAX_MARGIN_PCT = float(os.getenv("MAX_MARGIN_PCT", "0.20"))
LIQ_BUFFER_PCT = float(os.getenv("LIQ_BUFFER_PCT", "0.005"))

EQUITY_DEMO = float(os.getenv("EQUITY_DEMO", "1000"))
MAX_NOTIONAL_USDT = float(os.getenv("MAX_NOTIONAL_USDT", "4000"))

# ---------------- Telegram / OpenClaw ----------------
TELEGRAM_TARGET = os.getenv("TELEGRAM_TARGET", "-5170271645")
OPENCLAW_BIN = os.getenv("OPENCLAW_BIN", "/home/mark/.npm-global/bin/openclaw")

# ---------------- State (dedupe) ----------------
STATE_PATH = os.getenv("STATE_PATH", "/home/mark/.openclaw/workspace/state_strategy3_btc_signal.json")

LOCAL_TZ = timezone(timedelta(hours=8))
LOCAL_TZ_LABEL = "GMT+8"


def fetch_klines_latest() -> List[Candle]:
    r = requests.get(
        f"{BINANCE_FAPI}/fapi/v1/klines",
        params={"symbol": SYMBOL, "interval": INTERVAL, "limit": LIMIT},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    out: List[Candle] = []
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
    return out


def supertrend(candles: List[Candle], atr_n: int, mult: float) -> Tuple[List[Optional[float]], List[Optional[int]]]:
    n = len(candles)
    a = atr(candles, atr_n)
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


def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def ms_to_local_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=LOCAL_TZ).strftime(f"%Y-%m-%d %H:%M:%S {LOCAL_TZ_LABEL}")


def build_signal(candles: List[Candle]) -> Optional[dict]:
    if len(candles) < max(200, ST_ATR_N + 10):
        return None

    st_line, st_dir = supertrend(candles, ST_ATR_N, ST_MULT)
    atr14 = atr(candles, 14)

    i = len(candles) - 2  # last closed bar
    bar = candles[i]

    if st_dir[i] is None or st_dir[i - 1] is None or st_line[i] is None:
        return None
    if atr14[i] is None or atr14[i] <= 0:
        return None

    flip_up = st_dir[i] == 1 and st_dir[i - 1] == -1
    flip_dn = st_dir[i] == -1 and st_dir[i - 1] == 1
    if not (flip_up or flip_dn):
        return None

    side = "long" if flip_up else "short"
    entry_ref = bar.close
    sl = st_line[i]
    risk_dist = abs(entry_ref - sl)
    if risk_dist <= 0:
        return None

    if side == "long":
        tp = entry_ref + ST_TP_R * risk_dist if ST_TP_R > 0 else None
        liq = entry_ref * (1.0 - 1.0 / max(1.0, float(LEVERAGE)))
        if sl <= liq * (1.0 + LIQ_BUFFER_PCT):
            return None
    else:
        tp = entry_ref - ST_TP_R * risk_dist if ST_TP_R > 0 else None
        liq = entry_ref * (1.0 + 1.0 / max(1.0, float(LEVERAGE)))
        if sl >= liq * (1.0 - LIQ_BUFFER_PCT):
            return None

    # sizing demo based on EQUITY_DEMO (this answers the question: sizing depends on equity)
    stop_dist = risk_dist
    risk_usdt = EQUITY_DEMO * RISK_PCT
    qty = risk_usdt / stop_dist
    max_notional_by_margin = EQUITY_DEMO * MAX_MARGIN_PCT * LEVERAGE
    notional = qty * entry_ref
    if max_notional_by_margin > 0:
        notional = min(notional, max_notional_by_margin)
    if MAX_NOTIONAL_USDT > 0:
        notional = min(notional, MAX_NOTIONAL_USDT)
    qty = notional / entry_ref
    margin = notional / max(1.0, float(LEVERAGE))

    if side == "long":
        loss_usdt = qty * max(0.0, entry_ref - sl)
        profit_usdt = qty * max(0.0, (tp - entry_ref)) if tp is not None else 0.0
    else:
        loss_usdt = qty * max(0.0, sl - entry_ref)
        profit_usdt = qty * max(0.0, (entry_ref - tp)) if tp is not None else 0.0

    risk_pct = (abs(entry_ref - sl) / entry_ref) * 100
    reward_pct = (abs(tp - entry_ref) / entry_ref) * 100 if tp is not None else 0.0
    rr = (reward_pct / risk_pct) if (risk_pct > 0 and tp is not None) else 0.0

    return {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "bar_close_time_ms": bar.close_time_ms,
        "time": ms_to_local_str(bar.close_time_ms),
        "side": side,
        "entry": entry_ref,
        "sl": sl,
        "tp": tp,
        "atr14": float(atr14[i]),
        "atr_pct": (float(atr14[i]) / entry_ref) * 100,
        "liq_est": liq,
        "rr": rr,
        "notional": notional,
        "qty": qty,
        "margin": margin,
        "loss_usdt": loss_usdt,
        "profit_usdt": profit_usdt,
        "params": {
            "atr_n": ST_ATR_N,
            "mult": ST_MULT,
            "tp_r": ST_TP_R,
            "leverage": LEVERAGE,
            "risk_pct": RISK_PCT,
            "max_margin_pct": MAX_MARGIN_PCT,
            "liq_buffer_pct": LIQ_BUFFER_PCT,
        },
    }


def should_send(sig: dict, st: dict) -> bool:
    close_ms = sig["bar_close_time_ms"]
    if st.get("last_sent_close_time_ms") == close_ms:
        return False
    # also dedupe by side+time
    key = f"{sig['side']}@{close_ms}"
    if st.get("last_key") == key:
        return False
    return True


def format_msg(sig: dict) -> str:
    side_cn = "做多" if sig["side"] == "long" else "做空"
    direction = "📈" if sig["side"] == "long" else "📉"

    tp_line = f"🎯 止盈(TP): {sig['tp']:.2f}\n" if sig["tp"] is not None else "🎯 止盈(TP): (無固定TP，靠Supertrend跟隨出場)\n"
    rr_line = f"📐 RR≈{sig['rr']:.2f} | " if sig["tp"] is not None else "📐 RR: n/a | "

    return (
        f"{direction} {sig['symbol']} {side_cn} 信号（Supertrend Trend / 15m）\n\n"
        f"⏰ K线收盘时间: {sig['time']}\n"
        f"💰 进场(参考): {sig['entry']:.2f}\n"
        f"🛑 止损(SL=Supertrend線): {sig['sl']:.2f}\n"
        f"{tp_line}"
        f"{rr_line}ATR14={sig['atr14']:.2f} (ATR%≈{sig['atr_pct']:.2f}%)\n"
        f"🧯 爆仓保护: LEV={LEVERAGE}x  预估爆仓价≈{sig['liq_est']:.2f}  buffer={LIQ_BUFFER_PCT*100:.2f}%\n\n"
        f"📦 下單示例(以 {EQUITY_DEMO:.0f}U / 隔離, 依資金量等比例調整):\n"
        f"• 風險金額≈{EQUITY_DEMO*RISK_PCT:.2f}U (RISK_PCT={RISK_PCT*100:.2f}%)\n"
        f"• 名目≈{sig['notional']:.2f}U (上限 {MAX_NOTIONAL_USDT:.0f}U)\n"
        f"• qty≈{sig['qty']:.6f}\n"
        f"• 保證金≈{sig['margin']:.2f}U\n"
        f"• 預計停損≈-{sig['loss_usdt']:.2f}U"
        + (f" | 預計獲利≈+{sig['profit_usdt']:.2f}U\n\n" if sig["tp"] is not None else "\n\n")
        + f"📌 參數: ATR_N={ST_ATR_N} MULT={ST_MULT} TP_R={ST_TP_R}\n"
        + "备注：研究用输出，不保证盈利；下单前请自行确认流动性与风险。"
    )


def send_telegram(text: str) -> bool:
    cmd = [
        OPENCLAW_BIN,
        "message",
        "send",
        "--channel",
        "telegram",
        "--target",
        TELEGRAM_TARGET,
        "--message",
        text,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode == 0:
        return True
    print(p.stderr)
    return False


def main():
    st = load_state()
    candles = fetch_klines_latest()
    sig = build_signal(candles)
    if not sig:
        return
    if not should_send(sig, st):
        return
    msg = format_msg(sig)
    if send_telegram(msg):
        st["last_sent_close_time_ms"] = sig["bar_close_time_ms"]
        st["last_key"] = f"{sig['side']}@{sig['bar_close_time_ms']}"
        save_state(st)


if __name__ == "__main__":
    main()
