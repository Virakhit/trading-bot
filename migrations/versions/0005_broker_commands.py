"""Durable broker command/attempt boundary for test-environment network operations."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "broker_commands",
        sa.Column("order_id", sa.String(36), sa.ForeignKey("broker_orders.id"), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("broker_accounts.id"), nullable=False),
        sa.Column("command_type", sa.String(16), nullable=False),
        sa.Column("client_order_id", sa.String(64), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("safe_error_category", sa.String(80), nullable=True),
        sa.Column("correlation_id", sa.String(160), nullable=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("order_id", "command_type", "id", name="uq_broker_command_attempt"),
    )


def downgrade():
    op.drop_table("broker_commands")
