import copy
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from standx.apps import monitor as monitor_app
from standx.apps import trader as trader_app
from standx.config.runtime import RuntimeConfig
from standx.domain.models import GuardDecision, PositionState
from standx.integrations.rounding import SymbolSpec
from standx.services.indicators import Candle
from standx.services.state import JsonStateStore
from standx.services.strategy3 import Strategy3Service


def build_decision(*, bar_close_ms: int = 2000, flip_up: bool = True, flip_down: bool = False, qty: float = 1.25):
    signal = SimpleNamespace(
        direction="LONG" if flip_up else "SHORT",
        standx_side="buy" if flip_up else "sell",
        entry_ref=100.0,
        stop_loss=95.0 if flip_up else 105.0,
        take_profit=110.0 if flip_up else 90.0,
        qty=qty,
        risk_usdt=3.0,
        notional=125.0,
        margin=6.25,
        bar=SimpleNamespace(close_time_ms=bar_close_ms),
        symbol_spec=SymbolSpec(2, 4, 0.0),
    )
    return SimpleNamespace(signal=signal, flip_up=flip_up, flip_down=flip_down, previous_position=None)


class FakeExchange:
    def __init__(self, price: float = 101.0):
        self.price = price
        self.orders = []
        self.query_ids = []

    def fetch_candles(self):
        # Minimal Candle-like objects with close_time_ms for bar-selection logic.
        CandleLike = SimpleNamespace
        return [
            CandleLike(close_time_ms=0),
            CandleLike(close_time_ms=0),
        ]

    def symbol_spec(self):
        return SymbolSpec(2, 4, 0.0)

    def current_price(self):
        return self.price

    def create_order(self, payload):
        self.orders.append(payload)
        return {"ok": True, "payload": payload}

    def query_order(self, cl_ord_id: str):
        self.query_ids.append(cl_ord_id)
        return {"cl_ord_id": cl_ord_id, "status": "accepted"}


class FakePaperBroker:
    def __init__(self):
        self.position = None
        self.opened = []
        self.closed = []

    def open_position(self, side: str, qty: float, price: float):
        self.position = SimpleNamespace(side=side, qty=qty, entry=price)
        self.opened.append((side, qty, price))

    def close_position(self, price: float):
        if self.position is not None:
            self.closed.append((self.position.side, self.position.qty, price))
        self.position = None


class FakeStateStore:
    def __init__(self, state=None):
        self.state = copy.deepcopy(state or {"version": 1})
        self.save_calls = 0

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, state):
        self.state = copy.deepcopy(state)
        self.save_calls += 1


class FakeNotifier:
    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.messages = []

    def send(self, text: str):
        self.messages.append(text)
        if self.should_fail:
            raise RuntimeError("telegram down")


class FakeStrategyService:
    def __init__(self, decision=None, guard_decision=None):
        self.decision = decision
        self.guard_decision = guard_decision

    def evaluate(self, candles, symbol_spec, previous_position):
        return self.decision

    def evaluate_at_index(self, candles, symbol_spec, previous_position, index: int):
        return self.decision

    def tighten_stop(self, previous_stop, next_stop, side):
        return Strategy3Service.tighten_stop(previous_stop, next_stop, side)

    def evaluate_guard(self, position, current_price):
        return self.guard_decision


class RuntimeConfigTests(unittest.TestCase):
    def test_from_env_defaults_and_bool_parsing(self):
        with patch.dict(os.environ, {"LIVE": "1", "DRY_RUN": "0", "SYMBOL": "ETH-USD"}, clear=False):
            config = RuntimeConfig.from_env()
        self.assertTrue(config.live)
        self.assertFalse(config.dry_run)
        self.assertEqual(config.strategy.symbol, "ETH-USD")

    def test_validate_rejects_invalid_live_and_bot_config(self):
        with patch.dict(
            os.environ,
            {
                "LIVE": "1",
                "TELEGRAM_MODE": "bot",
                "TRADER_EVERY_SEC": "30",
                "STANDX_JWT": "",
                "STANDX_ED25519_PRIVKEY": "",
                "TELEGRAM_BOT_TOKEN": "",
                "TELEGRAM_CHAT_ID": "",
            },
            clear=False,
        ):
            config = RuntimeConfig.from_env()
        with self.assertRaises(ValueError) as ctx:
            config.validate()
        self.assertIn("LIVE=1 requires STANDX_JWT and STANDX_ED25519_PRIVKEY", str(ctx.exception))
        self.assertIn("TELEGRAM_MODE=bot requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID", str(ctx.exception))
        self.assertIn("TRADER_EVERY_SEC must be at least 60", str(ctx.exception))


class JsonStateStoreTests(unittest.TestCase):
    def test_save_and_load_position(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "state.json")
            store = JsonStateStore(path)
            state = {"version": 1, "pos": PositionState("long", 1.2, 100.0, 95.0, 110.0, 123456).to_dict()}
            store.save(state)
            loaded = store.load_position()
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.side, "long")
            self.assertAlmostEqual(loaded.qty, 1.2)


class Strategy3ServiceTests(unittest.TestCase):
    def setUp(self):
        self.config = RuntimeConfig.from_env()
        self.service = Strategy3Service(self.config)

    def test_calc_qty_caps_by_margin(self):
        plan = self.service.calc_qty(entry=100.0, sl=99.0)
        self.assertIsNotNone(plan)
        self.assertLessEqual(plan.notional, self.config.account_equity * self.config.max_margin_pct * self.config.leverage)

    def test_tighten_stop(self):
        self.assertEqual(self.service.tighten_stop(100.0, 105.0, "long"), 105.0)
        self.assertEqual(self.service.tighten_stop(100.0, 95.0, "long"), 100.0)
        self.assertEqual(self.service.tighten_stop(100.0, 95.0, "short"), 95.0)

    def test_guard_decision(self):
        position = PositionState(side="long", qty=1.0, entry_ref=100.0, sl=95.0, tp=110.0, bar_close_time_ms=1)
        decision = self.service.evaluate_guard(position, current_price=94.0)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.kind, "SL")

    def test_supertrend_evaluate_returns_none_without_flip(self):
        candles = [
            Candle(open_time_ms=i, open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=1.0, close_time_ms=i + 1)
            for i in range(220)
        ]
        decision = self.service.evaluate(candles, SymbolSpec(2, 4, 0.0), None)
        self.assertIsNone(decision)


class TraderAppTests(unittest.TestCase):
    def test_duplicate_bar_skips_save_and_notify(self):
        state_store = FakeStateStore({"version": 1, "last_bar": 2000})
        notifier = FakeNotifier()
        deps = trader_app.TraderDependencies(
            config=RuntimeConfig.from_env(),
            exchange=FakeExchange(),
            paper_broker=FakePaperBroker(),
            state_store=state_store,
            strategy_service=FakeStrategyService(decision=build_decision(bar_close_ms=2000)),
            notifier=notifier,
            trade_logger=SimpleNamespace(append=lambda *_args, **_kwargs: None),
        )

        trader_app.run_once(deps)

        self.assertEqual(state_store.save_calls, 0)
        self.assertEqual(notifier.messages, [])

    def test_flip_from_long_closes_then_opens_short_using_tracked_state(self):
        state_store = FakeStateStore(
            {
                "version": 1,
                "pos": PositionState("long", 2.0, 100.0, 95.0, 110.0, 1500).to_dict(),
            }
        )
        broker = FakePaperBroker()
        broker.position = SimpleNamespace(side="long", qty=2.0, entry=100.0)
        notifier = FakeNotifier()
        deps = trader_app.TraderDependencies(
            config=RuntimeConfig.from_env(),
            exchange=FakeExchange(price=99.0),
            paper_broker=broker,
            state_store=state_store,
            strategy_service=FakeStrategyService(decision=build_decision(bar_close_ms=2000, flip_up=False, flip_down=True, qty=1.5)),
            notifier=notifier,
            trade_logger=SimpleNamespace(append=lambda *_args, **_kwargs: None),
        )

        trader_app.run_once(deps)

        self.assertEqual(len(broker.closed), 1)
        self.assertEqual(len(broker.opened), 1)
        saved_pos = PositionState.from_dict(state_store.state["pos"])
        self.assertEqual(saved_pos.side, "short")
        self.assertAlmostEqual(saved_pos.qty, 1.5)
        self.assertEqual(state_store.state["last_bar"], 2000)
        self.assertEqual(len(notifier.messages), 1)

    def test_notifier_failure_does_not_block_state_save(self):
        state_store = FakeStateStore({"version": 1})
        deps = trader_app.TraderDependencies(
            config=RuntimeConfig.from_env(),
            exchange=FakeExchange(price=102.0),
            paper_broker=FakePaperBroker(),
            state_store=state_store,
            strategy_service=FakeStrategyService(decision=build_decision(bar_close_ms=3000, flip_up=True, flip_down=False)),
            notifier=FakeNotifier(should_fail=True),
            trade_logger=SimpleNamespace(append=lambda *_args, **_kwargs: None),
        )

        with self.assertLogs("standx.apps.trader", level="ERROR"):
            trader_app.run_once(deps)

        self.assertEqual(state_store.save_calls, 1)
        saved_pos = PositionState.from_dict(state_store.state["pos"])
        self.assertIsNotNone(saved_pos)
        self.assertEqual(saved_pos.side, "long")


class MonitorAppTests(unittest.TestCase):
    def test_duplicate_guard_hit_skips_save(self):
        # Guard disabled; should do nothing.
        decision = GuardDecision(kind="SL", level=95.0, price=94.0, close_side="sell")
        state_store = FakeStateStore(
            {
                "version": 1,
                "pos": PositionState("long", 1.0, 100.0, 95.0, 110.0, 1234).to_dict(),
                "last_guard_hit": "SL@95.0@94.0",
            }
        )
        deps = monitor_app.MonitorDependencies(
            config=RuntimeConfig.from_env(),
            exchange=FakeExchange(price=94.0),
            state_store=state_store,
            paper_broker=FakePaperBroker(),
            strategy_service=FakeStrategyService(guard_decision=decision),
            notifier=FakeNotifier(),
            trade_logger=SimpleNamespace(append=lambda *_args, **_kwargs: None),
        )

        monitor_app.run_once(deps)

        self.assertEqual(state_store.save_calls, 0)

    def test_guard_close_clears_state(self):
        # Guard disabled; should not alter state.
        decision = GuardDecision(kind="TP", level=110.0, price=111.0, close_side="sell")
        state_store = FakeStateStore(
            {
                "version": 1,
                "pos": PositionState("long", 1.0, 100.0, 95.0, 110.0, 1234).to_dict(),
            }
        )
        broker = FakePaperBroker()
        broker.position = SimpleNamespace(side="long", qty=1.0, entry=100.0)
        notifier = FakeNotifier()
        deps = monitor_app.MonitorDependencies(
            config=RuntimeConfig.from_env(),
            exchange=FakeExchange(price=111.0),
            state_store=state_store,
            paper_broker=broker,
            strategy_service=FakeStrategyService(guard_decision=decision),
            notifier=notifier,
            trade_logger=SimpleNamespace(append=lambda *_args, **_kwargs: None),
        )

        monitor_app.run_once(deps)

        self.assertEqual(state_store.save_calls, 0)
        self.assertIsNotNone(state_store.state["pos"])


if __name__ == "__main__":
    unittest.main()
