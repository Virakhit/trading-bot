"""Persist incremental market processing checkpoints."""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "market_checkpoints",
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("last_processed_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_processed_hash", sa.String(64), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "symbol", "source"),
    )


def downgrade():
    op.drop_table("market_checkpoints")
