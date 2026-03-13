from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from standx.config.runtime import RuntimeConfig
from standx.domain.models import PositionState
from standx.integrations.telegram import TelegramNotifier
from standx.services.exchange import ExchangeGateway
from standx.services.paper_broker import PaperBroker
from standx.services.state import JsonStateStore
from standx.services.strategy3 import Strategy3Service
from standx.services.trade_log import TradeLogger

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MonitorDependencies:
    config: RuntimeConfig
    exchange: ExchangeGateway
    state_store: JsonStateStore
    paper_broker: PaperBroker
    strategy_service: Strategy3Service
    notifier: TelegramNotifier
    trade_logger: TradeLogger


def build_dependencies(config: RuntimeConfig) -> MonitorDependencies:
    return MonitorDependencies(
        config=config,
        exchange=ExchangeGateway(config),
        state_store=JsonStateStore(config.state_path),
        paper_broker=PaperBroker(config.paper_state_path),
        strategy_service=Strategy3Service(config),
        notifier=TelegramNotifier(config),
        trade_logger=TradeLogger(config.trade_log_path),
    )


def _safe_notify(notifier: TelegramNotifier, message: str) -> None:
    try:
        notifier.send(message)
    except Exception:
        logger.exception("guard.notify_failed")


def run_once(deps: MonitorDependencies | None = None) -> None:
    # Guard is disabled; we rely on broker-side TP/SL (tp_price/sl_price) and
    # periodic health/heartbeat messages.
    return


def main() -> None:
    run_once()


if __name__ == "__main__":
    main()
