import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.brokers.commands import CommandStatus, DurableBrokerExecutor
from app.brokers.errors import (BrokerAuthenticationError, BrokerCommandOutcomeUnknown,
                                BrokerDefinitelyNotSent, DuplicateBrokerEvent)
from app.brokers.mock import MockBroker
from app.brokers.models import BrokerOrderView, OrderState
from app.brokers.reconciliation import Difference, reconcile
from app.brokers.service import BrokerExecutionService
from app.brokers.webull import WebullTestAdapter, WebullTestConfig, map_webull_order_status
from app.brokers.webull.events import WebullTestEvents
from app.database.models import BrokerAccount, BrokerCommand, BrokerEventRow, BrokerFillRow, BrokerOrder
from test_phase2 import arun, bar, event, service_for, setup_order
from test_system import engine


class Response:
    def __init__(self, data): self.data = data
    def json(self): return self.data


class FakeAccount:
    def __init__(self, account="acct"): self.account = account
    def get_account_list(self): return Response({"accounts": [{"account_id": self.account}]})
    def get_account_balance(self, account): return Response({"cash_balance": "123.45"})
    def get_account_position(self, account): return Response({"positions": [{"symbol": "AAPL", "quantity": "2"}]})


class FakeOrders:
    def __init__(self): self.placed = []; self.cancelled = []
    def place_order(self, account, orders): self.placed += orders; return Response({"request_id": "r1", "order_id": "b1"})
    def cancel_order(self, account, client): self.cancelled.append(client); return Response({"request_id": "r2"})
    def get_order_detail(self, account, client):
        return Response({"client_order_id": client, "order_id": "b1", "order_status": "SUBMITTED", "symbol": "AAPL", "side": "BUY", "quantity": "2", "filled_qty": "0"})
    def list_order_open(self, account): return Response({"orders": [self.get_order_detail(account, "c1").json()]})
    def list_order_history(self, account, start, end, cursor): return Response({"orders": [self.get_order_detail(account, "c1").json()], "pagination_key": "next"})
    def list_order_executions(self, account):
        return Response({"executions": [{"execution_id": "e1", "client_order_id": "c1", "order_id": "b1", "filled_qty": "2", "filled_price": "100", "actual_commission": "0.1", "filled_time": "2026-01-01T00:00:00+00:00"}]})


def adapter(account="acct"):
    config = WebullTestConfig(app_key="key", app_secret="secret", account_id="acct")
    return WebullTestAdapter(config, trade_client=SimpleNamespace(account_v2=FakeAccount(account), order_v3=FakeOrders()))


@pytest.mark.parametrize("raw,expected", [("SUBMITTED", OrderState.ACKNOWLEDGED), ("CANCELLED", OrderState.CANCELLED),
    ("FAILED", OrderState.REJECTED), ("FILLED", OrderState.FILLED), ("PARTIAL FILLED", OrderState.PARTIALLY_FILLED),
    ("PENDING_CANCEL", OrderState.CANCEL_PENDING), ("EXPIRED", OrderState.EXPIRED), ("new-status", OrderState.UNKNOWN)])
def test_documented_status_mapping_is_conservative(raw, expected): assert map_webull_order_status(raw) == expected


def test_test_boundary_and_secret_masking():
    cfg = WebullTestConfig(app_key="key-value", app_secret="secret-value", account_id="acct-value")
    assert "secret-value" not in str(cfg.diagnostics()) and "acct-value" not in str(cfg.diagnostics())
    for field, value in (("region", "us"), ("environment", "production"), ("endpoint", "api.webull.co.th")):
        with pytest.raises(ValidationError): WebullTestConfig(app_key="k", app_secret="s", account_id="a", **{field: value})


def test_account_attestation_exact_match():
    good = adapter(); arun(good.attest_account()); assert good.attested
    with pytest.raises(BrokerAuthenticationError): arun(adapter("other").attest_account())


def test_adapter_maps_queries_history_fills_positions_and_account():
    broker = adapter(); broker.bind_order("o1", "c1")
    assert arun(broker.query_order("o1")).internal_order_id == "o1"
    assert len(arun(broker.query_open_orders())) == 1
    page = arun(broker.query_order_history(start_time=bar().timestamp, end_time=bar().timestamp + timedelta(days=1)))
    assert len(page.orders) == 1 and page.next_cursor == "next"
    assert arun(broker.query_fills())[0].fill.fee == Decimal("0.1")
    assert arun(broker.query_positions()) == {"AAPL": Decimal("2")}
    state = arun(broker.query_account_state()); assert state.cash == Decimal("123.45") and state.environment == "test"


def test_adapter_attests_before_submit_and_cancel(engine):
    _, _, _, intent = setup_order(engine)
    broker = adapter(); submitted = arun(broker.submit_order(intent))
    assert broker.attested and submitted.state == OrderState.SUBMITTED and broker.client.order_v3.placed
    assert arun(broker.cancel_order(intent.internal_order_id)).state == OrderState.CANCEL_PENDING


class AmbiguousAccept(MockBroker):
    async def submit_order(self, intent):
        await super().submit_order(intent)
        raise TimeoutError("lost response")


class DefinitelyNotSent(MockBroker):
    async def submit_order(self, intent): raise BrokerDefinitelyNotSent("socket never opened")


def test_durable_prepare_crash_and_no_blind_retry(engine):
    _, account, order, _ = setup_order(engine); ex = DurableBrokerExecutor(engine, MockBroker(), account)
    command = ex.prepare_submit(order)
    assert ex.get_command(command).status == CommandStatus.PREPARED
    ex._mark_sending(command)
    assert ex.recover_inflight() == [command]
    with pytest.raises(BrokerCommandOutcomeUnknown): arun(ex.dispatch(command))


def test_accept_then_crash_reconciles_without_duplicate(engine):
    _, account, order, _ = setup_order(engine); broker = AmbiguousAccept(); ex = DurableBrokerExecutor(engine, broker, account)
    command = ex.prepare_submit(order)
    with pytest.raises(BrokerCommandOutcomeUnknown): arun(ex.dispatch(command))
    assert ex.get_command(command).status == CommandStatus.UNKNOWN and len(broker.orders) == 1
    remote = arun(ex.reconcile_unknown(command))
    assert remote.broker_order_id and ex.get_command(command).status == CommandStatus.RECONCILED and len(broker.orders) == 1


def test_response_received_then_transaction_b_crash_reconciles(engine, monkeypatch):
    _, account, order, _ = setup_order(engine); broker = MockBroker(); ex = DurableBrokerExecutor(engine, broker, account)
    command = ex.prepare_submit(order)
    monkeypatch.setattr(ex, "_persist_success", lambda *args: (_ for _ in ()).throw(RuntimeError("crash before commit")))
    with pytest.raises(RuntimeError): arun(ex.dispatch(command))
    assert ex.get_command(command).status == CommandStatus.SENDING and len(broker.orders) == 1
    ex.recover_inflight(); assert arun(ex.reconcile_unknown(command)).broker_order_id


class AmbiguousCancel(MockBroker):
    async def cancel_order(self, internal_order_id):
        view = self.orders[internal_order_id]
        self.orders[internal_order_id] = view.model_copy(update={"state": OrderState.CANCELLED})
        raise TimeoutError("cancel response lost")


def test_cancel_accepted_then_crash_reconciles(engine):
    _, account, order, intent = setup_order(engine); broker = AmbiguousCancel(); arun(broker.submit_order(intent))
    with Session(engine) as session, session.begin(): session.get(BrokerOrder, order).state = OrderState.ACKNOWLEDGED
    ex = DurableBrokerExecutor(engine, broker, account); command = ex.prepare_cancel(order)
    with pytest.raises(BrokerCommandOutcomeUnknown): arun(ex.dispatch(command))
    assert ex.get_command(command).status == CommandStatus.UNKNOWN
    assert arun(ex.reconcile_unknown(command)).state == OrderState.CANCELLED


def test_definite_pre_send_failure_allows_only_explicit_retry(engine):
    _, account, order, _ = setup_order(engine); ex = DurableBrokerExecutor(engine, DefinitelyNotSent(), account)
    command = ex.prepare_submit(order)
    with pytest.raises(BrokerDefinitelyNotSent): arun(ex.dispatch(command))
    assert ex.get_command(command).status == CommandStatus.FAILED_PRE_SEND
    retry = ex.prepare_submit(order, allow_retry_after_pre_send=True); assert retry != command


def test_unknown_query_none_stays_unknown(engine):
    _, account, order, _ = setup_order(engine); ex = DurableBrokerExecutor(engine, MockBroker(), account)
    command = ex.prepare_submit(order); ex._mark_sending(command); ex.recover_inflight()
    assert arun(ex.reconcile_unknown(command)) is None
    assert ex.get_command(command).status == CommandStatus.UNKNOWN


def test_durable_cancel_ambiguous_and_recovery(engine):
    _, account, order, intent = setup_order(engine); broker = MockBroker(); broker.bind_order(order, intent.client_order_id)
    arun(broker.submit_order(intent))
    with Session(engine) as session, session.begin(): session.get(BrokerOrder, order).state = OrderState.ACKNOWLEDGED
    ex = DurableBrokerExecutor(engine, broker, account); command = ex.prepare_cancel(order)
    assert ex.get_command(command).status == CommandStatus.PREPARED
    ex._mark_sending(command); ex.recover_inflight()
    assert ex.get_command(command).status == CommandStatus.UNKNOWN


def test_fill_reconciliation_detects_payload_conflict(engine):
    _, account_id, order, _ = setup_order(engine)
    local = event(order, OrderState.PARTIALLY_FILLED, "same", quantity="2", price="100")
    with Session(engine) as session, session.begin(): service_for(session, account_id).apply_event(local)
    conflict = local.model_copy(update={"event_id": "other", "fill": local.fill.model_copy(update={"price": Decimal("101")})})
    with Session(engine) as session:
        account = session.get(BrokerAccount, account_id)
        state = SimpleNamespace(account_ref=account.account_ref, environment=account.environment, cash=account.cash, positions={"TEST": Decimal("2")})
        assert Difference.PAYLOAD_MISMATCH in {x.kind for x in reconcile(session, account, [], state, [conflict])}


def test_duplicate_execution_and_conflict_remain_safe(engine):
    _, account, order, _ = setup_order(engine); first = event(order, OrderState.PARTIALLY_FILLED, "e", quantity="2")
    with Session(engine) as session, session.begin():
        service = service_for(session, account); service.apply_event(first)
        service.apply_event(first.model_copy(update={"event_id": "replay"}))
        bad = first.model_copy(update={"event_id": "bad", "fill": first.fill.model_copy(update={"price": Decimal("99")})})
        with pytest.raises(DuplicateBrokerEvent): service.apply_event(bad)
        assert session.scalar(select(func.count()).select_from(BrokerFillRow)) == 1


def test_events_translate_duplicate_stably_and_gap_recovery():
    seen = []; resolver = SimpleNamespace(resolve=lambda client_id: "internal-" + client_id)
    stream = WebullTestEvents(adapter().config, seen.append, client=SimpleNamespace(), resolver=resolver)
    payload = {"request_id": "r", "client_order_id": "c", "order_status": "FILLED", "filled_qty": "1", "filled_price": "10", "filled_time": "2026-01-01T00:00:00+00:00"}
    assert stream.translate(payload).event_id == stream.translate(payload).event_id
    stream.handle(1, 1, payload); assert seen[0].state == OrderState.FILLED
    arun(stream.recover_gap(SimpleNamespace(recover=lambda: asyncio.sleep(0, result=[stream.translate(payload)]))))
    assert len(seen) == 2
