from __future__ import annotations

import csv
import os
import time
from typing import Any


class TradeLogger:
    def __init__(self, path: str):
        self.path = path

    def _ensure_dir(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def append(self, row: dict[str, Any]) -> None:
        self._ensure_dir()
        exists = os.path.exists(self.path)
        # Normalize
        row = {**row, "ts": int(time.time())}
        with open(self.path, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            if not exists:
                writer.writeheader()
            writer.writerow(row)
