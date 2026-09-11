from datetime import datetime, timezone
from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from app.core.types import uid


class Base(DeclarativeBase):
    pass


class Record:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class StrategyRow(Record, Base):
    __tablename__ = "strategies"
    name: Mapped[str] = mapped_column(String(200))


class StrategyVersion(Record, Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (UniqueConstraint("strategy_id", "version"),)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"))
    version: Mapped[str] = mapped_column(String(100))
    config: Mapped[dict] = mapped_column(JSON)
    config_hash: Mapped[str] = mapped_column(String(64))
    code_hash: Mapped[str] = mapped_column(String(64))
    source_code: Mapped[str | None] = mapped_column(Text)
    git_sha: Mapped[str | None] = mapped_column(String(64))


class Run(Record, Base):
    __tablename__ = "runs"
    version_id: Mapped[str] = mapped_column(ForeignKey("strategy_versions.id"))
    settings: Mapped[dict] = mapped_column(JSON)
    data_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="RUNNING")
    cash: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)


class SignalRow(Record, Base):
    __tablename__ = "signals"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    version_id: Mapped[str] = mapped_column(ForeignKey("strategy_versions.id"))
    symbol: Mapped[str] = mapped_column(String(32))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    action: Mapped[str] = mapped_column(String(10))
    payload: Mapped[dict] = mapped_column(JSON)


class Snapshot(Record, Base):
    __tablename__ = "market_snapshots"
    signal_id: Mapped[str] = mapped_column(ForeignKey("signals.id"), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)


class RiskRow(Record, Base):
    __tablename__ = "risk_decisions"
    signal_id: Mapped[str] = mapped_column(ForeignKey("signals.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20))
    codes: Mapped[list] = mapped_column(JSON)


class OrderRow(Record, Base):
    __tablename__ = "orders"
    signal_id: Mapped[str] = mapped_column(ForeignKey("signals.id"))
    status: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON)


class PositionRow(Record, Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("run_id", "symbol"),)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    symbol: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[int] = mapped_column(Integer)
    average_entry: Mapped[float] = mapped_column(Float)
    mark: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float)


class Trade(Record, Base):
    __tablename__ = "trades"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    version_id: Mapped[str] = mapped_column(ForeignKey("strategy_versions.id"))
    position_id: Mapped[str] = mapped_column(ForeignKey("positions.id"))
    entry_signal_id: Mapped[str] = mapped_column(ForeignKey("signals.id"))
    exit_signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"))
    status: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON)


class FillRow(Record, Base):
    __tablename__ = "fills"
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    trade_id: Mapped[str] = mapped_column(ForeignKey("trades.id"))
    position_id: Mapped[str] = mapped_column(ForeignKey("positions.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TradeMetrics(Record, Base):
    __tablename__ = "trade_metrics"
    trade_id: Mapped[str] = mapped_column(ForeignKey("trades.id"), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)


class DailyMetrics(Record, Base):
    __tablename__ = "daily_metrics"
    __table_args__ = (UniqueConstraint("run_id", "day"),)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    day: Mapped[str] = mapped_column(String(10))
    payload: Mapped[dict] = mapped_column(JSON)


class SystemEvent(Record, Base):
    __tablename__ = "system_events"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSON)
