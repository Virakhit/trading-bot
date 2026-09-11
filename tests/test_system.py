from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.config import Settings
from app.core.runner import run_paper
from app.core.types import Bar, Fill, OrderRequest, Signal
from app.database import engine_for
from app.database.models import (Base, FillRow, OrderRow, PositionRow, RiskRow, Run, SignalRow,
                                 Snapshot, StrategyVersion, SystemEvent, Trade, TradeMetrics)
from app.execution import PaperExecutionEngine, WebullExecutionEngine
from app.journal import register_strategy
from app.market_data import CSVProvider
from app.portfolio import Portfolio, Position
from app.risk import RiskEngine
from app.strategies import Context, MomentumStrategy

T = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)


def bar(price=100, minute=0, volume=1000):
    return Bar(symbol="TEST", timestamp=T + timedelta(minutes=minute), open=price, high=price + 1,
               low=price - 1, close=price, bid=price - .01, ask=price + .01, volume=volume)


def signal(action="BUY", b=None):
    b = b or bar()
    return Signal(strategy_id="test", strategy_version="1", symbol=b.symbol, timestamp=b.timestamp,
                  action=action, signal_price=b.close, reasons=["TEST"])


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


@pytest.fixture
def engine(tmp_path):
    url = "sqlite:///" + str(tmp_path / "test.db")
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
    engine = engine_for(url)
    yield engine
    engine.dispose()


def test_strategy_contract():
    strategy = MomentumStrategy()
    result = strategy.evaluate(Context(tuple(bar(p, i) for i, p in enumerate([100, 100, 101])), 0))
    assert result.action == "BUY"
    assert result.strategy_version == strategy.version
    assert result.indicators["sma"] == pytest.approx(100 + 1 / 3)
    assert strategy.evaluate(Context((bar(),), 0)).action == "HOLD"


@pytest.mark.parametrize("config,code", [
    ({"kill_switch": True}, "KILL_SWITCH"),
    ({"max_position_size": 1}, "MAX_POSITION_SIZE"),
    ({"max_position_pct": .001}, "MAX_POSITION_PCT"),
    ({"max_symbol_exposure": 1}, "MAX_SYMBOL_EXPOSURE"),
])
def test_risk_rejection(config, code):
    result = RiskEngine(settings(**config)).evaluate(signal(), 10, bar(), Portfolio(10000))
    assert result.status == "REJECTED" and code in result.codes


@pytest.mark.parametrize("state,code", [
    ({"cash": 1}, "INSUFFICIENT_CASH"),
    ({"day_start_equity": 11000}, "MAX_DAILY_LOSS"),
    ({"trades_today": 20}, "MAX_TRADES_PER_DAY"),
    ({"cooldown_until": T + timedelta(minutes=1)}, "LOSS_COOLDOWN"),
    ({"positions": {str(i): Position(1, 100, 100) for i in range(5)}}, "MAX_CONCURRENT_POSITIONS"),
])
def test_portfolio_risk_limits(state, code):
    portfolio = Portfolio(10000)
    for key, value in state.items():
        setattr(portfolio, key, value)
    assert code in RiskEngine(settings()).evaluate(signal(), 10, bar(), portfolio).codes


def test_kill_switch_allows_exit_and_no_short():
    risk = RiskEngine(settings(kill_switch=True))
    portfolio = Portfolio(9000, {"TEST": Position(10, 100, 100)})
    assert risk.evaluate(signal("EXIT"), 10, bar(), portfolio).status == "APPROVED"
    assert "NO_POSITION" in risk.evaluate(signal("SELL"), 11, bar(), portfolio).codes


def test_position_weighted_average_realized_and_fees():
    p = Portfolio(10000)
    p.apply("TEST", "BUY", Fill(quantity=10, price=100, fee=1, timestamp=T))
    p.apply("TEST", "BUY", Fill(quantity=10, price=110, fee=1, timestamp=T))
    assert p.positions["TEST"].average_entry == 105
    assert p.apply("TEST", "SELL", Fill(quantity=5, price=120, fee=1, timestamp=T)) == 75
    assert p.positions["TEST"].quantity == 15
    p.apply("TEST", "SELL", Fill(quantity=15, price=100, fee=1, timestamp=T))
    assert p.cash == 9996 and p.realized_pnl == 0 and p.fees == 4
    with pytest.raises(ValueError, match="NO_SHORT"):
        p.apply("TEST", "SELL", Fill(quantity=1, price=100, fee=0, timestamp=T))


def test_partial_fill_spread_slippage_fees_and_limits():
    execution = PaperExecutionEngine(settings(slippage_bps=10, fee_per_share=.1))
    order = OrderRequest(signal_id="s", symbol="TEST", side="BUY", quantity=10)
    result = execution.execute(order, bar(volume=3))
    assert result.status == "PARTIALLY_FILLED" and result.fills[0].quantity == 3
    assert result.fills[0].price == pytest.approx(100.01 * 1.001)
    assert result.fills[0].fee == pytest.approx(.3)
    assert execution.execute(order, bar(volume=0)).status == "REJECTED"
    limit = order.model_copy(update={"order_type": "LIMIT", "limit_price": 100})
    assert execution.execute(limit, bar()).status == "CANCELLED"
    assert execution.execute(limit.model_copy(update={"limit_price": 101}), bar()).status == "FILLED"
    sell = order.model_copy(update={"side": "SELL"})
    assert execution.execute(sell, bar()).fills[0].price == pytest.approx(99.99 * .999)


@pytest.mark.parametrize("price,reason", [(96, "STOP_TRIGGERED"), (106, "TAKE_PROFIT_TRIGGERED"), (101, None)])
def test_protective_exit(price, reason):
    assert RiskEngine(settings()).exit_reason(Position(10, 100, 100), bar(price)) == reason


class HoldUntilEnd(MomentumStrategy):
    strategy_id = "test-hold"
    def evaluate(self, context):
        b = context.bars[-1]
        return Signal(strategy_id=self.strategy_id, strategy_version=self.version, symbol=b.symbol, timestamp=b.timestamp,
                      action="BUY" if len(context.bars) == 1 else "HOLD", signal_price=b.close, reasons=["TEST"])


def test_lifecycle_mae_mfe_and_reconstruction(engine):
    config = settings(slippage_bps=0, fee_per_share=.1, stop_loss_pct=None, take_profit_pct=None)
    run_id = run_paper(engine, config, HoldUntilEnd(), [bar(p, i) for i, p in enumerate([100, 98, 105, 102])])
    with Session(engine) as session:
        trade = session.scalar(select(Trade).where(Trade.run_id == run_id))
        assert trade.status == "CLOSED"
        metrics = session.scalar(select(TradeMetrics).where(TradeMetrics.trade_id == trade.id)).payload
        assert metrics["gross_pnl"] == pytest.approx(19.8)
        assert metrics["net_pnl"] == pytest.approx(17.8)
        assert metrics["fees"] == 2
        assert metrics["mae"] == pytest.approx(-20.1)
        assert metrics["mfe"] == pytest.approx(49.9)
        assert metrics["holding_seconds"] == 180
        assert metrics["total_slippage"] == pytest.approx(.2)
        fills = session.execute(select(FillRow, OrderRow, SignalRow, Snapshot)
                                .join(OrderRow, FillRow.order_id == OrderRow.id)
                                .join(SignalRow, OrderRow.signal_id == SignalRow.id)
                                .join(Snapshot, Snapshot.signal_id == SignalRow.id)
                                .where(FillRow.trade_id == trade.id)).all()
        assert len(fills) == 2
        assert {r[2].id for r in fills} == {trade.entry_signal_id, trade.exit_signal_id}
        assert all(r[2].version_id == trade.version_id for r in fills)
        assert fills[0][3].payload["bid"] > 0
        assert session.get(PositionRow, trade.position_id).quantity == 0
        assert session.get(Run, run_id).cash == pytest.approx(10017.8)
        assert session.scalar(select(func.count()).select_from(RiskRow).where(RiskRow.status == "REJECTED")) == 3
        assert session.scalar(select(func.count()).select_from(Snapshot)) == 5


def test_stop_lifecycle_and_partial_exit(engine):
    run_id = run_paper(engine, settings(), HoldUntilEnd(), [bar(100), bar(95, 1, 3), bar(94, 2)])
    with Session(engine) as session:
        assert session.get(Run, run_id).status == "COMPLETED"
        assert session.scalar(select(func.count()).select_from(FillRow)) == 3
        assert session.scalar(select(func.count()).select_from(SystemEvent).where(SystemEvent.event_type == "STOP_TRIGGERED")) == 2
        assert session.scalar(select(Trade)).status == "CLOSED"


def test_no_liquidity_leaves_explicit_open_position(engine):
    run_id = run_paper(engine, settings(), HoldUntilEnd(), [bar(), bar(101, 1, 0)])
    with Session(engine) as session:
        assert session.get(Run, run_id).status == "OPEN_POSITIONS"
        assert session.scalar(select(PositionRow)).quantity == 10
        assert session.scalar(select(func.count()).select_from(TradeMetrics)) == 0


def test_strategy_version_immutable(engine):
    with Session(engine) as session, session.begin():
        original = register_strategy(session, MomentumStrategy())
        original_id = original.id
        assert register_strategy(session, MomentumStrategy()).id == original_id
        changed = MomentumStrategy()
        changed.warmup_bars = 4
        with pytest.raises(ValueError, match="increment version"):
            register_strategy(session, changed)
        changed.version = "1.0.1"
        assert register_strategy(session, changed).id != original_id
        assert session.get(StrategyVersion, original_id).config["warmup_bars"] == 3


def test_invalid_input_and_live_disabled():
    with pytest.raises(ValidationError):
        settings(mode="live")
    with pytest.raises(ValidationError):
        bar(float("nan"))
    with pytest.raises(ValidationError):
        OrderRequest(signal_id="s", symbol="TEST", side="BUY", quantity=1, order_type="LIMIT")
    with pytest.raises(RuntimeError, match="disabled"):
        WebullExecutionEngine().execute(OrderRequest(signal_id="s", symbol="TEST", side="BUY", quantity=1), bar())


def test_day_reset():
    p = Portfolio(10000)
    p.new_day(T)
    p.trades_today = 20
    p.new_day(T + timedelta(days=1))
    assert p.trades_today == 0 and p.day_start_equity == 10000


class ReentryStrategy(MomentumStrategy):
    strategy_id = "test-reentry"
    def evaluate(self, context):
        b = context.bars[-1]
        return Signal(strategy_id=self.strategy_id, strategy_version=self.version, symbol=b.symbol, timestamp=b.timestamp,
                      action="EXIT" if context.position_quantity else "BUY", signal_price=b.close, reasons=["TEST"])


def test_losing_trade_starts_cooldown(engine):
    run_paper(engine, settings(loss_streak_limit=1, stop_loss_pct=None), ReentryStrategy(),
              [bar(p, i) for i, p in enumerate([100, 99, 100, 101])])
    with Session(engine) as session:
        decisions = list(session.scalars(select(RiskRow)))
        assert any("LOSS_COOLDOWN" in d.codes for d in decisions)
        assert session.scalar(select(func.count()).select_from(Trade)) == 1


class FailingStrategy(HoldUntilEnd):
    strategy_id = "test-failure"
    def evaluate(self, context):
        if len(context.bars) == 2:
            raise ValueError("simulated failure")
        return super().evaluate(context)


def test_failure_retains_only_committed_bars(engine):
    with pytest.raises(ValueError, match="simulated failure"):
        run_paper(engine, settings(), FailingStrategy(), [bar(), bar(101, 1)])
    with Session(engine) as session:
        run = session.scalar(select(Run))
        assert run.status == "FAILED"
        assert session.scalar(select(func.count()).select_from(SignalRow)) == 1
        assert session.scalar(select(PositionRow)).quantity == 10
        assert session.scalar(select(func.count()).select_from(SystemEvent).where(SystemEvent.event_type == "SYSTEM_ERROR")) == 1
        from scripts.verify_lifecycle import verify
        assert verify(session, run)["verified"]


def test_final_partial_strategy_exit_does_not_reuse_volume(engine):
    run_id = run_paper(engine, settings(stop_loss_pct=None), ReentryStrategy(), [bar(), bar(101, 1, 3)])
    with Session(engine) as session:
        assert session.get(Run, run_id).status == "OPEN_POSITIONS"
        assert session.scalar(select(PositionRow)).quantity == 7
        assert session.scalar(select(func.count()).select_from(FillRow)) == 2


def test_rejected_entries_preserve_context(engine):
    run_id = run_paper(engine, settings(kill_switch=True), HoldUntilEnd(), [bar(), bar(101, 1)])
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(OrderRow)) == 0
        assert session.scalar(select(func.count()).select_from(Snapshot)) == 2
        assert session.scalar(select(Snapshot)).payload["portfolio"]["cash"] == 10000
        from scripts.verify_lifecycle import verify
        assert verify(session, session.get(Run, run_id))["rejected_signals"] == 2


def test_csv_rejects_duplicates(tmp_path):
    from app.cli.__main__ import seed
    path = tmp_path / "bars.csv"
    seed(path)
    assert len(CSVProvider(path).historical("DEMO")) == 11
    with path.open("a") as file:
        file.write(path.read_text().splitlines()[1] + "\n")
    with pytest.raises(ValueError, match="unique"):
        CSVProvider(path)


def test_timestamp_normalizes_before_daily_risk():
    values = bar().model_dump()
    values["timestamp"] = "2026-01-06T00:30:00+07:00"
    normalized = Bar.model_validate(values)
    assert normalized.timestamp.isoformat() == "2026-01-05T17:30:00+00:00"
    with pytest.raises(ValidationError):
        Bar.model_validate({**values, "timestamp": "2026-01-06T00:30:00"})
