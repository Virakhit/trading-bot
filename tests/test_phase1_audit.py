"""Phase 1 audit regressions; reuse existing demo/test strategies only."""
import json
import os
import subprocess
import sys
from uuid import NAMESPACE_URL, uuid4, uuid5
import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.cli.__main__ import seed
from app.core.runner import process_signal, record_execution, run_paper
from app.core.types import Bar, ExecutionResult, Fill, OrderRequest, Signal
from app.database.models import Base, FillRow, OrderRow, PositionRow, Run, SignalRow, Snapshot, StrategyVersion, Trade, TradeMetrics, SystemEvent
from app.execution import PaperExecutionEngine
from app.journal import register_strategy
from app.market_data import CSVProvider
from app.portfolio import Portfolio
from app.portfolio.recovery import load_portfolio
from app.risk import RiskEngine
from app.strategies import MomentumStrategy
from scripts.audit_phase1 import audit
from test_system import engine, bar, settings, T, HoldUntilEnd, ReentryStrategy


def demo(tmp_path):
    path = tmp_path / "demo.csv"
    seed(path)
    return path, CSVProvider(path).historical("DEMO")


def counts(session):
    return tuple(session.scalar(select(func.count()).select_from(model))
                 for model in (SignalRow, OrderRow, FillRow, PositionRow, Trade, TradeMetrics, SystemEvent))


def test_empty_migration_matches_models(engine):
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


def test_fresh_demo_decimal_reconciliation(engine, tmp_path):
    _, bars = demo(tmp_path)
    run_id = run_paper(engine, settings(), MomentumStrategy(), bars)
    with Session(engine) as session:
        result = audit(session, run_id)
        assert result["reconciliation"]["gross_realized_pnl"] == -11.202
        assert result["reconciliation"]["fees"] == .2
        assert result["reconciliation"]["slippage_already_in_fill_prices"] == 1.202
        assert result["reconciliation"]["net_pnl"] == -11.402
        assert result["reconciliation"]["ending_equity"] == 9988.598
        assert result["signal_categories"] == {"executed": 4, "rejected by risk": 0, "HOLD/no action": 7, "duplicate/prevented": 0, "other": 0}


@pytest.mark.parametrize("prices,expected_mae,expected_mfe,net", [
    ([100, 98, 105, 102], -20.1, 49.9, 19.7),
    ([100, 103, 101, 98], -20.2, 29.9, -20.3),
])
def test_independent_excursions_profitable_and_losing(engine, prices, expected_mae, expected_mfe, net):
    run_id = run_paper(engine, settings(slippage_bps=0, stop_loss_pct=None, take_profit_pct=None), HoldUntilEnd(),
                       [bar(p, i) for i, p in enumerate(prices)])
    with Session(engine) as session:
        result = audit(session, run_id)["trades"][0]["independently_recalculated"]
        assert result["mae"] == pytest.approx(expected_mae)
        assert result["mfe"] == pytest.approx(expected_mfe)
        assert result["net_pnl"] == pytest.approx(net)


def test_requested_weighted_cost_basis_example():
    p = Portfolio(10000)
    p.apply("TEST", "BUY", Fill(quantity=2, price=100, fee=.01, timestamp=T))
    p.apply("TEST", "BUY", Fill(quantity=3, price=110, fee=.015, timestamp=T))
    position = p.positions["TEST"]
    assert position.quantity == 5 and position.average_entry == 106
    assert position.quantity * position.average_entry == 530
    assert p.apply("TEST", "SELL", Fill(quantity=2, price=120, fee=.01, timestamp=T)) == 28
    assert position.quantity == 3 and position.average_entry == 106
    assert position.quantity * position.average_entry == 318
    assert p.apply("TEST", "SELL", Fill(quantity=3, price=100, fee=.015, timestamp=T)) == -18
    assert p.realized_pnl == 10 and position.quantity == 0 and position.average_entry == 0
    assert p.cash == pytest.approx(10009.95)


def test_fill_replay_is_noop_and_conflict_fails():
    p = Portfolio(10000)
    fill = Fill(quantity=2, price=100, fee=.01, timestamp=T)
    p.apply("TEST", "BUY", fill)
    cash = p.cash
    assert p.apply("TEST", "BUY", fill) == 0
    assert p.cash == cash and p.positions["TEST"].quantity == 2 and p.fees == .01
    with pytest.raises(ValueError, match="FILL_ID_CONFLICT"):
        p.apply("TEST", "BUY", fill.model_copy(update={"quantity": 3}))
    sell = Fill(quantity=2, price=110, fee=.01, timestamp=T)
    assert p.apply("TEST", "SELL", sell) == 20
    p.apply("TEST", "SELL", sell)
    assert p.realized_pnl == 20 and p.positions["TEST"].quantity == 0 and p.fees == .02


def test_execution_retry_has_stable_fill_identity():
    order = OrderRequest(signal_id="s", symbol="TEST", side="BUY", quantity=10)
    first = PaperExecutionEngine(settings()).execute(order, bar(volume=3))
    second = PaperExecutionEngine(settings()).execute(order, bar(volume=3))
    assert first == second


def test_process_restart_resume_and_replay(engine, tmp_path):
    path, _ = demo(tmp_path)
    env = {**os.environ, "DATABASE_URL": str(engine.url)}
    cmd = [sys.executable, "-m", "app.cli", "run-paper", "--csv", str(path)]
    first = subprocess.run([*cmd, "--max-bars", "3"], env=env, capture_output=True, text=True, check=True)
    run_id = first.stdout.strip()
    with Session(engine) as session:
        p = load_portfolio(session, run_id)
        assert p.positions["DEMO"].quantity == 10
        assert p.cash == pytest.approx(8989.64798)
        assert p.trades_today == 1 and p.day_start_equity == 10000
        trade = session.scalar(select(Trade))
        identities = (trade.id, trade.entry_signal_id, trade.position_id, trade.version_id)
        entry_fill_id = session.scalar(select(FillRow.id))
    engine.dispose()
    subprocess.run([*cmd, "--run-id", run_id], env=env, capture_output=True, text=True, check=True)
    with Session(engine) as session:
        p = load_portfolio(session, run_id)
        assert p.cash == pytest.approx(9988.598) and p.positions["DEMO"].quantity == 0
        trade = session.get(Trade, identities[0])
        assert (trade.id, trade.entry_signal_id, trade.position_id, trade.version_id) == identities
        assert session.get(FillRow, entry_fill_id)
        assert audit(session, run_id)["verified"]
        before = counts(session)
    subprocess.run([*cmd, "--run-id", run_id], env=env, capture_output=True, text=True, check=True)
    with Session(engine) as session:
        assert counts(session) == before


def test_recovery_rejects_changed_data_or_settings(engine, tmp_path):
    _, bars = demo(tmp_path)
    run_id = run_paper(engine, settings(), MomentumStrategy(), bars, max_bars=3)
    with pytest.raises(ValueError, match="identical"):
        run_paper(engine, settings(fee_per_share=.1), MomentumStrategy(), bars, run_id=run_id)
    with pytest.raises(ValueError, match="identical"):
        run_paper(engine, settings(), MomentumStrategy(), bars[:-1], run_id=run_id)
    with pytest.raises(ValueError, match="identical"):
        run_paper(engine, settings(), MomentumStrategy(), bars, quantity=20, run_id=run_id)


def test_recovery_rejects_corrupt_cash(engine, tmp_path):
    _, bars = demo(tmp_path)
    run_id = run_paper(engine, settings(), MomentumStrategy(), bars, max_bars=3)
    with Session(engine) as session, session.begin():
        session.get(Run, run_id).cash += 100
    with Session(engine) as session, pytest.raises(ValueError, match="reconcile"):
        load_portfolio(session, run_id)


def test_signal_and_order_event_replay_after_reload(engine, tmp_path):
    _, bars = demo(tmp_path)
    cfg = settings()
    run_id = run_paper(engine, cfg, MomentumStrategy(), bars, max_bars=3)
    with Session(engine) as session, session.begin():
        p = load_portfolio(session, run_id)
        trade = session.scalar(select(Trade))
        signal = Signal.model_validate(session.get(SignalRow, trade.entry_signal_id).payload)
        position_row = session.get(PositionRow, trade.position_id)
        position = p.positions["DEMO"]
        order_row = session.scalar(select(OrderRow))
        order = OrderRequest.model_validate({k: v for k, v in order_row.payload.items() if k in OrderRequest.model_fields})
        saved = session.scalar(select(FillRow))
        result = ExecutionResult(status=order_row.status, fills=[Fill(fill_id=saved.id, quantity=saved.quantity,
                                  price=saved.price, fee=saved.fee, timestamp=signal.timestamp)])
        before, cash = counts(session), p.cash
        for replay in (signal, signal.model_copy(update={"signal_id": str(uuid4())})):
            process_signal(session, run_id, trade.version_id, replay, bars[2], position, p, RiskEngine(cfg),
                           PaperExecutionEngine(cfg), 10, position_row, trade)
        for _ in range(2):
            record_execution(session, run_id, trade.version_id, signal, bars[2], position, p, RiskEngine(cfg), order, result, position_row, trade)
        assert counts(session) == before and p.cash == cash
        with pytest.raises(ValueError, match="SIGNAL_ID_CONFLICT"):
            process_signal(session, run_id, trade.version_id, signal.model_copy(update={"action": "SELL"}), bars[2],
                           position, p, RiskEngine(cfg), PaperExecutionEngine(cfg), 10, position_row, trade)
        with pytest.raises(ValueError, match="ORDER_ID_CONFLICT"):
            record_execution(session, run_id, trade.version_id, signal, bars[2], position, p, RiskEngine(cfg),
                             order.model_copy(update={"quantity": 20}), result, position_row, trade)


def test_multiple_fills_fee_per_fill_and_no_duplicates(engine, monkeypatch):
    execute = PaperExecutionEngine.execute
    def split(self, order, quote):
        result = execute(self, order, quote)
        if not result.fills:
            return result
        f = result.fills[0]
        one = f.model_copy(update={"fill_id": str(uuid5(NAMESPACE_URL, f.fill_id + "a")), "quantity": 2, "fee": 2 * self.settings.fee_per_share})
        two = f.model_copy(update={"fill_id": str(uuid5(NAMESPACE_URL, f.fill_id + "b")), "quantity": f.quantity - 2, "fee": (f.quantity - 2) * self.settings.fee_per_share})
        return result.model_copy(update={"fills": [one, one, two]})
    monkeypatch.setattr(PaperExecutionEngine, "execute", split)
    run_id = run_paper(engine, settings(), HoldUntilEnd(), [bar(), bar(101, 1)])
    with Session(engine) as session:
        p = load_portfolio(session, run_id)
        assert p.trades_today == 1
        assert p.fees == pytest.approx(.1)
        assert session.scalar(select(func.count()).select_from(FillRow)) == 4
        assert session.scalar(select(func.count()).select_from(PositionRow)) == 1
        metrics = session.scalar(select(TradeMetrics)).payload
        assert metrics["fees"] == pytest.approx(.1)
        assert metrics["net_pnl"] == pytest.approx(p.cash - 10000)
        assert p.positions["TEST"].quantity == 0


@pytest.mark.parametrize("bad_field,bad_value,error", [("fee", 0, "INVALID_PAPER_FEE"), ("quantity", 11, "INVALID_FILLED_QUANTITY")])
def test_invalid_execution_rolls_back_financial_state(engine, monkeypatch, bad_field, bad_value, error):
    execute = PaperExecutionEngine.execute
    def invalid(self, order, quote):
        result = execute(self, order, quote)
        return result.model_copy(update={"fills": [result.fills[0].model_copy(update={bad_field: bad_value})]})
    monkeypatch.setattr(PaperExecutionEngine, "execute", invalid)
    with pytest.raises(ValueError, match=error):
        run_paper(engine, settings(), HoldUntilEnd(), [bar(), bar(101, 1)])
    with Session(engine) as session:
        assert session.scalar(select(Run)).cash == 10000
        assert session.scalar(select(func.count()).select_from(FillRow)) == 0
        assert session.scalar(select(func.count()).select_from(PositionRow)) == 0


def test_configuration_change_preserves_closed_trade_history(engine, tmp_path):
    _, bars = demo(tmp_path)
    run_id = run_paper(engine, settings(), MomentumStrategy(), bars)
    with Session(engine) as session:
        before = audit(session, run_id)
    changed = MomentumStrategy()
    changed.warmup_bars = 4
    with Session(engine) as session, pytest.raises(ValueError, match="increment version"):
        register_strategy(session, changed)
    changed.version = "1.0.1"
    with Session(engine) as session, session.begin():
        new = register_strategy(session, changed)
        assert new.id != before["strategy"]["version_id"]
    with Session(engine) as session:
        assert audit(session, run_id) == before


def test_sell_over_position_is_atomic():
    p = Portfolio(10000)
    p.apply("TEST", "BUY", Fill(quantity=2, price=100, fee=.01, timestamp=T))
    before = p.cash, p.realized_pnl, p.fees, p.positions["TEST"].quantity
    with pytest.raises(ValueError, match="NO_SHORT_SELLING"):
        p.apply("TEST", "SELL", Fill(quantity=3, price=110, fee=.015, timestamp=T))
    assert (p.cash, p.realized_pnl, p.fees, p.positions["TEST"].quantity) == before


@pytest.mark.parametrize("entry_ask,exit_bid,expected", [(100.01, 101.99, .2), (99.02, 102.98, -19.6)])
def test_slippage_signs_include_price_improvement(engine, entry_ask, exit_bid, expected):
    first = Bar.model_validate({**bar().model_dump(), "bid": entry_ask - .02, "ask": entry_ask})
    last = Bar.model_validate({**bar(102, 1).model_dump(), "bid": exit_bid, "ask": exit_bid + .02})
    run_id = run_paper(engine, settings(slippage_bps=0, stop_loss_pct=None, take_profit_pct=None), HoldUntilEnd(), [first, last])
    with Session(engine) as session:
        result = audit(session, run_id)["trades"][0]
        assert all(step["requested_price"] is None for step in result["steps"])
        assert result["independently_recalculated"]["total_slippage"] == pytest.approx(expected)


def test_zero_fill_orders_charge_no_fees():
    execution = PaperExecutionEngine(settings())
    order = OrderRequest(signal_id="s", symbol="TEST", side="BUY", quantity=10)
    rejected = execution.execute(order, bar(volume=0))
    cancelled = execution.execute(order.model_copy(update={"order_type": "LIMIT", "limit_price": 99}), bar())
    assert rejected.status == "REJECTED" and cancelled.status == "CANCELLED"
    assert sum(f.fee for r in (rejected, cancelled) for f in r.fills) == 0


def test_interruption_after_committed_entry_can_resume(engine, tmp_path):
    _, bars = demo(tmp_path)
    strategy = MomentumStrategy()
    evaluate = strategy.evaluate
    def interrupted(context):
        if len(context.bars) == 4:
            raise KeyboardInterrupt("simulated process interruption")
        return evaluate(context)
    strategy.evaluate = interrupted
    with pytest.raises(KeyboardInterrupt):
        run_paper(engine, settings(), strategy, bars)
    with Session(engine) as session:
        run = session.scalar(select(Run))
        run_id = run.id
        assert run.checkpoint["next_index"] == 3
        assert load_portfolio(session, run_id).positions["DEMO"].quantity == 10
    run_paper(engine, settings(), MomentumStrategy(), bars, run_id=run_id)
    with Session(engine) as session:
        assert audit(session, run_id)["reconciliation"]["net_pnl"] == -11.402


def test_failure_after_fill_before_checkpoint_rolls_back_and_recovers(engine, tmp_path, monkeypatch):
    import app.core.runner as runner
    _, bars = demo(tmp_path)
    original = runner.checkpoint
    def fail_after_fill(portfolio, next_index, quantity):
        if next_index == 3:
            raise RuntimeError("simulated checkpoint write failure")
        return original(portfolio, next_index, quantity)
    monkeypatch.setattr(runner, "checkpoint", fail_after_fill)
    with pytest.raises(RuntimeError, match="checkpoint write"):
        run_paper(engine, settings(), MomentumStrategy(), bars)
    with Session(engine) as session:
        run = session.scalar(select(Run))
        run_id = run.id
        assert run.cash == 10000 and run.checkpoint["next_index"] == 2
        assert session.scalar(select(func.count()).select_from(FillRow)) == 0
        assert load_portfolio(session, run_id).positions["DEMO"].quantity == 0
    monkeypatch.setattr(runner, "checkpoint", original)
    run_paper(engine, settings(), MomentumStrategy(), bars, run_id=run_id)
    with Session(engine) as session:
        assert audit(session, run_id)["reconciliation"]["net_pnl"] == -11.402


def test_config_registration_takes_detached_snapshot(engine):
    strategy = MomentumStrategy()
    config = {"warmup_bars": 3, "nested": {"thresholds": [1, 2]}}
    strategy.config = lambda: config
    with Session(engine) as session, session.begin():
        version = register_strategy(session, strategy)
        version_id = version.id
        config["nested"]["thresholds"].append(3)
        assert version.config["nested"]["thresholds"] == [1, 2]
    with Session(engine) as session:
        assert session.get(StrategyVersion, version_id).config["nested"]["thresholds"] == [1, 2]
