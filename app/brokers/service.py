from datetime import datetime
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.brokers.errors import (BrokerEventConsistencyError, DuplicateBrokerEvent, InvalidOrderTransition,
                                OrderRejected, SubmissionOutcomeUnknown)
from app.brokers.models import (Broker, BrokerEvent, BrokerFill, BrokerOrderIntent, LEGAL_TRANSITIONS,
                                OrderState, TERMINAL_STATES)
from app.database.models import (BrokerAccount, BrokerEventRow, BrokerFillRow, BrokerOrder,
                                 BrokerPosition, RiskRow, SignalRow)
from app.journal import digest


class BrokerExecutionService:
    def __init__(self, session: Session, broker: Broker, account: BrokerAccount):
        self.session, self.broker, self.account = session, broker, account

    def create_order(self, intent: BrokerOrderIntent, *, broker_name: str = "mock") -> BrokerOrder:
        existing = self.session.get(BrokerOrder, intent.internal_order_id)
        if existing:
            if existing.payload != self._intent_payload(intent):
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
                          broker=broker_name, environment=self.account.environment, symbol=intent.symbol,
                          side=intent.side, order_type=intent.order_type, quantity=intent.quantity,
                          limit_price=intent.limit_price, state=OrderState.CREATED, filled_quantity=Decimal(0),
                          quote_timestamp=intent.quote_timestamp, payload=self._intent_payload(intent))
        self.session.add(row)
        self.session.flush()
        return row

    async def submit(self, order_id: str) -> BrokerEventRow:
        order = self._order(order_id)
        current = OrderState(order.state)
        if current == OrderState.SUBMITTING:
            raise SubmissionOutcomeUnknown("Submission outcome is unknown; reconcile before any retry")
        if current != OrderState.CREATED:
            raise InvalidOrderTransition(f"Cannot submit order from {current}")
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
        # Caller-managed transaction is the durability boundary. All business-rule failures are
        # validated before accounting/state mutation so catching a domain error cannot commit a partial fill.
        return self._apply_event(event)

    def _apply_event(self, event: BrokerEvent) -> BrokerEventRow:
        order = self._order(event.internal_order_id)
        payload_hash = digest(event.model_dump(mode="json"))
        duplicate = self.session.scalar(select(BrokerEventRow).where(BrokerEventRow.order_id == order.id,
                                                                      BrokerEventRow.broker_event_id == event.event_id))
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
            existing_fill = self.session.scalar(select(BrokerFillRow).where(BrokerFillRow.order_id == order.id,
                                                                            BrokerFillRow.execution_id == event.fill.execution_id))
            if existing_fill:
                if not self._same_fill(existing_fill, order, event.fill):
                    raise DuplicateBrokerEvent("Execution ID reused with different payload")
                disposition = "DUPLICATE_EXECUTION"
                return self._record_event(order, event, payload_hash, disposition)
            current = OrderState(order.state)
            if current in {OrderState.REJECTED, OrderState.EXPIRED, OrderState.FILLED}:
                raise BrokerEventConsistencyError(f"Fill contradicts terminal order state {current}")
            new_total = order.filled_quantity + event.fill.quantity
            if new_total > order.quantity:
                raise OrderRejected("Fill exceeds order quantity")
            target = OrderState.FILLED if new_total == order.quantity else OrderState.PARTIALLY_FILLED
            self._validate_fill_accounting(order, event.fill)
            if current == OrderState.CANCELLED:
                if target == OrderState.FILLED:
                    order.state = OrderState.FILLED
                    disposition = "CANCELLED_SUPERSEDED_BY_FILL"
                else:
                    disposition = "LATE_FILL_AFTER_CANCELLED"
            else:
                self.transition(order, target)
            self._apply_fill(order, event.fill)
            order.filled_quantity = new_total
            row = self._record_event(order, event, payload_hash, disposition)
            self.session.add(BrokerFillRow(order_id=order.id, event_id=row.id,
                                           execution_id=event.fill.execution_id, quantity=event.fill.quantity,
                                           price=event.fill.price, fee=event.fill.fee, timestamp=event.fill.timestamp))
            self.session.flush()
            return row
        try:
            changed = self.transition(order, event.state)
            disposition = "APPLIED" if changed else "DUPLICATE_STATUS"
        except InvalidOrderTransition:
            if self._is_stale_status(OrderState(order.state), event.state):
                disposition = "OUT_OF_ORDER_IGNORED"
            else:
                raise
        return self._record_event(order, event, payload_hash, disposition)

    def _record_event(self, order, event, payload_hash, disposition):
        row = BrokerEventRow(order_id=order.id, broker_event_id=event.event_id, state=event.state,
                             timestamp=event.timestamp, source=event.source, reason=event.reason,
                             disposition=disposition, payload_hash=payload_hash)
        self.session.add(row)
        self.session.flush()
        return row

    async def recover(self) -> list[BrokerEventRow]:
        return [self.apply_event(event) for event in await self.broker.recover()]

    async def reconcile(self, *, history_start: datetime, history_end: datetime, page_limit: int = 100):
        from app.brokers.reconciliation import reconcile
        if history_start.tzinfo is None or history_end.tzinfo is None or history_start >= history_end:
            raise ValueError("Reconciliation history window must be bounded and timezone-aware")
        if page_limit <= 0 or page_limit > 500:
            raise ValueError("Invalid reconciliation page limit")
        remote_orders = list(await self.broker.query_open_orders())
        cursor = None
        seen_cursors = set()
        while True:
            page = await self.broker.query_order_history(start_time=history_start, end_time=history_end,
                                                         cursor=cursor, limit=page_limit)
            remote_orders.extend(page.orders)
            if page.next_cursor is None:
                break
            if page.next_cursor in seen_cursors:
                raise BrokerEventConsistencyError("Broker history cursor repeated")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        local_ids = list(self.session.scalars(select(BrokerOrder.id).where(BrokerOrder.account_id == self.account.id)))
        for order_id in local_ids:
            order = await self.broker.query_order(order_id)
            if order:
                remote_orders.append(order)
        return reconcile(self.session, self.account, remote_orders,
                         await self.broker.query_account_state(), await self.broker.query_fills())

    def _validate_fill_accounting(self, order: BrokerOrder, fill: BrokerFill) -> None:
        if order.side == "BUY":
            if fill.quantity * fill.price + fill.fee > self.account.cash:
                raise OrderRejected("Fill exceeds available cash")
            return
        position = self.session.scalar(select(BrokerPosition).where(BrokerPosition.account_id == self.account.id,
                                                                      BrokerPosition.symbol == order.symbol))
        if position is None or fill.quantity > position.quantity:
            raise OrderRejected("Fill would oversell position")

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
    def _same_fill(existing, order, fill: BrokerFill) -> bool:
        stored_timestamp = existing.timestamp
        incoming_timestamp = fill.timestamp
        if stored_timestamp.tzinfo is None and incoming_timestamp.tzinfo is not None:
            stored_timestamp = stored_timestamp.replace(tzinfo=incoming_timestamp.tzinfo)
        return (existing.order_id == order.id and existing.quantity == fill.quantity and
                existing.price == fill.price and existing.fee == fill.fee and
                stored_timestamp == incoming_timestamp)

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
