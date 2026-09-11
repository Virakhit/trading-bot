from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, TypeDecorator, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from app.core.types import uid


class Base(DeclarativeBase):
    pass


class ExactDecimal(TypeDecorator):
    """Lossless decimal text storage; conversion never passes through binary float."""
    impl = String(80)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(value)

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


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
    checkpoint: Mapped[dict | None] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer, server_default="0")


class SignalRow(Record, Base):
    __tablename__ = "signals"
    __table_args__ = (UniqueConstraint("run_id", "decision_key", name="uq_signal_decision"),)
    decision_key: Mapped[str | None] = mapped_column(String(64))
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


class BrokerAccount(Record, Base):
    __tablename__ = "broker_accounts"
    __table_args__ = (UniqueConstraint("run_id", "broker", "environment"),)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    broker: Mapped[str] = mapped_column(String(40))
    environment: Mapped[str] = mapped_column(String(40))
    account_ref: Mapped[str] = mapped_column(String(64))
    cash: Mapped[Decimal] = mapped_column(ExactDecimal())


class BrokerPosition(Record, Base):
    __tablename__ = "broker_positions"
    __table_args__ = (UniqueConstraint("account_id", "symbol"),)
    account_id: Mapped[str] = mapped_column(ForeignKey("broker_accounts.id"))
    symbol: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[Decimal] = mapped_column(ExactDecimal())
    average_entry: Mapped[Decimal] = mapped_column(ExactDecimal())
    realized_pnl: Mapped[Decimal] = mapped_column(ExactDecimal())
    fees: Mapped[Decimal] = mapped_column(ExactDecimal())


class BrokerOrder(Record, Base):
    __tablename__ = "broker_orders"
    __table_args__ = (UniqueConstraint("account_id", "client_order_id", name="uq_broker_order_client_account"),
                      UniqueConstraint("broker", "environment", "account_id", "broker_order_id", name="uq_broker_order_remote_account"))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    signal_id: Mapped[str] = mapped_column(ForeignKey("signals.id"))
    risk_decision_id: Mapped[str] = mapped_column(ForeignKey("risk_decisions.id"))
    account_id: Mapped[str] = mapped_column(ForeignKey("broker_accounts.id"))
    client_order_id: Mapped[str] = mapped_column(String(64))
    broker_order_id: Mapped[str | None] = mapped_column(String(100))
    broker: Mapped[str] = mapped_column(String(40))
    environment: Mapped[str] = mapped_column(String(40))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[Decimal] = mapped_column(ExactDecimal())
    limit_price: Mapped[Decimal | None] = mapped_column(ExactDecimal())
    state: Mapped[str] = mapped_column(String(30))
    filled_quantity: Mapped[Decimal] = mapped_column(ExactDecimal())
    quote_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON)


class BrokerEventRow(Record, Base):
    __tablename__ = "broker_events"
    __table_args__ = (UniqueConstraint("order_id", "broker_event_id", name="uq_broker_event_order"),)
    order_id: Mapped[str] = mapped_column(ForeignKey("broker_orders.id"))
    broker_event_id: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(30))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(String(300))
    disposition: Mapped[str] = mapped_column(String(30))
    payload_hash: Mapped[str] = mapped_column(String(64))


class BrokerFillRow(Record, Base):
    __tablename__ = "broker_fills"
    __table_args__ = (UniqueConstraint("order_id", "execution_id", name="uq_broker_fill_order"),)
    order_id: Mapped[str] = mapped_column(ForeignKey("broker_orders.id"))
    event_id: Mapped[str] = mapped_column(ForeignKey("broker_events.id"), unique=True)
    execution_id: Mapped[str] = mapped_column(String(100))
    quantity: Mapped[Decimal] = mapped_column(ExactDecimal())
    price: Mapped[Decimal] = mapped_column(ExactDecimal())
    fee: Mapped[Decimal] = mapped_column(ExactDecimal())
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BrokerCommand(Record, Base):
    __tablename__ = "broker_commands"
    __table_args__ = (UniqueConstraint("order_id", "command_type", "id", name="uq_broker_command_attempt"),)
    order_id: Mapped[str] = mapped_column(ForeignKey("broker_orders.id"))
    account_id: Mapped[str] = mapped_column(ForeignKey("broker_accounts.id"))
    command_type: Mapped[str] = mapped_column(String(16))
    client_order_id: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    safe_error_category: Mapped[str | None] = mapped_column(String(80))
    correlation_id: Mapped[str | None] = mapped_column(String(160))
