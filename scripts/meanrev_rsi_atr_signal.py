#!/usr/bin/env python3
"""meanrev_rsi_atr_signal.py

币安人生USDT（Binance UM Perp）15m：RSI 均值回归 + SMA 趋势过滤 + ATR 止损止盈

用途：
- 给 Telegram 群发“可执行”的交易计划：方向 / 进场价 / 止损 / 止盈
- 设计成可被 cron 每 15 分钟跑一次

策略（可调参数见 CONFIG）：
- 趋势过滤：
  - 价格 >= SMA200：只做多
  - 价格 <  SMA200：只做空
- 入场：
  - 多：RSI14 <= RSI_LONG
  - 空：RSI14 >= RSI_SHORT
- 风控：
  - SL = entry ± SL_ATR * ATR14
  - TP = entry ± TP_R * (SL_ATR * ATR14)
- 去重：同一根已收盘 K 只发一次；并且若上次信号同方向且未过 cooldown，不重复轰炸

⚠️ 研究工具，不构成投资建议；不保证盈利。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from typing import Optional, List

import requests

from strategy_lab import Candle, rsi, sma, atr

# ---------------- CONFIG ----------------
SYMBOL = "币安人生USDT"
INTERVAL = "15m"
LIMIT = 400  # 需要 SMA200 + 预热，抓 400 根够用

# 入场阈值（可后续用回测 sweep 来优化）
RSI_N = 14
SMA_N = 200
ATR_N = 14
# 默认：稳定性优先（近期回测 PF/回撤更好的一组）
RSI_LONG = 30.0
RSI_SHORT = 70.0

SL_ATR = 1.3     # 初始止损 = 1.3 * ATR
TP_R = 2.2       # 止盈 = 2.2R（R=止损距离）

# 冷却：同方向信号至少间隔多少根 15m K 才再发（避免极端行情刷屏）
COOLDOWN_BARS = 8

# 盘整/低波动过滤（ATR%）。稳定性优先：默认 1.0%
MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.010"))

# 槓桿/倉位（建議用「風險固定」來避免爆倉）
LEVERAGE = int(float(os.getenv("LEVERAGE", "20")))
RISK_PCT = float(os.getenv("RISK_PCT", "0.01"))          # 每單願意承擔的虧損佔 equity 比例（以 SL 距離估）
MAX_MARGIN_PCT = float(os.getenv("MAX_MARGIN_PCT", "0.20"))  # 隔離保證金最多用 equity 的比例（1000U/20x -> 名目上限約 4000U）
LIQ_BUFFER_PCT = float(os.getenv("LIQ_BUFFER_PCT", "0.005")) # 距離預估爆倉價的緩衝（避免滑點/手續費）

# 名目下單範圍（USDT）：你說通常 1000~4000U。
# 不強制；若你想「小於 1000U 就不出訊號」可把 MIN_NOTIONAL_USDT 設成 1000。
MIN_NOTIONAL_USDT = float(os.getenv("MIN_NOTIONAL_USDT", "0"))
MAX_NOTIONAL_USDT = float(os.getenv("MAX_NOTIONAL_USDT", "4000"))

# Telegram
TELEGRAM_TARGET = "-5170271645"
OPENCLAW_BIN = "/home/mark/.npm-global/bin/openclaw"

# 状态文件：记录最后一次已通知的 close_time，避免重复发
STATE_PATH = "/home/mark/.openclaw/workspace/state_meanrev_rsi_atr.json"

# 时区显示（台北）
LOCAL_TZ = timezone(timedelta(hours=8))
LOCAL_TZ_LABEL = "GMT+8"

BINANCE_FAPI = "https://fapi.binance.com"


def fetch_klines_latest() -> List[Candle]:
    url = f"{BINANCE_FAPI}/fapi/v1/klines"
    params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": LIMIT}
    r = requests.get(url, params=params, timeout=30)
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
    # 使用“最新已收盘K”：最后一根通常是进行中，因此用 -2
    if len(candles) < max(SMA_N, RSI_N, ATR_N) + 5:
        return None

    closes = [c.close for c in candles]
    rsis = rsi(closes, RSI_N)
    smas = sma(closes, SMA_N)
    atrs = atr(candles, ATR_N)

    i = len(candles) - 2
    c = candles[i]

    price = c.close

    r = rsis[i]
    s = smas[i]
    a = atrs[i]
    if r is None or s is None or a is None or a <= 0:
        return None

    # 低波动过滤
    if (a / price) < MIN_ATR_PCT:
        return None

    # 趋势过滤：上方只做多，下方只做空
    allow_long = price >= s
    allow_short = price < s

    side = None
    if allow_long and r <= RSI_LONG:
        side = "long"
    elif allow_short and r >= RSI_SHORT:
        side = "short"
    else:
        return None

    entry = price  # 以收盘价作为“触发后的建议进场价”（可自行改成市价/下一根开盘）
    risk = SL_ATR * a

    if side == "long":
        sl = entry - risk
        tp = entry + TP_R * risk
        liq = entry * (1.0 - 1.0 / max(1.0, float(LEVERAGE)))
        # SL 必須在爆倉價上方留 buffer
        if sl <= liq * (1.0 + LIQ_BUFFER_PCT):
            return None
    else:
        sl = entry + risk
        tp = entry - TP_R * risk
        liq = entry * (1.0 + 1.0 / max(1.0, float(LEVERAGE)))
        # SL 必須在爆倉價下方留 buffer
        if sl >= liq * (1.0 - LIQ_BUFFER_PCT):
            return None

    # 建議倉位（以固定風險為主，並加上最大保證金限制）
    stop_dist = abs(entry - sl)
    if stop_dist <= 0:
        return None

    # 注意：訊號腳本不知道你的實際帳戶 equity；這裡只回傳「每 1 USDT equity」的倉位比例
    # 你下單時用：qty = equity * qty_per_usdt_equity
    qty_per_usdt_equity = (RISK_PCT / stop_dist)

    # 另外估算 max notional / equity（隔離保證金限制）
    max_notional_per_usdt_equity = MAX_MARGIN_PCT * LEVERAGE

    return {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "bar_close_time_ms": c.close_time_ms,
        "time": ms_to_local_str(c.close_time_ms),
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "liq_est": liq,
        "qty_per_usdt_equity": qty_per_usdt_equity,
        "max_notional_per_usdt_equity": max_notional_per_usdt_equity,
        "rsi": float(r),
        "sma": float(s),
        "atr": float(a),
        "params": {
            "rsi_n": RSI_N,
            "sma_n": SMA_N,
            "atr_n": ATR_N,
            "rsi_long": RSI_LONG,
            "rsi_short": RSI_SHORT,
            "sl_atr": SL_ATR,
            "tp_r": TP_R,
            "min_atr_pct": MIN_ATR_PCT,
            "leverage": LEVERAGE,
            "risk_pct": RISK_PCT,
            "max_margin_pct": MAX_MARGIN_PCT,
            "liq_buffer_pct": LIQ_BUFFER_PCT,
        },
    }


def should_send(sig: dict, st: dict) -> bool:
    close_ms = sig["bar_close_time_ms"]

    # 1) 同一根 bar 不重复发
    if st.get("last_sent_close_time_ms") == close_ms:
        return False

    # 2) 冷却：同方向间隔 N 根
    last_side = st.get("last_side")
    last_ms = st.get("last_side_time_ms")
    if last_side == sig.get("side") and isinstance(last_ms, int):
        bar_ms = 15 * 60 * 1000
        if close_ms - last_ms < COOLDOWN_BARS * bar_ms:
            return False

    return True


def format_msg(sig: dict) -> str:
    side_cn = "做多" if sig["side"] == "long" else "做空"
    direction_emoji = "📈" if sig["side"] == "long" else "📉"

    entry = sig["entry"]
    sl = sig["sl"]
    tp = sig["tp"]
    liq = sig.get("liq_est")

    # 风险/收益（百分比）
    if sig["side"] == "long":
        risk_pct = (entry - sl) / entry * 100
        reward_pct = (tp - entry) / entry * 100
    else:
        risk_pct = (sl - entry) / entry * 100
        reward_pct = (entry - tp) / entry * 100

    atr_pct = (sig["atr"] / entry) * 100

    rr = reward_pct / risk_pct if risk_pct > 0 else 0.0

    # 以 1000U equity 估算一個示例倉位（方便你直接下單）
    equity_demo = 1000.0
    qty_demo = equity_demo * float(sig.get("qty_per_usdt_equity", 0.0))
    notional_demo = qty_demo * entry

    # 先套用「隔離保證金上限」（MAX_MARGIN_PCT）
    max_notional_by_margin = equity_demo * float(sig.get("max_notional_per_usdt_equity", 0.0))
    if max_notional_by_margin > 0:
        notional_demo = min(notional_demo, max_notional_by_margin)

    # 再套用你指定的「名目上限」（例如 4000U）
    if MAX_NOTIONAL_USDT > 0:
        notional_demo = min(notional_demo, MAX_NOTIONAL_USDT)

    qty_demo = notional_demo / entry if entry > 0 else 0.0
    margin_demo = notional_demo / max(1.0, float(LEVERAGE))

    # 預估停損/獲利金額（USDT）
    if sig["side"] == "long":
        loss_usdt = qty_demo * max(0.0, entry - sl)
        profit_usdt = qty_demo * max(0.0, tp - entry)
    else:
        loss_usdt = qty_demo * max(0.0, sl - entry)
        profit_usdt = qty_demo * max(0.0, entry - tp)

    return (
        f"{direction_emoji} {sig['symbol']} {side_cn} 信号（15m）\n\n"
        f"⏰ K线收盘时间: {sig['time']}\n"
        f"📌 条件: RSI({RSI_N})={sig['rsi']:.1f}  |  SMA{SMA_N}={sig['sma']:.6f}\n"
        f"💰 建议进场(参考): {entry:.6f}\n"
        f"🛑 止损(SL): {sl:.6f}  (风险 {risk_pct:.2f}%)\n"
        f"🎯 止盈(TP): {tp:.6f}  (潜在 {reward_pct:.2f}%)\n"
        f"📐 预估RR: {rr:.2f}  |  ATR{ATR_N}={sig['atr']:.6f} (ATR%≈{atr_pct:.2f}%)\n"
        f"🔎 盘整过滤: MIN_ATR_PCT={MIN_ATR_PCT*100:.2f}%\n"
        f"🧯 爆仓保护: LEV={LEVERAGE}x  预估爆仓价≈{liq:.6f}  buffer={LIQ_BUFFER_PCT*100:.2f}%\n"
        f"📦 下單示例(以 1000U / 隔離):\n"
        f"   - 名目≈{notional_demo:.2f}U (上限 {MAX_NOTIONAL_USDT:.0f}U)\n"
        f"   - qty≈{qty_demo:.2f}\n"
        f"   - 保證金≈{margin_demo:.2f}U\n"
        f"   - 預計停損≈-{loss_usdt:.2f}U  |  預計獲利≈+{profit_usdt:.2f}U\n\n"
        f"备注：这是回测/研究用策略输出，不保证盈利；下单前请自行确认流动性与风险。"
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
    ok = send_telegram(msg)
    if ok:
        st["last_sent_close_time_ms"] = sig["bar_close_time_ms"]
        st["last_side"] = sig["side"]
        st["last_side_time_ms"] = sig["bar_close_time_ms"]
        save_state(st)


if __name__ == "__main__":
    main()
