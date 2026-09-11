"""Preserve strategy source as well as its hash. Existing history remains unchanged."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("strategy_versions", sa.Column("source_code", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("strategy_versions", "source_code")
