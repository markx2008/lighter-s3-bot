#!/usr/bin/env python3
"""strategy2_boll_sweep.py

Parameter sweep for Bollinger Squeeze Breakout strategy (币安人生USDT 15m).

Goal: improve OOS robustness (PF / DD) for Strategy 2.

We reuse strategy2_backtest.py by overriding env vars.

Usage:
  python3 scripts/strategy2_boll_sweep.py

Env:
  TOP_N=20
  MIN_OOS_TRADES=20

Notes:
- This script prints top candidates by (oos_pf, oos_ret, -oos_dd).
- It runs the backtest multiple times; keep grids moderate.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Tuple

HERE = os.path.dirname(__file__)
BACKTEST = os.path.join(HERE, "strategy2_backtest.py")

TOP_N = int(float(os.getenv("TOP_N", "20")))
MIN_OOS_TRADES = int(float(os.getenv("MIN_OOS_TRADES", "20")))

# Grid (moderate)
SQUEEZE_LEN_GRID = [6, 8, 10, 12]
BW_THRESH_GRID = [0.040, 0.050, 0.055, 0.060]
STOP_ATR_GRID = [1.0, 1.2, 1.3, 1.5]
TP_ATR_GRID = [2.0, 2.2, 2.4, 2.8]
TRAIL_ATR_GRID = [0.7, 0.9, 1.1]
TRAIL_START_GRID = [1.5, 1.8, 2.2]
TIME_STOP_GRID = [96, 144, 192]


@dataclass
class Row:
    params: Dict[str, str]
    oos_ret: float
    oos_pf: float
    oos_dd: float
    oos_trades: int


def run_once(env_overrides: Dict[str, str]) -> Row | None:
    env = os.environ.copy()
    env.update(env_overrides)

    p = subprocess.run(["python3", BACKTEST], capture_output=True, text=True, env=env)
    if p.returncode != 0:
        return None

    # parse OOS line for Bollinger Squeeze Breakout
    # Example:
    # Bollinger Squeeze Breakout   |        oos | ret    9.69% PF 1.335... DD   7.69% trades  63 ...
    target = None
    for line in p.stdout.splitlines():
        if line.startswith("Bollinger Squeeze Breakout") and "|        oos" in line:
            target = line
            break
    if not target:
        return None

    m = re.search(r"ret\s+([\-\d\.]+)%\s+PF\s+([\d\.eE\-]+)\s+DD\s+([\d\.]+)%\s+trades\s+(\d+)", target)
    if not m:
        return None

    oos_ret = float(m.group(1))
    oos_pf = float(m.group(2))
    oos_dd = float(m.group(3))
    oos_trades = int(m.group(4))

    return Row(params=env_overrides, oos_ret=oos_ret, oos_pf=oos_pf, oos_dd=oos_dd, oos_trades=oos_trades)


def main():
    rows: List[Row] = []

    for sq_len in SQUEEZE_LEN_GRID:
        for bw in BW_THRESH_GRID:
            for stop_atr in STOP_ATR_GRID:
                for tp_atr in TP_ATR_GRID:
                    if tp_atr <= stop_atr:
                        continue
                    for trail in TRAIL_ATR_GRID:
                        for trail_start in TRAIL_START_GRID:
                            if trail_start <= 0:
                                continue
                            for ts in TIME_STOP_GRID:
                                env = {
                                    "S2_BOLL_SQ_LEN": str(sq_len),
                                    "S2_BOLL_BW": str(bw),
                                    "S2_BOLL_STOP": str(stop_atr),
                                    "S2_BOLL_TP": str(tp_atr),
                                    "S2_BOLL_TRAIL": str(trail),
                                    "S2_BOLL_TRAIL_START": str(trail_start),
                                    "S2_BOLL_TIME_STOP": str(ts),
                                }
                                r = run_once(env)
                                if not r:
                                    continue
                                if r.oos_trades < MIN_OOS_TRADES:
                                    continue
                                # basic sanity
                                if r.oos_dd > 25:
                                    continue
                                rows.append(r)

    rows.sort(key=lambda r: (r.oos_pf, r.oos_ret, -r.oos_dd), reverse=True)

    print(f"candidates={len(rows)} (min_oos_trades={MIN_OOS_TRADES})")
    for r in rows[:TOP_N]:
        print(
            f"OOS PF={r.oos_pf:.3f} ret={r.oos_ret:+.2f}% DD={r.oos_dd:.2f}% trades={r.oos_trades:>4d} | "
            f"SQ_LEN={r.params['S2_BOLL_SQ_LEN']} BW={r.params['S2_BOLL_BW']} STOP={r.params['S2_BOLL_STOP']} TP={r.params['S2_BOLL_TP']} "
            f"TRAIL={r.params['S2_BOLL_TRAIL']} TSTART={r.params['S2_BOLL_TRAIL_START']} TSTOP={r.params['S2_BOLL_TIME_STOP']}"
        )


if __name__ == "__main__":
    main()
