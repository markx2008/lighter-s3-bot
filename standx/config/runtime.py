from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(float(os.getenv(name, str(default))))


@dataclass(frozen=True)
class StrategyConfig:
    symbol: str
    resolution: str
    lookback_days: int
    supertrend_atr_n: int
    supertrend_mult: float
    tp_r_multiple: float


@dataclass(frozen=True)
class RuntimeConfig:
    strategy: StrategyConfig
    account_equity: float
    leverage: int
    risk_pct: float
    max_margin_pct: float
    max_notional_usdt: float
    time_in_force: str
    dry_run: bool
    live: bool
    state_path: str
    paper_state_path: str
    standx_jwt: str
    standx_ed25519_privkey: str
    standx_base_url: str
    telegram_mode: str
    telegram_bot_token: str
    telegram_chat_id: str
    openclaw_bin: str
    telegram_target: str
    trade_log_path: str
    trader_every_sec: int
    guard_every_sec: int

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        return cls(
            strategy=StrategyConfig(
                symbol=os.getenv("SYMBOL", "BTC-USD"),
                resolution=os.getenv("RESOLUTION", "15"),
                lookback_days=_env_int("LOOKBACK_DAYS", 30),
                supertrend_atr_n=_env_int("S3_ST_ATR_N", 10),
                supertrend_mult=_env_float("S3_ST_MULT", 3.0),
                tp_r_multiple=_env_float("S3_ST_TP_R", 2.0),
            ),
            account_equity=_env_float("ACCOUNT_EQUITY", 300),
            leverage=_env_int("LEVERAGE", 20),
            risk_pct=_env_float("RISK_PCT", 0.01),
            max_margin_pct=_env_float("MAX_MARGIN_PCT", 0.20),
            max_notional_usdt=_env_float("MAX_NOTIONAL_USDT", 0),
            time_in_force=os.getenv("TIME_IN_FORCE", "gtc"),
            dry_run=_env_bool("DRY_RUN", True),
            live=_env_bool("LIVE", False),
            state_path=os.getenv("STATE_PATH", "/home/mark/.openclaw/workspace/state_standx_live_trader_s3.json"),
            paper_state_path=os.getenv("PAPER_STATE_PATH", "/home/mark/.openclaw/workspace/state_paper_standx_live_pos.json"),
            standx_jwt=os.getenv("STANDX_JWT", "").strip(),
            standx_ed25519_privkey=os.getenv("STANDX_ED25519_PRIVKEY", "").strip(),
            standx_base_url=os.getenv("STANDX_BASE_URL", "https://perps.standx.com").strip(),
            telegram_mode=os.getenv("TELEGRAM_MODE", "openclaw").strip(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            openclaw_bin=os.getenv("OPENCLAW_BIN", "/home/mark/.npm-global/bin/openclaw").strip(),
            telegram_target=os.getenv("TELEGRAM_TARGET", "-5170271645").strip(),
            trade_log_path=os.getenv("TRADE_LOG_PATH", "/runtime/standx_trades.csv").strip(),
            trader_every_sec=_env_int("TRADER_EVERY_SEC", 900),
            guard_every_sec=_env_int("GUARD_EVERY_SEC", 60),
        )

    def validate(self) -> None:
        errors: list[str] = []
        try:
            resolution_minutes = int(self.strategy.resolution)
        except ValueError:
            resolution_minutes = 0
            errors.append("RESOLUTION must be a numeric minute value")

        if resolution_minutes <= 0:
            errors.append("RESOLUTION must be greater than 0")
        if self.live and (not self.standx_jwt or not self.standx_ed25519_privkey):
            errors.append("LIVE=1 requires STANDX_JWT and STANDX_ED25519_PRIVKEY")
        if self.telegram_mode == "bot" and (not self.telegram_bot_token or not self.telegram_chat_id):
            errors.append("TELEGRAM_MODE=bot requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        if self.leverage <= 0:
            errors.append("LEVERAGE must be greater than 0")
        if self.risk_pct <= 0:
            errors.append("RISK_PCT must be greater than 0")
        if self.max_margin_pct <= 0:
            errors.append("MAX_MARGIN_PCT must be greater than 0")
        if self.strategy.lookback_days <= 0:
            errors.append("LOOKBACK_DAYS must be greater than 0")
        if self.trader_every_sec < 60:
            errors.append("TRADER_EVERY_SEC must be at least 60")
        if self.guard_every_sec < 1:
            errors.append("GUARD_EVERY_SEC must be at least 1")

        if errors:
            raise ValueError("; ".join(errors))

    def startup_summary(self) -> str:
        return (
            f"LIVE={int(self.live)} DRY_RUN={int(self.dry_run)} "
            f"SYMBOL={self.strategy.symbol} RESOLUTION={self.strategy.resolution} "
            f"TRADER_EVERY_SEC={self.trader_every_sec} GUARD_EVERY_SEC={self.guard_every_sec} "
            f"TELEGRAM_MODE={self.telegram_mode}"
        )
