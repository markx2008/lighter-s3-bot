#!/usr/bin/env python3
"""strategy2_boll_signal.py

TG 訊號：策略2 - 布林收斂突破（币安人生USDT 永續，15m）

設計目標：
- 有訊號才通知（避免刷屏）
- 通知包含：方向、進場(參考)、SL/TP、RR、1000U 基準倉位/名目/保證金、預計停損/獲利
- 風控與回測一致：20x 隔離、固定風險倉位、保證金上限、爆倉 buffer

核心參數（預設採用你選的 1A/2A/3開）：
- 每 15 分鐘由 cron 觸發
- Exit-opt 參數僅用來計算 TP（trailing/time-stop 會在訊息備註中標示）
- 環境濾網：bandwidth 擴張確認（slope）

⚠️ 研究用途，不構成投資建議。
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import requests

from strategy_lab import Candle, atr, sma

# ---------------- Market / Data ----------------
SYMBOL = os.getenv("SYMBOL", "币安人生USDT")
INTERVAL = os.getenv("INTERVAL", "15m")
LIMIT = int(float(os.getenv("LIMIT", "220")))
BINANCE_FAPI = "https://fapi.binance.com"

# ---------------- Strategy params (A + tuned) ----------------
BOLL_LEN = int(float(os.getenv("S2_BOLL_LEN", "20")))
SQ_LEN = int(float(os.getenv("S2_BOLL_SQ_LEN", "12")))
BW_THR = float(os.getenv("S2_BOLL_BW", "0.05"))

# entry filters
VOL_FACTOR = float(os.getenv("S2_BOLL_VOL_FACTOR", "1.2"))
BREAK_ATR = float(os.getenv("S2_BOLL_BREAK_ATR", "0.30"))
MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.012"))

# bandwidth expansion filter (enabled)
BW_SLOPE_LEN = int(float(os.getenv("S2_BOLL_BW_SLOPE_LEN", "6")))
BW_SLOPE_MIN = float(os.getenv("S2_BOLL_BW_SLOPE_MIN", "0.0005"))

# exits (2A)
STOP_ATR = float(os.getenv("S2_BOLL_STOP", "1.0"))
TP_ATR = float(os.getenv("S2_BOLL_TP", "2.1"))
TRAIL_ATR = float(os.getenv("S2_BOLL_TRAIL", "0.5"))
TRAIL_START_ATR = float(os.getenv("S2_BOLL_TRAIL_START", "2.0"))
TIME_STOP = int(float(os.getenv("S2_BOLL_TIME_STOP", "72")))

# ---------------- Risk model (signal sizing demo) ----------------
LEVERAGE = int(float(os.getenv("LEVERAGE", "20")))
RISK_PCT = float(os.getenv("RISK_PCT", "0.01"))
MAX_MARGIN_PCT = float(os.getenv("MAX_MARGIN_PCT", "0.20"))
LIQ_BUFFER_PCT = float(os.getenv("LIQ_BUFFER_PCT", "0.005"))

# demo equity for message
EQUITY_DEMO = float(os.getenv("EQUITY_DEMO", "1000"))
MAX_NOTIONAL_USDT = float(os.getenv("MAX_NOTIONAL_USDT", "4000"))

# ---------------- Telegram / OpenClaw ----------------
TELEGRAM_TARGET = os.getenv("TELEGRAM_TARGET", "-5170271645")
OPENCLAW_BIN = os.getenv("OPENCLAW_BIN", "/home/mark/.npm-global/bin/openclaw")

# ---------------- State (dedupe) ----------------
STATE_PATH = os.getenv("STATE_PATH", "/home/mark/.openclaw/workspace/state_strategy2_boll_signal.json")

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


def rolling_std(values: List[float], window: int) -> List[Optional[float]]:
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
            out[i] = (var if var > 0 else 0.0) ** 0.5
    return out


def has_recent_squeeze(idx: int, bw: List[Optional[float]], length: int, thr: float) -> bool:
    if length <= 0 or idx < length:
        return False
    for j in range(idx - length, idx):
        v = bw[j]
        if v is None or v > thr:
            return False
    return True


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
    if len(candles) < max(BOLL_LEN, 20, 60) + 5:
        return None

    closes = [c.close for c in candles]
    vols = [c.volume for c in candles]

    atr14 = atr(candles, 14)
    sma20 = sma(closes, BOLL_LEN)
    std20 = rolling_std(closes, BOLL_LEN)

    # bands + bandwidth
    upper: List[Optional[float]] = [None] * len(candles)
    lower: List[Optional[float]] = [None] * len(candles)
    bw: List[Optional[float]] = [None] * len(candles)
    for i in range(len(candles)):
        mid = sma20[i]
        dev = std20[i]
        if mid is None or dev is None or mid == 0:
            continue
        up = mid + 2.0 * dev
        lo = mid - 2.0 * dev
        upper[i] = up
        lower[i] = lo
        bw[i] = (up - lo) / mid

    # use last closed bar
    i = len(candles) - 2
    bar = candles[i]

    a = atr14[i]
    up = upper[i]
    lo = lower[i]
    bwi = bw[i]
    if a is None or a <= 0 or up is None or lo is None or bwi is None:
        return None

    close = bar.close

    # atr% filter
    if (a / close) < MIN_ATR_PCT:
        return None

    # squeeze filter
    if not has_recent_squeeze(i, bw, SQ_LEN, BW_THR):
        return None

    # bandwidth expansion filter
    if BW_SLOPE_LEN > 1 and i >= BW_SLOPE_LEN and bw[i - BW_SLOPE_LEN] is not None:
        slope = (bw[i] - bw[i - BW_SLOPE_LEN]) / BW_SLOPE_LEN
        if slope < BW_SLOPE_MIN:
            return None

    # volume filter (volSMA20)
    if VOL_FACTOR > 0 and i >= 20:
        vol_sma = sum(vols[i - 20 : i]) / 20.0
        if vol_sma > 0 and vols[i] < vol_sma * VOL_FACTOR:
            return None

    side = None
    # breakout magnitude filter
    if close > up:
        if BREAK_ATR > 0 and close < (up + BREAK_ATR * a):
            return None
        side = "long"
    elif close < lo:
        if BREAK_ATR > 0 and close > (lo - BREAK_ATR * a):
            return None
        side = "short"
    else:
        return None

    entry = close
    risk_dist = STOP_ATR * a

    if side == "long":
        sl = entry - risk_dist
        tp = entry + TP_ATR * risk_dist
        liq = entry * (1.0 - 1.0 / max(1.0, float(LEVERAGE)))
        if sl <= liq * (1.0 + LIQ_BUFFER_PCT):
            return None
    else:
        sl = entry + risk_dist
        tp = entry - TP_ATR * risk_dist
        liq = entry * (1.0 + 1.0 / max(1.0, float(LEVERAGE)))
        if sl >= liq * (1.0 - LIQ_BUFFER_PCT):
            return None

    stop_dist = abs(entry - sl)
    if stop_dist <= 0:
        return None

    # sizing (for demo equity)
    risk_usdt = EQUITY_DEMO * RISK_PCT
    qty = risk_usdt / stop_dist
    max_notional_by_margin = EQUITY_DEMO * MAX_MARGIN_PCT * LEVERAGE
    notional = qty * entry
    if max_notional_by_margin > 0:
        notional = min(notional, max_notional_by_margin)
    if MAX_NOTIONAL_USDT > 0:
        notional = min(notional, MAX_NOTIONAL_USDT)
    qty = notional / entry
    margin = notional / max(1.0, float(LEVERAGE))

    if side == "long":
        loss_usdt = qty * max(0.0, entry - sl)
        profit_usdt = qty * max(0.0, tp - entry)
    else:
        loss_usdt = qty * max(0.0, sl - entry)
        profit_usdt = qty * max(0.0, entry - tp)

    risk_pct = (abs(entry - sl) / entry) * 100
    reward_pct = (abs(tp - entry) / entry) * 100
    rr = reward_pct / risk_pct if risk_pct > 0 else 0.0

    return {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "bar_close_time_ms": bar.close_time_ms,
        "time": ms_to_local_str(bar.close_time_ms),
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "atr": a,
        "atr_pct": (a / entry) * 100,
        "liq_est": liq,
        "rr": rr,
        "notional": notional,
        "qty": qty,
        "margin": margin,
        "loss_usdt": loss_usdt,
        "profit_usdt": profit_usdt,
        "params": {
            "sq_len": SQ_LEN,
            "bw_thr": BW_THR,
            "vol_factor": VOL_FACTOR,
            "break_atr": BREAK_ATR,
            "bw_slope_len": BW_SLOPE_LEN,
            "bw_slope_min": BW_SLOPE_MIN,
            "stop_atr": STOP_ATR,
            "tp_atr": TP_ATR,
            "trail_atr": TRAIL_ATR,
            "trail_start_atr": TRAIL_START_ATR,
            "time_stop": TIME_STOP,
            "leverage": LEVERAGE,
            "risk_pct": RISK_PCT,
            "max_margin_pct": MAX_MARGIN_PCT,
            "liq_buffer_pct": LIQ_BUFFER_PCT,
            "min_atr_pct": MIN_ATR_PCT,
        },
    }


def should_send(sig: dict, st: dict) -> bool:
    close_ms = sig["bar_close_time_ms"]
    if st.get("last_sent_close_time_ms") == close_ms:
        return False
    return True


def format_msg(sig: dict) -> str:
    side_cn = "做多" if sig["side"] == "long" else "做空"
    direction = "📈" if sig["side"] == "long" else "📉"

    return (
        f"{direction} {sig['symbol']} {side_cn} 信号（布林收斂突破 / 15m）\n\n"
        f"⏰ K线收盘时间: {sig['time']}\n"
        f"💰 进场(参考): {sig['entry']:.6f}\n"
        f"🛑 止损(SL): {sig['sl']:.6f}\n"
        f"🎯 止盈(TP): {sig['tp']:.6f}\n"
        f"📐 RR≈{sig['rr']:.2f} | ATR14={sig['atr']:.6f} (ATR%≈{sig['atr_pct']:.2f}%)\n"
        f"🔎 过滤: vol×{VOL_FACTOR} | break={BREAK_ATR}ATR | bwSlope(L={BW_SLOPE_LEN},min={BW_SLOPE_MIN}) | MIN_ATR_PCT={MIN_ATR_PCT*100:.2f}%\n"
        f"🧯 爆仓保护: LEV={LEVERAGE}x  预估爆仓价≈{sig['liq_est']:.6f}  buffer={LIQ_BUFFER_PCT*100:.2f}%\n\n"
        f"📦 下單示例(以 {EQUITY_DEMO:.0f}U / 隔離):\n"
        f"• 名目≈{sig['notional']:.2f}U (上限 {MAX_NOTIONAL_USDT:.0f}U)\n"
        f"• qty≈{sig['qty']:.2f}\n"
        f"• 保證金≈{sig['margin']:.2f}U\n"
        f"• 預計停損≈-{sig['loss_usdt']:.2f}U | 預計獲利≈+{sig['profit_usdt']:.2f}U\n\n"
        f"📌 出场结构(参数): TP={TP_ATR}R  | trailing={TRAIL_ATR}ATR@{TRAIL_START_ATR}ATR  | time-stop={TIME_STOP}根\n"
        f"备注：研究用输出，不保证盈利；下单前请自行确认流动性与风险。"
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
        save_state(st)


if __name__ == "__main__":
    main()
