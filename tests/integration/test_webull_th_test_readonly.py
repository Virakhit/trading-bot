import os
import pytest
from app.brokers.webull import WebullTestAdapter, WebullTestConfig

pytestmark = pytest.mark.skipif(os.getenv("RUN_WEBULL_TH_TEST_INTEGRATION") != "1", reason="Webull Thailand TEST integration is opt-in")


def test_readonly_account_positions_and_orders():
    broker = WebullTestAdapter(WebullTestConfig.from_env())
    import asyncio
    asyncio.run(broker.query_account_state())
    asyncio.run(broker.query_open_orders())
