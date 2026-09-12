from dataclasses import dataclass
from app.config import Settings
from app.core.runner import run_paper
from app.execution import PaperExecutionEngine
from app.market_data import ReplayProvider
from app.sessions import SessionCalendar
from app.automation.broker import AsyncBrokerOrchestrator, BrokerStep


@dataclass
class AutomatedOrchestrator:
    engine: object
    settings: Settings
    strategy: object
    broker: object | None = None

    def broker_pipeline(self) -> AsyncBrokerOrchestrator:
        if self.settings.execution_backend not in {"mock-broker", "webull-th-test"}:
            raise RuntimeError("Broker pipeline requires mock-broker or webull-th-test")
        if self.settings.execution_backend == "webull-th-test" and not self.settings.automated_webull_test_enabled:
            raise RuntimeError("AUTOMATED_WEBULL_TEST_ENABLED is required")
        if self.broker is None:
            if self.settings.execution_backend == "webull-th-test":
                from app.brokers.webull import WebullTestConfig, WebullTestAdapter
                self.broker = WebullTestAdapter(WebullTestConfig.from_env())
            else:
                raise RuntimeError("Broker adapter is required for mock-broker")
        return AsyncBrokerOrchestrator(self.engine, self.settings, self.strategy, self.broker)

    def run(self, bars, quantity: int = 1, *, run_id=None):
        backend = self.settings.execution_backend
        if backend == "webull-th-test":
            if not self.settings.automated_webull_test_enabled:
                raise RuntimeError("AUTOMATED_WEBULL_TEST_ENABLED is required")
            if self.broker is None:
                raise RuntimeError("Verified Webull Thailand TEST broker is required")
        if backend not in {"local-paper", "mock-broker", "webull-th-test"}:
            raise RuntimeError("Unsupported execution backend")
        if backend != "local-paper":
            raise RuntimeError("Use run_broker() for asynchronous broker backends")
        return run_paper(self.engine, self.settings, self.strategy, list(bars), quantity,
                         run_id=run_id, execution_engine=PaperExecutionEngine(self.settings))

    async def run_broker(self, bars, quantity: int = 1, *, observed_at=None):
        return await self.broker_pipeline().run_broker(bars, quantity, observed_at=observed_at)
