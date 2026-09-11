from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5
from app.brokers.errors import OrderRejected, UnsupportedBrokerFeature
from app.brokers.models import (AccountState, BrokerEvent, BrokerFill, BrokerOrderHistoryPage, BrokerOrderIntent,
                                BrokerOrderView, OrderState, TERMINAL_STATES)


class MockBroker:
    """Deterministic no-network asynchronous broker used by contract tests."""
    def __init__(self, *, cash: Decimal = Decimal("10000"), account_ref: str = "mock:test"):
        self.cash = cash
        self.account_ref = account_ref
        self.orders: dict[str, BrokerOrderView] = {}
        self.events: list[BrokerEvent] = []
        self._scripts: dict[str, deque[BrokerEvent]] = {}
        self._history_times: dict[str, datetime] = {}
        self._client_to_internal: dict[str, str] = {}

    def script(self, internal_order_id: str, events: list[BrokerEvent]) -> None:
        self._scripts[internal_order_id] = deque(events)

    def bind_order(self, internal_order_id: str, client_order_id: str) -> None:
        self._client_to_internal[client_order_id] = internal_order_id

    async def submit_order(self, intent: BrokerOrderIntent) -> BrokerEvent:
        existing = self.orders.get(intent.internal_order_id)
        if existing:
            return next(e for e in self.events if e.internal_order_id == intent.internal_order_id)
        self.bind_order(intent.internal_order_id, intent.client_order_id)
        broker_id = "mock-" + intent.internal_order_id
        view = BrokerOrderView(internal_order_id=intent.internal_order_id, client_order_id=intent.client_order_id,
                               broker_order_id=broker_id, state=OrderState.SUBMITTED, symbol=intent.symbol,
                               side=intent.side, quantity=intent.quantity, filled_quantity=0)
        self.orders[intent.internal_order_id] = view
        event = BrokerEvent(event_id=str(uuid5(NAMESPACE_URL, broker_id + ":submitted")),
                            internal_order_id=intent.internal_order_id, broker_order_id=broker_id,
                            state=OrderState.SUBMITTED, timestamp=intent.submitted_at, source="mock")
        self.events.append(event)
        self._history_times[intent.internal_order_id] = intent.submitted_at
        return event

    async def next_event(self, internal_order_id: str) -> BrokerEvent:
        try:
            event = self._scripts[internal_order_id].popleft()
        except (KeyError, IndexError) as exc:
            raise UnsupportedBrokerFeature("No scripted event remains") from exc
        self.events.append(event)
        self._history_times[internal_order_id] = event.timestamp
        view = self.orders[internal_order_id]
        filled = view.filled_quantity + (event.fill.quantity if event.fill else 0)
        if event.fill:
            value = event.fill.quantity * event.fill.price
            self.cash += value - event.fill.fee if view.side == "SELL" else -value - event.fill.fee
        self.orders[internal_order_id] = view.model_copy(update={"broker_order_id": event.broker_order_id or view.broker_order_id,
                                                                 "state": event.state, "filled_quantity": filled})
        return event

    async def cancel_order(self, internal_order_id: str) -> BrokerEvent:
        view = self.orders.get(internal_order_id)
        if view is None:
            raise OrderRejected("Unknown order")
        event = BrokerEvent(event_id=str(uuid5(NAMESPACE_URL, internal_order_id + ":cancel-request")),
                           internal_order_id=internal_order_id, broker_order_id=view.broker_order_id,
                           state=OrderState.CANCEL_PENDING,
                           timestamp=datetime(2000, 1, 1, tzinfo=timezone.utc), source="mock")
        self.events.append(event)
        self._history_times[internal_order_id] = event.timestamp
        return event

    async def query_order(self, internal_order_id: str) -> BrokerOrderView | None:
        return self.orders.get(internal_order_id)

    async def query_order_by_client_id(self, client_order_id: str) -> BrokerOrderView | None:
        internal_order_id = self._client_to_internal.get(client_order_id)
        if internal_order_id is not None:
            return self.orders.get(internal_order_id)
        return next((order for order in self.orders.values() if order.client_order_id == client_order_id), None)

    async def query_open_orders(self) -> list[BrokerOrderView]:
        return [order for order in self.orders.values() if order.state not in TERMINAL_STATES]

    async def query_order_history(self, *, start_time: datetime, end_time: datetime, cursor: str | None = None,
                                  limit: int = 100) -> BrokerOrderHistoryPage:
        if start_time.tzinfo is None or end_time.tzinfo is None or start_time >= end_time:
            raise ValueError("History window must be bounded and timezone-aware")
        if limit <= 0 or limit > 500:
            raise ValueError("History page limit must be between 1 and 500")
        offset = int(cursor or "0")
        ordered = [self.orders[order_id] for order_id, timestamp in
                   sorted(self._history_times.items(), key=lambda item: (item[1], item[0]))
                   if start_time <= timestamp <= end_time and order_id in self.orders]
        page = ordered[offset:offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(ordered) else None
        return BrokerOrderHistoryPage(orders=page, next_cursor=next_cursor)

    def record_remote_order(self, order: BrokerOrderView, *, timestamp: datetime) -> None:
        if timestamp.tzinfo is None:
            raise ValueError("History timestamp must be timezone-aware")
        self.orders[order.internal_order_id] = order
        self.bind_order(order.internal_order_id, order.client_order_id)
        self._history_times[order.internal_order_id] = timestamp

    async def query_fills(self) -> list[BrokerEvent]:
        return [event for event in self.events if event.fill]

    async def query_positions(self) -> dict[str, Decimal]:
        positions: dict[str, Decimal] = {}
        for event in await self.query_fills():
            order = self.orders[event.internal_order_id]
            delta = event.fill.quantity * (1 if order.side == "BUY" else -1)
            positions[order.symbol] = positions.get(order.symbol, Decimal(0)) + delta
        return positions

    async def query_account_state(self) -> AccountState:
        return AccountState(account_ref=self.account_ref, environment="mock", cash=self.cash,
                            positions=await self.query_positions(), timestamp=datetime.now(timezone.utc))

    async def recover(self) -> list[BrokerEvent]:
        return list(self.events)
