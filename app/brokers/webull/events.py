from datetime import datetime, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5
from app.brokers.models import BrokerEvent, BrokerFill, OrderState
from app.brokers.webull.adapter import TH_TEST_EVENTS_HOST, map_webull_order_status


class WebullTestEvents:
    """Translate TEST stream callbacks; callers persist through BrokerExecutionService."""
    def __init__(self, config, on_event, *, client=None):
        if config.region != "th" or config.environment != "test" or config.events_endpoint != TH_TEST_EVENTS_HOST:
            raise ValueError("Only Webull Thailand TEST events are permitted")
        self.config, self.on_event, self.client = config, on_event, client

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
        return BrokerEvent(event_id=str(uuid5(NAMESPACE_URL, key)), internal_order_id=client_id,
                           broker_order_id=payload.get("order_id"),
                           state=map_webull_order_status(payload.get("order_status")), timestamp=stamp,
                           source="webull-events", fill=fill)

    def handle(self, event_type, subscribe_type, payload, raw_message=None):
        self.on_event(self.translate(payload))

    def subscribe(self):
        if self.client is None:
            from webull.trade.trade_events_client import TradeEventsClient
            self.client = TradeEventsClient(self.config.app_key.get_secret_value(), self.config.app_secret.get_secret_value(),
                                            "th", host=TH_TEST_EVENTS_HOST)
        self.client.on_events_message = self.handle
        return self.client.do_subscribe([self.config.account_id.get_secret_value()])

    async def recover_gap(self, broker):
        for event in await broker.recover():
            self.on_event(event)
