import os
import pytest
from app.brokers.webull import WebullTestAdapter, WebullTestConfig
from app.brokers.commands import DurableBrokerExecutor

pytestmark = pytest.mark.skipif(os.getenv("RUN_WEBULL_TH_TEST_ORDER") != "1", reason="Webull Thailand TEST order contract is separately opt-in")


def test_order_contract_is_separately_guarded(engine):
    # Keep optional integration collection independent from the unit-test module path.
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parents[1]))
    from test_phase2 import setup_order, arun
    symbol, price = os.getenv("WEBULL_TEST_ORDER_SYMBOL"), os.getenv("WEBULL_TEST_ORDER_LIMIT_PRICE")
    if not symbol or not price:
        pytest.skip("Set WEBULL_TEST_ORDER_SYMBOL and WEBULL_TEST_ORDER_LIMIT_PRICE")
    import asyncio
    broker = WebullTestAdapter(WebullTestConfig.from_env())
    # The durable executor is the contract under test; direct SDK submission is not used.
    _, account, order_id, _ = setup_order(engine)
    executor = DurableBrokerExecutor(engine, broker, account)
    command_id = executor.prepare_submit(order_id)
    try:
        asyncio.run(executor.dispatch(command_id))
        asyncio.run(broker.query_order(order_id))
    finally:
        try:
            cancel_id = executor.prepare_cancel(order_id, allow_retry_after_pre_send=True)
            asyncio.run(executor.dispatch(cancel_id))
        except Exception:
            pass
