import asyncio
import json
import subprocess
import sys
from datetime import timedelta, timezone
from decimal import Decimal
from uuid import uuid4
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.brokers.errors import (BrokerAuthenticationError, BrokerEventConsistencyError, DuplicateBrokerEvent,
                                InvalidOrderTransition, OrderRejected, StaleMarketData, SubmissionOutcomeUnknown,
                                UnsafeEnvironmentError, UnsupportedBrokerFeature)
from app.brokers.mock import MockBroker
from app.brokers.models import (AccountState, BrokerEvent, BrokerFill, BrokerOrderIntent, BrokerOrderView,
                                InstrumentRules, OrderState, TERMINAL_STATES)
from app.brokers.reconciliation import Difference, reconcile
from app.brokers.service import BrokerExecutionService
from app.brokers.webull import WebullSandboxAdapter, WebullSandboxConfig
from app.core.runner import run_paper
from app.config import Settings
from app.database.models import (BrokerAccount, BrokerEventRow, BrokerFillRow, BrokerOrder,
                                 BrokerPosition, FillRow, RiskRow, Run, SignalRow)
from app.execution import PaperExecutionEngine
from app.strategies import MomentumStrategy
from test_system import engine, bar, settings


def arun(value):
    return asyncio.run(value)


def setup_order(engine, *, side="BUY", quantity="5", cash="10000"):
    bars = [bar(100), bar(100, 1), bar(101, 2), bar(101, 3)]
    run_id = run_paper(engine, settings(), MomentumStrategy(), bars, max_bars=3)
    with Session(engine) as session, session.begin():
        signal = session.scalar(select(SignalRow).join(RiskRow).where(SignalRow.run_id == run_id, RiskRow.status == "APPROVED"))
        account = BrokerAccount(run_id=run_id, broker="mock", environment="mock", account_ref="mock:test", cash=Decimal(cash))
        session.add(account)
        session.flush()
        timestamp = signal.timestamp.replace(tzinfo=timezone.utc) if signal.timestamp.tzinfo is None else signal.timestamp
        intent = BrokerOrderIntent(internal_order_id=str(uuid4()), client_order_id=str(uuid4()), run_id=run_id,
                                   signal_id=signal.id, symbol="TEST", side=side, order_type="MARKET",
                                   quantity=quantity, quote_timestamp=timestamp,
                                   submitted_at=timestamp, quote_age_limit_seconds="0")
        order_id, account_id = intent.internal_order_id, account.id
        BrokerExecutionService(session, MockBroker(), account).create_order(intent)
    return run_id, account_id, order_id, intent


def event(order_id, state, n, *, quantity=None, price="100", fee="0.01", timestamp=None, broker_id=None):
    fill = None if quantity is None else BrokerFill(execution_id=f"exec-{n}", quantity=quantity, price=price,
                                                   fee=fee, timestamp=timestamp or bar().timestamp)
    return BrokerEvent(event_id=f"event-{n}", internal_order_id=order_id,
                       broker_order_id=broker_id or f"mock-{order_id}", state=state,
                       timestamp=timestamp or bar().timestamp, source="mock", fill=fill)


def service_for(session, account_id, broker=None):
    return BrokerExecutionService(session, broker or MockBroker(), session.get(BrokerAccount, account_id))


def test_phase1_execution_dependency_injection(engine):
    class Spy(PaperExecutionEngine):
        calls = 0
        def execute(self, order, quote):
            self.calls += 1
            return super().execute(order, quote)
    spy = Spy(settings())
    run_id = run_paper(engine, settings(), MomentumStrategy(), [bar(100), bar(100, 1), bar(101, 2), bar(101, 3)],
                       max_bars=3, execution_engine=spy)
    assert spy.calls == 1
    with Session(engine) as session:
        assert session.get(Run, run_id).cash == pytest.approx(8989.64798)


def test_order_state_machine_legal_and_illegal(engine):
    _, account_id, order_id, _ = setup_order(engine)
    with Session(engine) as session, session.begin():
        service = service_for(session, account_id)
        order = session.get(BrokerOrder, order_id)
        assert service.transition(order, OrderState.SUBMITTING)
        assert service.transition(order, OrderState.SUBMITTED)
        assert service.transition(order, OrderState.ACKNOWLEDGED)
        assert service.transition(order, OrderState.CANCEL_PENDING)
        assert service.transition(order, OrderState.CANCELLED)
        assert OrderState(order.state) in TERMINAL_STATES
        with pytest.raises(InvalidOrderTransition):
            service.transition(order, OrderState.SUBMITTED)


def test_async_submit_ack_partial_partial_filled_exact_accounting(engine):
    _, account_id, order_id, intent = setup_order(engine)
    broker = MockBroker()
    with Session(engine) as session, session.begin():
        service = service_for(session, account_id, broker)
        submitted = arun(service.submit(order_id))
        assert submitted.state == OrderState.SUBMITTED
        service.apply_event(event(order_id, OrderState.ACKNOWLEDGED, "ack"))
        service.apply_event(event(order_id, OrderState.PARTIALLY_FILLED, "1", quantity="2", price="100.01", fee="0.02"))
        service.apply_event(event(order_id, OrderState.PARTIALLY_FILLED, "2", quantity="2", price="100.02", fee="0.03"))
        service.apply_event(event(order_id, OrderState.FILLED, "3", quantity="1", price="99.99", fee="0.01"))
        order = session.get(BrokerOrder, order_id)
        position = session.scalar(select(BrokerPosition))
        account = session.get(BrokerAccount, account_id)
        assert order.state == "FILLED" and order.filled_quantity == Decimal("5")
        assert position.quantity == Decimal("5")
        assert position.average_entry == Decimal("100.01")
        assert position.fees == Decimal("0.06")
        assert account.cash == Decimal("9499.89")
        assert session.scalar(select(func.count()).select_from(BrokerFillRow)) == 3


def test_duplicate_fill_and_status_events_are_idempotent(engine):
    _, account_id, order_id, _ = setup_order(engine)
    partial = event(order_id, OrderState.PARTIALLY_FILLED, "1", quantity="2")
    ack = event(order_id, OrderState.ACKNOWLEDGED, "ack")
    with Session(engine) as session, session.begin():
        service = service_for(session, account_id)
        service.apply_event(ack)
        service.apply_event(ack)
        service.apply_event(partial)
        service.apply_event(partial)
        assert session.get(BrokerAccount, account_id).cash == Decimal("9799.99")
        assert session.scalar(select(func.count()).select_from(BrokerEventRow)) == 2
        assert session.scalar(select(func.count()).select_from(BrokerFillRow)) == 1
        with pytest.raises(DuplicateBrokerEvent):
            service.apply_event(partial.model_copy(update={"reason": "changed"}))


def test_fill_before_status_and_late_ack_does_not_regress(engine):
    _, account_id, order_id, _ = setup_order(engine)
    with Session(engine) as session, session.begin():
        service = service_for(session, account_id)
        fill_event = event(order_id, OrderState.PARTIALLY_FILLED, "1", quantity="2")
        service.apply_event(fill_event)
        late = service.apply_event(event(order_id, OrderState.ACKNOWLEDGED, "late"))
        assert late.disposition == "OUT_OF_ORDER_IGNORED"
        assert session.get(BrokerOrder, order_id).state == "PARTIALLY_FILLED"
        assert session.scalar(select(BrokerPosition)).quantity == Decimal("2")


def test_cancel_after_partial_and_rejection_change_no_extra_accounting(engine):
    _, account_id, order_id, _ = setup_order(engine)
    with Session(engine) as session, session.begin():
        service = service_for(session, account_id)
        service.apply_event(event(order_id, OrderState.PARTIALLY_FILLED, "1", quantity="2"))
        service.transition(session.get(BrokerOrder, order_id), OrderState.CANCEL_PENDING)
        service.apply_event(event(order_id, OrderState.CANCELLED, "cancel"))
        assert session.get(BrokerOrder, order_id).state == "CANCELLED"
        assert session.scalar(select(BrokerPosition)).quantity == Decimal("2")
        assert session.get(BrokerAccount, account_id).cash == Decimal("9799.99")
    _, account2, order2, _ = setup_order(engine)
    with Session(engine) as session, session.begin():
        service = service_for(session, account2)
        service.apply_event(event(order2, OrderState.REJECTED, "reject"))
        assert session.get(BrokerAccount, account2).cash == Decimal("10000")
        assert session.scalar(select(func.count()).select_from(BrokerPosition).where(BrokerPosition.account_id == account2)) == 0


def test_restart_between_partial_fills_and_recover_replay(engine):
    _, account_id, order_id, _ = setup_order(engine)
    first = event(order_id, OrderState.PARTIALLY_FILLED, "1", quantity="2", price="100", fee="0.01")
    second = event(order_id, OrderState.FILLED, "2", quantity="3", price="110", fee="0.02")
    with Session(engine) as session, session.begin():
        service_for(session, account_id).apply_event(first)
    with Session(engine) as session, session.begin():
        service_for(session, account_id).apply_event(second)
    with Session(engine) as session:
        order = session.get(BrokerOrder, order_id)
        position = session.scalar(select(BrokerPosition))
        assert order.filled_quantity == 5 and order.state == "FILLED"
        assert position.average_entry == Decimal("106") and position.quantity == 5
        assert session.get(BrokerAccount, account_id).cash == Decimal("9469.97")
    broker = MockBroker()
    broker.events = [first, second]
    with Session(engine) as session, session.begin():
        rows = arun(service_for(session, account_id, broker).recover())
        assert len(rows) == 2
        assert session.scalar(select(func.count()).select_from(BrokerFillRow)) == 2


def test_oversell_rejected_atomically(engine):
    _, account_id, buy_id, _ = setup_order(engine)
    with Session(engine) as session, session.begin():
        service_for(session, account_id).apply_event(event(buy_id, OrderState.FILLED, "buy", quantity="5"))
    _, _, sell_id, _ = setup_order(engine, side="SELL", quantity="6")
    with Session(engine) as session, session.begin():
        sell = session.get(BrokerOrder, sell_id)
        sell.account_id = account_id
    with Session(engine) as session, pytest.raises(OrderRejected, match="oversell"):
        with session.begin():
            service_for(session, account_id).apply_event(event(sell_id, OrderState.FILLED, "sell", quantity="6"))
    with Session(engine) as session:
        assert session.get(BrokerAccount, account_id).cash == Decimal("9499.99")
        assert session.scalar(select(BrokerPosition).where(BrokerPosition.account_id == account_id)).quantity == 5


def test_reconciliation_classifies_without_mutation(engine):
    _, account_id, order_id, _ = setup_order(engine)
    with Session(engine) as session:
        account = session.get(BrokerAccount, account_id)
        local = session.get(BrokerOrder, order_id)
        remote = BrokerOrderView(internal_order_id=order_id, client_order_id=local.client_order_id,
                                 broker_order_id=None, state=OrderState.ACKNOWLEDGED, symbol="TEST",
                                 side="BUY", quantity="5", filled_quantity="1")
        missing = remote.model_copy(update={"internal_order_id": "remote-only"})
        state = AccountState(account_ref="mock:test", environment="mock", cash="9999",
                             positions={"TEST": "1"}, timestamp=bar().timestamp)
        before = account.cash
        kinds = {item.kind for item in reconcile(session, account, [remote, missing], state)}
        assert {Difference.STATUS_MISMATCH, Difference.LOCAL_MISSING, Difference.CASH_MISMATCH,
                Difference.POSITION_MISMATCH} <= kinds
        assert account.cash == before


def test_async_reconciliation_match(engine):
    _, account_id, order_id, intent = setup_order(engine)
    broker = MockBroker()
    with Session(engine) as session, session.begin():
        service = service_for(session, account_id, broker)
        arun(service.submit(order_id))
        fill = event(order_id, OrderState.FILLED, "all", quantity="5", price="100", fee="0")
        broker.events.append(fill)
        broker.orders[order_id] = broker.orders[order_id].model_copy(update={"state": OrderState.FILLED, "filled_quantity": Decimal("5")})
        broker.cash = Decimal("9500")
        service.apply_event(fill)
        stamp = bar().timestamp
        kinds = {item.kind for item in arun(service.reconcile(history_start=stamp - timedelta(days=1),
                                                               history_end=stamp + timedelta(days=1)))}
        assert kinds == {Difference.MATCH}


def test_decimal_rules_and_stale_quote_are_explicit():
    rules = InstrumentRules(price_increment="0.01", quantity_increment="0.001")
    rules.validate(price=Decimal("10.23"), quantity=Decimal("1.005"))
    with pytest.raises(ValueError, match="Price"):
        rules.validate(price=Decimal("10.235"), quantity=Decimal("1"))
    with pytest.raises(ValueError, match="Quantity"):
        rules.validate(price=Decimal("10.23"), quantity=Decimal("1.0005"))
    with pytest.raises(StaleMarketData):
        BrokerOrderIntent(internal_order_id="o", client_order_id="c", run_id="r", signal_id="s", symbol="TEST",
                          side="BUY", order_type="MARKET", quantity="1.000000000000000001",
                          quote_timestamp=bar().timestamp, submitted_at=bar().timestamp + timedelta(seconds=2),
                          quote_age_limit_seconds="1")


def test_webull_config_masks_secrets_and_rejects_non_test():
    config = WebullSandboxConfig(app_key="secret-key", app_secret="secret-value", account_id="personal-account")
    diagnostics = str(config.diagnostics())
    assert "secret-key" not in diagnostics and "secret-value" not in diagnostics and "personal-account" not in diagnostics
    assert config.region == "th" and config.environment == "test"
    assert config.endpoint == "th-api.uat.webullbroker.com"
    assert config.events_endpoint == "th-events-api.uat.webullbroker.com" and len(config.account_ref) == 64
    with pytest.raises((UnsafeEnvironmentError, ValidationError)):
        WebullSandboxConfig(app_key="x", app_secret="y", account_id="z", endpoint="not-test.invalid")
    with pytest.raises(ValidationError):
        WebullSandboxConfig(app_key="x", app_secret="y", account_id="z", region="us")
    with pytest.raises(ValidationError):
        WebullSandboxConfig(app_key="x", app_secret="y", account_id="z", environment="production")
    with pytest.raises((BrokerAuthenticationError, ValidationError)):
        WebullSandboxConfig(app_key="", app_secret="y", account_id="z")


def test_webull_missing_environment_credentials(monkeypatch):
    for name in ("WEBULL_TEST_APP_KEY", "WEBULL_TEST_APP_SECRET", "WEBULL_TEST_ACCOUNT_ID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(BrokerAuthenticationError):
        WebullSandboxConfig.from_env(None)


def test_main_settings_accepts_separate_webull_environment(tmp_path):
    path = tmp_path / ".env"
    path.write_text("MODE=paper\nWEBULL_TEST_APP_KEY=separate-secret\n", encoding="utf-8")
    assert Settings(_env_file=path).mode == "paper"


@pytest.mark.parametrize("start_state", [OrderState.CREATED, OrderState.SUBMITTING, OrderState.SUBMITTED,
                                          OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.CANCEL_PENDING])
def test_restart_states_keep_ids_and_do_not_duplicate_orders(engine, start_state):
    _, account_id, order_id, intent = setup_order(engine)
    with Session(engine) as session, session.begin():
        session.get(BrokerOrder, order_id).state = start_state
    with Session(engine) as session, session.begin():
        service = service_for(session, account_id)
        before = session.scalar(select(func.count()).select_from(BrokerOrder))
        assert service.create_order(intent).id == order_id
        assert session.scalar(select(func.count()).select_from(BrokerOrder)) == before
        assert session.get(BrokerOrder, order_id).client_order_id == intent.client_order_id
    probe = subprocess.run([sys.executable, "-m", "scripts.phase2_recovery_probe", "--database-url", str(engine.url),
                            "--order-id", order_id], capture_output=True, text=True, check=True)
    recovered = json.loads(probe.stdout)
    assert recovered["order_id"] == order_id and recovered["client_order_id"] == intent.client_order_id
    assert recovered["state"] == start_state


def test_final_fill_transaction_rollback_then_recovery(engine):
    _, account_id, order_id, _ = setup_order(engine)
    partial = event(order_id, OrderState.PARTIALLY_FILLED, "1", quantity="2")
    final = event(order_id, OrderState.FILLED, "2", quantity="3")
    with Session(engine) as session, session.begin():
        service_for(session, account_id).apply_event(partial)
    with pytest.raises(RuntimeError):
        with Session(engine) as session, session.begin():
            service_for(session, account_id).apply_event(final)
            raise RuntimeError("crash before commit")
    with Session(engine) as session:
        assert session.get(BrokerOrder, order_id).filled_quantity == 2
        assert session.scalar(select(BrokerPosition)).quantity == 2
    with Session(engine) as session, session.begin():
        service_for(session, account_id).apply_event(final)
    with Session(engine) as session:
        assert session.get(BrokerOrder, order_id).filled_quantity == 5
        assert session.scalar(select(BrokerPosition)).quantity == 5



def test_submitting_can_receive_partial_or_full_fill_and_late_ack(engine):
    _, account_id, partial_id, _ = setup_order(engine)
    with Session(engine) as session, session.begin():
        service = service_for(session, account_id)
        order = session.get(BrokerOrder, partial_id)
        service.transition(order, OrderState.SUBMITTING)
        service.apply_event(event(partial_id, OrderState.PARTIALLY_FILLED, "submit-partial", quantity="2"))
        assert order.state == "PARTIALLY_FILLED" and order.filled_quantity == 2
    _, account2, full_id, _ = setup_order(engine)
    with Session(engine) as session, session.begin():
        service = service_for(session, account2)
        order = session.get(BrokerOrder, full_id)
        service.transition(order, OrderState.SUBMITTING)
        service.apply_event(event(full_id, OrderState.FILLED, "submit-full", quantity="5"))
        late = service.apply_event(event(full_id, OrderState.ACKNOWLEDGED, "submit-late-ack"))
        assert order.state == "FILLED"
        assert late.disposition == "OUT_OF_ORDER_IGNORED"
        assert session.get(BrokerAccount, account2).cash == Decimal("9499.99")


def test_cancel_fill_race_and_cancelled_late_fill_policy(engine):
    _, account_id, order_id, _ = setup_order(engine)
    with Session(engine) as session, session.begin():
        service = service_for(session, account_id)
        service.apply_event(event(order_id, OrderState.PARTIALLY_FILLED, "race-1", quantity="2"))
        service.transition(session.get(BrokerOrder, order_id), OrderState.CANCEL_PENDING)
        service.apply_event(event(order_id, OrderState.PARTIALLY_FILLED, "race-2", quantity="1"))
        service.apply_event(event(order_id, OrderState.CANCELLED, "race-cancel"))
        order = session.get(BrokerOrder, order_id)
        assert order.state == "CANCELLED" and order.filled_quantity == 3
    _, account2, cancelled_id, _ = setup_order(engine)
    with Session(engine) as session, session.begin():
        service = service_for(session, account2)
        service.apply_event(event(cancelled_id, OrderState.CANCELLED, "cancel-first"))
        late_partial = service.apply_event(event(cancelled_id, OrderState.PARTIALLY_FILLED, "late-1", quantity="2"))
        assert late_partial.disposition == "LATE_FILL_AFTER_CANCELLED"
        assert session.get(BrokerOrder, cancelled_id).state == "CANCELLED"
        late_final = service.apply_event(event(cancelled_id, OrderState.FILLED, "late-2", quantity="3"))
        assert late_final.disposition == "CANCELLED_SUPERSEDED_BY_FILL"
        assert session.get(BrokerOrder, cancelled_id).state == "FILLED"
        assert session.get(BrokerOrder, cancelled_id).filled_quantity == 5


@pytest.mark.parametrize("terminal", [OrderState.REJECTED, OrderState.EXPIRED])
def test_impossible_terminal_fill_is_safety_critical_and_atomic(engine, terminal):
    _, account_id, order_id, _ = setup_order(engine)
    with Session(engine) as session, session.begin():
        service = service_for(session, account_id)
        if terminal == OrderState.EXPIRED:
            service.apply_event(event(order_id, OrderState.ACKNOWLEDGED, "pre-expire-ack"))
        service.apply_event(event(order_id, terminal, f"terminal-{terminal}"))
        with pytest.raises(BrokerEventConsistencyError) as exc:
            service.apply_event(event(order_id, OrderState.FILLED, f"contradiction-{terminal}", quantity="5"))
        assert exc.value.safety_critical
    with Session(engine) as session:
        assert session.get(BrokerAccount, account_id).cash == Decimal("10000")
        assert session.get(BrokerOrder, order_id).filled_quantity == 0
        assert session.scalar(select(func.count()).select_from(BrokerPosition).where(BrokerPosition.account_id == account_id)) == 0
        assert session.scalar(select(func.count()).select_from(BrokerFillRow).where(BrokerFillRow.order_id == order_id)) == 0


def test_duplicate_execution_new_event_is_idempotent_but_conflict_fails(engine):
    _, account_id, order_id, _ = setup_order(engine)
    first = event(order_id, OrderState.PARTIALLY_FILLED, "same", quantity="2", price="100")
    replay = first.model_copy(update={"event_id": "event-same-replayed"})
    conflict_fill = first.fill.model_copy(update={"price": Decimal("101")})
    conflict = first.model_copy(update={"event_id": "event-same-conflict", "fill": conflict_fill})
    with Session(engine) as session, session.begin():
        service = service_for(session, account_id)
        service.apply_event(first)
        row = service.apply_event(replay)
        assert row.disposition == "DUPLICATE_EXECUTION"
        assert session.scalar(select(func.count()).select_from(BrokerFillRow).where(BrokerFillRow.order_id == order_id)) == 1
        with pytest.raises(DuplicateBrokerEvent):
            service.apply_event(conflict)
        assert session.get(BrokerOrder, order_id).filled_quantity == 2
        assert session.get(BrokerAccount, account_id).cash == Decimal("9799.99")


def test_broker_only_terminal_orders_are_discovered_from_bounded_history(engine):
    _, account_id, local_order_id, _ = setup_order(engine)
    broker = MockBroker()
    stamp = bar().timestamp
    for state in (OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED):
        remote_id = f"remote-{state}"
        broker.record_remote_order(BrokerOrderView(internal_order_id=remote_id, client_order_id=f"client-{state}",
                                                   broker_order_id=f"broker-{state}", state=state, symbol="TEST",
                                                   side="BUY", quantity="5", filled_quantity="5" if state == OrderState.FILLED else "0"),
                                   timestamp=stamp)
    with Session(engine) as session, session.begin():
        items = arun(service_for(session, account_id, broker).reconcile(history_start=stamp - timedelta(hours=1),
                                                                        history_end=stamp + timedelta(hours=1),
                                                                        page_limit=1))
        missing = {item.object_id for item in items if item.kind == Difference.LOCAL_MISSING and item.object_type == "order"}
        assert {"remote-FILLED", "remote-CANCELLED", "remote-REJECTED"} <= missing
        assert any(item.kind == Difference.BROKER_MISSING and item.object_id == local_order_id for item in items)


def test_conflicting_duplicate_remote_order_is_reported(engine):
    _, account_id, order_id, _ = setup_order(engine)
    with Session(engine) as session:
        account = session.get(BrokerAccount, account_id)
        local = session.get(BrokerOrder, order_id)
        a = BrokerOrderView(internal_order_id=order_id, client_order_id=local.client_order_id, broker_order_id="b1",
                            state=OrderState.ACKNOWLEDGED, symbol="TEST", side="BUY", quantity="5", filled_quantity="0")
        b = a.model_copy(update={"state": OrderState.CANCELLED})
        state = AccountState(account_ref="mock:test", environment="mock", cash="10000", positions={}, timestamp=bar().timestamp)
        items = reconcile(session, account, [a, b], state)
        assert any(item.kind == Difference.UNKNOWN and item.object_type == "order_duplicate" for item in items)


def test_submitting_order_cannot_be_blindly_retried(engine):
    _, account_id, order_id, _ = setup_order(engine)
    with Session(engine) as session, session.begin():
        session.get(BrokerOrder, order_id).state = OrderState.SUBMITTING
        with pytest.raises(SubmissionOutcomeUnknown):
            arun(service_for(session, account_id).submit(order_id))
