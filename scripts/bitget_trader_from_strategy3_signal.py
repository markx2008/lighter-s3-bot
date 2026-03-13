#!/usr/bin/env python3
"""bitget_trader_from_strategy3_signal.py

Develop Bitget auto-trading using current Strategy3 signal (Supertrend flip), but default to DRY-RUN.

What it does now (safe):
- Recompute Strategy3 BTCUSDT Supertrend flip signal from Binance Futures klines
- If a new flip signal appears:
  - If there is an opposite position -> close then open (allow flip)
  - If no position -> open
  - If same-side position -> do nothing
- Placeholders for Bitget REST order placement; currently uses PaperBroker.
- Sends all actions to Telegram group.

Env:
  DRY_RUN=1
  SYMBOL=BTCUSDT
  INTERVAL=15m
  LIMIT=600

  # sizing demo uses *ACCOUNT_EQUITY* (not demo) for future; for now set to 1000
  ACCOUNT_EQUITY=1000
  LEVERAGE=20
  RISK_PCT=0.01
  MAX_MARGIN_PCT=0.20
  MAX_NOTIONAL_USDT=4000

  # order policy
  ORDER_TYPE=limit
  LIMIT_OFFSET_BPS=2          # 2 bps away from reference price
  POST_ONLY=1                 # if later supported
  TIME_IN_FORCE=GTC

  TELEGRAM_TARGET=-5170271645
  STATE_PATH=/home/mark/.openclaw/workspace/state_bitget_trader_s3.json
  PAPER_STATE_PATH=/home/mark/.openclaw/workspace/state_paper_bitget_pos.json

Notes:
- Bitget API docs are protected by anti-bot; we'll wire real REST endpoints after you provide API details.
- This file is structured to swap PaperBroker -> BitgetBroker later.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import requests

from strategy_lab import Candle, atr
from bitget_paper_broker import PaperBroker

# ---------------- Market / Data ----------------
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
INTERVAL = os.getenv("INTERVAL", "15m")
LIMIT = int(float(os.getenv("LIMIT", "600")))
BINANCE_FAPI = "https://fapi.binance.com"

# ---------------- Strategy params ----------------
ST_ATR_N = int(float(os.getenv("S3_ST_ATR_N", "10")))
ST_MULT = float(os.getenv("S3_ST_MULT", "3.0"))
ST_TP_R = float(os.getenv("S3_ST_TP_R", "2.0"))

# ---------------- Risk / sizing ----------------
ACCOUNT_EQUITY = float(os.getenv("ACCOUNT_EQUITY", "1000"))
LEVERAGE = int(float(os.getenv("LEVERAGE", "20")))
RISK_PCT = float(os.getenv("RISK_PCT", "0.01"))
MAX_MARGIN_PCT = float(os.getenv("MAX_MARGIN_PCT", "0.20"))
MAX_NOTIONAL_USDT = float(os.getenv("MAX_NOTIONAL_USDT", "4000"))

# ---------------- Order policy ----------------
ORDER_TYPE = os.getenv("ORDER_TYPE", "limit")  # limit only for now
LIMIT_OFFSET_BPS = float(os.getenv("LIMIT_OFFSET_BPS", "2"))
POST_ONLY = os.getenv("POST_ONLY", "1") == "1"
TIME_IN_FORCE = os.getenv("TIME_IN_FORCE", "GTC")

# ---------------- Runtime ----------------
DRY_RUN = os.getenv("DRY_RUN", "1") == "1"
STATE_PATH = os.getenv("STATE_PATH", "/home/mark/.openclaw/workspace/state_bitget_trader_s3.json")
PAPER_STATE_PATH = os.getenv("PAPER_STATE_PATH", "/home/mark/.openclaw/workspace/state_paper_bitget_pos.json")

# ---------------- Telegram / OpenClaw ----------------
TELEGRAM_TARGET = os.getenv("TELEGRAM_TARGET", "-5170271645")
OPENCLAW_BIN = os.getenv("OPENCLAW_BIN", "/home/mark/.npm-global/bin/openclaw")

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


def supertrend(candles: List[Candle], atr_n: int, mult: float):
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

    return st, dir_, a


def ms_to_local_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=LOCAL_TZ).strftime(f"%Y-%m-%d %H:%M:%S {LOCAL_TZ_LABEL}")


def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def send_telegram(text: str) -> bool:
    cmd = [OPENCLAW_BIN, "message", "send", "--channel", "telegram", "--target", TELEGRAM_TARGET, "--message", text]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode == 0:
        return True
    print(p.stderr)
    return False


def calc_qty(entry: float, sl: float) -> Optional[dict]:
    stop_dist = abs(entry - sl)
    if stop_dist <= 0:
        return None
    risk_usdt = ACCOUNT_EQUITY * RISK_PCT
    qty = risk_usdt / stop_dist
    max_notional_by_margin = ACCOUNT_EQUITY * MAX_MARGIN_PCT * LEVERAGE
    notional = qty * entry
    if max_notional_by_margin > 0:
        notional = min(notional, max_notional_by_margin)
    if MAX_NOTIONAL_USDT > 0:
        notional = min(notional, MAX_NOTIONAL_USDT)
    qty = notional / entry
    margin = notional / max(1.0, float(LEVERAGE))
    return {"qty": qty, "notional": notional, "margin": margin, "risk_usdt": risk_usdt}


def main():
    st = load_state()
    broker = PaperBroker(PAPER_STATE_PATH)

    candles = fetch_klines_latest()
    st_line, st_dir, atr_n = supertrend(candles, ST_ATR_N, ST_MULT)

    i = len(candles) - 2
    bar = candles[i]
    if st_dir[i] is None or st_dir[i - 1] is None or st_line[i] is None:
        return

    flip_up = st_dir[i] == 1 and st_dir[i - 1] == -1
    flip_dn = st_dir[i] == -1 and st_dir[i - 1] == 1
    if not (flip_up or flip_dn):
        return

    if st.get("last_sent_close_time_ms") == bar.close_time_ms:
        return

    side = "long" if flip_up else "short"
    entry_ref = bar.close
    sl = float(st_line[i])
    plan = calc_qty(entry_ref, sl)
    if not plan:
        return

    # limit price: offset bps from reference
    off = LIMIT_OFFSET_BPS / 10000.0
    if side == "long":
        limit_px = entry_ref * (1.0 - off)
    else:
        limit_px = entry_ref * (1.0 + off)

    # TP (optional)
    risk_dist = abs(entry_ref - sl)
    tp = None
    if ST_TP_R > 0:
        tp = entry_ref + ST_TP_R * risk_dist if side == "long" else entry_ref - ST_TP_R * risk_dist

    pos = broker.get_position()

    actions = []
    if pos is None:
        actions.append(("open", side))
    else:
        if pos.side != side:
            actions.append(("close", pos.side))
            actions.append(("open", side))
        else:
            actions.append(("hold", side))

    # execute (paper)
    lines = []
    lines.append(f"🤖 Bitget AutoTrader (DEV / {'DRY' if DRY_RUN else 'LIVE'})")
    lines.append(f"⏰ Signal close: {ms_to_local_str(bar.close_time_ms)}")
    lines.append(f"📌 Signal: {SYMBOL} {side.upper()} (Supertrend flip)")
    lines.append(f"entry_ref={entry_ref:.2f}  limit_px={limit_px:.2f}  SL={sl:.2f}" + (f"  TP={tp:.2f}" if tp is not None else ""))
    lines.append(f"sizing: equity={ACCOUNT_EQUITY:.0f}U risk={plan['risk_usdt']:.2f}U notional≈{plan['notional']:.2f}U qty≈{plan['qty']:.6f} margin≈{plan['margin']:.2f}U")
    lines.append(f"order: type={ORDER_TYPE} tif={TIME_IN_FORCE} postOnly={int(POST_ONLY)} offset={LIMIT_OFFSET_BPS}bps")

    if pos:
        lines.append(f"pos(before): {pos.side} qty={pos.qty:.6f} entry={pos.entry:.2f}")
    else:
        lines.append("pos(before): none")

    for act, s in actions:
        if act == "hold":
            lines.append("✅ action: HOLD (same side position)")
        elif act == "close":
            res = broker.close_position(price=entry_ref)
            lines.append(f"🧾 action: CLOSE {s.upper()} @mkt_ref {entry_ref:.2f}  pnl≈{(res['pnl'] if res else 0.0):.2f}U")
        elif act == "open":
            broker.open_position(side=side, qty=float(plan["qty"]), price=limit_px)
            lines.append(f"🟦 action: OPEN {side.upper()} LIMIT {limit_px:.2f} qty={plan['qty']:.6f} (paper)")

    send_telegram("\n".join(lines))

    st["last_sent_close_time_ms"] = bar.close_time_ms
    save_state(st)


if __name__ == "__main__":
    main()
