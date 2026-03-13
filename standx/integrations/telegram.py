from __future__ import annotations

import subprocess

import requests

from standx.config.runtime import RuntimeConfig


class TelegramNotifier:
    def __init__(self, config: RuntimeConfig):
        self.config = config

    def send(self, text: str) -> None:
        if self.config.telegram_mode == "bot":
            if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
                raise RuntimeError("TELEGRAM_MODE=bot requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
            url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
            response = requests.post(url, json={"chat_id": self.config.telegram_chat_id, "text": text, "disable_web_page_preview": True}, timeout=15)
            response.raise_for_status()
            return
        result = subprocess.run(
            [self.config.openclaw_bin, "message", "send", "--channel", "telegram", "--target", self.config.telegram_target, "--message", text],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "openclaw send failed")
