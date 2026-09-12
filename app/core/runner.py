from datetime import timedelta
from math import isclose
from sqlalchemy import Engine, select, update, or_
from sqlalchemy.orm import Session
from app.analytics import trade_metrics
from app.config import Settings
from app.core.types import Bar, ExecutionResult, OrderRequest, Signal
from app.database.models import (DailyMetrics, FillRow, OrderRow, PositionRow, RiskRow, Run,
                                 SignalRow, Snapshot, Trade, TradeMetrics)
from app.execution import PaperExecutionEngine
from app.journal import digest, event, register_strategy
from app.portfolio import Portfolio, Position
from app.portfolio.recovery import checkpoint, load_portfolio
from app.risk import RiskDecision, RiskEngine
from app.strategies import Context, Strategy


class ConcurrentRunError(RuntimeError):
    pass


def run_paper(engine: Engine, settings: Settings, strategy: Strategy, bars: list[Bar], quantity: int = 10,
              *, run_id: str | None = None, max_bars: int | None = None,
              execution_engine: PaperExecutionEngine | None = None) -> str:
    """One isolated long-only portfolio per run, one symbol, close-quote execution."""
    if not bars or quantity <= 0 or (max_bars is not None and max_bars <= 0):
        raise ValueError("Bars and positive quantity required")
    if len({b.symbol for b in bars}) != 1 or any(a.timestamp >= b.timestamp for a, b in zip(bars, bars[1:])):
        raise ValueError("Run requires one symbol and strictly increasing timestamps")
    portfolio = Portfolio(settings.initial_cash)
    risk = RiskEngine(settings)
    execution = execution_engine or PaperExecutionEngine(settings)
    with Session(engine) as session, session.begin():
        version = register_strategy(session, strategy)
        run_settings = settings.model_dump(exclude={"database_url"})
        data_hash = digest([b.model_dump(mode="json") for b in bars])
        if run_id is None:
            run = Run(version_id=version.id, settings=run_settings, data_hash=data_hash,
                      cash=portfolio.cash, equity=portfolio.equity, checkpoint=checkpoint(portfolio, 0, quantity))
            session.add(run)
        else:
            run = session.get(Run, run_id)
            if run is None or run.checkpoint is None:
                raise ValueError("No recovery checkpoint; legacy runs remain read-only")
            if (run.version_id != version.id or run.settings != run_settings or run.data_hash != data_hash
                    or run.checkpoint["quantity"] != quantity):
                raise ValueError("Resume requires identical strategy, settings, quantity and input data")
            portfolio = load_portfolio(session, run_id)
        session.flush()
        run_id, version_id = run.id, version.id
        start_index, revision = run.checkpoint["next_index"], run.revision
        active_trade_id = session.scalar(select(Trade.id).where(Trade.run_id == run_id, Trade.status == "OPEN"))
    if start_index == len(bars):
        return run_id
    end_index = min(len(bars), start_index + max_bars) if max_bars else len(bars)
    try:
        for index in range(start_index, end_index):
            bar = bars[index]
            with Session(engine) as session, session.begin():
                claimed = session.execute(update(Run).where(Run.id == run_id, Run.revision == revision)
                                          .values(revision=revision + 1))
                if claimed.rowcount != 1:
                    raise ConcurrentRunError("Run changed; reload its checkpoint before retrying")
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
                run.checkpoint = checkpoint(portfolio, index + 1, quantity)
                run.status = "PAUSED" if index + 1 < len(bars) else ("OPEN_POSITIONS" if active_trade_id else "COMPLETED")
                daily = session.scalar(select(DailyMetrics).where(DailyMetrics.run_id == run_id, DailyMetrics.day == str(bar.timestamp.date())))
                if daily is None:
                    daily = DailyMetrics(run_id=run_id, day=str(bar.timestamp.date()), payload={})
                    session.add(daily)
                daily.payload = {"cash": portfolio.cash, "equity": portfolio.equity, "buying_power": portfolio.buying_power,
                                 "realized_pnl": portfolio.realized_pnl, "fees": portfolio.fees,
                                 "unrealized_pnl": sum(p.unrealized_pnl for p in portfolio.positions.values()),
                                 "daily_pnl": portfolio.equity - portfolio.day_start_equity, "entry_orders": portfolio.trades_today}
                if index + 1 == end_index:
                    event(session, run_id, bar.timestamp, "SESSION_PAUSED" if end_index < len(bars) else "SESSION_FINISHED",
                          open_positions=bool(active_trade_id))
            revision += 1
    except ConcurrentRunError:
        raise
    except Exception as exc:
        with Session(engine) as session, session.begin():
            session.get(Run, run_id).status = "FAILED"
            event(session, run_id, bar.timestamp, "SYSTEM_ERROR", error_type=type(exc).__name__)
        raise
    return run_id


def process_signal(session: Session, run_id: str, version_id: str, signal: Signal, bar: Bar,
                   position: Position, portfolio: Portfolio, risk: RiskEngine, execution: PaperExecutionEngine,
                   quantity: int, db_position: PositionRow, trade: Trade | None, block: str | None = None) -> None:
    decision_key = digest([version_id, signal.symbol, signal.timestamp.isoformat(), signal.metadata.get("source", "strategy")])
    existing = session.scalar(select(SignalRow).where(or_(SignalRow.id == signal.signal_id,
                              (SignalRow.run_id == run_id) & (SignalRow.decision_key == decision_key))))
    if existing:
        saved_snapshot = session.scalar(select(Snapshot).where(Snapshot.signal_id == existing.id))
        same_signal = {k: v for k, v in existing.payload.items() if k != "signal_id"} == signal.model_dump(mode="json", exclude={"signal_id"})
        same_bar = saved_snapshot and all(saved_snapshot.payload.get(k) == v for k, v in bar.model_dump(mode="json").items())
        if existing.run_id != run_id or existing.version_id != version_id or not same_signal or not same_bar:
            raise ValueError("SIGNAL_ID_CONFLICT")
        return
    session.add(SignalRow(id=signal.signal_id, decision_key=decision_key, run_id=run_id, version_id=version_id, symbol=signal.symbol,
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
    record_execution(session, run_id, version_id, signal, bar, position, portfolio, risk, order, result, db_position, trade)


def record_execution(session: Session, run_id: str, version_id: str, signal: Signal, bar: Bar,
                     position: Position, portfolio: Portfolio, risk: RiskEngine, order: OrderRequest,
                     result: ExecutionResult, db_position: PositionRow, trade: Trade | None) -> None:
    """Apply one terminal paper IOC result atomically; identical re-deliveries are no-ops."""
    saved_signal = session.get(SignalRow, signal.signal_id)
    saved_risk = session.scalar(select(RiskRow).where(RiskRow.signal_id == signal.signal_id))
    if (saved_signal is None or saved_signal.run_id != run_id or saved_signal.version_id != version_id
            or saved_signal.payload != signal.model_dump(mode="json") or saved_risk is None or saved_risk.status != "APPROVED"
            or order.signal_id != signal.signal_id or order.symbol != signal.symbol or bar.symbol != signal.symbol
            or order.side != ("BUY" if signal.action == "BUY" else "SELL") or db_position.run_id != run_id
            or db_position.symbol != signal.symbol or bar.timestamp != signal.timestamp):
        raise ValueError("EXECUTION_CONTEXT_MISMATCH")
    unique = {}
    for fill in result.fills:
        if fill.fill_id in unique and unique[fill.fill_id] != fill:
            raise ValueError("FILL_ID_CONFLICT")
        unique[fill.fill_id] = fill
    fills = list(unique.values())
    result = result.model_copy(update={"fills": fills})
    result_hash = digest(result.model_dump(mode="json"))
    existing = session.get(OrderRow, order.order_id)
    if existing:
        if (existing.signal_id != signal.signal_id or existing.payload.get("execution_hash") != result_hash
                or any(existing.payload.get(k) != v for k, v in order.model_dump().items())):
            raise ValueError("ORDER_ID_CONFLICT")
        return
    if session.scalar(select(OrderRow.id).where(OrderRow.signal_id == signal.signal_id)):
        raise ValueError("Signal already has a terminal IOC order")
    filled_quantity = sum(f.quantity for f in fills)
    if ((result.status == "FILLED" and filled_quantity != order.quantity)
            or (result.status == "PARTIALLY_FILLED" and not 0 < filled_quantity < order.quantity)
            or (result.status in ("CANCELLED", "REJECTED") and filled_quantity != 0)):
        raise ValueError("INVALID_FILLED_QUANTITY")
    if order.side == "SELL" and filled_quantity > position.quantity:
        raise ValueError("NO_SHORT_SELLING")
    if order.side == "BUY" and sum(f.quantity * f.price + f.fee for f in fills) > portfolio.cash + 1e-9:
        raise ValueError("INSUFFICIENT_CASH")
    for fill in fills:
        if session.get(FillRow, fill.fill_id) or fill.fill_id in portfolio.applied_fills:
            raise ValueError("FILL_ID_CONFLICT")
        if not isclose(fill.fee, fill.quantity * risk.settings.fee_per_share, rel_tol=0, abs_tol=1e-9):
            raise ValueError("INVALID_PAPER_FEE")
        if fill.timestamp != bar.timestamp:
            raise ValueError("INVALID_PAPER_FILL_TIMESTAMP")
        if order.order_type == "LIMIT" and ((order.side == "BUY" and fill.price > order.limit_price)
                                             or (order.side == "SELL" and fill.price < order.limit_price)):
            raise ValueError("LIMIT_PRICE_VIOLATION")
    row = OrderRow(id=order.order_id, signal_id=signal.signal_id, status=result.status,
                   payload={**order.model_dump(), "reason": result.reason, "execution_hash": result_hash})
    session.add(row)
    session.flush()
    portfolio.record_order(order.symbol)
    event(session, run_id, bar.timestamp, "ORDER_CREATED", signal.signal_id, order_id=row.id)
    event(session, run_id, bar.timestamp, "ORDER_SUBMITTED", signal.signal_id, order_id=row.id)
    event(session, run_id, bar.timestamp, "ORDER_" + result.status, signal.signal_id, order_id=row.id, reason=result.reason)
    if result.status == "PARTIALLY_FILLED":
        event(session, run_id, bar.timestamp, "ORDER_CANCELLED", signal.signal_id, order_id=row.id, reason="IOC_REMAINDER_CANCELLED")
    if fills and order.side == "BUY":
        portfolio.trades_today += 1
    for fill in fills:
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
        session.add(FillRow(id=fill.fill_id, order_id=row.id, trade_id=trade.id, position_id=db_position.id,
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
