#!/usr/bin/env python3
from __future__ import annotations

from standx.config.runtime import RuntimeConfig
from standx.integrations.telegram import TelegramNotifier


def send(text: str) -> None:
    TelegramNotifier(RuntimeConfig.from_env()).send(text)
