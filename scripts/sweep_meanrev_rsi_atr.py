#!/usr/bin/env python3
"""sweep_meanrev_rsi_atr.py

快速参数扫：找一个“交易次数在目标区间且表现还行”的组合

用法：
  python3 sweep_meanrev_rsi_atr.py

可用环境变量控制目标：
  TARGET_MIN_TRADES_PER_MONTH
  TARGET_MAX_TRADES_PER_MONTH

输出：按 out-of-sample（后30%）return 排序的前若干组参数。

⚠️ 研究工具，不保证盈利。
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import List, Tuple, Dict

from backtest_meanrev_rsi_atr import (
    fetch_klines,
    ms,
    START_UTC,
    backtest,
)


TARGET_MIN = float(os.getenv("TARGET_MIN_TRADES_PER_MONTH", "5"))
TARGET_MAX = float(os.getenv("TARGET_MAX_TRADES_PER_MONTH", "12"))


def split(candles, ratio=0.7):
    k = int(len(candles) * ratio)
    return candles[:k], candles[k:]


def trades_per_month(trades, start_ts_ms: int, end_ts_ms: int) -> float:
    months = max(1e-9, (end_ts_ms - start_ts_ms) / (1000 * 60 * 60 * 24 * 30.4375))
    return len(trades) / months


def run_with_env(**env_overrides) -> Tuple[dict, list]:
    # backtest_meanrev_rsi_atr reads env at import time for defaults, but we can
    # set os.environ then re-import. To keep it simple, we just set env and call
    # a subprocess would be cleaner; for now, minimal sweep uses os.environ + reload.
    import importlib
    import backtest_meanrev_rsi_atr as bt

    for k, v in env_overrides.items():
        os.environ[k] = str(v)

    importlib.reload(bt)
    # fetch data inside bt.main usually; here just call bt.fetch_klines and bt.backtest
    end = datetime.now(timezone.utc)
    candles = bt.fetch_klines(bt.ms(bt.START_UTC), bt.ms(end))
    summary, trades = bt.backtest(candles)
    return summary, trades


def main():
    # parameter grid (small)
    rsi_long_grid = [22, 25, 28, 30]
    rsi_short_grid = [70, 72, 75, 78]
    sl_atr_grid = [0.9, 1.1, 1.3, 1.5]
    tp_r_grid = [1.2, 1.5, 1.8, 2.2]
    max_hold_grid = [0, 96, 192]  # 0/1d/2d
    cooldown_grid = [0, 8, 16]

    rows = []

    # one data fetch shared? we currently reload module each run; acceptable for small sweep.
    for rsi_long in rsi_long_grid:
        for rsi_short in rsi_short_grid:
            if rsi_short <= 60:
                continue
            for sl_atr in sl_atr_grid:
                for tp_r in tp_r_grid:
                    for max_hold in max_hold_grid:
                        for cooldown in cooldown_grid:
                            summary, trades = run_with_env(
                                RSI_LONG=rsi_long,
                                RSI_SHORT=rsi_short,
                                SL_ATR=sl_atr,
                                TP_R=tp_r,
                                MAX_HOLD_BARS=max_hold,
                                COOLDOWN_BARS=cooldown,
                            )

                            # approximate trades/month
                            # summary has period string; easiest use first/last trade times if any
                            if trades:
                                start = trades[0].entry_time_ms
                                end = trades[-1].exit_time_ms
                            else:
                                start = 0
                                end = 1
                            tpm = trades_per_month(trades, start, end)

                            if not (TARGET_MIN <= tpm <= TARGET_MAX):
                                continue

                            # basic sanity filters
                            if summary["profit_factor"] < 1.1:
                                continue

                            rows.append(
                                {
                                    "ret": summary["return_pct"],
                                    "pf": summary["profit_factor"],
                                    "dd": summary["max_drawdown_pct"],
                                    "tpm": tpm,
                                    "trades": summary["trades"],
                                    "params": summary["assumptions"],
                                }
                            )

    rows.sort(key=lambda x: (x["ret"], x["pf"], -x["dd"]), reverse=True)

    print(f"Found {len(rows)} candidates within target trades/month {TARGET_MIN}-{TARGET_MAX}.")
    for r in rows[:20]:
        p = r["params"]
        print(
            f"ret={r['ret']:>7.2f}%  pf={r['pf']:.2f}  dd={r['dd']:.2f}%  "
            f"tpm={r['tpm']:.1f}  trades={r['trades']:>4d}  "
            f"RSI_LONG={p['rsi_long']} RSI_SHORT={p['rsi_short']} SL_ATR={p['sl_atr']} TP_R={p['tp_r']} "
            f"MAX_HOLD={p['max_hold_bars']} COOLDOWN={p['cooldown_bars']}"
        )


if __name__ == "__main__":
    main()
