"""Recover only committed state; never fund an existing run a second time."""
from datetime import date, datetime, timezone
from math import isclose
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.types import Fill
from app.database.models import FillRow, OrderRow, PositionRow, Run, Trade
from app.portfolio import Portfolio, Position


def checkpoint(portfolio: Portfolio, next_index: int, quantity: int) -> dict:
    return {"next_index": next_index, "quantity": quantity,
            "day": str(portfolio.current_day) if portfolio.current_day else None,
            "day_start_equity": portfolio.day_start_equity, "trades_today": portfolio.trades_today,
            "consecutive_losses": portfolio.consecutive_losses,
            "cooldown_until": portfolio.cooldown_until.isoformat() if portfolio.cooldown_until else None}


def load_portfolio(session: Session, run_id: str) -> Portfolio:
    run = session.get(Run, run_id)
    if run is None or run.checkpoint is None:
        raise ValueError("No recovery checkpoint; legacy runs remain read-only")
    portfolio = Portfolio(run.settings["initial_cash"])
    rows = session.execute(select(FillRow, OrderRow).join(OrderRow, FillRow.order_id == OrderRow.id)
                           .join(Trade, FillRow.trade_id == Trade.id).where(Trade.run_id == run_id)
                           .order_by(FillRow.timestamp, OrderRow.created_at, FillRow.created_at, FillRow.id))
    for fill, order in rows:
        portfolio.apply(order.payload["symbol"], order.payload["side"],
                        Fill(fill_id=fill.id, quantity=fill.quantity, price=fill.price, fee=fill.fee,
                             timestamp=fill.timestamp if fill.timestamp.tzinfo else fill.timestamp.replace(tzinfo=timezone.utc)))
    for row in session.scalars(select(PositionRow).where(PositionRow.run_id == run_id)):
        position = portfolio.positions.setdefault(row.symbol, Position())
        if position.quantity != row.quantity or not isclose(position.average_entry, row.average_entry, rel_tol=0, abs_tol=1e-8):
            raise ValueError("Position does not reconcile with fills")
        position.mark = row.mark
    if not isclose(portfolio.cash, run.cash, rel_tol=0, abs_tol=1e-8) or not isclose(portfolio.equity, run.equity, rel_tol=0, abs_tol=1e-8):
        raise ValueError("Cash/equity does not reconcile with fills")
    state = run.checkpoint
    portfolio.current_day = date.fromisoformat(state["day"]) if state["day"] else None
    portfolio.day_start_equity = state["day_start_equity"]
    portfolio.trades_today = state["trades_today"]
    portfolio.consecutive_losses = state["consecutive_losses"]
    portfolio.cooldown_until = datetime.fromisoformat(state["cooldown_until"]) if state["cooldown_until"] else None
    return portfolio
