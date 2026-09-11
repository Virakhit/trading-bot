from datetime import timedelta
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from app.analytics import trade_metrics
from app.config import Settings
from app.core.types import Bar, OrderRequest, Signal
from app.database.models import (DailyMetrics, FillRow, OrderRow, PositionRow, RiskRow, Run,
                                 SignalRow, Snapshot, Trade, TradeMetrics)
from app.execution import PaperExecutionEngine
from app.journal import digest, event, register_strategy
from app.portfolio import Portfolio, Position
from app.risk import RiskDecision, RiskEngine
from app.strategies import Context, Strategy


def run_paper(engine: Engine, settings: Settings, strategy: Strategy, bars: list[Bar], quantity: int = 10) -> str:
    """One isolated long-only portfolio per run, one symbol, close-quote execution."""
    if not bars or quantity <= 0:
        raise ValueError("Bars and positive quantity required")
    if len({b.symbol for b in bars}) != 1 or any(a.timestamp >= b.timestamp for a, b in zip(bars, bars[1:])):
        raise ValueError("Run requires one symbol and strictly increasing timestamps")
    portfolio = Portfolio(settings.initial_cash)
    risk = RiskEngine(settings)
    execution = PaperExecutionEngine(settings)
    with Session(engine) as session, session.begin():
        version = register_strategy(session, strategy)
        run = Run(version_id=version.id, settings=settings.model_dump(exclude={"database_url"}),
                  data_hash=digest([b.model_dump(mode="json") for b in bars]), cash=portfolio.cash, equity=portfolio.equity)
        session.add(run)
        session.flush()
        run_id, version_id = run.id, version.id
    active_trade_id: str | None = None
    try:
        for index, bar in enumerate(bars):
            with Session(engine) as session, session.begin():
                # Mark after resetting the day so overnight changes count toward daily loss.
                portfolio.new_day(bar.timestamp)
                position = portfolio.positions.setdefault(bar.symbol, Position(mark=bar.close))
                position.mark = bar.close
                db_position = session.scalar(select(PositionRow).where(PositionRow.run_id == run_id, PositionRow.symbol == bar.symbol))
                if db_position is None:
                    db_position = PositionRow(run_id=run_id, symbol=bar.symbol, quantity=0, average_entry=0, mark=bar.close, unrealized_pnl=0)
                    session.add(db_position)
                    session.flush()
                trade = session.get(Trade, active_trade_id) if active_trade_id else None
                if trade:
                    state = dict(trade.payload)
                    state["mae"] = min(state["mae"], position.unrealized_pnl)
                    state["mfe"] = max(state["mfe"], position.unrealized_pnl)
                    trade.payload = state
                signal = strategy.evaluate(Context(tuple(bars[:index + 1]), position.quantity))
                if (signal.strategy_id, signal.strategy_version, signal.symbol, signal.timestamp) != (strategy.strategy_id, strategy.version, bar.symbol, bar.timestamp):
                    raise ValueError("Strategy returned mismatched signal provenance")
                reason = risk.exit_reason(position, bar)
                block = "PROTECTIVE_EXIT_PENDING" if reason else ("END_OF_DATA_ENTRY_DISABLED" if index == len(bars) - 1 and signal.action == "BUY" else None)
                process_signal(session, run_id, version_id, signal, bar, position, portfolio, risk,
                               execution, quantity, db_position, trade, block)
                # A protective decision is a separate signal; preserve the strategy's original decision.
                trade = session.scalar(select(Trade).where(Trade.run_id == run_id, Trade.status == "OPEN"))
                if position.quantity and (reason or (index == len(bars) - 1 and signal.action not in ("EXIT", "SELL"))):
                    reason = reason or "END_OF_DATA"
                    protective = Signal(strategy_id=strategy.strategy_id, strategy_version=strategy.version,
                                        symbol=bar.symbol, timestamp=bar.timestamp, action="EXIT", signal_price=bar.close,
                                        reasons=[reason], metadata={"source": "risk" if reason != "END_OF_DATA" else "simulation"})
                    process_signal(session, run_id, version_id, protective, bar, position, portfolio, risk,
                                   execution, quantity, db_position, trade)
                    event(session, run_id, bar.timestamp, reason, protective.signal_id)
                active = session.scalar(select(Trade).where(Trade.run_id == run_id, Trade.status == "OPEN"))
                active_trade_id = active.id if active else None
                db_position.quantity = position.quantity
                db_position.average_entry = position.average_entry
                db_position.mark = bar.close
                position.mark = bar.close
                db_position.unrealized_pnl = position.unrealized_pnl
                run = session.get(Run, run_id)
                run.cash, run.equity = portfolio.cash, portfolio.equity
                daily = session.scalar(select(DailyMetrics).where(DailyMetrics.run_id == run_id, DailyMetrics.day == str(bar.timestamp.date())))
                if daily is None:
                    daily = DailyMetrics(run_id=run_id, day=str(bar.timestamp.date()), payload={})
                    session.add(daily)
                daily.payload = {"cash": portfolio.cash, "equity": portfolio.equity, "buying_power": portfolio.buying_power,
                                 "realized_pnl": portfolio.realized_pnl, "fees": portfolio.fees,
                                 "unrealized_pnl": sum(p.unrealized_pnl for p in portfolio.positions.values()),
                                 "daily_pnl": portfolio.equity - portfolio.day_start_equity, "entry_orders": portfolio.trades_today}
        with Session(engine) as session, session.begin():
            session.get(Run, run_id).status = "OPEN_POSITIONS" if active_trade_id else "COMPLETED"
            event(session, run_id, bars[-1].timestamp, "SESSION_FINISHED", open_positions=bool(active_trade_id))
    except Exception as exc:
        with Session(engine) as session, session.begin():
            session.get(Run, run_id).status = "FAILED"
            event(session, run_id, bar.timestamp, "SYSTEM_ERROR", error_type=type(exc).__name__)
        raise
    return run_id


def process_signal(session: Session, run_id: str, version_id: str, signal: Signal, bar: Bar,
                   position: Position, portfolio: Portfolio, risk: RiskEngine, execution: PaperExecutionEngine,
                   quantity: int, db_position: PositionRow, trade: Trade | None, block: str | None = None) -> None:
    session.add(SignalRow(id=signal.signal_id, run_id=run_id, version_id=version_id, symbol=signal.symbol,
                          timestamp=signal.timestamp, action=signal.action, payload=signal.model_dump(mode="json")))
    session.flush()
    session.add(Snapshot(signal_id=signal.signal_id, payload={**bar.model_dump(mode="json"), "price": bar.close,
                                                            "spread": bar.spread, "indicators": signal.indicators,
                                                            "metadata": signal.metadata,
                                                            "portfolio": {"cash": portfolio.cash, "equity": portfolio.equity,
                                                                          "quantity": position.quantity, "average_entry": position.average_entry,
                                                                          "realized_pnl": portfolio.realized_pnl, "fees": portfolio.fees,
                                                                          "day_start_equity": portfolio.day_start_equity,
                                                                          "trades_today": portfolio.trades_today,
                                                                          "consecutive_losses": portfolio.consecutive_losses,
                                                                          "cooldown_until": portfolio.cooldown_until.isoformat() if portfolio.cooldown_until else None}}))
    event(session, run_id, bar.timestamp, "SIGNAL_CREATED", signal.signal_id)
    requested = position.quantity if signal.action in ("EXIT", "SELL") else quantity
    decision = RiskDecision("REJECTED", (block,)) if block else risk.evaluate(signal, requested, bar, portfolio)
    session.add(RiskRow(signal_id=signal.signal_id, status=decision.status, codes=list(decision.codes)))
    if decision.status == "REJECTED":
        event(session, run_id, bar.timestamp, "SIGNAL_REJECTED", signal.signal_id, codes=decision.codes)
        if "NO_ACTION" not in decision.codes:
            event(session, run_id, bar.timestamp, "KILL_SWITCH_TRIGGERED" if "KILL_SWITCH" in decision.codes else "RISK_LIMIT_TRIGGERED", signal.signal_id, codes=decision.codes)
        return
    order = OrderRequest(signal_id=signal.signal_id, symbol=bar.symbol,
                         side="BUY" if signal.action == "BUY" else "SELL", quantity=requested)
    result = execution.execute(order, bar)
    row = OrderRow(signal_id=signal.signal_id, status=result.status, payload={**order.model_dump(), "reason": result.reason})
    session.add(row)
    session.flush()
    event(session, run_id, bar.timestamp, "ORDER_CREATED", signal.signal_id, order_id=row.id)
    event(session, run_id, bar.timestamp, "ORDER_SUBMITTED", signal.signal_id, order_id=row.id)
    event(session, run_id, bar.timestamp, "ORDER_" + result.status, signal.signal_id, order_id=row.id, reason=result.reason)
    if result.status == "PARTIALLY_FILLED":
        event(session, run_id, bar.timestamp, "ORDER_CANCELLED", signal.signal_id, order_id=row.id, reason="IOC_REMAINDER_CANCELLED")
    for fill in result.fills:
        opening = position.quantity == 0
        if opening:
            trade = Trade(run_id=run_id, version_id=version_id, position_id=db_position.id,
                          entry_signal_id=signal.signal_id, status="OPEN", payload={})
            session.add(trade)
            session.flush()
            state = {"entry_quantity": 0, "entry_cost": 0, "exit_value": 0, "fees": 0,
                     "mae": 0, "mfe": 0, "entry_slippage": 0, "exit_slippage": 0,
                     "entry_time": bar.timestamp.isoformat(), "entry_reason": signal.reasons}
        else:
            state = dict(trade.payload)
        if order.side == "BUY":
            state["entry_quantity"] += fill.quantity
            state["entry_cost"] += fill.quantity * fill.price
            state["entry_slippage"] += (fill.price - signal.signal_price) * fill.quantity
            portfolio.trades_today += 1
        else:
            state["exit_value"] += fill.quantity * fill.price
            state["exit_slippage"] += (signal.signal_price - fill.price) * fill.quantity
            state["exit_time"] = bar.timestamp.isoformat()
            state["exit_reason"] = signal.reasons
            trade.exit_signal_id = signal.signal_id
            # Include the execution price before reducing the position.
            excursion = position.quantity * (fill.price - position.average_entry)
            state["mae"], state["mfe"] = min(state["mae"], excursion), max(state["mfe"], excursion)
        state["fees"] += fill.fee
        portfolio.apply(bar.symbol, order.side, fill)
        if order.side == "BUY":
            excursion = position.quantity * (bar.close - position.average_entry)
            state["mae"], state["mfe"] = min(state["mae"], excursion), max(state["mfe"], excursion)
        session.add(FillRow(order_id=row.id, trade_id=trade.id, position_id=db_position.id,
                            quantity=fill.quantity, price=fill.price, fee=fill.fee, timestamp=fill.timestamp))
        trade.payload = state
        event(session, run_id, bar.timestamp, "POSITION_OPENED" if opening else "POSITION_UPDATED", signal.signal_id,
              position_id=db_position.id, trade_id=trade.id, quantity=position.quantity, average_entry=position.average_entry)
        if not position.quantity:
            trade.status = "CLOSED"
            metrics = trade_metrics(state)
            session.add(TradeMetrics(trade_id=trade.id, payload=metrics))
            portfolio.consecutive_losses = portfolio.consecutive_losses + 1 if metrics["net_pnl"] < 0 else 0
            if portfolio.consecutive_losses >= risk.settings.loss_streak_limit:
                portfolio.cooldown_until = bar.timestamp + timedelta(seconds=risk.settings.cooldown_seconds)
            event(session, run_id, bar.timestamp, "POSITION_CLOSED", signal.signal_id, trade_id=trade.id, position_id=db_position.id)
    session.flush()
