from datetime import datetime, timezone
from decimal import Decimal
from queue import Empty, SimpleQueue
from uuid import NAMESPACE_URL, uuid5
from app.brokers.models import BrokerEvent, BrokerFill, OrderState
from app.brokers.webull.adapter import TH_TEST_EVENTS_HOST, map_webull_order_status


class WebullTestEvents:
    """Translate TEST stream callbacks; callers persist through BrokerExecutionService."""
    def __init__(self, config, on_event, *, client=None, resolver=None):
        if config.region != "th" or config.environment != "test" or config.events_endpoint != TH_TEST_EVENTS_HOST:
            raise ValueError("Only Webull Thailand TEST events are permitted")
        self.config, self.on_event, self.client, self.resolver = config, on_event, client, resolver
        self.unmapped_events = []
        self.queue = SimpleQueue()
        self.connected = False
        self.reconnect_count = 0
        self.last_event_at = None

    def translate(self, payload):
        client_id = str(payload["client_order_id"])
        stamp = datetime.fromisoformat(str(payload.get("filled_time") or datetime.now(timezone.utc).isoformat()).replace("Z", "+00:00"))
        qty = Decimal(str(payload.get("filled_qty") or 0))
        fill = None
        if qty > 0 and payload.get("filled_price") is not None:
            execution = str(payload.get("execution_id") or payload.get("request_id"))
            fill = BrokerFill(execution_id=execution, quantity=qty, price=payload["filled_price"],
                              fee=payload.get("actual_commission") or 0, timestamp=stamp)
        key = str(payload.get("request_id") or client_id) + ":" + str(payload.get("scene_type"))
        internal_id = self.resolver.resolve(client_id) if self.resolver else None
        if internal_id is None:
            raise LookupError(f"LOCAL_MISSING client_order_id={client_id}")
        return BrokerEvent(event_id=str(uuid5(NAMESPACE_URL, key)), internal_order_id=internal_id,
                           broker_order_id=payload.get("order_id"),
                           state=map_webull_order_status(payload.get("order_status")), timestamp=stamp,
                           source="webull-events", fill=fill)

    def handle(self, event_type, subscribe_type, payload, raw_message=None):
        try:
            event = self.translate(payload)
            self.queue.put(event)
            self.connected, self.last_event_at = True, event.timestamp
            if self.on_event:
                self.on_event(event)
        except LookupError as exc:
            # Preserve an auditable broker event without handing an unknown ID to accounting.
            self.unmapped_events.append({"payload": payload, "error": str(exc)})

    def subscribe(self):
        if self.client is None:
            from webull.trade.trade_events_client import TradeEventsClient
            self.client = TradeEventsClient(self.config.app_key.get_secret_value(), self.config.app_secret.get_secret_value(),
                                            "th", host=TH_TEST_EVENTS_HOST)
        self.client.on_events_message = self.handle
        result = self.client.do_subscribe([self.config.account_id.get_secret_value()])
        self.connected = True
        return result

    def disconnected(self):
        self.connected = False

    def reconnect(self):
        self.reconnect_count += 1
        return self.subscribe()

    def drain(self, limit: int | None = None):
        events = []
        while limit is None or len(events) < limit:
            try:
                events.append(self.queue.get_nowait())
            except Empty:
                break
        return events

    async def recover_gap(self, broker):
        for event in await broker.recover():
            self.queue.put(event)
            if self.on_event:
                self.on_event(event)
