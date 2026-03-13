from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from standx.config.runtime import RuntimeConfig
from standx.domain.models import PositionState
from standx.integrations.telegram import TelegramNotifier
from standx.services.exchange import ExchangeGateway
from standx.services.paper_broker import PaperBroker
from standx.services.state import JsonStateStore
from standx.services.strategy3 import Strategy3Service
from standx.services.trade_log import TradeLogger

LOCAL_TZ = timezone(timedelta(hours=8))
LOCAL_TZ_LABEL = "GMT+8"
logger = logging.getLogger(__name__)

# Timezone is configured at container level via Dockerfile (TZ=Asia/Taipei).


def ms_to_local_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=LOCAL_TZ).strftime(f"%Y-%m-%d %H:%M:%S {LOCAL_TZ_LABEL}")


@dataclass(frozen=True)
class TraderDependencies:
    config: RuntimeConfig
    exchange: ExchangeGateway
    paper_broker: PaperBroker
    state_store: JsonStateStore
    strategy_service: Strategy3Service
    notifier: TelegramNotifier
    trade_logger: TradeLogger


@dataclass(frozen=True)
class TraderAction:
    kind: str


def build_dependencies(config: RuntimeConfig) -> TraderDependencies:
    return TraderDependencies(
        config=config,
        exchange=ExchangeGateway(config),
        paper_broker=PaperBroker(config.paper_state_path),
        state_store=JsonStateStore(config.state_path),
        strategy_service=Strategy3Service(config),
        notifier=TelegramNotifier(config),
        trade_logger=TradeLogger(config.trade_log_path),
    )


def _safe_notify(notifier: TelegramNotifier, message: str) -> None:
    try:
        notifier.send(message)
    except Exception:
        logger.exception("trader.notify_failed")


def _plan_actions(position: PositionState | None, flip_up: bool, flip_down: bool) -> list[TraderAction]:
    if position is None:
        return [TraderAction("open")]
    if (position.side == "long" and flip_down) or (position.side == "short" and flip_up):
        return [TraderAction("close"), TraderAction("open")]
    return [TraderAction("hold")]


def _signal_notes(config: RuntimeConfig, decision) -> list[str]:
    mode = "真單" if config.live else "模擬"
    is_long = decision.signal.direction == "LONG"
    direction = "做多" if is_long else "做空"

    entry = float(decision.signal.entry_ref)
    sl = float(decision.signal.stop_loss)
    tp = float(decision.signal.take_profit) if decision.signal.take_profit is not None else None

    sl_dist = abs(entry - sl)
    sl_pct = (sl_dist / entry * 100) if entry > 0 else 0.0

    tp_dist = abs(tp - entry) if tp is not None else None
    rr = (tp_dist / sl_dist) if (tp_dist is not None and sl_dist > 0) else None

    # Three-block format requested by user: 📈/🛑/🎯
    lines = [
        f"🤖 StandX S3｜{mode}",
        f"⏰ K線收盤時間：{ms_to_local_str(decision.signal.bar.close_time_ms)}",
        "",
        f"📈 {config.strategy.symbol} {direction}（15m Supertrend 翻轉）",
        f"進場參考(收盤價)：{entry:.2f}",
        f"qty≈{decision.signal.qty:.6f}｜名目≈{decision.signal.notional:.2f}U｜保證金≈{decision.signal.margin:.2f}U",
        f"風險：{decision.signal.risk_usdt:.2f}U（{config.risk_pct*100:.2f}% of {config.account_equity:.0f}U）",
        "",
        f"🛑 止損(SL=Supertrend)：{sl:.2f}（距離≈{sl_dist:.2f} / {sl_pct:.2f}%）",
    ]
    if tp is not None:
        rr_str = f"RR≈{rr:.2f}" if rr is not None else ""
        lines.append(f"🎯 止盈(TP)：{tp:.2f}" + (f"（{rr_str}）" if rr_str else ""))
    else:
        lines.append("🎯 止盈(TP)：未設定")

    return lines


def _open_position_state(decision) -> PositionState:
    return PositionState(
        side="long" if decision.flip_up else "short",
        qty=float(decision.signal.qty),
        entry_ref=decision.signal.entry_ref,
        sl=decision.signal.stop_loss,
        tp=decision.signal.take_profit,
        bar_close_time_ms=decision.signal.bar.close_time_ms,
    )


def run_once(deps: TraderDependencies | None = None) -> None:
    deps = deps or build_dependencies(RuntimeConfig.from_env())
    config = deps.config
    exchange = deps.exchange
    paper_broker = deps.paper_broker
    state_store = deps.state_store
    strategy_service = deps.strategy_service
    notifier = deps.notifier
    trade_logger = deps.trade_logger

    state = state_store.load()
    tracked_position = PositionState.from_dict(state.get("pos"))
    try:
        candles = exchange.fetch_candles()
        symbol_spec = exchange.symbol_spec()
    except Exception:
        logger.exception("trader.market_data_failed symbol=%s", config.strategy.symbol)
        raise

    # Determine the latest closed candle index and ensure we have the expected just-closed bar.
    bar_ms = int(int(config.strategy.resolution) * 60 * 1000)

    def expected_close_time_ms(now_ms: int) -> int:
        boundary = (now_ms // bar_ms) * bar_ms
        return boundary - 1

    def latest_closed_index(candles: list, now_ms: int) -> int:
        if not candles:
            return -1
        # If last candle is already closed, use it; otherwise use the previous.
        if candles[-1].close_time_ms <= now_ms:
            return len(candles) - 1
        return len(candles) - 2 if len(candles) >= 2 else -1

    retries = 0
    while True:
        now_ms = int(time.time() * 1000)
        expect_ms = expected_close_time_ms(now_ms)
        idx = latest_closed_index(candles, now_ms)
        if idx >= 1 and candles[idx].close_time_ms >= expect_ms:
            break
        if retries >= 20:
            break
        time.sleep(1)
        retries += 1
        candles = exchange.fetch_candles()
        symbol_spec = exchange.symbol_spec()

    idx = latest_closed_index(candles, int(time.time() * 1000))
    decision = strategy_service.evaluate_at_index(candles, symbol_spec, tracked_position, index=idx)
    if not decision:
        logger.info("trader.skip reason=no_signal symbol=%s", config.strategy.symbol)
        return
    if state.get("last_bar") == decision.signal.bar.close_time_ms:
        logger.info(
            "trader.skip reason=duplicate_bar symbol=%s bar_close_ms=%s",
            config.strategy.symbol,
            decision.signal.bar.close_time_ms,
        )
        return

    notes = _signal_notes(config, decision)
    actions = _plan_actions(tracked_position, decision.flip_up, decision.flip_down)
    did_trade = any(a.kind in {"open", "close"} for a in actions)
    logger.info(
        "trader.signal symbol=%s direction=%s actions=%s bar_close_ms=%s qty=%.6f",
        config.strategy.symbol,
        decision.signal.direction,
        ",".join(action.kind for action in actions),
        decision.signal.bar.close_time_ms,
        decision.signal.qty,
    )

    if tracked_position is not None:
        next_stop = strategy_service.tighten_stop(tracked_position.sl, decision.signal.stop_loss, tracked_position.side)
        tracked_position = PositionState(
            side=tracked_position.side,
            qty=float(tracked_position.qty),
            entry_ref=decision.signal.entry_ref,
            sl=next_stop,
            tp=decision.signal.take_profit,
            bar_close_time_ms=decision.signal.bar.close_time_ms,
        )
        state["pos"] = tracked_position.to_dict()

    for action in actions:
        if action.kind == "hold":
            logger.info("trader.hold symbol=%s side=%s", config.strategy.symbol, tracked_position.side if tracked_position else "none")
            # 使用者要求：不要發「只有訊號/持有」訊息
            continue
        if action.kind == "close" and tracked_position is not None:
            payload = {
                "symbol": config.strategy.symbol,
                "side": "sell" if tracked_position.side == "long" else "buy",
                "order_type": "market",
                "qty": str(tracked_position.qty),
                "time_in_force": config.time_in_force,
                "reduce_only": True,
                "cl_ord_id": f"s3-close-{uuid.uuid4().hex[:16]}",
            }
            if config.live:
                response = exchange.create_order(payload)
                notes.append(f"🧾 平倉送出（市價 / reduce_only）cl_ord_id={payload['cl_ord_id']}\nres={response}")
                trade_logger.append({"event": "close", "symbol": config.strategy.symbol, "side": tracked_position.side, "qty": float(tracked_position.qty), "cl_ord_id": payload["cl_ord_id"], "mode": "live"})
                try:
                    notes.append(f"🧾 平倉查詢 query_order: {exchange.query_order(payload['cl_ord_id'])}")
                except Exception as exc:
                    notes.append(f"⚠️ CLOSE query_order failed: {exc}")
                    logger.exception("trader.query_order_failed action=close cl_ord_id=%s", payload["cl_ord_id"])
            else:
                paper_broker.close_position(price=decision.signal.entry_ref)
                notes.append(f"🧾（模擬）平倉：@{decision.signal.entry_ref:.2f}")
            state["pos"] = None
            logger.info("trader.close_completed symbol=%s side=%s qty=%s", config.strategy.symbol, tracked_position.side, tracked_position.qty)
            tracked_position = None

        if action.kind == "open":
            payload = {
                "symbol": config.strategy.symbol,
                "side": decision.signal.standx_side,
                "order_type": "market",
                "qty": str(decision.signal.qty),
                "time_in_force": config.time_in_force,
                "reduce_only": False,
                "cl_ord_id": f"s3-open-{uuid.uuid4().hex[:16]}",
                # Broker-side TP/SL (created after this order fills)
                "tp_price": str(decision.signal.take_profit) if decision.signal.take_profit is not None else None,
                "sl_price": str(decision.signal.stop_loss),
            }
            # Drop None fields for clean payload
            payload = {k: v for k, v in payload.items() if v is not None}
            if config.live:
                response = exchange.create_order(payload)
                notes.append(f"🟦 開倉送出（市價）cl_ord_id={payload['cl_ord_id']}")
                if payload.get('sl_price') or payload.get('tp_price'):
                    notes.append(f"🛑/🎯 已請交易商預掛 SL/TP：SL={payload.get('sl_price')} TP={payload.get('tp_price')}")
                notes.append(f"res={response}")
                trade_logger.append({"event": "open", "symbol": config.strategy.symbol, "side": "long" if decision.flip_up else "short", "qty": float(decision.signal.qty), "cl_ord_id": payload["cl_ord_id"], "mode": "live"})
                # query_order may return 404 briefly after submission; fall back to query_open_orders.
                try:
                    notes.append(f"🟦 開倉查詢 query_order: {exchange.query_order(payload['cl_ord_id'])}")
                except Exception as exc:
                    try:
                        oo = exchange.query_open_orders()
                        notes.append(f"🟦 開倉查詢 query_open_orders: {oo}")
                    except Exception as exc2:
                        notes.append(f"⚠️ OPEN query_order/open_orders failed: {exc} | {exc2}")
                    logger.exception("trader.query_order_failed action=open cl_ord_id=%s", payload["cl_ord_id"])
            else:
                fill = exchange.current_price()
                paper_broker.open_position(side="long" if decision.flip_up else "short", qty=float(decision.signal.qty), price=fill)
                notes.append(f"🟦（模擬）開倉：市價成交≈{fill:.2f} qty={decision.signal.qty:.6f}")
            tracked_position = _open_position_state(decision)
            state["pos"] = tracked_position.to_dict()
            logger.info("trader.open_completed symbol=%s side=%s qty=%.6f", config.strategy.symbol, tracked_position.side, tracked_position.qty)

    state["last_bar"] = decision.signal.bar.close_time_ms
    state["last_trader_run_ts"] = int(time.time())
    state["version"] = 1
    state_store.save(state)
    # 使用者要求：不要發「只有訊號」訊息，只在實際開/平倉時通知
    if did_trade:
        _safe_notify(notifier, "\n".join(notes))


def main() -> None:
    run_once()


if __name__ == "__main__":
    main()
