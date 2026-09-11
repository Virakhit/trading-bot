"""Persist exact broker lifecycle state without rewriting Phase 1 history."""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("broker_accounts",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("broker", sa.String(40), nullable=False),
        sa.Column("environment", sa.String(40), nullable=False),
        sa.Column("account_ref", sa.String(64), nullable=False),
        sa.Column("cash", sa.String(80), nullable=False),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "broker", "environment"))
    op.create_table("broker_positions",
        sa.Column("account_id", sa.String(36), sa.ForeignKey("broker_accounts.id"), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("quantity", sa.String(80), nullable=False),
        sa.Column("average_entry", sa.String(80), nullable=False),
        sa.Column("realized_pnl", sa.String(80), nullable=False),
        sa.Column("fees", sa.String(80), nullable=False),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "symbol"))
    op.create_table("broker_orders",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("signal_id", sa.String(36), sa.ForeignKey("signals.id"), nullable=False),
        sa.Column("risk_decision_id", sa.String(36), sa.ForeignKey("risk_decisions.id"), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("broker_accounts.id"), nullable=False),
        sa.Column("client_order_id", sa.String(64), nullable=False),
        sa.Column("broker_order_id", sa.String(100), nullable=True),
        sa.Column("broker", sa.String(40), nullable=False),
        sa.Column("environment", sa.String(40), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("quantity", sa.String(80), nullable=False),
        sa.Column("limit_price", sa.String(80), nullable=True),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("filled_quantity", sa.String(80), nullable=False),
        sa.Column("quote_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("client_order_id"),
        sa.UniqueConstraint("broker", "environment", "broker_order_id"))
    op.create_table("broker_events",
        sa.Column("order_id", sa.String(36), sa.ForeignKey("broker_orders.id"), nullable=False),
        sa.Column("broker_event_id", sa.String(100), nullable=False, unique=True),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("disposition", sa.String(30), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("broker_fills",
        sa.Column("order_id", sa.String(36), sa.ForeignKey("broker_orders.id"), nullable=False),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("broker_events.id"), nullable=False, unique=True),
        sa.Column("execution_id", sa.String(100), nullable=False, unique=True),
        sa.Column("quantity", sa.String(80), nullable=False),
        sa.Column("price", sa.String(80), nullable=False),
        sa.Column("fee", sa.String(80), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("broker_fills")
    op.drop_table("broker_events")
    op.drop_table("broker_orders")
    op.drop_table("broker_positions")
    op.drop_table("broker_accounts")
