"""Run from repository root: python -m scripts.verify_lifecycle [--run-id UUID]."""
import argparse
import json
import math
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import Settings
from app.database import engine_for
from app.database.models import (FillRow, OrderRow, PositionRow, RiskRow, Run, SignalRow,
                                 Snapshot, StrategyVersion, Trade, TradeMetrics)


def verify(session: Session, run: Run) -> dict:
    signals = list(session.scalars(select(SignalRow).where(SignalRow.run_id == run.id)))
    rejected = 0
    for signal in signals:
        assert session.scalar(select(Snapshot).where(Snapshot.signal_id == signal.id)), "Missing snapshot"
        decision = session.scalar(select(RiskRow).where(RiskRow.signal_id == signal.id))
        assert decision, "Missing risk decision"
        rejected += decision.status == "REJECTED"
        assert signal.version_id == run.version_id
    trades = list(session.scalars(select(Trade).where(Trade.run_id == run.id)))
    closed = []
    total_buy, total_sell, total_fees = 0.0, 0.0, 0.0
    for trade in trades:
        assert session.get(StrategyVersion, trade.version_id)
        rows = session.execute(select(FillRow, OrderRow).join(OrderRow, FillRow.order_id == OrderRow.id)
                               .where(FillRow.trade_id == trade.id)).all()
        entries = [(f, o) for f, o in rows if o.payload["side"] == "BUY"]
        exits = [(f, o) for f, o in rows if o.payload["side"] == "SELL"]
        assert entries and any(o.signal_id == trade.entry_signal_id for f, o in entries)
        assert all(f.position_id == trade.position_id for f, o in rows)
        entry_value = sum(f.quantity * f.price for f, o in entries)
        exit_value = sum(f.quantity * f.price for f, o in exits)
        fees = sum(f.fee for f, o in rows)
        total_buy += entry_value
        total_sell += exit_value
        total_fees += fees
        if trade.status == "CLOSED":
            assert any(o.signal_id == trade.exit_signal_id for f, o in exits)
            assert sum(f.quantity for f, o in entries) == sum(f.quantity for f, o in exits)
            metrics = session.scalar(select(TradeMetrics).where(TradeMetrics.trade_id == trade.id)).payload
            assert math.isclose(metrics["net_pnl"], exit_value - entry_value - fees, abs_tol=1e-8)
            closed.append({"trade_id": trade.id, "version_id": trade.version_id, "fills": len(rows), **metrics})
    assert math.isclose(run.cash, run.settings["initial_cash"] - total_buy + total_sell - total_fees, abs_tol=1e-8)
    positions = list(session.scalars(select(PositionRow).where(PositionRow.run_id == run.id)))
    assert math.isclose(run.equity, run.cash + sum(p.quantity * p.mark for p in positions), abs_tol=1e-8)
    return {"verified": True, "run_id": run.id, "status": run.status, "signals": len(signals),
            "rejected_signals": rejected, "cash": run.cash, "equity": run.equity, "closed_trades": closed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id")
    args = parser.parse_args()
    with Session(engine_for(Settings().database_url)) as session:
        run = session.get(Run, args.run_id) if args.run_id else session.scalar(select(Run).order_by(Run.created_at.desc()))
        if run is None:
            parser.error("No paper session found")
        print(json.dumps(verify(session, run), indent=2))
