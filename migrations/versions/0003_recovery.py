"""Atomic replay checkpoints and semantic signal identity; preserve existing runs."""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runs", sa.Column("checkpoint", sa.JSON(), nullable=True))
    op.add_column("runs", sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))
    with op.batch_alter_table("signals") as batch:
        batch.add_column(sa.Column("decision_key", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_signal_decision", ["run_id", "decision_key"])


def downgrade():
    with op.batch_alter_table("signals") as batch:
        batch.drop_constraint("uq_signal_decision", type_="unique")
        batch.drop_column("decision_key")
    op.drop_column("runs", "revision")
    op.drop_column("runs", "checkpoint")
