import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import pytest
from sqlalchemy import select
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
from app.database.models import DataQualityEvent
from app.ops.controls import ControlStore
from app.sessions import SessionCalendar
from app.automation.orchestrator import AutomatedOrchestrator
from app.worker import TradingWorker
from app.strategies import MomentumStrategy
from test_phase2 import arun, event, setup_order
from test_system import engine, bar, settings
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


def test_walk_forward_never_uses_future_segments():
    split = split_walk_forward(range(10), .5, .2)
    assert split.train == (0, 1, 2, 3, 4)
    assert split.validation == (5, 6) and split.out_of_sample == (7, 8, 9)
