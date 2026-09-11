import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
import pytest
from app.brokers.models import BrokerOrderIntent
from app.brokers.webull import WebullTestAdapter, WebullTestConfig

pytestmark = pytest.mark.skipif(os.getenv("RUN_WEBULL_TH_TEST_ORDER") != "1", reason="Webull Thailand TEST order contract is separately opt-in")


def test_order_contract_is_separately_guarded():
    symbol, price = os.getenv("WEBULL_TEST_ORDER_SYMBOL"), os.getenv("WEBULL_TEST_ORDER_LIMIT_PRICE")
    if not symbol or not price:
        pytest.skip("Set WEBULL_TEST_ORDER_SYMBOL and WEBULL_TEST_ORDER_LIMIT_PRICE")
    client_id = uuid4().hex
    intent = BrokerOrderIntent(internal_order_id=client_id, client_order_id=client_id, run_id="integration",
        signal_id="operator", symbol=symbol, side="BUY", order_type="LIMIT",
        quantity=Decimal(os.getenv("WEBULL_TEST_ORDER_QUANTITY", "1")), limit_price=Decimal(price),
        quote_timestamp=datetime.now(timezone.utc), submitted_at=datetime.now(timezone.utc))
    import asyncio
    broker = WebullTestAdapter(WebullTestConfig.from_env())
    asyncio.run(broker.submit_order(intent))
    asyncio.run(broker.query_order_by_client_id(client_id))
    asyncio.run(broker.cancel_order(client_id))
