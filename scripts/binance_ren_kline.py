#!/usr/bin/env python3
"""
币安人生 (1000LUNCUSDT) 15分K線反轉監控
執行時間: 每15分鐘的01秒 (0:01, 15:01, 30:01, 45:01)
邏輯: 連漲N根 + 第N+1根反轉下跌
"""

import requests
import json
from datetime import datetime, timezone, timedelta
import sys
import os

# 顯示用時區：GMT+8（台北）
LOCAL_TZ = timezone(timedelta(hours=8))
LOCAL_TZ_LABEL = "GMT+8"

# 配置
SYMBOL = "币安人生USDT"       # API用的交易對代碼（完全照用戶指定）
DISPLAY_NAME = "币安人生USDT" # 顯示用的中文名稱
INTERVAL = "15m"             # 15分鐘K線
LIMIT = 10                   # 抓最近10根，計算連漲數量
# Telegram 通知目標（原 fe 群組）
TELEGRAM_TARGET = "-5170271645"   # telegram chat id
OPENCLAW_BIN = "/home/mark/.npm-global/bin/openclaw"  # cron 環境下 PATH 可能找不到

MIN_CONSECUTIVE = 3          # 最少連漲幾根才通知

def get_klines(end_time_ms: int | None = None):
    """從幣安 API 抓K線資料。

    重點：為了避免「剛收盤但 /klines 尚未更新」的延遲，
    我們優先用 endTime 鎖定『應該已收盤的那根 K 線』。
    """
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "limit": LIMIT,
    }
    if end_time_ms is not None:
        params["endTime"] = int(end_time_ms)

    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[ERROR] 抓取K線失敗: {e}")
        return None

def detect_reversal(candles):
    """偵測多空反轉：連漲 N 根後首次收跌、或連跌 N 根後首次收漲。

    candles: 已解析的K線列表（含最後一根「進行中」）
    只使用倒數第2根作為「最新已收盤K」。

    回傳:
      (count, reversal_candle, prior_trend_candles, direction)

    direction:
      - "long_to_short"  連漲後轉跌（多轉空）
      - "short_to_long"  連跌後轉漲（空轉多）
      - None             未形成反轉
    """

    if len(candles) < 3:
        return 0, None, [], None

    last_closed = candles[-2]  # 最新已收盤

    consecutive = 0
    prior = []

    if last_closed["is_bullish"]:
        # 反轉K為陽線：往前數連跌（陰線）
        direction = "short_to_long"
        for i in range(-3, -len(candles)-1, -1):
            if abs(i) > len(candles):
                break
            c = candles[i]
            if not c["is_bullish"]:
                consecutive += 1
                prior.append(c)
            else:
                break
        if consecutive == 0:
            return 0, None, [], None
        return consecutive, last_closed, list(reversed(prior)), direction

    else:
        # 反轉K為陰線：往前數連漲（陽線）
        direction = "long_to_short"
        for i in range(-3, -len(candles)-1, -1):
            if abs(i) > len(candles):
                break
            c = candles[i]
            if c["is_bullish"]:
                consecutive += 1
                prior.append(c)
            else:
                break
        if consecutive == 0:
            return 0, None, [], None
        return consecutive, last_closed, list(reversed(prior)), direction

def analyze_candles(klines):
    """
    分析K線，計算連漲數量
    """
    if not klines or len(klines) < 4:
        return False, {"error": "K線資料不足"}
    
    # 解析所有K線
    candles = []
    for k in klines:
        open_price = float(k[1])
        close_price = float(k[4])
        high_price = float(k[2])
        low_price = float(k[3])
        volume = float(k[5])
        close_time = k[6]
        
        is_bullish = close_price > open_price
        change_pct = ((close_price - open_price) / open_price) * 100
        
        candles.append({
            "open": open_price,
            "close": close_price,
            "high": high_price,
            "low": low_price,
            "volume": volume,
            "close_time": close_time,
            "is_bullish": is_bullish,
            "change_pct": change_pct,
            "time_str": datetime.fromtimestamp(close_time/1000, tz=LOCAL_TZ).strftime('%H:%M')
        })
    
    # 偵測多空反轉
    consecutive_count, reversal_candle, prior_trend_candles, direction = detect_reversal(candles)

    # 條件: 連續同向 >= MIN_CONSECUTIVE 且 有反轉K線
    triggered = (consecutive_count >= MIN_CONSECUTIVE) and (reversal_candle is not None)

    return triggered, {
        "symbol": SYMBOL,
        "display_name": DISPLAY_NAME,
        "consecutive_count": consecutive_count,
        "prior_trend_candles": prior_trend_candles,
        "direction": direction,
        "reversal_candle": reversal_candle,
        "current_price": reversal_candle["close"] if reversal_candle else candles[-2]["close"],
        "analysis_time": datetime.now(LOCAL_TZ).strftime(f'%Y-%m-%d %H:%M:%S {LOCAL_TZ_LABEL}')
    }

def format_notification(info):
    """格式化 Telegram 通知訊息"""
    count = info["consecutive_count"]
    rev = info["reversal_candle"]
    prior = info["prior_trend_candles"]
    direction = info.get("direction")

    extra_line = ""

    if direction == "long_to_short":
        title = "多轉空 反轉訊號"
        trend_label = f"連漲 {count} 根"
        trend_lines = [f"  {c['time_str']} 漲 {c['change_pct']:+.2f}%" for c in prior]
        reversal_label = "反轉K線(收跌)"
        advice = "觀察空單機會或考慮減倉"

        # 總漲幅：從「連漲第一根的開盤」到「連漲最後一根的收盤」
        if prior:
            start_open = float(prior[0]['open'])
            end_close = float(prior[-1]['close'])
            total_rise_pct = (end_close - start_open) / start_open * 100 if start_open else 0.0
            extra_line = f"\n📈 總漲幅: {total_rise_pct:+.2f}% (從 {prior[0]['time_str']} 開 {start_open:.6f} → {prior[-1]['time_str']} 收 {end_close:.6f})"

    elif direction == "short_to_long":
        title = "空轉多 反轉訊號"
        trend_label = f"連跌 {count} 根"
        trend_lines = [f"  {c['time_str']} 跌 {c['change_pct']:+.2f}%" for c in prior]
        reversal_label = "反轉K線(收漲)"
        advice = "觀察多單機會或考慮回補"

        # 總跌幅：從「連跌第一根的開盤」到「連跌最後一根的收盤」
        if prior:
            start_open = float(prior[0]['open'])
            end_close = float(prior[-1]['close'])
            total_drop_pct = (end_close - start_open) / start_open * 100 if start_open else 0.0
            extra_line = f"\n📉 總跌幅: {total_drop_pct:+.2f}% (從 {prior[0]['time_str']} 開 {start_open:.6f} → {prior[-1]['time_str']} 收 {end_close:.6f})"

    else:
        title = "反轉訊號"
        trend_label = f"連續 {count} 根"
        trend_lines = []
        reversal_label = "反轉K線"
        advice = ""

    msg = f"""🔄 {info['display_name']} {title}

⏰ 時間: {info['analysis_time']}
📊 交易對: {info['display_name']} (15分K)
💰 當前價格: {info['current_price']:.6f}

📌 {trend_label}:
{chr(10).join(trend_lines)}{extra_line}

🔁 {reversal_label}:
  {rev['time_str']} {'漲' if rev['is_bullish'] else '跌'} {rev['change_pct']:+.2f}% (收 {rev['close']:.6f})

⚠️ 建議: {advice}
"""
    return msg

def send_telegram(message: str) -> bool:
    """用 openclaw message send 直接發到 Telegram 群組。

    之前用 `openclaw agent ...` 在 cron 環境會遇到 PATH 找不到 openclaw。
    這裡改成：指定 openclaw 絕對路徑 + message.send 直發目標群。
    """
    import subprocess

    try:
        msg_file = "/tmp/ren_signal_msg.txt"
        with open(msg_file, "w", encoding="utf-8") as f:
            f.write(message)

        # 直接把內容塞到 --message（避免把 "@file:..." 當成字串送出去）
        cmd = [
            OPENCLAW_BIN,
            "message",
            "send",
            "--channel",
            "telegram",
            "--target",
            TELEGRAM_TARGET,
            "--message",
            message,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"[OK] Telegram 已通知 {TELEGRAM_TARGET}")
            return True

        print(f"[WARN] Telegram 發送失敗: {result.stderr}")
        return False

    except Exception as e:
        print(f"[ERROR] Telegram 發送失敗: {e}")
        return False

def main():
    print(f"[{datetime.now()}] 開始監控 {DISPLAY_NAME} ({SYMBOL})...")

    # Binance 在分K剛收盤的前幾秒，有時 /klines 還沒完全更新到「最新已收盤K」。
    # 這裡用『預期收盤時間』做對齊：如果抓到的 last_closed_close_ms 落後於預期，就等待並重試。
    import time

    def interval_ms(interval: str) -> int:
        # 支援 15m/1m/1h 這類
        if interval.endswith('m'):
            return int(interval[:-1]) * 60_000
        if interval.endswith('h'):
            return int(interval[:-1]) * 3_600_000
        raise ValueError(f"unsupported interval: {interval}")

    ims = interval_ms(INTERVAL)

    # 目標：確保抓到「剛剛那個整點(0/15/30/45)開盤」的 15m K 線，並且已收盤。
    # 我們用 endTime 直接鎖定 expected_close_ms，避免 /klines 邊界延遲導致拿到上一根。
    klines = None
    for attempt in range(12):
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        expected_close_ms = (now_ms // ims) * ims - 1  # 例如 02:45:00.000 -> 02:44:59.999

        klines = get_klines(end_time_ms=expected_close_ms)
        if not klines:
            sys.exit(1)

        try:
            last_closed_close_ms = int(klines[-1][6])  # 用 endTime 取回的最後一根應該就是 expected_close
        except Exception:
            last_closed_close_ms = 0

        # 若回來的最後一根不是我們要的 close_time，代表 API 還沒準備好
        if last_closed_close_ms != expected_close_ms:
            print(f"[INFO] endTime 對齊失敗：got={last_closed_close_ms} expected={expected_close_ms}，等待 1s 重試...")
            time.sleep(1.0)
            continue

        # 若距離現在太近，補等到 >= 8 秒（避免剛寫入的瞬間）
        freshness_ms = now_ms - last_closed_close_ms
        if freshness_ms < 8000:
            wait_s = max(0.0, (8000 - freshness_ms) / 1000.0)
            print(f"[INFO] K線剛收盤({freshness_ms}ms)，等待 {wait_s:.1f}s 後重試抓取...")
            time.sleep(wait_s)
            continue

        break

    if not klines:
        sys.exit(1)

    # analyze_candles 預期最後一根是「進行中」，所以我們補抓 1 根進行中K
    # 做法：再抓一次不帶 endTime 的 klines，取其最後一根接到 klines 後面（避免 index -2 取錯）
    live = get_klines()
    if live and len(live) > 0:
        # 確保不要重複
        if int(live[-1][0]) != int(klines[-1][0]):
            klines = klines + [live[-1]]

    triggered, info = analyze_candles(klines)

    if triggered:
        dir_ = info.get('direction')
        count = info.get('consecutive_count')
        print(f"[ALERT] 反轉訊號觸發! dir={dir_} count={count}")
        print(json.dumps(info, indent=2, ensure_ascii=False, default=str))

        msg = format_notification(info)
        send_telegram(msg)
    else:
        consecutive = info.get('consecutive_count', 0)
        has_reversal = info.get('reversal_candle') is not None
        print(f"[INFO] 條件未觸發 (連續:{consecutive}, 反轉:{has_reversal})")
        print(f"[{datetime.now()}] 檢查完成，無訊號")

if __name__ == "__main__":
    main()
