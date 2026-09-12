import asyncio
import os
import signal
from alembic import command
from alembic.config import Config
from app.config import Settings
from app.database import engine_for
from app.market_data import CSVProvider, ReplayProvider
from app.observability import SafeMetrics
from app.ops.controls import ControlStore
from app.sessions import SessionCalendar
from app.automation.orchestrator import AutomatedOrchestrator
from app.strategies import MomentumStrategy


class TradingWorker:
    """Long-running paper/TEST shell; network writes remain explicit adapter calls."""
    def __init__(self, engine, orchestrator, provider, symbol, quantity=1):
        self.engine, self.orchestrator, self.provider = engine, orchestrator, provider
        self.symbol, self.quantity, self.metrics = symbol, quantity, SafeMetrics()
        self.controls, self.stop = ControlStore(engine), asyncio.Event()
        self.calendar = SessionCalendar(orchestrator.settings.session_timezone,
                                        orchestrator.settings.allowed_session_start,
                                        orchestrator.settings.allowed_session_end)

    def request_stop(self, *_): self.stop.set()

    async def run_once(self):
        loop = asyncio.get_running_loop()
        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig:
                try: loop.add_signal_handler(sig, self.request_stop)
                except (NotImplementedError, OSError): pass
        bars = [bar for bar in self.provider.historical(self.symbol) if self.calendar.allows_orders(bar.timestamp)]
        if bars and self.controls.permits_new_orders():
            self.metrics.inc("market_snapshots", len(bars))
            self.orchestrator.run(bars, self.quantity)
        elif not self.controls.permits_new_orders():
            self.metrics.inc("orders_paused")

    async def run(self):
        while not self.stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=max(0.1, self.orchestrator.settings.scheduler_interval_seconds))
            except asyncio.TimeoutError:
                pass
        return self.metrics.snapshot()


def main():
    settings = Settings()
    engine = engine_for(settings.database_url)
    config = Config("alembic.ini")
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "head")
    csv_path = os.getenv("WORKER_CSV")
    provider = CSVProvider(csv_path) if csv_path else ReplayProvider([])
    worker = TradingWorker(engine, AutomatedOrchestrator(engine, settings, MomentumStrategy()), provider,
                           os.getenv("WORKER_SYMBOL", "DEMO"), int(os.getenv("WORKER_QUANTITY", "1")))
    asyncio.run(worker.run())


if __name__ == "__main__": main()
