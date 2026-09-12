from dataclasses import dataclass
from app.config import Settings
from app.core.runner import run_paper
from app.execution import PaperExecutionEngine
from app.market_data import ReplayProvider
from app.sessions import SessionCalendar


@dataclass
class AutomatedOrchestrator:
    engine: object
    settings: Settings
    strategy: object
    broker: object | None = None

    def run(self, bars, quantity: int = 1, *, run_id=None):
        backend = self.settings.execution_backend
        if backend == "webull-th-test":
            if not self.settings.automated_webull_test_enabled:
                raise RuntimeError("AUTOMATED_WEBULL_TEST_ENABLED is required")
            if self.broker is None:
                raise RuntimeError("Verified Webull Thailand TEST broker is required")
        if backend not in {"local-paper", "mock-broker", "webull-th-test"}:
            raise RuntimeError("Unsupported execution backend")
        # Paper remains the deterministic research source of truth; broker orchestration is opt-in.
        return run_paper(self.engine, self.settings, self.strategy, list(bars), quantity,
                         run_id=run_id, execution_engine=PaperExecutionEngine(self.settings))
