from dataclasses import dataclass
from math import sqrt
from statistics import mean, pstdev
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database.models import Trade, TradeMetrics, Run


@dataclass(frozen=True)
class Evaluation:
    total_return: float
    gross_pnl: float
    net_pnl: float
    trades: int
    win_rate: float
    average_win: float
    average_loss: float
    profit_factor: float
    expectancy: float
    max_drawdown: float
    sharpe_like: float | None
    exposure: float
    turnover: float
    fees: float
    slippage: float
    mae: float
    mfe: float


def evaluate_run(session: Session, run_id: str) -> Evaluation:
    run = session.get(Run, run_id)
    metrics = [m.payload for m in session.scalars(select(TradeMetrics).join(Trade).where(Trade.run_id == run_id))]
    nets = [float(m.get("net_pnl", 0)) for m in metrics]
    wins = [x for x in nets if x > 0]; losses = [x for x in nets if x < 0]
    curve, peak, drawdown = 0.0, 0.0, 0.0
    for value in nets:
        curve += value; peak = max(peak, curve); drawdown = max(drawdown, peak - curve)
    returns = [x / max(1.0, float(run.cash)) for x in nets]
    deviation = pstdev(returns) if len(returns) > 1 else 0
    return Evaluation((run.equity - run.settings.get("initial_cash", run.cash)) / max(1, run.cash),
        sum(float(m.get("gross_pnl", 0)) for m in metrics), sum(nets), len(metrics),
        len(wins) / len(metrics) if metrics else 0, mean(wins) if wins else 0, mean(losses) if losses else 0,
        sum(wins) / abs(sum(losses)) if losses else float("inf"), mean(nets) if nets else 0, drawdown,
        sqrt(len(returns)) * mean(returns) / deviation if deviation else None,
        sum(float(m.get("holding_seconds", 0)) for m in metrics),
        sum(abs(float(m.get("gross_pnl", 0))) for m in metrics), sum(float(m.get("fees", 0)) for m in metrics),
        sum(float(m.get("total_slippage", 0)) for m in metrics), mean(float(m.get("mae", 0)) for m in metrics) if metrics else 0,
        mean(float(m.get("mfe", 0)) for m in metrics) if metrics else 0)
