"""Persistent external-to-internal broker identity resolution."""
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database.models import BrokerOrder


class PersistentOrderResolver:
    def __init__(self, engine, account_id: str):
        self.engine, self.account_id = engine, account_id

    def resolve(self, client_order_id: str) -> str | None:
        with Session(self.engine) as session:
            return session.scalar(select(BrokerOrder.id).where(
                BrokerOrder.account_id == self.account_id,
                BrokerOrder.client_order_id == client_order_id))

    def client_id(self, internal_order_id: str) -> str | None:
        with Session(self.engine) as session:
            return session.scalar(select(BrokerOrder.client_order_id).where(
                BrokerOrder.account_id == self.account_id,
                BrokerOrder.id == internal_order_id))

    def require(self, client_order_id: str) -> str:
        order_id = self.resolve(client_order_id)
        if order_id is None:
            raise LookupError(f"LOCAL_MISSING client_order_id={client_order_id}")
        return order_id
