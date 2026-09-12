import asyncio
import os
import signal
from datetime import datetime, timezone
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import Settings
from app.database import engine_for
from app.market_data import CSVProvider, ReplayProvider
from app.core.runner import run_paper
from app.database.models import MarketCheckpoint, Run, StrategyVersion
from app.journal import digest
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
        self.run_id = None
        self.broker_orchestrator = orchestrator.broker_pipeline() if orchestrator.settings.execution_backend in {"mock-broker", "webull-th-test"} else None
        self.last_reconciliation_at = None
        exchange = "XNYS" if orchestrator.settings.execution_backend == "webull-th-test" else orchestrator.settings.market_exchange
        timezone_name = "America/New_York" if exchange == "XNYS" else orchestrator.settings.session_timezone
        self.calendar = SessionCalendar(timezone_name, orchestrator.settings.allowed_session_start,
                                        orchestrator.settings.allowed_session_end, exchange=exchange)

    def request_stop(self, *_): self.stop.set()

    async def run_once(self):
        loop = asyncio.get_running_loop()
        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig:
                try: loop.add_signal_handler(sig, self.request_stop)
                except (NotImplementedError, OSError): pass
        bars = [bar for bar in self.provider.historical(self.symbol) if self.calendar.allows_orders(bar.timestamp)]
        if self.orchestrator.settings.execution_backend != "local-paper":
            if not bars:
                return
            # Broker recovery and reconciliation continue while PAUSED/KILL_SWITCH;
            # process_snapshot records the blocked decision without creating an order.
            await self.broker_orchestrator.startup()
            with Session(self.engine) as session:
                checkpoint = session.scalar(select(MarketCheckpoint).where(
                    MarketCheckpoint.run_id == self.broker_orchestrator.run_id,
                    MarketCheckpoint.symbol == self.symbol,
                    MarketCheckpoint.source == "broker"))
                last_timestamp = checkpoint.last_processed_timestamp if checkpoint else None
                if last_timestamp is not None and last_timestamp.tzinfo is None:
                    last_timestamp = last_timestamp.replace(tzinfo=timezone.utc)
            candidates = [bar for bar in bars if last_timestamp is None or bar.timestamp > last_timestamp]
            for bar in candidates:
                self.metrics.inc("market_snapshots")
                await self.broker_orchestrator.process_snapshot(bar, self.quantity,
                                                                 observed_at=datetime.now(timezone.utc))
            now = datetime.now(timezone.utc)
            if (self.last_reconciliation_at is None or
                    (now - self.last_reconciliation_at).total_seconds() >= self.orchestrator.settings.reconciliation_interval_seconds):
                await self.broker_orchestrator.reconcile_once()
                self.last_reconciliation_at = now
            return
        if bars and self.controls.permits_new_orders():
            if self.orchestrator.settings.execution_backend == "local-paper":
                with Session(self.engine) as session:
                    latest = session.scalar(select(Run).join(StrategyVersion)
                                           .where(StrategyVersion.strategy_id == self.orchestrator.strategy.strategy_id)
                                           .order_by(Run.created_at.desc()))
                    self.run_id = latest.id if latest else None
                self.run_id = run_paper(self.engine, self.orchestrator.settings, self.orchestrator.strategy,
                                        bars, self.quantity, run_id=self.run_id, max_bars=1,
                                        allow_appended_data=True)
                with Session(self.engine) as session, session.begin():
                    run = session.get(Run, self.run_id)
                    index = run.checkpoint["next_index"]
                    if index:
                        processed = bars[index - 1]
                        source = getattr(self.provider, "source", type(self.provider).__name__)
                        row = session.scalar(select(MarketCheckpoint).where(
                            MarketCheckpoint.run_id == self.run_id,
                            MarketCheckpoint.symbol == self.symbol,
                            MarketCheckpoint.source == source))
                        if row is None:
                            row = MarketCheckpoint(run_id=self.run_id, symbol=self.symbol, source=source)
                            session.add(row)
                        row.last_processed_timestamp = processed.timestamp
                        row.last_processed_hash = digest(processed.model_dump(mode="json"))
                        row.sequence = index
                        self.metrics.inc("market_snapshots")
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
