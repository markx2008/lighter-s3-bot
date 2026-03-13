from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class PaperPosition:
    side: str
    qty: float
    entry: float


class PaperBroker:
    def __init__(self, state_path: str):
        self.state_path = state_path
        self.state = self._load()

    def _load(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return {}

    def _save(self) -> None:
        directory = os.path.dirname(self.state_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp_path = self.state_path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(self.state, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.state_path)

    def get_position(self) -> Optional[PaperPosition]:
        position = self.state.get("position")
        if not position:
            return None
        return PaperPosition(side=position["side"], qty=float(position["qty"]), entry=float(position["entry"]))

    def open_position(self, side: str, qty: float, price: float) -> None:
        self.state["position"] = {"side": side, "qty": qty, "entry": price}
        self._save()

    def close_position(self, price: float) -> Optional[dict]:
        position = self.get_position()
        if not position:
            return None
        pnl = (price - position.entry) * position.qty if position.side == "long" else (position.entry - price) * position.qty
        self.state["position"] = None
        self._save()
        return {"side": position.side, "qty": position.qty, "entry": position.entry, "exit": price, "pnl": pnl}
