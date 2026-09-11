"""Scope broker identifiers by account and align the command constraint."""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade():
    with op.batch_alter_table("broker_orders", recreate="always", naming_convention=NAMING) as batch:
        batch.drop_constraint("uq_broker_orders_client_order_id", type_="unique")
        batch.drop_constraint("uq_broker_orders_broker", type_="unique")
        batch.create_unique_constraint("uq_broker_order_client_account", ["account_id", "client_order_id"])
        batch.create_unique_constraint("uq_broker_order_remote_account", ["broker", "environment", "account_id", "broker_order_id"])
    with op.batch_alter_table("broker_events", recreate="always", naming_convention=NAMING) as batch:
        batch.drop_constraint("uq_broker_events_broker_event_id", type_="unique")
        batch.create_unique_constraint("uq_broker_event_order", ["order_id", "broker_event_id"])
    with op.batch_alter_table("broker_fills", recreate="always", naming_convention=NAMING) as batch:
        batch.drop_constraint("uq_broker_fills_execution_id", type_="unique")
        batch.create_unique_constraint("uq_broker_fill_order", ["order_id", "execution_id"])


def downgrade():
    with op.batch_alter_table("broker_fills", recreate="always") as batch:
        batch.drop_constraint("uq_broker_fill_order", type_="unique")
        batch.create_unique_constraint(None, ["execution_id"])
    with op.batch_alter_table("broker_events", recreate="always") as batch:
        batch.drop_constraint("uq_broker_event_order", type_="unique")
        batch.create_unique_constraint(None, ["broker_event_id"])
    with op.batch_alter_table("broker_orders", recreate="always") as batch:
        batch.drop_constraint("uq_broker_order_client_account", type_="unique")
        batch.drop_constraint("uq_broker_order_remote_account", type_="unique")
        batch.create_unique_constraint(None, ["client_order_id"])
        batch.create_unique_constraint("uq_broker_orders_broker", ["broker", "environment", "broker_order_id"])
