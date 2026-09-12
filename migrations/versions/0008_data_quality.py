"""Persist market-data quality failures for operator review."""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("data_quality_events",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(80), nullable=False),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("detail", sa.String(300), nullable=True),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade(): op.drop_table("data_quality_events")
