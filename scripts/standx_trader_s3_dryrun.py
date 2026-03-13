#!/usr/bin/env python3
"""standx_trader_s3_dryrun.py

StandX AutoTrader (DEV / DRY-RUN) driven by Strategy3 Supertrend flip.

- Signal source: StandX perps klines (BTC-USD) OR Binance klines; default uses StandX kline history.
- Broker: PaperBroker (simulated position) + performance tracking
- Notifications: Telegram group

This is NOT live trading. It is meant to validate:
- sizing logic with ACCOUNT_EQUITY
- flip behavior (allow reverse)
- limit price placement (offset)
- PnL accounting + equity curve / monthly reports

Env:
  DRY_RUN=1
  SYMBOL=BTC-USD
  RESOLUTION=15
  LOOKBACK_DAYS=30

  ACCOUNT_EQUITY=20000
  LEVERAGE=20
  RISK_PCT=0.01
  MAX_MARGIN_PCT=0.20
  MAX_NOTIONAL_USDT=0          # 0 = no hard cap

  ORDER_TYPE=limit
  LIMIT_OFFSET_BPS=2
  TIME_IN_FORCE=gtc

  TELEGRAM_TARGET=-5170271645
  STATE_PATH=/home/mark/.openclaw/workspace/state_standx_trader_s3.json
  PAPER_STATE_PATH=/home/mark/.openclaw/workspace/state_paper_standx_pos.json

"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

from standx_client import StandXClient, StandXConfig
from strategy_lab import Candle, atr
from bitget_paper_broker import PaperBroker

# --- config ---
SYMBOL = os.getenv("SYMBOL", "BTC-USD")
RESOLUTION = os.getenv("RESOLUTION", "15")
LOOKBACK_DAYS = int(float(os.getenv("LOOKBACK_DAYS", "30")))

ST_ATR_N = int(float(os.getenv("S3_ST_ATR_N", "10")))
ST_MULT = float(os.getenv("S3_ST_MULT", "3.0"))
ST_TP_R = float(os.getenv("S3_ST_TP_R", "2.0"))

ACCOUNT_EQUITY = float(os.getenv("ACCOUNT_EQUITY", "20000"))
LEVERAGE = int(float(os.getenv("LEVERAGE", "20")))
RISK_PCT = float(os.getenv("RISK_PCT", "0.01"))
MAX_MARGIN_PCT = float(os.getenv("MAX_MARGIN_PCT", "0.20"))
MAX_NOTIONAL_USDT = float(os.getenv("MAX_NOTIONAL_USDT", "0"))

ORDER_TYPE = os.getenv("ORDER_TYPE", "limit")
LIMIT_OFFSET_BPS = float(os.getenv("LIMIT_OFFSET_BPS", "2"))
TIME_IN_FORCE = os.getenv("TIME_IN_FORCE", "gtc")

DRY_RUN = os.getenv("DRY_RUN", "1") == "1"
STATE_PATH = os.getenv("STATE_PATH", "/home/mark/.openclaw/workspace/state_standx_trader_s3.json")
PAPER_STATE_PATH = os.getenv("PAPER_STATE_PATH", "/home/mark/.openclaw/workspace/state_paper_standx_pos.json")

TELEGRAM_TARGET = os.getenv("TELEGRAM_TARGET", "-5170271645")
OPENCLAW_BIN = os.getenv("OPENCLAW_BIN", "/home/mark/.npm-global/bin/openclaw")

LOCAL_TZ = timezone(timedelta(hours=8))
LOCAL_TZ_LABEL = "GMT+8"


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


def fetch_standx_candles(client: StandXClient) -> List[Candle]:
    now = int(time.time())
    frm = now - LOOKBACK_DAYS * 86400
    data = client._get("/api/kline/history", params={"symbol": SYMBOL, "resolution": RESOLUTION, "from": frm, "to": now})
    # format: {s:'ok', t:[sec], o/h/l/c/v arrays}
    if not isinstance(data, dict) or data.get("s") != "ok":
        return []
    t = data["t"]
    o = data["o"]
    h = data["h"]
    l = data["l"]
    c = data["c"]
    v = data["v"]
    out: List[Candle] = []
    for i in range(len(t)):
        open_ms = int(t[i]) * 1000
        close_ms = open_ms + int(int(RESOLUTION) * 60 * 1000) - 1
        out.append(Candle(open_time_ms=open_ms, open=float(o[i]), high=float(h[i]), low=float(l[i]), close=float(c[i]), volume=float(v[i]), close_time_ms=close_ms))
    return out


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
    if MAX_NOTIONAL_USDT and MAX_NOTIONAL_USDT > 0:
        notional = min(notional, MAX_NOTIONAL_USDT)
    qty = notional / entry
    margin = notional / max(1.0, float(LEVERAGE))
    return {"qty": qty, "notional": notional, "margin": margin, "risk_usdt": risk_usdt, "stop_dist": stop_dist}


def main():
    st = load_state()
    broker = PaperBroker(PAPER_STATE_PATH)

    client = StandXClient(StandXConfig())
    candles = fetch_standx_candles(client)
    if len(candles) < 200:
        return

    st_line, st_dir, _a = supertrend(candles, ST_ATR_N, ST_MULT)

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

    off = LIMIT_OFFSET_BPS / 10000.0
    limit_px = entry_ref * (1.0 - off) if side == "long" else entry_ref * (1.0 + off)

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

    lines = []
    lines.append(f"🤖 StandX AutoTrader (DEV / {'DRY' if DRY_RUN else 'LIVE'})")
    lines.append(f"⏰ Signal close: {ms_to_local_str(bar.close_time_ms)}")
    lines.append(f"📌 Signal: {SYMBOL} {side.upper()} (Supertrend flip)")
    lines.append(f"entry_ref={entry_ref:.2f}  limit_px={limit_px:.2f}  SL={sl:.2f}" + (f"  TP={tp:.2f}" if tp is not None else ""))
    lines.append(f"sizing: equity={ACCOUNT_EQUITY:.0f}U risk={plan['risk_usdt']:.2f}U notional≈{plan['notional']:.2f}U qty≈{plan['qty']:.6f} margin≈{plan['margin']:.2f}U")
    lines.append(f"order: type={ORDER_TYPE} tif={TIME_IN_FORCE} offset={LIMIT_OFFSET_BPS}bps")
    lines.append(f"MAX_NOTIONAL_USDT={MAX_NOTIONAL_USDT} (0=無上限)")

    if pos:
        lines.append(f"pos(before): {pos.side} qty={pos.qty:.6f} entry={pos.entry:.2f}")
    else:
        lines.append("pos(before): none")

    for act, s in actions:
        if act == "hold":
            lines.append("✅ action: HOLD")
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
