from __future__ import annotations

import json
import os
from typing import Any

from standx.domain.models import PositionState


class JsonStateStore:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return {"version": 1}

    def save(self, state: dict[str, Any]) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temp_path = self.path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.path)

    def load_position(self) -> PositionState | None:
        return PositionState.from_dict(self.load().get("pos"))
