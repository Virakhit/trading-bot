"""Asynchronous strategy-to-broker execution with durable boundaries."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.brokers.commands import CommandStatus, DurableBrokerExecutor
from app.brokers.models import BrokerOrderIntent, OrderState
from app.brokers.resolver import PersistentOrderResolver
from app.brokers.service import BrokerExecutionService
from app.config import Settings
from app.core.types import Bar
from app.database.models import (BrokerAccount, BrokerOrder, BrokerPosition, BrokerCommand,
                                 MarketCheckpoint, RiskRow, Run, SignalRow, Snapshot)
from app.journal import digest, event, register_strategy
from app.market_data import MarketSnapshot, validate_bar
from app.ops.controls import ControlStore
from app.portfolio import Portfolio, Position
from app.risk import RiskDecision, RiskEngine
from app.sessions import SessionCalendar


@dataclass(frozen=True)
class BrokerStep:
    run_id: str
    signal_id: str
    risk_id: str
    order_id: str | None = None
    command_id: str | None = None
    event_id: str | None = None
    disposition: str = "PROCESSED"


class AsyncBrokerOrchestrator:
    """One broker-independent strategy step; only the adapter performs transport."""

    def __init__(self, engine, settings: Settings, strategy, broker):
        if settings.execution_backend not in {"mock-broker", "webull-th-test"}:
            raise ValueError("Async broker orchestration requires mock-broker or webull-th-test")
        self.engine, self.settings, self.strategy, self.broker = engine, settings, strategy, broker
        self.controls = ControlStore(engine)
        exchange = "XNYS" if settings.execution_backend == "webull-th-test" else settings.market_exchange
        timezone_name = "America/New_York" if exchange == "XNYS" else settings.session_timezone
        self.calendar = SessionCalendar(timezone_name, settings.allowed_session_start,
                                        settings.allowed_session_end, exchange=exchange)
        self.run_id = None
        self.account_id = None
        self.version_id = None
        self._history: dict[str, list[Bar]] = {}
        self.started = False
        self.safe_degraded = False
        self.last_reconciliation = []

    async def startup(self) -> None:
        if self.started:
            return
        if self.settings.execution_backend == "webull-th-test":
            if not self.settings.automated_webull_test_enabled:
                raise RuntimeError("AUTOMATED_WEBULL_TEST_ENABLED is required")
            if not hasattr(self.broker, "attest_account"):
                raise RuntimeError("Verified Webull Thailand TEST adapter is required")
            await self.broker.attest_account()
        state = await self.broker.query_account_state()
        expected_broker = "webull" if self.settings.execution_backend == "webull-th-test" else "mock"
        with Session(self.engine) as session, session.begin():
            version = register_strategy(session, self.strategy)
            run = session.scalar(select(Run).where(Run.version_id == version.id).order_by(Run.created_at.desc()))
            if run is None:
                run = Run(version_id=version.id, settings=self.settings.model_dump(exclude={"database_url"}),
                          data_hash=digest(["broker", self.strategy.strategy_id, self.strategy.version]),
                          cash=float(state.cash), equity=float(state.cash), checkpoint={"next_index": 0, "quantity": 1})
                session.add(run)
                session.flush()
            account = session.scalar(select(BrokerAccount).where(
                BrokerAccount.run_id == run.id, BrokerAccount.broker == expected_broker,
                BrokerAccount.environment == state.environment))
            if account is None:
                account = BrokerAccount(run_id=run.id, broker=expected_broker, environment=state.environment,
                                        account_ref=state.account_ref, cash=state.cash)
                session.add(account)
                session.flush()
            elif account.account_ref != state.account_ref or account.environment != state.environment:
                raise RuntimeError("BROKER_ACCOUNT_IDENTITY_MISMATCH")
            self.run_id, self.account_id, self.version_id = run.id, account.id, version.id
        if self.settings.execution_backend == "webull-th-test" and hasattr(self.broker, "resolver"):
            self.broker.resolver = PersistentOrderResolver(self.engine, self.account_id)
        executor = DurableBrokerExecutor(self.engine, self.broker, self.account_id)
        for command_id in executor.recover_inflight():
            await executor.reconcile_unknown(command_id)
        self.last_reconciliation = await self.reconcile_once()
        if any(item.kind.name not in {"MATCH"} for item in self.last_reconciliation):
            self.safe_degraded = True
            self.controls.set("PAUSED", "STARTUP_RECONCILIATION_MISMATCH")
        self._load_history()
        self.started = True

    def _load_history(self) -> None:
        with Session(self.engine) as session:
            rows = session.execute(select(Snapshot).join(SignalRow).where(SignalRow.run_id == self.run_id)
                                   .order_by(SignalRow.timestamp)).scalars()
            fields = {"symbol", "timestamp", "open", "high", "low", "close", "bid", "ask", "volume"}
            for row in rows:
                payload = {key: row.payload[key] for key in fields if key in row.payload}
                bar = Bar.model_validate(payload)
                self._history.setdefault(bar.symbol, []).append(bar)

    async def reconcile_once(self, *, hours: int = 24 * 365) -> list:
        if self.account_id is None:
            return []
        start = datetime.now(timezone.utc) - timedelta(hours=hours)
        end = datetime.now(timezone.utc)
        with Session(self.engine) as session:
            account = session.get(BrokerAccount, self.account_id)
            if account is None:
                return []
            service = BrokerExecutionService(session, self.broker, account)
            return await service.reconcile(history_start=start, history_end=end)

    async def process_snapshot(self, snapshot: MarketSnapshot | Bar, quantity: int = 1, *, observed_at=None) -> BrokerStep:
        await self.startup()
        bar = snapshot.bar if isinstance(snapshot, MarketSnapshot) else snapshot
        previous = self._history.get(bar.symbol, [])[-1] if self._history.get(bar.symbol) else None
        quality = validate_bar(bar, previous=previous)
        if previous and bar.timestamp < previous.timestamp:
            raise ValueError("MARKET_TIMESTAMP_REGRESSION")
        if previous and bar.timestamp == previous.timestamp:
            with Session(self.engine) as session:
                existing = session.scalar(select(SignalRow).where(SignalRow.run_id == self.run_id,
                                                                     SignalRow.timestamp == bar.timestamp,
                                                                     SignalRow.symbol == bar.symbol))
            if existing:
                risk = self._risk_for(existing.id)
                with Session(self.engine) as session:
                    order = session.scalar(select(BrokerOrder).where(BrokerOrder.signal_id == existing.id))
                if order is None:
                    return BrokerStep(self.run_id, existing.id, risk.id, disposition="DUPLICATE_SNAPSHOT")
                command_id = await self._submit_order(order.id)
                self._checkpoint_after(bar, len(self._history[bar.symbol]))
                with Session(self.engine) as session:
                    command = session.get(BrokerCommand, command_id)
                return BrokerStep(self.run_id, existing.id, risk.id, order.id, command_id,
                                  command.correlation_id if command else None, "DUPLICATE_SNAPSHOT")
        self._history.setdefault(bar.symbol, []).append(bar)
        context = self._context(bar.symbol)
        signal = self.strategy.evaluate(context)
        if (signal.strategy_id, signal.strategy_version, signal.symbol, signal.timestamp) != (
                self.strategy.strategy_id, self.strategy.version, bar.symbol, bar.timestamp):
            raise ValueError("Strategy returned mismatched signal provenance")
        observed_at = observed_at or signal.timestamp
        position, account, pending = self._portfolio(bar)
        decision_key = digest([self.run_id, self.version_id, bar.symbol, bar.timestamp.isoformat(),
                               signal.action, position.quantity])
        existing_order_id = None
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            existing = session.scalar(select(SignalRow).where(SignalRow.run_id == self.run_id,
                                                               SignalRow.decision_key == decision_key))
            if existing:
                risk = session.scalar(select(RiskRow).where(RiskRow.signal_id == existing.id))
                order = session.scalar(select(BrokerOrder).where(BrokerOrder.signal_id == existing.id))
                if order is not None:
                    existing_order_id = order.id
                    signal_id, risk_id = existing.id, risk.id
                elif risk is not None and risk.status != "APPROVED":
                    self._checkpoint(session, bar, sequence=len(self._history[bar.symbol]))
                    return BrokerStep(self.run_id, existing.id, risk.id, disposition="DUPLICATE_SIGNAL")
                else:
                    signal_id = existing.id
            else:
                signal_id = signal.signal_id
                session.add(SignalRow(id=signal_id, decision_key=decision_key, run_id=self.run_id,
                                      version_id=self.version_id, symbol=bar.symbol, timestamp=bar.timestamp,
                                      action=signal.action, payload=signal.model_dump(mode="json")))
                session.add(Snapshot(signal_id=signal_id, payload={**bar.model_dump(mode="json"),
                                                                     "price": bar.close,
                                                                     "quality": list(quality),
                                                                     "indicators": signal.indicators,
                                                                     "metadata": signal.metadata}))
                event(session, self.run_id, bar.timestamp, "SIGNAL_CREATED", signal_id)
            if existing_order_id is None:
                if quality:
                    decision = RiskDecision("REJECTED", tuple(["DATA_QUALITY", *quality]))
                elif not self.calendar.allows_orders(bar.timestamp):
                    decision = RiskDecision("REJECTED", ("SESSION_CLOSED",))
                elif not self.controls.permits_new_orders() or self.safe_degraded:
                    decision = RiskDecision("REJECTED", ("OPERATIONAL_CONTROL",))
                else:
                    requested = position.quantity if signal.action in ("EXIT", "SELL") else quantity
                    decision = RiskEngine(self.settings).evaluate(
                        signal, requested, bar, position and self._portfolio_value(position, account, bar.symbol, bar.timestamp),
                        observed_at=observed_at, pending_orders=pending)
                risk = session.scalar(select(RiskRow).where(RiskRow.signal_id == signal_id))
                if risk is None:
                    risk = RiskRow(signal_id=signal_id, status=decision.status, codes=list(decision.codes))
                    session.add(risk)
                    session.flush()
                if decision.status != "APPROVED":
                    self._checkpoint(session, bar, sequence=len(self._history[bar.symbol]))
                    return BrokerStep(self.run_id, signal_id, risk.id, disposition="REJECTED")
                requested = position.quantity if signal.action in ("EXIT", "SELL") else quantity
                side = "SELL" if signal.action in ("EXIT", "SELL") else "BUY"
                internal_id = str(uuid5(NAMESPACE_URL, f"broker-order:{self.run_id}:{signal_id}"))
                client_id = str(uuid5(NAMESPACE_URL, f"client-order:{self.run_id}:{signal_id}"))
                intent = BrokerOrderIntent(internal_order_id=internal_id, client_order_id=client_id,
                                           run_id=self.run_id, signal_id=signal_id, symbol=bar.symbol, side=side,
                                           order_type="MARKET", quantity=requested, quote_timestamp=bar.timestamp,
                                           submitted_at=observed_at, quote_age_limit_seconds=Decimal(str(self.settings.max_stale_seconds)))
                service = BrokerExecutionService(session, self.broker, account)
                service.create_order(intent, broker_name=account.broker)
        if existing_order_id is not None:
            command_id = await self._submit_order(existing_order_id)
            self._checkpoint_after(bar, len(self._history[bar.symbol]))
            with Session(self.engine) as session:
                command = session.get(BrokerCommand, command_id)
                event_id = command.correlation_id if command else None
            return BrokerStep(self.run_id, signal_id, risk_id, existing_order_id, command_id, event_id,
                              "DUPLICATE_SIGNAL")
        command_id = await self._submit_order(internal_id)
        self._checkpoint_after(bar, len(self._history[bar.symbol]))
        with Session(self.engine) as session:
            command = session.get(BrokerCommand, command_id)
            event_id = command.correlation_id if command else None
            risk = session.scalar(select(RiskRow).where(RiskRow.signal_id == signal_id))
        return BrokerStep(self.run_id, signal_id, risk.id, internal_id, command_id, event_id)

    async def run_broker(self, bars: Bar | MarketSnapshot | list, quantity: int = 1, *, observed_at=None):
        await self.startup()
        values = bars if isinstance(bars, list) else [bars]
        result = None
        for value in values:
            result = await self.process_snapshot(value, quantity, observed_at=observed_at)
        return result

    async def _submit_order(self, order_id: str) -> str:
        executor = DurableBrokerExecutor(self.engine, self.broker, self.account_id)
        with Session(self.engine) as session:
            order = session.get(BrokerOrder, order_id)
            command = session.scalar(select(BrokerCommand).where(BrokerCommand.order_id == order_id)
                                     .order_by(BrokerCommand.created_at.desc()))
        if command is None:
            command_id = executor.prepare_submit(order_id)
        elif command.status == CommandStatus.PREPARED:
            command_id = command.id
        elif command.status in {CommandStatus.UNKNOWN, CommandStatus.SENDING}:
            return command.id
        else:
            return command.id
        if command is None or command.status == CommandStatus.PREPARED:
            await executor.dispatch(command_id)
        return command_id

    async def dispatch_order(self, command_id: str):
        executor = DurableBrokerExecutor(self.engine, self.broker, self.account_id)
        return await executor.dispatch(command_id)

    async def cancel_order(self, order_id: str, *, allow_retry_after_pre_send: bool = False):
        await self.startup()
        executor = DurableBrokerExecutor(self.engine, self.broker, self.account_id)
        command_id = executor.prepare_cancel(order_id, allow_retry_after_pre_send=allow_retry_after_pre_send)
        event = await executor.dispatch(command_id)
        return command_id, event

    def _risk_for(self, signal_id):
        with Session(self.engine) as session:
            return session.scalar(select(RiskRow).where(RiskRow.signal_id == signal_id))

    def _context(self, symbol):
        from app.strategies import Context
        position = self._current_quantity(symbol)
        return Context(tuple(self._history.get(symbol, ())), position)

    def _current_quantity(self, symbol):
        with Session(self.engine) as session:
            position = session.scalar(select(BrokerPosition).where(BrokerPosition.account_id == self.account_id,
                                                                    BrokerPosition.symbol == symbol))
            return int(position.quantity) if position else 0

    def _portfolio(self, bar):
        with Session(self.engine) as session:
            account = session.get(BrokerAccount, self.account_id)
            positions = list(session.scalars(select(BrokerPosition).where(BrokerPosition.account_id == self.account_id)))
            pending = list(session.scalars(select(BrokerOrder).where(
                BrokerOrder.account_id == self.account_id,
                BrokerOrder.state.in_((OrderState.CREATED, OrderState.SUBMITTING, OrderState.SUBMITTED,
                                       OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.CANCEL_PENDING)))))
            current = next((p for p in positions if p.symbol == bar.symbol), None)
            return Position(int(current.quantity) if current else 0, float(current.average_entry) if current else 0, bar.close), account, pending

    @staticmethod
    def _portfolio_value(position, account, symbol, timestamp):
        portfolio = Portfolio(float(account.cash), positions={symbol: position})
        portfolio.new_day(timestamp)
        return portfolio

    def _checkpoint(self, session, bar, sequence):
        row = session.scalar(select(MarketCheckpoint).where(MarketCheckpoint.run_id == self.run_id,
                                                           MarketCheckpoint.symbol == bar.symbol,
                                                           MarketCheckpoint.source == "broker"))
        if row is None:
            row = MarketCheckpoint(run_id=self.run_id, symbol=bar.symbol, source="broker")
            session.add(row)
        row.last_processed_timestamp = bar.timestamp
        row.last_processed_hash = digest(bar.model_dump(mode="json"))
        row.sequence = sequence

    def _checkpoint_after(self, bar, sequence):
        with Session(self.engine) as session, session.begin():
            self._checkpoint(session, bar, sequence)
