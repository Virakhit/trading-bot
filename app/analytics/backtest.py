from dataclasses import dataclass
from sqlalchemy.orm import Session
from app.analytics.evaluation import Evaluation, evaluate_run
from app.automation.orchestrator import AutomatedOrchestrator


@dataclass(frozen=True)
class BacktestResult:
    run_id: str
    metrics: Evaluation


def replay(orchestrator: AutomatedOrchestrator, bars, quantity=1) -> BacktestResult:
    run_id = orchestrator.run(list(bars), quantity)
    with Session(orchestrator.engine) as session:
        return BacktestResult(run_id, evaluate_run(session, run_id))
