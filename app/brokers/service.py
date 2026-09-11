from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.brokers.errors import DuplicateBrokerEvent, InvalidOrderTransition, OrderRejected
from app.brokers.models import (Broker, BrokerEvent, BrokerFill, BrokerOrderIntent, LEGAL_TRANSITIONS,
                                OrderState, TERMINAL_STATES)
from app.database.models import (BrokerAccount, BrokerEventRow, BrokerFillRow, BrokerOrder,
                                 BrokerPosition, RiskRow, SignalRow)
from app.journal import digest


def decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


class BrokerExecutionService:
    def __init__(self, session: Session, broker: Broker, account: BrokerAccount):
        self.session, self.broker, self.account = session, broker, account

    def create_order(self, intent: BrokerOrderIntent, *, broker_name: str = "mock") -> BrokerOrder:
        existing = self.session.get(BrokerOrder, intent.internal_order_id)
        if existing:
            expected = self._intent_payload(intent)
            if existing.payload != expected:
                raise DuplicateBrokerEvent("Internal order ID reused with different intent")
            return existing
        if self.account.run_id != intent.run_id or self.account.broker != broker_name:
            raise OrderRejected("Broker account does not match order run/environment")
        signal = self.session.get(SignalRow, intent.signal_id)
        risk = self.session.scalar(select(RiskRow).where(RiskRow.signal_id == intent.signal_id))
        if signal is None or signal.run_id != intent.run_id or risk is None or risk.status != "APPROVED":
            raise OrderRejected("A persisted approved risk decision is required")
        row = BrokerOrder(id=intent.internal_order_id, run_id=intent.run_id, signal_id=intent.signal_id,
                          risk_decision_id=risk.id, account_id=self.account.id, client_order_id=intent.client_order_id,
                          broker=broker_name,
                          environment=self.account.environment, symbol=intent.symbol, side=intent.side,
                          order_type=intent.order_type, quantity=intent.quantity, limit_price=intent.limit_price,
                          state=OrderState.CREATED, filled_quantity=Decimal(0), quote_timestamp=intent.quote_timestamp,
                          payload=self._intent_payload(intent))
        self.session.add(row)
        self.session.flush()
        return row

    async def submit(self, order_id: str) -> BrokerEventRow:
        order = self._order(order_id)
        if order.state == OrderState.CREATED:
            self.transition(order, OrderState.SUBMITTING)
            self.session.flush()
        intent = BrokerOrderIntent.model_validate(order.payload)
        event = await self.broker.submit_order(intent)
        return self.apply_event(event)

    async def cancel(self, order_id: str) -> BrokerEventRow:
        order = self._order(order_id)
        if OrderState(order.state) in TERMINAL_STATES:
            raise InvalidOrderTransition("Terminal order cannot be cancelled")
        self.transition(order, OrderState.CANCEL_PENDING)
        return self.apply_event(await self.broker.cancel_order(order_id))

    def transition(self, order: BrokerOrder, target: OrderState) -> bool:
        current = OrderState(order.state)
        if current == target:
            return False
        if target not in LEGAL_TRANSITIONS.get(current, set()):
            raise InvalidOrderTransition(f"{current} -> {target}")
        order.state = target
        return True

    def apply_event(self, event: BrokerEvent) -> BrokerEventRow:
        order = self._order(event.internal_order_id)
        payload_hash = digest(event.model_dump(mode="json"))
        duplicate = self.session.scalar(select(BrokerEventRow).where(BrokerEventRow.broker_event_id == event.event_id))
        if duplicate:
            if duplicate.payload_hash != payload_hash:
                raise DuplicateBrokerEvent("Broker event ID reused with different payload")
            return duplicate
        if event.broker_order_id and order.broker_order_id not in (None, event.broker_order_id):
            raise DuplicateBrokerEvent("Broker order ID changed")
        if event.broker_order_id:
            order.broker_order_id = event.broker_order_id
        disposition = "APPLIED"
        if event.fill:
            existing_fill = self.session.scalar(select(BrokerFillRow).where(BrokerFillRow.execution_id == event.fill.execution_id))
            if existing_fill:
                raise DuplicateBrokerEvent("Execution ID already belongs to another event")
            new_total = order.filled_quantity + event.fill.quantity
            if new_total > order.quantity:
                raise OrderRejected("Fill exceeds order quantity")
            target = OrderState.FILLED if new_total == order.quantity else OrderState.PARTIALLY_FILLED
            self._apply_fill(order, event.fill)
            order.filled_quantity = new_total
            if OrderState(order.state) not in TERMINAL_STATES:
                self.transition(order, target)
        else:
            try:
                changed = self.transition(order, event.state)
                disposition = "APPLIED" if changed else "DUPLICATE_STATUS"
            except InvalidOrderTransition:
                if self._is_stale_status(OrderState(order.state), event.state):
                    disposition = "OUT_OF_ORDER_IGNORED"
                else:
                    raise
        row = BrokerEventRow(order_id=order.id, broker_event_id=event.event_id, state=event.state,
                             timestamp=event.timestamp, source=event.source, reason=event.reason,
                             disposition=disposition, payload_hash=payload_hash)
        self.session.add(row)
        self.session.flush()
        if event.fill:
            self.session.add(BrokerFillRow(order_id=order.id, event_id=row.id, execution_id=event.fill.execution_id,
                                           quantity=event.fill.quantity, price=event.fill.price, fee=event.fill.fee,
                                           timestamp=event.fill.timestamp))
        return row

    async def recover(self) -> list[BrokerEventRow]:
        rows = []
        for event in await self.broker.recover():
            rows.append(self.apply_event(event))
        return rows

    async def reconcile(self):
        from app.brokers.reconciliation import reconcile
        remote = {order.internal_order_id: order for order in await self.broker.query_open_orders()}
        local_ids = self.session.scalars(select(BrokerOrder.id).where(BrokerOrder.account_id == self.account.id))
        for order_id in local_ids:
            order = await self.broker.query_order(order_id)
            if order:
                remote[order_id] = order
        return reconcile(self.session, self.account, list(remote.values()),
                         await self.broker.query_account_state(), await self.broker.query_fills())

    def _apply_fill(self, order: BrokerOrder, fill: BrokerFill) -> None:
        position = self.session.scalar(select(BrokerPosition).where(BrokerPosition.account_id == self.account.id,
                                                                      BrokerPosition.symbol == order.symbol))
        if position is None:
            position = BrokerPosition(account_id=self.account.id, symbol=order.symbol, quantity=Decimal(0),
                                      average_entry=Decimal(0), realized_pnl=Decimal(0), fees=Decimal(0))
            self.session.add(position)
            self.session.flush()
        if order.side == "BUY":
            cost = fill.quantity * fill.price + fill.fee
            if cost > self.account.cash:
                raise OrderRejected("Fill exceeds available cash")
            position.average_entry = ((position.quantity * position.average_entry) + (fill.quantity * fill.price)) / (position.quantity + fill.quantity)
            position.quantity += fill.quantity
            self.account.cash -= cost
        else:
            if fill.quantity > position.quantity:
                raise OrderRejected("Fill would oversell position")
            position.realized_pnl += fill.quantity * (fill.price - position.average_entry)
            position.quantity -= fill.quantity
            self.account.cash += fill.quantity * fill.price - fill.fee
            if position.quantity == 0:
                position.average_entry = Decimal(0)
        position.fees += fill.fee

    def _order(self, order_id: str) -> BrokerOrder:
        order = self.session.get(BrokerOrder, order_id)
        if order is None or order.account_id != self.account.id:
            raise OrderRejected("Unknown internal order")
        return order

    @staticmethod
    def _intent_payload(intent: BrokerOrderIntent) -> dict:
        return intent.model_dump(mode="json")

    @staticmethod
    def _is_stale_status(current: OrderState, incoming: OrderState) -> bool:
        rank = {OrderState.CREATED: 0, OrderState.SUBMITTING: 1, OrderState.SUBMITTED: 2,
                OrderState.ACKNOWLEDGED: 3, OrderState.PARTIALLY_FILLED: 4,
                OrderState.CANCEL_PENDING: 5, OrderState.FILLED: 6,
                OrderState.CANCELLED: 6, OrderState.REJECTED: 6, OrderState.EXPIRED: 6}
        return incoming != OrderState.UNKNOWN and rank.get(incoming, -1) <= rank.get(current, -1)
