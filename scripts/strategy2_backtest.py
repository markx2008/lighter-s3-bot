#!/usr/bin/env python3
"""strategy2_backtest.py

Unified backtest for the strategy2 family (non-RSI) on 币安人生USDT 15m.

Each signal avoids RSI and reuses our 20x isolated fixed-risk model (RISK_PCT, MAX_MARGIN_PCT, LIQ_BUFFER_PCT, MIN_ATR_PCT).
Outputs per-strategy stats plus trades.csv for the full sample and in/out-of-sample splits.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import requests

from strategy_lab import Candle, atr, ema, sma, utc_ms_to_str

SYMBOL = "币安人生USDT"
INTERVAL = "15m"
START_UTC = datetime(2025, 10, 20, 0, 0, 0, tzinfo=timezone.utc)
BINANCE_FAPI = "https://fapi.binance.com"

INITIAL_EQUITY = float(os.getenv("INITIAL_EQUITY", "10000"))
TAKER_FEE = float(os.getenv("TAKER_FEE", "0.0004"))
SLIPPAGE = float(os.getenv("SLIPPAGE", "0.0001"))

LEVERAGE = int(float(os.getenv("LEVERAGE", "20")))
RISK_PCT = float(os.getenv("RISK_PCT", "0.01"))
MAX_MARGIN_PCT = float(os.getenv("MAX_MARGIN_PCT", "0.20"))
LIQ_BUFFER_PCT = float(os.getenv("LIQ_BUFFER_PCT", "0.005"))
MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.012"))

EXPORT_DIR = "exports"
TRADES_CSV = os.path.join(EXPORT_DIR, "strategy2_trades.csv")
SUMMARY_CSV = os.path.join(EXPORT_DIR, "strategy2_summary.csv")


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
        try:
            resp = requests.get(f"{BINANCE_FAPI}/fapi/v1/klines", params=params, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise SystemExit(f"Failed to fetch klines: {exc}") from exc
        data = resp.json()
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
        time.sleep(0.25)
    dedup: Dict[int, Candle] = {}
    for c in out:
        dedup[c.open_time_ms] = c
    return [dedup[k] for k in sorted(dedup.keys())]


def rolling_std(values: Sequence[float], window: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if window <= 0 or len(values) < window:
        return out
    sum_ = 0.0
    sum_sq = 0.0
    for i, v in enumerate(values):
        sum_ += v
        sum_sq += v * v
        if i >= window:
            old = values[i - window]
            sum_ -= old
            sum_sq -= old * old
        if i >= window - 1:
            mean = sum_ / window
            variance = sum_sq / window - mean * mean
            out[i] = math.sqrt(max(0.0, variance))
    return out


def rolling_extreme(values: Sequence[float], window: int, use_max: bool) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if window <= 0 or len(values) < window:
        return out
    for i in range(len(values)):
        if i >= window:
            window_values = values[i - window : i]
            if not window_values:
                continue
            out[i] = max(window_values) if use_max else min(window_values)
    return out


def calc_vwap(candles: Sequence[Candle], idx: int, window: int) -> Optional[float]:
    if window <= 0:
        return None
    start = max(0, idx - window + 1)
    price_vol = 0.0
    vol_sum = 0.0
    for c in candles[start : idx + 1]:
        tp = (c.high + c.low + c.close) / 3.0
        price_vol += tp * c.volume
        vol_sum += c.volume
    if vol_sum <= 0:
        return None
    return price_vol / vol_sum


@dataclass
class EntryPlan:
    side: int
    stop_atr: float
    tp_atr: float
    trailing_atr: Optional[float]
    trail_start_atr: Optional[float]
    time_stop: Optional[int]
    reason: str


StrategyEntryFn = Callable[
    [int, Sequence[Candle], Dict[str, List[Optional[float]]], Dict[str, Any]], Optional[EntryPlan]
]


@dataclass
class StrategyDefinition:
    name: str
    description: str
    lookback: int
    entry_fn: StrategyEntryFn
    params: Dict[str, Any] = None


@dataclass
class TradeRecord:
    strategy: str
    dataset: str
    entry_time_ms: int
    exit_time_ms: int
    side: str
    entry_price: float
    exit_price: float
    pnl_usdt: float
    reason: str


def prepare_indicators(candles: Sequence[Candle]) -> Dict[str, List[Optional[float]]]:
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    volumes = [c.volume for c in candles]

    atr14 = atr(list(candles), 14)
    sma20 = sma(closes, 20)
    ema21 = ema(closes, 21)
    ema55 = ema(closes, 55)
    vol_sma20 = sma(volumes, 20)
    std20 = rolling_std(closes, 20)

    bandwidth: List[Optional[float]] = [None] * len(candles)
    bb_upper: List[Optional[float]] = [None] * len(candles)
    bb_lower: List[Optional[float]] = [None] * len(candles)
    for i in range(len(candles)):
        mid = sma20[i]
        dev = std20[i]
        if mid is not None and dev is not None and mid != 0:
            upper = mid + 2.0 * dev
            lower = mid - 2.0 * dev
            bb_upper[i] = upper
            bb_lower[i] = lower
            bandwidth[i] = (upper - lower) / mid

    return {
        "closes": closes,
        "highs": highs,
        "lows": lows,
        "volumes": volumes,
        "atr": atr14,
        "sma20": sma20,
        "ema21": ema21,
        "ema55": ema55,
        "vol_sma20": vol_sma20,
        "std20": std20,
        "bandwidth": bandwidth,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "donchian_high_20": rolling_extreme(highs, 20, True),
        "donchian_low_20": rolling_extreme(lows, 20, False),
        "range_high_12": rolling_extreme(highs, 12, True),
        "range_low_12": rolling_extreme(lows, 12, False),
    }


# Strategy specific constants
DONCHIAN_LEN = 20
DONCHIAN_STOP = 1.2
DONCHIAN_TP = 2.0
DONCHIAN_TRAIL_ATR = 0.8
DONCHIAN_TRAIL_START = 1.5
DONCHIAN_TIME_STOP = 120

VOL_RANGE = 12
VOL_STOP = 1.0
VOL_TP = 1.8
VOL_TRAIL = 0.6
VOL_TRAIL_START = 1.3
VOL_TIME_STOP = 72
VOL_VOLUME_FACTOR = 1.35

BOLLINGER_LEN = int(float(os.getenv("S2_BOLL_LEN", "20")))
# Bollinger squeeze breakout (tuned)
BOLLINGER_SQUEEZE_LEN = int(float(os.getenv("S2_BOLL_SQ_LEN", "12")))
BOLLINGER_BW_THRESH = float(os.getenv("S2_BOLL_BW", "0.05"))
BOLLINGER_STOP = float(os.getenv("S2_BOLL_STOP", "1.0"))
BOLLINGER_TP = float(os.getenv("S2_BOLL_TP", "2.2"))
BOLLINGER_TRAIL = float(os.getenv("S2_BOLL_TRAIL", "0.7"))
BOLLINGER_TRAIL_START = float(os.getenv("S2_BOLL_TRAIL_START", "2.2"))
BOLLINGER_TIME_STOP = int(float(os.getenv("S2_BOLL_TIME_STOP", "96")))
BOLLINGER_VOL_FACTOR = float(os.getenv("S2_BOLL_VOL_FACTOR", "1.20"))  # 突破K量能 >= volSMA20 * factor
BOLLINGER_EMA55_FILTER = os.getenv("S2_BOLL_EMA55", "0") not in ("0", "false", "False")

# breakout magnitude filter: require close to exceed band by X * ATR
BOLLINGER_BREAK_ATR = float(os.getenv("S2_BOLL_BREAK_ATR", "0.20"))

# bandwidth expansion filter: require bandwidth slope positive (expanding) over N bars
BOLLINGER_BW_SLOPE_LEN = int(float(os.getenv("S2_BOLL_BW_SLOPE_LEN", "6")))
BOLLINGER_BW_SLOPE_MIN = float(os.getenv("S2_BOLL_BW_SLOPE_MIN", "0.0"))

VWAP_LEN = 48
VWAP_BIAS = 0.012
VWAP_STOP = 1.0
VWAP_TP = 1.7
VWAP_TRAIL = 0.5
VWAP_TRAIL_START = 1.0
VWAP_TIME_STOP = 60


def donchian_entry(
    idx: int,
    candles: Sequence[Candle],
    indicators: Dict[str, List[Optional[float]]],
    _: Dict[str, Any],
) -> Optional[EntryPlan]:
    if idx < DONCHIAN_LEN:
        return None
    close = candles[idx].close
    range_high = indicators["donchian_high_20"][idx]
    range_low = indicators["donchian_low_20"][idx]
    atr_val = indicators["atr"][idx]
    ema55 = indicators["ema55"][idx]
    if range_high is None or range_low is None or atr_val is None or atr_val <= 0:
        return None
    atr_pct = atr_val / close if close > 0 else 0
    if atr_pct < MIN_ATR_PCT:
        return None
    if close > range_high and (ema55 is None or close >= ema55):
        return EntryPlan(1, DONCHIAN_STOP, DONCHIAN_TP, DONCHIAN_TRAIL_ATR, DONCHIAN_TRAIL_START, DONCHIAN_TIME_STOP, "donchian_breakout")
    if close < range_low and (ema55 is None or close <= ema55):
        return EntryPlan(-1, DONCHIAN_STOP, DONCHIAN_TP, DONCHIAN_TRAIL_ATR, DONCHIAN_TRAIL_START, DONCHIAN_TIME_STOP, "donchian_breakout")
    return None


def volume_volatility_entry(
    idx: int,
    candles: Sequence[Candle],
    indicators: Dict[str, List[Optional[float]]],
    _: Dict[str, Any],
) -> Optional[EntryPlan]:
    if idx < VOL_RANGE:
        return None
    close = candles[idx].close
    atr_val = indicators["atr"][idx]
    range_high = indicators["range_high_12"][idx]
    range_low = indicators["range_low_12"][idx]
    volume = indicators["volumes"][idx]
    vol_sma = indicators["vol_sma20"][idx]
    sma20 = indicators["sma20"][idx]
    if any(v is None for v in (atr_val, range_high, range_low, volume, vol_sma, sma20)):
        return None
    if atr_val <= 0 or vol_sma <= 0:
        return None
    atr_pct = atr_val / close if close > 0 else 0
    if atr_pct < MIN_ATR_PCT:
        return None
    if volume >= vol_sma * VOL_VOLUME_FACTOR and close > range_high and close >= sma20:
        return EntryPlan(1, VOL_STOP, VOL_TP, VOL_TRAIL, VOL_TRAIL_START, VOL_TIME_STOP, "vol_breakout")
    if volume >= vol_sma * VOL_VOLUME_FACTOR and close < range_low and close <= sma20:
        return EntryPlan(-1, VOL_STOP, VOL_TP, VOL_TRAIL, VOL_TRAIL_START, VOL_TIME_STOP, "vol_breakout")
    return None


def has_recent_squeeze(
    idx: int, bandwidth: List[Optional[float]], length: int, thresh: float
) -> bool:
    if length <= 0 or idx < length:
        return False
    for j in range(idx - length, idx):
        if j < 0 or bandwidth[j] is None or bandwidth[j] > thresh:
            return False
    return True


def bollinger_entry(
    idx: int,
    candles: Sequence[Candle],
    indicators: Dict[str, List[Optional[float]]],
    params: Dict[str, Any],
) -> Optional[EntryPlan]:
    # allow per-strategy overrides via params
    boll_len = int(params.get("boll_len", BOLLINGER_LEN))
    sq_len = int(params.get("squeeze_len", BOLLINGER_SQUEEZE_LEN))
    bw_thr = float(params.get("bw_thresh", BOLLINGER_BW_THRESH))
    stop_atr = float(params.get("stop_atr", BOLLINGER_STOP))
    tp_atr = float(params.get("tp_atr", BOLLINGER_TP))
    trail_atr = float(params.get("trail_atr", BOLLINGER_TRAIL))
    trail_start = float(params.get("trail_start_atr", BOLLINGER_TRAIL_START))
    time_stop = int(params.get("time_stop", BOLLINGER_TIME_STOP))
    vol_factor = float(params.get("vol_factor", BOLLINGER_VOL_FACTOR))
    break_atr = float(params.get("break_atr", BOLLINGER_BREAK_ATR))
    bw_slope_len = int(params.get("bw_slope_len", BOLLINGER_BW_SLOPE_LEN))
    bw_slope_min = float(params.get("bw_slope_min", BOLLINGER_BW_SLOPE_MIN))

    bandwidth = indicators["bandwidth"]
    if idx < boll_len or bandwidth[idx] is None:
        return None
    if not has_recent_squeeze(idx, bandwidth, sq_len, bw_thr):
        return None

    # bandwidth expansion confirmation (avoid flat squeezes with fake breakouts)
    if bw_slope_len > 1 and idx >= bw_slope_len and bandwidth[idx] is not None and bandwidth[idx - bw_slope_len] is not None:
        slope = (bandwidth[idx] - bandwidth[idx - bw_slope_len]) / bw_slope_len
        if slope < bw_slope_min:
            return None
    close = candles[idx].close
    atr_val = indicators["atr"][idx]
    upper = indicators["bb_upper"][idx]
    lower = indicators["bb_lower"][idx]
    vol = indicators["volumes"][idx]
    vol_sma = indicators["vol_sma20"][idx]
    ema55 = indicators["ema55"][idx]
    if atr_val is None or atr_val <= 0 or upper is None or lower is None:
        return None
    # 量能濾網：沒有 vol_sma 就跳過濾網；有的話要求突破K量能放大
    if vol_sma is not None and vol_sma > 0 and vol is not None and vol_factor > 0:
        if vol < vol_sma * vol_factor:
            return None
    atr_pct = atr_val / close if close > 0 else 0
    if atr_pct < MIN_ATR_PCT:
        return None
    if close > upper:
        if BOLLINGER_EMA55_FILTER and ema55 is not None and close < ema55:
            return None
        # breakout magnitude filter
        if break_atr > 0 and atr_val is not None and close < (upper + break_atr * atr_val):
            return None
        return EntryPlan(1, stop_atr, tp_atr, trail_atr, trail_start, time_stop, "bollinger_squeeze")
    if close < lower:
        if BOLLINGER_EMA55_FILTER and ema55 is not None and close > ema55:
            return None
        if break_atr > 0 and atr_val is not None and close > (lower - break_atr * atr_val):
            return None
        return EntryPlan(-1, stop_atr, tp_atr, trail_atr, trail_start, time_stop, "bollinger_squeeze")
    return None


def vwap_bias_entry(
    idx: int,
    candles: Sequence[Candle],
    indicators: Dict[str, List[Optional[float]]],
    _: Dict[str, Any],
) -> Optional[EntryPlan]:
    if idx < VWAP_LEN:
        return None
    close = candles[idx].close
    atr_val = indicators["atr"][idx]
    ema21 = indicators["ema21"][idx]
    vwap_value = calc_vwap(candles, idx, VWAP_LEN)
    if atr_val is None or atr_val <= 0 or vwap_value is None or vwap_value <= 0:
        return None
    atr_pct = atr_val / close if close > 0 else 0
    if atr_pct < MIN_ATR_PCT:
        return None
    bias = (close - vwap_value) / vwap_value
    if bias > VWAP_BIAS and (ema21 is None or close >= ema21):
        return EntryPlan(1, VWAP_STOP, VWAP_TP, VWAP_TRAIL, VWAP_TRAIL_START, VWAP_TIME_STOP, "vwap_bias")
    if bias < -VWAP_BIAS and (ema21 is None or close <= ema21):
        return EntryPlan(-1, VWAP_STOP, VWAP_TP, VWAP_TRAIL, VWAP_TRAIL_START, VWAP_TIME_STOP, "vwap_bias")
    return None


STRATEGIES: List[StrategyDefinition] = [
    StrategyDefinition("Donchian Trend Breakout", "20-bar Donchian breakout with ATR stops/trailling", DONCHIAN_LEN, donchian_entry, params={}),
    StrategyDefinition("Volume+Volatility Breakout", "Volume spike + 12-bar range breakout", VOL_RANGE, volume_volatility_entry, params={}),
    StrategyDefinition(
        "Bollinger Squeeze Breakout",
        "Low bandwidth followed by band breakout",
        BOLLINGER_LEN,
        bollinger_entry,
        params={
            "boll_len": BOLLINGER_LEN,
            "squeeze_len": BOLLINGER_SQUEEZE_LEN,
            "bw_thresh": BOLLINGER_BW_THRESH,
            "stop_atr": BOLLINGER_STOP,
            "tp_atr": BOLLINGER_TP,
            "trail_atr": BOLLINGER_TRAIL,
            "trail_start_atr": BOLLINGER_TRAIL_START,
            "time_stop": BOLLINGER_TIME_STOP,
            "vol_factor": BOLLINGER_VOL_FACTOR,
            "break_atr": BOLLINGER_BREAK_ATR,
            "bw_slope_len": BOLLINGER_BW_SLOPE_LEN,
            "bw_slope_min": BOLLINGER_BW_SLOPE_MIN,
        },
    ),
    StrategyDefinition("VWAP Bias Re-entry", "VWAP deviation with trend confirmation", VWAP_LEN, vwap_bias_entry, params={}),
]


def backtest_strategy(
    candles: Sequence[Candle],
    indicators: Dict[str, List[Optional[float]]],
    strategy: StrategyDefinition,
    dataset_label: str,
) -> Tuple[Dict[str, Any], List[TradeRecord]]:
    trades: List[TradeRecord] = []
    equity = INITIAL_EQUITY
    peak = equity
    max_dd = 0.0
    pos = 0
    qty = 0.0
    entry_price = 0.0
    entry_idx = 0
    entry_time = 0
    stop_price = 0.0
    base_stop = 0.0
    tp_price = 0.0
    best_price = 0.0
    trail_triggered = False
    current_plan: Optional[EntryPlan] = None

    def fee(notional: float) -> float:
        return notional * TAKER_FEE

    def mark_to_market(price: float) -> None:
        nonlocal peak, max_dd
        cur = equity
        if pos != 0:
            if pos == 1:
                cur = equity + qty * (price - entry_price)
            else:
                cur = equity + qty * (entry_price - price)
        peak = max(peak, cur)
        dd = (peak - cur) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    start_idx = max(strategy.lookback, 1)
    last_idx = len(candles) - 1
    for i in range(start_idx, max(start_idx, last_idx)):
        if i >= len(candles) - 1:
            break
        bar = candles[i]
        next_bar = candles[i + 1]
        mark_to_market(bar.close)
        atr_val = indicators.get("atr", [None] * len(candles))[i]
        if atr_val is None or atr_val <= 0:
            continue
        if pos != 0 and current_plan is not None:
            if pos == 1:
                best_price = max(best_price, bar.high)
            else:
                best_price = min(best_price, bar.low)
            if current_plan.trail_start_atr and atr_val > 0:
                move = (best_price - entry_price) / atr_val if pos == 1 else (entry_price - best_price) / atr_val
                if move >= current_plan.trail_start_atr:
                    trail_triggered = True
            if current_plan.trailing_atr and trail_triggered and atr_val > 0:
                trail_stop = (
                    best_price - current_plan.trailing_atr * atr_val if pos == 1 else best_price + current_plan.trailing_atr * atr_val
                )
                if pos == 1 and trail_stop > stop_price:
                    stop_price = trail_stop
                elif pos == -1 and trail_stop < stop_price:
                    stop_price = trail_stop
            stop_hit = bar.low <= stop_price if pos == 1 else bar.high >= stop_price
            exit_reason: Optional[str] = None
            exit_price = 0.0
            if stop_hit:
                exit_price = stop_price * (1 - SLIPPAGE) if pos == 1 else stop_price * (1 + SLIPPAGE)
                if current_plan.trailing_atr and trail_triggered and ((pos == 1 and stop_price > base_stop) or (pos == -1 and stop_price < base_stop)):
                    exit_reason = "trail"
                else:
                    exit_reason = "stop"
            elif (bar.high >= tp_price if pos == 1 else bar.low <= tp_price):
                exit_price = tp_price * (1 - SLIPPAGE) if pos == 1 else tp_price * (1 + SLIPPAGE)
                exit_reason = "tp"
            elif current_plan.time_stop and (i - entry_idx) >= current_plan.time_stop:
                exit_price = bar.close * (1 - SLIPPAGE) if pos == 1 else bar.close * (1 + SLIPPAGE)
                exit_reason = "time"
            if exit_reason is not None:
                exit_time = bar.close_time_ms
                notional = qty * exit_price
                eq_before_fee = equity
                equity -= fee(notional)
                pnl = qty * (exit_price - entry_price) if pos == 1 else qty * (entry_price - exit_price)
                equity += pnl
                trades.append(
                    TradeRecord(
                        strategy=strategy.name,
                        dataset=dataset_label,
                        entry_time_ms=entry_time,
                        exit_time_ms=exit_time,
                        side="long" if pos == 1 else "short",
                        entry_price=entry_price,
                        exit_price=exit_price,
                        pnl_usdt=pnl,
                        reason=exit_reason,
                    )
                )
                pos = 0
                qty = 0.0
                current_plan = None
                base_stop = 0.0
                stop_price = 0.0
                tp_price = 0.0
                best_price = 0.0
                trail_triggered = False
                entry_price = 0.0
                entry_time = 0
                entry_idx = 0
                continue
        if pos == 0:
            plan = strategy.entry_fn(i, candles, indicators, strategy.params or {})
            if plan is None:
                continue
            open_price = next_bar.open
            entry_px = open_price * (1 + SLIPPAGE) if plan.side == 1 else open_price * (1 - SLIPPAGE)
            if entry_px <= 0:
                continue
            if plan.side == 1:
                sl = entry_px - plan.stop_atr * atr_val
                tp = entry_px + plan.tp_atr * atr_val
            else:
                sl = entry_px + plan.stop_atr * atr_val
                tp = entry_px - plan.tp_atr * atr_val
            stop_dist = abs(entry_px - sl)
            if stop_dist <= 0:
                continue
            atr_pct = atr_val / entry_px if entry_px > 0 else 0
            if atr_pct < MIN_ATR_PCT:
                continue
            liq_price = entry_px * (1 - 1 / LEVERAGE) if plan.side == 1 else entry_px * (1 + 1 / LEVERAGE)
            if plan.side == 1:
                if sl <= liq_price * (1 + LIQ_BUFFER_PCT):
                    continue
            else:
                if sl >= liq_price * (1 - LIQ_BUFFER_PCT):
                    continue
            risk_usdt = equity * RISK_PCT
            if risk_usdt <= 0:
                continue
            qty_calc = risk_usdt / stop_dist
            max_notional = equity * MAX_MARGIN_PCT * LEVERAGE
            if max_notional <= 0:
                continue
            notional = qty_calc * entry_px
            if notional > max_notional:
                notional = max_notional
                qty_calc = notional / entry_px
            if qty_calc <= 0:
                continue
            equity -= fee(qty_calc * entry_px)
            pos = plan.side
            qty = qty_calc
            entry_price = entry_px
            base_stop = sl
            stop_price = sl
            tp_price = tp
            entry_idx = i + 1
            entry_time = next_bar.open_time_ms
            best_price = entry_px
            trail_triggered = False
            current_plan = plan
    mark_to_market(candles[-1].close)
    if pos != 0:
        last = candles[-1]
        exit_px = last.close * (1 - SLIPPAGE) if pos == 1 else last.close * (1 + SLIPPAGE)
        exit_time = last.close_time_ms
        notional = qty * exit_px
        equity -= fee(notional)
        pnl = qty * (exit_px - entry_price) if pos == 1 else qty * (entry_price - exit_px)
        equity += pnl
        trades.append(
            TradeRecord(
                strategy=strategy.name,
                dataset=dataset_label,
                entry_time_ms=entry_time,
                exit_time_ms=exit_time,
                side="long" if pos == 1 else "short",
                entry_price=entry_price,
                exit_price=exit_px,
                pnl_usdt=pnl,
                reason="eod",
            )
        )
        pos = 0
        qty = 0.0
        current_plan = None
    wins = [t for t in trades if t.pnl_usdt > 0]
    losses = [t for t in trades if t.pnl_usdt <= 0]
    win_rate = (len(wins) / len(trades) * 100) if trades else 0.0
    avg_pnl = (sum(t.pnl_usdt for t in trades) / len(trades)) if trades else 0.0
    gross_win = sum(t.pnl_usdt for t in wins)
    gross_loss = -sum(t.pnl_usdt for t in losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    stats: Dict[str, Any] = {
        "strategy": strategy.name,
        "dataset": dataset_label,
        "return_pct": (equity / INITIAL_EQUITY - 1) * 100,
        "final_equity": equity,
        "max_drawdown_pct": max_dd * 100,
        "trades": len(trades),
        "win_rate_pct": win_rate,
        "avg_pnl": avg_pnl,
        "profit_factor": pf,
        "taker_fee": TAKER_FEE,
        "slippage": SLIPPAGE,
    }
    return stats, trades


SUMMARY_HEADER = [
    "strategy",
    "dataset",
    "return_pct",
    "final_equity",
    "max_drawdown_pct",
    "profit_factor",
    "trades",
    "win_rate_pct",
    "avg_pnl",
    "taker_fee",
    "slippage",
]


def print_summary_line(stats: Dict[str, Any]) -> None:
    print(
        f"{stats['strategy'][:28]:28} | {stats['dataset']:>10} | ret {stats['return_pct']:7.2f}% "
        f"PF {stats['profit_factor'] if math.isfinite(stats['profit_factor']) else 'inf':>6} "
        f"DD {stats['max_drawdown_pct']:6.2f}% trades {stats['trades']:3d} "
        f"win {stats['win_rate_pct']:6.2f}% avg {stats['avg_pnl']:7.2f}"
    )


def write_summary_csv(rows: List[Dict[str, Any]]) -> None:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    with open(SUMMARY_CSV, "w", encoding="utf-8") as f:
        f.write(",".join(SUMMARY_HEADER) + "\n")
        for row in rows:
            values = [
                row["strategy"],
                row["dataset"],
                f"{row['return_pct']:.2f}",
                f"{row['final_equity']:.2f}",
                f"{row['max_drawdown_pct']:.2f}",
                f"{row['profit_factor'] if math.isfinite(row['profit_factor']) else 'inf'}",
                str(row["trades"]),
                f"{row['win_rate_pct']:.2f}",
                f"{row['avg_pnl']:.2f}",
                f"{row['taker_fee']:.5f}",
                f"{row['slippage']:.5f}",
            ]
            f.write(",".join(values) + "\n")


def write_trades_csv(trades: List[TradeRecord]) -> None:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    with open(TRADES_CSV, "w", encoding="utf-8") as f:
        headers = ["strategy", "dataset", "side", "entry_time", "exit_time", "entry_price", "exit_price", "pnl_usdt", "reason"]
        f.write(",".join(headers) + "\n")
        for tr in trades:
            values = [
                tr.strategy,
                tr.dataset,
                tr.side,
                utc_ms_to_str(tr.entry_time_ms),
                utc_ms_to_str(tr.exit_time_ms),
                f"{tr.entry_price:.6f}",
                f"{tr.exit_price:.6f}",
                f"{tr.pnl_usdt:.2f}",
                tr.reason,
            ]
            f.write(",".join(values) + "\n")


def main() -> None:
    now = datetime.now(timezone.utc)
    candles = fetch_klines(ms(START_UTC), ms(now))
    if not candles:
        raise SystemExit("No candles fetched")
    split_idx = max(int(len(candles) * 0.7), 4)
    if split_idx >= len(candles) - 2:
        split_idx = max(len(candles) - 2, 4)
    print(f"Fetched {len(candles)} candles ({SYMBOL} {INTERVAL}) from 2025-10-20 to {utc_ms_to_str(candles[-1].close_time_ms)}")
    summary_rows: List[Dict[str, Any]] = []
    print("\n== In-sample / Out-of-sample evaluation ==")
    for label, subset in (("in-sample", candles[:split_idx]), ("oos", candles[split_idx:])):
        if len(subset) < 100:
            print(f"Skipping {label} (only {len(subset)} bars)")
            continue
        indicators = prepare_indicators(subset)
        for strategy in STRATEGIES:
            stats, _ = backtest_strategy(subset, indicators, strategy, label)
            summary_rows.append(stats)
            print_summary_line(stats)
    print("\n== Full-sample runs for export(s) ==")
    full_indicators = prepare_indicators(candles)
    final_trades: List[TradeRecord] = []
    for strategy in STRATEGIES:
        stats, trades = backtest_strategy(candles, full_indicators, strategy, "full")
        summary_rows.append(stats)
        final_trades.extend(trades)
        print_summary_line(stats)
    write_summary_csv(summary_rows)
    write_trades_csv(final_trades)
    print(f"\nSummary written to {SUMMARY_CSV}")
    print(f"Trades written to {TRADES_CSV}")


if __name__ == "__main__":
    main()
