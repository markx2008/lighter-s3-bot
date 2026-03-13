#!/usr/bin/env python3
"""bitget_paper_broker.py

Paper broker (dry-run) to simulate Bitget order actions.

Purpose:
- Let us develop the trading bot logic (open/close/flip, sizing, notifications)
  before wiring real Bitget REST endpoints.

This broker keeps a small JSON state:
- position side/qty/entry
- last action time

No external API calls.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class Position:
    side: str  # 'long' | 'short'
    qty: float
    entry: float


class PaperBroker:
    def __init__(self, state_path: str):
        self.state_path = state_path
        self.state = self._load()

    def _load(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    def get_position(self) -> Optional[Position]:
        p = self.state.get("position")
        if not p:
            return None
        return Position(side=p["side"], qty=float(p["qty"]), entry=float(p["entry"]))

    def open_position(self, side: str, qty: float, price: float) -> None:
        self.state["position"] = {"side": side, "qty": qty, "entry": price}
        self._save()

    def close_position(self, price: float) -> Optional[dict]:
        pos = self.get_position()
        if not pos:
            return None
        pnl = (price - pos.entry) * pos.qty if pos.side == "long" else (pos.entry - price) * pos.qty
        self.state["position"] = None
        self._save()
        return {"side": pos.side, "qty": pos.qty, "entry": pos.entry, "exit": price, "pnl": pnl}
