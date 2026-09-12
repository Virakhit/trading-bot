import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.analytics.walkforward import split_walk_forward
from app.brokers.commands import CommandStatus, DurableBrokerExecutor
from app.brokers.models import OrderState
from app.brokers.mock import MockBroker
from app.brokers.resolver import PersistentOrderResolver
from app.brokers.webull import WebullTestConfig
from app.brokers.webull.events import WebullTestEvents
from app.config import Settings
from app.market_data import ReplayProvider, validate_bar
from app.database.models import (BrokerAccount, BrokerCommand, BrokerEventRow, BrokerFillRow, BrokerOrder,
                                  BrokerPosition, DataQualityEvent, MarketCheckpoint, RiskRow, SignalRow, Snapshot,
                                  StrategyVersion)
from app.ops.controls import ControlStore
from app.sessions import SessionCalendar
from app.automation.orchestrator import AutomatedOrchestrator
from app.worker import TradingWorker
from app.strategies import MomentumStrategy
from app.portfolio import Portfolio, Position
from app.risk import RiskEngine
from test_phase2 import arun, event, setup_order
from test_system import engine, bar, settings, signal
from app.database.models import BrokerOrder


def test_persistent_client_order_resolution_survives_new_session(engine):
    _, account, order, intent = setup_order(engine)
    resolver = PersistentOrderResolver(engine, account)
    assert resolver.resolve(intent.client_order_id) == order
    fresh = PersistentOrderResolver(engine, account)
    assert fresh.require(intent.client_order_id) == order
    assert fresh.client_id(order) == intent.client_order_id
    with pytest.raises(LookupError, match="LOCAL_MISSING"): fresh.require("missing")


def test_unknown_client_event_cannot_mutate_account():
    cfg = WebullTestConfig(app_key="k", app_secret="s", account_id="a")
    stream = WebullTestEvents(cfg, lambda _: None, resolver=SimpleNamespace(resolve=lambda _: None))
    with pytest.raises(LookupError, match="LOCAL_MISSING"):
        stream.translate({"request_id": "r", "client_order_id": "unknown", "order_status": "FILLED"})
    stream.handle("trade", "order", {"request_id": "r", "client_order_id": "unknown", "order_status": "FILLED"})
    assert stream.unmapped_events[0]["error"].startswith("LOCAL_MISSING")


def test_webull_fill_history_uses_persistent_resolver():
    cfg = WebullTestConfig(app_key="k", app_secret="s", account_id="a")
    fake = SimpleNamespace(
        order_v3=SimpleNamespace(list_order_executions=lambda *_: SimpleNamespace(json=lambda: {
            "executions": [{"client_order_id": "client-1", "execution_id": "exec-1",
                             "quantity": "1", "price": "100", "fee": "0.01",
                             "timestamp": "2026-01-05T14:30:00+00:00", "order_id": "broker-1"}]})),
        account_v2=SimpleNamespace(get_account_list=lambda: SimpleNamespace(json=lambda: {"accounts": [{"account_id": "a"}]})),
    )
    adapter = __import__("app.brokers.webull", fromlist=["WebullTestAdapter"]).WebullTestAdapter(
        cfg, trade_client=fake, resolver=SimpleNamespace(resolve=lambda client: "internal-1"))
    assert arun(adapter.query_fills())[0].internal_order_id == "internal-1"


def test_unknown_filled_snapshot_without_execution_stays_unresolved(engine):
    _, account, order, intent = setup_order(engine)
    class Remote(MockBroker):
        async def query_order_by_client_id(self, client):
            return self.orders[order].model_copy(update={"state": OrderState.FILLED, "filled_quantity": Decimal("5")})
    broker = Remote(); arun(broker.submit_order(intent)); ex = DurableBrokerExecutor(engine, broker, account)
    command = ex.prepare_submit(order)
    ex._mark_sending(command); ex.recover_inflight()
    remote = arun(ex.reconcile_unknown(command))
    assert remote.state == OrderState.FILLED and ex.get_command(command).status == CommandStatus.UNKNOWN
    with Session(engine) as session:
        assert session.get(BrokerOrder, order).filled_quantity == 0


def test_end_to_end_durable_mock_stream_is_idempotent(engine):
    _, account, order, intent = setup_order(engine)
    broker = MockBroker(); executor = DurableBrokerExecutor(engine, broker, account)
    command = executor.prepare_submit(order)
    ack = arun(executor.dispatch(command))
    broker.script(order, [event(order, OrderState.PARTIALLY_FILLED, "e1", quantity="2", price="100"),
                          event(order, OrderState.FILLED, "e2", quantity="3", price="101")])
    with Session(engine) as session, session.begin():
        service = __import__("app.brokers.service", fromlist=["BrokerExecutionService"]).BrokerExecutionService(session, broker, session.get(__import__("app.database.models", fromlist=["BrokerAccount"]).BrokerAccount, account))
        first = service.apply_event(arun(broker.next_event(order)))
        second = service.apply_event(arun(broker.next_event(order)))
        service.apply_event(event(order, OrderState.FILLED, "e2", quantity="3", price="101"))
        assert first.disposition == "APPLIED" and second.disposition == "APPLIED"
    with Session(engine) as session:
        row = session.get(BrokerOrder, order)
        assert row.state == OrderState.FILLED and row.filled_quantity == 5


def test_automated_orchestrator_mock_pipeline_replays_async_fills_without_drift(engine):
    from app.brokers.models import BrokerFill, BrokerEvent
    from app.brokers.service import BrokerExecutionService
    settings_ = Settings(_env_file=None, execution_backend="mock-broker",
                         allowed_session_start="00:00", allowed_session_end="23:59",
                         fee_per_share=0.1, slippage_bps=0, max_stale_seconds=3600)
    broker = MockBroker()
    pipeline = AutomatedOrchestrator(engine, settings_, MomentumStrategy(), broker).broker_pipeline()
    bars = [bar(100, 0), bar(100, 1), bar(101, 2), bar(99, 3)]

    assert asyncio.run(pipeline.process_snapshot(bars[0])).disposition == "REJECTED"
    assert asyncio.run(pipeline.process_snapshot(bars[1])).disposition == "REJECTED"
    entry = asyncio.run(pipeline.process_snapshot(bars[2], quantity=2))
    assert entry.order_id and entry.command_id
    entry_broker_id = broker.orders[entry.order_id].broker_order_id
    entry_events = [
        BrokerEvent(event_id="entry-fill-1", internal_order_id=entry.order_id, broker_order_id=entry_broker_id,
                    state=OrderState.PARTIALLY_FILLED, timestamp=bars[2].timestamp, source="mock",
                    fill=BrokerFill(execution_id="entry-exec-1", quantity=1, price=100, fee=0.1,
                                    timestamp=bars[2].timestamp)),
        BrokerEvent(event_id="entry-fill-2", internal_order_id=entry.order_id, broker_order_id=entry_broker_id,
                    state=OrderState.FILLED, timestamp=bars[2].timestamp, source="mock",
                    fill=BrokerFill(execution_id="entry-exec-2", quantity=1, price=101, fee=0.1,
                                    timestamp=bars[2].timestamp)),
    ]
    broker.script(entry.order_id, entry_events)
    for expected in entry_events:
        received = asyncio.run(broker.next_event(entry.order_id))
        with Session(engine) as session, session.begin():
            account = session.get(BrokerAccount, pipeline.account_id)
            BrokerExecutionService(session, broker, account).apply_event(received)
            BrokerExecutionService(session, broker, account).apply_event(received)

    exit_step = asyncio.run(pipeline.process_snapshot(bars[3], quantity=2))
    assert exit_step.order_id and exit_step.order_id != entry.order_id
    exit_broker_id = broker.orders[exit_step.order_id].broker_order_id
    exit_events = [
        BrokerEvent(event_id="exit-fill-1", internal_order_id=exit_step.order_id, broker_order_id=exit_broker_id,
                    state=OrderState.PARTIALLY_FILLED, timestamp=bars[3].timestamp, source="mock",
                    fill=BrokerFill(execution_id="exit-exec-1", quantity=1, price=99, fee=0.1,
                                    timestamp=bars[3].timestamp)),
        BrokerEvent(event_id="exit-fill-2", internal_order_id=exit_step.order_id, broker_order_id=exit_broker_id,
                    state=OrderState.FILLED, timestamp=bars[3].timestamp, source="mock",
                    fill=BrokerFill(execution_id="exit-exec-2", quantity=1, price=98, fee=0.1,
                                    timestamp=bars[3].timestamp)),
    ]
    broker.script(exit_step.order_id, exit_events)
    for expected in exit_events:
        received = asyncio.run(broker.next_event(exit_step.order_id))
        with Session(engine) as session, session.begin():
            account = session.get(BrokerAccount, pipeline.account_id)
            BrokerExecutionService(session, broker, account).apply_event(received)
            BrokerExecutionService(session, broker, account).apply_event(received)

    with Session(engine) as session:
        account = session.get(BrokerAccount, pipeline.account_id)
        position = session.scalar(select(BrokerPosition).where(BrokerPosition.account_id == pipeline.account_id,
                                                               BrokerPosition.symbol == "TEST"))
        orders = list(session.scalars(select(BrokerOrder).where(BrokerOrder.account_id == pipeline.account_id)
                                     .order_by(BrokerOrder.created_at)))
        assert position.quantity == 0 and position.realized_pnl == Decimal("-4") and position.fees == Decimal("0.4")
        assert account.cash == Decimal("9995.6")
        assert [order.state for order in orders] == [OrderState.FILLED, OrderState.FILLED]
        assert all(order.signal_id and order.risk_decision_id and order.client_order_id for order in orders)
        assert session.scalar(select(func.count()).select_from(BrokerFillRow)) == 4
        assert session.scalar(select(func.count()).select_from(BrokerEventRow)) == 6
        assert session.scalar(select(func.count()).select_from(BrokerCommand)) == 2
        assert session.scalar(select(func.count()).select_from(Snapshot)) == 4
        version = session.get(StrategyVersion, pipeline.version_id)
        assert version.config_hash and version.git_sha
        assert all(session.get(SignalRow, order.signal_id).version_id == version.id for order in orders)
    assert all(item.kind.name == "MATCH" for item in asyncio.run(pipeline.reconcile_once()))


def test_history_page_is_never_sliced_by_adapter_limit():
    from app.brokers.webull.adapter import WebullTestAdapter
    rows = [{"client_order_id": f"c{i}", "order_id": f"b{i}", "order_status": "FILLED", "symbol": "AAPL", "side": "BUY", "quantity": "1", "filled_qty": "1"} for i in range(100)]
    fake = SimpleNamespace(account_v2=SimpleNamespace(get_account_list=lambda: SimpleNamespace(json=lambda: {"accounts": [{"account_id": "a"}]})),
                           order_v3=SimpleNamespace(list_order_history=lambda *args: SimpleNamespace(json=lambda: {"orders": rows, "pagination_key": "page2"})))
    broker = WebullTestAdapter(WebullTestConfig(app_key="k", app_secret="s", account_id="a"), trade_client=fake)
    page = arun(broker.query_order_history(start_time=bar().timestamp, end_time=bar().timestamp + timedelta(days=1), limit=10))
    assert len(page.orders) == 100 and page.next_cursor == "page2"


def test_execution_modes_and_automation_guard(engine):
    with pytest.raises(ValueError): Settings(_env_file=None, execution_backend="live")
    orchestrator = AutomatedOrchestrator(engine, Settings(_env_file=None), MomentumStrategy())
    result = orchestrator.run([bar(100, 0), bar(100, 1), bar(101, 2)], 1)
    assert result
    guarded = AutomatedOrchestrator(engine, Settings(_env_file=None, execution_backend="webull-th-test"), MomentumStrategy())
    with pytest.raises(RuntimeError, match="AUTOMATED_WEBULL_TEST_ENABLED"): guarded.run([bar(100)], 1)


def test_market_quality_replay_and_session_dst():
    bars = [bar(100), bar(101, 1)]
    assert len(ReplayProvider(bars).snapshots("TEST")) == 2
    assert "TIMESTAMP_REGRESSION" in validate_bar(bars[0], previous=bars[1])
    assert "NON_POSITIVE_PRICE" in validate_bar(bar().model_copy(update={"close": 0.0}))
    cal = SessionCalendar("America/New_York")
    assert cal.state(datetime(2026, 3, 9, 14, 0, tzinfo=timezone.utc)) == "OPEN"
    assert cal.state(datetime(2026, 3, 8, 14, 0, tzinfo=timezone.utc)) == "CLOSED"
    with pytest.raises(ValueError, match="timezone-aware"):
        cal.state(datetime(2026, 3, 9, 14, 0))


def test_xnys_calendar_honors_dst_holidays_and_early_close():
    cal = SessionCalendar("America/New_York", exchange="XNYS")
    assert cal.state(datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc)) == "OPEN"  # 09:30 EDT
    assert cal.state(datetime(2026, 3, 8, 15, 0, tzinfo=timezone.utc)) == "CLOSED"
    assert cal.state(datetime(2026, 11, 27, 17, 0, tzinfo=timezone.utc)) == "OPEN"  # 12:00 EST
    assert cal.state(datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)) == "AFTER_HOURS"  # 13:00 EST early close


def test_pending_orders_are_reserved_for_buy_and_sell_risk():
    portfolio = Portfolio(10000, {"TEST": Position(2, 100, 100)})
    pending_buy = SimpleNamespace(symbol="TEST", side="BUY", quantity=2, filled_quantity=0)
    buy = RiskEngine(Settings(_env_file=None, max_position_size=3)).evaluate(
        signal(), 1, bar(), portfolio, pending_orders=[pending_buy])
    assert "MAX_POSITION_SIZE" in buy.codes
    pending_sell = SimpleNamespace(symbol="TEST", side="SELL", quantity=2, filled_quantity=0)
    sell = RiskEngine(Settings(_env_file=None)).evaluate(signal("EXIT"), 1, bar(), portfolio,
                                                        pending_orders=[pending_sell])
    assert "PENDING_SELL_OVERSUBSCRIBE" in sell.codes


def test_market_quality_failure_can_be_persisted(engine):
    bad = [bar(100, 1), bar(101, 0)]
    provider = ReplayProvider(bad, strict=False)
    assert "TIMESTAMP_REGRESSION" in provider.quality_report()["failures"]
    with Session(engine) as session, session.begin(): provider.persist_quality(session)
    with Session(engine) as session: assert session.scalar(select(DataQualityEvent.code)) == "TIMESTAMP_REGRESSION"


def test_controls_persist_restart(engine):
    controls = ControlStore(engine); controls.set("PAUSED", "test")
    assert ControlStore(engine).get() == "PAUSED" and not controls.permits_new_orders()
    controls.set("RUNNING")
    assert ControlStore(engine).permits_new_orders()


def test_worker_respects_persisted_pause(engine):
    settings_ = Settings(_env_file=None, allowed_session_start="00:00", allowed_session_end="23:59")
    controls = ControlStore(engine); controls.set("PAUSED", "test")
    worker = TradingWorker(engine, AutomatedOrchestrator(engine, settings_, MomentumStrategy()),
                           ReplayProvider([bar()]), "TEST", 1)
    asyncio.run(worker.run_once())
    assert worker.metrics.snapshot()["orders_paused"] == 1


def test_worker_incremental_checkpoint_survives_restart_and_append(engine):
    settings_ = Settings(_env_file=None, allowed_session_start="00:00", allowed_session_end="23:59")
    bars = [bar(100, 0), bar(100, 1), bar(101, 2)]
    initial = bars[:2]
    provider = ReplayProvider(initial, source="worker-test")
    first = TradingWorker(engine, AutomatedOrchestrator(engine, settings_, MomentumStrategy()), provider, "TEST", 1)
    asyncio.run(first.run_once())
    asyncio.run(first.run_once())
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(SignalRow)) == 2
        run_id = session.scalar(select(MarketCheckpoint.run_id))
        checkpoint = session.scalar(select(MarketCheckpoint).where(MarketCheckpoint.run_id == run_id))
        assert checkpoint.sequence == 2
    restarted = TradingWorker(engine, AutomatedOrchestrator(engine, settings_, MomentumStrategy()),
                              ReplayProvider(initial, source="worker-test"), "TEST", 1)
    asyncio.run(restarted.run_once())
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(SignalRow)) == 2
    extended = TradingWorker(engine, AutomatedOrchestrator(engine, settings_, MomentumStrategy()),
                             ReplayProvider(bars + [bar(103, 3)], source="worker-test"), "TEST", 1)
    asyncio.run(extended.run_once())
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(SignalRow)) == 3
        checkpoint = session.scalar(select(MarketCheckpoint).where(MarketCheckpoint.run_id == run_id))
        assert checkpoint.sequence == 3 and checkpoint.last_processed_timestamp.replace(tzinfo=timezone.utc) == bars[-1].timestamp


def test_walk_forward_never_uses_future_segments():
    split = split_walk_forward(range(10), .5, .2)
    assert split.train == (0, 1, 2, 3, 4)
    assert split.validation == (5, 6) and split.out_of_sample == (7, 8, 9)
