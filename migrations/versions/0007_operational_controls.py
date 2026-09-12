"""Persist pause and kill-switch operator controls."""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("operational_controls",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))


def downgrade(): op.drop_table("operational_controls")
